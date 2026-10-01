#!/usr/bin/env python3
"""Strays, and a `clean` that names them rather than hiding them.

Run: python3 -m unittest tests.test_strays
(or the whole suite: python3 -m unittest discover -s tests -p 'test_*.py')
"""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path

# For `support`, which puts this checkout on sys.path and sets up the sandbox
# every test runs in, so it is imported before anything from clustertool.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import support  # noqa: E402
from support import _patched  # noqa: E402
from clustertool import platform as plat, workstation  # noqa: E402


def clean_ctx(ledger=(), **tmux):
    """What `clean` sees: main on holylogin06, and *tmux*'s answers over the
    defaults of a node read and running nothing."""
    from types import SimpleNamespace

    answers = dict(
        sessions_and_crumbs_checked=lambda _n: (True, [], {}),
        list_sessions_checked=lambda _n, settle=False: (True, []),
        list_sessions_direct_checked=lambda _node, settle=False: (True, []),
        crumb_sync=lambda _n: (0, 0))
    answers.update(tmux)
    return SimpleNamespace(
        by_hand=lambda: None,
        state=SimpleNamespace(
            known_logins=lambda: ["main"], pin_read=lambda _n: "holylogin06.rc",
            read_meta=lambda _n: {}, abandoned=lambda: [],
            abandoned_on=lambda _short: [], ledger_nodes=lambda: list(ledger)),
        tmux=SimpleNamespace(**answers),
        logins=SimpleNamespace(active_names=lambda: ["main"],
                               is_active=lambda _n: True,
                               node_of=lambda _n: "holylogin06.rc"),
        backend=SimpleNamespace(short=lambda n: (n or "").split(".")[0],
                                fqdn=lambda n: f"{n}.rc", name="fasrc"))


def run_clean(ctx, out=None, **flags):
    """`clean` over *ctx* with *flags* set: what it printed."""
    from types import SimpleNamespace
    from clustertool.commands.maintenance import clean_backend

    opts = SimpleNamespace(force=False, all=False, all_backends=False,
                           dry_run=False, include_untagged=False, yes=False)
    vars(opts).update(flags)
    out = out or io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        clean_backend(ctx, opts)
    return out.getvalue()


class TestCleanCatalogueSafety(unittest.TestCase):
    def test_clean_stops_before_sweeping_when_an_active_catalogue_fails(self):
        ctx = clean_ctx(sessions_and_crumbs_checked=lambda _name: (False, [], {}))
        out = io.StringIO()
        with self.assertRaises(SystemExit):
            run_clean(ctx, out)
        self.assertIn("did not touch any sessions or breadcrumbs", out.getvalue())


