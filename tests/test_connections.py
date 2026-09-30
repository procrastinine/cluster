#!/usr/bin/env python3
"""Connections: control masters, the channel budget and connection retries.

Also the disposable shell and boot recovery.

Run: python3 -m unittest tests.test_connections
(or the whole suite: python3 -m unittest discover -s tests -p 'test_*.py')
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# For `support`, which puts this checkout on sys.path and sets up the sandbox
# every test runs in, so it is imported before anything from clustertool.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import FakeClock, _patched, _refusal, hold_flock, temp_state  # noqa: E402
from clustertool import (agentscope, bridge, config, linger, platform as plat,  # noqa: E402
                         sshmux, state as state_module, ui)
from clustertool.backends import load  # noqa: E402
from clustertool.commands import mounts, sessions  # noqa: E402
from clustertool.config import Settings  # noqa: E402
from clustertool.sshmux import Logins  # noqa: E402
from clustertool.tmuxlayer import Tmux  # noqa: E402


class TestDisposableShell(unittest.TestCase):
    def test_direct_shell_has_no_master_pin_or_managed_login(self):
        calls = []

        class Backend:
            settings = Settings("fasrc")
            paces_totp = interactive_auth = records_refusals = False
            pool_host = "example"

            def ensure_credential(self):
                calls.append("credential")

            def ssh_argv(self, **kwargs):
                calls.append(("argv", kwargs))
                return ["ssh", "example"]

            def exec_interactive(self, argv, transcript=None):
                calls.append(("exec", argv))
                return 17

        # No socket/pin/login methods exist on purpose: using one would fail.
        state = SimpleNamespace()
        logins = Logins(Backend(), state=state)
        with _patched(plat, "save_tty", lambda: None):
            with _patched(plat, "restore_tty", lambda _saved: None):
                self.assertEqual(logins.disposable_interactive(), 17)
        self.assertEqual(calls[0], "credential")
        options = calls[1][1]["extra"]
        self.assertIn("ControlMaster=no", options)
        self.assertIn("ControlPath=none", options)
        self.assertIn("ControlPersist=no", options)
        self.assertEqual(calls[-1], ("exec", ["ssh", "example"]))

    def test_bare_cli_shell_is_disposable_but_named_shell_is_managed(self):
        calls = []
        logins = SimpleNamespace(
            disposable_interactive=lambda: calls.append("disposable") or 3,
            ensure=lambda name: calls.append(("ensure", name)),
            interactive=lambda name: calls.append(("interactive", name)) or 4,
        )
        ctx = SimpleNamespace(logins=logins, login=lambda name: name,
                              by_hand=lambda: calls.append("by hand"))
        with _patched(sessions, "auto_mount", lambda _ctx, name, skip=False:
                      calls.append(("mount", name, skip))):
            self.assertEqual(sessions.cmd_shell(ctx, []), 3)
            self.assertEqual(calls, ["by hand", "disposable"])
            calls.clear()
            self.assertEqual(sessions.cmd_shell(ctx, ["main"]), 4)
        self.assertEqual(calls, [
            "by hand", ("ensure", "main"), ("mount", "main", False),
            ("interactive", "main"),
        ])


class TestAuthenticatingWithoutAMaster(unittest.TestCase):
    """A connection of its own (clean's direct sweep, `sh`, rescue, init's
    test login) is held to the shared refusal record as a master's open is."""

    DENIED = "u@holylogin05: Permission denied (keyboard-interactive)."

    def setUp(self):
        temp_state(self)
        self.logins = Logins(load("fasrc"))
        self.logins.state.totp_pace = lambda log=None: True
        self.ran = []

    def direct(self, rc, text=""):
        def run():
            self.ran.append(rc)
            return subprocess.CompletedProcess([], rc, text, text)

        with contextlib.redirect_stderr(io.StringIO()):
            return self.logins.authenticate_directly("holylogin05", run)

    def test_a_refusal_goes_on_the_record_and_holds_the_next_unattended_one(self):
        self.assertIsNotNone(self.direct(255, self.DENIED))
        self.assertEqual(self.logins.last_failure, self.DENIED)
        self.assertIsNotNone(self.logins.state.refusals.current())
        self.assertIsNone(self.direct(0))
        self.assertEqual(self.ran, [255], "nothing was sent")
        self.assertIn("the credentials were refused at", self.logins.held)
        # One by hand goes ahead, and what it finds clears the record: the
        # far side's own failure means the credential was taken.
        self.logins.backend.by_hand = True
        self.assertEqual(self.direct(1).returncode, 1)
        self.assertIsNone(self.logins.state.refusals.current())
        self.assertEqual(self.logins.last_failure, "")


class TestMasterReadiness(unittest.TestCase):
    """A freshly forked master is polled for, not asked about once.

    `ssh -f` returns as soon as it has forked, so a single check immediately
    afterwards is a race. Losing that race is not a failure: the socket is
    polled until it answers or the deadline passes.
    """

    def wait(self, answers, timeout):
        logins = Logins(load("fasrc"))
        calls = []
        logins._socket_live = lambda _sock: calls.append(1) or answers(len(calls))
        start = time.monotonic()
        live = logins.wait_socket_live(Path("/nonexistent.sock"), timeout=timeout)
        return live, len(calls), time.monotonic() - start

    def test_a_master_is_waited_for_until_it_is_live(self):
        live, looks, _took = self.wait(lambda looks: looks >= 3, 5)
        self.assertTrue(live)
        self.assertGreaterEqual(looks, 3)
        live, looks, took = self.wait(lambda _looks: True, 5)
        self.assertEqual((live, looks), (True, 1))
        self.assertLess(took, 0.5, "an already live master returns at once")
        # An intermittent open must be retried rather than surfaced, so the
        # configured attempt count has to be greater than one.
        self.assertGreater(Settings("fasrc").int("TRANSFER_OPEN_TRIES"), 1)

    def test_gives_up_on_a_master_that_never_comes_up(self):
        live, _looks, took = self.wait(lambda _looks: False, 0.3)
        self.assertFalse(live)
        # It must actually wait out the deadline rather than returning at once,
        # and must not overrun it by much either.
        self.assertGreaterEqual(took, 0.3)
        self.assertLess(took, 2.3)


