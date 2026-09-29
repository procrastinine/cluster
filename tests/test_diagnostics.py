#!/usr/bin/env python3
"""`status` and `doctor`: batched reads, boot entries and mount points.

Run: python3 -m unittest tests.test_diagnostics
(or the whole suite: python3 -m unittest discover -s tests -p 'test_*.py')
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

# For `support`, which puts this checkout on sys.path and sets up the sandbox
# every test runs in, so it is imported before anything from clustertool.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import REPO_ROOT, _IsolatedMachine, _patched  # noqa: E402
from clustertool import diagnostics, platform as plat  # noqa: E402


class TestStatusRoundTrips(unittest.TestCase):
    """Status batches node/session reads and reads shared crumbs only once."""

    class Backend:
        name = "example"
        label = "Example"
        user = "user"

        @staticmethod
        def credential_state():
            return "ok", "ready"

        @staticmethod
        def short(node):
            return (node or "").split(".")[0]

    class State:
        #: What `status` says of a refused credential; nothing, unless a test
        #: sets it.
        refused = ""

        @property
        def refusals(self):
            return SimpleNamespace(status=lambda: self.refused)

        def known_logins(self):
            return ["one", "two"]

        def pin_read(self, name):
            return f"{name}.example"

        def read_meta(self, _name):
            return {}

        def abandoned(self):
            return []

        def mountnode_read(self, _name):
            return ""

        def ledger_nodes(self):
            return []

    def status(self, crumbs_read=True, refused=""):
        """Run `status` over two live logins: (output, [(login, command)] sent)."""
        from clustertool import transfer
        from clustertool.tmuxlayer import CRUMBS_MARKER, LAYOUTS_MARKER, LS_MARKER, Tmux

        sent = []
        self.counted = []

        def remote_value(name, command, timeout=30):
            sent.append((name, command))
            # A shell rc file may print before the command's own output.
            reply = ["+--- Slurm Stats ---+", LS_MARKER, f"{name}.example",
                     f"session-{name}\t1\t0\t{name}\t\t"]
            if CRUMBS_MARKER in command and crumbs_read:
                # The home is shared, so every login would return this same row.
                reply += [CRUMBS_MARKER, "old-node\torphan-session\tone"]
            if LAYOUTS_MARKER in command:
                reply += [LAYOUTS_MARKER, "old-node"]
            return "\n".join(reply)

        state = self.State()
        state.refused = refused
        backend = self.Backend()
        settings = SimpleNamespace(int=lambda _key: 5)
        logins = SimpleNamespace(
            backend=backend, state=state, settings=settings,
            is_active=lambda _name: True,
            active_names=lambda: self.fail("active names were already collected"),
            live_node=lambda _name: self.fail("node must come from the batched read"),
            node_of=lambda name: f"{name}.example",
            remote_value=remote_value,
            run_remote=lambda *_a, **_kw: self.fail("status reads nothing unbatched"),
            connection_count=lambda active=None, transfers=None: (
                self.counted.append((active, transfers)) or 2),
        )
        ctx = SimpleNamespace(
            backend=backend, state=state, tmux=Tmux(logins), logins=logins,
            mounts=SimpleNamespace(
                is_mounted=lambda _name: False,
                watcher_running=lambda _name: True,
            ),
            settings=settings,
        )

        output = io.StringIO()
        with _patched(transfer.Transfers, "active_tags", lambda _self: []):
            with contextlib.redirect_stdout(output), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(diagnostics.status(ctx), 0)
        return output.getvalue(), sent

    def test_status_batches_each_login_and_deduplicates_shared_crumbs(self):
        from clustertool.tmuxlayer import CRUMBS_MARKER

        output, sent = self.status(
            refused="refused at 14:02 and 14:04; not retrying until the "
                    "credentials change or you connect by hand")
        self.assertIn("credential: refused at 14:02 and 14:04; not retrying", output)
        self.assertEqual([name for name, _command in sent], ["one", "two"],
                         "one round trip per live login")
        # The shared home is read once, with the first login's own read.
        self.assertIn(CRUMBS_MARKER, sent[0][1])
        self.assertNotIn(CRUMBS_MARKER, sent[1][1])
        # Full breadcrumb catalogue plus the actionable stranded-session callout.
        self.assertEqual(output.count("orphan-session"), 2)
        self.assertIn("layout snapshots on shared home: old-node\n", output)
        self.assertNotIn("Slurm", output)
        # The connection count asks no master status just asked.
        self.assertEqual(self.counted, [(["one", "two"], [])])
        self.assertIn("connections in use: 2/5", output)

    def test_a_breadcrumb_read_that_failed_is_not_called_empty(self):
        output, _sent = self.status(crumbs_read=False)
        self.assertIn("session breadcrumbs on shared home: unreadable", output)
        self.assertNotIn("session breadcrumbs on shared home: none", output)


class TestDoctorSignals(unittest.TestCase):
    def test_boot_entries_match_what_is_actually_installed(self):
        """Boot lines usually name a backend (`cluster --backend X boot`), so
        the check matches `boot` after any flags, not the literal
        `cluster boot`."""
        from clustertool.diagnostics import boot_entries

        crontab = (
            "@reboot /home/u/.local/bin/cluster --backend fasrc boot main >> /x 2>&1\n"
            "@reboot /home/u/.local/bin/cluster --backend nersc boot work >> /y 2>&1\n"
            "20 1,9,17 * * * /home/u/.local/bin/cluster bridge push --cron\n"
            "# @reboot cluster boot main\n"
        )
        found = boot_entries(crontab)
        self.assertEqual(len(found), 2)
        self.assertTrue(all("boot" in line for line in found))
        # The plain shape matches too, and a non-@reboot line never does.
        self.assertEqual(len(boot_entries("@reboot cluster boot main")), 1)
        self.assertEqual(boot_entries("0 * * * * cluster boot main"), [])
        self.assertEqual(boot_entries(""), [])


class TestMountPointHygiene(unittest.TestCase):
    """An unmounted mount point that holds files is silent data loss.

    A write into an unmounted mount point lands on the local disk — for
    example when a login declines to mount under ONE_MOUNT_PER_BACKEND and
    something writes to its mount point anyway.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.ctx = SimpleNamespace(state=SimpleNamespace(
            mount_root=self.root, known_logins=lambda: ["main"]))
        self.mounted = set()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(_patched(
            plat, "mount_table_has", lambda p: Path(p).name in self.mounted))
        (self.root / "main").mkdir()

    def test_a_non_empty_unmounted_mount_point_is_reported(self):
        (self.root / "main" / ".livecheck").write_text("x")
        findings = diagnostics.mountpoint_findings(self.ctx)
        self.assertEqual([ok for _l, _d, ok in findings], [False])
        self.assertIn("written to this machine", findings[0][1])
        self.assertTrue((self.root / "main").is_dir(), "must never delete data")

    def test_only_the_empty_directory_of_a_vanished_login_is_removed(self):
        (self.root / "project2").mkdir()
        findings = diagnostics.mountpoint_findings(self.ctx)
        self.assertEqual([ok for _l, _d, ok in findings], [True])
        self.assertFalse((self.root / "project2").exists())
        self.assertTrue((self.root / "main").is_dir(), "a live login's is left alone")
        self.assertEqual(diagnostics.mountpoint_findings(self.ctx), [])

    def test_a_mounted_path_is_never_stat_ed(self):
        """A wedged FUSE mount blocks stat(2) forever; only the mount table
        may be asked about a mounted path."""
        self.mounted.add("main")
        real_is_dir = Path.is_dir

        def guarded(self, *a, **k):
            if self.name == "main":
                raise AssertionError("stat'ed a mounted path")
            return real_is_dir(self, *a, **k)

        with _patched(Path, "is_dir", guarded):
            self.assertEqual(diagnostics.mountpoint_findings(self.ctx), [])



