#!/usr/bin/env python3
"""Mounts: busy versus wedged, probes, repair and one mount per backend.

Run: python3 -m unittest tests.test_mounts
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

# For `support`, which puts this checkout on sys.path and sets up the sandbox
# every test runs in, so it is imported before anything from clustertool.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import FakeClock, _IsolatedMachine, _patched, _refusal, temp_state  # noqa: E402
from clustertool import platform as plat  # noqa: E402
from clustertool.backends import load  # noqa: E402
from clustertool.config import Settings  # noqa: E402


_module_patches = contextlib.ExitStack()


def setUpModule():
    # Whatever this Mac's macFUSE can do, a mount here goes through the kext,
    # the same way these tests mount on Linux: what they test is the rest.
    from clustertool import macfuse

    _module_patches.enter_context(_patched(
        macfuse, "usable", lambda _preference="auto": ("kext", "")))


def tearDownModule():
    _module_patches.close()


class TestMountBusyVsWedged(unittest.TestCase):
    """A saturated mount and a wedged one both miss the probe deadline.

    The difference is whether the FUSE queue moves. One wedged mount sat at
    exactly 13 queued requests for days; a mount being walked by an editor's file
    search carries ~50 that drain. Remounting the second kind is pure churn, so
    they must not be conflated. The waits run on a virtual clock, with the
    settings' own timeout and grace.
    """

    def setUp(self):
        from clustertool import mounts as mounts_mod
        from clustertool.mounts import Mounts
        from clustertool.sshmux import Logins

        self.mod = mounts_mod
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for key, value in (("CLUSTER_MOUNT_CHECK_TIMEOUT", "1"),
                           ("CLUSTER_MOUNT_BUSY_GRACE", "2")):
            os.environ[key] = value
            self.addCleanup(os.environ.pop, key, None)
        self.mounts = Mounts(Logins(load("fasrc")))
        tmp = stack.enter_context(tempfile.TemporaryDirectory())
        # Point the probe at a directory that exists but never answers: the
        # prober's result file is what "answered" means, and nothing writes it.
        # Its own state directory, so it cannot race a real watcher's probes.
        self.mounts.mountpoint = lambda _name: Path(tmp)
        self.mounts.state.dir = Path(tmp)
        stack.enter_context(_patched(plat, "mount_table_has", lambda _p: True))
        stack.enter_context(_patched(mounts_mod, "time", FakeClock()))
        self.mounts._spawn_probe = lambda *a, **k: None

    def probe(self, queue, result=None):
        """A probe while fuse_waiting() returns each of *queue* in turn and
        the prober writes *result* (None: writes nothing)."""
        values = list(queue)
        if result is not None:
            self.mounts._spawn_probe = lambda _mp, path: path.write_text(result)
        with _patched(plat, "fuse_waiting",
                      lambda _mp: values.pop(0) if len(values) > 1 else values[0]):
            return self.mounts.probe("main")

    def test_the_queue_and_the_result_file_decide(self):
        M = self.mod
        cases = [
            # A queue that drains is a mount answering slowly.
            ([50, 44, 30, 12], None, M.BUSY, "turning over"),
            # The measured shape under a recursive scanner: refilled as fast
            # as it drains, 14,14,…,15,14 for fifteen seconds while answering.
            # Any movement means answering; requiring a dip below the opening
            # depth would have the watcher tear down a live mount.
            ([14, 14, 14, 15, 14], None, M.BUSY, "turning over"),
            # A wedged mount's signature: the same count forever.
            ([13], None, M.NO_ANSWER, "no answer"),
            ([0], None, M.NO_ANSWER, "no answer"),
            ([20, 0], None, M.ANSWERED, "drained"),
            # The prober's `2>` creates the file before the mkdir returns, so
            # a file without `rc=` is no answer: taking it for one would call
            # a wedged mount with a queue healthy. While busy it stays busy.
            ([13], "", M.NO_ANSWER, "no answer"),
            ([14, 14, 15, 14], "", M.BUSY, "turning over"),
            ([13], "rc=0\n", M.ANSWERED, ""),
            ([13], "mkdir: cannot create directory: Transport endpoint is not "
                   "connected\nrc=1\n", M.ERRORED, "Transport endpoint"),
            # A dead macFUSE daemon.
            ([13], "mkdir: /x/.probe: Device not configured\nrc=1\n", M.ERRORED,
             "Device not configured"),
        ]
        for queue, result, status, said in cases:
            with self.subTest(queue=queue, result=result):
                got, detail = self.probe(queue, result)
                self.assertEqual(got, status, detail)
                self.assertIn(said, detail)

    def test_busy_needs_no_intervention_and_every_status_has_a_label(self):
        M = self.mod
        self.assertIn(M.BUSY, M.STATUS_OK)
        self.assertNotIn(M.NO_ANSWER, M.STATUS_OK)
        for state in (M.ANSWERED, M.ERRORED, M.NO_ANSWER, M.BUSY):
            self.assertIn(state, M.STATUS_LABEL)


class TestProbeResultSweep(unittest.TestCase):
    """One probe must never delete a result file another probe is waiting on.

    The watcher probes on every tick, so a hand-run probe often runs beside it.
    A probe whose result file is deleted can never see `rc=` and reports a live
    mount as wedged, so a sweep removes only files whose prober is dead, plus
    its own finished ones.
    """

    def test_only_dead_probers_and_our_own_finished_probes_are_swept(self):
        from clustertool.mounts import Mounts
        from clustertool.sshmux import Logins

        mounts = Mounts(Logins(load("fasrc")))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        mounts.state.dir = Path(tmp.name)

        def result(pid, token, name="main"):
            path = Path(tmp.name) / f"probe-{name}-{pid}-{token}.result"
            path.write_text("")
            return path

        foreign = result(1, "333")            # pid 1 always exists: the watcher
        dead = result(4000000000, "444")      # too large for a pid; must not raise
        keep = result(os.getpid(), "111")     # this probe's own, still awaited
        done = result(os.getpid(), "222")     # this process's, finished
        other = result(os.getpid(), "1", name="main2")  # another login's
        mounts._sweep_probe_results("main", keep=keep)
        self.assertEqual([p.exists() for p in (foreign, dead, keep, done, other)],
                         [True, False, True, False, True])


class TestBusyMountIsNotRemounted(unittest.TestCase):
    """A slow mount must not be treated as a broken one.

    Remounting aborts the FUSE connection out from under whatever is reading and
    does nothing about the load, which simply re-saturates the new mount. And the
    probe it takes to decide costs MOUNT_CHECK_TIMEOUT + MOUNT_BUSY_GRACE, so it
    must not be paid twice for one answer.
    """

    def setUp(self):
        from clustertool import mounts as mounts_mod
        from clustertool.mounts import Mounts
        from clustertool.sshmux import Logins

        self.mod = mounts_mod
        self.mounts = Mounts(Logins(load("fasrc")))
        self.probes = []
        self.mounted = []

    def _probe_returns(self, status):
        def probe(name, timeout=None):
            self.probes.append(name)
            return status, "stubbed"
        self.mounts.probe = probe

    def _record_mount(self):
        def mount(name, **kwargs):
            self.mounted.append(kwargs)
            return True
        self.mounts.mount = mount

    def test_healthy_accepts_busy_and_rejects_wedged(self):
        self._probe_returns(self.mod.BUSY)
        self.assertTrue(self.mounts.healthy("main"))
        self._probe_returns(self.mod.NO_ANSWER)
        self.assertFalse(self.mounts.healthy("main"))

    def test_try_mount_probes_once_and_remounts_only_what_does_not_answer(self):
        for mounted, status, probes, health_checked in (
                # A busy mount must not be remounted.
                (True, self.mod.BUSY, 1, []),
                # try_mount and mount each probed; one answer, one probe, and
                # mount is told the health answer is already known.
                (True, self.mod.NO_ANSWER, 1, [True]),
                # Nothing to probe when nothing is mounted.
                (False, self.mod.ANSWERED, 0, [False])):
            with self.subTest(mounted=mounted, status=status):
                del self.probes[:], self.mounted[:]
                self.mounts.is_mounted = lambda _n, m=mounted: m
                self._probe_returns(status)
                self._record_mount()
                self.assertTrue(self.mounts.try_mount("main"))
                self.assertEqual(len(self.probes), probes)
                self.assertEqual([m["health_checked"] for m in self.mounted],
                                 health_checked)


class TestRepairLocking(unittest.TestCase):
    def test_rebuild_does_not_reacquire_the_login_lock(self):
        from clustertool.mounts import Mounts
        from clustertool.sshmux import Logins

        mounts = Mounts(Logins(load("fasrc")))
        released, created = [], []

        class Lock:
            def release(self):
                released.append(True)

        mounts.state.login_lock = lambda _name, wait=0: Lock()
        mounts.logins.is_active = lambda _name: False
        mounts.state.mountnode_read = lambda _name: ""
        mounts.failover = lambda _name, quiet=False: False
        mounts.unwedge = lambda _name, quiet=False: True
        mounts.logins.close = lambda *args, **kwargs: None
        mounts.logins.ensure = lambda *args, **kwargs: self.fail(
            "repair must not reacquire the lock it already holds")
        mounts.logins._ensure_locked = lambda name, quiet=False: created.append(name) or True
        mounts.mount = lambda *args, **kwargs: True
        mounts.healthy = lambda _name: True

        with _patched(mounts.settings, "int",
                      lambda key: 1 if key == "MOUNT_FAILOVER_AFTER" else 0):
            self.assertTrue(mounts.repair("main", attempt=2, quiet=True))
        self.assertEqual(created, ["main"])
        self.assertEqual(released, [True])


class TestOneMountPerBackend(unittest.TestCase):
    """One backend, one mount.

    Every login node of a backend serves the same home filesystem, so a second
    login's auto-mount is a duplicate: identical bytes under a second path, paid
    for with one of that connection's ten channels — and the channel budget is
    what actually runs out first.
    """

    def setUp(self):
        from clustertool.mounts import Mounts
        from clustertool.sshmux import Logins

        self.mounts = Mounts(Logins(load("fasrc")))
        self._real_table = plat.mount_table_has
        self._real_missing = plat.mount_tools_missing
        plat.mount_tools_missing = lambda: []

    def tearDown(self):
        plat.mount_table_has = self._real_table
        plat.mount_tools_missing = self._real_missing

    def _known(self, *names):
        self.mounts.state.known_logins = lambda: list(names)

    def _mounted(self, *names):
        paths = {str(self.mounts.mountpoint(name)) for name in names}
        plat.mount_table_has = lambda mp: str(mp) in paths

    def _context(self):
        from clustertool.context import Context

        ctx = Context("fasrc")
        # Bound first: binding starts the context with no Mounts of its own.
        self.assertEqual(ctx.backend.name, "fasrc")
        ctx._mounts = self.mounts
        return ctx

    def _auto_mount(self, login):
        from clustertool.commands.mounts import auto_mount

        return auto_mount(self._context(), login)

    def test_the_holder_is_the_other_mounted_login_default_first(self):
        # Two mounted logins: the answer must be the stable one, not whichever
        # name sorts first, because the holder's path is what gets quoted at you.
        for known, mounted, asking, holder in (
                (("main", "second"), ("main",), "second", "main"),
                (("aardvark", "main", "third"), ("aardvark", "main"), "third", "main"),
                (("main",), ("main",), "main", ""),     # never its own holder
                (("main", "second"), (), "second", "")):
            with self.subTest(known=known, mounted=mounted, asking=asking):
                self._known(*known)
                self._mounted(*mounted)
                self.assertEqual(self.mounts.mounted_elsewhere(asking), holder)
        self._known("main", "second")
        self._mounted("main")
        os.environ["CLUSTER_FASRC_ONE_MOUNT_PER_BACKEND"] = "0"
        self.addCleanup(os.environ.pop, "CLUSTER_FASRC_ONE_MOUNT_PER_BACKEND", None)
        self.assertEqual(self.mounts.mounted_elsewhere("second"), "",
                         "the policy can be switched off per backend")

    def test_auto_mount_declines_the_duplicate_and_says_where_the_mount_is(self):
        self._known("main", "second")
        self._mounted("main")
        tried = []
        self.mounts.try_mount = lambda name, **kw: tried.append(name)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertFalse(self._auto_mount("second"))
        self.assertEqual(tried, [], "the second login must not mount as well")
        self.assertIn("main", err.getvalue())
        self.assertIn("cluster_mounts/fasrc/main", err.getvalue())

    def test_a_login_with_its_own_mount_is_still_healed(self):
        # The policy declines to *create* a duplicate; it must never stop
        # try_mount from noticing that an existing mount has wedged.
        self._known("main", "second")
        self._mounted("main", "second")
        tried = []
        self.mounts.try_mount = lambda name, **kw: tried.append(name)
        with contextlib.redirect_stderr(io.StringIO()):
            self._auto_mount("second")
        self.assertEqual(tried, ["second"])

    def test_the_watcher_does_not_remount_a_sharing_login(self):
        from clustertool.watcher import Watcher

        self._known("main", "second")
        self._mounted("main")
        watcher = Watcher(self.mounts.logins, self.mounts, None, "second")
        watcher.logins.is_active = lambda _name, **kw: True
        self.mounts.try_mount = lambda *a, **kw: self.fail(
            "the watcher remounted a mount that is deliberately shared")
        logged = []
        watcher.log = logged.append
        self.assertTrue(watcher._tick())
        self.assertTrue(any("shared" in line for line in logged))
        # Every tick takes this branch; only a change of holder is worth a line.
        logged.clear()
        self.assertTrue(watcher._tick())
        self.assertEqual(logged, [])

    def test_list_distinguishes_sharing_from_unmounted(self):
        from clustertool.listing import list_evidence

        self._known("main", "second")
        self._mounted("main")
        ctx = self._context()
        ctx.logins.is_active = lambda _name: False
        self.assertEqual(list_evidence(ctx, "second")["row"][5], "shares main")
        self.assertEqual(list_evidence(ctx, "main")["row"][5], "mounted")


class TestMountIgnoresTheUsersSshConfig(_IsolatedMachine):
    def test_sshfs_is_given_the_same_config_as_every_other_ssh(self):
        from clustertool import mounts as mounts_mod, platform as plat_mod
        from clustertool.backends import load
        from clustertool.mounts import Mounts
        from clustertool.sshmux import Logins

        self.enrol("fasrc")
        mounts = Mounts(Logins(load("fasrc")))
        mounts.logins.is_active = lambda name: True
        mounts.logins.node_of = lambda name: "holylogin05.rc.fas.harvard.edu"
        ran = []

        def run(argv, timeout=None, **kw):
            ran.append(argv)
            return subprocess.CompletedProcess(argv, 1, "", "refused")

        with _patched(plat_mod, "mount_tools_missing", lambda: []), \
                _patched(plat_mod, "mount_table_has", lambda mp: False), \
                _patched(mounts_mod.plat, "run", run), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                mounts.mount("main", mountpoint=str(self.root / "mp"))
        # On macOS, the failed attempt's cleanup also lists processes (ps).
        argv, = [argv for argv in ran if argv[0] == "sshfs"]
        self.assertEqual(argv[0], "sshfs")
        self.assertEqual(argv[argv.index("-F") + 1], "/dev/null")
        self.assertLess(argv.index("-F"), argv.index("ControlMaster=no"))


class _MountHarness(unittest.TestCase):
    """A Mounts over a sandboxed state tree, with every outside effect faked.

    The mount table is a set, sshfs "mounts" by adding to it, and nothing
    touches FUSE, ssh or a real mount point.
    """

    BACKEND = "fasrc"

    def setUp(self):
        from clustertool import mounts as mounts_mod
        from clustertool.mounts import Mounts
        from clustertool.sshmux import Logins

        temp_state(self)
        self.mod = mounts_mod
        self.mounts = Mounts(Logins(load(self.BACKEND)))
        self.table = set()
        self.events = []
        self.ran = []
        self.logins = self.mounts.logins
        self.logins.is_active = lambda _name: True
        self.logins.node_of = lambda _name: "login01.example"
        self.logins._stop_pid = lambda pid: self.events.append(("stop", pid))
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for attr, value in (("mount_tools_missing", lambda: []),
                            ("mount_table_has", lambda mp: str(mp) in self.table),
                            ("run", self._run),
                            ("fuse_abort", self._abort),
                            ("unmount", self._unmount),
                            ("own_processes", lambda: self.processes)):
            stack.enter_context(_patched(plat, attr, value))
        self.processes = []
        self.sshfs_rc = 0

    def _run(self, argv, timeout=None, **_kw):
        self.ran.append(argv)
        if argv[0] == "sshfs" and self.sshfs_rc == 0:
            self.table.add(argv[-1])
        return subprocess.CompletedProcess(argv, self.sshfs_rc if argv[0] == "sshfs"
                                           else 0, "", "")

    def _abort(self, mp):
        self.events.append(("abort", str(mp)))
        return True

    def _unmount(self, mp):
        self.events.append(("unmount", str(mp)))
        self.table.discard(str(mp))
        return True

    def sshfs_argv(self):
        return next(argv for argv in self.ran if argv[0] == "sshfs")


class TestMountPointHandling(_MountHarness):
    """The mount table decides, before anything touches the mount point."""

    def test_the_first_mount_creates_the_mount_root_privately_and_then_reuses_it(self):
        from clustertool import config

        self.assertFalse(config.MOUNT_ROOT.exists())
        self.mounts.mount("main", quiet=True)
        mp = self.mounts.mountpoint("main")
        self.assertEqual(mp, config.MOUNT_ROOT / "fasrc" / "main")
        self.assertTrue(mp.is_dir())
        self.assertEqual(config.MOUNT_ROOT.stat().st_mode & 0o777, 0o700)
        self.assertIn(str(mp), self.table)
        # An existing mount point is used as it is.
        self.table.clear()
        self.mounts.mount("main", quiet=True)
        self.assertIn(str(mp), self.table)

    def test_a_stuck_mount_is_released_before_the_mount_point_is_touched(self):
        mp = self.mounts.mountpoint("main")
        self.table.add(str(mp))
        self.mounts.healthy = lambda _name: False
        made = []
        real_make = self.mounts._make_mountpoint

        def make(path):
            made.append(str(path) in self.table)
            real_make(path)

        self.mounts._make_mountpoint = make
        with _patched(plat, "IS_MAC", False):
            self.mounts.mount("main", quiet=True)
        self.assertEqual(self.events[:2], [("abort", str(mp)), ("unmount", str(mp))])
        self.assertEqual(made, [False], "the mount point was touched while mounted")

    def test_a_mount_that_will_not_let_go_stops_the_mount(self):
        mp = self.mounts.mountpoint("main")
        self.table.add(str(mp))
        self.mounts.healthy = lambda _name: False
        self.mounts.unwedge = lambda _name, quiet=True: False
        self.mounts._make_mountpoint = lambda _mp: self.fail("touched a stuck mount")
        said = _refusal(lambda: self.mounts.mount("main", quiet=True))
        self.assertIn("would not let go", said)

    def test_another_mount_point_already_in_use_is_left_alone(self):
        from clustertool import config

        other = config.MOUNT_ROOT / "fasrc" / "api"
        self.table.add(str(other))
        said = _refusal(lambda: self.mounts.mount("main", mountpoint=str(other)))
        self.assertIn("already a mount point", said)
        self.assertEqual(self.events, [])

    def test_a_mount_point_that_cannot_be_made_is_reported(self):
        import errno

        mp = self.mounts.mountpoint("main")
        real_mkdir = os.mkdir

        def refuse(path, *a, **kw):
            if str(path) == str(mp):
                raise OSError(errno.ENXIO, "Device not configured")
            return real_mkdir(path, *a, **kw)

        with _patched(self.mod.os, "mkdir", refuse):
            said = _refusal(lambda: self.mounts.mount("main", quiet=True))
        self.assertIn("cannot create the mount point", said)
        self.assertIn("Device not configured", said)

    def test_try_mount_carries_on_past_an_os_error(self):
        import errno

        def broken(_name):
            raise OSError(errno.ENXIO, "Device not configured")

        self.mounts.is_mounted = broken
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertFalse(self.mounts.try_mount("main"))
        self.assertIn("continuing without it", err.getvalue())
        self.assertIn("Device not configured", err.getvalue())

    def test_a_failed_mount_log_is_kept_bounded(self):
        log = self.mounts.state.sshfs_log_path("main")
        log.write_text("x" * 64)
        self.sshfs_rc = 1
        with _patched(plat, "LOG_LIMIT", 16):
            _refusal(lambda: self.mounts.mount("main", quiet=True))
        self.assertEqual(Path(str(log) + ".1").read_text(), "x" * 64)
        self.assertIn("mount failed", log.read_text())


class TestMissingSshfs(_MountHarness):
    """Without sshfs, mounts are off: said once, and explained when asked for."""

    def test_an_explicit_mount_names_what_to_install(self):
        for is_mac, words in ((False, "apt install sshfs"), (True, "macFUSE")):
            with _patched(plat, "mount_tools_missing", lambda: ["sshfs"]), \
                    _patched(plat, "IS_MAC", is_mac):
                said = _refusal(lambda: self.mounts.mount("main"))
            self.assertIn("sshfs is not installed", said)
            self.assertIn(words, said)
            self.assertIn("cluster config set AUTO_MOUNT 0", said)
        self.assertEqual(self.ran, [])

    def test_auto_mount_notes_it_once_and_mounts_nothing(self):
        from types import SimpleNamespace
        from clustertool.commands.mounts import auto_mount

        tried = []
        self.mounts.try_mount = lambda name, **kw: tried.append(name)
        ctx = SimpleNamespace(settings=self.mounts.settings, mounts=self.mounts)
        err = io.StringIO()
        with _patched(plat, "mount_tools_missing", lambda: ["sshfs"]), \
                contextlib.redirect_stderr(err):
            self.assertFalse(auto_mount(ctx, "main"))
        self.assertEqual(tried, [])
        self.assertEqual(len(err.getvalue().strip().splitlines()), 1)
        self.assertIn("mounts are off", err.getvalue())
        self.assertIn("cluster config set AUTO_MOUNT 0", err.getvalue())


class TestSshfsOptions(_MountHarness):
    def test_the_options_ride_the_login_and_suit_both_sshfs_versions(self):
        from clustertool import sshmux

        self.mounts.mount("main", quiet=True)
        argv = self.sshfs_argv()
        options = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-o"]
        # sshfs 2.5 (the macFUSE site's build) rejects dir_cache as unknown;
        # the cache it names is on by default in every version.
        self.assertNotIn("dir_cache=yes", options)
        for wanted in ("reconnect", "idmap=user", "follow_symlinks",
                       sshmux.NO_CONNECTION_OF_ITS_OWN):
            self.assertIn(wanted, options)
        self.assertNotIn(",", sshmux.NO_CONNECTION_OF_ITS_OWN,
                         "sshfs would split the option at the comma")
        rider = sshmux.rider_argv(self.mounts.state.socket("main"))[1:]
        self.assertIn(rider, [argv[i:i + len(rider)] for i in range(len(argv))],
                      "the rider's options, in order, with no program name")

    def test_only_macos_adds_a_volume_name_and_its_permission_options(self):
        for is_mac in (True, False):
            self.ran.clear()
            self.table.clear()
            with self.subTest(is_mac=is_mac), _patched(plat, "IS_MAC", is_mac):
                self.mounts.mount("main", quiet=True)
                argv = self.sshfs_argv()
                for option in ("volname=cluster-fasrc-main", "defer_permissions",
                               "noappledouble"):
                    self.assertEqual(option in argv, is_mac, option)

    def test_a_mount_with_no_master_says_the_connection_is_gone(self):
        self.sshfs_rc = 1
        self.mounts.logins.is_active = lambda _name: True

        def run(argv, timeout=None, **_kw):
            self.ran.append(argv)
            return subprocess.CompletedProcess(
                argv, 1, "", "cluster: the connection to login01 is gone or refused "
                "another session - not opening a new one\nread: Connection reset by peer\n")

        with _patched(plat, "run", run):
            said = _refusal(lambda: self.mounts.mount("main", quiet=True))
        self.assertIn("the connection to login01 is gone", said)
        self.assertNotIn("Connection reset", said)


class TestWhichDaemonsAreTheMounts(_MountHarness):
    """Only the processes serving this mount are ever stopped."""

    def test_sshfs_is_matched_on_the_whole_mount_point_spaces_and_all(self):
        mp = self.mounts.mountpoint("main")
        self.processes = [
            (10, f"sshfs -o reconnect user@login01:. {mp}"),
            (11, f"sshfs -o reconnect user@login01:. {mp}2"),
            (12, f"vim {mp}"),
            (13, f"/usr/bin/sshfs user@login01:. {mp} -o reconnect"),
        ]
        self.assertEqual(self.mounts.sshfs_pids("main"), [10, 13])
        mp = Path("/nonexistent/my mounts/main")
        self.processes = [(20, f"sshfs user@login01:. {mp}"),
                          (21, f"sshfs user@login01:. {mp}-old")]
        self.assertEqual(self.mounts.sshfs_pids("main", mp), [20])

    def test_the_sftp_channel_is_matched_on_the_whole_socket(self):
        sock = self.mounts.state.socket("main")
        self.processes = [
            (30, f"ssh -x -a -oClearAllForwardings=yes -oControlPath={sock} "
                 "user@login01 -s sftp"),
            (31, f"ssh -x -a -oClearAllForwardings=yes -oControlPath=/elsewhere{sock} "
                 "user@login01 -s sftp"),
            (32, f"ssh -o ControlPath={sock} -o ControlMaster=no user@login01 -s sftp"),
            (33, f"ssh -x -a -oClearAllForwardings=yes -o ControlPath={sock} "
                 "user@login01 -s sftp"),
        ]
        self.assertEqual(self.mounts.sftp_channel_pids("main"), [30, 33])


class TestUnwedgeOrder(_MountHarness):
    """Queued requests must fail before the unmount can succeed."""

    def test_the_queue_is_failed_first_then_unmounted(self):
        mp = self.mounts.mountpoint("main")
        sock = self.mounts.state.socket("main")
        self.processes = [
            (40, f"sshfs user@login01:. {mp}"),
            (41, f"ssh -oClearAllForwardings=yes -oControlPath={sock} user@login01 -s sftp"),
        ]
        # macFUSE has no abort file: the daemon's death is what fails the queue.
        for is_mac, order in ((False, [("abort", str(mp)), ("unmount", str(mp)),
                                       ("stop", 40), ("stop", 41)]),
                              (True, [("stop", 40), ("stop", 41), ("unmount", str(mp))])):
            self.table.add(str(mp))
            self.events.clear()
            with self.subTest(is_mac=is_mac), _patched(plat, "IS_MAC", is_mac):
                self.assertTrue(self.mounts.unwedge("main"))
                self.assertEqual(self.events, order)


class TestBusyWithoutAQueue(_MountHarness):
    """Where no FUSE queue can be read, the daemons' CPU time tells busy from wedged."""

    def setUp(self):
        super().setUp()
        for key, value in (("CLUSTER_MOUNT_CHECK_TIMEOUT", "1"),
                           ("CLUSTER_MOUNT_BUSY_GRACE", "2")):
            os.environ[key] = value
            self.addCleanup(os.environ.pop, key, None)
        mp = self.mounts.mountpoint("main")
        self.table.add(str(mp))
        self.processes = [(50, f"sshfs user@login01:. {mp}")]
        self.mounts._spawn_probe = lambda *a, **k: None

    def test_the_daemons_cpu_decides(self):
        for readings, status, said in (([12.0, 12.0, 12.4], self.mod.BUSY, "still working"),
                                       ([12.0], self.mod.NO_ANSWER, "idle"),
                                       ([None], self.mod.NO_ANSWER, "no answer")):
            values, asked = list(readings), []

            def cpu(pids):
                asked.append(list(pids))
                return values.pop(0) if len(values) > 1 else values[0]

            with self.subTest(readings=readings), \
                    _patched(plat, "fuse_waiting", lambda _mp: None), \
                    _patched(plat, "cpu_seconds", cpu), \
                    _patched(self.mod, "time", FakeClock()):
                got, detail = self.mounts.probe("main")
                self.assertEqual(got, status, detail)
                self.assertIn(said, detail)
                self.assertEqual(asked[0], [50], "the mount's own sshfs is the one read")