class TestChannelBudget(unittest.TestCase):
    """One connection, MaxSessions channels, shared by everything riding it.

    sshd refuses the overrun as "channel N: open failed: connect failed: open
    failed", naming neither the limit nor what filled it, and `ssh -O check` still
    answers "Master running" — so this has to be accounted for locally to be
    explainable at all.
    """

    def setUp(self):
        self.logins = Logins(load("fasrc"))
        self.sock = str(self.logins.state.socket("main"))

    def clients(self, entries):
        with _patched(plat, "own_processes", lambda: entries):
            return [kind for _pid, kind in self.logins.channel_clients("main")]

    def test_what_holds_a_session_channel(self):
        sock = self.sock
        cases = [
            # The master itself, and control commands, hold none.
            ([(1, f"ssh -o ControlPath={sock} -o ControlMaster=yes -N -f host")], []),
            ([(1, f"ssh -O check -o ControlPath={sock} -F /dev/null dummy")], []),
            # sshfs names the control path too, but the channel is its child's:
            # counting both would count the mount twice and under-report the
            # free channels.
            ([(1, f"sshfs -o ControlPath={sock} -o ControlMaster=no user@host:. /mnt"),
              (2, f"ssh -x -a -oClearAllForwardings=yes -oControlPath={sock} "
                  "user@host -s sftp")], ["sshfs mount"]),
            ([(n, f"ssh -F /dev/null -o ControlPath={sock} -o ControlMaster=no "
                  f"-t user@host tmux new-session -A -s s{n}") for n in (10, 11, 12)],
             ["interactive session"] * 3),
            # Another login's clients are not counted.
            ([(1, "ssh -o ControlPath=/tmp/cl-fasrc-other.sock -t user@host tmux")], []),
        ]
        for entries, kinds in cases:
            self.assertEqual(self.clients(entries), kinds, entries)

    def test_free_is_the_limit_minus_what_is_held_and_never_negative(self):
        for limit, held, free in (("10", 6, 4), ("2", 5, 0)):
            entries = [(n, f"ssh -o ControlPath={self.sock} -o ControlMaster=no "
                           "-t user@host tmux") for n in range(held)]
            with mock.patch.dict(os.environ, {"CLUSTER_SSH_MAX_SESSIONS": limit}), \
                    _patched(plat, "own_processes", lambda entries=entries: entries):
                self.assertEqual(self.logins.channels_in_use("main"), held)
                self.assertEqual(self.logins.channels_free("main"), free)


class TestOutOfChannelsDetection(unittest.TestCase):
    """Recognising sshd's refusal, which is the only signal that crosses the wire.

    Getting this wrong is expensive in both directions: missing it makes restore
    tear down a perfectly good master (dropping every other attach on it, killing
    any transfer in flight, and spending a TOTP window), while a false positive
    would leave a genuinely dead connection un-rebuilt.
    """

    def test_only_sshds_refusal_of_a_session_is_channel_exhaustion(self):
        from clustertool.sshmux import out_of_channels

        cases = [("channel 22: open failed: connect failed: open failed\n", True),
                 ("channel 3: open failed: administratively prohibited: open failed\n",
                  True),
                 ("ssh: connect to host x port 22: Connection refused\n", False),
                 ("Connection to host closed by remote host.\n", False),
                 ("control socket connect: No such file or directory\n", False),
                 ("", False),
                 # A refused port forward says "open failed" too, but not for
                 # a session.
                 ("channel 4: open failed: unknown channel type: open failed\n", False)]
        for text, exhausted in cases:
            proc = subprocess.CompletedProcess(["ssh"], 255, "", text)
            self.assertEqual(out_of_channels(proc), exhausted, text)


class TestBootRecovery(unittest.TestCase):
    """cmd_boot leaves what it cannot do to the watcher, and never tries a
    refused credential twice: the watcher confirms it once, and boot trying it
    BOOT_TRIES times would spend a TOTP window on each."""

    def boot(self, ensure, args=("--wait", "0"), dns=True, backend="fasrc",
             last_failure=""):
        self.watched, self.mounted = [], []
        logins = SimpleNamespace(last_failure=last_failure, node_of=lambda _n: "node")
        logins.ensure = lambda name: ensure(logins, name)
        ctx = SimpleNamespace(
            backend=SimpleNamespace(pool_host="pool.example", short=lambda n: n,
                                    reach_host=lambda: ("pool.example", 22)),
            login=lambda name: name, settings=Settings(backend), logins=logins,
            mounts=SimpleNamespace(
                start_watcher=lambda name: self.watched.append(name) or True))
        with _patched(mounts.plat, "dns_ok", lambda _host: dns), \
                _patched(mounts, "auto_mount",
                         lambda _ctx, name: self.mounted.append(name)), \
                _patched(mounts.time, "sleep", lambda _s: self.fail("boot waited")), \
                contextlib.redirect_stderr(io.StringIO()) as said:
            self.assertEqual(mounts.cmd_boot(ctx, ["main", *args]), 1)
        self.assertEqual(self.watched, ["main"])
        self.assertEqual(self.mounted, [])
        return said.getvalue()

    def test_what_boot_cannot_do_now_is_left_to_the_watcher(self):
        def fail(_logins, _name):
            raise ui.Die(1)

        said = self.boot(fail, ["--tries", "1", "--wait", "0"])
        self.assertIn("keep retrying", said)
        tried = []
        said = self.boot(lambda _logins, name: tried.append(name), dns=False)
        self.assertEqual(tried, [], "no login attempt without a network")
        self.assertIn("no route to pool.example after 0s", said)
        self.assertIn("keep retrying", said)

    def test_a_refused_credential_at_boot_is_not_tried_again(self):
        tries = []

        def refused(logins, _name):
            tries.append(1)
            logins.last_failure = "Permission denied (keyboard-interactive)."
            raise ui.Die(1)

        said = self.boot(refused)
        self.assertEqual(len(tries), 1)
        self.assertIn("Permission denied", said)
        self.assertIn("once more at most", said)

        def sshproxy_refused(_logins, _name):
            tries.append(1)
            raise SystemExit("cluster: sshproxy rejected the credentials: "
                             "Authentication failed")

        self.boot(sshproxy_refused, backend="nersc")
        self.assertEqual(len(tries), 2)


def _any_socket_path(test):
    """Let *test* bind sockets under whatever temp dir it was given.

    A socket path's length depends on where the runner's temp directory is
    (under /var/folders on macOS), so tests about something else must not
    trip over the guard; TestSocketPathLength and the guard tests below
    check it on purpose.
    """
    stack = contextlib.ExitStack()
    test.addCleanup(stack.close)
    stack.enter_context(_patched(sshmux, "require_socket_path", lambda sock: None))


