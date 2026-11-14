"""Redaction shared by the audit recorders: credentials, tokens and account names are
replaced by a length marker before anything is written to an audit log."""
import base64
import binascii
import json
import re

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
