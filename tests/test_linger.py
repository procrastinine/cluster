#!/usr/bin/env python3
"""Linger: keeping a login node's tmux alive once the connections end.

Also the shutdown hook that asserts it on the way down, and releasing
only the linger this tool itself enabled.

Run: python3 -m unittest tests.test_linger
(or the whole suite: python3 -m unittest discover -s tests -p 'test_*.py')
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

# For `support`, which puts this checkout on sys.path and sets up the sandbox
# every test runs in, so it is imported before anything from clustertool.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import _patched  # noqa: E402
from clustertool import linger  # noqa: E402
from clustertool.backends import load  # noqa: E402
from clustertool.backends.base import Backend  # noqa: E402
from clustertool.config import Settings  # noqa: E402


class TestLinger(unittest.TestCase):
    """Why a login node has to be told, over and over, to keep your work.

    See clustertool.linger: the node clears linger from under us, and the only
    instant that decides whether a tmux server lives is the one when the last
    connection to that node ends — which a client reboot reaches with no
    warning and no chance to act.
    """

    class FakeLogins:
        """Just enough of Logins for the linger module and for Tmux."""

        def __init__(self, reaps=True, accepts=True, flag=True, reply=""):
            self.backend = types.SimpleNamespace(reaps_on_logout=reaps,
                                                 name="fasrc")
            self.settings = types.SimpleNamespace(flag=lambda key: flag,
                                                  int=lambda key: 30)
            self.state = types.SimpleNamespace(
                note_sessions=lambda *a, **kw: None)
            self.commands = []
            self.accepts = accepts
            self.reply = reply
            self.active = True

        def is_active(self, _name):
            return self.active

        def run_remote(self, name, command, timeout=60, capture=True, idle=None):
            self.commands.append(command)
            return types.SimpleNamespace(
                returncode=0 if self.accepts else 1, stdout=self.reply,
                stderr="")

        @staticmethod
        def command_timeout(own_connection=False):
            return 60

        def remote_value(self, name, command, timeout=30):
            self.commands.append(command)
            return self.reply

    def test_a_backend_that_does_not_reap_or_the_setting_off_is_never_asked(self):
        logins = self.FakeLogins(reaps=False)
        self.assertFalse(linger.required(logins))
        self.assertEqual(linger.prefix(logins), "")
        self.assertEqual(linger.scope_setup(logins), "")
        self.assertTrue(linger.assert_enabled(logins, "main"),
                        "a site that keeps processes anyway is not a failure")
        self.assertEqual(logins.commands, [], "and costs no round trip")
        self.assertFalse(linger.required(self.FakeLogins(flag=False)))

    def test_asserting_costs_one_exec_and_a_refusal_is_reported(self):
        logins = self.FakeLogins()
        self.assertTrue(linger.assert_enabled(logins, "main"))
        self.assertEqual(logins.commands, [linger.ENABLE], "never reads first")
        self.assertFalse(linger.assert_enabled(self.FakeLogins(accepts=False),
                                               "main"))

    def test_logind_is_reached_bounded_and_only_when_the_file_is_missing(self):
        # A logind can fail to answer enable-linger at all, and an unbounded
        # assertion piggybacked on a session create would then sit on the
        # caller's 90s timeout. Anything that reaches logind is bounded on the
        # node, the only end that can cut short a call already made.
        prefix = linger.prefix(self.FakeLogins())
        for form in (linger.ENABLE, prefix):
            self.assertIn(f"timeout {linger.TIMEOUT} loginctl", form)
        self.assertLessEqual(linger.TIMEOUT, 10)
        # The file *is* the state logind reads, so a stat answers the question
        # and an already-lingering node costs no bus call — which is what keeps
        # a minute-by-minute assertion honest on a login node under load.
        self.assertNotIn("loginctl", linger.READ)
        self.assertTrue(linger.ENABLE.startswith("test -e "),
                        "asserting must short-circuit on the file first")
        # A node without loginctl must not turn every session create into a
        # failure, and its complaint must not land in a parsed reply.
        self.assertTrue(prefix.endswith("|| true; "))
        self.assertIn(">/dev/null 2>&1", prefix)

    def test_every_half_is_read_in_one_round_trip_and_silence_is_not_a_no(self):
        # The node that most needs asking is the one too busy to ask twice.
        # doctor says different things about a no and a node that will not
        # say: one is a finding, the other is a question that failed.
        for reply, want in (
                ("linger=yes\nkeeper=yes\nscope=user",
                 {"linger": "yes", "keeper": "yes", "scope": "user"}),
                ("linger=yes\nkeeper=no", {"linger": "yes"}),
                ("linger=no\nkeeper=no", {"linger": "no"}),
                ("", {"linger": ""}), ("nonsense", {"linger": ""})):
            with self.subTest(reply=reply):
                logins = self.FakeLogins(reply=reply)
                got = linger.read(logins, "main")
                self.assertEqual({key: got[key] for key in want}, want)
                self.assertEqual(logins.commands, [linger.READ])

    def test_the_two_deaths_are_reported_separately(self):
        # Linger keeps user@$UID.service alive when the account has no
        # sessions; it says nothing about the per-connection scope logind
        # kills when a connection ends. A server in the second one dies on a
        # clean close with linger fully on, so reporting them as one number
        # would call a doomed node healthy.
        from clustertool import diagnostics

        ok, detail = diagnostics.scope_finding("user")
        self.assertTrue(ok)
        self.assertIn("user@", detail)

        ok, detail = diagnostics.scope_finding("session")
        self.assertFalse(ok, "measured on holylogin07: a session-scope server "
                         "does not survive a clean close, linger or not")
        self.assertIn("recreate", detail, "it cannot be moved, so the only "
                      "actionable advice is to make a new one")
        # No server, or a question that failed, is not a finding.
        for scope in ("none", ""):
            self.assertIsNone(diagnostics.scope_finding(scope), scope)

    def test_the_server_is_started_out_of_the_session_scope(self):
        from clustertool.tmuxlayer import ensure_server_snippet

        # $S, not the command itself: set where a user scope actually works
        # and empty where it does not, so this degrades to plain tmux rather
        # than failing to start a server at all.
        self.assertTrue(ensure_server_snippet("work").startswith("$S tmux"))
        self.assertIn("systemd-run --scope --user", linger.SCOPE_SETUP)
        self.assertIn("true >/dev/null 2>&1 &&", linger.SCOPE_SETUP,
                      "a node with systemd-run but no user manager must be "
                      "found out by trying it, not by looking for the binary")
        self.assertIn(f"timeout {linger.TIMEOUT} systemd-run", linger.SCOPE_SETUP,
                      "a wedged user manager costs the try its bound, and the "
                      "session is then started without a scope")

    def test_creating_a_session_asserts_linger_in_the_same_round_trip(self):
        # Only the command that starts a server is wrapped: a session created
        # on a server that already exists is that server's child and inherits
        # its cgroup, so wrapping the client would buy nothing and cost a
        # scope per command.
        from clustertool.tmuxlayer import Tmux

        logins = self.FakeLogins()
        tmux = Tmux(logins)
        tmux.tag_owner = lambda *a, **kw: True
        tmux.crumb_add = lambda *a, **kw: True
        with contextlib.redirect_stderr(io.StringIO()):
            tmux.create("main", "work")
        self.assertEqual(len(logins.commands), 1,
                         "protection must not cost a second round trip")
        self.assertTrue(logins.commands[0].startswith(linger.ENABLE))
        self.assertIn("$S tmux new-session", logins.commands[0])
        self.assertIn(linger.SCOPE_SETUP, logins.commands[0],
                      "$S has to be set in the same shell that uses it")
        for call in ("ensure_server", "register_session"):
            logins = self.FakeLogins()
            getattr(Tmux(logins), call)("main", "work")
            self.assertTrue(logins.commands[0].startswith(linger.ENABLE),
                            f"{call} left the session it just made unprotected")

    def test_the_watcher_says_so_once_then_on_change_and_on_the_way_down(self):
        # Reconciling the keeper costs a `crontab -l` on the node. Worth it
        # when a watcher starts (every boot and every reconnect starts one, so
        # a changed setting is picked up promptly) and on the way down; not
        # worth it once a minute forever for a setting that changes twice a
        # year.
        from clustertool.watcher import Watcher

        logins = self.FakeLogins()
        watcher = Watcher(logins, None, None, "main")
        logged = []
        watcher.log = logged.append
        watcher._assert_linger()
        watcher._assert_linger()
        self.assertEqual(len(logged), 1,
                         "a steady state must not narrate itself every minute")
        watcher._assert_linger(force=True)
        self.assertEqual(len(logged), 2, "the last assertion before a reboot "
                         "is the evidence of whether the work was protected")
        self.assertEqual(
            logins.commands,
            [linger.apply_command(True), linger.ENABLE, linger.apply_command(True)])
        logins.accepts = False
        watcher._assert_linger()
        self.assertEqual(len(logged), 3)
        self.assertIn("may NOT survive", logged[2])
        self.assertIn("too slow", logged[2],
                      "a node that did not answer must not be reported as one "
                      "that refused")

    def test_a_login_that_is_already_down_is_not_reported_as_a_refusal(self):
        # A watcher stopped while its login is down says there is no
        # connection left. "Could not enable linger" there would read
        # afterwards like the reason the work went.
        from clustertool.watcher import Watcher

        logins = self.FakeLogins()
        logins.active = False
        watcher = Watcher(logins, None, None, "main")
        logged = []
        watcher.log = logged.append
        watcher._assert_linger()
        self.assertEqual(logged, [], "nothing to say on an ordinary tick")
        watcher._assert_linger(force=True)
        self.assertEqual(logins.commands, [], "and nothing to assert it over")
        self.assertIn("no connection left", logged[0])

    def test_the_watcher_is_silent_where_linger_is_not_the_mechanism(self):
        from clustertool.watcher import Watcher

        logins = self.FakeLogins(reaps=False)
        watcher = Watcher(logins, None, None, "main")
        watcher.log = lambda _m: self.fail("nothing to say about this backend")
        watcher._assert_linger()
        self.assertEqual(logins.commands, [])

    def test_a_graceful_shutdown_protects_the_work_on_its_way_out(self):
        from clustertool.watcher import Watcher

        logins = self.FakeLogins()
        logins.settings = types.SimpleNamespace(flag=lambda key: True,
                                                int=lambda key: 1)
        watcher = Watcher(logins, None, None, "main")
        watcher.stop = True      # as if SIGTERM landed before the first tick
        watcher.log = lambda _message: None
        handlers = {sig: signal.getsignal(sig)
                    for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            watcher._watch()
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
        self.assertEqual(logins.commands, [linger.apply_command(True)],
                         "a machine on its way down has to tell the node to "
                         "keep the sessions before its connection goes")
        self.assertIn(linger.ENABLE, logins.commands[0])

    def test_the_keeper_is_opt_in_and_append_only(self):
        # A login node's crontab is shared with everything else that user runs
        # there, so this one stays off until it is asked for, and when it is
        # asked for it adds a line rather than installing a crontab.
        off = self.FakeLogins()
        off.settings = types.SimpleNamespace(
            flag=lambda key: key != "LINGER_KEEPER", int=lambda key: 30)
        self.assertFalse(linger.keeper_wanted(off))
        self.assertNotIn(linger.keeper_line(), linger.apply_command(False),
                         "nothing may be added to a shared crontab by default")

        install = linger.keeper_command(True)
        self.assertIn(linger.CRONTAB_READ, install,
                      "the existing crontab has to be read back, not replaced")
        self.assertIn(f"*{linger.KEEPER_MARKER}*) exit 0", install,
                      "and left alone if ours is in it")
        self.assertNotIn("crontab -r", install)

    def test_the_keeper_follows_the_setting_in_both_directions(self):
        # The half that makes cleanup automatic. Installing on demand is easy;
        # the leftover that nothing ever clears is a keeper still asserting
        # once a minute after LINGER_KEEPER went back off, on a node nothing
        # local records having touched. So every path that asserts linger also
        # reconciles the crontab against the setting, whichever way it moved.
        wanted, unwanted = self.FakeLogins(), self.FakeLogins()
        unwanted.settings = types.SimpleNamespace(
            flag=lambda key: key != "LINGER_KEEPER", int=lambda key: 30)

        linger.apply(wanted, "main")
        linger.apply(unwanted, "main")
        self.assertIn("| crontab -", wanted.commands[0])
        self.assertIn("grep -vF", unwanted.commands[0],
                      "turning the setting off has to reach the node by "
                      "itself, or it never takes effect anywhere in use")
        for logins in (wanted, unwanted):
            self.assertEqual(len(logins.commands), 1,
                             "reconciling must not cost a second round trip")
            self.assertIn(linger.ENABLE, logins.commands[0],
                          "and must still assert linger")

    def test_a_crontab_that_will_not_rewrite_is_not_an_unprotected_login(self):
        # The exit status is linger's. A node whose crontab could not be
        # rewritten has had linger asserted all the same, and reporting it as
        # unprotected would send doctor and the shutdown hook shouting about
        # the wrong thing.
        command = linger.apply_command(True)
        self.assertTrue(command.endswith("exit $ok"))
        self.assertIn(f"if {linger.ENABLE}", command,
                      "linger's own result is what is captured")

    def test_the_keeper_is_never_pulled_from_under_somebody_else(self):
        # `close` kills the sessions it owns and spares foreign and untagged
        # ones on purpose, so *settling* a node — leaving one whose contents
        # the caller does not know — must not quietly strip protection from
        # what it spared. That guard is structural: SETTLE only reaches the
        # removal in the branch where the node has no tmux server at all.
        # Verified on holylogin05: session present, the line stayed; node
        # empty, the line went.
        before, after = linger.SETTLE.split("else", 1)
        self.assertNotIn("crontab", before,
                         "a node still running tmux keeps its keeper")
        self.assertIn("grep -vF", after)

        # An explicit instruction is a different question, and answering it
        # with "no" would mean LINGER_KEEPER off never takes effect on any
        # node actually in use. Removal only stops re-assertion; it never
        # disables linger, so it cannot kill anything by itself. It takes
        # only its own line out.
        logins = self.FakeLogins()
        self.assertTrue(linger.remove_keeper(logins, "main"))
        command, = logins.commands
        self.assertNotIn("tmux ls", command)
        self.assertIn("grep -vF", command, "every other line is kept")
        self.assertIn(linger.KEEPER_MARKER, command,
                      "only a line we marked is ever removed")
        self.assertNotIn("crontab -r", command)

    def test_the_keeper_line_is_a_valid_crontab_entry(self):
        line = linger.keeper_line()
        self.assertTrue(line.startswith("* * * * * "))
        self.assertNotIn("\n", line, "a crontab entry is one line")
        self.assertNotIn("%", line, "crontab(5) reads an unescaped '%' as a "
                         "newline and would truncate the command")
        self.assertTrue(line.rstrip().endswith(linger.KEEPER_MARKER),
                        "the line has to be findable again to remove it")
        self.assertIn(linger.ENABLE, line,
                      "the keeper asserts what the client asserts, and stops "
                      "at the same stat when there is nothing to do")

    def test_the_node_decides_what_happens_to_it_in_one_command(self):
        # `repin` passes keep_tmux only to avoid killing sessions twice,
        # `close --keep-tmux` means work is being left behind, a sweep may have
        # spared somebody else's session. No flag knows what is actually
        # running there, so none of them branches: every ending sends the same
        # command and the node answers it.
        import inspect

        from clustertool import sshmux

        logins = self.FakeLogins()
        self.assertTrue(linger.settle(logins, "main"))
        self.assertEqual(logins.commands, [linger.SETTLE])
        # A check followed by an action leaves an interval in which a session
        # appears on the node and is killed by the release that follows.
        self.assertTrue(linger.SETTLE.startswith("if tmux ls"),
                        "the test and the act must be the same command")
        self.assertIn("disable-linger", linger.SETTLE,
                      "releasing a node nothing is left on matters as much as "
                      "asserting one that still has work")
        self.assertNotIn("&&", linger.SETTLE,
                         "with `a && b || c` a failing assert falls through "
                         "to the release and kills what it meant to keep")
        self.assertTrue(linger.SETTLE.endswith("; true"),
                        "a node without loginctl must not fail the close")
        self.assertRegex(
            inspect.getsource(sshmux.Logins.close), r"settle_node|linger\.settle",
            "closing a login leaves the node's linger as it found it: either a "
            "node still holding work it will drop, or one keeping a user "
            "manager and a keeper alive for work that is gone")

    def test_a_login_that_will_not_name_its_node_is_still_protected(self):
        # Measured on holylogin05: the master came up and `hostname -f` did not
        # answer in time. That login still gets sessions, so returning early
        # from the connect path must not skip the one thing that keeps them.
        from clustertool import sshmux

        class Stub(sshmux.Logins):
            def __init__(self, landed):
                self.backend = types.SimpleNamespace(
                    name="fasrc", reaps_on_logout=True, node_choosable=False,
                    ensure_credential=lambda quiet=False: None,
                    short=lambda node: node or "")
                self.settings = types.SimpleNamespace(
                    int=lambda key: 5, flag=lambda key: True)
                self.state = types.SimpleNamespace(
                    pin_read=lambda _name: "", known_logins=lambda: ["main"],
                    socket=lambda name: f"/nonexistent/{name}.sock",
                    master_log_path=lambda name: f"/nonexistent/{name}.log")
                self.landed = landed

            def _cleanup_stale(self, name):
                pass

            def connection_count(self):
                return 0

            def open_master(self, sock, node, log, **_kw):
                return True, ""

            def refresh_meta(self, name):
                return self.landed

        for landed, described in (("", "would not report its node"),
                                  ("holylogin05.rc", "reported its node")):
            protected = []
            with _patched(sshmux.linger, "apply",
                          lambda _l, name, **kw: protected.append(name)), \
                 _patched(sshmux.ui, "warn", lambda *a, **kw: None):
                Stub(landed)._create("main")
            self.assertEqual(protected, ["main"],
                             f"a login that {described} was left unprotected")

    def test_the_shutdown_hook_is_reported_by_where_it_is_installed(self):
        # Only the system unit is ordered against the slice holding the control
        # masters, so only it is guaranteed to still have a connection to
        # assert over. doctor has to be able to tell them apart.
        from clustertool import diagnostics

        self.assertEqual(
            diagnostics.hook_installed(lambda argv: "enabled"), "system",
            "the stronger form wins when both are enabled")
        self.assertEqual(
            diagnostics.hook_installed(
                lambda argv: "enabled" if "--user" in argv else "disabled"),
            "user")
        self.assertEqual(diagnostics.hook_installed(lambda argv: "disabled"), "")
        self.assertEqual(diagnostics.hook_installed(lambda argv: ""), "",
                         "a systemctl that cannot answer is not an install")

    def test_an_enabled_hook_pointing_at_nothing_is_not_an_armed_hook(self):
        # The failure that looks like success from every angle: the unit is
        # enabled, `systemctl status` is clean, the shutdown is orderly, and
        # nothing was asserted. ExecStop's leading '-' is there so an
        # unreachable node cannot fail a shutdown, and it swallows "no such
        # file" just as quietly. Moving the working tree is all it takes,
        # this repo being deployed through a ~/.local/bin symlink into it.
        from clustertool import diagnostics

        def systemctl(argv):
            if "show" in argv:
                return ("{ path=/home/u/.local/bin/cluster ; "
                        "argv[]=/home/u/.local/bin/cluster linger --quiet ; "
                        "ignore_errors=yes ; status=0/0 }")
            return "enabled" if "--user" not in argv else "disabled"

        armed, detail = diagnostics.hook_report(systemctl, lambda _path: True)
        self.assertTrue(armed)
        self.assertIn("system unit", detail)

        armed, detail = diagnostics.hook_report(systemctl, lambda _path: False)
        self.assertFalse(armed, "an enabled unit that runs nothing is not a "
                         "hook, and must not be reported as one")
        self.assertIn("/home/u/.local/bin/cluster", detail,
                      "doctor has to name the path so it can be fixed")

        # But `systemctl show` failing, or printing something this does not
        # parse, is a question that failed — not evidence that the program is
        # gone.
        armed, _detail = diagnostics.hook_report(
            lambda argv: "" if "show" in argv else "enabled",
            lambda _path: self.fail("nothing to check the existence of"))
        self.assertTrue(armed)

    def test_the_program_is_read_from_the_unit_that_is_actually_enabled(self):
        from clustertool import diagnostics

        asked = []

        def systemctl(argv):
            asked.append(argv)
            if "show" in argv:
                return "{ path=/usr/bin/cluster ; ignore_errors=yes }"
            return "enabled" if "--user" in argv else "disabled"

        self.assertEqual(diagnostics.hook_program("user", systemctl),
                         "/usr/bin/cluster")
        self.assertIn("--user", asked[-1],
                      "the user unit's ExecStop is not the system unit's")
        self.assertEqual(diagnostics.hook_program("", systemctl), "",
                         "nothing installed is nothing to ask about")

    def test_a_keeper_left_behind_is_a_finding_of_its_own(self):
        # Nothing on this machine records that a keeper was installed, and it
        # outlives the setting being turned back off — so if doctor does not
        # say it, nothing ever will, and a node goes on asserting linger once
        # a minute for work that stopped existing.
        from clustertool import diagnostics

        for keeper, wanted, says in (
                ("no", False, None), ("yes", True, None),
                ("yes", False, "--remove-keeper"), ("no", True, "cluster linger"),
                # linger.read returns "" for a question that failed. Treating
                # that as "no keeper" would nag about installing one on every
                # loaded node, and treating it as "yes" would invent residue.
                ("", True, None), ("", False, None)):
            with self.subTest(keeper=keeper, wanted=wanted):
                found = diagnostics.keeper_finding(keeper, wanted=wanted)
                if says is None:
                    self.assertIsNone(found)
                else:
                    self.assertFalse(found[0])
                    self.assertIn(says, found[1], "a finding has to be actionable")

    def test_the_hook_installs_itself_in_the_strongest_form_available(self):
        from clustertool import diagnostics

        ran = []

        def runner(argv, stdin=None):
            ran.append(argv)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        kind, detail = diagnostics.install_hook(run=runner)
        self.assertEqual(kind, "system", "the ordered form is tried first")
        self.assertIn("ordered", detail)
        self.assertTrue(any("tee" in " ".join(a) for a in ran))
        self.assertTrue(any("enable" in a for a in ran),
                        "installed but not enabled is not installed")
        self.assertFalse(any("--user" in a for a in ran),
                         "the weaker unit must not be touched once the "
                         "stronger one is in place")

    def test_a_machine_without_root_still_gets_a_hook(self):
        from clustertool import diagnostics

        ran = []

        def runner(argv, stdin=None):
            ran.append(argv)
            # sudo refuses; everything the user manager is asked to do works.
            return types.SimpleNamespace(
                returncode=1 if argv[0] == "sudo" else 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as home:
            with _patched(os, "environ", dict(os.environ, XDG_CONFIG_HOME=home)):
                kind, detail = diagnostics.install_hook(run=runner)
            written = Path(home) / "systemd/user" / diagnostics.HOOK_UNIT
            self.assertTrue(written.is_file(), "the --user unit is a file we "
                            "write ourselves; no root is involved anywhere")
            self.assertIn("ExecStop", written.read_text())
        self.assertEqual(kind, "user")
        self.assertIn("not ordered", detail,
                      "the fallback has to say what it does not guarantee")
        self.assertTrue(any("--user" in a for a in ran))

    def test_an_unattended_install_never_waits_for_a_password(self):
        # `boot` runs from an @reboot cron line. A sudo prompt there waits
        # forever on a terminal that does not exist, and the login it was
        # restoring never comes back.
        from clustertool import diagnostics

        for interactive, expect in ((True, False), (False, True)):
            ran = []
            diagnostics.install_hook(
                interactive=interactive,
                run=lambda argv, stdin=None: (
                    ran.append(argv),
                    types.SimpleNamespace(returncode=0))[1])
            sudo = [a for a in ran if a[0] == "sudo"]
            self.assertTrue(sudo)
            self.assertEqual(all("-n" in a for a in sudo), expect)

    def test_an_install_that_cannot_happen_says_why(self):
        from clustertool import diagnostics

        with tempfile.TemporaryDirectory() as empty:
            kind, detail = diagnostics.install_hook(
                run=lambda argv, stdin=None: self.fail("nothing to run"),
                extras=empty)
        self.assertEqual(kind, "")
        self.assertIn("missing", detail)

    def _create_with(self, reply):
        from clustertool.tmuxlayer import STEP_MARKER, Tmux

        # The create tags and records the session itself, and says so for
        # each step ahead of the scope report.
        recorded = "".join(f"{STEP_MARKER}\t{step}\t0\n"
                           for step in ("owner", "crumb"))
        logins = self.FakeLogins(reply=recorded + reply)
        tmux = Tmux(logins)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            tmux.create("main", "work")
            tmux.create("main", "other")
        return tmux, logins, out.getvalue()

    def test_a_silent_downgrade_is_reported_once_and_a_healthy_scope_not_at_all(self):
        # SCOPE_SETUP degrades quietly on purpose — a node with no user
        # manager should still get its session rather than an error. Quiet is
        # exactly wrong afterwards: without this the tool would go on making
        # sessions that cannot survive and nothing would say so until somebody
        # thought to run doctor. restore-layout creates in a loop, and the
        # fact is about the node, so it is said once, not once per session.
        _tmux, _logins, printed = self._create_with(
            f"{linger.SCOPE_MARKER}=session")
        self.assertIn("will not survive", printed)
        self.assertEqual(printed.count("session scope"), 1)
        _tmux, _logins, printed = self._create_with(
            f"{linger.SCOPE_MARKER}=user")
        self.assertEqual(printed, "", "working is not news")

    def test_the_creates_own_status_is_what_decides_success(self):
        # The report runs after the create and must not be able to turn a
        # session that exists into a reported failure, nor a failure into a
        # success.
        _tmux, logins, _printed = self._create_with("")
        command = logins.commands[0]
        self.assertIn("then rc=0;", command)
        self.assertIn("else rc=1; fi", command)
        self.assertTrue(command.endswith("exit $rc"))
        self.assertIn(linger.SCOPE_MARKER, command)

    def test_a_client_is_never_mistaken_for_the_server(self):
        # An attached client always sits in the session scope of whichever
        # connection has it open, so the server is found by its own pid
        # (`#{pid}`), never by matching "tmux" in a process list.
        self.assertIn("tmux display-message -p '#{pid}'", linger.SERVER_PID)
        self.assertIn("tmux: server", linger.SERVER_PID)
        self.assertNotIn("/tmux/", linger.SERVER_PID)

    def test_the_backend_that_measurably_reaps_declares_it(self):
        # The capability is declared, not discovered, so the declaration is the
        # only thing standing between a FASRC reboot and lost sessions.
        self.assertTrue(load("fasrc").reaps_on_logout)
        self.assertFalse(load("nersc").reaps_on_logout,
                         "NERSC login nodes were measured not to reap")
        self.assertFalse(Backend(Settings("fasrc")).reaps_on_logout,
                         "a site is assumed to keep processes until measured")


class TestShutdownHookRunsThisCluster(unittest.TestCase):
    """The installed unit names the `cluster` that installed it."""

    def render(self, kind, program, repo):
        from clustertool import diagnostics

        filename = dict(diagnostics.HOOK_TEMPLATES)[kind]
        text = (diagnostics.extras_dir() / filename).read_text()
        body = diagnostics.render_hook(text, kind, program=program, repo=repo)
        return [line for line in body.splitlines()
                if line and not line.startswith("#")]

    def test_a_standard_install_renders_home_relative_specifiers(self):
        home = str(Path.home())
        program, repo = f"{home}/.local/bin/cluster", f"{home}/cluster"
        user = self.render("user", program, repo)
        self.assertIn("ExecStop=-%h/.local/bin/cluster linger --quiet", user)
        self.assertIn("Documentation=file:%h/cluster/USAGE.md", user)
        system = self.render("system", program, repo)
        self.assertIn(f"ExecStop=-{home}/.local/bin/cluster linger --quiet", system)
        self.assertIn(f"Documentation=file:{home}/cluster/USAGE.md", system)
        self.assertIn(f"Environment=HOME={home}", system)
        for line in user + system:
            self.assertNotRegex(line, "@[A-Z]+@", "every placeholder is filled")

    def test_a_checkout_anywhere_else_is_named_as_it_is(self):
        user = self.render("user", "/opt/tools/bin/cluster", "/data/100%/cluster")
        self.assertIn("ExecStop=-/opt/tools/bin/cluster linger --quiet", user)
        self.assertIn("Documentation=file:/data/100%%/cluster/USAGE.md", user,
                      "a literal % is a systemd specifier unless doubled")

    def test_the_entry_point_is_the_running_one(self):
        from unittest.mock import patch

        from clustertool import diagnostics

        with patch.object(sys, "argv", ["/opt/c/bin/cluster", "linger"]):
            self.assertEqual(diagnostics.hook_entry_point(), "/opt/c/bin/cluster")
        with patch.object(sys, "argv", ["test_linger.py"]), \
                patch.object(diagnostics.shutil, "which", lambda name: None):
            self.assertEqual(diagnostics.hook_entry_point(),
                             str(diagnostics.repo_root() / "bin" / "cluster"))

        def refuse_sudo(argv, stdin=None):
            return types.SimpleNamespace(
                returncode=1 if argv[0] == "sudo" else 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as home, \
                patch.object(sys, "argv", ["/opt/c/bin/cluster", "linger"]), \
                _patched(os, "environ", dict(os.environ, XDG_CONFIG_HOME=home)):
            kind, _detail = diagnostics.install_hook(run=refuse_sudo)
            written = (Path(home) / "systemd/user" / diagnostics.HOOK_UNIT).read_text()
        self.assertEqual(kind, "user")
        self.assertIn("ExecStop=-/opt/c/bin/cluster linger --quiet", written)


class TestLingerReleasesOnlyWhatItEnabled(unittest.TestCase):
    """`disable-linger` is for undoing our own enable, never somebody else's.

    The node-side snippets are run for real, in sh and in bash, against a fake
    logind: loginctl, tmux, crontab and uname are stubs on PATH, and the linger
    directory is a temp dir standing in for /var/lib/systemd/linger.
    """

    STUBS = {
        "loginctl": """#!/bin/sh
