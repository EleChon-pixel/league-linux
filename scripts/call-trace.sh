#!/usr/bin/env bash
# Record what one image's code asks the runtime for (system calls, Mach requests, CPUID).
#
#   scripts/call-trace.sh on [image]   start recording for sessions started from now on
#                                      (image: part of its path; default mvg.framework/)
#   scripts/call-trace.sh off          stop recording
#   scripts/call-trace.sh summary      group the recording into questions and answers
#
# The runtime does the recording (darling-lol's call trace): while the file below names
# an image, every call made with that image's code on the calling thread's stack is
# appended to darling-call-trace.log next to it, with its arguments and the answer. Both
# files are hidden from the guest. Nothing is injected into the traced process and none
# of its files change. Recording costs a few microseconds per traced call; turn it off
# for normal play.
set -euo pipefail

home=${LEAGUE_LINUX_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/league-linux}
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
conf="$home/prefix/private/etc/darling-call-trace.conf"
log="$home/prefix/private/etc/darling-call-trace.log"

case "${1:-}" in
  on)
    image=${2:-mvg.framework/}
    case "$image" in *[!A-Za-z0-9._/-]*|??|?|'') echo "image: three or more of A-Z a-z 0-9 . _ / -" >&2; exit 2;; esac
    ( umask 077; : > "$log"; printf '%s\n' "$image" > "$conf" )
    echo "Recording calls made by '$image' to $log (sessions started from now on).";;
  off)
    rm -f "$conf"
    echo "Call trace off. Log kept: $log";;
  summary)
    python3 "$here/call-trace-summary.py" "$log";;
  *) sed -n '2,7p' "$0" >&2; exit 2;;
esac
