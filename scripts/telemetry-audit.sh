#!/usr/bin/env bash
# Record what the Riot Client and League send, through mitmproxy, once.
#
#   scripts/telemetry-audit.sh [seconds]     (default: until Ctrl+C)
#
# A dedicated mitmproxy CA (kept in $LEAGUE_LINUX_HOME/audit/mitmproxy, mode 0700) is
# trusted in the guest's System keychain for the duration of the run and removed again
# afterwards. Guest processes reach the proxy through http(s)_proxy; local traffic
# (127.0.0.1, localhost) bypasses it. Requests are written, with credentials, tokens and
# account names redacted, to $LEAGUE_LINUX_HOME/audit/telemetry-<time>.jsonl (mode 0600).
# Hosts the client refuses to talk to through the proxy are listed as pinned.
set -euo pipefail

home=${LEAGUE_LINUX_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/league-linux}
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
launcher=${LEAGUE_LINUX_BIN:-$HOME/.local/bin/league-linux}
mitmdump=${MITMDUMP:-$(command -v mitmdump || echo "$HOME/.local/bin/mitmdump")}
port=${LEAGUE_AUDIT_PORT:-18080}
duration=${1:-}
audit="$home/audit"
confdir="$audit/mitmproxy"
log="$audit/telemetry-$(date +%Y%m%d-%H%M%S).jsonl"
guest_ca=/Users/Shared/.league-audit-ca.pem
host_ca="$home/prefix$guest_ca"
proxy="http://127.0.0.1:$port"
proxy_vars="http_proxy https_proxy HTTP_PROXY HTTPS_PROXY no_proxy NO_PROXY"

[ -x "$mitmdump" ] || { echo "mitmdump not found; install mitmproxy (uv tool install --python 3.12 mitmproxy)" >&2; exit 1; }
mkdir -p "$confdir"
chmod 700 "$audit" "$confdir"

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

# First start creates the CA in confdir.
if [ ! -f "$confdir/mitmproxy-ca-cert.pem" ]; then
  timeout 5 "$mitmdump" --set confdir="$confdir" --listen-host 127.0.0.1 --listen-port "$port" >/dev/null 2>&1 || true
fi
[ -f "$confdir/mitmproxy-ca-cert.pem" ] || { echo "mitmproxy did not create its CA in $confdir" >&2; exit 1; }

"$launcher" stop >/dev/null 2>&1 || true
install -m 644 "$confdir/mitmproxy-ca-cert.pem" "$host_ca"
echo "Adding the audit CA to the guest System keychain..."
# Darling ships no System keychain, and its securityd cannot store admin trust
# settings (-2070). The Riot Client loads every certificate of the system keychain
# into its own BoringSSL store, so membership is what matters there.
cat > "$home/prefix/Users/Shared/.league-audit-ca.sh" <<EOF_CA
mkdir -p /Library/Keychains
[ -f /Library/Keychains/System.keychain ] || security create-keychain -p '' /Library/Keychains/System.keychain
security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain $guest_ca 2>/dev/null ||
  security add-certificates -k /Library/Keychains/System.keychain $guest_ca 2>/dev/null
security find-certificate -c mitmproxy /Library/Keychains/System.keychain >/dev/null
EOF_CA
guest /bin/bash /Users/Shared/.league-audit-ca.sh || { echo "Could not add the CA to the guest keychain" >&2; exit 1; }

cleanup() {
  set +e
  "$launcher" stop >/dev/null 2>&1
  systemctl --user unset-environment $proxy_vars RCLOL_KEEP_HOST_ENV
  [ -n "${mitm_pid:-}" ] && kill "$mitm_pid" 2>/dev/null && wait "$mitm_pid" 2>/dev/null
  echo "Removing the audit CA from the guest..."
  guest /usr/bin/security remove-trusted-cert -d "$guest_ca" >/dev/null 2>&1
  guest /usr/bin/security delete-certificate -c mitmproxy /Library/Keychains/System.keychain >/dev/null 2>&1
  rm -f "$host_ca" "$home/prefix/Users/Shared/.league-audit-ca.sh"
  echo "Log: $log"
  [ -s "$log" ] && python3 "$here/telemetry-audit-summary.py" "$log"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

LEAGUE_AUDIT_LOG="$log" "$mitmdump" --set confdir="$confdir" --listen-host 127.0.0.1 \
  --listen-port "$port" --set ssl_insecure=false -q -s "$here/telemetry_audit_addon.py" &
mitm_pid=$!
sleep 3
kill -0 "$mitm_pid" 2>/dev/null || { mitm_pid=; echo "mitmdump did not start on port $port" >&2; exit 1; }

# The managed session reads the user manager's environment when it starts.
systemctl --user set-environment http_proxy="$proxy" https_proxy="$proxy" HTTP_PROXY="$proxy" \
  HTTPS_PROXY="$proxy" no_proxy=127.0.0.1,localhost NO_PROXY=127.0.0.1,localhost \
  RCLOL_KEEP_HOST_ENV="$proxy_vars"
"$launcher" start
systemctl --user unset-environment $proxy_vars RCLOL_KEEP_HOST_ENV
echo "Recording to $log. Use the client (log in, open the store, start a practice game...)."
if [ -n "$duration" ]; then
  sleep "$duration"
else
  echo "Press Ctrl+C to stop."
  wait "$mitm_pid"
fi
