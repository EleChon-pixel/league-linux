#!/usr/bin/env python3
"""Resume one official binary-cache download with system curl, then let Nix verify it.

This is an optional recovery tool for interrupted Nix downloads. It does not
change substituter trust settings or disable signature/content verification.
"""
import argparse
from pathlib import Path
import re
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import league_linux as app

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("store_path", help="Exact /nix/store/hash-name path in the failed download")
args = parser.parse_args()
match = re.fullmatch(r"/nix/store/([0-9a-z]{32})-[^/\r\n]+", args.store_path)
if not match:
    parser.error("Expected one absolute Nix store path")
cache = app.ROOT / ".cache/nix-cache"
cache.mkdir(parents=True, exist_ok=True)
metadata = cache / (match.group(1) + ".narinfo")
subprocess.run(["curl", "--http1.1", "-fsSL", "https://cache.nixos.org/" + metadata.name,
                "-o", str(metadata)], check=True)
fields = dict(line.split(": ", 1) for line in metadata.read_text().splitlines() if ": " in line)
if fields.get("StorePath") != args.store_path or not fields.get("Sig", "").startswith("cache.nixos.org-1:"):
    raise SystemExit("Unexpected cache metadata; refusing import")
url = fields.get("URL", "")
if not re.fullmatch(r"nar/[0-9a-z]{52}\.nar\.(zst|xz|bz2)", url):
    raise SystemExit("Unexpected cache archive URL")
archive = cache / url
archive.parent.mkdir(exist_ok=True)
expected_size = int(fields["FileSize"])
stalled = 0
for attempt in range(32):
    before = archive.stat().st_size if archive.is_file() else 0
    if before == expected_size:
        break
    if before > expected_size:
        raise SystemExit("Existing archive exceeds signed metadata size; refusing to append")
    # Invoke curl anew on each retry: -C - then observes the CURRENT file size.
    # curl's built-in retry can restart at the original offset of that invocation.
    print(f"Resuming at {before}/{expected_size} bytes (attempt {attempt + 1})", flush=True)
    transfer = subprocess.run(["curl", "--http1.1", "-fL", "--continue-at", "-",
                              "--connect-timeout", "20", "--max-time", "60",
                              "https://cache.nixos.org/" + url, "-o", str(archive)])
    after = archive.stat().st_size if archive.is_file() else 0
    stalled = stalled + 1 if after <= before else 0
    if stalled >= 5:
        raise SystemExit("Download made no progress in five attempts; partial file retained")
    if after <= before:
        time.sleep(1)
if archive.stat().st_size != expected_size:
    raise SystemExit("Incomplete archive; rerun to resume")
(cache / "nix-cache-info").write_text("StoreDir: /nix/store\nWantMassQuery: 1\nPriority: 40\n")
nix = app.Manager().nix()
if not nix:
    raise SystemExit("Nix is not installed")
# Realise rather than `nix copy`: a partial local cache does not contain the
# entire dependency closure. Missing references still use the official cache.
subprocess.run([str(Path(nix).with_name("nix-store")), "--realise", args.store_path,
                "--option", "substituters", cache.as_uri() + " https://cache.nixos.org",
                "--option", "http-connections", "2", "--option", "http2", "false"], check=True)
print("Imported through Nix's official signature/content verification:", args.store_path)