class TestStrayRecords(unittest.TestCase):
    """Sessions recorded on nodes no login occupies.

    A login repinned off a node leaves its breadcrumbs there. With no login
    occupying that node, nothing else will prune them, so they are reported
    as strays: `clean` must not protect them as live work, and the session
    guard must not refuse a name on the strength of one of them.
    """

    @staticmethod
    def _ctx(pins, abandoned=(), meta=None):
        from types import SimpleNamespace

        meta = meta or {}
        return SimpleNamespace(
            backend=SimpleNamespace(name="fasrc",
                                    short=lambda n: (n or "").split(".")[0]),
            state=SimpleNamespace(
                known_logins=lambda: sorted(pins),
                pin_read=lambda name: pins.get(name, ""),
                read_meta=lambda name: meta.get(name, {}),
                abandoned=lambda: list(abandoned),
            ),
        )

    def test_what_is_a_stray_what_is_a_loss_and_what_is_neither(self):
        from clustertool import strays

        on06 = {"main": "holylogin06.rc"}
        work = {("holylogin06", "work"): "main"}
        for why, pins, crumbs, live, want, extra in (
                ("a crumb on a node no login occupies is a stray", on06,
                 {("holylogin06", "x"): "main", ("boslogin08", "main"): "main"},
                 None, [("boslogin08", "main", strays.STRANDED)], {}),
                # `project` is pinned to holylogin05 and simply not connected.
                # Something is still looking there — `ls` shows it,
                # reconnecting returns to it — so its sessions are not strays.
                ("a disconnected login still occupies its node",
                 dict(on06, project="holylogin05.rc"),
                 {("holylogin05", "work"): "project"}, None, [], {}),
                ("an unpinned but seen login occupies its node too", {"main": ""},
                 {("holylogin06", "x"): "main"}, None, [],
                 {"meta": {"main": {"node": "holylogin06.rc"}}}),
                # A crumb whose session is missing from its own node's live
                # listing is a loss, and is named as one rather than skipped
                # with the rest.
                ("a session that died under its own login is named", on06,
                 {("holylogin06", "gone"): "main", ("holylogin06", "here"): "main"},
                 {"holylogin06": {"here"}}, [("holylogin06", "gone", strays.LOST)], {}),
                # "Did not ask" and "not there" must never render as the same
                # thing: a node too loaded to answer would otherwise be
                # reported as having destroyed everything recorded on it.
                ("no live evidence at all", on06, work, None, [], {}),
                ("live evidence for other nodes only", on06, work, {}, [], {}),
                ("a different node's listing says nothing about this one", on06,
                 work, {"holylogin07": set()}, [], {}),
                # The node was read and is running nothing. That is the reboot
                # case, and the one it most matters to report.
                ("an empty listing is still an answer", on06, work,
                 {"holylogin06": set()}, [("holylogin06", "work", strays.LOST)], {}),
                ("retired and vanished owners are told apart", on06,
                 {("boslogin08", "left"): "main@boslogin08",
                  ("boslogin08", "ghost"): "deleted-login", ("boslogin08", "bare"): ""},
                 None, [("boslogin08", "bare", strays.ORPHAN),
                        ("boslogin08", "ghost", strays.ORPHAN),
                        ("boslogin08", "left", strays.ABANDONED)], {}),
                # The local record says work was left running. Tidying the
                # shared home must not be able to make that statement disappear.
                ("an abandonment record outlives its breadcrumb", on06, {}, None,
                 [("boslogin08", "sim", strays.ABANDONED)],
                 {"abandoned": [("boslogin08", "sim", "main")]})):
            with self.subTest(why):
                ctx = self._ctx(pins, **extra)
                found = strays.collect(ctx, crumbs, **({} if live is None
                                                       else {"live": live}))
                self.assertEqual(sorted((s.node, s.session, s.state) for s in found),
                                 want)

    def test_a_loss_is_reported_with_what_can_be_rebuilt_and_a_stray_with_a_check(self):
        # A stray asks a question ("does this still exist?"); a loss does not,
        # and the only useful reply is the layout snapshot.
        from clustertool import strays

        for row, says, unsaid in (
                (strays.Stray("fasrc", "holylogin06", "gone", "main", strays.LOST),
                 "cluster restore-layout main holylogin06",
                 # A login is sitting on that node; the session is what is
                 # missing, not the attention.
                 "nothing is looking at"),
                (strays.Stray("fasrc", "boslogin08", "main", "main", strays.STRANDED),
                 "cluster strays check boslogin08", None)):
            with self.subTest(state=row.state):
                out = io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                    strays.report([row])
                self.assertIn(says, out.getvalue())
                if unsaid:
                    self.assertNotIn(unsaid, out.getvalue())

    def test_select_takes_a_node_a_session_or_both(self):
        from clustertool import strays

        rows = [strays.Stray("fasrc", "boslogin08", "main", "main", strays.STRANDED),
                strays.Stray("fasrc", "boslogin08", "train", "main", strays.STRANDED),
                strays.Stray("fasrc", "holylogin08", "main", "main", strays.STRANDED)]
        self.assertEqual(len(strays.select(rows, "boslogin08")), 2)
        self.assertEqual(len(strays.select(rows, "train")), 1)
        self.assertEqual(len(strays.select(rows, "boslogin08:main")), 1)
        self.assertEqual(strays.select(rows, "nothing"), [])
        # A bare token is a node first: that is the unit one visit answers for.
        self.assertEqual([s.session for s in strays.select(rows, "boslogin08")],
                         ["main", "train"])