class TestFailoverOnAPasswordBackend(_MountHarness):
    """Probing a node over BatchMode cannot work where every connection types a
    password, and probing it any other way would spend an authentication."""

    def setUp(self):
        super().setUp()
        self.via = []
        self.mounts._mount_via = lambda name, node, quiet=False: (
            self.via.append(node) or node == "login03.example")
        self.mounts.failover_candidates = lambda name, avoid=(): [
            "login02.example", "login03.example"]
        self.remote = []
        self.logins.run_remote = lambda name, command, timeout=60: (
            self.remote.append(command)
            or subprocess.CompletedProcess([], 0, "ok\n" if "printf ok" in command
                                           else "0\n", ""))

    def test_failover_mounts_without_probing(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertTrue(self.mounts.failover("main"))
        self.assertEqual(self.via, ["login02.example", "login03.example"])
        self.assertEqual(self.ran, [], "a BatchMode probe was attempted")
        self.assertIn("without probing", err.getvalue())

    def test_a_refused_credential_stops_the_move_at_the_first_node(self):
        def refused(name, node, quiet=False):
            self.via.append(node)
            self.logins.last_failure = "Permission denied (keyboard-interactive)."
            return False

        self.mounts._mount_via = refused
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertFalse(self.mounts.failover("main"))
        self.assertEqual(self.via, ["login02.example"],
                         "each further node would spend a TOTP window on a refusal")
        self.assertIn("not trying other nodes", err.getvalue())

    def test_only_the_logins_own_node_is_probed_and_over_its_master(self):
        self.assertTrue(self.mounts.node_storage_ok("main", "login01.example"))
        self.assertFalse(self.mounts.node_overloaded("main", "login01.example"))
        self.assertEqual(len(self.remote), 2)
        self.assertIsNone(self.mounts.node_storage_ok("main", "login02.example"))
        self.assertFalse(self.mounts.node_overloaded("main", "login02.example"))
        self.assertEqual(len(self.remote), 2, "another node is not asked")
        self.assertEqual(self.ran, [])

    def test_failback_to_the_login_node_probes_over_the_login(self):
        self.mounts.state.mountnode_write("main", "login03.example")
        remounted = []
        self.mounts.mount = lambda name, quiet=False, node=None: remounted.append(node)
        self.mounts.close_mount_master = lambda name: None
        self.mounts.unwedge = lambda name, quiet=True: True
        self.assertTrue(self.mounts.failback("main", quiet=True))
        self.assertEqual(remounted, ["login01.example"])
        self.assertEqual(len(self.remote), 2)


class TestFailoverOnAKeyBackend(_MountHarness):
    BACKEND = "nersc"

    def test_a_node_is_probed_over_batchmode(self):
        self.assertFalse(self.mounts.node_storage_ok("main", "dtn02.example"))
        argv, = self.ran
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("ControlPath=none", argv)


class TestTheMountMaster(_MountHarness):
    """A relocated mount's master is opened like every other master."""

    BACKEND = "nersc"

    def setUp(self):
        super().setUp()
        self.opened = []
        self.logins.open_master = lambda sock, node, log, **kw: (
            self.opened.append((sock, node, log, kw)) or (True, ""))
        self.logins._socket_live = lambda sock: False
        self.logins.connection_count = lambda: 0
        self.mounts.backend.ensure_credential = lambda **kw: True

    def test_it_uses_open_master_with_its_own_socket_and_log(self):
        self.assertEqual(self.mounts._open_mount_master("main", "dtn02.example"),
                         (True, ""))
        (sock, node, log, kw), = self.opened
        self.assertEqual(sock, self.mounts.state.mount_socket("main"))
        self.assertEqual(node, "dtn02.example")
        self.assertEqual(log, self.mounts.state.mount_master_log_path("main"))
        self.assertGreaterEqual(kw["tries"], 1)

    def test_a_live_master_on_the_same_node_is_reused(self):
        self.logins._socket_live = lambda sock: True
        self.mounts.state.mountnode_write("main", "dtn02.example")
        self.assertEqual(self.mounts._open_mount_master("main", "dtn02.example"),
                         (True, ""))
        self.assertEqual(self.opened, [])

    def test_the_connection_limit_is_the_reason_given(self):
        self.logins.connection_count = lambda: 99
        opened, detail = self.mounts._open_mount_master("main", "dtn02.example")
        self.assertFalse(opened)
        self.assertIn("cluster connections", detail)

    def test_closing_sends_exit_over_the_control_socket(self):
        sock = self.mounts.state.mount_socket("main")
        sock.write_text("")
        self.mounts.close_mount_master("main")
        argv, = self.ran
        self.assertEqual(argv[:5], ["ssh", "-F", os.devnull, "-O", "exit"])
        self.assertFalse(sock.exists())


class _Watching(unittest.TestCase):
    """main's watcher over a sandboxed state tree, its log a list."""

    def setUp(self):
        from clustertool.mounts import Mounts
        from clustertool.sshmux import Logins
        from clustertool.watcher import Watcher

        temp_state(self)
        self.mounts = Mounts(Logins(load("fasrc")))
        self.state, self.logins = self.mounts.state, self.mounts.logins
        self.watcher = Watcher(self.logins, self.mounts, None, "main")
        self.logged = []
        self.watcher.log = self.logged.append


class TestOneWatcherPerLogin(_Watching):
    """The watcher holds its login's lock and records its own pid."""

    def test_the_watcher_holds_the_lock_and_writes_its_pid_while_it_runs(self):
        seen = []

        def watch():
            seen.append((plat.is_locked(self.state.watch_lock_path("main")),
                         self.state.watch_pid_path("main").read_text().strip()))
            return 0

        self.watcher._watch = watch
        self.assertEqual(self.watcher.run(), 0)
        self.assertEqual(seen, [(True, str(os.getpid()))])
        self.assertFalse(self.state.watch_pid_path("main").exists())
        self.assertFalse(plat.is_locked(self.state.watch_lock_path("main")))

    def test_a_second_watcher_leaves_the_first_one_alone(self):
        holder = plat.FileLock(self.state.watch_lock_path("main"))
        self.assertTrue(holder.acquire())
        self.addCleanup(holder.release)
        self.state.watch_pid_path("main").write_text("4242\n")
        self.watcher._watch = lambda: self.fail("a second watcher ran")
        self.assertEqual(self.watcher.run(), 1)
        self.assertEqual(self.state.watch_pid_path("main").read_text(), "4242\n")
        self.assertTrue(any("already watching" in line for line in self.logged))

    def test_a_pid_file_rewritten_by_someone_else_is_not_removed(self):
        def watch():
            self.state.watch_pid_path("main").write_text("4242\n")
            return 0

        self.watcher._watch = watch
        self.watcher.run()
        self.assertEqual(self.state.watch_pid_path("main").read_text(), "4242\n")

    def test_a_hangup_stops_the_watcher_between_ticks(self):
        import signal

        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            self.addCleanup(signal.signal, signum, signal.getsignal(signum))

        def tick():
            os.kill(os.getpid(), signal.SIGHUP)
            return True

        self.watcher._tick = tick
        self.watcher._assert_linger = lambda force=False: self.logged.append(
            f"linger force={force}")
        self.watcher._snapshot = lambda: None
        self.assertEqual(self.watcher._watch(), 0)
        self.assertEqual(self.logged[-2:], ["linger force=True", "watcher stopping"])

    def test_only_a_monitor_of_this_very_login_counts(self):
        self.state.watch_pid_path("main").write_text(f"{os.getpid()}\n")
        asked = []
        for args, expected in (("cluster --backend fasrc monitor main", os.getpid()),
                               ("cluster --backend fasrc monitor main2", None),
                               ("cluster --backend fasrc monitoring main", None)):
            with _patched(plat, "process_args",
                          lambda pid, a=args: asked.append(pid) or a):
                self.assertEqual(self.mounts.watcher_pid("main"), expected, args)
        self.assertEqual(len(asked), 3, "the command line is read once per check")


class TestTheWatcherBacksOff(_Watching):
    """Failed ticks fade with healthy ones; a burst backs off, and it never stops."""

    def setUp(self):
        super().setUp()
        self.watcher._assert_linger = lambda force=False: None
        self.watcher._snapshot = lambda: None
        self.watcher._repair_if_needed = lambda ok, attempt: None
        self.slept = []

    def run_ticks(self, results):
        results = list(results)

        def tick():
            return results.pop(0)

        def sleep(seconds):
            self.slept.append(seconds)
            if not results:
                self.watcher.stop = True

        self.watcher._tick = tick
        self.watcher._sleep = sleep
        self.assertEqual(self.watcher._watch(), 0)

    def test_a_burst_backs_off_doubling_up_to_the_ceiling(self):
        interval = Settings("fasrc").int("WATCH_INTERVAL")
        self.run_ticks([False] * 12)
        extra = [s - interval for s in self.slept]
        self.assertEqual(extra[:4], [0, 0, 0, 0])
        self.assertEqual(extra[4:8], [interval, 2 * interval, 4 * interval,
                                      8 * interval])
        self.assertEqual(max(extra), Settings("fasrc").int("WATCH_BACKOFF_MAX"))
        self.assertTrue(any("backing off" in line and "still watching" in line
                            for line in self.logged))

    def test_failures_among_healthy_ticks_fade_but_a_short_spell_keeps_a_burst(self):
        interval = Settings("fasrc").int("WATCH_INTERVAL")
        self.run_ticks(([False] + [True] * 120) * 6)
        self.assertEqual(set(self.slept), {interval}, "scattered never back off")
        self.slept.clear()
        self.watcher.stop = False
        self.run_ticks([False] * 6 + [True] + [False])
        self.assertGreater(self.slept[-1], interval)


class FakeChild:
    def __init__(self, pid, status=None):
        self.pid = pid
        self.status = status

    def poll(self):
        return self.status


class TestStartingTheWatcher(unittest.TestCase):
    def setUp(self):
        from clustertool.mounts import Mounts
        from clustertool.sshmux import Logins

        temp_state(self)
        os.environ["CLUSTER_WATCH_START_TIMEOUT"] = "1"
        self.addCleanup(os.environ.pop, "CLUSTER_WATCH_START_TIMEOUT", None)
        self.mounts = Mounts(Logins(load("fasrc")))
        self.spawned = []

    def start(self, child, running=lambda: None):
        pids = []

        def watcher_pid(_name):
            pids.append(1)
            return running() if len(pids) > 1 else None

        self.mounts.watcher_pid = watcher_pid
        err = io.StringIO()
        with _patched(plat, "spawn_detached",
                      lambda argv, log_path=None: self.spawned.append(log_path) or child), \
                contextlib.redirect_stderr(err):
            started = self.mounts.start_watcher("main")
        return started, err.getvalue()

    def test_a_watcher_is_reported_by_its_pid_or_its_status_and_a_lost_race_is_fine(self):
        for child, running, started, says, unsaid in (
                (FakeChild(777), lambda: 777, True, "pid 777", None),
                (FakeChild(778, status=2), lambda: None, False, "status 2", None),
                # Losing the race to another watcher is not a failure.
                (FakeChild(779, status=1), lambda: 4242, True, "", "exited")):
            with self.subTest(pid=child.pid):
                got, said = self.start(child, running)
                self.assertEqual(got, started)
                self.assertIn(says, said)
                if not started:
                    self.assertIn(str(self.mounts.state.watch_log_path("main")), said)
                if unsaid:
                    self.assertNotIn(unsaid, said)

    def test_the_watch_log_is_rotated_before_it_is_reopened(self):
        log = self.mounts.state.watch_log_path("main")
        log.write_text("x" * 64)
        with _patched(plat, "LOG_LIMIT", 16):
            self.start(FakeChild(780), running=lambda: 780)
        self.assertEqual(Path(str(log) + ".1").read_text(), "x" * 64)
        self.assertEqual(self.spawned, [log])


class TestTheWatcherAndItsCredentials(_Watching):
    """A refused credential on record holds reconnecting; a network blip does not."""

    DENIED = "Permission denied (keyboard-interactive)."

    def setUp(self):
        super().setUp()
        self.logins.is_active = lambda _name: False
        self.record = self.logins.state.refusals
        self.tries = []

    def fail_with(self, exc, last_failure=""):
        def ensure(name, quiet=False):
            self.tries.append(name)
            self.logins.last_failure = last_failure
            raise exc
        self.logins.ensure = ensure
        return self.watcher._tick()

    def test_every_failed_reconnect_is_logged_with_what_it_said(self):
        import http.client
        from clustertool import ui

        for exc, last_failure, logged in (
                (ui.Die(1), self.DENIED, f"reconnect failed: {self.DENIED}"),
                (SystemExit("cluster: sshproxy rejected the credentials: "
                            "Authentication failed\n"
                            "  This usually means a wrong password"), "",
                 "reconnect failed: sshproxy rejected the credentials: "
                 "Authentication failed This usually means a wrong password"),
                # Nothing was sent anywhere, so nothing was refused: the next
                # tick tries again, and finds the file once it is there.
                (FileNotFoundError("missing password file: /nonexistent/pass"), "",
                 "reconnect failed: missing password file: /nonexistent/pass"),
                (ui.Die(1), "Connection closed by 192.0.2.10 port 22",
                 "reconnect failed: Connection closed by 192.0.2.10 port 22"),
                # Not an OSError, so it needs catching in its own right.
                (http.client.IncompleteRead(b"partial", 20), "",
                 "reconnect failed: IncompleteRead(7 bytes read, 20 more expected)"),
                (ui.Die(1), "", "reconnect failed: no error output")):
            with self.subTest(logged=logged):
                self.assertFalse(self.fail_with(exc, last_failure))
                self.assertIn(logged, self.logged)
                self.assertIsNone(self.record.current())

    def test_a_refusal_on_record_holds_reconnecting_and_says_so_once(self):
        from clustertool import ui

        self.record.refused(self.DENIED)
        self.assertIsNone(self.fail_with(ui.Die(1), self.DENIED),
                          "neither a failure nor health")
        self.assertIsNone(self.watcher._tick())
        self.assertEqual(self.tries, [], "nothing authenticated")
        said = [line for line in self.logged if line.startswith("not reconnecting")]
        self.assertEqual(len(said), 1, self.logged)
        self.assertIn("the credentials were refused at", said[0])
        self.assertIn("config credentials", said[0])

    def test_the_confirming_try_is_the_watchers_once_it_is_due(self):
        from clustertool import ui

        self.record.refused(self.DENIED)
        self.watcher._tick()
        record = self.record.current()
        record["first"] -= 90
        self.record._write(record)
        self.assertFalse(self.fail_with(ui.Die(1), "Connection timed out"))
        self.assertEqual(self.tries, ["main"])
        self.assertIn("trying the refused credentials once more", self.logged)

    def test_a_try_held_by_the_record_is_no_failed_reconnect(self):
        # Another process took the confirming try between this watcher's look
        # and its own: nothing was sent, so nothing failed.
        from clustertool import ui

        def ensure(name, quiet=False):
            self.tries.append(name)
            self.logins.held = self.logins.last_failure = (
                "the credentials were refused at 12:00; pid 7 is trying them "
                "once more")
            raise ui.Die(1)

        self.logins.ensure = ensure
        self.assertIsNone(self.watcher._tick())
        self.assertEqual(self.tries, ["main"])
        self.assertFalse([line for line in self.logged
                          if line.startswith("reconnect failed")], self.logged)

    def test_a_changed_credential_is_reconnected_with(self):
        from clustertool import ui

        self.record.refused(self.DENIED)
        self.watcher._tick()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        (Path(tmp.name) / "pass").write_text("new\n")
        with _patched(self.logins.backend, "cred_dir", Path(tmp.name)):
            self.fail_with(ui.Die(1), "Connection timed out")
        self.assertEqual(self.tries, ["main"])
        self.assertTrue(any("no longer on record" in line for line in self.logged))

    def test_a_repair_that_raises_or_is_cut_short_is_logged_not_fatal(self):
        import errno
        import http.client

        for exc, said in (
                (http.client.RemoteDisconnected("Remote end closed connection"),
                 "repair aborted: Remote end closed connection"),
                (OSError(errno.ENXIO, "Device not configured"), "Device not configured")):
            with self.subTest(exc=type(exc).__name__):
                def repair(*a, exc=exc, **kw):
                    raise exc

                self.mounts.repair = repair
                self.watcher._repair_if_needed(False, 1)
                self.assertTrue(any("repair aborted" in line and said in line
                                    for line in self.logged), self.logged)

    def test_a_lock_wait_that_ended_says_why(self):
        # ensure's own refusal, as the watcher logs it: why the wait for the
        # login lock ended, never "no error output".
        state = self.logins.state
        why = ("pid 4242 (cluster login) holds login 'main's lock and has been "
               "stopped for 31s (resume it with fg, or end it)")

        def login_lock(*_a, **_kw):
            state.login_lock_blocked = why
            return None

        with _patched(state, "login_lock", login_lock), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(self.watcher._tick())
        self.assertIn(f"reconnect failed: {why}", self.logged)

    def test_without_sshfs_the_watcher_leaves_mounts_alone(self):
        self.logins.is_active = lambda _name: True
        self.mounts.try_mount = lambda *a, **kw: self.fail("mounted without sshfs")
        with _patched(plat, "mount_tools_missing", lambda: ["sshfs"]):
            self.assertTrue(self.watcher._tick())
            self.assertTrue(self.watcher._tick())
        self.assertEqual(self.logged, ["sshfs is not installed, so mounts are off"])


class TestTheWatchLogStaysBounded(unittest.TestCase):
    def test_a_long_running_watcher_starts_a_fresh_log(self):
        # Run in a child: the watcher points its own stdout at the new file,
        # which must not happen to the test runner's.
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "watch-main.log"
            script = "\n".join([
                "import sys, types",
                f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})",
                "from pathlib import Path",
                "from clustertool import platform as plat",
                "from clustertool.watcher import Watcher",
                "plat.LOG_LIMIT = 200",
                f"log = Path({str(log)!r})",
                "state = types.SimpleNamespace(watch_log_path=lambda name: log)",
                "logins = types.SimpleNamespace(state=state, settings=None)",
                "watcher = Watcher(logins, None, None, 'main')",
                "for n in range(20):",
                "    watcher.log(f'line {n:02d} ' + 'x' * 20)",
            ])
            with open(log, "ab") as handle:
                subprocess.run([sys.executable, "-c", script], stdout=handle,
                               stderr=subprocess.STDOUT, check=True, timeout=60)
            old = Path(str(log) + ".1").read_text()
            new = log.read_text()
            # One generation is kept: the newest lines in the log, the ones
            # before them in .1, and nothing grows past the limit by much.
            self.assertIn("line 19", new)
            self.assertIn("line", old)
            self.assertNotIn("line 19", old)
            self.assertNotIn("line 00", new + old)
            self.assertLess(len(new), 400)
            self.assertLess(len(old), 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
