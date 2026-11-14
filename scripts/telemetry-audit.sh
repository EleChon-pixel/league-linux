#!/usr/bin/env bash
# Record what the Riot Client and League send, through mitmproxy, once.
#
#   scripts/telemetry-audit.sh [seconds]     (default: until Ctrl+C)
#
# Transparent mode (default when the runtime supports darling-lol's RCLOL_NETNS_HOOK):
# the session runs in its own network namespace (scripts/audit-netns.sh) whose TCP and
# DNS all go through a WireGuard tunnel into mitmproxy, so clients that ignore proxy
# settings (Riot's login, entitlements, platform and chat services) are recorded too.
# UDP other than DNS (game traffic) is not tunnelled. Proxy mode (LEAGUE_AUDIT_MODE=proxy,
# or older runtimes) only sees clients that honour http(s)_proxy.
#
# A dedicated mitmproxy CA (kept in $LEAGUE_LINUX_HOME/audit/mitmproxy, mode 0700) is
# trusted in the guest for the duration of the run (System keychain plus admin trust
# settings, as `security add-trusted-cert -d` does on a Mac) and removed again afterwards.
# Requests are written, with credentials, tokens and account names redacted, to
# $LEAGUE_LINUX_HOME/audit/telemetry-<time>.jsonl (mode 0600). Hosts that refuse the CA
# are listed as pinned and then passed through untouched, so they keep working.
# In transparent mode the session also runs with SSLKEYLOGFILE set and its TCP is captured:
# the League client and the game refuse the CA, but their libcurl logs each connection's
# TLS keys, so scripts/telemetry-audit-decrypt.py reads their requests afterwards (to
# telemetry-<time>.keylog.jsonl, same redaction). The capture and the keys are deleted
# after that unless LEAGUE_AUDIT_KEEP_CAPTURE=1. Vanguard's own TLS library ignores the
# variable and encrypts its payloads itself, so those stay unread.
# Connections that did not go through mitmproxy (sampled from the session's sockets every
# 2 s; UDP only when connected) go to telemetry-<time>.connections.tsv.
set -euo pipefail

home=${LEAGUE_LINUX_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/league-linux}
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
launcher=${LEAGUE_LINUX_BIN:-$HOME/.local/bin/league-linux}
mitmdump=${MITMDUMP:-$(command -v mitmdump || echo "$HOME/.local/bin/mitmdump")}
port=${LEAGUE_AUDIT_PORT:-18080}
wg_port=${LEAGUE_AUDIT_WG_PORT:-51820}
duration=${1:-}
audit="$home/audit"
confdir="$audit/mitmproxy"
log="$audit/telemetry-$(date +%Y%m%d-%H%M%S).jsonl"
connections="${log%.jsonl}.connections.tsv"
pcap="${log%.jsonl}.pcap"
keys="${log%.jsonl}.tls-keys.log"
decrypted="${log%.jsonl}.keylog.jsonl"
guest_keys=/Users/Shared/.league-audit-tls-keys.log
host_keys="$home/prefix$guest_keys"
pasta_pid="$audit/.pasta.pid"
netns_pid="$audit/.netns.pid"
active="$audit/.active"
unit=league-linux-session.service
guest_ca=/Users/Shared/.league-audit-ca.pem
host_ca="$home/prefix$guest_ca"
proxy="http://127.0.0.1:$port"
proxy_vars="http_proxy https_proxy HTTP_PROXY HTTPS_PROXY no_proxy NO_PROXY RCLOL_KEEP_HOST_ENV"
netns_vars="RCLOL_NETNS_HOOK LEAGUE_AUDIT_WG_CONF LEAGUE_AUDIT_WG_PORT LEAGUE_AUDIT_PASTA_PID LEAGUE_AUDIT_NETNS_PID LEAGUE_AUDIT_PCAP SSLKEYLOGFILE PASTA WG"

[ -x "$mitmdump" ] || { echo "mitmdump not found; install mitmproxy (uv tool install --python 3.12 mitmproxy)" >&2; exit 1; }
mkdir -p "$confdir"
chmod 700 "$audit" "$confdir"

mode=${LEAGUE_AUDIT_MODE:-}
if [ -z "$mode" ]; then
  if grep -q RCLOL_NETNS_HOOK "$home/package/bin/.darling-lol-wrapped" 2>/dev/null; then
    mode=transparent
  else
    mode=proxy
    echo "This runtime has no RCLOL_NETNS_HOOK (run league-linux prepare); proxy mode only." >&2
  fi
