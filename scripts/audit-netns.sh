#!/usr/bin/env bash
# Network for an audited session (darling-lol RCLOL_NETNS_HOOK). Called on the host with
# the PID of a process in the session's fresh network namespace, before Darling starts:
#
#   - pasta gives the namespace IPv4 to the outside (no inbound ports);
#   - a WireGuard tunnel into mitmproxy's WireGuard mode carries every TCP connection,
#     so clients that ignore the proxy settings are recorded too;
#   - DNS for the host's stub resolver (the guest's resolv.conf points at it) goes to
#     mitmproxy's resolver at 10.0.0.53 through the tunnel, so lookups are recorded.
#
# UDP other than DNS (game traffic, the tunnel itself) leaves through pasta directly.
# Needs: LEAGUE_AUDIT_WG_CONF (mitmproxy's wireguard.conf), LEAGUE_AUDIT_WG_PORT, PASTA, WG,
# and LEAGUE_AUDIT_PASTA_PID, where pasta's PID is written for the caller to stop it.
# LEAGUE_AUDIT_NETNS_PID, if set, receives the namespace PID (to sample its sockets).
# LEAGUE_AUDIT_PCAP, if set, receives a capture of the tunnel's TCP from the first packet
# (telemetry-audit-decrypt.py reads it with the TLS keys the clients log).
set -euo pipefail

pid=$1
conf=${LEAGUE_AUDIT_WG_CONF:?}
port=${LEAGUE_AUDIT_WG_PORT:?}
pasta=${PASTA:?}
pasta_pid=${LEAGUE_AUDIT_PASTA_PID:?}
wg=${WG:?}

[ -z "${LEAGUE_AUDIT_NETNS_PID:-}" ] || echo "$pid" > "$LEAGUE_AUDIT_NETNS_PID"

keys=$(python3 -c 'import json, sys; d = json.load(open(sys.argv[1])); print(d["client_key"], d["server_key"])' "$conf")
client_key=${keys% *}
server_public=$(printf '%s' "${keys#* }" | "$wg" pubkey)

# IPv4 only: the tunnel has no IPv6 address, so IPv6 would bypass it. The session runs
# as a subordinate UID, so pasta cannot watch /proc/<pid>/ns to quit by itself; the
# caller stops it through the PID file.
"$pasta" --config-net -4 --quiet -t none -u none -T none -U none --no-netns-quit \
  --pid "$pasta_pid" --userns "/proc/$pid/ns/user" --netns "/proc/$pid/ns/net"

nsenter --target "$pid" --user --net env CLIENT_KEY="$client_key" SERVER_PUBLIC="$server_public" \
  PORT="$port" WG="$wg" bash -euo pipefail -s <<'EOF'
# pasta maps the gateway address to the host's loopback, where mitmproxy listens.
gateway=$(ip -4 route show default | awk '{print $3; exit}')
[ -n "$gateway" ]
ip link set lo up
ip link add wg0 type wireguard
"$WG" set wg0 private-key <(printf '%s\n' "$CLIENT_KEY") \
  peer "$SERVER_PUBLIC" endpoint "$gateway:$PORT" allowed-ips 0.0.0.0/0 persistent-keepalive 25
ip address add 10.0.0.1/32 dev wg0
ip link set wg0 up
# Every TCP connection that is not local goes through the tunnel.
ip route add default dev wg0 table 51820
ip rule add ipproto tcp lookup 51820 priority 100
ip route add 10.0.0.53/32 dev wg0
echo 1 > /proc/sys/net/ipv4/conf/all/route_localnet
nft -f - <<'NFT'
table ip audit {
  chain output {
    type nat hook output priority -100;
    ip daddr 127.0.0.53 udp dport 53 dnat to 10.0.0.53
    ip daddr 127.0.0.53 tcp dport 53 dnat to 10.0.0.53
  }
  chain postrouting {
    type nat hook postrouting priority 100;
    oifname "wg0" masquerade
  }
}
NFT
EOF

if [ -n "${LEAGUE_AUDIT_PCAP:-}" ]; then
  # The shell opens the file: tcpdump's AppArmor profile cannot write below ~/.local/share.
  ( umask 077
    setsid nsenter --target "$pid" --user --net tcpdump -Z root -i wg0 -s 0 -U -w - tcp \
      > "$LEAGUE_AUDIT_PCAP" 2>/dev/null < /dev/null & )
  sleep 1
fi