class TestTransientSetupRetry(unittest.TestCase):
    """A pool address that drops the connection before auth is a redraw.

    `login.rc.fas.harvard.edu` publishes two addresses; one of them completes the
    TCP handshake and then closes, measured on roughly a quarter of attempts. ssh
    does not fall through to the other address, because *connecting* is what
    succeeded — so the tool has to ask again. What it must not retry is a rejected
    credential: on FASRC each attempt spends a TOTP window, and repeated auth
    failures are what locks an account.
    """

    DROP = "Connection closed by 192.0.2.10 port 22"
    DENIED = "Permission denied (keyboard-interactive)."

    def setUp(self):
        from clustertool.auth import is_transient_setup

        temp_state(self)
        _any_socket_path(self)
        self.logins = Logins(load("fasrc"))
        self.is_transient = is_transient_setup
        self.paced = 0
        self.attempts = []

        def pace(**_kw):
            self.paced += 1
            return True

        self.logins.state.totp_pace = pace

    def _opens(self, *results):
        pending = list(results)

        def attempt(sock, node, log, forward_agent, env, quiet):
            self.attempts.append(node)
            opened, detail = pending.pop(0) if pending else (False, self.DROP)
            return opened, detail, not opened and self.is_transient(detail)

        self.logins._attempt_master = attempt

    def _retry(self, tries=None):
        if tries is None:
            tries = self.logins.settings.int("POOL_OPEN_TRIES")
        return self.logins.open_master(
            self.logins.state.socket("x"), None,
            self.logins.state.master_log_path("x"), tries=tries, quiet=True)

    def test_what_fails_before_authentication_is_transient(self):
        for detail in (self.DROP, "Connection reset by peer",
                       "kex_exchange_identification: read: Connection reset",
                       "Connection timed out"):
            self.assertTrue(self.is_transient(detail), detail)
        for detail in (self.DENIED, "Too many authentication failures", ""):
            self.assertFalse(self.is_transient(detail), detail)

    def test_a_dropped_connection_is_retried_each_time_in_a_fresh_totp_window(self):
        self._opens((False, self.DROP), (True, ""))
        result = self._retry()
        self.assertTrue(result.opened)
        self.assertFalse(result.another_node, "an opened master needs no other node")
        self.assertEqual(len(self.attempts), 2)
        self.assertEqual(self.paced, 2, "a reused code fails as a wrong password")
        self.assertEqual(self.logins.last_failure, "")

    def test_a_rejected_credential_is_not_retried_and_goes_on_the_record(self):
        self._opens((False, self.DENIED), (True, ""))
        result = self._retry()
        self.assertEqual(result, (False, self.DENIED))
        self.assertEqual(len(self.attempts), 1,
                         "a bad password must not spend a second TOTP window")
        self.assertEqual(self.logins.last_failure, self.DENIED)
        self.assertFalse(result.another_node, "every node refuses it the same way")
        self.assertEqual(self.logins.state.refusals.current()["detail"], self.DENIED)

    def test_retries_are_bounded_by_a_setting_and_the_node_is_worth_another(self):
        self._opens()          # every attempt drops
        result = self._retry()
        self.assertEqual(result, (False, self.DROP), "still the pair it always was")
        self.assertEqual(result.detail, self.DROP)
        self.assertTrue(result.another_node)
        self.assertEqual(len(self.attempts),
                         self.logins.settings.int("POOL_OPEN_TRIES"))
        self.attempts.clear()
        with mock.patch.dict(os.environ, {"CLUSTER_FASRC_POOL_OPEN_TRIES": "1"}):
            self.assertEqual(self._retry(), (False, self.DROP))
        self.assertEqual(len(self.attempts), 1, "1 means a single draw with no redraw")

    def test_an_unattended_open_waits_on_a_refusal_on_record(self):
        from clustertool.auth import is_rejection

        self.logins.state.refusals.refused(self.DENIED)
        self._opens((True, ""))
        said = _refusal(self._retry)
        self.assertEqual(self.attempts, [], "nothing authenticated")
        self.assertEqual(self.paced, 0, "no TOTP window spent")
        self.assertIn("not authenticating", said)
        self.assertIn("trying them once more at", said)
        self.assertTrue(is_rejection(self.logins.last_failure),
                        "a reconnect loop stops on it at once")

    def test_a_refusal_recorded_during_the_wait_for_a_window_stops_the_attempt(self):
        # Two reconnect at once: both find the record clear, the second waits
        # for the next TOTP window while the first is refused.
        refusals = self.logins.state.refusals

        def pace(**_kw):
            self.paced += 1
            refusals.refused(self.DENIED)
            return True

        self.logins.state.totp_pace = pace
        self._opens((False, self.DENIED))
        said = _refusal(self._retry)
        self.assertEqual(self.attempts, [], "authenticated past a refusal recorded "
                         "while it waited for its window")
        self.assertIn("not authenticating", said)

    def test_a_window_that_cannot_be_had_stops_it_and_gives_the_confirming_try_back(self):
        self._opens((True, ""))
        self.logins.state.totp_pace = lambda **_kw: False
        self.assertIn("could not reserve a TOTP window", _refusal(self._retry))
        self.assertEqual(self.attempts, [])
        refusals = self.logins.state.refusals
        refusals.refused(self.DENIED)
        record = refusals.current()
        record["first"] -= 90                      # the confirming try is due
        refusals._write(record)
        self._opens((False, self.DENIED))
        _refusal(self._retry)
        self.assertEqual(self.attempts, [])
        self.assertIsNone(refusals.current().get("claim"),
                          "held by a process that never made its try")

    def test_the_confirming_try_is_made_once_it_is_due_and_a_refusal_confirms(self):
        refusals = self.logins.state.refusals
        refusals.refused(self.DENIED)
        record = refusals.current()
        record["first"] -= 90
        refusals._write(record)
        self._opens((False, self.DENIED))
        self._retry()
        self.assertEqual(len(self.attempts), 1)
        self.assertTrue(refusals.current()["confirmed"])
        self._opens((True, ""))
        _refusal(self._retry)
        self.assertEqual(len(self.attempts), 1, "no more unattended tries")

    def test_by_hand_the_refused_credential_is_tried_and_that_is_said(self):
        self.logins.state.refusals.refused(self.DENIED)
        self._opens((True, ""))
        err = io.StringIO()
        with _patched(self.logins.backend, "by_hand", True), \
                contextlib.redirect_stderr(err):
            opened, _ = self._retry()
        self.assertTrue(opened)
        self.assertIn("trying them again, as you asked", err.getvalue())
        self.assertIsNone(self.logins.state.refusals.current(),
                          "a success clears the record")

    def test_a_reconnect_after_an_attach_is_not_by_hand(self):
        backend = self.logins.backend
        backend.by_hand = True
        self.addCleanup(setattr, backend, "by_hand", False)
        self.logins.ensure = lambda name: None
        seen = []
        with _patched(self.logins.state, "socket", lambda name: "/nonexistent"), \
                _patched(plat, "restore_tty", lambda *_a: None), \
                _patched(sshmux, "_resync_size", lambda: None), \
                _patched(subprocess, "run", lambda argv: seen.append(
                    backend.by_hand) or SimpleNamespace(returncode=0)):
            self.logins.interactive("x", tty=False)
        self.assertEqual(seen, [False])

    def _new_login_on(self, *nodes):
        self.logins.refresh_meta = lambda name: nodes[-1]
        self.logins._protect = lambda name: None
        return self.logins._create("x", preferred=list(nodes))

    def test_a_new_login_moves_past_a_node_that_dropped_it_but_not_a_refusal(self):
        self._opens((False, self.DROP), (False, self.DROP), (False, self.DROP),
                    (True, ""))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(self._new_login_on("n1", "n2"))
        self.assertEqual(self.attempts, ["n1", "n1", "n1", "n2"])
        self.attempts.clear()
        self._opens((False, self.DENIED), (True, ""))
        said = _refusal(lambda: self._new_login_on("n1", "n2", "n3"))
        self.assertEqual(self.attempts, ["n1"])
        self.assertIn(self.DENIED, said)
        self.assertIn("the other nodes were not tried", said)
        self.assertIn("config credentials", said)


