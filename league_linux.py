#!/usr/bin/env python3
"""Local desktop/CLI manager for the pinned, rootless Darling League runtime."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import platform
import re
import shutil
import time
import signal
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parent
UNIT = "league-linux-session.service"
# A session younger than this has no client window yet; `play` waits instead of restarting.
STARTUP_GRACE_SECONDS = 60
DISPLAY_ENV = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR",
               "DBUS_SESSION_BUS_ADDRESS", "XDG_SESSION_TYPE", "PULSE_SERVER")
REGIONS = ("euw", "eune", "na", "br", "kr", "jp", "la1", "la2", "oc1", "ru", "tr")


# The Mac every prefix presents (configure-darling-platform.py profiles): a 2019 16-inch
# MacBook Pro on macOS Sonoma 14.7, the version the Darling kernel already reports.
HARDWARE_PROFILE = "macmini8,1"
OS_PROFILE = "sonoma-14.7-23H124"


class Error(Exception):
    pass


class Manager:
    def __init__(self, home=None):
        self.home = Path(home or os.environ.get("LEAGUE_LINUX_HOME") or
                         Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) /
                         "league-linux").expanduser().absolute()
        self.prefix = self.home / "prefix"
        self.workspace = self.home / "runtime-data/darling-lol"
        self.config_file = self.home / "config.json"
        try:
            self.config = json.loads(self.config_file.read_text()) if self.config_file.exists() else {}
        except (OSError, ValueError) as e:
            raise Error(f"Cannot read {self.config_file}: {e}") from e
        if not isinstance(self.config, dict):
            raise Error("config.json must contain an object")
        self.region = self.config.get("region", "eune")
        if self.region not in REGIONS:
            raise Error(f"Invalid region: {self.region}")
        self.gpu = self.config.get("gpu", "auto")
        if self.gpu not in ("auto", "nvidia", "intel"):
            raise Error("gpu must be auto, intel or nvidia")
        self.jobs = self.config.get("jobs", 2)
        if not isinstance(self.jobs, int) or not 1 <= self.jobs <= 32:
            raise Error("jobs must be an integer between 1 and 32")

    def ensure_home(self):
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)

    def backend(self):
        path = Path(self.config.get("backend", str(self.home / "package/bin/darling-lol")))
        if not path.is_absolute():
            raise Error("backend must be an absolute executable path")
        if not path.is_file() or not os.access(path, os.X_OK):
            raise Error("Darling runtime package is missing. Run: league-linux prepare")
        return path

    def environment(self):
        env = os.environ.copy()
        # A dedicated prefix/workspace, not a security boundary against host-home access.
        env["LEAGUE_LINUX_HOME"] = str(self.home)
        env["DPREFIX"] = str(self.prefix)
        env["XDG_DATA_HOME"] = str(self.home / "runtime-data")
        env["JOBS"] = str(self.jobs)
        env["RCLOL_REGION"] = self.region.upper()
        env["RCLOL_GAMEMODE"] = "0"
        env["PYTHONUNBUFFERED"] = "1"
        env.pop("DYLD_INSERT_LIBRARIES", None)
        if self.gpu == "nvidia":
            env.update(__NV_PRIME_RENDER_OFFLOAD="1", __GLX_VENDOR_LIBRARY_NAME="nvidia",
                       __VK_LAYER_NV_optimus="NVIDIA_only")
        elif self.gpu == "intel":
            env["MESA_VK_DEVICE_SELECT"] = "8086:46a6!"
        return env

    @contextlib.contextmanager
    def operation(self):
        self.ensure_home()
        with (self.home / "operation.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as e:
                raise Error("Another build, installer or managed session is active.") from e
            yield

    def operation_active(self):
        try:
            with (self.home / "operation.lock").open("r") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                fcntl.flock(lock, fcntl.LOCK_UN)
        except FileNotFoundError:
            pass
        return False

    def run(self, command, *, capture=False, timeout=None, env=None, check=True, umask=-1):
        try:
            result = subprocess.run([str(x) for x in command], env=env or self.environment(),
                                    text=True, capture_output=capture, timeout=timeout, umask=umask)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise Error(f"Could not run {command[0]}: {e}") from e
        if check and result.returncode:
            detail = ((result.stderr or result.stdout or "").strip() if capture else "")
            raise Error(f"{command[0]} exited with status {result.returncode}" +
                        (f": {detail}" if detail else ""))
        return result

    def nix(self):
        found = shutil.which("nix")
        for candidate in (Path.home() / ".nix-profile/bin/nix", Path("/nix/var/nix/profiles/default/bin/nix")):
            if not found and candidate.is_file():
                found = str(candidate)
        return found

    def dependency_checks(self):
        checks = []
        def add(name, ok, detail):
            checks.append({"name": name, "ok": bool(ok), "detail": detail})
        add("architecture", platform.machine() == "x86_64", platform.machine())
        nix = self.nix()
        result = self.run([nix, "--version"], capture=True, timeout=10, check=False) if nix else None
        match = re.search(r"(\d+)\.(\d+)", result.stdout) if result else None
        add("nix", match and tuple(map(int, match.groups())) >= (2, 24),
            result.stdout.strip() if result else "Nix >=2.24 is required; see README.md")
        for name in ("newuidmap", "newgidmap", "git", "bwrap", "systemctl"):
            path = shutil.which(name)
            add(name, path is not None, path or f"Missing {name}")
        user = __import__("pwd").getpwuid(os.getuid()).pw_name
        for name in ("subuid", "subgid"):
            try:
                entries = Path("/etc", name).read_text().splitlines()
            except OSError:
                entries = []
            valid = any(len(parts := line.split(":")) == 3 and parts[0] in (user, str(os.getuid()))
                        and parts[2].isdigit() and int(parts[2]) >= 65536 for line in entries)
            add(name, valid, "Subordinate IDs configured" if valid else f"No adequate /etc/{name} allocation")
        location = self.home
        while not location.exists():
            location = location.parent
        free = shutil.disk_usage(location).free / 1024**3
        add("disk", free >= 60, f"{free:.1f} GiB free; allow 60 GiB for build plus game")
        return checks

    def package_ready(self):
        try:
            backend = self.backend()
        except Error:
            return False
        script = self.workspace / "scripts/nix-state.py"
        if not script.is_file():
            return False
        env = self.environment()
        env["DARLING_PACKAGE_ID"] = str(backend.resolve().parent.parent / "share/darling-lol")
        result = self.run([sys.executable, script, "check"], env=env, capture=True,
                          timeout=60, check=False)
        return result.returncode == 0

    def client_installed(self):
        return any(p.is_dir() for p in (self.prefix / "Applications/Riot Client.app",
                                      self.prefix / "Users/Shared/Riot Games/Riot Client.app"))

    def prepare(self):
        with self.operation():
            missing = [c for c in self.dependency_checks() if not c["ok"]]
            if missing:
                raise Error("Build prerequisites missing:\n" + "\n".join(
                    f"  {c['name']}: {c['detail']}" for c in missing))
            pin = json.loads((ROOT / "upstream.lock.json").read_text())
            source = ROOT / pin["directory"]
            if not (source / ".git").is_dir():
                raise Error(f"Source missing. Run scripts/fetch-upstream.sh to fetch {pin['revision']}.")
            head = self.run(["git", "-C", source, "rev-parse", "HEAD"], capture=True).stdout.strip()
            dirty = self.run(["git", "-C", source, "status", "--porcelain", "--untracked-files=all"],
                             capture=True).stdout.strip()
            if head != pin["revision"] or dirty:
                raise Error("Upstream checkout differs from upstream.lock.json or has local changes; build refused.")
            self.run([self.nix(), "--extra-experimental-features", "nix-command flakes", "build",
                      f"path:{source}#default", "--no-update-lock-file", "--no-write-lock-file",
                      "--out-link", self.home / "package", "--max-jobs", str(self.jobs),
                      "--cores", str(self.jobs), "--option", "http-connections", "2",
                      "--option", "http2", "false", "--option", "download-attempts", "8"])
            print("Building the rootless runtime. No Riot client or game will be launched.", flush=True)
            # launchctl refuses group-writable LaunchDaemons directories. Do not
            # inherit a desktop shell's common 0002 umask into runtime installation.
            self.run(["nice", "-n", "10", self.backend(), "--update"], umask=0o022)
            if not self.package_ready():
                raise Error("Build finished but upstream runtime validation did not pass.")
            self.provision_identity()
            print("Runtime built and validated. Next: league-linux install-client")

    def provision_identity(self, show_host=False, host_cpuid=False):
        """One Mac identity for every guest process: IOKit, sysctl, Metal, hostname, OS version.

        Idempotent; an existing serial and UUID are kept, so the machine stays the same
        across rebuilds. The prefix must be stopped.
        """
        tool = self.workspace / "tools/configure-darling-platform.py"
        if not tool.is_file():
            raise Error(f"Platform identity tool missing: {tool}. Run league-linux prepare.")
        self.run([sys.executable, tool, "--prefix", self.prefix, "--provision",
                  "--hardware-profile", HARDWARE_PROFILE, "--os-profile", OS_PROFILE, "--write",
                  *(["--show-host"] if show_host else []), *(["--host-cpuid"] if host_cpuid else [])],
                 env=self.environment())

    def install_client(self):
        if not sys.stdin.isatty():
            raise Error("Install the Riot client from a terminal: league-linux install-client")
        with self.operation():
            if not self.package_ready():
                raise Error("Runtime is not built and validated. Run: league-linux prepare")
            print("Experimental compatibility runtime. Upstream reports account suspensions.\n"
                  "Riot will display its own login UI. No account automation is provided.", flush=True)
            return self.run([self.backend(), "--installer"], check=False).returncode

    def status(self):
        result = self.run(["systemctl", "--user", "show", UNIT, "--property=LoadState,ActiveState,SubState,ExecMainStatus,MainPID",
                           "--no-pager"], capture=True, timeout=10, check=False)
        if result.returncode:
            raise Error("User service manager unavailable: " + result.stderr.strip())
        props = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        return {"service": props, "runtime_package": (self.home / "package").exists(),
                "operation_active": self.operation_active(),
                "client_installed": self.client_installed(), "prefix": str(self.prefix),
                "region": self.region, "gpu": self.gpu}

    def start(self):
        if not self.package_ready():
            raise Error("Runtime is not built and validated. Run: league-linux prepare")
        if not self.client_installed():
            raise Error("Riot client is not installed. Run: league-linux install-client")
        unit_file = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd/user" / UNIT
        if not unit_file.is_file():
            raise Error("User service is missing. Run: league-linux install")
        self.ensure_home()
        lines = []
        for key in DISPLAY_ENV:
            if key in os.environ:
                value = os.environ[key]
                if "\n" in value or "\r" in value or "\0" in value:
                    raise Error(f"Invalid {key} value")
                lines.append(f'{key}="{value.replace(chr(92), chr(92)*2).replace(chr(34), chr(92)+chr(34))}"')
        target = self.home / "session.env"
        temporary = self.home / "session.env.tmp"
        temporary.write_text("\n".join(lines) + "\n")
        temporary.chmod(0o600)
        temporary.replace(target)
        self.run(["systemctl", "--user", "reset-failed", UNIT], capture=True, check=False)
        self.run(["systemctl", "--user", "start", UNIT], capture=True)
        print("Session start requested. Check status and logs for runtime/client errors.")

    def audit_active(self):
        """True while scripts/telemetry-audit.sh records the session (it writes its PID)."""
        try:
            pid = int((self.home / "audit/.active").read_text().strip())
            os.kill(pid, 0)
        except (OSError, ValueError):
            return False
        return True

    def session_age(self):
        """Seconds since the session became active, or None if unknown."""
        result = self.run(["systemctl", "--user", "show", UNIT, "--property=ActiveEnterTimestampMonotonic",
                           "--value"], capture=True, timeout=10, check=False)
        try:
            entered = int(result.stdout.strip())
        except (AttributeError, ValueError):
            return None
        if entered <= 0:
            return None
        return time.monotonic() - entered / 1e6

    def stop(self):
        self.run(["systemctl", "--user", "stop", UNIT], capture=True, timeout=100)
        print("Managed session stopped.")

    def stop_backend(self):
        self.run([self.backend(), "--stop"], timeout=65)

    def session(self):
        # Unit stop executes upstream's graceful, prefix-scoped shutdown first.
        # The control group then collects remaining descendants.
        with self.operation():
            if not self.package_ready() or not self.client_installed():
                raise Error("Session prerequisites changed; run doctor and prepare before starting.")
            child = subprocess.Popen([str(self.backend())], env=self.environment())
            interrupted = False
            def forward(signum, _frame):
                nonlocal interrupted
                interrupted = True
                if child.poll() is None:
                    child.send_signal(signum)
            signal.signal(signal.SIGTERM, forward)
            signal.signal(signal.SIGINT, forward)
            result = child.wait()
            return 0 if interrupted and result in (-signal.SIGTERM, -signal.SIGINT) else result

    def configure(self, region=None, gpu=None, jobs=None):
        self.ensure_home()
        if region is not None:
            self.config["region"] = region
        if gpu is not None:
            self.config["gpu"] = gpu
        if jobs is not None:
            if not 1 <= jobs <= 32:
                raise Error("jobs must be between 1 and 32")
            self.config["jobs"] = jobs
        with self.operation():
            target = self.config_file.with_suffix(".tmp")
            target.write_text(json.dumps(self.config, indent=2) + "\n")
            target.chmod(0o600)
            target.replace(self.config_file)


def systemd_quote(value):
    value = str(value)
    if "\n" in value or "\r" in value:
        raise Error("Newlines are unsupported in install paths")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def unit_text(manager):
    command = f"{systemd_quote(sys.executable)} {systemd_quote(ROOT / 'league_linux.py')}"
    env_file = str(manager.home / "session.env")
    if "\n" in env_file or "\r" in env_file:
        raise Error("Newlines are unsupported in install paths")
    # EnvironmentFile is a single path directive, not an Exec argument list.
    env_file = env_file.replace("%", "%%")
    return f"""[Unit]