class TestCleanClearsDebrisOnlyWhenAnswered(unittest.TestCase):
    """A record naming a session its own node is not running.

    Reaping it destroys no work — there is none left — but it does destroy the
    last thing that names the work, which `restore-layout` reads. So it is
    offered, not assumed.
    """

    def _fixture(self):
        self.removed = []
        # The node is read and is running nothing; the crumb says it had
        # 'gone'. That is the whole shape of a loss.
        return clean_ctx(
            sessions_and_crumbs_checked=lambda _n: (
                True, [], {("holylogin06", "gone"): "main"}),
            crumbs_remove=lambda login, sessions, node=None: (
                self.removed.extend(sessions), True)[1])

    def test_the_loss_is_named_with_what_can_be_rebuilt_and_y_clears_it(self):
        text = run_clean(self._fixture())
        self.assertIn("holylogin06:gone", text)
        self.assertIn("cluster restore-layout main holylogin06", text)
        run_clean(self._fixture(), yes=True)
        self.assertEqual(self.removed, ["gone"])

    def test_an_unanswered_prompt_keeps_the_record_and_still_sweeps(self):
        # An unattended `clean` — a cron line, the boot path — must not abort
        # over a question nobody is there to answer: the record is kept and
        # the sweep still runs.
        with _patched(plat, "terminal_attached", lambda: False):
            text = run_clean(self._fixture())
        self.assertEqual(self.removed, [], "nothing agreed to this")
        self.assertIn("pass -y", text)
        self.assertIn("sweeping", text, "the sweep itself still has to happen")

    def test_a_refused_credential_ends_the_direct_sweep_but_not_the_rest(self):
        # One refusal says every node would refuse it, and each try counts;
        # the node a login is on needs none.
        ctx = self._fixture()
        ctx.backend.pool_nodes = lambda: ["holylogin07.rc", "holylogin06.rc",
                                          "holylogin08.rc"]
        ctx.logins.last_failure = ""
        visited = []

        def refused(node, settle=False):
            visited.append(node)
            ctx.logins.last_failure = "u@h: Permission denied (keyboard-interactive)."
            return False, []

        ctx.tmux.list_sessions_direct_checked = refused
        ctx.tmux.list_sessions_checked = lambda login, settle=False: (
            visited.append(login) or (True, []))
        text = run_clean(ctx, all=True)
        self.assertEqual(visited, ["holylogin07.rc", "main"])
        self.assertIn("not tried on the other node(s), which would refuse it "
                      "too: holylogin08", text)

    def test_a_dry_run_clears_nothing(self):
        text = run_clean(self._fixture(), dry_run=True, yes=True)
        self.assertEqual(self.removed, [],
                         "--yes is an answer to a prompt, not a licence for "
                         "a dry run to change something")
        self.assertIn("dry run", text)


class TestCleanNamesStraysInsteadOfHidingThem(unittest.TestCase):
    def test_a_crumb_whose_owner_lives_elsewhere_is_not_protected_work(self):
        """A session is protected work only on the node its owner occupies.

        A crumb anywhere else is a stray, which `clean` keeps and names
        rather than protecting it forever without a word."""
        from types import SimpleNamespace

        row = SimpleNamespace(name="sim", owner="main", foreign="",
                              tagged=True, attached=False, windows="1")
        listed = []
        text = run_clean(clean_ctx(
            sessions_and_crumbs_checked=lambda _n: (
                True, [], {("boslogin08", "sim"): "main"}),
            list_sessions_checked=lambda name, settle=False: (
                listed.append((name, settle)) or (True, [])),
            list_sessions_direct_checked=lambda node, settle=False: (
                listed.append((node, settle)) or (True, [row]))))
        self.assertIn("kept (stray", text)
        self.assertIn("boslogin08:sim", text)
        self.assertIn("cluster strays", text)
        self.assertNotIn("killed:", text)
        self.assertEqual(listed, [("main", True), ("boslogin08.rc", True)],
                         "a sweep settles every node it visits, in the command "
                         "that lists it, whether it kills anything there or "
                         "spares everything")


