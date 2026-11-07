#!/usr/bin/env python3
"""Summarize a telemetry-audit JSONL log: hosts, endpoints, pinned hosts, identity fields.

    scripts/telemetry-audit-summary.py <log.jsonl>

"Identity fields" are request values whose key or content names the machine: OS and
version, hardware model, CPU, GPU, memory, hostname, serial/UUID/MAC-like identifiers,
and any mention of Linux, Darling, Wine or a host path. These are what a Mac-compatible
identity has to get right.
"""
import collections
import json
import re
import sys
from urllib.parse import urlsplit

IDENTITY_KEY = re.compile(r"(os|platform|version|build|model|machine|hardware|cpu|processor|gpu|"
                          r"graphics|renderer|vendor|chipset|memory|ram|hostname|computer|device|"
                          r"serial|uuid|udid|hwid|mac(addr|_address)?|fingerprint|locale|timezone|"
                          r"display|resolution|screen|driver)", re.I)
TELLTALE = re.compile(r"(linux|darling|wine|proton|/home/|/Volumes/SystemRoot|nvidia|geforce|"
                      r"i5-12500H|12th gen|iris xe|mesa|vulkan|x11|wayland|pop-os|ubuntu|nix)", re.I)


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
    for line in open(path, encoding="utf-8"):
        record = json.loads(line)
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
                if TELLTALE.search(text) or TELLTALE.search(key):
                    telltales[(where + ":" + key, text[:200])].add(endpoint)
                elif IDENTITY_KEY.search(key.rsplit(".", 1)[-1]) and len(text) < 200:
                    identity[(where + ":" + key, text)].add(endpoint)

    print("\n== Hosts (%d)" % len(hosts))
    for host, count in hosts.most_common():
        print("%6d  %s" % (count, host))
    if pinned:
        print("\n== Refused our CA (pinned or own trust store)")
        for host, count in pinned.most_common():
            print("%6d  %s" % (count, host))
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


if __name__ == "__main__":
    main(sys.argv[1])