Description=League Linux compatibility session
After=graphical-session.target

[Service]
Type=exec
Environment=LEAGUE_LINUX_HOME={systemd_quote(manager.home)}
EnvironmentFile=-{env_file}
ExecStart={command} _session
ExecStop={command} _stop-backend
Restart=no
KillMode=control-group
TimeoutStopSec=90
UMask=0077
Nice=5
StandardOutput=journal
StandardError=journal
"""


def install(manager):
    manager.ensure_home()
    config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    unit = config / "systemd/user" / UNIT
    desktop = data / "applications/league-linux.desktop"
    binary = Path.home() / ".local/bin/league-linux"
    for path in (unit, desktop, binary):
        path.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(unit_text(manager))
    import shlex
    binary.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, str(ROOT / "league_linux.py")]) + ' "$@"\n')
    binary.chmod(0o755)
    desktop.write_text(desktop_entries(binary)["manager"])
    play = data / "applications/league-linux-play.desktop"
    play.write_text(desktop_entries(binary)["play"])
    quit_entry = data / "applications/league-linux-quit.desktop"
    quit_entry.write_text(desktop_entries(binary)["quit"])
    manager.run(["systemctl", "--user", "daemon-reload"], capture=True)
    print(f"Installed {binary}\nDesktop entries: {desktop}, {play}, {quit_entry}\nUser service: {unit}\n"
          "No login autostart enabled.")


def desktop_entries(binary):
    """The manager window entry and the one-click play and quit entries."""
    # Desktop quoting is similar to Exec syntax, but dollar signs are literal.
    executable = str(binary).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    manager = f"""[Desktop Entry]