class TestStrayCommand(unittest.TestCase):
    """The verbs that reconcile strays: what they cost and what they refuse."""

    def _ctx(self, crumbs, live=(), killed=(), abandoned=()):
        from types import SimpleNamespace

        self.calls = []
        self.removed = []
        state = SimpleNamespace(
            known_logins=lambda: ["main"],
            pin_read=lambda n: "holylogin06.rc" if n == "main" else "",
            read_meta=lambda _n: {},
            abandoned=lambda: list(abandoned),
            abandon_forget=lambda node, session: self.calls.append(
                ("forget", node, session)),
            read_list_evidence=lambda: {"evidence": []},
        )

        def crumb_remove(login, session, node=None):
            self.removed.append((node, session))
            return True

        def kill_direct(node, sessions, settle=False):
            self.calls.append(("kill", node, tuple(sessions), settle))
            return list(killed), [s for s in sessions if s not in killed]

        tmux = SimpleNamespace(
            node_sessions_and_crumbs=lambda name, with_sessions=True, \
                with_crumbs=True, timeout=60: (
                    self.calls.append(("read", name)) or
                    ("holylogin06.rc", [], dict(crumbs))),
            list_sessions_direct_explained=lambda node: (
                self.calls.append(("visit", node)) or
                (True, [SimpleNamespace(name=n) for n in live], "")),
            kill_sessions_direct=kill_direct,
            crumb_remove=crumb_remove,
            crumb_add=lambda login, session, node=None, owner=None: True,
            rename_session_direct=lambda node, old, new: (
                self.calls.append(("rename", node, old, new)) or True),
        )
        ctx = SimpleNamespace(
            by_hand=lambda: None,
            backend=SimpleNamespace(name="fasrc", label="Harvard FASRC",
                                    paces_totp=True,
                                    short=lambda n: (n or "").split(".")[0],
                                    fqdn=lambda n: f"{n}.rc"),
            state=state, tmux=tmux,
            settings=SimpleNamespace(str=lambda _k: "main",
                                     flag=lambda _k: True),
            logins=SimpleNamespace(
                active_names=lambda: ["main"], last_failure="",
                ensure=lambda name: self.calls.append(("connect", name))),
            scope=lambda: ["fasrc"],
        )
        ctx.sibling = lambda _name: ctx
        return ctx

    def strays(self, ctx, *args, dies=False):
        """`cluster strays ARGS`: (its status, or None when it died; what it said)."""
        from clustertool.commands.strays import cmd_strays

        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            if not dies:
                return cmd_strays(ctx, list(args)), out.getvalue()
            with self.assertRaises(SystemExit):
                cmd_strays(ctx, list(args))
        return None, out.getvalue()

    def test_listing_never_opens_a_connection(self):
        rc, said = self.strays(self._ctx({("boslogin08", "main"): "main"}))
        self.assertEqual(rc, 0)
        self.assertIn("boslogin08", said)
        self.assertNotIn(("connect", "main"), self.calls)
        self.assertNotIn("visit", [c[0] for c in self.calls])

    def test_check_drops_only_the_records_it_proved_gone(self):
        ctx = self._ctx({("boslogin08", "main"): "main",
                         ("boslogin08", "train"): "main"},
                        live=["train"])
        rc, said = self.strays(ctx, "check", "boslogin08", "-y")
        self.assertEqual(rc, 0)
        self.assertEqual(self.removed, [("boslogin08", "main")])
        self.assertIn("still running", said)

    def test_an_unreachable_node_keeps_every_record(self):
        ctx = self._ctx({("boslogin08", "main"): "main"})
        ctx.tmux.list_sessions_direct_explained = lambda _node: (
            False, [], "no usable credential for this backend")
        rc, said = self.strays(ctx, "check", "boslogin08", "-y")
        self.assertEqual(rc, 1)
        self.assertEqual(self.removed, [])
        self.assertIn("boslogin08: no usable credential for this backend; "
                      "its records are unchanged", said)

    def test_a_refused_credential_is_not_tried_on_the_next_node(self):
        # Every node refuses it the same way, and each try counts.
        ctx = self._ctx({("boslogin08", "main"): "main",
                         ("boslogin09", "main"): "main"})

        def refused(node):
            self.calls.append(("visit", node))
            ctx.logins.last_failure = "u@b: Permission denied (keyboard-interactive)."
            return False, [], "the credential was refused"

        ctx.tmux.list_sessions_direct_explained = refused
        rc, said = self.strays(ctx, "check", "main", "-y")
        self.assertEqual(rc, 1)
        self.assertEqual([c for c in self.calls if c[0] == "visit"],
                         [("visit", "boslogin08.rc")])
        self.assertIn("not tried on boslogin09", said)
        self.assertEqual(self.removed, [])

    def test_clearing_refuses_to_run_unattended_without_consent(self):
        ctx = self._ctx({("boslogin08", "main"): "main"})
        with _patched(plat, "terminal_attached", lambda: False):
            self.strays(ctx, "clear", "boslogin08", dies=True)
        self.assertEqual(self.removed, [])

    def test_a_kill_keeps_the_record_until_confirmed_and_settles_as_it_kills(self):
        # The rule everywhere else in this tool: nothing is forgotten until it
        # is proven dead.
        rc, said = self.strays(self._ctx({("boslogin08", "main"): "main"}, killed=[]),
                               "kill", "boslogin08", "-y")
        self.assertEqual(rc, 1)
        self.assertEqual(self.removed, [])
        self.assertIn("could not confirm dead", said)
        ctx = self._ctx({("boslogin08", "main"): "main"}, killed=["main"])
        self.assertEqual(self.strays(ctx, "kill", "boslogin08", "-y")[0], 0)
        self.assertEqual([call for call in self.calls if call[0] == "kill"],
                         [("kill", "boslogin08.rc", ("main",), True)])

    def test_renaming_moves_the_record_and_refuses_a_name_already_recorded(self):
        ctx = self._ctx({("boslogin08", "main"): "main"})
        self.assertEqual(
            self.strays(ctx, "rename", "boslogin08:main", "oldmain", "-y")[0], 0)
        self.assertIn(("rename", "boslogin08.rc", "main", "oldmain"), self.calls)
        self.assertEqual(self.removed, [("boslogin08", "main")])
        ctx = self._ctx({("boslogin08", "main"): "main",
                         ("holylogin06", "x"): "main"})
        self.strays(ctx, "rename", "boslogin08:main", "x", "-y", dies=True)
        self.assertNotIn("rename", [c[0] for c in self.calls])

    def test_adopt_pins_before_connecting_and_never_over_a_holder(self):
        ctx = self._ctx({("boslogin08", "main"): "main"})
        ctx.state.login_pinned_to = lambda node, exclude=None, short=None: ""
        ctx.state.pin_write = lambda name, node: self.calls.append(
            ("pin", name, node))
        ctx.tmux.tag_owner = lambda login, session, owner=None, force=False: (
            self.calls.append(("tag", session, owner, force)) or True)
        self.assertEqual(
            self.strays(ctx, "adopt", "boslogin08", "--as", "old", "-y")[0], 0)
        order = [c[0] for c in self.calls]
        self.assertLess(order.index("pin"), order.index("connect"),
                        "the pin must be written before dialling the node")
        self.assertIn(("tag", "main", "old", True), self.calls)

        ctx = self._ctx({("boslogin08", "main"): "main"})
        ctx.state.login_pinned_to = lambda node, exclude=None, short=None: "other"
        ctx.state.pin_write = lambda *a: self.fail("must not pin over a holder")
        self.strays(ctx, "adopt", "boslogin08", "--as", "old", "-y", dies=True)


