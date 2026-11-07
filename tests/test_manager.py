import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import league_linux as app


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "state"
        self.manager = app.Manager(self.home)

    def test_unbuilt_runtime_never_starts_service_or_installer(self):
        with patch.object(self.manager, "run") as run:
            with self.assertRaisesRegex(app.Error, "not built"):
                self.manager.start()
            run.assert_not_called()
        with patch("sys.stdin.isatty", return_value=True), patch.object(self.manager, "run") as run:
            with self.assertRaisesRegex(app.Error, "not built"):
                self.manager.install_client()
            run.assert_not_called()

    def test_lock_serializes_build_install_and_session(self):
        other = app.Manager(self.home)
        self.assertFalse(other.operation_active())
        self.assertFalse(self.home.exists())
        with self.manager.operation():
            self.assertTrue(other.operation_active())
            with self.assertRaisesRegex(app.Error, "Another"):
                with other.operation():
                    self.fail("second operation acquired the lock")
        with other.operation():
            pass
        self.assertFalse(other.operation_active())

    def test_settings_roundtrip_and_invalid_configuration(self):
        self.manager.configure("euw", "nvidia", 3)
        loaded = app.Manager(self.home)
        self.assertEqual((loaded.region, loaded.gpu, loaded.jobs), ("euw", "nvidia", 3))
        self.assertEqual(self.manager.config_file.stat().st_mode & 0o777, 0o600)
        self.manager.config_file.write_text('{"jobs": 0}')
        with self.assertRaisesRegex(app.Error, "jobs"):
            app.Manager(self.home)

    def test_dedicated_workspace_and_gpu_environment(self):
        self.manager.configure(gpu="nvidia")
        with patch.dict(os.environ, {"DYLD_INSERT_LIBRARIES": "unexpected.dylib"}):
            env = app.Manager(self.home).environment()
        self.assertEqual(env["DPREFIX"], str(self.home / "prefix"))
        self.assertEqual(env["XDG_DATA_HOME"], str(self.home / "runtime-data"))
        self.assertEqual(env["__NV_PRIME_RENDER_OFFLOAD"], "1")
        self.assertNotIn("DYLD_INSERT_LIBRARIES", env)

    def test_systemd_argument_escaping(self):
        self.assertEqual(app.systemd_quote('/tmp/a b%$"c'), '"/tmp/a b%%$$\\"c"')
        with self.assertRaises(app.Error):
            app.systemd_quote("/tmp/bad\nExecStart=anything")
        unit = app.unit_text(self.manager)
        self.assertIn("KillMode=control-group", unit)
        self.assertIn("Restart=no", unit)
        self.assertNotIn("[Install]", unit)
        self.assertNotIn("pkill", unit)

    def test_build_stops_at_missing_prerequisites(self):
        with patch.object(self.manager, "dependency_checks", return_value=[
                {"name": "nix", "ok": False, "detail": "missing"}]), \
                patch.object(self.manager, "run") as run:
            with self.assertRaisesRegex(app.Error, "nix: missing"):
                self.manager.prepare()
            run.assert_not_called()

    def test_build_umask_creates_launchctl_compatible_directories(self):
        directory = Path(self.tmp.name) / "LaunchDaemons"
        previous = os.umask(0o002)
        try:
            self.manager.run([sys.executable, "-c",
                              "import os, sys; os.mkdir(sys.argv[1])", str(directory)],
                             umask=0o022)
            inherited = os.umask(previous)
        finally:
            os.umask(previous)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o755)
        self.assertEqual(inherited, 0o002)

    def test_start_writes_only_selected_environment(self):
        config = Path(self.tmp.name) / "config"
        unit = config / "systemd/user" / app.UNIT
        unit.parent.mkdir(parents=True)
        unit.write_text("test unit")
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config), "DISPLAY": ":123",
                                      "UNRELATED_SECRET": "should-not-be-copied"}), \
                patch.object(self.manager, "package_ready", return_value=True), \
                patch.object(self.manager, "client_installed", return_value=True), \
                patch.object(self.manager, "run") as run:
            self.manager.start()
            self.assertEqual(run.call_args.args[0], ["systemctl", "--user", "start", app.UNIT])
        text = (self.home / "session.env").read_text()
        self.assertIn('DISPLAY=":123"', text)
        self.assertNotIn("UNRELATED_SECRET", text)

    def test_failed_upstream_validation_is_not_ready(self):
        backend = self.home / "package/bin/darling-lol"
        backend.parent.mkdir(parents=True)
        backend.write_text("#!/bin/sh\nexit 0\n")
        backend.chmod(0o755)
        script = self.manager.workspace / "scripts/nix-state.py"
        script.parent.mkdir(parents=True)
        script.write_text("raise SystemExit(1)\n")
        self.assertFalse(self.manager.package_ready())
        script.write_text("raise SystemExit(0)\n")
        self.assertTrue(self.manager.package_ready())

    def test_commands_preserve_literal_metacharacters_without_shell(self):
        text = 'value; $(touch /tmp/league-should-not-exist) `id`'
        result = self.manager.run([sys.executable, "-c", "import sys; print(sys.argv[1])", text], capture=True)
        self.assertEqual(result.stdout.strip(), text)

    def test_service_error_is_not_reported_as_inactive(self):
        bad = subprocess.CompletedProcess([], 1, "", "Failed to connect to bus")
        with patch.object(self.manager, "run", return_value=bad):
            with self.assertRaisesRegex(app.Error, "unavailable"):
                self.manager.status()

    def test_play_entry_starts_session_and_offers_stop_and_manager(self):
        entries = app.desktop_entries(Path('/home/u/my "bin"/league%linux'))
        play = entries["play"]
        self.assertIn('Exec="/home/u/my \\"bin\\"/league%%linux" play\n', play)
        self.assertIn('Actions=stop;manager;', play)
        self.assertIn('[Desktop Action stop]\nName=Stop session\nExec="/home/u/my \\"bin\\"/league%%linux" stop', play)
        self.assertIn('Exec="/home/u/my \\"bin\\"/league%%linux" gui', entries["manager"])

    def test_play_reports_failures_as_notifications(self):
        with patch.object(app.Manager, "start", side_effect=app.Error("Runtime is not built")), \
                patch.object(app, "notify") as notify, patch("sys.stderr"):
            self.assertEqual(app.main(["play"]), 1)
        notify.assert_called_once_with("League could not start", "Runtime is not built")
        with patch.object(app.Manager, "start"), patch.object(app, "notify") as notify:
            self.assertEqual(app.main(["play"]), 0)
        self.assertEqual(notify.call_args.args[0], "Starting League")

    def test_cosmic_terminal_action_uses_argument_separator(self):
        with patch("shutil.which", side_effect=lambda name: "/usr/bin/cosmic-term" if name == "cosmic-term" else None), \
                patch("subprocess.Popen") as launch:
            app.terminal("prepare")
        command = launch.call_args.args[0]
        self.assertEqual(command[:2], ["cosmic-term", "--"])
        self.assertEqual(command[-1], "prepare")
        self.assertTrue(command[-2].endswith("scripts/terminal-action.py"))


if __name__ == "__main__":
    unittest.main()