def _which(present):
    """A shutil.which that finds only the programs in *present*."""
    return lambda name: f"/usr/bin/{name}" if name in present else None


class TestDoctorFeatures(unittest.TestCase):
    """Every optional part says whether it is available, and if not, how."""

    def rows(self, mac, present, rclone=(None, None), unmount=True,
             macfuse=False):
        from pathlib import Path as RealPath

        from clustertool import globuslayer
        from clustertool.transfer import MIN_RCLONE, RcloneFound

        path, version = rclone
        found = RcloneFound(path, version,
                            None if version and version >= MIN_RCLONE else
                            "too old" if version else "not found")

        real_exists = RealPath.exists

        def exists(path, *a, **k):
            if str(path) == "/Library/Filesystems/macfuse.fs":
                return macfuse
            return real_exists(path, *a, **k)

        with _patched(plat, "IS_MAC", mac), \
                _patched(diagnostics.shutil, "which", _which(present)), \
                _patched(plat, "have_unmount", lambda: unmount), \
                _patched(diagnostics, "_rclone_found", lambda: found), \
                _patched(globuslayer, "find_cli",
                         lambda: "/usr/bin/globus" if "globus" in present else None), \
                _patched(RealPath, "exists", exists):
            return {feature: (available, detail)
                    for feature, available, detail in diagnostics.feature_rows()}

    def test_a_linux_machine_with_everything(self):
        rows = self.rows(False, {"sshfs", "rsync", "globus", "systemctl",
                                 "crontab"},
                         rclone=("/usr/bin/rclone", (1, 70, 0)))
        self.assertTrue(all(available for available, _d in rows.values()), rows)
        self.assertIn("rclone 1.70.0 at /usr/bin/rclone",
                      rows["transfer and archive-sync"][1])

    def test_a_bare_linux_machine_says_what_to_install(self):
        rows = self.rows(False, set(), unmount=False)
        self.assertFalse(any(available for available, _d in rows.values()))
        mounts = rows["mounts"][1]
        self.assertIn("sshfs and fusermount not installed", mounts)
        self.assertIn("apt install sshfs", mounts)
        self.assertIn("cluster config set AUTO_MOUNT 0", mounts)
        self.assertIn("apt install rsync", rows["push, pull and bridge"][1])
        transfer = rows["transfer and archive-sync"][1]
        self.assertIn("rclone not found", transfer)
        self.assertIn("cluster config set RCLONE /path/to/rclone", transfer)
        self.assertIn("pipx install globus-cli", rows["Globus transfers"][1])
        self.assertIn("no systemd", rows["linger shutdown hook"][1])
        self.assertIn("install cron", rows["restore after a reboot"][1])

    def test_an_old_rclone_is_named_with_the_version_needed(self):
        from clustertool.transfer import MIN_RCLONE

        rows = self.rows(False, set(), rclone=("/usr/bin/rclone", (1, 50, 0)))
        detail = rows["transfer and archive-sync"][1]
        self.assertIn("rclone 1.50.0 at /usr/bin/rclone is too old", detail)
        self.assertIn("need " + ".".join(map(str, MIN_RCLONE)), detail)

    def test_a_mac_without_macfuse_is_told_about_the_system_extension(self):
        rows = self.rows(True, {"sshfs", "systemctl"})
        available, detail = rows["mounts"]
        self.assertFalse(available)
        self.assertIn("macFUSE not installed", detail)
        self.assertIn("Privacy & Security", detail)
        self.assertTrue(self.rows(True, {"sshfs"}, macfuse=True)["mounts"][0])
        # Never a systemd unit on macOS, even with a systemctl on PATH.
        available, detail = rows["linger shutdown hook"]
        self.assertFalse(available)
        self.assertIn(diagnostics.NO_SYSTEMD_HINT, detail)
        self.assertIn("every LINGER_INTERVAL seconds while it is connected", detail)
        self.assertIn("may come after the connection is gone", detail)
        self.assertIn("not automatic on macOS", rows["restore after a reboot"][1])
        self.assertIn("docs/setup.md#macos-launchagents",
                      rows["restore after a reboot"][1])
        heading = "### macOS: LaunchAgents"
        self.assertIn(heading, (REPO_ROOT / "docs" / "setup.md").read_text(),
                      "the anchor the row points at")