fi
case "$mode" in
  transparent)
    nix=$(command -v nix || echo "$HOME/.nix-profile/bin/nix")
    nix_build() { "$nix" --extra-experimental-features 'nix-command flakes' build --no-link --print-out-paths "$1"; }
    PASTA=${PASTA:-$(nix_build nixpkgs#passt | grep -v -- '-man$' | head -1)/bin/pasta}
    WG=${WG:-$(nix_build nixpkgs#wireguard-tools | grep -v -- '-man$' | head -1)/bin/wg}
    [ -x "$PASTA" ] && [ -x "$WG" ] || { echo "pasta or wg missing ($PASTA, $WG)" >&2; exit 1; }
    TSHARK=${TSHARK:-$(command -v tshark || echo "$(nix_build nixpkgs#wireshark-cli | head -1)/bin/tshark")}
    [ -x "$TSHARK" ] || { echo "tshark missing ($TSHARK)" >&2; exit 1; }
    export TSHARK
    for tool in nsenter ip nft tcpdump; do
      command -v "$tool" >/dev/null || { echo "$tool is required for transparent mode" >&2; exit 1; }
    done
    mitm_mode=(--mode "wireguard@127.0.0.1:$wg_port");;
  proxy) mitm_mode=(--listen-host 127.0.0.1 --listen-port "$port");;
  *) echo "LEAGUE_AUDIT_MODE must be transparent or proxy" >&2; exit 2;;
esac

guest() { # run one command in the guest through the launcher's backend environment
  LEAGUE_LINUX_ROOT="$here/.." python3 - "$@" <<'EOF'
import os, subprocess, sys
sys.path.insert(0, os.environ["LEAGUE_LINUX_ROOT"])
import league_linux as L
m = L.Manager()
env = m.environment()
backend = m.backend()
env["DARLING_PACKAGE_ID"] = str(backend.resolve().parent.parent / "share/darling-lol")
command = " ".join("'" + a.replace("'", "'\\''") + "'" for a in sys.argv[1:])
script = ("PATH=$DARLING_RUNNER_DIR:$PATH darling shell " + command +
          "; status=$?; PATH=$DARLING_RUNNER_DIR:$PATH darling shutdown >/dev/null 2>&1; exit $status")
sys.exit(subprocess.run([str(backend), "--shell", "-c", script], env=env, cwd=str(m.workspace)).returncode)
EOF
}

# First start creates the CA (and the WireGuard keys) in confdir.
if [ ! -f "$confdir/mitmproxy-ca-cert.pem" ] || { [ "$mode" = transparent ] && [ ! -f "$confdir/wireguard.conf" ]; }; then
  timeout 5 "$mitmdump" --set confdir="$confdir" "${mitm_mode[@]}" >/dev/null 2>&1 || true
fi
[ -f "$confdir/mitmproxy-ca-cert.pem" ] || { echo "mitmproxy did not create its CA in $confdir" >&2; exit 1; }

"$launcher" stop >/dev/null 2>&1 || true
install -m 644 "$confdir/mitmproxy-ca-cert.pem" "$host_ca"
echo "Adding the audit CA to the guest System keychain..."
# Darling ships no System keychain, so create one. Chromium (the Riot Client UI, the
# League client UI) trusts a local root only through admin trust settings; the Riot
# Client's BoringSSL loads every System keychain certificate. Runtimes older than
# darling-lol's ocspd-less trust settings cannot store them (-2070): then only the
# keychain copy is added and the Chromium hosts show up as refusing the CA.
cat > "$home/prefix/Users/Shared/.league-audit-ca.sh" <<EOF_CA
mkdir -p /Library/Keychains
[ -f /Library/Keychains/System.keychain ] || security create-keychain -p '' /Library/Keychains/System.keychain
if security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain $guest_ca 2>/dev/null; then
  echo "  trusted as an admin root (Chromium and BoringSSL)"
else
  echo "  admin trust settings unavailable; keychain only (BoringSSL)"
fi
security find-certificate -c mitmproxy /Library/Keychains/System.keychain >/dev/null
EOF_CA
guest /bin/bash /Users/Shared/.league-audit-ca.sh || { echo "Could not add the CA to the guest keychain" >&2; exit 1; }

# Connections of the session that did not go through mitmproxy: in proxy mode every
# outside connection of its processes; in transparent mode the UDP that leaves the
# namespace untunnelled (not DNS, not the tunnel itself).
watch_connections() {
  local awk_filter='
    $6 ~ /^(127\.|\[::1\]|\[::ffff:127\.|\*|0\.0\.0\.0:|\[::\]:)/ { next }
    mode == "transparent" && ($1 != "udp" || $6 ~ /^10\.0\.0\.53:/ || $6 ~ (":" wg "$")) { next }
    {
      n = split($0, part, "pid=")
      for (i = 2; i <= n; i++) {
        split(part[i], f, ",")
        if (mode == "transparent" || index(pids, " " f[1] " ")) {
          name = $0; sub(/.*users:\(\("/, "", name); sub(/".*/, "", name)
          print $1 "\t" $6 "\t" name
          break
        }
      }
    }'
  while sleep 2; do
    if [ "$mode" = transparent ]; then
      [ -s "$netns_pid" ] || continue
      nsenter --target "$(cat "$netns_pid")" --user --net ss -tunpH 2>/dev/null |
        awk -v mode="$mode" -v wg="$wg_port" -v pids="" "$awk_filter"
    else
      cg=$(systemctl --user show -p ControlGroup --value "$unit" 2>/dev/null) || continue
      [ -n "$cg" ] && [ -d "/sys/fs/cgroup$cg" ] || continue
      pids=$(find "/sys/fs/cgroup$cg" -name cgroup.procs -exec cat {} + 2>/dev/null | tr '\n' ' ')
      ss -tunpH 2>/dev/null | awk -v mode="$mode" -v wg="$wg_port" -v pids=" $pids " "$awk_filter"
    fi
  done >> "$connections"
}

cleanup() {
  set +e
  rm -f "$active"
  [ -n "${watch_pid:-}" ] && kill "$watch_pid" 2>/dev/null
  "$launcher" stop >/dev/null 2>&1
  systemctl --user unset-environment $proxy_vars $netns_vars
  [ -s "$pasta_pid" ] && kill "$(cat "$pasta_pid")" 2>/dev/null
  rm -f "$pasta_pid" "$netns_pid"
  [ -n "${mitm_pid:-}" ] && kill "$mitm_pid" 2>/dev/null && wait "$mitm_pid" 2>/dev/null
  echo "Removing the audit CA from the guest..."
  guest /usr/bin/security remove-trusted-cert -d "$guest_ca" >/dev/null 2>&1
  guest /usr/bin/security delete-certificate -c mitmproxy /Library/Keychains/System.keychain >/dev/null 2>&1
  rm -f "$host_ca" "$home/prefix/Users/Shared/.league-audit-ca.sh"
  [ -f "$host_keys" ] && mv "$host_keys" "$keys" && chmod 600 "$keys"
  logs=("$log")
  if [ -s "$pcap" ] && [ -s "$keys" ]; then
    echo "Reading the pinned hosts with the clients' TLS keys..."
    if python3 "$here/telemetry-audit-decrypt.py" "$pcap" "$keys" "$decrypted" "$log"; then
      logs+=("$decrypted")
      echo "Log: $decrypted"
    fi
  fi
  if [ "${LEAGUE_AUDIT_KEEP_CAPTURE:-0}" = 1 ]; then
    chmod 600 "$pcap" "$keys" 2>/dev/null
  else
    rm -f "$pcap" "$keys"  # together they hold the session's credentials in the clear
  fi
  echo "Log: $log"
  [ -f "$connections" ] && chmod 600 "$connections"
  [ -s "$log" ] && python3 "$here/telemetry-audit-summary.py" "${logs[@]}" "$connections"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

LEAGUE_AUDIT_LOG="$log" "$mitmdump" --set confdir="$confdir" "${mitm_mode[@]}" \
  --set ssl_insecure=false -q -s "$here/telemetry_audit_addon.py" &
mitm_pid=$!
sleep 3
kill -0 "$mitm_pid" 2>/dev/null || { mitm_pid=; echo "mitmdump did not start" >&2; exit 1; }

# The managed session reads the user manager's environment when it starts.
rm -f "$pasta_pid" "$netns_pid"
if [ "$mode" = transparent ]; then
  ( umask 077; : > "$host_keys"; : > "$pcap" )
  systemctl --user set-environment RCLOL_NETNS_HOOK="$here/audit-netns.sh" \
    LEAGUE_AUDIT_WG_CONF="$confdir/wireguard.conf" LEAGUE_AUDIT_WG_PORT="$wg_port" \
    LEAGUE_AUDIT_PASTA_PID="$pasta_pid" LEAGUE_AUDIT_NETNS_PID="$netns_pid" PASTA="$PASTA" WG="$WG" \
    LEAGUE_AUDIT_PCAP="$pcap" SSLKEYLOGFILE="$guest_keys" RCLOL_KEEP_HOST_ENV=SSLKEYLOGFILE
else
  systemctl --user set-environment http_proxy="$proxy" https_proxy="$proxy" HTTP_PROXY="$proxy" \
    HTTPS_PROXY="$proxy" no_proxy=127.0.0.1,localhost NO_PROXY=127.0.0.1,localhost \
    RCLOL_KEEP_HOST_ENV="${proxy_vars% RCLOL_KEEP_HOST_ENV}"
fi
"$launcher" start
systemctl --user unset-environment $proxy_vars $netns_vars
: > "$connections"
watch_connections &
watch_pid=$!
# `league-linux play` reads this and leaves the recorded session alone instead of
# restarting it outside the audit.
echo $$ > "$active"
echo "Recording ($mode mode) to $log. Use the client (log in, open the store, start a practice game...)."
[ -n "$duration" ] || echo "Press Ctrl+C or quit League to stop."
# Short sleeps in the background: bash runs the INT/TERM trap only between commands, so a
# foreground `sleep "$duration"` held off Ctrl+C and kill until it finished.
deadline=$(( $(date +%s) + ${duration:-0} ))
while kill -0 "$mitm_pid" 2>/dev/null; do
  sleep 2 & wait $!
  case $(systemctl --user is-active "$unit" 2>/dev/null) in
    active|activating|reloading) ;;
    *) echo "The session ended."; break;;
  esac
  if [ -n "$duration" ] && [ "$(date +%s)" -ge "$deadline" ]; then break; fi
done