class TestCleanForceIsNotIncludeUntagged(unittest.TestCase):
    """`--force` answers two questions; whether to kill untagged work is not one."""

    def sweep(self, **flags):
        from types import SimpleNamespace

        rows = [
            SimpleNamespace(name="scratch", owner="", foreign="", tagged=False,
                            attached=False, windows="1"),
            SimpleNamespace(name="theirs", owner="", foreign="othertool",
                            tagged=True, attached=False, windows="1"),
            # Made under this machine's workstation ID, by a login it forgot.
            SimpleNamespace(name="old", owner="gone", foreign="", tagged=True,
                            attached=False, windows="1",
                            workstation=workstation.ident()),
            # Made before workstation IDs, by a login unknown here.
            SimpleNamespace(name="legacy", owner="gone", foreign="", tagged=True,
                            attached=False, windows="1"),
            # Another machine's: never this machine's to reap.
            SimpleNamespace(name="laptop", owner="gone", foreign="workstation lap-1",
                            tagged=True, attached=False, windows="1",
                            workstation="lap-1"),
        ]
        killed = []

        def kill_sessions(_login, names, settle=False):
            killed.extend(names)
            return list(names), []

        run_clean(clean_ctx(list_sessions_checked=lambda _n, settle=False: (True, rows),
                            kill_sessions=kill_sessions), **flags)
        return sorted(killed)

    def test_force_reaps_foreign_sessions_but_not_untagged_ones(self):
        from clustertool.cli import COMMANDS, declared_options

        self.assertEqual(self.sweep(), ["old"])
        self.assertEqual(self.sweep(force=True), ["legacy", "old", "theirs"])
        self.assertEqual(self.sweep(include_untagged=True), ["old", "scratch"])
        self.assertEqual(self.sweep(force=True, include_untagged=True),
                         ["legacy", "old", "scratch", "theirs"])
        # And the help says so.
        helps = dict(declared_options(COMMANDS["clean"]))
        self.assertIn("still need --include-untagged", helps["--force"])