Type=Application
Name=League Linux
Comment=Manage the experimental macOS League compatibility runtime
Exec="{executable}" gui
Icon=applications-games
Terminal=false
Categories=Game;
StartupNotify=true
"""
    play = f"""[Desktop Entry]
Type=Application
Name=League of Legends
Comment=Start the Riot Client in the League Linux runtime
Exec="{executable}" play
Icon=applications-games
Terminal=false
Categories=Game;
Keywords=League;Riot;LoL;
Actions=stop;manager;

[Desktop Action stop]
Name=Stop session
Exec="{executable}" stop

[Desktop Action manager]
Name=Open League Linux manager
Exec="{executable}" gui
"""
    quit_entry = f"""[Desktop Entry]
Type=Application
Name=Quit League
Comment=Close the Riot Client, League and the League Linux runtime
Exec="{executable}" quit
Icon=application-exit
Terminal=false
Categories=Game;
Keywords=League;Riot;LoL;Exit;Stop;Close;
"""
    return {"manager": manager, "play": play, "quit": quit_entry}


def raise_client_windows():
    """Restore and focus the Riot Client's windows (X11/Xwayland). True if any."""
    if not shutil.which("xdotool"):
        return False
    found = subprocess.run(["xdotool", "search", "--class", "^Riot Client$"],
                           capture_output=True, text=True, check=False)
    windows = found.stdout.split()
    raised = False
    for window in windows:
        # Skip the unmapped splash window; map+activate un-minimizes the main one.
        state = subprocess.run(["xwininfo", "-id", window], capture_output=True, text=True,
                               check=False).stdout if shutil.which("xwininfo") else ""
        if "IsUnMapped" in state:
            continue
        subprocess.run(["xdotool", "windowmap", window, "windowactivate", window],
                       capture_output=True, check=False)
        raised = True
    return raised


