#!/usr/bin/env python3
"""Summarize a telemetry-audit JSONL log: hosts, endpoints, pinned hosts, identity fields.

    scripts/telemetry-audit-summary.py <log.jsonl> [<connections.tsv>]

The optional connections file (written next to the log by telemetry-audit.sh) lists the
outside connections the session's processes opened without the proxy; those requests are
missing from the log, so it shows where the log is blind.

"Identity fields" are request values whose key or content names the machine: OS and
version, hardware model, CPU, GPU, memory, hostname, serial/UUID/MAC-like identifiers,
and any mention of Linux, Darling, Wine or a host path. These are what a Mac-compatible
identity has to get right.
"""
import collections
import concurrent.futures
import json
import re
import socket
import sys
from urllib.parse import urlsplit

IDENTITY_KEY = re.compile(r"(os|platform|version|build|model|machine|hardware|cpu|processor|gpu|"
                          r"graphics|renderer|vendor|chipset|memory|ram|hostname|computer|device|"
                          r"serial|uuid|udid|hwid|mac(addr|_address)?|fingerprint|locale|timezone|"
                          r"display|resolution|screen|driver)", re.I)
# Short words only as whole words: "mesa" is also inside "TimeSample".
TELLTALE = re.compile(r"(linux|darling|\bwine\b|\bproton\b|/home/|/Volumes/SystemRoot|nvidia|geforce|"
                      r"i5-12500H|12th gen|iris xe|\bmesa\b|vulkan|\bx11\b|wayland|pop-os|ubuntu|"
                      r"\bnix\b|/nix/store)", re.I)


def walk(value, path=""):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from walk(item, path + "." + str(key) if path else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from walk(item, "%s[%d]" % (path, index))
    else:
        yield path, value


def main(path):
    hosts = collections.Counter()
    endpoints = collections.Counter()
    pinned = collections.Counter()
    errors = collections.Counter()
    identity = collections.defaultdict(set)
    telltales = collections.defaultdict(set)
    lookups = collections.Counter()
    raw_tcp = collections.Counter()
    passthrough = collections.Counter()
    for line in open(path, encoding="utf-8"):
        record = json.loads(line)
        if "dns" in record:
            lookups.update(name.rstrip(".") for name in record["dns"])
            continue
        if "tcp" in record:
            raw_tcp[record["tcp"]] += 1
            continue
        if "passthrough" in record:
            passthrough[record["passthrough"]] += 1
            continue
        if "tls_rejected_by_client" in record:
            pinned[record["tls_rejected_by_client"]] += 1
            continue
        url = urlsplit(record.get("url", ""))
        if "error" in record:
            errors[url.netloc] += 1
            continue
        hosts[url.netloc] += 1
        endpoints["%s %s%s" % (record.get("method"), url.netloc, url.path)] += 1
        sources = [("header", record.get("request_headers") or {}), ("body", record.get("request_body")),
                   ("query", url.query)]
        for where, value in sources:
            for key, item in walk(value):
                text = str(item)
                endpoint = url.netloc + url.path
                match = TELLTALE.search(text) or TELLTALE.search(key)
                if match:
                    if match.string is text:  # show the match in context, not a binary body's start
                        text = text[max(0, match.start() - 60):match.end() + 60]
                    telltales[(where + ":" + key, text[:200])].add(endpoint)
                elif IDENTITY_KEY.search(key.rsplit(".", 1)[-1]) and len(text) < 200:
                    identity[(where + ":" + key, text)].add(endpoint)

    print("\n== Hosts (%d)" % len(hosts))
    for host, count in hosts.most_common():
        print("%6d  %s" % (count, host))
    if pinned:
        print("\n== Refused our CA (pinned or own trust store; later connections passed through unrecorded)")
        for host, count in pinned.most_common():
            print("%6d  %s  (+%d passed through)" % (count, host, passthrough[host]))
    if raw_tcp:
        print("\n== Non-HTTP TCP (endpoints only)")
        for host, count in raw_tcp.most_common():
            print("%6d  %s" % (count, host))
    if lookups:
        print("\n== DNS lookups (%d names)" % len(lookups))
        for name, count in lookups.most_common():
            print("%6d  %s" % (count, name))
    if errors:
        print("\n== Connection errors")
        for host, count in errors.most_common():
            print("%6d  %s" % (count, host))
    print("\n== Endpoints")
    for endpoint, count in endpoints.most_common(60):
        print("%6d  %s" % (count, endpoint))
    print("\n== Machine identity the clients send")
    for (key, value), where in sorted(identity.items()):
        print("  %-55s %-40s  (%s)" % (key[:55], value[:40], ", ".join(sorted(where))[:80]))
    print("\n== Telltales (Linux/Darling/host hardware) -- each of these is a leak")
    if not telltales:
        print("  none")
    for (key, value), where in sorted(telltales.items()):
        print("  %s = %s\n      in %s" % (key, value, ", ".join(sorted(where))[:160]))


def direct_connections(path):
    """Outside connections that did not go through the proxy, from telemetry-audit.sh."""
    seen = collections.Counter()
    for line in open(path, encoding="utf-8"):
        fields = line.rstrip("\n").split("\t")
        if len(fields) == 3:
            seen[tuple(fields)] += 1
    print("\n== Not through mitmproxy (sampled sockets; not in the log above)")
    if not seen:
        print("  none")
        return
    peers = sorted({peer.rsplit(":", 1)[0].strip("[]") for _, peer, _ in seen})
    with concurrent.futures.ThreadPoolExecutor(16) as pool:
        names = dict(zip(peers, pool.map(reverse_name, peers)))
    for (proto, peer, process), samples in sorted(seen.items(), key=lambda item: (item[0][2], item[0][1])):
        address = peer.rsplit(":", 1)[0].strip("[]")
        print("  %-4s %-40s %-18s %s" % (proto, peer, process[:18], names.get(address) or ""))


def reverse_name(address):
    try:
        return socket.gethostbyaddr(address)[0]
    except (OSError, UnicodeError):
        return ""


if __name__ == "__main__":
    socket.setdefaulttimeout(3)
    main(sys.argv[1])
    if len(sys.argv) > 2:
        direct_connections(sys.argv[2])
