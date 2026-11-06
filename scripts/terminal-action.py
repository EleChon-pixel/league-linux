#!/usr/bin/env python3
"""Keep build/installer output visible when a desktop terminal action finishes."""
import argparse
from pathlib import Path
import subprocess
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("action", choices=("prepare", "install-client"))
args = parser.parse_args()
try:
    result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "league_linux.py"), args.action])
    code = result.returncode
except KeyboardInterrupt:
    code = 130
print(f"\nAction finished with status {code}.")
if sys.stdin.isatty():
    try:
        input("Press Enter to close this terminal.")
    except (EOFError, KeyboardInterrupt):
        pass
raise SystemExit(code)