def notify(summary, body):
    """Desktop notification for actions started without a terminal."""
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "--app-name=League Linux", "--icon=applications-games",
                        summary, body], check=False)


def terminal(command):
    for program, flags in (("cosmic-term", ["--"]), ("gnome-terminal", ["--"]),
                           ("kgx", ["--"]), ("xterm", ["-e"])):
        if shutil.which(program):
            subprocess.Popen([program, *flags, sys.executable, str(ROOT / "scripts/terminal-action.py"), command],
                             start_new_session=True)
            return
    raise Error(f"No terminal found. Run league-linux {command} in your terminal.")


def gui(manager, on_ready=None):
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk, GLib, Gio
    except (ImportError, ValueError) as e:
        raise Error("GTK 4 Python bindings are required for the desktop window.") from e

    class Application(Gtk.Application):
        def __init__(self):
            super().__init__(application_id="local.league_linux.Launcher",
                             flags=Gio.ApplicationFlags.NON_UNIQUE if on_ready else Gio.ApplicationFlags.FLAGS_NONE)
            self.busy = False

        def do_activate(self):
            if self.get_active_window():
                self.get_active_window().present()
                return
            window = Gtk.ApplicationWindow(application=self, title="League Linux")
            window.set_default_size(820, 650)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
            for method in (box.set_margin_top, box.set_margin_bottom, box.set_margin_start, box.set_margin_end):
                method(24)
            window.set_child(box)
            title = Gtk.Label(label="League on your Linux desktop", xalign=0)
            title.add_css_class("title-1")
            box.append(title)
            subtitle = Gtk.Label(label="macOS client · Darling compatibility runtime", xalign=0)
            subtitle.add_css_class("dim-label")
            box.append(subtitle)
            notice = Gtk.Label(label="Experimental. Upstream reports account suspensions.\n"
                               "A working runtime does not guarantee access to online matches.", xalign=0)
            notice.set_wrap(True)
            box.append(notice)
            self.status_label = Gtk.Label(label="Checking setup…", xalign=0)
            box.append(self.status_label)
            row = Gtk.Box(spacing=8)
            self.buttons = []
            for label, action in (("Start", "start"), ("Stop", "stop"), ("Check setup", "doctor"), ("Logs", "logs")):
                button = Gtk.Button(label=label)
                button.connect("clicked", lambda _button, a=action: self.action(a))
                self.buttons.append(button)
                row.append(button)
            box.append(row)
            setup = Gtk.Box(spacing=8)
            for label, action in (("Build runtime", "prepare"), ("Install Riot client", "install-client")):
                button = Gtk.Button(label=label)
                button.connect("clicked", lambda _button, a=action: self.open_terminal(a))
                setup.append(button)
            box.append(setup)
            options = Gtk.Box(spacing=8)
            options.append(Gtk.Label(label="Region"))
            region = Gtk.DropDown.new_from_strings(list(REGIONS))
            region.set_selected(REGIONS.index(manager.region))
            options.append(region)
            options.append(Gtk.Label(label="GPU"))
            gpu_values = ("auto", "nvidia", "intel")
            gpu = Gtk.DropDown.new_from_strings(list(gpu_values))
            gpu.set_selected(gpu_values.index(manager.gpu))
            options.append(gpu)
            save = Gtk.Button(label="Save settings")
            def save_settings(_button):
                self.action("configure", ["--region", REGIONS[region.get_selected()],
                                          "--gpu", gpu_values[gpu.get_selected()]])
            save.connect("clicked", save_settings)
            options.append(save)
            box.append(options)
            scroll = Gtk.ScrolledWindow(vexpand=True)
            self.output = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
            scroll.set_child(self.output)
            box.append(scroll)
            footer = Gtk.Label(label="Closing this window leaves a running session active. Use Stop to end it.", xalign=0)
            footer.set_wrap(True)
            footer.add_css_class("dim-label")
            box.append(footer)
            window.present()
            self.action("doctor")
            if on_ready:
                GLib.timeout_add(2500, lambda: on_ready(self, window))

        def show_result(self, text):
            self.output.get_buffer().set_text(text)
            self.busy = False
            for button in self.buttons:
                button.set_sensitive(True)
            try:
                current = Manager().status()
                state = current["service"].get("ActiveState", "unknown")
                if state == "inactive" and current["operation_active"]:
                    state = "setup/build running"
                self.status_label.set_text(f"Session: {state} · Riot client: " +
                                           ("installed" if current["client_installed"] else "not installed"))
            except Error as e:
                self.status_label.set_text(str(e))
            return False

        def action(self, action, args=()):
            if self.busy:
                return
            self.busy = True
            for button in self.buttons:
                button.set_sensitive(False)
            def work():
                try:
                    result = subprocess.run([sys.executable, str(ROOT / "league_linux.py"), action, *args],
                                            capture_output=True, text=True, timeout=120)
                    text = result.stdout + result.stderr
                except (OSError, subprocess.TimeoutExpired) as e:
                    text = str(e)
                GLib.idle_add(self.show_result, text)
            threading.Thread(target=work, daemon=True).start()

        def open_terminal(self, action):
            try:
                terminal(action)
            except Error as e:
                self.show_result(str(e))

    return Application().run([])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("install", "prepare", "install-client", "start", "play", "stop", "quit", "gui",
                 "_session", "_stop-backend"):
        sub.add_parser(name)
    for name in ("status", "doctor"):
        sub.add_parser(name).add_argument("--json", action="store_true")
    identity = sub.add_parser("identity")
    identity.add_argument("--show-host", action="store_true",
                          help="let applications see /proc, the host root, Linux interface names and host variables")
    identity.add_argument("--host-cpuid", action="store_true",
                          help="leave CPUID instructions reporting the host CPU")
    log = sub.add_parser("logs")
    log.add_argument("--follow", action="store_true")
    cfg = sub.add_parser("configure")
    cfg.add_argument("--region", choices=REGIONS)
    cfg.add_argument("--gpu", choices=("auto", "nvidia", "intel"))
    cfg.add_argument("--jobs", type=int)
    args = parser.parse_args(argv)
    try:
        manager = Manager()
        if args.command == "install":
            install(manager)
        elif args.command == "prepare":
            manager.prepare()
        elif args.command == "install-client":
            return manager.install_client()
        elif args.command == "identity":
            with manager.operation():
                manager.provision_identity(args.show_host, args.host_cpuid)
        elif args.command == "start":
            manager.start()
        elif args.command == "play":
            # The desktop entry has no terminal: report the outcome as a notification.
            try:
                running = manager.status()["service"].get("ActiveState") in ("active", "activating")
                if running:
                    # A second start is a no-op; bring the existing client forward instead.
                    if raise_client_windows():
                        return 0
                    # Never restart a session an audit records (the new one would run
                    # outside it), nor one still starting (its window is not up yet).
                    if manager.audit_active():
                        notify("League is being audited",
                               "The recorded session keeps running; use its window, or Quit League to end the audit.")
                        return 0
                    age = manager.session_age()
                    if age is not None and age < STARTUP_GRACE_SECONDS:
                        notify("League is starting", "The Riot Client window opens in a few seconds.")
                        return 0
                    # Closing the Riot Client's window keeps it running for the macOS
                    # menu-bar icon, which Darling lacks: restart to get a window back.
                    notify("Restarting League", "The Riot Client window was closed; reopening it.")
                    manager.stop()
                manager.start()
            except (Error, OSError) as e:
                notify("League could not start", str(e))
                raise
            notify("Starting League", "The Riot Client opens in a few seconds.")
        elif args.command == "stop":
            manager.stop()
        elif args.command == "quit":
            # Desktop-entry variant of stop: no terminal, so report via notifications.
            try:
                manager.stop()
            except (Error, OSError) as e:
                notify("League could not be closed", str(e))
                raise
            notify("League closed", "The Riot Client and League have exited.")
        elif args.command == "_session":
            return manager.session()
        elif args.command == "_stop-backend":
            manager.stop_backend()
        elif args.command == "configure":
            manager.configure(args.region, args.gpu, args.jobs)
            print("Settings saved.")
        elif args.command == "status":
            state = manager.status()
            if args.json:
                print(json.dumps(state, indent=2))
            else:
                print(f"Session: {state['service'].get('ActiveState', 'unknown')}\n"
                      f"Build/installer/session operation: {'active' if state['operation_active'] else 'idle'}\n"
                      f"Runtime package: {'present' if state['runtime_package'] else 'missing'}\n"
                      f"Riot client: {'installed' if state['client_installed'] else 'not installed'}\n"
                      f"Region: {state['region']} · GPU: {state['gpu']}\nPrefix: {state['prefix']}")
        elif args.command == "doctor":
            checks = manager.dependency_checks()
            ready = manager.package_ready()
            checks += [{"name": "runtime", "ok": ready, "detail": "Validated" if ready else "Not built/validated"},
                       {"name": "riot-client", "ok": manager.client_installed(),
                        "detail": "Installed" if manager.client_installed() else "Not installed"}]
            if args.json:
                print(json.dumps({"checks": checks, "prefix": str(manager.prefix)}, indent=2))
            else:
                for c in checks:
                    print(f"{'OK     ' if c['ok'] else 'MISSING'} {c['name']}: {c['detail']}")
            return 0 if all(c["ok"] for c in checks) else 1
        elif args.command == "logs":
            return manager.run(["journalctl", "--user", "--unit", UNIT, "--no-pager", "-n", "100",
                                *(["--follow"] if args.follow else [])], check=False).returncode
        elif args.command == "gui":
            return gui(manager)
    except (Error, OSError) as e:
        print(f"league-linux: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