class TestDoctorMachineChecks(unittest.TestCase):
    """The machine half of `doctor`, with this machine's programs replaced."""

    def run_checks(self, mac, portable=False, features=(), crontab=None):
        report = diagnostics.Report()
        out = io.StringIO()
        present = {"ssh", "ssh-keygen"} | ({"crontab"} if crontab else set())

        def run(argv, **kwargs):
            if argv[-1] == "-l":
                self.assertEqual(kwargs.get("timeout"), diagnostics.LOCAL_TIMEOUT)
                return crontab(argv, **kwargs)
            raise AssertionError(f"unexpected command {argv}")

        with _patched(plat, "IS_MAC", mac), \
                _patched(plat, "FORCE_PORTABLE", portable), \
                _patched(diagnostics.shutil, "which", _which(present)), \
                _patched(diagnostics, "feature_rows", lambda: list(features)), \
                _patched(diagnostics, "has_systemd", lambda: False), \
                _patched(diagnostics.subprocess, "run", run), \
                _patched(diagnostics, "_lock_excludes_processes",
                         lambda: (True, "logins are serialized with this")), \
                contextlib.redirect_stdout(out):
            diagnostics.machine_checks(report)
            rc = report.finish()
        return rc, out.getvalue()

    def test_a_missing_feature_is_off_and_never_ok(self):
        rc, out = self.run_checks(False, features=[
            ("Globus transfers", False, "globus-cli not installed; install it"),
            ("push, pull and bridge", True, "/usr/bin/rsync")])
        self.assertEqual(rc, 0, "an absent optional part is not a problem")
        self.assertIn("off   Globus transfers — globus-cli not installed", out)
        self.assertIn("ok    push, pull and bridge — available (/usr/bin/rsync)",
                      out)
        self.assertNotIn("ok    Globus", out)
        self.assertNotIn("tmux", out, "doctor asks nothing about a local tmux")

    def test_the_fuse_abort_does_not_apply_on_macos(self):
        _rc, out = self.run_checks(True)
        self.assertIn("n/a   fuse abort", out)
        self.assertIn("diskutil unmount force", out)
        self.assertNotIn("fuse abort available", out)
        self.assertNotIn("reboot restoration", out)
        # Forced portable shims are named on macOS too.
        _rc, out = self.run_checks(True, portable=True)
        self.assertIn("os — macOS (portable shims forced)", out)

    def test_a_crontab_that_does_not_answer_is_a_warning_not_a_hang(self):
        def slow(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

        rc, out = self.run_checks(False, crontab=slow)
        self.assertEqual(rc, 0)
        self.assertIn("`crontab -l` did not answer within", out)
        self.assertIn("warning", out)

    def test_the_lock_probe_leaves_nothing_behind(self):
        from clustertool import config

        with tempfile.TemporaryDirectory() as tmp:
            with _patched(config, "STATE_ROOT", Path(tmp)):
                ok, detail = diagnostics._lock_excludes_processes()
            self.assertTrue(ok, detail)
            self.assertEqual(list(Path(tmp).iterdir()), [])


class TestClockFinding(unittest.TestCase):
    def test_a_drifted_clock_says_what_breaks_and_one_with_no_offset_is_a_warning(self):
        for offset, via, ok, fatal, said in (
                (0.4, "ntp.example", True, None, "+0.40s via ntp.example"),
                (-42.0, "ntp.example", False, True, "system clock not NTP-synchronised; "
                                                    "TOTP codes will be rejected"),
                (None, "timedatectl", False, False, "system clock not NTP-synchronised")):
            with self.subTest(offset=offset):
                found_ok, detail, found_fatal = diagnostics.clock_finding(offset, via)
                self.assertEqual(found_ok, ok)
                if not ok:
                    self.assertEqual(found_fatal, fatal)
                self.assertIn(said, detail)


class TestShutdownHookWithoutSystemd(unittest.TestCase):
    """No systemd, no unit: nothing is written and the reason is said."""

    def test_install_writes_no_unit(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}), \
                _patched(diagnostics, "has_systemd", lambda: False):
            self.assertEqual(diagnostics.install_hook(), ("", diagnostics.NO_SYSTEMD))
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_macos_has_no_systemd_even_with_a_systemctl(self):
        with _patched(plat, "IS_MAC", True), \
                _patched(diagnostics.shutil, "which", _which({"systemctl"})):
            self.assertFalse(diagnostics.has_systemd())

    def test_a_missing_program_is_a_failed_step_not_a_traceback(self):
        run = diagnostics._hook_runner()
        proc = run(["/nonexistent/cluster-test-program"])
        self.assertEqual(proc.returncode, 127)

    def test_systemctl_is_asked_with_a_timeout(self):
        seen = []

        def run(argv, **kwargs):
            seen.append(kwargs.get("timeout"))
            return type("P", (), {"stdout": "enabled\n"})()

        with _patched(diagnostics.subprocess, "run", run):
            self.assertEqual(diagnostics._systemctl(["systemctl", "x"]), "enabled")
        self.assertEqual(seen, [diagnostics.LOCAL_TIMEOUT])

    def test_a_systemctl_that_does_not_answer_is_reported(self):
        def run(argv):
            raise subprocess.TimeoutExpired(argv, diagnostics.LOCAL_TIMEOUT)

        armed, detail = diagnostics.hook_report(run=run)
        self.assertFalse(armed)
        self.assertIn("systemctl did not answer within 10s", detail)

    def linger_install(self, mac):
        from clustertool.commands.maintenance import cmd_linger

        err = io.StringIO()
        with _patched(plat, "IS_MAC", mac), \
                _patched(diagnostics, "has_systemd", lambda: False), \
                _patched(diagnostics, "install_hook",
                         lambda *a, **k: self.fail("must not try to install")), \
                contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                cmd_linger(None, ["--install-hook"])
        self.assertEqual(caught.exception.code, 1)
        return err.getvalue()

    def test_linger_install_hook_says_why_and_on_macos_what_protects_instead(self):
        said = self.linger_install(mac=True)
        self.assertIn(diagnostics.NO_SYSTEMD, said)
        self.assertIn("cluster watch", said)
        said = self.linger_install(mac=False)
        self.assertIn(diagnostics.NO_SYSTEMD, said)
        self.assertNotIn("macOS", said)