class TestInteractiveReconnect(unittest.TestCase):
    """An attach rides out drops scattered over days, and stops on a burst.

    Each drop adds to a count that the time the session then stays up fades
    (clustertool.backoff), so no fixed number of drops over the whole run ends
    it. A reconnect that fails for a reason that can pass is one more drop; a
    refused credential ends the attach at once.
    """

    def setUp(self):
        temp_state(self)
        _any_socket_path(self)
        self.sshmux = sshmux
        self.logins = sshmux.Logins(load("fasrc"))
        self.logins.ensure = lambda name, **_kw: True
        self.recovered = []
        self.logins.restore = lambda name: self.recovered.append(name)
        self.clock = FakeClock()
        self.sessions = []   # (seconds it lasts, exit status), in order
        self.argvs = []
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(_patched(sshmux, "time", self.clock))
        stack.enter_context(_patched(sshmux.plat, "save_tty", lambda: None))
        stack.enter_context(_patched(sshmux.plat, "restore_tty", lambda _saved=None: None))
        stack.enter_context(_patched(sshmux, "_resync_size", lambda: None))
        stack.enter_context(_patched(sshmux.subprocess, "run", self._session))
        self.err = io.StringIO()
        stack.enter_context(contextlib.redirect_stderr(self.err))

    def _session(self, _argv):
        lasts, rc = self.sessions.pop(0)
        self.clock.now += lasts
        self.argvs.append(_argv)
        return subprocess.CompletedProcess(_argv, rc)

    def test_drops_scattered_over_days_never_end_it(self):
        self.sessions = [(86400, 255)] * 40 + [(60, 0)]
        self.assertEqual(self.logins.interactive("work"), 0)
        self.assertEqual(len(self.recovered), 40)
        self.assertEqual(set(self.clock.slept), {2})

    def test_a_burst_of_drops_backs_off_and_then_gives_up(self):
        limit = Settings("fasrc").int("INTERACTIVE_RETRIES")
        self.sessions = [(0, 255)] * (limit + 1)
        with self.assertRaises(SystemExit):
            self.logins.interactive("work")
        self.assertEqual(len(self.recovered), limit)
        self.assertEqual(self.clock.slept[:6], [2, 4, 8, 16, 32, 60])
        self.assertEqual(max(self.clock.slept), 60)
        self.assertIn("gave up reconnecting", self.err.getvalue())

    def test_a_reconnect_is_made_only_over_a_connection_that_does_not_answer(self):
        cases = [
            # The remote command's own status is the answer.
            ([(5, 3)], None, {}, 3, []),
            # `ssh node` failed inside the shell, then Ctrl-D: bash exits 255,
            # and the connection it rode still carries a round trip.
            ([(600, 255), (5, 0)], True, {}, 255, []),
            ([(600, 255), (5, 0)], False, {}, 0, ["work"]),
            ([(3600, 255)], None, {"INTERACTIVE_RETRIES": 0}, SystemExit, []),
        ]
        for sessions, answers, overrides, want, recovered in cases:
            with self.subTest(sessions=sessions, answers=answers, **overrides):
                self.sessions, self.recovered = list(sessions), []
                if answers is not None:
                    self.logins.answers = lambda name, answers=answers: answers
                with _patched(self.logins, "settings", _Overrides(**overrides)):
                    if want is SystemExit:
                        self.assertRaises(SystemExit, self.logins.interactive, "work")
                    else:
                        self.assertEqual(self.logins.interactive("work"), want)
                self.assertEqual(self.recovered, recovered)

    def test_a_reconnect_that_cannot_open_the_login_is_tried_again_unless_refused(self):
        attempts = []

        def recover(name):
            attempts.append(name)
            if len(attempts) < 3:
                self.logins.last_failure = ("ssh: connect to host login01 port 22: "
                                            "Network is unreachable")
                raise ui.Die(1)

        self.logins.restore = recover
        self.sessions = [(3600, 255), (60, 0)]
        self.assertEqual(self.logins.interactive("work"), 0)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(self.clock.slept, [2, 4, 8])
        self.assertIn("Network is unreachable", self.err.getvalue())

        def refused(name):
            attempts.append(name)
            self.logins.last_failure = "Permission denied (keyboard-interactive)."
            raise ui.Die(1)

        attempts.clear()
        self.logins.restore = refused
        self.sessions = [(3600, 255)]
        with self.assertRaises(SystemExit):
            self.logins.interactive("work")
        self.assertEqual(len(attempts), 1, "a refused credential ends it at once")

    def test_an_attach_goes_back_to_its_session_and_never_makes_a_new_one(self):
        # Its tmux client ended with the connection up (a signal on the
        # node), or with it: either way the session outlived the channel.
        for up in (True, False):
            with self.subTest(connection_up=up):
                self.logins.answers = lambda name, up=up: up
                self.argvs, self.recovered = [], []
                self.sessions = [(600, 255), (600, 255), (5, 0)]
                rc = self.logins.interactive("work", "CREATE-IT", again="GO-BACK")
                self.assertEqual(rc, 0)
                self.assertEqual([argv[-1] for argv in self.argvs],
                                 ["CREATE-IT", "GO-BACK", "GO-BACK"])

    def test_a_connection_answers_only_over_a_new_channel(self):
        ran = []

        def run_remote(name, command, timeout=None, capture=True):
            ran.append((command, timeout))
            return subprocess.CompletedProcess([], 0, "ok", "")

        self.logins.run_remote = run_remote
        with _patched(self.logins, "is_active", lambda name: False):
            self.assertFalse(self.logins.answers("work"))
        self.assertEqual(ran, [], "no master, nothing to ask")
        with _patched(self.logins, "is_active", lambda name: True):
            self.assertTrue(self.logins.answers("work"))
        self.assertEqual(ran, [("printf ok",
                                Settings("fasrc").int("REMOTE_CHECK_TIMEOUT"))])


class _Overrides:
    """Settings("fasrc"), with some values replaced."""

    def __init__(self, **values):
        self.values = values
        self.base = Settings("fasrc")

    def int(self, key):
        return int(self.values.get(key, self.base.int(key)))

    def __getattr__(self, name):
        return getattr(self.base, name)