class TestSweepsSettleInTheCommandsTheySend(unittest.TestCase):
    """A node is settled in a command a sweep sends anyway, never in one of its own.

    Every direct command to a FASRC node is an authentication, so a settle of
    its own would cost one per node visited.
    """

    def layer(self, required=True):
        from subprocess import CompletedProcess
        from types import SimpleNamespace
        from clustertool.tmuxlayer import LS_MARKER, STEP_MARKER, Tmux

        self.sent = []

        def reply(where, snippet):
            self.sent.append((where, snippet))
            return CompletedProcess(
                [], 0, f"{STEP_MARKER}\tapi\n{LS_MARKER}\nkeep\t1\t0\tmain\t\n", "")

        logins = SimpleNamespace(
            backend=SimpleNamespace(short=lambda n: (n or "").split(".")[0],
                                    reaps_on_logout=required),
            state=SimpleNamespace(note_sessions=lambda *_a, **_k: None),
            settings=SimpleNamespace(flag=lambda key: key == "LINGER"),
            node_of=lambda _name: "holylogin06.rc",
            command_timeout=lambda own_connection=False: 60,
            run_remote=lambda _login, snippet, **_kw: reply("login", snippet))
        tmux = Tmux(logins)
        tmux._direct_run = lambda _node, snippet, timeout=120: reply("direct", snippet)
        return tmux

    def test_a_kill_over_a_login_is_one_command_and_one_for_the_records(self):
        from clustertool import linger
        from clustertool.tmuxlayer import LS_MARKER

        tmux = self.layer()
        self.assertEqual(tmux.kill_sessions("main", ["api", "worker"], settle=True),
                         (["api", "worker"], []))
        self.assertEqual([where for where, _ in self.sent], ["login", "login"])
        kill, records = (snippet for _, snippet in self.sent)
        self.assertIn("kill-session -t =api: ", kill)
        self.assertIn("kill-session -t =worker: ", kill)
        self.assertLess(kill.index(LS_MARKER), kill.index(linger.SETTLE),
                        "the node is settled after the kills it confirms")
        self.assertIn("/holylogin06/api ", records)
        self.assertIn("/holylogin06/worker ", records)

    def test_a_direct_kill_settles_in_its_one_connection(self):
        from clustertool import linger

        tmux = self.layer()
        tmux.kill_sessions_direct("boslogin08.rc", ["api"], settle=True)
        self.assertEqual(len(self.sent), 1)
        self.assertIn(linger.SETTLE, self.sent[0][1])
        self.sent.clear()
        self.assertEqual(tmux.kill_sessions_direct("boslogin08.rc", [], settle=True),
                         ([], []))
        self.assertEqual(self.sent, [("direct", linger.SETTLE)],
                         "a node with nothing to kill is still settled, once")

    def test_a_listing_settles_only_when_asked_and_needed(self):
        from clustertool import linger

        for required, settle, expected in ((True, True, True), (True, False, False),
                                           (False, True, False)):
            with self.subTest(required=required, settle=settle):
                tmux = self.layer(required)
                tmux.list_sessions_checked("main", settle=settle)
                tmux.list_sessions_direct_checked("boslogin08.rc", settle=settle)
                self.assertEqual([linger.SETTLE in snippet for _, snippet in self.sent],
                                 [expected, expected])

    def sweep(self, dry_run=False):
        """`clean` over holylogin06, where main is, and boslogin08, where no login is."""
        from types import SimpleNamespace

        rows = [SimpleNamespace(name="old", owner="gone", foreign="", tagged=True,
                                attached=False, windows="1",
                                workstation=workstation.ident())]
        calls = []

        def note(*call):
            calls.append(call)
            return call

        run_clean(clean_ctx(
            ledger=["boslogin08.rc"],
            list_sessions_checked=lambda name, settle=False: (
                note("list", name, settle) and (True, rows)),
            list_sessions_direct_checked=lambda node, settle=False: (
                note("list", node, settle) and (True, rows)),
            kill_sessions=lambda login, names, settle=False: (
                note("kill", login, tuple(names), settle) and (list(names), [])),
            kill_sessions_direct=lambda node, names, settle=False: (
                note("kill", node, tuple(names), settle) and (list(names), [])),
            crumbs_remove=lambda login, names, node=None: bool(
                note("records", login, tuple(names), node))), dry_run=dry_run)
        return calls

    def test_clean_settles_each_node_in_the_commands_that_list_and_kill(self):
        self.assertEqual(self.sweep(), [
            ("list", "main", True),
            ("kill", "main", ("old",), True),
            ("list", "boslogin08.rc", True),
            ("kill", "boslogin08.rc", ("old",), True),
            ("records", "main", ("old",), "boslogin08.rc"),
        ])
        # A dry run settles nothing.
        self.assertEqual(self.sweep(dry_run=True), [
            ("list", "main", False),
            ("list", "boslogin08.rc", False),
        ])


