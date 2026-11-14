#!/usr/bin/env python3
"""Read the requests mitmproxy could not: decrypt a capture with the clients' own TLS keys.

    scripts/telemetry-audit-decrypt.py <capture.pcap> <tls-keys.log> <out.jsonl> [<mitm.jsonl>]

The League client and the game check certificates against their own list, so mitmproxy
has to pass their HTTPS through. Their libcurl writes each connection's TLS secrets to
the file named by SSLKEYLOGFILE, though, and telemetry-audit.sh sets that for the audited
session and captures the session's TCP. With both, tshark (TSHARK, default: on PATH)
decodes the HTTP/1.1 and HTTP/2 requests of those connections. They are written to
<out.jsonl> in the format of telemetry_audit_addon.py, marked "via": "keylog", with the
same redaction. Hosts mitmproxy already decoded (from <mitm.jsonl>) are skipped.

Decrypted connections that do not carry HTTP (chat) get one record with the start of
what the client sent. Connections whose keys were not logged (a TLS library that ignores
SSLKEYLOGFILE) are listed as "undecrypted".
"""
import collections
import gzip
import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audit_scrub import MAX_BODY, header, redacted, scrub  # noqa: E402

TSHARK = os.environ.get("TSHARK", "tshark")
MAX_STREAM_BYTES = 256 * 1024
RAW_PREVIEW = 4096
FOLLOW_BATCH = 100
OPAQUE = re.compile(r"[A-Za-z0-9+/_=-]{40,}")
PRINTABLE = re.compile(rb"[\x20-\x7e]{6,}")


def tshark(pcap, keys, *args):
    command = [TSHARK, "-r", pcap, "-n", "-o", "tls.keylog_file:" + keys, *args]
    return subprocess.run(command, check=True, capture_output=True, text=True, errors="replace").stdout


def field_rows(pcap, keys, display_filter, *fields):
    arguments = ["-Y", display_filter, "-T", "fields", "-E", "separator=\t"]
    for name in fields:
        arguments += ["-e", name]
    for line in tshark(pcap, keys, *arguments).splitlines():
        row = line.split("\t")
        if len(row) == len(fields):
            yield row


def ek_packets(pcap, keys, display_filter, fields):
    arguments = ["-Y", display_filter, "-T", "ek"]
    for name in fields:
        arguments += ["-e", name]
    process = subprocess.Popen([TSHARK, "-r", pcap, "-n", "-o", "tls.keylog_file:" + keys, *arguments],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors="replace")
    for line in process.stdout:
        if not line.startswith('{"timestamp"'):
            continue
        layers = json.loads(line).get("layers", {})
        yield {name: as_list(layers.get(name.replace(".", "_"))) for name in fields}
    process.wait()


def as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def from_hex(value):
    try:
        return bytes.fromhex(value.replace(":", ""))
    except ValueError:
        return value.encode("utf-8", "replace")


def body_of(raw, headers):
    if not raw:
        return None
    if headers.get("content-encoding", "").lower() == "gzip":
        try:
            raw = gzip.decompress(raw)
        except (OSError, EOFError):
            pass
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        strings = [scrub(OPAQUE.sub(lambda m: redacted(m.group(0)), s.decode()))
                   for s in PRINTABLE.findall(raw[:MAX_BODY])]
        return {"binary_bytes": len(raw), "content_type": headers.get("content-type", "?"),
                "strings": strings[:400]}
    if len(text) > MAX_BODY:
        return "<%d chars; first %d follow> " % (len(text), MAX_BODY) + scrub(text[:MAX_BODY])
    try:
        return scrub(json.loads(text))
    except ValueError:
        return scrub(text)


def record(when, method, url, headers, raw):
    return {
        "time": time.strftime("%H:%M:%S", time.localtime(float(when))),
        "via": "keylog",
        "method": method,
        "url": scrub(url),
        "request_headers": {k: header(k, v) for k, v in headers.items()},
        "request_body": body_of(raw, headers),
    }


def clients(pcap, keys):
    """Addresses that open connections (the capture's client side)."""
    return sorted({row[0] for row in field_rows(pcap, keys, "tcp.flags.syn == 1 and tcp.flags.ack == 0", "ip.src")})


def server_names(pcap, keys):
    """Per TCP stream with a ClientHello: (server name, tshark's TLS stream number, client endpoint)."""
    names = {}
    for stream, name, address, port, tls_stream, source, source_port in field_rows(
            pcap, keys, "tls.handshake.type == 1", "tcp.stream",
            "tls.handshake.extensions_server_name", "ip.dst", "tcp.dstport", "tls.stream", "ip.src", "tcp.srcport"):
        names.setdefault(stream, (name.split(",")[0] or "%s:%s" % (address, port), tls_stream,
                                  "%s:%s" % (source, source_port)))
    return names