class TestLoginLockIsWaitedFor(unittest.TestCase):
    """Another process repairing a login is waited for, never cut down.

    Its holder is authenticating or rebuilding, which on a slow node takes
    minutes, and the flock is gone the moment that process is.
    """

    def setUp(self):
        temp_state(self)
        _any_socket_path(self)
        self.logins = Logins(load("fasrc"))
        self.torn_down = []
        self.logins.close = lambda name, **_kw: self.torn_down.append(("close", name))
        self.logins._create = lambda name, **_kw: self.torn_down.append(("create", name))

    def _answers_after(self, calls):
        """is_active that says no *calls* times, then yes; channels answer."""
        seen = {"n": 0}

        def is_active(_name):
            seen["n"] += 1
            return seen["n"] > calls

        self.logins.is_active = is_active
        self.logins.run_remote = lambda *_a, **_kw: subprocess.CompletedProcess(
            ["ssh"], 0, "ok", "")

    def test_a_foreground_connect_waits_for_the_holder_and_names_it(self):
        child = hold_flock(self, self.logins.state.login_lock_path("api"), 0.4)
        self._answers_after(1)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertTrue(self.logins.ensure("api"))
        self.assertIn(f"waiting for pid {child.pid}", err.getvalue())
        self.assertEqual(self.torn_down, [], "the holder's fresh master is kept")

    def test_recovery_leaves_a_login_the_holder_just_rebuilt(self):
        hold_flock(self, self.logins.state.login_lock_path("api"), 0.4)
        self._answers_after(1)
        with contextlib.redirect_stderr(io.StringIO()):
            self.logins.restore("api")
        self.assertEqual(self.torn_down, [])
        # One that is still down is rebuilt.
        self._answers_after(10 ** 6)
        self.logins.restore("api")
        self.assertEqual(self.torn_down, [("close", "api"), ("create", "api")])

    def test_a_bounded_wait_still_gives_up(self):
        hold_flock(self, self.logins.state.login_lock_path("api"), 30)
        self.assertIsNone(self.logins.state.login_lock("api", wait=0.2))


class TestOneWayToOpenAMaster(unittest.TestCase):
    """Every master, whatever it is for, is opened by Logins.open_master."""

    def setUp(self):
        temp_state(self)
        _any_socket_path(self)
        self.logins = Logins(load("nersc"))
        self.sock = self.logins.state.socket("work")
        self.log = self.logins.state.master_log_path("work")
        self.ran = []
        self.live = False
        self.logins.wait_socket_live = lambda sock: self.live

    def _run_ssh(self, rc, stderr="", log_line=""):
        def run_ssh(argv, timeout=None, capture=True, quiet=True, env=None):
            self.ran.append((argv, env))
            if log_line:
                with open(self.log, "a", encoding="utf-8") as handle:
                    handle.write(log_line + "\n")
            return subprocess.CompletedProcess(argv, rc, "", stderr)
        self.logins.backend.run_ssh = run_ssh

    def test_a_master_logs_to_its_rotated_log_and_carries_the_agent_it_is_given(self):
        # What sshproxy refused is the password; a connection presents the
        # certificate, which risks nothing on it and says nothing about it.
        refusals = self.logins.state.refusals
        refusals.refused("sshproxy rejected the credentials")
        refusals._write(dict(refusals._read(), confirmed=True))
        self.log.write_text("x" * 64)
        self._run_ssh(0)
        self.live = True
        env = {"SSH_AUTH_SOCK": "/nonexistent/agent"}
        with _patched(plat, "LOG_LIMIT", 16):
            self.assertEqual(self.logins.open_master(
                self.sock, None, self.log, forward_agent=True, env=env), (True, ""))
        self.assertEqual(Path(str(self.log) + ".1").read_text(), "x" * 64)
        (argv, given), = self.ran
        self.assertEqual(argv[argv.index("-E") + 1], str(self.log))
        for option in ("ControlMaster=yes", f"ControlPath={self.sock}",
                       "ForwardAgent=yes"):
            self.assertIn(option, argv)
        self.assertIs(given, env)
        self.assertIsNotNone(refusals.current(), "neither held nor cleared")

    def test_a_failure_is_explained_from_the_log_and_leaves_no_socket_file(self):
        self.sock.write_text("")
        self._run_ssh(255, log_line="Permission denied (publickey).")
        self.logins.backend.credential_refused = lambda detail, quiet=False: False
        opened, detail = self.logins.open_master(self.sock, None, self.log, tries=3)
        self.assertFalse(opened)
        self.assertEqual(detail, "Permission denied (publickey).")
        self.assertEqual(len(self.ran), 1)
        self.assertFalse(self.sock.exists())

    def _certificates(self, answers):
        """A certificate ssh answers *answers* to in turn, and a fetch that
        installs a new one: the fetches made."""
        keys = tempfile.TemporaryDirectory()
        self.addCleanup(keys.cleanup)
        backend = self.logins.backend
        backend.key_path = Path(keys.name) / "nersc"
        backend.cert_path.write_text("refused\n")
        fetched = []

        def fetch_certificate(quiet=False, seen=None, min_left=None, by_hand=None):
            fetched.append(seen)
            backend.cert_path.write_text(f"fresh, number {len(fetched)}\n")

        def run_ssh(argv, timeout=None, capture=True, quiet=True, env=None):
            self.ran.append((argv, env))
            self.live = next(answers) == 0
            if not self.live:
                self.log.write_text("Permission denied (publickey).\n")
            return subprocess.CompletedProcess(argv, 0 if self.live else 255, "", "")

        backend.fetch_certificate, backend.run_ssh = fetch_certificate, run_ssh
        return fetched

    def test_a_refused_certificate_is_replaced_once_and_the_new_one_tried(self):
        fetched = self._certificates(iter([255, 0]))
        opened, _detail = self.logins.open_master(self.sock, None, self.log, quiet=True)
        self.assertTrue(opened)
        self.assertEqual((len(fetched), len(self.ran)), (1, 2))
        self.assertFalse((self.logins.state.dir / "certificate.refused").exists(),
                         "an accepted certificate ends the replacement's record")
        # A replacement refused in turn is not replaced.
        self.ran.clear()
        fetched = self._certificates(iter([255] * 3))
        for _ in range(2):
            opened, detail = self.logins.open_master(self.sock, None, self.log,
                                                     quiet=True)
            self.assertFalse(opened)
            self.assertIn("Permission denied", detail)
        self.assertEqual((len(fetched), len(self.ran)), (1, 3))

    def test_a_master_gone_before_it_answered_is_tried_again(self):
        self._run_ssh(0)
        opened, detail = self.logins.open_master(self.sock, None, self.log, tries=2,
                                                  quiet=True)
        self.assertFalse(opened)
        self.assertEqual(len(self.ran), 2)
        self.assertIn("gone before it answered", detail)

    def test_a_master_that_came_up_late_is_told_to_exit(self):
        # Unlinking its socket alone would leave it running, a connection
        # nothing here can see or close.
        told = []

        def run_ssh(argv, timeout=None, capture=True, quiet=True, env=None):
            self.ran.append((argv, env))
            self.sock.write_text("")        # it binds after the wait gave up
            return subprocess.CompletedProcess(argv, 0, "", "")

        self.logins.backend.run_ssh = run_ssh
        with _patched(sshmux, "control",
                      lambda sock, operation, timeout=10: told.append(
                          (str(sock), operation)) or True):
            self.logins.open_master(self.sock, None, self.log, tries=2, quiet=True)
        self.assertEqual(len(self.ran), 2)
        self.assertEqual(told, [(str(self.sock), "exit")] * 2)

    def test_a_socket_path_too_long_to_bind_stops_before_ssh(self):
        self._run_ssh(0)
        deep = config.CTL_DIR / ("d" * 120) / "cl-nersc-work.sock"
        with _patched(sshmux, "require_socket_path", state_module.require_socket_path):
            said = _refusal(lambda: self.logins.open_master(deep, None, self.log))
        self.assertIn("too long for this system", said)
        self.assertIn("CTL_DIR", said)
        self.assertEqual(self.ran, [])