class TestDoctorBackends(_IsolatedMachine):
    """`doctor` checks this machine first, whatever is set up."""

    def doctor(self, *argv, checked=None):
        seen = [] if checked is None else checked

        def backend_checks(ctx, report):
            seen.append(ctx.backend.name)
            report.check(f"{ctx.backend.name} checked", True)

        with _patched(diagnostics, "machine_checks",
                      lambda report: report.check("this machine", True)), \
                _patched(diagnostics, "backend_checks", backend_checks):
            return self.run_cli(*argv, "doctor")

    def test_with_nothing_set_up_it_names_every_backend(self):
        rc, out, _err = self.doctor()
        self.assertEqual(rc, 1)
        self.assertIn("this machine", out)
        for name in ("fasrc", "nersc"):
            self.assertIn(f"not set up; set it up with: cluster --{name} config "
                          "credentials", out)
        self.assertIn("none is set up on this machine yet", out)
        self.assertFalse(self.config.STATE_ROOT.exists())

    def test_one_backend_set_up_leaves_the_other_off(self):
        checked = []
        self.enrol("nersc")
        rc, out, _err = self.doctor(checked=checked)
        self.assertEqual(rc, 0, out)
        self.assertEqual(checked, ["nersc"])
        self.assertIn("off   fasrc — not set up", out)
        self.assertIn("no problems", out)

    def test_recorded_logins_without_a_username_are_a_warning(self):
        self.enrol("nersc")
        self.record_login("fasrc", "work")
        rc, out, _err = self.doctor()
        self.assertEqual(rc, 0)
        self.assertIn("logins are recorded for it (work)", out)
        self.assertIn("1 warning(s)", out)

    def test_a_named_backend_that_is_not_set_up_is_a_problem(self):
        checked = []
        self.enrol("fasrc")
        rc, out, _err = self.doctor("--nersc", checked=checked)
        self.assertEqual(rc, 1)
        self.assertEqual(checked, [])
        self.assertIn("FAIL  nersc — not set up", out)
        self.assertNotIn("fasrc", out)


