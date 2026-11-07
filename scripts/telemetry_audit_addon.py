"""mitmproxy addon: record what the Riot/League clients send, with credentials redacted.

Every request becomes one JSON line in $LEAGUE_AUDIT_LOG (default ./telemetry-audit.jsonl):
time, method, URL, request headers and body, response status and size. Authorization,
cookies and token/password fields are replaced by a length marker before anything is
written, so the log can be read and shared without exposing the account. The log file is
created with mode 0600. TLS failures are recorded too: a host the client refuses to talk
through the proxy is pinned or uses its own trust store.

    mitmdump -s scripts/telemetry_audit_addon.py
"""
import base64
import binascii
import json
import os
import re
import time

from mitmproxy import http, tls

SECRET_HEADERS = {"authorization", "cookie", "set-cookie", "x-riot-entitlements-jwt",
                  "proxy-authorization"}
# Base64 JSON platform descriptions (OS, version, chipset): the point of the audit.
DECODED_HEADERS = {"x-riot-clientplatform"}
SECRET_KEYS = re.compile(r"(pass(word)?|token|secret|jwt|session|cookie|auth|code|"
                         r"entitlement|signature|nonce|puuid|credential|username|email|login|"
                         r"gamename|tagline|summonername|displayname)", re.I)
# JWTs and long opaque strings inside free text (query strings, form bodies).
SECRET_VALUES = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]*")
MAX_BODY = 64 * 1024


def redacted(value):
    return "<redacted %d chars>" % len(str(value))


def scrub(value, key=""):
    if isinstance(value, dict):
        return {k: (redacted(v) if SECRET_KEYS.search(k) and not isinstance(v, (dict, list))
                    else scrub(v, k)) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, key) for v in value]
    if isinstance(value, str):
        return SECRET_VALUES.sub(lambda m: redacted(m.group(0)), value)
    return value


def header(name, value):
    if name.lower() in SECRET_HEADERS:
        return redacted(value)
    if name.lower() in DECODED_HEADERS:
        try:
            return {"decoded": scrub(json.loads(base64.b64decode(value + "=" * (-len(value) % 4))))}
        except (ValueError, binascii.Error):
            pass
    return scrub(value)


def body(message):
    if not message.raw_content:
        return None
    try:
        text = message.get_text(strict=False)
    except ValueError:
        text = None
    if text is None:
        return "<%d bytes binary %s>" % (len(message.raw_content), message.headers.get("content-type", "?"))
    if len(text) > MAX_BODY:
        return "<%d chars; first %d follow> " % (len(text), MAX_BODY) + scrub(text[:MAX_BODY])
    try:
        return scrub(json.loads(text))
    except ValueError:
        return scrub(text)


class TelemetryAudit:
    def __init__(self):
        path = os.environ.get("LEAGUE_AUDIT_LOG", "telemetry-audit.jsonl")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self.log = os.fdopen(fd, "a", buffering=1)

    def write(self, record):
        self.log.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def response(self, flow: http.HTTPFlow):
        request = flow.request
        self.write({
            "time": time.strftime("%H:%M:%S", time.localtime(request.timestamp_start)),
            "method": request.method,
            "url": scrub(request.pretty_url),
            "request_headers": {k: header(k, v) for k, v in request.headers.items()},
            "request_body": body(request),
            "status": flow.response.status_code if flow.response else None,
            "response_type": flow.response.headers.get("content-type") if flow.response else None,
            "response_bytes": len(flow.response.raw_content or b"") if flow.response else None,
        })

    def error(self, flow: http.HTTPFlow):
        self.write({"time": time.strftime("%H:%M:%S"), "url": scrub(flow.request.pretty_url),
                    "error": str(flow.error)})

    def tls_failed_client(self, data: tls.TlsData):
        # The client rejected our certificate: pinned, or its own CA bundle.
        self.write({"time": time.strftime("%H:%M:%S"), "tls_rejected_by_client": data.context.server.sni
                    or str(data.context.server.address), "error": str(data.conn.error)})


addons = [TelemetryAudit()]
