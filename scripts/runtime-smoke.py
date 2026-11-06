#!/usr/bin/env python3
"""Run a macOS command through the installed runtime, then shut down its namespace."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from league_linux import Error, Manager


def main():
    manager = Manager()
    with manager.operation():
        if not manager.package_ready():
            raise Error("Runtime is not built and validated. Run: league-linux prepare")
        # Keep shutdown authority inside the same user namespace as the guest.
        # The timeout bounds the guest command; the lifecycle handles shutdown.
        result = manager.run([
            manager.backend(), "--shell", "-c",
            "python3 scripts/nix-state.py check && "
            "exec python3 scripts/nix-lifecycle.py timeout 60 "
            "darling shell /bin/sh -c "
            "'printf \"LEAGUE_LINUX_GUEST=\"; /usr/bin/uname -s; "
            "printf \"LEAGUE_LINUX_ARCH=\"; /usr/bin/uname -m'",
        ], capture=True, check=False)
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        if result.returncode:
            raise Error(f"Runtime smoke check exited with status {result.returncode}")
        lines = result.stdout.splitlines()
        if "LEAGUE_LINUX_GUEST=Darwin" not in lines or "LEAGUE_LINUX_ARCH=x86_64" not in lines:
            raise Error("Runtime returned without the expected Darwin/x86_64 guest output")
        print(json.dumps({"guest": "Darwin", "architecture": "x86_64",
                          "command": "passed", "lifecycle_exit_status": 0,
                          "riot_launched": False}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Error as error:
        print(f"runtime-smoke: {error}", file=sys.stderr)
        raise SystemExit(1)
