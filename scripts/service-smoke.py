#!/usr/bin/env python3
"""Exercise the real user service with a fake backend; never launch Riot."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import league_linux as app


def main():
    unit_name = f"league-linux-smoke-{os.getpid()}.service"
    unit_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd/user"
    unit_path = unit_dir / unit_name
    unrelated = subprocess.Popen(["sleep", "180"])
    unit_dir.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="league-linux-smoke-") as tmp:
            home = Path(tmp) / "state with spaces"
            manager = app.Manager(home)
            manager.ensure_home()
            backend = home / "fake-backend.py"
            backend.write_text('''#!/usr/bin/env python3
import os, pathlib, sys, time
home = pathlib.Path(os.environ['LEAGUE_LINUX_HOME'])
if '--stop' in sys.argv:
    (home / 'stop').touch()
    deadline = time.monotonic() + 10
    while (home / 'pid').exists() and pathlib.Path('/proc/' + (home / 'pid').read_text()).exists():
        if time.monotonic() > deadline: raise SystemExit(1)
        time.sleep(.1)
else:
    (home / 'pid').write_text(str(os.getpid()))
    (home / 'display').write_text(os.environ.get('DISPLAY', ''))
    print('FAKE backend started; no Riot process', flush=True)
    while not (home / 'stop').exists(): time.sleep(.1)
    print('FAKE backend shut down cleanly', flush=True)
''')
            backend.chmod(0o755)
            manager.config_file.write_text(json.dumps({"backend": str(backend)}))
            state_script = manager.workspace / "scripts/nix-state.py"
            state_script.parent.mkdir(parents=True)
            state_script.write_text("raise SystemExit(0)\n")
            (manager.prefix / "Applications/Riot Client.app").mkdir(parents=True)
            (home / "session.env").write_text('DISPLAY=":777"\n')
            # Only the unit under test has this temporary home and fake backend.
            unit_path.write_text(app.unit_text(manager))
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
            subprocess.run(["systemctl", "--user", "start", unit_name], check=True)
            deadline = time.monotonic() + 15
            while not (home / "pid").exists() and time.monotonic() < deadline:
                time.sleep(.1)
            if not (home / "pid").exists():
                raise RuntimeError("Fake backend did not start")
            backend_pid = int((home / "pid").read_text())
            assert (home / "display").read_text() == ":777", "Session environment was not loaded"
            active = subprocess.check_output(["systemctl", "--user", "is-active", unit_name], text=True).strip()
            assert active == "active", active
            subprocess.run(["systemctl", "--user", "stop", unit_name], check=True, timeout=100)
            result = subprocess.check_output(["systemctl", "--user", "show", unit_name,
                "--property=ActiveState,ExecMainStatus,Result"], text=True)
            assert "ActiveState=inactive" in result, result
            assert "ExecMainStatus=0" in result, result
            assert not Path(f"/proc/{backend_pid}").exists(), "Backend remained alive"
            assert unrelated.poll() is None, "Unrelated process was stopped"
            print(json.dumps({"backend": "FAKE; no Darling/Riot/League launched", "start": "passed",
                              "stop": "passed", "exit_status": 0, "unrelated_process": "survived"}, indent=2))
    finally:
        subprocess.run(["systemctl", "--user", "stop", unit_name], capture_output=True)
        subprocess.run(["systemctl", "--user", "reset-failed", unit_name], capture_output=True)
        unit_path.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        unrelated.terminate()
        unrelated.wait(timeout=10)


if __name__ == "__main__":
    main()