class TestDoctorConfiguration(_IsolatedMachine):
    """The settings file, and the directories that hold settings and secrets."""

    def checks(self, run):
        report = diagnostics.Report()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            run(report)
            rc = report.finish()
        return rc, out.getvalue()

    def test_a_directory_of_secrets_open_to_others_is_a_warning(self):
        self.enrol("fasrc")
        self.config.CRED_ROOT.chmod(0o755)
        with _patched(self.config, "CONFIG_ROOT", self.root):
            rc, out = self.checks(diagnostics.configuration_checks)
        self.assertEqual(rc, 0, out)
        self.assertIn(f"chmod 700 {self.config.CRED_ROOT}", out)
        self.assertIn("none yet", out, "no settings file is no problem")

    def test_a_directory_that_is_not_yours_or_not_a_directory_says_so(self):
        self.enrol("fasrc")
        self.config.CRED_ROOT.chmod(0o700)
        with _patched(self.config, "CONFIG_ROOT", self.root), \
                _patched(os, "getuid", lambda: os.stat(self.root).st_uid + 1):
            _rc, out = self.checks(diagnostics.configuration_checks)
        self.assertIn(f"{self.config.CRED_ROOT} is mode 700; it belongs to uid", out)
        self.assertIn("not to you", out)
        stray = self.root / "not-a-directory"
        stray.write_text("")
        with _patched(self.config, "CONFIG_ROOT", stray):
            _rc, out = self.checks(diagnostics.configuration_checks)
        self.assertIn(f"warn  configuration directory — {stray} is not a directory", out)

    def test_a_malformed_settings_file_is_a_problem(self):
        self.config.SETTINGS_FILE.write_text("[global\n")
        rc, out = self.checks(diagnostics.configuration_checks)
        self.assertEqual(rc, 1)
        self.assertIn("FAIL  settings file", out)

    def test_settings_nothing_reads_are_named_but_not_judged(self):
        self.config.SETTINGS_FILE.write_text(
            "[global]\nMAX_LOGINS = 5\nMAX_LOGIN = 6\nRELAY_HOST = x\n"
            "[FASRC]\nLINGER = 1\n[nersc]\nKEY = ~/k\n[fasrc]\nKEY = ~/k\n"
            "[relay]\nHOST = y\nSELF = z\n")
        rc, out = self.checks(diagnostics.configuration_checks)
        self.assertEqual(rc, 0, out)
        self.assertIn("no problems", out)
        self.assertIn("note  settings nothing reads — [global] MAX_LOGIN, "
                      "[global] RELAY_HOST, [FASRC] LINGER, [fasrc] KEY, "
                      "[relay] SELF;", out)

    def test_other_files_beside_the_credentials_are_a_note(self):
        from clustertool.backends import load

        self.enrol("fasrc")
        directory = self.config.CRED_ROOT / "fasrc"
        directory.chmod(0o700)
        for name, text in (("pass", "secret"), ("key.txt", "JBSWY3DPEHPK3PXP"),
                           ("notes", "x")):
            (directory / name).write_text(text)
            (directory / name).chmod(0o600)
        rc, out = self.checks(lambda report: diagnostics.credential_checks(
            load("fasrc"), report))
        self.assertEqual(rc, 0, out)
        self.assertIn("no problems", out)
        self.assertNotIn("warning", out)
        self.assertIn("note  other files in the credential directory — notes; "
                      "this tool reads only user, pass, key.txt", out)
        (directory / "pass").chmod(0o644)
        rc, out = self.checks(lambda report: diagnostics.credential_checks(
            load("fasrc"), report))
        self.assertEqual(rc, 1, "an unreadable password is a missing credential")
        self.assertIn(f"chmod 600 {directory / 'pass'}", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