def http_requests(pcap, keys, client_filter):
    """Yield (tcp stream, time, method, url, headers, body bytes) for every decoded request."""
    fields = ["frame.time_epoch", "tcp.stream", "ip.src", "tcp.srcport",
              "http.request.method", "http.host", "http.request.uri", "http.request.line", "http.file_data",
              "http2.type", "http2.streamid", "http2.length", "http2.header.name", "http2.header.value",
              "http2.data.data"]
    pending = {}  # (tcp stream, client endpoint, HTTP/2 stream) -> [time, headers]
    bodies = collections.defaultdict(bytearray)
    done = []
    display = "(%s) and (http.request or http2.type == 1 or http2.type == 9 or http2.data.data)" % client_filter
    for packet in ek_packets(pcap, keys, display, fields):
        when = (packet["frame.time_epoch"] or ["0"])[0]
        stream = (packet["tcp.stream"] or ["?"])[0]
        endpoint = "%s:%s" % ((packet["ip.src"] or ["?"])[0], (packet["tcp.srcport"] or ["?"])[0])
        if packet["http.request.method"]:
            headers = {}
            for line in packet["http.request.line"]:
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()
            host = (packet["http.host"] or [headers.get("host", "?")])[0]
            uri = (packet["http.request.uri"] or ["/"])[0]
            raw = from_hex(packet["http.file_data"][0]) if packet["http.file_data"] else b""
            done.append((stream, when, packet["http.request.method"][0], "https://%s%s" % (host, uri), headers, raw))
        types, ids, lengths = packet["http2.type"], packet["http2.streamid"], packet["http2.length"]
        if not types:
            continue
        # Header blocks in packet order; each HEADERS frame (type 1) opens one.
        blocks = []
        for name, value in zip(packet["http2.header.name"], packet["http2.header.value"]):
            if name in (":method", ":status") and (not blocks or name in blocks[-1]):
                blocks.append({})
            if not blocks:
                blocks.append({})
            blocks[-1][name] = value
        header_ids = [sid for kind, sid in zip(types, ids) if kind == "1"]
        for index, block in enumerate(blocks):
            if ":method" in block:
                sid = header_ids[index] if len(header_ids) == len(blocks) else None
                pending[(stream, endpoint, sid)] = [when, block]
        data = iter(packet["http2.data.data"])
        for kind, sid, length in zip(types, ids, lengths):
            if kind == "0" and length != "0":
                chunk = next(data, None)
                key = (stream, endpoint, sid)
                if chunk is not None and len(bodies[key]) < MAX_STREAM_BYTES:
                    bodies[key] += from_hex(chunk)
    yield from done
    for key, (when, block) in pending.items():
        headers = {k: v for k, v in block.items() if not k.startswith(":")}
        url = "%s://%s%s" % (block.get(":scheme", "https"), block.get(":authority", "?"), block.get(":path", "/"))
        yield key[0], when, block[":method"], url, headers, bytes(bodies.get(key, b""))


def followed(pcap, keys, streams):
    """Decrypted bytes the client sent, per TLS stream (empty when its keys were not logged).

    streams maps tshark's TLS stream number to the client's endpoint."""
    result = {}
    numbers = sorted(streams, key=int)
    for start in range(0, len(numbers), FOLLOW_BATCH):
        arguments = ["-q"]
        for number in numbers[start:start + FOLLOW_BATCH]:
            arguments += ["-z", "follow,tls,raw,%s" % number]
        current, sent, client_indented = None, None, False
        for line in tshark(pcap, keys, *arguments).splitlines():
            if line.startswith("Filter: tls.stream eq "):
                current, sent = line.rsplit(" ", 1)[1], bytearray()
                result[current] = sent
            elif line.startswith("Node 1: "):  # node 1's data lines are the tab-indented ones
                client_indented = line[8:].strip() == streams.get(current)
            elif current and line.strip() and not line.startswith(("=", "Follow:", "Node ")):
                if line.startswith("\t") == client_indented and len(sent) < RAW_PREVIEW:
                    try:
                        sent += bytes.fromhex(line.strip())
                    except ValueError:
                        pass
    return result


def main(pcap, keys, out, mitm_log=None):
    already = set()
    if mitm_log and os.path.exists(mitm_log):
        for line in open(mitm_log, encoding="utf-8"):
            entry = json.loads(line)
            if "method" in entry:
                already.add(urlsplit(entry.get("url", "")).netloc)
    names = server_names(pcap, keys)
    sources = clients(pcap, keys)
    client_filter = "ip.src in {%s}" % " ".join(sources) if sources else "tcp"
    with_http = set()
    written = collections.Counter()
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as log:
        def write(entry):
            log.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

        for stream, when, method, url, headers, raw in http_requests(pcap, keys, client_filter):
            with_http.add(stream)
            host = urlsplit(url).netloc
            if host in already or stream not in names:  # mitmproxy read it, or plain HTTP
                continue
            written[host] += 1
            write(record(when, method, url, headers, raw))
        rest = sorted((s for s in names if s not in with_http), key=int)
        sent = followed(pcap, keys, {names[s][1]: names[s][2] for s in rest})
        raw_hosts, unread = collections.Counter(), collections.Counter()
        for stream in rest:
            host, tls_stream, _ = names[stream]
            data = bytes(sent.get(tls_stream, b""))
            if not data:
                unread[host] += 1
                continue
            raw_hosts[host] += 1
            if raw_hosts[host] > 3:
                continue
            text = "".join(chr(b) if 32 <= b < 127 or b in (10, 13) else "." for b in data)
            write({"via": "keylog", "tcp": host,
                   "client_start": scrub(OPAQUE.sub(lambda m: redacted(m.group(0)), text))})
        for host, count in unread.items():
            if host not in already:
                write({"via": "keylog", "undecrypted": host, "connections": count})
    print("Decrypted with the clients' TLS keys: %d requests to %d hosts; %d non-HTTP hosts; %d hosts still unread."
          % (sum(written.values()), len(written), len(raw_hosts), sum(1 for h in unread if h not in already)))


if __name__ == "__main__":
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    main(*sys.argv[1:5])