class TestControlCommands(unittest.TestCase):
    """`-O` commands and riders are built in one place each."""

    def test_a_control_command_reads_no_config_file_and_a_rider_the_masters(self):
        ran = []

        def run(argv, timeout=None, **_kw):
            ran.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        with _patched(sshmux.plat, "run", run):
            self.assertTrue(sshmux.control("/nonexistent/s.sock", "exit"))
        argv, = ran
        self.assertEqual(argv[:5], ["ssh", "-F", os.devnull, "-O", "exit"])
        self.assertIn("ControlPath=/nonexistent/s.sock", argv)
        argv = sshmux.rider_argv("/nonexistent/s.sock", "user@login01",
                                 ["-t"], remote="tmux ls")
        self.assertEqual(argv[1:3], ["-F", "/dev/null"])
        self.assertLess(argv.index("ControlMaster=no"), argv.index("user@login01"))
        self.assertEqual(argv[-2:], ["user@login01", "tmux ls"])
        with _patched(sshmux.config, "global_value",
                      lambda key, default=None: "/nonexistent/ssh_config"
                      if key == "SSH_CONFIG" else default):
            argv = sshmux.rider_argv("/nonexistent/s.sock", "user@login01")
        self.assertEqual(argv[1:3], ["-F", "/nonexistent/ssh_config"])
        # A rider for another program leaves the host to it.
        argv = sshmux.rider_argv("/nonexistent/s.sock", options=["-o", "BatchMode=yes"])
        self.assertEqual(argv[-2:], ["-o", "BatchMode=yes"])
        self.assertIn("ControlMaster=no", argv)

    def test_a_backend_builds_masters_and_leaves_riders_to_rider_argv(self):
        temp_state(self)
        backend = load("fasrc")
        opts = backend.ssh_opts(sock="/nonexistent/s.sock", master=True)
        self.assertIn("ControlMaster=yes", opts)
        self.assertIn("ControlPersist=yes", opts)
        # One without the rider's guard would authenticate by itself once
        # its master is gone.
        with self.assertRaises(ValueError):
            backend.ssh_opts(sock="/nonexistent/s.sock")

    def test_a_rider_with_no_master_fails_instead_of_connecting(self):
        # ssh with ControlMaster=no and no master connects by itself, through
        # the ProxyCommand; the rider's own one only says why and ends.
        argv = sshmux.rider_argv("/nonexistent/s.sock", "user@login01",
                                 ["-o", "ProxyCommand=ssh -W %h:%p jump01"])
        given = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-o"]
        proxies = [one for one in given if one.startswith("ProxyCommand=")]
        self.assertEqual(proxies[0], sshmux.NO_CONNECTION_OF_ITS_OWN,
                         "ssh takes the first ProxyCommand it is given")
        command = proxies[0].split("=", 1)[1].replace("%h", "login01")
        self.assertNotIn("%", command, "ssh expands only the tokens it knows")
        ran = subprocess.run(["/bin/sh", "-c", "exec " + command],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             universal_newlines=True, timeout=10)
        self.assertEqual(ran.stdout, "", "a proxy's stdout would be the connection")
        self.assertIn("the connection to login01 is gone", ran.stderr)

    def test_a_rider_is_written_for_rclone_the_way_rclone_reads_it(self):
        import csv

        argv = sshmux.rider_argv("/home/user/state dir/s.sock", "user@login01",
                                 ["-o", 'Tag="quoted"'], remote="")
        value = sshmux.rclone_ssh_value(argv)
        # rclone splits it as a CSV record with a space for the comma.
        words, = csv.reader([value], delimiter=" ", strict=True)
        self.assertEqual(words, argv)
        # Go's reader refuses a quote inside an unquoted field, which
        # Python's lets through, so check the quoted forms themselves.
        self.assertIn(' "Tag=""quoted""" ', value)
        self.assertIn(' "ControlPath=/home/user/state dir/s.sock" ', value)
        self.assertTrue(value.endswith(' ""'), "an empty word is kept, quoted")

    def test_is_active_is_the_socket_check_and_no_socket_file_asks_no_ssh(self):
        temp_state(self)
        logins = Logins(load("fasrc"))
        with _patched(sshmux, "control", lambda *a, **kw: self.fail("ssh ran")):
            self.assertFalse(logins.is_active("main"))
        asked = []
        logins._socket_live = lambda sock: asked.append(sock) or True
        self.assertTrue(logins.is_active("main"))
        self.assertEqual(asked, [logins.state.socket("main")])

    def _close(self, live, socket_file=True):
        """Close login 'main' over a master that is *live*: the `-O` sent."""
        temp_state(self)
        logins = Logins(load("fasrc"))
        sock = logins.state.socket("main")
        if socket_file:
            sock.write_text("")
        told, rode = [], []
        logins.run_remote = lambda name, command, **_kw: rode.append(command) or (
            subprocess.CompletedProcess([], 0 if live else 255, "", ""))
        logins.master_pids = lambda _name: []
        with _patched(sshmux, "control", lambda _sock, operation, timeout=10:
                      told.append(operation) or (live or operation != "check")):
            logins.close("main", quiet=True)
        self.assertFalse(sock.exists())
        return told, rode

    def test_closing_a_login_checks_it_once_and_tells_it_to_exit(self):
        told, rode = self._close(live=True)
        self.assertEqual(told, ["check", "exit"])
        self.assertEqual(len(rode), 1, "the node is settled on the way out")
        # A master already gone costs the same and settles nothing.
        told, rode = self._close(live=False)
        self.assertEqual(told, ["check", "exit"])
        self.assertEqual(rode, [])
        told, _rode = self._close(live=False, socket_file=False)
        self.assertEqual(told, [], "no socket file, nothing to ask")

    def test_a_report_counts_the_masters_it_just_found_without_asking_again(self):
        temp_state(self)
        logins = Logins(load("fasrc"))
        for name in ("cl-fasrc-xfer-a.sock", "cl-fasrc-mnt-b.sock"):
            (logins.state.ctl_dir / name).write_text("")
        asked = []
        logins._socket_live = lambda sock: asked.append(Path(sock).name) or True
        self.assertEqual(logins.connection_count(active=["main"], transfers=["a"]), 3)
        self.assertEqual(asked, ["cl-fasrc-mnt-b.sock"])
        asked.clear()
        logins.state.known_logins = lambda: ["main"]
        self.assertEqual(logins.connection_count(), 3, "the gate asks every one")
        self.assertEqual(sorted(asked), ["cl-fasrc-main.sock", "cl-fasrc-mnt-b.sock",
                                         "cl-fasrc-xfer-a.sock"])


