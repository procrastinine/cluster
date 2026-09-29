#!/usr/bin/env python3
"""Local state and the login registry.

Run: python3 -m unittest tests.test_state
(or the whole suite: python3 -m unittest discover -s tests -p 'test_*.py')
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import time
import types
import unittest
from pathlib import Path

# For `support`, which puts this checkout on sys.path and sets up the sandbox
# every test runs in, so it is imported before anything from clustertool.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import FakeClock, _patched, _refusal, temp_state  # noqa: E402
from clustertool import config, platform as plat, state as state_module  # noqa: E402
from clustertool.backends import load  # noqa: E402
from clustertool.config import Settings  # noqa: E402
from clustertool.state import State  # noqa: E402
from clustertool.tmuxlayer import valid_name  # noqa: E402


class TestGlobalLoginNames(unittest.TestCase):
    """One name is one connection, on exactly one backend.

    The registry reads local state only, so these tests build a fake state tree
    and point config at it — no credentials, no network.
    """

    def setUp(self):
        from clustertool import config, registry

        self.config, self.registry = config, registry
        temp_state(self)

    def _make(self, backend, login, kind="node"):
        d = self.config.STATE_ROOT / backend
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{login}.{kind}").write_text("somenode\n")

    def test_finds_the_owning_backend(self):
        self._make("fasrc", "main")
        self._make("nersc", "work")
        self._make("fasrc", "meta-only", kind="json")
        for name, backend in (("main", "fasrc"), ("work", "nersc"),
                              ("meta-only", "fasrc"), ("nobody", None)):
            self.assertEqual(self.registry.find(name), backend, name)
        self.assertEqual(self.registry.collisions(), {})
        self.assertFalse(self.registry.is_taken_elsewhere("main", "fasrc"))
        self.assertTrue(self.registry.is_taken_elsewhere("work", "fasrc"))

    def test_a_name_on_two_backends_is_a_collision(self):
        self._make("fasrc", "main")
        self._make("nersc", "main")
        self.assertEqual(self.registry.collisions(), {"main": ["fasrc", "nersc"]})
        self.assertEqual(sorted(self.registry.backends_claiming("main")),
                         ["fasrc", "nersc"])

    def test_the_registry_and_the_state_agree_on_what_is_a_login(self):
        from clustertool import bridge

        self._make("nersc", "work")
        self._make("nersc", "api", kind="json")
        # A login whose state files were removed but whose master is still up
        # must not vanish from the registry, or it becomes unmanageable.
        (self.config.CTL_DIR / "cl-nersc-ghost.sock").write_text("")
        # What else is kept beside the logins is not one: mount and transfer
        # sockets, the bridge's record and lock, the refusal record.
        (self.config.CTL_DIR / "cl-nersc-mnt-work.sock").write_text("")
        (self.config.CTL_DIR / "cl-nersc-xfer-pool.sock").write_text("")
        record = bridge._state_path()
        self.assertEqual(record.parent, self.config.STATE_ROOT / "nersc")
        record.write_text('{"login": "main"}\n')
        (record.parent / "bridge.lock").write_text("")
        state = State(load("nersc"))
        state.refusals.refused("u@login05: Permission denied (publickey).")
        self.assertEqual(self.registry.logins_of("nersc"), ["api", "ghost", "work"])
        self.assertEqual(state.known_logins(), ["api", "ghost", "work"])

    def test_asking_creates_no_state_or_mount_directory(self):
        self.assertEqual(self.registry.logins_of("fasrc"), [])
        self.assertFalse((self.config.STATE_ROOT / "fasrc").exists())
        # Only a mount creates the mount root.
        state = State(load("nersc"))
        self.assertEqual(state.mount_root, self.config.MOUNT_ROOT / "nersc")
        self.assertFalse(self.config.MOUNT_ROOT.exists())


class TestAbandonedSessions(unittest.TestCase):
    """A session left on a node its login moved off must stay tracked.

    It keeps that login's ownership tag, and `clean` protects any session whose
    owner still exists — so without a record it would be invisible to `where`
    (which reports the login's *current* node) and unreapable forever. The record
    is local on purpose: you repin because a node died, and a dead node cannot be
    asked anything.
    """

    def setUp(self):
        self.config = config
        temp_state(self)
        self.state = State(load("nersc"))

    def test_records_and_forgets(self):
        self.assertEqual(self.state.abandoned(), [])
        self.state.abandon_record("login31", "x", "zztest")
        self.state.abandon_record("login31", "x", "a")      # not duplicated
        self.state.abandon_record("login31", "y", "a")
        self.state.abandon_record("login32", "x", "b")
        self.assertEqual(self.state.abandoned_on("login31"), ["x", "y"])
        self.state.abandon_forget("login31", "x")
        self.assertEqual(sorted(self.state.abandoned()),
                         [("login31", "y", "a"), ("login32", "x", "b")])
        # Same session name on another node must survive: the key is (node, name).
        self.assertEqual(self.state.abandoned_on("login32"), ["x"])
        self.state.abandon_forget("login31", "y")
        self.state.abandon_forget("login32", "x")
        self.assertEqual(self.state.abandoned(), [])
        # The file goes away entirely, so nothing later reads an empty record.
        self.assertFalse(self.state.abandoned_path.exists())

    def test_a_retired_owner_can_never_be_a_real_login(self):
        # That is what makes `clean` able to prove the session is an orphan.
        from clustertool.lifecycle import retired_owner

        tag = retired_owner("zztest", "login31")
        self.assertFalse(valid_name(tag))

    def test_a_garbled_line_is_skipped_not_fatal(self):
        self.state.abandoned_path.write_text("login31\tx\tzztest\nbroken\n\t\t\n")
        self.assertEqual(self.state.abandoned(), [("login31", "x", "zztest")])

    def test_each_change_is_read_and_written_under_the_lock(self):
        # Two commands abandoning sessions at once must not each write back a
        # copy that lacks the other's row, so the whole rewrite holds the lock.
        seen = []
        real_write = plat.atomic_write_text
        real_read = self.state.abandoned

        def write(path, text):
            seen.append(("write", plat.is_locked(self.state.abandoned_lock_path)))
            real_write(path, text)

        def read():
            seen.append(("read", plat.is_locked(self.state.abandoned_lock_path)))
            return real_read()

        with _patched(state_module.plat, "atomic_write_text", write), \
                _patched(self.state, "abandoned", read):
            self.state.abandon_record("login31", "x", "a")
            self.state.abandon_forget("login31", "y")
        self.assertEqual(seen, [("read", True), ("write", True)] * 2)
        self.assertFalse(plat.is_locked(self.state.abandoned_lock_path))

    def test_a_lock_that_stays_held_does_not_lose_the_change(self):
        holder = plat.FileLock(self.state.abandoned_lock_path)
        self.assertTrue(holder.acquire())
        try:
            with _patched(self.state, "_patience", lambda key="LOCK_PATIENCE": 0.2), \
                    contextlib.redirect_stderr(io.StringIO()) as said:
                self.state.abandon_record("login31", "x", "a")
        finally:
            holder.release()
        self.assertEqual(self.state.abandoned(), [("login31", "x", "a")])
        self.assertIn("has held the abandoned-session record", said.getvalue())
        self.assertIn("writing it anyway", said.getvalue())


class TestTotpPacing(unittest.TestCase):
    """The window claimed is the window the code is typed in.

    A code is generated when ssh asks for it, seconds after the claim, so a
    claim late in a window would type the next window's code, which another
    process may claim too; the cluster rejects the second use.
    """

    def setUp(self):
        temp_state(self)
        self.module = state_module
        self.state = state_module.State(
            types.SimpleNamespace(name="fasrc", paces_totp=True,
                                  settings=Settings("fasrc")))

    def pace(self, now, **kwargs):
        clock = FakeClock(now)
        with _patched(self.module, "time", clock):
            result = self.state.totp_pace(**kwargs)
        return result, clock

    def claimed(self):
        return int(self.state.totp_window_path.read_text())

    def test_the_window_claimed_has_time_left_and_no_other_claim(self):
        self.assertEqual(self.module.TOTP_MIN_LEFT, 8)
        for into, taken, slept, claimed in (
                (10, None, [], 100),        # 20 s left in window 100
                (22, None, [], 100),        # exactly the 8 s minimum left
                (25, None, [5.5], 101),     # 5 s left: about to end
                (10, "100", [20.5], 101)):  # claimed already
            with self.subTest(into=into, taken=taken):
                if taken:
                    self.state.totp_window_path.write_text(taken + "\n")
                ok, clock = self.pace(3000.0 + into)
                self.assertTrue(ok)
                self.assertEqual(clock.slept, slept)
                self.assertEqual(self.claimed(), claimed)
                self.state.totp_window_path.unlink()

    def test_without_waiting_a_late_window_is_refused(self):
        ok, clock = self.pace(3000.0 + 25, wait=False)
        self.assertFalse(ok)
        self.assertEqual(clock.slept, [])
        self.assertFalse(self.state.totp_window_path.exists())

    def _hold(self, seconds):
        import subprocess

        script = ("import fcntl, os, sys, time\n"
                  "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)\n"
                  "fcntl.flock(fd, fcntl.LOCK_EX)\n"
                  "os.ftruncate(fd, 0); os.write(fd, b'%d\\n' % os.getpid())\n"
                  "print('held', flush=True)\n"
                  "time.sleep(float(sys.argv[2]))\n")
        path = self.state.totp_lock_path
        path.parent.mkdir(parents=True, exist_ok=True)
        child = subprocess.Popen([sys.executable, "-c", script, str(path),
                                  str(seconds)], stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.assertEqual(child.stdout.readline().strip(), "held")
        return child

    def test_another_authentication_claiming_its_window_is_waited_for(self):
        self._hold(0.3)
        said = []
        with _patched(self.module, "time", FakeClock(3000.0 + 10)):
            self.assertTrue(self.state.totp_pace(log=said.append))
        self.assertIn("waiting for another authentication to claim its TOTP window",
                      said)

    def test_one_holder_sitting_on_the_lock_is_given_up_on(self):
        child = self._hold(30)
        with _patched(self.state, "_patience", self.patience(still=0.3)):
            self.assertFalse(self.state.totp_pace())
        self.assertIn(f"pid {child.pid}", self.state.totp_blocked)
        self.assertIn("with nothing moving", self.state.totp_blocked)

    def test_a_stopped_holder_is_named_as_such(self):
        import signal

        child = self._hold(30)
        os.kill(child.pid, signal.SIGSTOP)
        self.addCleanup(os.kill, child.pid, signal.SIGCONT)
        # A stopped holder is looked at once a second: given up on at the second look.
        with _patched(self.state, "_patience", self.patience(still=60, stopped=0.5)):
            started = time.monotonic()
            self.assertFalse(self.state.totp_pace())
        self.assertLess(time.monotonic() - started, 30)
        self.assertIn(f"pid {child.pid}", self.state.totp_blocked)
        self.assertIn("has been stopped for", self.state.totp_blocked)

    def test_a_caller_that_needs_the_window_dies_saying_what_held_it(self):
        child = self._hold(30)
        with _patched(self.state, "_patience", self.patience(still=0.3)):
            said = _refusal(self.state.claim_totp_window)
        self.assertIn("could not reserve a TOTP window", said)
        self.assertIn(f"pid {child.pid}", said)
        self.assertIn("with nothing moving", said)

    @staticmethod
    def patience(still=90, stopped=30):
        return lambda key="LOCK_PATIENCE": still if key == "TOTP_LOCK_PATIENCE" \
            else stopped

    def test_windows_being_claimed_count_as_the_queue_moving(self):
        """One process retrying keeps its pid, but each window it claims is progress."""
        import threading

        self._hold(1.5)
        stop = threading.Event()

        def claim_windows():
            window = 1
            while not stop.is_set():
                self.state.totp_window_path.write_text(f"{window}\n")
                window += 1
                stop.wait(0.1)

        claimer = threading.Thread(target=claim_windows)
        claimer.start()
        try:
            # Patience shorter than the hold, so ignoring the claims would give
            # up; long enough that a busy machine delaying the claimer thread
            # does not look like a still queue.
            with _patched(self.state, "_patience", self.patience(still=1.0)), \
                    _patched(self.module, "time", FakeClock(3000.0 + 10)):
                started = time.monotonic()
                held = self.state.totp_pace(wait=True)
        finally:
            stop.set()
            claimer.join()
        self.assertTrue(held)
        self.assertGreaterEqual(time.monotonic() - started, 1.0)

    def test_a_backend_without_pacing_claims_nothing(self):
        state = self.module.State(types.SimpleNamespace(name="nersc", paces_totp=False))
        self.assertTrue(state.totp_pace())
        self.assertFalse(state.totp_window_path.exists())


class TestSocketPathLength(unittest.TestCase):
    """ssh binds ControlPath plus 17 characters, within the system's limit."""

    def setUp(self):
        self.module = state_module

    def path_of(self, total):
        return "/" + "s" * (total - 1)

    def test_the_limit_with_the_suffix_is_107_bytes_on_linux_and_103_on_macos(self):
        for mac, fits, limit in ((False, 90, 107), (True, 86, 103)):
            with self.subTest(mac=mac), _patched(self.module.plat, "IS_MAC", mac):
                self.assertIsNone(self.module.socket_path_problem(self.path_of(fits)))
                problem = self.module.socket_path_problem(self.path_of(fits + 1))
                self.assertIn(f"{limit + 1} bytes", problem)
                self.assertIn(f"holds {limit}", problem)

    def test_the_length_is_counted_in_bytes(self):
        with _patched(self.module.plat, "IS_MAC", False):
            self.assertIsNotNone(
                self.module.socket_path_problem("/" + "\u00e9" * 46))

    def test_the_refusal_names_the_control_directory_setting(self):
        with _patched(self.module.plat, "IS_MAC", True):
            said = _refusal(lambda: self.module.require_socket_path(self.path_of(100)))
        self.assertIn("too long for this system", said)
        self.assertIn(str(config.CTL_DIR), said)
        self.assertIn("cluster config set CTL_DIR", said)


class TestStateRename(unittest.TestCase):
    """Renaming a login must move every per-login file, and nothing else."""

    def setUp(self):
        self.config = config
        temp_state(self)
        self.state = State(load("nersc"))

    def evidence(self, login, cell, sessions, loaded=True):
        """Write what `ls` last saw of *login*: its sessions cell and names."""
        row = ["nersc", login, "active", "login01", "login01", "-", cell]
        self.state.write_list_evidence([{"login": login, "row": row,
                                         "sessions": sessions,
                                         "sessions_loaded": loaded}])
        return row

    def test_moves_pin_meta_and_sockets_and_nothing_of_another_login(self):
        self.state.pin_write("old", "login09.example.gov")
        self.state.pin_write("bystander", "b.example.gov")
        self.state.write_meta("old", node="login09.example.gov")
        self.state.socket("old").write_text("")
        self.state.mount_socket("old").write_text("")

        moved = self.state.rename("old", "new")
        self.assertGreaterEqual(moved, 4)
        self.assertEqual(self.state.pin_read("new"), "login09.example.gov")
        self.assertEqual(self.state.pin_read("old"), "")
        self.assertEqual(self.state.pin_read("bystander"), "b.example.gov")
        self.assertTrue(self.state.socket("new").exists())
        self.assertFalse(self.state.socket("old").exists())
        self.assertTrue(self.state.mount_socket("new").exists())

    def test_rewrites_the_recorded_mountpoint_but_not_a_custom_one(self):
        # The mountpoint embeds the login name and is what mountpoint() returns;
        # a stale one would point the renamed login at the old directory.
        for old, new in ((str(self.state.default_mountpoint("old")),
                          str(self.state.default_mountpoint("new"))),
                         ("/somewhere/else", "/somewhere/else")):
            self.state.write_meta("old", mountpoint=old)
            self.state.rename("old", "new")
            self.assertEqual(self.state.read_meta("new")["mountpoint"], new)
            self.state.forget_login_files("new")

    def test_transfer_leases_belong_to_their_connection_not_to_a_login(self):
        # A lease directory is named after a transfer connection's tag, and a
        # login may share that name. Moving or removing it would hide live
        # leases from the next teardown, which then closes a connection that
        # a running transfer still uses.
        self.state.pin_write("pool", "login09.example.gov")
        leases = self.state.dir / "transfer-pool.users"
        leases.mkdir(parents=True)
        (leases / "12345").write_text("0")
        self.state.rename("pool", "work")
        self.assertTrue((leases / "12345").exists())
        self.assertFalse((self.state.dir / "transfer-work.users").exists())
        (leases / "12345").unlink()
        self.state.forget_login_files("work")
        self.state.forget_login_files("pool")
        self.assertTrue(leases.is_dir())

    def test_login_pinned_to_matches_across_spellings(self):
        # Sessions are node-local: one login per node, and the check must not
        # be defeated by one pin being an FQDN and the other a short name.
        short = lambda n: n.split(".")[0]  # noqa: E731
        pinned = self.state.login_pinned_to
        self.state.pin_write("main", "login09.example.gov")
        self.assertEqual(pinned("login09", short=short), "main")
        self.assertEqual(pinned("login09.example.gov", short=short), "main")
        # The login being moved does not collide with itself; a free node is free.
        self.assertEqual(pinned("login09", exclude="main", short=short), "")
        self.assertEqual(pinned("login10", short=short), "")
        # No normalizer: exact match only.
        self.assertEqual(pinned("login09.example.gov"), "main")
        self.assertEqual(pinned(""), "")

    def test_one_login_per_node_is_the_default(self):
        from clustertool.config import DEFAULTS, Settings
        self.assertEqual(DEFAULTS["ONE_LOGIN_PER_NODE"], 1)
        self.assertTrue(Settings("nersc").flag("ONE_LOGIN_PER_NODE"))

    def test_list_evidence_round_trips_for_completion(self):
        row = self.evidence("main", "x, work*", ["x", "work"])
        self.assertEqual(self.state.read_list_evidence()["evidence"][0]["row"], row)
        self.assertEqual(self.state.read_completion_sessions(),
                         {"main": ["work", "x"]})

    def test_a_live_read_that_fails_keeps_the_sessions_and_an_empty_one_clears_them(self):
        self.evidence("main", "x", ["x"])
        self.evidence("main", "?", [], loaded=False)
        self.assertEqual(self.state.read_completion_sessions(), {"main": ["x"]})
        self.assertEqual(
            self.state.read_list_evidence()["evidence"][0]["row"][6], "cached: x")
        self.evidence("main", "none", [])
        self.assertEqual(self.state.read_completion_sessions(), {})

    def test_a_confirmed_session_is_cached_without_waiting_for_ls(self):
        # A session that `cluster new` confirms goes into the saved evidence at
        # once, so the next `cluster ls` shows it before the live table
        # arrives.
        self.evidence("main", "x*", ["x"])
        self.assertEqual(self.state.note_sessions("main", add=["work"]),
                         ["x", "work"])
        item = self.state.read_list_evidence()["evidence"][0]
        self.assertEqual(item["sessions"], ["x", "work"])
        # Local evidence, so it is labelled as such rather than posing as a
        # live catalogue — and the attached marker is not carried over.
        self.assertEqual(item["row"][6], "cached: x, work")
        self.assertEqual(self.state.read_completion_sessions(),
                         {"main": ["work", "x"]})

    def test_a_confirmed_kill_leaves_the_cache(self):
        self.evidence("main", "x, work", ["x", "work"])
        self.assertEqual(self.state.note_sessions("main", remove=["x"]), ["work"])
        self.assertEqual(self.state.note_sessions("main", remove=["work"]), [])
        item = self.state.read_list_evidence()["evidence"][0]
        # An emptied list must say so; the old cell would still advertise work.
        self.assertEqual(item["row"][6], "-")
        self.assertEqual(self.state.read_completion_sessions(), {})

    def test_an_unchanged_note_rewrites_nothing(self):
        self.evidence("main", "x", ["x"])
        before = self.state.list_evidence_path.read_text()
        self.assertEqual(self.state.note_sessions("main", add=["x"]), ["x"])
        self.assertEqual(self.state.note_sessions("main", remove=["gone"]), ["x"])
        self.assertEqual(self.state.list_evidence_path.read_text(), before)

    def test_a_login_ls_has_never_seen_still_completes(self):
        # No row exists to correct, but the names file is what TAB reads and it
        # can be right immediately.
        self.assertEqual(self.state.note_sessions("fresh", add=["work"]), ["work"])
        self.assertEqual(self.state.read_completion_sessions(), {"fresh": ["work"]})
        self.assertEqual(self.state.read_list_evidence()["evidence"], [])

    def test_noting_one_login_keeps_what_is_known_about_another(self):
        self.evidence("main", "x", ["x"])
        self.state.note_sessions("fresh", add=["work"])
        self.state.note_sessions("main", add=["api"])
        self.assertEqual(self.state.read_completion_sessions(),
                         {"fresh": ["work"], "main": ["api", "x"]})

    def test_the_rename_carries_saved_evidence_to_the_new_name(self):
        self.evidence("old", "x", ["x"])
        self.state.rename("old", "new")
        item = self.state.read_list_evidence()["evidence"][0]
        self.assertEqual(item["login"], "new")
        self.assertEqual(item["row"][1], "new")
        self.assertEqual(item["sessions"], ["x"])
        self.assertEqual(self.state.read_completion_sessions(), {"new": ["x"]})


class TestTheSharedRefusalRecord(unittest.TestCase):
    """A refused credential is confirmed once, by one process, and then waits
    for the credential to change or for a person to connect by hand."""

    DENIED = "u@holylogin05: Permission denied (keyboard-interactive)."

    def setUp(self):
        temp_state(self)
        self.module = state_module
        self.now = 1_000_000.0
        self.clock = types.SimpleNamespace(
            time=lambda: self.now, localtime=time.localtime,
            strftime=time.strftime, sleep=time.sleep, monotonic=time.monotonic)
        self.marks = [["pass", 1, 2]]
        backend = types.SimpleNamespace(
            name="fasrc", settings=Settings("fasrc"),
            credential_marks=lambda: self.marks)
        self.record = state_module.Refusals(backend)

    def blocks(self, **kwargs):
        with _patched(self.module, "time", self.clock):
            return self.record.blocks(**kwargs)

    def refused(self):
        with _patched(self.module, "time", self.clock):
            self.record.settle(False, self.DENIED)

    def confirmed(self):
        """Refused, and refused again at the confirming try."""
        self.refused()
        self.now += 90
        self.blocks()
        self.refused()

    def claimed_by(self, pid, until=None):
        record = self.record.current()
        record["claim"] = pid
        if until is not None:
            record["claim_until"] = until
        self.record._write(record)

    def test_nothing_refused_blocks_nothing(self):
        self.assertEqual(self.blocks(), "")
        self.assertEqual(self.record.status(), "")

    def test_a_refusal_holds_every_unattended_try_until_the_confirming_one(self):
        self.refused()
        why = self.blocks()
        self.assertIn("the credentials were refused at", why)
        self.assertIn("Permission denied", why)
        self.assertIn("trying them once more at", why)
        self.now += 89
        self.assertIn("trying them once more at", self.blocks())

    def test_one_process_takes_the_confirming_try_and_the_others_wait(self):
        self.refused()
        self.now += 90
        self.assertEqual(self.blocks(), "", "the confirming try is taken")
        self.assertEqual(self.record.current()["claim"], os.getpid())
        self.assertEqual(self.blocks(), "", "and stays this process's")
        self.claimed_by(os.getppid(), self.now + 60)
        self.assertIn(f"pid {os.getppid()}", self.blocks())
        self.assertIn("is trying them once more", self.blocks())

    def test_a_claim_lasts_as_long_as_the_claimants_authentication_can(self):
        # A claimant that died, and whose pid a live process has since: its
        # claim ends when its try would have, not when that process does.
        self.refused()
        self.now += 90
        self.assertEqual(self.blocks(), "")
        bound = (180 + 2 * Settings("fasrc").int("STOP_TIMEOUT")
                 + Settings("fasrc").int("MASTER_READY_WAIT"))
        self.assertEqual(self.record.current()["claim_until"], self.now + bound)
        self.claimed_by(os.getppid())
        self.now += bound - 1
        self.assertIn("is trying them once more", self.blocks())
        self.now += 2
        self.assertEqual(self.blocks(), "", "the confirming try is free again")
        self.assertEqual(self.record.current()["claim"], os.getpid())

    def test_a_claimant_says_how_long_its_try_can_take(self):
        self.refused()
        self.now += 90
        self.assertEqual(self.blocks(bound=240), "")
        self.assertEqual(self.record.current()["claim_until"], self.now + 240)

    def test_a_claim_without_a_deadline_or_whose_holder_is_gone_is_free_again(self):
        self.refused()
        self.now += 90
        self.claimed_by(os.getppid())
        self.assertEqual(self.blocks(), "")
        self.claimed_by(2 ** 22 + 12345, self.now + 60)     # no such process
        self.assertEqual(self.blocks(), "")
        with _patched(self.module, "time", self.clock):
            self.record.settle(False, "Connection timed out")
        self.assertIsNone(self.record.current()["claim"],
                          "a try that ended in neither answer lets go")

    def test_a_second_refusal_after_the_delay_stops_every_unattended_try(self):
        self.confirmed()
        self.now += 10 ** 6
        why = self.blocks()
        self.assertIn("and again at", why)
        self.assertIn("until they change or someone connects by hand", why)
        self.assertIn("not retrying", self.record.status())

    def test_refusals_close_together_are_one_refusal(self):
        # Two processes refused in the same minute saw the same passing
        # cause, if it was one: a reused code, a clock off after a wake.
        self.refused()
        self.now += 5
        self.refused()
        self.assertFalse(self.record.current()["confirmed"])
        self.assertIn("trying once more", self.record.status())

    def test_the_record_holds_only_for_the_credential_refused(self):
        self.confirmed()
        self.marks = [["pass", 3, 4]]
        self.assertEqual(self.blocks(), "", "a changed credential is tried")
        self.assertEqual(self.record.status(), "")

    def test_a_success_clears_it(self):
        self.refused()
        with _patched(self.module, "time", self.clock):
            self.record.settle(True, "")
        self.assertFalse(self.record.path.exists())

    def test_a_person_connecting_by_hand_tries_it_and_is_told(self):
        self.confirmed()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.blocks(by_hand=True), "")
        self.assertIn("trying them again, as you asked", err.getvalue())

    def test_looking_takes_no_claim(self):
        self.refused()
        self.now += 90
        self.assertEqual(self.blocks(claim=False), "")
        self.assertIsNone(self.record.current()["claim"])

    def test_the_marks_are_a_stat_of_the_declared_credential_files(self):
        backend = load("fasrc")
        tmp = Path(temp_state(self)) / "creds"
        tmp.mkdir()
        (tmp / "pass").write_text("one\n")
        with _patched(backend, "cred_dir", tmp):
            first = backend.credential_marks()
            # Finder's .DS_Store, an editor's backup: nothing a connection presents.
            for clutter in (".DS_Store", "pass~", ".pass.swp", "notes.txt"):
                (tmp / clutter).write_text("clutter\n")
            self.assertEqual(backend.credential_marks(), first)
            (tmp / "pass").write_text("two, longer\n")
            self.assertNotEqual(backend.credential_marks(), first)
            second = backend.credential_marks()
            (tmp / "key.txt").write_text("JBSWY3DPEHPK3PXP\n")
            self.assertNotEqual(backend.credential_marks(), second,
                                "a credential file that appears is a change")
        self.assertEqual([mark[0] for mark in first],
                         sorted(f.filename for f in backend.CREDENTIALS))

    def test_a_new_nersc_certificate_is_not_a_new_password(self):
        # The record is sshproxy's refusal of the password: a certificate,
        # however new, changes nothing it is about.
        backend = load("nersc")
        tmp = Path(temp_state(self))
        with _patched(backend, "key_path", tmp / "nersc"):
            before = backend.credential_marks()
            backend.cert_path.write_text("cert\n")
            self.assertEqual(backend.credential_marks(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
