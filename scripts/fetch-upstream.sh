#!/bin/sh
set -eu
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec python3 - "$repo" <<'PY'
import json, pathlib, subprocess, sys
root = pathlib.Path(sys.argv[1])
pin = json.loads((root / 'upstream.lock.json').read_text())
target = root / pin['directory']
if not target.exists():
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['git', 'init', str(target)], check=True)
    subprocess.run(['git', '-C', str(target), 'remote', 'add', 'origin', pin['repository']], check=True)
if subprocess.check_output(['git', '-C', str(target), 'status', '--porcelain']).strip():
    raise SystemExit('Upstream has local changes; refusing to replace them.')
subprocess.run(['git', '-C', str(target), 'fetch', '--depth', '1', 'origin', pin['revision']], check=True)
subprocess.run(['git', '-C', str(target), 'checkout', '--detach', pin['revision']], check=True)
print('Pinned upstream ready:', pin['revision'])
PY