echo "$1" >> "$CALLS"
case "$1" in
  enable-linger) touch "$LINGERDIR/$(id -un)" ;;
  disable-linger) [ -z "$REFUSE_DISABLE" ] || exit 1
                  rm -f "$LINGERDIR/$(id -un)" ;;
esac
""",
        "tmux": '#!/bin/sh\n[ -n "$TMUX_RUNNING" ]\n',
        "crontab": """#!/bin/sh
if [ "$1" = -l ]; then
  [ -z "$CRONTAB_FAIL" ] || { echo "crontab: cannot read the spool" >&2; exit 1; }
  [ -f "$CRONTAB" ] || { echo "no crontab for $(id -un)" >&2; exit 1; }
  cat "$CRONTAB"
else cat > "$CRONTAB"; fi
""",
        "uname": '#!/bin/sh\necho "$NODE"\n',
    }

    @classmethod
    def setUpClass(cls):
        probe = subprocess.run(["ls", "-nid", "--full-time", "/"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if probe.returncode != 0:
            raise unittest.SkipTest("needs GNU ls, as a Linux login node has")
        if shutil.which("timeout") is None:
            raise unittest.SkipTest("needs timeout(1), as a Linux login node has")
        cls.user = subprocess.run(["id", "-un"], stdout=subprocess.PIPE,
                                  text=True, check=True).stdout.strip()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        stubs = self.root / "bin"
        stubs.mkdir()
        for name, body in self.STUBS.items():
            (stubs / name).write_text(body)
            (stubs / name).chmod(0o755)
        (self.root / "home").mkdir()
        self.env = dict(os.environ, PATH=f"{stubs}{os.pathsep}{os.environ['PATH']}",
                        HOME=str(self.root / "home"), CALLS=str(self.root / "calls"),
                        CRONTAB=str(self.root / "crontab"))
        self.on("login01")

    def tearDown(self):
        self.tmp.cleanup()

    def on(self, node):
        """Point the shell at *node*: its own linger dir, the shared home."""
        self.node = node
        self.lingerdir = self.root / "linger" / node
        self.lingerdir.mkdir(parents=True, exist_ok=True)
        self.env.update(NODE=node, LINGERDIR=str(self.lingerdir))

    def run_sh(self, snippet, shell="sh", **env):
        command = snippet.replace(linger.LINGER_DIR, str(self.lingerdir))
        self.assertNotIn(linger.LINGER_DIR, command)
        return subprocess.run([shell, "-c", command], env=dict(self.env, **env),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True).returncode

    @property
    def lingering(self):
        return (self.lingerdir / self.user).exists()

    @property
    def owned(self):
        return self.root / "home" / ".cluster" / "linger" / self.node

    def calls(self):
        path = self.root / "calls"
        found = path.read_text().split() if path.exists() else []
        path.unlink(missing_ok=True)
        return found

    def fresh(self):
        """No linger, no notes and no calls yet, on login01."""
        shutil.rmtree(self.root / "linger", ignore_errors=True)
        shutil.rmtree(self.root / "home" / ".cluster", ignore_errors=True)
        self.calls()
        self.on("login01")

    def each_shell(self):
        for shell in ("sh", "bash"):
            self.fresh()
            with self.subTest(shell=shell):
                yield shell

    def test_linger_this_tool_turned_on_is_turned_off_again(self):
        for shell in self.each_shell():
            self.assertEqual(self.run_sh(linger.ENABLE, shell), 0)
            self.assertTrue(self.lingering)
            self.assertTrue(self.owned.read_text().strip(), "the enable is noted")
            self.assertEqual(self.calls(), ["enable-linger"])
            self.assertEqual(self.run_sh(linger.SETTLE, shell), 0)
            self.assertEqual(self.calls(), ["disable-linger"])
            self.assertFalse(self.lingering)
            self.assertFalse(self.owned.exists(), "and the note goes with it")

    def test_linger_that_was_already_on_is_never_turned_off(self):
        for shell in self.each_shell():
            (self.lingerdir / self.user).touch()
            self.assertEqual(self.run_sh(linger.ENABLE, shell), 0)
            self.assertFalse(self.owned.exists())
            self.assertEqual(self.run_sh(linger.SETTLE, shell), 0)
            self.assertEqual(self.calls(), [], "logind is not even asked")
            self.assertTrue(self.lingering)

    def test_linger_its_owner_turned_on_again_is_theirs(self):
        for shell in self.each_shell():
            self.run_sh(linger.ENABLE, shell)
            later = time.time() + 5
            os.utime(self.lingerdir / self.user, (later, later))  # their enable
            self.calls()
            self.run_sh(linger.SETTLE, shell)
            self.assertEqual(self.calls(), [])
            self.assertTrue(self.lingering)
            self.assertFalse(self.owned.exists(),
                             "the note no longer describes the file, so it goes")

    def test_a_note_from_one_node_says_nothing_about_another(self):
        # Home is shared across a site's login nodes; linger is not.
        self.run_sh(linger.ENABLE)
        self.on("login02")
        (self.lingerdir / self.user).touch()
        self.calls()
        self.run_sh(linger.SETTLE)
        self.assertEqual(self.calls(), [])
        self.assertTrue(self.lingering)
        self.on("login01")
        self.run_sh(linger.SETTLE)
        self.assertEqual(self.calls(), ["disable-linger"])

    def test_a_release_waits_for_an_empty_node_and_is_tried_until_it_takes(self):
        def cleared():
            (self.lingerdir / self.user).unlink()

        for what, between, env, lingering, owned in (
                ("a node still running tmux keeps linger and the note",
                 None, {"TMUX_RUNNING": "1"}, True, True),
                ("linger the node cleared is not released twice",
                 cleared, {}, False, False),
                ("a release that failed is tried again",
                 None, {"REFUSE_DISABLE": "1"}, True, True)):
            with self.subTest(what):
                self.fresh()
                self.run_sh(linger.ENABLE)
                if between:
                    between()
                self.calls()
                self.run_sh(linger.SETTLE, **env)
                self.assertEqual(self.calls(),
                                 ["disable-linger"] if "REFUSE_DISABLE" in env else [])
                self.assertEqual((self.lingering, self.owned.exists()),
                                 (lingering, owned))
        # The failed release, tried again, takes.
        self.run_sh(linger.SETTLE)
        self.assertEqual(self.calls(), ["disable-linger"])
        self.assertFalse(self.lingering)

    def test_the_keeper_notes_its_enable_the_same_way(self):
        # It runs from the node's crontab with nobody else there to remember.
        line = linger.keeper_line()
        command = line[len("* * * * * "):line.index(" # ")]
        self.assertEqual(self.run_sh(command), 0)
        self.assertTrue(self.lingering)
        self.assertTrue(self.owned.read_text().strip())

    def crontab(self):
        path = self.root / "crontab"
        return path.read_text() if path.exists() else None

    def keeper_line(self):
        """The line as installed here, where run_sh moved the linger dir."""
        return linger.keeper_line().replace(linger.LINGER_DIR, str(self.lingerdir))

    def test_the_keeper_goes_in_once_beside_what_is_there_and_comes_out_alone(self):
        mine = "0 3 * * * backup.sh\n# a comment\n"
        crontab = self.root / "crontab"
        for shell in self.each_shell():
            crontab.unlink(missing_ok=True)
            self.assertEqual(self.run_sh(linger.keeper_command(True), shell), 0)
            self.assertEqual(self.crontab(), self.keeper_line() + "\n",
                             "it goes into a node with no crontab")
            crontab.write_text(mine)
            self.run_sh(linger.keeper_command(True), shell)
            self.run_sh(linger.keeper_command(True), shell)
            self.assertEqual(self.crontab(), mine + self.keeper_line() + "\n")
            self.assertEqual(self.run_sh(linger.keeper_command(False), shell), 0)
            self.assertEqual(self.crontab(), mine)
            self.assertEqual(self.run_sh(linger.keeper_command(False), shell), 0)
            self.assertEqual(self.crontab(), mine, "nothing to remove, nothing written")

    def test_a_crontab_that_cannot_be_read_is_never_rewritten(self):
        # An empty read that crontab did not explain as "no crontab" is a
        # failure, and writing it back would wipe the user's crontab.
        mine = "0 3 * * * backup.sh\n" + linger.keeper_line() + "\n"
        for shell in self.each_shell():
            (self.root / "crontab").write_text(mine)
            for command in (linger.keeper_command(True), linger.keeper_command(False),
                            linger.SETTLE, linger.apply_command(False)):
                self.run_sh(command, shell, CRONTAB_FAIL="1")
                self.assertEqual(self.crontab(), mine, command[:40])

    def test_a_crontab_failure_changes_neither_the_linger_answer_nor_the_settle(self):
        self.assertEqual(self.run_sh(linger.apply_command(True), CRONTAB_FAIL="1"), 0)
        self.assertTrue(self.lingering)
        self.calls()
        self.assertEqual(self.run_sh(linger.SETTLE, CRONTAB_FAIL="1"), 0)
        self.assertEqual(self.calls(), ["disable-linger"])
        self.assertFalse(self.lingering)

    def test_every_form_still_reports_logind_and_never_fails_its_caller(self):
        refusing = self.root / "bin" / "loginctl"
        refusing.write_text("#!/bin/sh\nexit 1\n")
        self.assertNotEqual(self.run_sh(linger.ENABLE), 0)
        self.assertFalse(self.owned.exists(), "a refused enable is not ours")
        self.assertNotEqual(self.run_sh(linger.apply_command(False)), 0)
        prefix = linger.prefix(TestLinger.FakeLogins())
        self.assertEqual(self.run_sh(prefix + "exit 3"), 3)
        self.assertEqual(self.run_sh(linger.SETTLE), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