class TestNewLoginNames(unittest.TestCase):
    """What a new login may be called on this machine."""

    def setUp(self):
        temp_state(self)
        _any_socket_path(self)
        self.config = config
        self.logins = Logins(load("fasrc"))

    def _existing(self, backend, name):
        directory = self.config.STATE_ROOT / backend
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name}.node").write_text("login01\n")

    def test_a_name_over_the_cap_or_differing_only_in_case_is_refused(self):
        from clustertool.sshmux import LOGIN_NAME_MAX

        self._existing("fasrc", "work")
        self.logins.check_new_login("api")
        self.logins.check_new_login("a" * LOGIN_NAME_MAX)
        said = _refusal(lambda: self.logins.check_new_login("a" * (LOGIN_NAME_MAX + 1)))
        self.assertIn(f"the limit is {LOGIN_NAME_MAX}", said)
        # On any backend.
        self._existing("nersc", "Api")
        said = _refusal(lambda: self.logins.check_new_login("api"))
        self.assertIn("login 'Api' already exists on nersc", said)
        self.assertIn("only in case", said)

    def test_a_name_whose_sockets_cannot_be_bound_is_refused(self):
        with _patched(sshmux, "require_socket_path", state_module.require_socket_path), \
                _patched(state_module.plat, "IS_MAC", True), \
                _patched(self.logins.state, "ctl_dir",
                         self.config.CTL_DIR / ("d" * 70)):
            said = _refusal(lambda: self.logins.check_new_login("work"))
        self.assertIn("too long for this system", said)

    def test_only_a_new_login_is_checked(self):
        checked = []
        self.logins.check_new_login = checked.append
        self.logins.backend.ensure_credential = lambda **_kw: (_ for _ in ()).throw(
            ui.Die(1))
        self._existing("fasrc", "work")
        for name in ("work", "api"):
            with self.assertRaises(ui.Die):
                self.logins._create(name)
        self.assertEqual(checked, ["api"])


class TestScopedAgent(unittest.TestCase):
    """The transfer's private agent: bounded while it lives, tidy when it goes."""

    def setUp(self):
        self.mod = agentscope
        self.ran = []
        self.tmp = tempfile.TemporaryDirectory(prefix="agentscope-")
        self.addCleanup(self.tmp.cleanup)

    def _fake_run(self, sock):
        def run(argv, timeout=None, env=None, **_kw):
            self.ran.append(argv)
            out = (f"SSH_AUTH_SOCK={sock}; export SSH_AUTH_SOCK;\n"
                   "SSH_AGENT_PID=4242; export SSH_AGENT_PID;\n")
            return subprocess.CompletedProcess(argv, 0, out if argv[0] == "ssh-agent"
                                               else "", "")
        return run

    def test_an_identity_is_bounded_only_by_a_known_lifetime(self):
        for lifetime, add in ((None, ["ssh-add", "/nonexistent/key"]),
                              (600, ["ssh-add", "-t", "600", "/nonexistent/key"])):
            self.ran.clear()
            with _patched(self.mod.plat, "run",
                          self._fake_run("/nonexistent/ssh-a/agent.1")):
                agent = self.mod.ScopedAgent(["/nonexistent/key"],
                                             lifetime=lifetime).start()
                agent.close()
            self.assertEqual(self.ran[:2], [["ssh-agent", "-s"], add])
            self.assertEqual(agent.sock, None)

    def test_a_terminated_transfer_still_kills_its_agent(self):
        import signal

        before = signal.getsignal(signal.SIGTERM)
        with _patched(self.mod.plat, "run", self._fake_run("/nonexistent/ssh-a/agent.1")):
            agent = self.mod.ScopedAgent(["/nonexistent/key"])
            with self.assertRaises(self.mod.plat.Terminated) as ended:
                with contextlib.closing(agent.start()):
                    os.kill(os.getpid(), signal.SIGTERM)
                    signal.pause()
        self.assertEqual(ended.exception.code, 128 + signal.SIGTERM)
        self.assertIn(["ssh-agent", "-k"], self.ran)
        self.assertIs(signal.getsignal(signal.SIGTERM), before)

    def _close_with_dir(self, root, name):
        directory = Path(root) / name
        directory.mkdir()
        agent = self.mod.ScopedAgent(["/nonexistent/key"])
        agent.sock, agent.pid = str(directory / "agent.1"), "4242"
        with _patched(self.mod.plat, "run", self._fake_run(agent.sock)):
            agent.close()
        return directory

    def test_only_the_agents_own_directory_in_the_temp_dirs_is_removed(self):
        # macOS puts it under a per-user $TMPDIR, not /tmp.
        nested = Path(self.tmp.name) / "deeper"
        nested.mkdir()
        for root, name, removed in ((self.tmp.name, "ssh-XXXXabcd", True),
                                    (self.tmp.name, "mine", False),
                                    (nested, "ssh-XXXXabcd", False)):
            self.ran.clear()
            with _patched(os, "environ", dict(os.environ, TMPDIR=self.tmp.name)):
                left = self._close_with_dir(root, name)
            self.assertEqual(left.exists(), not removed, (root, name))
            self.assertEqual(self.ran, [["ssh-agent", "-k"]])