class TestForgetAsksFirst(unittest.TestCase):
    """`forget` drops every pin on a backend, so nobody's silence agrees to it."""

    def forget(self, args, attached):
        from types import SimpleNamespace
        from clustertool.commands.maintenance import cmd_forget

        self.closed = []
        state = SimpleNamespace(
            dir=Path(support.HOME), forget_login_files=lambda _n: None,
            pin_clear=lambda _n: None, drop_meta=lambda _n: None,
            default_mountpoint=lambda n: Path(support.HOME) / "no-such" / n)
        ctx = SimpleNamespace(
            by_hand=lambda: None,
            backend=SimpleNamespace(name="fasrc"), state=state,
            mounts=SimpleNamespace(stop_watcher=lambda *_a, **_k: None,
                                   unmount=lambda *_a, **_k: None,
                                   close_mount_master=lambda _n: None),
            logins=SimpleNamespace(close=lambda name, **_k: self.closed.append(name)))
        err = io.StringIO()
        with _patched(plat, "terminal_attached", lambda: attached), \
                contextlib.redirect_stdout(err), contextlib.redirect_stderr(err):
            try:
                rc = cmd_forget(ctx, list(args))
            except SystemExit as exc:
                rc = exc.code
        return rc, err.getvalue()

    def test_unattended_it_refuses_without_yes_and_a_dry_run_changes_nothing(self):
        for args, rc, closed, says in (
                (["work"], 1, [], "pass -y to forget it unattended"),
                (["work", "-y"], 0, ["work"], ""),
                (["work", "-n"], 0, [], "would forget")):
            with self.subTest(args=args):
                got, err = self.forget(args, attached=False)
                self.assertEqual((got, self.closed), (rc, closed))
                self.assertIn(says, err)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestAnotherWorkstationsRecords(unittest.TestCase):
    """A breadcrumb another machine wrote is listed apart, never as a stray."""

    def ctx(self, logins=()):
        from types import SimpleNamespace

        return SimpleNamespace(
            backend=SimpleNamespace(name="fasrc",
                                    short=lambda n: (n or "").split(".")[0]),
            state=SimpleNamespace(known_logins=lambda: list(logins),
                                  pin_read=lambda _n: "",
                                  read_meta=lambda _n: {},
                                  abandoned=lambda: []))

    def test_they_are_elsewhere_not_orphans(self):
        from clustertool import strays

        Owner = workstation.Owner
        crumbs = {("holylogin06", "api"): Owner("main", "server-1234"),
                  ("holylogin06", "mine"): Owner("gone", workstation.ident()),
                  ("holylogin06", "legacy"): Owner("gone")}
        found = {s.session: s.state for s in strays.collect(self.ctx(), crumbs)}
        self.assertEqual(found, {"mine": strays.ORPHAN, "legacy": strays.ORPHAN})
        apart = strays.elsewhere(self.ctx(), crumbs)
        self.assertEqual([(s.session, s.owner, s.state) for s in apart],
                         [("api", "main", "server-1234")])
        self.assertEqual(strays.classify(Owner("main", "server-1234"), {"main"}),
                         strays.ELSEWHERE, "a login of the same name here is "
                         "another login")
