#!/usr/bin/env python3
"""Summarize a call trace: what one image's code asked the runtime for, and what it got.

    scripts/call-trace-summary.py [<darling-call-trace.log>]

The log is written by the runtime while <prefix>/private/etc/darling-call-trace.conf
names an image (scripts/call-trace.sh sets that up): one line per BSD system call, Mach
trap, Mach message and CPUID made with that image's code on the calling thread's stack.
This groups them into the questions that identify a machine -- sysctl names, files
looked up, IOKit and other Mach requests, CPUID leaves -- with the answer each one got,
and lists first the ones that failed: those are what a Mac would have answered and this
runtime did not.
"""
import collections
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.environ.get("LEAGUE_LINUX_HOME") or os.path.join(
    os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "league-linux")
DEFAULT_LOG = os.path.join(HOME, "prefix/private/etc/darling-call-trace.log")
NAMES = json.load(open(os.path.join(HERE, "call-trace-names.json")))

# BSD calls whose first (or, for the *at family, second) argument is a path.
PATH_ARG = {"open": 0, "open_nocancel": 0, "stat": 0, "lstat": 0, "stat64": 0, "lstat64": 0, "access": 0,
            "readlink": 0, "getattrlist": 0, "statfs": 0, "statfs64": 0, "getxattr": 0, "listxattr": 0,
            "pathconf": 0, "chdir": 0, "execve": 0, "posix_spawn": 1, "stat64_extended": 0,
            "lstat64_extended": 0, "openat": 1, "openat_nocancel": 1, "fstatat": 1, "fstatat64": 1,
            "faccessat": 1, "readlinkat": 1, "getattrlistat": 1, "unlink": 0, "mkdir": 0, "rename": 0}
ERRNO = {1: "EPERM", 2: "ENOENT", 3: "ESRCH", 5: "EIO", 9: "EBADF", 12: "ENOMEM", 13: "EACCES", 14: "EFAULT",
         17: "EEXIST", 20: "ENOTDIR", 22: "EINVAL", 25: "ENOTTY", 34: "ERANGE", 45: "ENOTSUP", 78: "ENOSYS",
         93: "ENOATTR", 102: "EOPNOTSUPP"}
FIELD = re.compile(r' (\w+)=("(?:[^"]*)"|\S*)')


def signed(text):
    value = int(text, 16)
    return value - (1 << 64) if value >> 63 else value


def result(ret):
    if ret is None:
        return "?"
    value = signed(ret)
    if -200 < value < 0:
        return ERRNO.get(-value, "errno %d" % -value)
    return "ok" if value == 0 else "ok (%d)" % value if value < 1 << 20 else "ok"


def parse(line):
    head, _, rest = line.partition(" from=")
    parts = head.split()
    if len(parts) != 5:
        return None
    fields = {key: value.strip('"') for key, value in FIELD.findall(" from=" + rest)}
    return {"time": parts[0], "pid": parts[1], "tid": parts[2], "kind": parts[3], "nr": parts[4], **fields}


def shown(counter, limit=None, width=60):
    for (key, answer), count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:limit]:
        print("  %6d  %-*s  %s" % (count, width, key[:width + 40], answer))


def main(path):
    sysctls = collections.Counter()
    paths = collections.Counter()
    other = collections.Counter()
    traps = collections.Counter()
    messages = collections.Counter()
    replies = collections.defaultdict(set)
    cpuid = collections.Counter()
    failed = collections.Counter()
    pending = {}  # thread -> the request it is waiting on
    callers = collections.Counter()
    lines = 0
    for line in open(path, errors="replace"):
        call = parse(line.rstrip("\n"))
        if not call:
            continue
        lines += 1
        callers[call.get("from", "").split(",")[0].split(":")[-1]] += 1
        kind, nr = call["kind"], call["nr"]
        if kind == "bsd":
            name = NAMES["bsd"].get(nr, "bsd#" + nr)
            answer = result(call.get("ret"))
            if name in ("sysctlbyname", "sysctl"):
                what = call.get("s0") if name == "sysctlbyname" else "mib " + call.get("mib", "?")
                value = call.get("outs") or call.get("out", "")[:32]
                key = (what or "?", answer if answer != "ok" else "= " + (value or "(empty)"))
                sysctls[key] += 1
            elif name in PATH_ARG:
                key = ("%s %s" % (name, call.get("s%d" % PATH_ARG[name], "?")), answer)
                paths[key] += 1
            else:
                key = (name, answer)
                other[key] += 1
            if not answer.startswith(("ok", "=")) and not key[1].startswith("="):
                failed[(key[0], answer)] += 1
        elif kind == "mach":
            traps[(NAMES["mach"].get(nr, "trap#" + nr), result(call.get("ret")))] += 1
        elif kind == "msg-send":
            name = NAMES["mig"].get(call.get("msgid", ""), "msg#" + call.get("msgid", "?"))
            request = "%s %s" % (name, call.get("strs", "")[:90])
            pending[call["tid"]] = request
            messages[(request, "")] += 1
        elif kind == "msg-reply":
            request = pending.pop(call["tid"], None)
            if request and call.get("strs"):
                replies[request].add(call["strs"][:100])
        elif kind == "cpuid":
            cpuid[("leaf 0x%x sub %s" % (int(nr), call.get("subleaf", "?")),
                   "eax=%s ebx=%s ecx=%s edx=%s" % tuple(call.get(r, "?") for r in ("eax", "ebx", "ecx", "edx")))] += 1
    print("%d calls from the traced image (%d distinct call sites)" % (lines, len(callers)))
    print("\n== Asked for and not answered (a Mac answers most of these)")
    shown(failed)
    if not failed:
        print("  none")
    print("\n== sysctl: name, answer")
    shown(sysctls)
    print("\n== Files and directories looked up")
    shown(paths, width=90)
    print("\n== Mach requests (IOKit and others), with what the request named")
    for (request, _), count in sorted(messages.items(), key=lambda item: (-item[1], item[0]))[:150]:
        print("  %6d  %s" % (count, request))
        for answer in sorted(replies.get(request, ()))[:2]:
            print("          -> %s" % answer)
    print("\n== CPUID leaves, with the registers returned")
    shown(cpuid, limit=60, width=28)
    print("\n== Mach traps")
    shown(traps, width=40)
    print("\n== Other BSD calls")
    shown(other, width=40)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_LOG)