class TestRemoteCommandTimeouts(unittest.TestCase):
    """A small command on a cluster is timed by a setting, never by a number
    written at the call."""

    def setUp(self):
        temp_state(self)
        self.sshmux = sshmux
        self.logins = sshmux.Logins(load("fasrc"))
        self.logins.settings = _Overrides(REMOTE_COMMAND_TIMEOUT=77,
                                          REMOTE_CHECK_TIMEOUT=7)
        self.timeouts = []

        def run(argv, timeout=None, **_kw):
            self.timeouts.append(timeout)
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(_patched(sshmux.plat, "run", run))

    def test_a_command_gets_the_setting_unless_told_otherwise(self):
        self.logins.run_remote("work", "tmux ls")
        self.logins.remote_value("work", "hostname -f")
        self.logins.live_node("work")
        self.logins.run_remote("work", "find . -print0", timeout=None)
        ctx = SimpleNamespace(logins=self.logins)
        bridge._remote(ctx, "work", "true")
        bridge._remote(ctx, "work", "true", timeout=None)
        bridge._remote(ctx, "work", "true", timeout=5)
        self.assertEqual(self.timeouts, [77, 77, 77, None, 77, None, 5])
        # Whether a connection answers is its own setting.
        self.timeouts.clear()
        self.logins.is_active = lambda _name: True
        self.assertTrue(self.logins._needs_no_rebuild("work"))
        self.assertEqual(self.timeouts, [7])

    def test_a_command_on_a_connection_of_its_own_is_let_in_first(self):
        tmux = Tmux(self.logins)
        seen = []
        self.logins.backend.run_ssh = (
            lambda argv, timeout=None, **_kw: seen.append(timeout)
            or subprocess.CompletedProcess(argv, 0, "", ""))
        self.logins.backend.ensure_credential = lambda quiet=False: None
        self.logins.backend.paces_totp = False
        tmux.list_sessions("work")
        tmux.list_sessions_direct_checked("login01.example")
        self.assertEqual(self.timeouts, [77])
        self.assertEqual(seen, [77 + Settings("fasrc").int("CONNECT_TIMEOUT")],
                         "the command's own time, and the connect's")

    def test_work_that_grows_with_what_it_finds_is_stopped_only_for_silence(self):
        watched = []

        def run(argv, timeout=None, idle=None, **_kw):
            watched.append((timeout, idle))
            return subprocess.CompletedProcess(argv, 0, ". . done", "")

        tmux = Tmux(self.logins)
        with _patched(self.sshmux.plat, "run", run):
            self.assertTrue(tmux.retag_owner("work", "work", "main"))
            tmux.kill_sessions("work", ["api"])
        self.assertEqual(watched[:2], [(None, 77), (None, 77)],
                         "no clock, and REMOTE_COMMAND_TIMEOUT of silence")
        # A retag that did not finish is not done.
        for rc, out in ((124, ". ."), (0, ". ."), (255, "done")):
            with self.subTest(rc=rc, out=out), _patched(
                    self.sshmux.plat, "run",
                    lambda argv, rc=rc, out=out, **_kw: subprocess.CompletedProcess(
                        argv, rc, out, "")):
                self.assertFalse(tmux.retag_owner("work", "work", "main"))

    def test_a_create_is_given_the_node_side_bounds_it_carries(self):
        tmux = Tmux(self.logins)
        self.logins.state.note_sessions = lambda *_a, **_k: None
        with contextlib.redirect_stderr(io.StringIO()):
            tmux.create("work", "api")
            tmux.register_session("work", "api")
        extra = linger.bound(self.logins)
        self.assertEqual(extra, 2 * linger.TIMEOUT)
        self.assertEqual(self.timeouts, [77 + extra, 77 + extra])


class TestASlowMasterIsNotRebuilt(unittest.TestCase):
    """A live master that is slow to start a session keeps its connection.

    Rebuilding it would drop everything else riding it and spend a TOTP
    window; trying the channel again costs nothing. Only a master that is
    gone, or a second no-answer in a row, is rebuilt.
    """

    def setUp(self):
        temp_state(self)
        self.sshmux = sshmux
        self.fresh()
        self.answers = []

        def run(argv, timeout=None, **_kw):
            rc = self.answers.pop(0) if self.answers else 124
            return subprocess.CompletedProcess(argv, rc, "ok" if rc == 0 else "", "")

        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(_patched(sshmux.plat, "run", run))
        self.err = io.StringIO()
        stack.enter_context(contextlib.redirect_stderr(self.err))

    def fresh(self):
        """Logins for a live master, which count no no-answers yet."""
        self.logins = self.sshmux.Logins(load("fasrc"))
        self.alive = [True]
        self.logins.is_active = lambda _n: self.alive[0]
        self.calls = []
        self.logins.close = lambda name, **kw: self.calls.append("close")
        self.logins._create = lambda name, **kw: self.calls.append("create")
        self.logins.state.login_lock = lambda *a, **k: SimpleNamespace(
            release=lambda: None)

    def test_a_live_master_is_rebuilt_only_on_a_second_no_answer_in_a_row(self):
        cases = [([[124]], []), ([[124], [124, 124]], ["close", "create"]),
                 # An answer in between starts the count again.
                 ([[124], [0], [124]], [])]
        for restores, rebuilt in cases:
            with self.subTest(restores=restores):
                self.fresh()
                for answers in restores:
                    self.answers = list(answers)
                    self.logins.restore("work")
                self.assertEqual(self.calls, rebuilt)
        said = self.err.getvalue()
        self.assertIn("did not answer within 10s", said)
        self.assertIn("its connection is up", said)
        self.assertIn("did not answer twice in a row", said)

    def test_a_master_that_is_gone_is_rebuilt_at_once(self):
        self.alive = [False]
        self.logins.restore("work")
        self.assertEqual(self.calls, ["close", "create"])
        # One that went while it was asked, too.
        self.calls.clear()
        answers = iter([True, False, False])
        self.logins.is_active = lambda _n: next(answers)
        self.answers = [124]
        self.logins.restore("work")
        self.assertEqual(self.calls, ["close", "create"])


class TestDiscardingAMaster(unittest.TestCase):
    """A master told to exit that does not answer is stopped, never orphaned
    behind a socket path unlinked from it."""

    def setUp(self):
        self.sshmux = sshmux
        self.sock = Path(temp_state(self)) / "ctl" / "cl-fasrc-xfer-pool.sock"
        self.sock.write_text("")
        self.stopped = []
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(_patched(sshmux.plat, "stop",
                                     lambda pid, grace: self.stopped.append((pid, grace))))
        path, mentions = f"ControlPath={self.sock}", f" ControlPath={self.sock} "
        stack.enter_context(_patched(sshmux.plat, "own_process_argv", lambda: [
            (11, ["ssh", "-F", "/dev/null", "-o", path, "-oControlMaster=yes",
                  "-o", "ControlPersist=yes", "-Nf", "user@host"]),
            (12, ["ssh", "-F", "/dev/null", "-o", path, "-o", "ControlMaster=no",
                  "user@host", "sftp"]),
            (13, ["ssh", "-F", "/dev/null", "-o", f"{path}-fwd.sock",
                  "-o", "ControlMaster=yes", "-N", "-f", "user@host"]),
            (14, ["ssh", "-F", "/dev/null", "-O", "check", "-o", path, "dummy"]),
            # A rider on another master, whose remote command only mentions
            # this one's options, as one word and as several.
            (15, ["ssh", "-o", "ControlPath=/other.sock", "user@host",
                  f"echo '{mentions} ControlMaster=yes '"]),
            (16, ["ssh", "-o", "ControlPath=/other.sock", "user@host", "echo",
                  "-o", path, "-o", "ControlMaster=yes"]),
        ]))

    def test_a_master_that_does_not_answer_the_exit_is_stopped(self):
        for answers, stopped in ((False, [(11, 3)]), (True, [])):
            self.stopped.clear()
            self.sock.write_text("")
            with _patched(self.sshmux, "control", lambda *_a, **_kw: answers):
                self.sshmux.discard(self.sock, grace=3)
            self.assertEqual(self.stopped, stopped, "the master alone")
            self.assertFalse(self.sock.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
