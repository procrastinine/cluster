#!/usr/bin/env python3
"""Sessions and tmux: attach, close, kill, the session cache and crumbs.

Also choosing a default session, reading tmux catalogues safely, and
restore-layout after a loss.

Run: python3 -m unittest tests.test_sessions
(or the whole suite: python3 -m unittest discover -s tests -p 'test_*.py')
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

# For `support`, which puts this checkout on sys.path and sets up the sandbox
# every test runs in, so it is imported before anything from clustertool.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import support  # noqa: E402
from support import temp_state  # noqa: E402
from clustertool import ui  # noqa: E402
from clustertool.backends import load  # noqa: E402
from clustertool.tmuxlayer import valid_name  # noqa: E402


class TestNames(unittest.TestCase):
    def test_no_name_may_start_with_a_dash(self):
        # A name starting with a dash would be parsed as an option by every
        # command that takes it.
        for name in ("--resume", "-d", ""):
            self.assertFalse(valid_name(name), name)
        for name in ("main", "main2", "my-login", "my_login", "a.b"):
            self.assertTrue(valid_name(name), name)


class TestDefaultSession(unittest.TestCase):
    """`cluster attach LOGIN` with no session named must not guess badly.

    Naming the session after the login is only a convention. Following it
    blindly would create a second, empty session next to the real work whenever
    that work lives under another name, such as a login 'work' holding a
    session 'api'.
    """

    class FakeRow:
        def __init__(self, name, foreign=""):
            self.name, self.foreign = name, foreign

    class FakeCtx:
        def __init__(self, rows):
            self.tmux = type("T", (), {
                "list_sessions": staticmethod(lambda login: rows)})()

    def pick(self, names, foreign=()):
        from clustertool.commands.sessions import default_session

        rows = [self.FakeRow(n, "other" if n in foreign else "") for n in names]
        return default_session(self.FakeCtx(rows), "work")

    def test_the_convention_then_a_lone_session_then_a_new_one_never_a_guess(self):
        for names, foreign, want, said in (
                (["api", "work", "shell"], (), "work", ""),
                # Attached even under another name, rather than an empty second
                # session being created beside it.
                (["api"], (), "api", "has one session"),
                # And it points at the other thing a user often wants here: a
                # raw ssh shell, untracked and not durable, is another verb.
                ([], (), "work", "cluster shell work"),
                # A foreign session is not ours to attach to: it must neither
                # make the choice look ambiguous nor be picked as the only one.
                (["theirs"], {"theirs"}, "work", ""),
                (["api", "theirs"], {"theirs"}, "api", "")):
            with self.subTest(names=names):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertEqual(self.pick(names, foreign), want)
                self.assertIn(said, err.getvalue())
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            self.pick(["a1", "a2"])
        self.assertIn("has 2 sessions", err.getvalue())
        self.assertIn("cluster attach work SESSION", err.getvalue())

    def test_rows_can_be_passed_in_instead_of_fetched(self):
        """attach reads the session list once and hands it here.

        The decision stays in Python — that is why attach is two round trips and
        not one — so it has to be able to work from rows it did not fetch.
        """
        from clustertool.commands.sessions import default_session

        asked = []
        ctx = type("C", (), {"tmux": type("T", (), {
            "list_sessions": staticmethod(lambda login: asked.append(login) or [])})()})()
        rows = [self.FakeRow("work"), self.FakeRow("other")]
        self.assertEqual(default_session(ctx, "work", sessions=rows), "work")
        self.assertEqual(asked, [], "must not fetch when the rows were given")


class TestAttachBatchedRoundTrips(unittest.TestCase):
    """`attach` asks two questions and issues three commands: 2 trips, not 5.

    A channel costs the same whatever it carries — sshd's per-session setup
    dominates, which is why `ls` batches too (node_and_sessions_snippet). On a
    login node at load average 25 a bare round trip measured 1.0-3.6s, so five of
    them would be most of attach's wall clock.
    """

    def test_the_read_carries_both_payloads_and_a_missing_one_is_absent(self):
        """Two markers, not one: both halves are tab-separated lines of unfixed
        length, so without a second marker the crumb rows would parse as
        sessions (three fields is a valid session row) and invent sessions
        that do not exist."""
        from clustertool.tmuxlayer import parse_sessions_and_crumbs, SESSIONS_MARKER, CRUMBS_MARKER

        text = "\n".join([SESSIONS_MARKER, "main\t3\t1\tmain\t", "dev\t1\t0\tmain\t",
                          CRUMBS_MARKER, "boslogin08\tmain\tmain", "boslogin08\tdev\tmain"])
        sessions, crumbs = parse_sessions_and_crumbs(text)
        self.assertEqual([s.name for s in sessions], ["main", "dev"])
        self.assertEqual(crumbs, {("boslogin08", "main"): "main",
                                  ("boslogin08", "dev"): "main"})
        for text, names, crumbs in (
                (f"{SESSIONS_MARKER}\n{CRUMBS_MARKER}\nboslogin08\tghost\tmain", [], 1),
                # No sessions is still a real answer.
                (f"{SESSIONS_MARKER}\n{CRUMBS_MARKER}", [], 0),
                # Nothing ran at all, or the listing ran but the crumb half did not.
                ("", [], 0), ("garbage", [], 0),
                (f"{SESSIONS_MARKER}\nmain\t1\t0\tmain\t", ["main"], 0)):
            with self.subTest(text=text):
                sessions, got = parse_sessions_and_crumbs(text)
                self.assertEqual([s.name for s in sessions], names)
                self.assertEqual(len(got), crumbs)

    def test_the_session_create_leads_and_an_existing_session_counts(self):
        """tmux 2.7 on FASRC loses its server if a query touches it first.

        The query starts a server, that server exits, and the follow-up
        new-session attaches to a corpse. Batching is only safe while the
        idempotent create leads, so the order is pinned by a test and not just by
        a comment.

        "Ensure" means "it exists now", not "I made it": on tmux 2.7 the -A path
        for an existing session tries to attach even under -d and exits 1 with
        `open terminal failed: not a terminal` — the common case. Without the
        has-session fallback the step would report failure almost every time it
        succeeds.
        """
        from clustertool.tmuxlayer import ensure_server_snippet, register_session_snippet

        snippet = register_session_snippet("main", "work")
        ensure = ensure_server_snippet("work")
        # The scope prefix may precede it — `$S` is a variable, not a tmux
        # command — but no tmux command may.
        for text in (snippet, ensure):
            self.assertTrue(text.startswith("$S tmux new-session -A -d -s work"), text)
            self.assertEqual(text.index("tmux"), text.index("tmux new-session"),
                             "a query that touches the server first loses it")
        self.assertLess(snippet.index("new-session"), snippet.index("show-options"))
        self.assertLess(snippet.index("new-session"), snippet.index("mkdir"))
        self.assertIn("has-session -t =work:", ensure)
        self.assertIn("||", ensure)
        self.assertIn(ensure, snippet)

    def test_the_node_is_resolved_remotely_and_names_are_spliced_intact(self):
        """Otherwise an unpinned login costs a live_node() trip and this is 3.

        ${n%%.*} is backend.short() in shell: the first DNS label.
        """
        from clustertool.tmuxlayer import (crumbs_snippet, register_session_snippet,
                                           sessions_and_crumbs_snippet)

        self.assertIn("hostname -f", register_session_snippet("main", "work"))
        given = register_session_snippet("main", "work", node="boslogin08")
        self.assertNotIn("hostname -f", given)
        self.assertIn("n=boslogin08", given)
        # require_name allows dots, dashes and underscores; they must survive
        # being spliced into a compound shell line.
        snippet = register_session_snippet("my-login", "a.b_c-1")
        self.assertIn("-s a.b_c-1", snippet)
        self.assertIn("/a.b_c-1", snippet)
        # Two copies of a subtle shell test drift apart, so the batched read
        # embeds the one definition.
        self.assertIn(crumbs_snippet(), sessions_and_crumbs_snippet())

    def test_every_step_reports_its_own_status_and_silence_is_a_failure(self):
        """The whole point of per-step markers: one exit status could not say.

        A missing breadcrumb in particular is how a session becomes invisible to
        `clean`, so it must not hide behind a successful create.
        """
        from clustertool.tmuxlayer import parse_register_steps, STEP_MARKER

        def lines(*steps):
            return "\n".join(f"{STEP_MARKER}\t{name}\t{rc}" for name, rc in steps)

        ok = {"server": True, "owner": True, "crumb": True}
        for text, want in (
                (lines(("server", 0), ("owner", 0), ("crumb", 0)), ok),
                (lines(("server", 0), ("owner", 0), ("crumb", 1)),
                 dict(ok, crumb=False)),
                # The connection died after the first step.
                (lines(("server", 0)), dict(ok, owner=False, crumb=False)),
                ("", dict.fromkeys(ok, False)),
                ("motd line\nsessions attached\n"
                 + lines(("server", 0), ("bogus", 0), ("owner", 0), ("crumb", 0)), ok)):
            with self.subTest(text=text):
                self.assertEqual(parse_register_steps(text), want)

    @staticmethod
    def _guard_ctx(node="login01.x", logins=("main", "other"), asked=None):
        from types import SimpleNamespace

        return SimpleNamespace(
            logins=SimpleNamespace(node_of=lambda n: node),
            backend=SimpleNamespace(short=lambda f: (f or "").split(".")[0]),
            state=SimpleNamespace(known_logins=lambda: list(logins),
                                  read_meta=lambda _n: {}),
            tmux=SimpleNamespace(crumbs=lambda login: (
                asked if asked is not None else []).append(login) or {}),
        )

    @staticmethod
    def _guard(ctx, login, session, crumbs=None):
        from clustertool.commands.sessions import guard_session_elsewhere

        return guard_session_elsewhere(ctx, login, session, crumbs=crumbs)

    def test_a_live_collision_is_refused_from_crumbs_it_was_given(self):
        # Registered on a different node under another *live* login: the real
        # collision, refused without fetching anything, stale record or not.
        for crumbs in ({("othernode", "work"): "other"},
                       {("oldnode", "work"): "main", ("othernode", "work"): "other"}):
            with self.subTest(crumbs=crumbs):
                asked = []
                err = io.StringIO()
                with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
                    self._guard(self._guard_ctx(asked=asked), "main", "work",
                                crumbs=crumbs)
                self.assertIn("already registered on othernode", err.getvalue())
                self.assertIn("cluster attach other work", err.getvalue())
                self.assertEqual(asked, [], "must not fetch when crumbs were given")

    def test_a_stale_or_ownerless_record_does_not_block(self):
        """A login's own breadcrumb on another node is stale, not a collision.

        A login is one connection to one node, so a record naming it on a
        different node cannot be live. It is reported as a stale record and
        creation proceeds; "attach to it there" could not be followed. A
        retired or vanished owner does not block either.
        """
        for session, owner, said in (
                ("main", "main", "stale record"),
                ("main", "main", "cluster strays check oldnode"),
                ("work", "main@oldnode", "not a live login"),
                ("work", "deleted-login", "not a live login"),
                ("work", "", "not a live login")):
            with self.subTest(owner=owner, said=said):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self._guard(self._guard_ctx(), "main", session,
                                crumbs={("oldnode", session): owner})
                self.assertIn(said, err.getvalue())

    def test_an_unpinned_login_is_not_told_its_own_node_is_elsewhere(self):
        """`node_of` is pin-only, so an unpinned login's node comes from its
        meta record, and '' is never read as "somewhere else"."""
        from types import SimpleNamespace

        ctx = self._guard_ctx(node="")
        ctx.state = SimpleNamespace(
            known_logins=lambda: ["main"],
            read_meta=lambda _n: {"node": "livenode.x"})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            # Its own node, known only from the meta record: no complaint.
            self._guard(ctx, "main", "work", crumbs={("livenode", "work"): "main"})
        self.assertEqual(err.getvalue(), "")


class TestRemoteDiagnostics(unittest.TestCase):
    def test_remote_formats_emit_real_tabs_not_backslash_t(self):
        """Pin the producer/consumer protocol, not merely the parser.

        A parser test alone would pass while the producer emits literal
        ``\\t`` text, and windows and clients would silently disappear.
        """
        from clustertool.tmuxlayer import details_snippet

        snippet = details_snippet()
        self.assertIn("'#{session_name}\t#{window_index}", snippet)
        self.assertNotIn("'#{session_name}\\t#{window_index}", snippet)
        self.assertIn("'#{client_name}\t#{client_session}", snippet)
        self.assertNotIn("'#{client_name}\\t#{client_session}", snippet)

    def test_sessions_windows_tmux_clients_and_ssh_clients_are_parsed(self):
        from clustertool.tmuxlayer import (
            CLIENTS_MARKER, SESSIONS_MARKER, WHO_MARKER, WINDOWS_MARKER,
            parse_details,
        )

        text = "\n".join([
            "shell banner that must be ignored",
            SESSIONS_MARKER,
            "main\t2\t1\tmain\t\tWed Aug 26 10:00:00 2026",
            WINDOWS_MARKER,
            "main\t0\teditor\t2\t1",
            "main\t1\tserver\t1\t0",
            CLIENTS_MARKER,
            "/dev/pts/4\tmain\t/dev/pts/4",
            WHO_MARKER,
            "user pts/4 2026-08-26 10:01 . 1234 (client.example)",
        ])
        got = parse_details(text)
        self.assertEqual(got.sessions[0].name, "main")
        self.assertEqual(got.sessions[0].created, "Wed Aug 26 10:00:00 2026")
        self.assertEqual([row.name for row in got.windows], ["editor", "server"])
        self.assertTrue(got.windows[0].active)
        self.assertEqual(got.clients[0].tty, "/dev/pts/4")
        self.assertIn("client.example", got.ssh_clients[0])

    def test_where_names_the_node_in_the_same_round_trip(self):
        from types import SimpleNamespace
        from clustertool.tmuxlayer import (
            CLIENTS_MARKER, LS_MARKER, SESSIONS_MARKER, WHO_MARKER,
            WINDOWS_MARKER, Tmux)

        sent, replies = [], []

        def run_remote(_login, command, **_kw):
            sent.append(command)
            return subprocess.CompletedProcess([], 0, replies.pop(0), "")

        tmux = Tmux(SimpleNamespace(backend=None, state=None, run_remote=run_remote))
        replies.append("\n".join([
            "shell banner that must be ignored", LS_MARKER, "login01.example.gov",
            SESSIONS_MARKER, "main\t2\t1\tmain\t\t", WINDOWS_MARKER,
            CLIENTS_MARKER, WHO_MARKER]))
        node, complete, details = tmux.node_and_details_checked("main")
        self.assertEqual(len(sent), 1)
        self.assertEqual((node, complete), ("login01.example.gov", True))
        self.assertEqual([row.name for row in details.sessions], ["main"])
        # Details cut short still name the node; they are not taken as none.
        replies.append(f"{LS_MARKER}\nlogin01.example.gov\n{SESSIONS_MARKER}\n")
        node, complete, details = tmux.node_and_details_checked("main")
        self.assertEqual((node, complete, details.sessions),
                         ("login01.example.gov", False, []))


class TestTmuxCatalogueSafety(unittest.TestCase):
    """A failed remote read must never be treated as an empty tmux server."""

    @staticmethod
    def tmux_with(result):
        from types import SimpleNamespace
        from clustertool.tmuxlayer import Tmux

        logins = SimpleNamespace(
            backend=SimpleNamespace(short=lambda node: node.split(".")[0]),
            state=SimpleNamespace(),
            run_remote=lambda *_a, **_k: result,
        )
        return Tmux(logins)

    def test_only_a_zero_status_and_the_marker_make_an_empty_catalogue(self):
        from subprocess import CompletedProcess
        from clustertool.tmuxlayer import LS_MARKER

        for rc, stdout, complete in (
                (0, LS_MARKER + "\n", True),
                # Even a marker in partial stdout does not overrule the SSH failure.
                (255, LS_MARKER + "\n", False),
                # Truncated success.
                (0, "login banner only\n", False)):
            with self.subTest(rc=rc, stdout=stdout):
                reply = CompletedProcess([], rc, stdout, "lost" if rc else "")
                tmux = self.tmux_with(reply)
                self.assertEqual(tmux.list_sessions_checked("main"), (complete, []))
                # The direct catalogue has the same failure/empty distinction.
                tmux._direct_run = lambda *_a, **_k: reply
                self.assertEqual(tmux.list_sessions_direct_checked("node1"),
                                 (complete, []))

    def test_a_failed_read_neither_prunes_crumbs_nor_confirms_a_kill(self):
        from subprocess import CompletedProcess

        tmux = self.tmux_with(CompletedProcess([], 255, "", "lost"))
        tmux.logins.node_of = lambda _name: "node1.example"
        writes = []
        tmux.crumb_add = lambda *_a, **_k: writes.append("add")
        tmux.crumb_remove = lambda *_a, **_k: writes.append("remove")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(tmux.crumb_sync("main"), (0, 0))
        self.assertFalse(tmux.kill_session("main", "work"))
        self.assertEqual(writes, [])

    def test_a_direct_kill_takes_exact_names_and_confirms_only_their_absence(self):
        from subprocess import CompletedProcess
        from clustertool.tmuxlayer import LS_MARKER

        tmux = self.tmux_with(CompletedProcess([], 0, "", ""))
        for listing, want in (
                # api disappeared, worker is still present after the attempted kills.
                (LS_MARKER + "\nworker\t1\t0\tmain\t\n", (["api"], ["worker"])),
                # A truncated listing confirms nothing.
                ("", ([], ["api", "worker"]))):
            with self.subTest(listing=listing):
                sent = []
                tmux._direct_run = lambda _node, snippet, listing=listing, **_k: (
                    sent.append(snippet) or CompletedProcess([], 0, listing, ""))
                self.assertEqual(tmux.kill_sessions_direct("node1", ["api", "worker"]),
                                 want)
        # A stray's session may well be gone; a bare `-t api` would then
        # kill a surviving `api2` by prefix.
        self.assertIn("kill-session -t =api: ", sent[0])


class TestCloseDisposition(unittest.TestCase):
    class Backend:
        name = "fake"

        @staticmethod
        def short(node):
            return node.split(".")[0] if node else ""

    class State:
        def __init__(self):
            self.records = []
            self.dropped = []

        def pin_read(self, _name):
            return "node1.example"

        def abandon_record(self, node, session, former):
            self.records.append((node, session, former))

        def drop_meta(self, name):
            self.dropped.append(name)

    class Tmux:
        def __init__(self):
            self.kills = 0
            self.retags = []
            self.catalogue_complete = True

        def owned_sessions_checked(self, _name):
            return self.catalogue_complete, (["api", "worker"]
                                             if self.catalogue_complete else [])

        def kill_owned_checked(self, _name):
            if not self.catalogue_complete:
                return False, [], []
            self.kills += 1
            return True, ["api", "worker"], []

        def crumb_retag_node(self, executor, node, old, new):
            self.retags.append((executor, node, old, new))

    def context(self):
        from types import SimpleNamespace

        state = self.State()
        tmux = self.Tmux()
        closed = []
        logins = SimpleNamespace(
            is_active=lambda _name: True,
            node_of=lambda _name: "node1.example",
            live_node=lambda _name: "node1.example",
            active_names=lambda: ["main"],
            close=lambda name, keep_tmux, keep_pin: closed.append(
                (name, keep_tmux, keep_pin)),
        )
        mounts = SimpleNamespace(
            stop_watcher=lambda *_a, **_k: None,
            unmount=lambda *_a, **_k: None,
        )
        ctx = SimpleNamespace(
            backend=self.Backend(), state=state, tmux=tmux, logins=logins,
            mounts=mounts, login=lambda name=None: name or "main",
        )
        return ctx, state, tmux, closed

    def test_abandon_leaves_sessions_records_them_and_releases_pin(self):
        from clustertool.commands.sessions import cmd_close

        ctx, state, tmux, closed = self.context()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cmd_close(ctx, ["main", "--abandon-tmux"]), 0)
        self.assertEqual(tmux.kills, 0)
        self.assertEqual(state.records,
                         [("node1", "api", "main"),
                          ("node1", "worker", "main")])
        self.assertEqual(closed, [("main", True, False)])
        self.assertEqual(state.dropped, ["main"])
        self.assertEqual(tmux.retags,
                         [("main", "node1", "main", "main@node1")])

    def test_the_pin_is_kept_whenever_the_sessions_cannot_be_catalogued(self):
        from clustertool.commands.sessions import cmd_close

        for args, active, complete in (
                (["main", "--abandon-tmux"], False, True),
                (["main", "--abandon-tmux"], True, False),
                (["main"], True, False)):
            with self.subTest(args=args, active=active):
                ctx, state, tmux, closed = self.context()
                if not active:
                    ctx.logins.is_active = lambda _name: False
                    ctx.logins.active_names = lambda: []
                tmux.catalogue_complete = complete
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertEqual(cmd_close(ctx, args), 1)
                self.assertIn("keeping the pin", err.getvalue())
                self.assertEqual((tmux.kills, state.records, state.dropped), (0, [], []))
                self.assertEqual(closed, [("main", "--abandon-tmux" in args, True)])

    def test_keep_and_abandon_are_rejected_as_contradictory(self):
        from clustertool.commands.sessions import cmd_close

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cmd_close(None, ["main", "--keep-tmux", "--abandon-tmux"])

    def test_default_close_kills_owned_sessions(self):
        from clustertool.commands.sessions import cmd_close

        ctx, state, tmux, closed = self.context()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cmd_close(ctx, ["main"]), 0)
        self.assertEqual(tmux.kills, 1)
        self.assertEqual(state.records, [])
        self.assertEqual(closed, [("main", False, False)])


class _TmuxOverState(unittest.TestCase):
    """A Tmux over a sandboxed state tree, whose remote commands get the
    replies it is given in turn, the last one for every call after it."""

    def setUp(self):
        from clustertool.state import State

        temp_state(self)
        self.state = State(load("nersc"))
        self.sent = []

    def tmux(self, *replies, value=""):
        from types import SimpleNamespace
        from clustertool.tmuxlayer import Tmux

        queue = list(replies)

        def run_remote(_login, snippet, **_kw):
            self.sent.append(snippet)
            return queue.pop(0) if len(queue) > 1 else queue[0]

        def remote_value(_login, snippet, **_kw):
            self.sent.append(snippet)
            return value

        return Tmux(SimpleNamespace(
            backend=self.state.backend, state=self.state,
            node_of=lambda _name: "login01.example.gov",
            live_node=lambda _name: "login01.example.gov",
            command_timeout=lambda own_connection=False: 60,
            run_remote=run_remote, remote_value=remote_value))

    @staticmethod
    def proc(returncode, stdout="", stderr=""):
        from subprocess import CompletedProcess

        return CompletedProcess([], returncode, stdout, stderr)

    @staticmethod
    def steps(*failed):
        """What a create reports of its owner and crumb steps."""
        from clustertool.tmuxlayer import STEP_MARKER

        return "".join(f"{STEP_MARKER}\t{step}\t{1 if step in failed else 0}\n"
                       for step in ("owner", "crumb"))

    def names(self):
        return self.state.read_completion_sessions()


class TestSessionCacheHooks(_TmuxOverState):
    """Only a confirmed remote answer may change the local session cache."""

    def test_a_create_is_recorded_and_a_confirmed_kill_forgets_it(self):
        from clustertool.tmuxlayer import LS_MARKER, STEP_MARKER

        tmux = self.tmux(self.proc(
            0, self.steps() + f"{STEP_MARKER}\twork\n{LS_MARKER}\n"))
        for kill, confirmed in ((tmux.kill_session, True),
                                (tmux.kill_session_checked, (True, True, []))):
            with self.subTest(kill=kill.__name__):
                with contextlib.redirect_stderr(io.StringIO()) as said:
                    tmux.create("main", "work")
                self.assertEqual(self.names(), {"main": ["work"]})
                self.assertEqual(said.getvalue(), "")
                self.assertEqual(kill("main", "work"), confirmed)
                self.assertEqual(self.names(), {})

    def test_an_unconfirmed_kill_or_a_failed_create_changes_nothing(self):
        from clustertool.tmuxlayer import LS_MARKER

        failed = self.tmux(self.proc(1, "", "no server running"))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            failed.create("main", "work")
        self.assertEqual(self.names(), {})
        self.tmux(self.proc(0, self.steps() + LS_MARKER + "\n")).create("main", "work")
        # The kill went out but the catalogue could not be read back, so the
        # session may well still be there: the record has to stay.
        lost = self.tmux(self.proc(255, "", "lost connection"))
        self.assertFalse(lost.kill_session("main", "work"))
        self.assertEqual(self.names(), {"main": ["work"]})

    def test_a_prefix_is_not_a_kill(self):
        # tmux reads a bare `-t NAME` as a unique prefix, so kills target
        # `=NAME:`; a name that matches nothing exactly kills nothing and
        # changes no record.
        from clustertool.tmuxlayer import LS_MARKER

        self.tmux(self.proc(0, self.steps())).create("main", "zzwork")
        sent = []
        tmux = self.tmux(None)

        def run_remote(_login, snippet, **_kw):
            sent.append(snippet)
            return self.proc(0, LS_MARKER + "\nzzwork\t1\t0\tmain\t\n")

        tmux.logins.run_remote = run_remote
        self.assertEqual(tmux.kill_session_checked("main", "zz"),
                         (True, False, ["zzwork"]))
        self.assertIn("kill-session -t =zz: ", sent[0])
        self.assertIn(LS_MARKER, sent[0], "the kill and its confirmation are "
                                          "one round trip")
        self.assertEqual(self.names(), {"main": ["zzwork"]})

    def test_only_a_positive_answer_records_a_session(self):
        from clustertool.tmuxlayer import STEP_MARKER

        def steps(server):
            return "\n".join(f"{STEP_MARKER}\t{step}\t{status}" for step, status in
                              (("server", server), ("owner", 0), ("crumb", 0)))

        # register_session records only when the server step succeeded.
        failed = self.tmux(self.proc(0), value=steps(1))
        self.assertFalse(failed.register_session("main", "work")["server"])
        self.assertEqual(self.names(), {})
        ok = self.tmux(self.proc(0), value=steps(0))
        self.assertTrue(ok.register_session("main", "work")["server"])
        self.assertEqual(self.names(), {"main": ["work"]})

        row = "\t".join(["other", "1", "0", "main", "", "Mon Jan  1 00:00:00 2026"])
        found = self.tmux(self.proc(0), value=row + "\n")
        self.assertTrue(found.session_exists("main", "other"))
        self.assertEqual(self.names(), {"main": ["other", "work"]})
        # An unchecked listing cannot tell a failure from an empty server, so
        # only a positive answer is evidence: nothing is added or removed here.
        lost = self.tmux(self.proc(255, "", "lost connection"))
        self.assertFalse(lost.session_exists("main", "third"))
        self.assertEqual(self.names(), {"main": ["other", "work"]})


class TestCreatingASession(_TmuxOverState):
    """A session this tool creates is tagged and recorded in the same command,
    and one whose answer was lost is looked for before anything is said."""

    @staticmethod
    def listing(*rows):
        from clustertool.tmuxlayer import LS_MARKER

        return LS_MARKER + "\n" + "".join(row + "\n" for row in rows)

    LOST = (255, "", "Connection to login01 closed by remote host.")

    def create(self, tmux):
        with contextlib.redirect_stderr(io.StringIO()) as said:
            try:
                got = tmux.create("main", "work", command="python train.py")
            except SystemExit:
                got = None
        return got, said.getvalue()

    def test_the_tag_and_the_breadcrumb_ride_the_create(self):
        got, said = self.create(self.tmux(self.proc(0, self.steps())))
        self.assertTrue(got)
        self.assertEqual(said, "")
        self.assertEqual(len(self.sent), 1, "one round trip")
        command = self.sent[0]
        self.assertLess(command.index("new-session"), command.index("set-option"))
        self.assertLess(command.index("set-option"), command.index("mkdir -p"))
        self.assertIn("@cluster_login main", command)

    def test_a_step_that_failed_is_said(self):
        _got, said = self.create(self.tmux(self.proc(0, self.steps("crumb"))))
        self.assertIn("could not record the session breadcrumb for 'work' on main",
                      said)
        self.assertNotIn("ownership", said)

    def test_a_lost_answer_with_the_session_there_is_a_create(self):
        tmux = self.tmux(self.proc(*self.LOST),
                         self.proc(0, self.listing("work\t1\t0\tmain\t\t")),
                         value=self.steps())
        got, said = self.create(tmux)
        self.assertTrue(got)
        self.assertIn("the answer to creating session 'work' was lost", said)
        self.assertIn("closed by remote host", said)
        self.assertIn("mkdir -p", self.sent[-1], "its breadcrumb is written again")
        self.assertEqual(self.names(), {"main": ["work"]})

    def test_a_lost_answer_is_otherwise_a_failure_that_says_why(self):
        for reply, listing, says, unsaid in (
                (self.LOST, self.listing(), ["could not create session 'work'"],
                 "may still have reached"),
                # The look itself failed: where to look is said.
                ((124,), None, ["it timed out", "may still have reached login01",
                                "cluster sessions main"], None),
                # Made by hand, it would be killed by `close` once tagged as
                # the login's, so it is not claimed on no evidence.
                (self.LOST, self.listing("work\t1\t0\t\t\t"),
                 ["no ownership tag", "cluster attach main work"], None),
                (self.LOST, self.listing("work\t1\t0\tother\t\t"),
                 ["belongs to 'other'"], None)):
            with self.subTest(says=says[0]):
                del self.sent[:]
                look = self.proc(*self.LOST) if listing is None else self.proc(0, listing)
                got, said = self.create(self.tmux(self.proc(*reply), look))
                self.assertIsNone(got)
                for text in says:
                    self.assertIn(text, said)
                if unsaid:
                    self.assertNotIn(unsaid, said)
                self.assertEqual(len(self.sent), 2, "nothing was written to it")
                self.assertEqual(self.names(), {})

    def new_session(self, tmux, *args):
        """`cluster new-session -d --no-mount ARGS` over *tmux*: (rc, said)."""
        from types import SimpleNamespace
        from clustertool.commands import sessions

        ctx = SimpleNamespace(
            login=lambda name=None: name, backend=self.state.backend, tmux=tmux,
            logins=SimpleNamespace(ensure=lambda _name: None,
                                   node_of=lambda _name: "login01.example.gov"),
            state=SimpleNamespace(known_logins=lambda: ["main", "other"],
                                  read_meta=lambda _name: {}))
        with contextlib.redirect_stderr(io.StringIO()) as said, \
                support._patched(sessions.plat, "set_process_name", lambda _n: None):
            try:
                rc = sessions.cmd_task(ctx, ["-d", "--no-mount", *args])
            except SystemExit as exc:
                rc = exc.code
        return rc, said.getvalue()

    @staticmethod
    def catalogue(sessions=(), crumbs=()):
        from clustertool.tmuxlayer import CRUMBS_MARKER, SESSIONS_MARKER

        return "\n".join([SESSIONS_MARKER, *sessions, CRUMBS_MARKER, *crumbs])

    def test_new_session_asks_both_its_questions_in_one_round_trip(self):
        from clustertool.tmuxlayer import crumbs_snippet

        for here in ((), ("--here",)):
            with self.subTest(here=here):
                del self.sent[:]
                tmux = self.tmux(self.proc(0, self.steps()), value=self.catalogue())
                rc, said = self.new_session(tmux, *here, "main", "work")
                self.assertEqual(rc, 0, said)
                self.assertEqual(len(self.sent), 2, "one read, then the create")
                self.assertIn("list-sessions", self.sent[0])
                # --here reads no crumbs.
                self.assertEqual(crumbs_snippet() in self.sent[0], not here)
                self.assertIn("new-session", self.sent[1])

    def test_new_session_decides_from_that_read_and_creates_nothing_more(self):
        for catalogue, rc, says, names in (
                (self.catalogue(crumbs=["login31\twork\tother"]), 1,
                 "already registered on login31", {}),
                (self.catalogue(sessions=["work\t1\t0\tmain\t\t"]), 0, "",
                 {"main": ["work"]})):
            with self.subTest(says=says):
                del self.sent[:]
                got, said = self.new_session(self.tmux(value=catalogue), "main", "work")
                self.assertEqual(got, rc, said)
                self.assertIn(says, said)
                self.assertEqual(len(self.sent), 1, "nothing created")
                self.assertEqual(self.names(), names)


class TestKillSessionCommand(unittest.TestCase):
    """`cluster k` must say a kill happened only when one did."""

    def run_kill(self, outcome, args=("main", "zz")):
        from types import SimpleNamespace
        from clustertool.commands.sessions import cmd_kill_session

        ctx = SimpleNamespace(
            login=lambda name=None: name or "main",
            logins=SimpleNamespace(ensure=lambda _name: None),
            tmux=SimpleNamespace(kill_session_checked=lambda *_a: outcome),
        )
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                rc = cmd_kill_session(ctx, list(args))
            except SystemExit as exc:
                rc = exc.code
        return rc, err.getvalue()

    def test_a_kill_is_said_only_when_one_happened(self):
        for outcome, name, rc, says in (
                ((True, True, ["other"]), "api", 0, ["killed session 'api' on main"]),
                ((True, False, ["other", "zzwork"]), "zz", 1,
                 ["no session 'zz' on main; nothing was killed",
                  "did you mean: cluster kill-session main zzwork"]),
                # A name near nothing lists what is there.
                ((True, False, ["worker", "api"]), "q", 1, ["sessions there: api, worker"]),
                ((True, False, []), "q", 1, ["main has no sessions"]),
                ((False, True, []), "zz", 1,
                 ["could not confirm session 'zz' was killed"])):
            with self.subTest(outcome=outcome):
                got, err = self.run_kill(outcome, ("main", name))
                self.assertEqual(got, rc)
                for text in says:
                    self.assertIn(text, err)
                if rc:
                    self.assertNotIn("killed session", err)


class TestRunAndSend(unittest.TestCase):
    """What `run` and `send` take from the words they are given."""

    def ctx(self, known=("work",)):
        from types import SimpleNamespace

        self.ran, self.sent = [], []
        return SimpleNamespace(
            resolve_login=lambda head: ((head[0] if head else "main"), head[1:]),
            login=lambda name=None: name or "main",
            logins=SimpleNamespace(
                ensure=lambda _name: None,
                run_remote=lambda name, line, **_kw: (
                    self.ran.append((name, line)),
                    SimpleNamespace(returncode=3))[1]),
            tmux=SimpleNamespace(
                send=lambda login, target, line: self.sent.append(
                    (login, target, line)) or True),
        ), known

    def run_cmd(self, args, known=("work",)):
        from clustertool import registry
        from clustertool.commands.sessions import cmd_run

        ctx, known = self.ctx(known)
        saved = registry.find
        registry.find = lambda name: "fasrc" if name in known else None
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                rc = cmd_run(ctx, list(args))
        except SystemExit as exc:
            rc = exc.code
        finally:
            registry.find = saved
        return rc, err.getvalue()

    def test_a_login_comes_first_only_if_it_exists_or_a_double_dash_follows(self):
        for args, rc, ran in (
                (["work", "hostname", "-f"], 3, [("work", "hostname -f")]),
                (["gpu", "--", "nvidia-smi"], 3, [("gpu", "nvidia-smi")]),
                # A bare double dash means the default login.
                (["--", "echo", "two words"], 3, [("main", "echo 'two words'")]),
                # A command word is never made into a login.
                (["hostname", "-f"], 1, [])):
            with self.subTest(args=args):
                got, err = self.run_cmd(args)
                self.assertEqual((got, self.ran), (rc, ran))
        self.assertIn("no login named 'hostname'", err)
        self.assertIn("cluster run -- hostname -f", err)

    def test_nothing_to_run_is_refused(self):
        for args in ([], ["work"], ["work", "--"]):
            with self.subTest(args=args):
                rc, err = self.run_cmd(args)
                self.assertEqual(rc, 1)
                self.assertIn("nothing to run", err)

    def send(self, args):
        from clustertool.commands.sessions import cmd_send

        ctx, _known = self.ctx()
        self.assertEqual(cmd_send(ctx, list(args)), 0)
        return self.sent[-1]

    def test_one_argument_is_typed_as_given_and_several_keep_their_boundaries(self):
        self.assertEqual(self.send(["api", "--", "make && make test"]),
                         ("main", "api", "make && make test"))
        self.assertEqual(self.send(["work", "api", "--", "grep", "-r", "two words", "."]),
                         ("work", "api", "grep -r 'two words' ."))


class TestLossRecovery(unittest.TestCase):
    """What is left after a login node has killed the work.

    Two records survive a loss and they do not hold the same thing: the layout
    snapshot has sessions, windows and cwds but is only as fresh as the
    watcher's last save, while the breadcrumb is written in the same round trip
    that creates the session and holds nothing but its name. A snapshot can
    hold `train, eval, notes` while the crumbs hold `build, notes`: neither is
    a superset, so restoring reads both.
    """

    def _ctx(self, snapshot, crumbs, existing=()):
        from types import SimpleNamespace

        self.created = []
        self.resolved = []
        return SimpleNamespace(
            backend=SimpleNamespace(
                short=lambda n: (n or "").split(".")[0],
                fqdn=lambda n: f"{n}.rc" if "." not in n else n),
            logins=SimpleNamespace(ensure=lambda _name: True),
            login=lambda name=None: self.resolved.append(name) or name or "main",
            tmux=SimpleNamespace(
                layout_read=lambda _login, _node: list(snapshot),
                crumbs=lambda _login: dict(crumbs),
                list_sessions=lambda _login: [
                    SimpleNamespace(name=n) for n in existing],
                create=lambda login, session, cwd=None: (
                    self.created.append(session), True)[1],
                new_window=lambda *a, **kw: True,
            ),
        )

    def _run(self, ctx, args):
        from clustertool.commands.sessions import cmd_restore_layout

        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = cmd_restore_layout(ctx, args)
        return rc, out.getvalue()

    def test_a_session_in_no_snapshot_is_still_restored_by_name(self):
        # A session created and killed between two watcher ticks is in no
        # snapshot, but its crumb was written by the create itself. Names are
        # the most that survives either way, since processes are never
        # restarted.
        ctx = self._ctx(
            snapshot=[{"session": "kept", "window": 0, "path": "/n", "name": ""}],
            crumbs={("holylogin06", "crumbed"): "main",
                    ("holylogin06", "kept"): "main"})
        rc, printed = self._run(ctx, ["main", "holylogin06"])
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(self.created), ["crumbed", "kept"])
        self.assertIn("known only from breadcrumbs", printed)
        self.assertIn("crumbed", printed)

    def test_another_node_another_login_or_running_work_is_not_restored_here(self):
        for snapshot, crumbs, existing, created in (
                ([{"session": "a", "window": 0, "path": "", "name": ""}],
                 {("holylogin07", "elsewhere"): "main",
                  ("holylogin06", "someone-else"): "other"}, (), ["a"]),
                # Restoring must never disturb work that is there.
                ([], {("holylogin06", "alive"): "main"}, ("alive",), [])):
            with self.subTest(created=created):
                self._run(self._ctx(snapshot, crumbs, existing), ["main", "holylogin06"])
                self.assertEqual(self.created, created)

    def test_nothing_recorded_at_all_says_so(self):
        ctx = self._ctx(snapshot=[], crumbs={})
        with self.assertRaises(ui.Die):
            self._run(ctx, ["main", "holylogin06"])

    def test_a_named_login_is_resolved_before_the_node(self):
        # Resolving the login binds the backend that owns it, and only that
        # backend can say what the node's short name stands for.
        from types import SimpleNamespace

        ctx = self._ctx(snapshot=[], crumbs={("holylogin06", "kept"): "work"})
        order = []
        login = ctx.login
        ctx.login = lambda name=None: order.append("login") or login(name)
        ctx.backend.fqdn = lambda n: order.append("fqdn") or n
        ctx.logins = SimpleNamespace(ensure=lambda _name: True)
        self._run(ctx, ["work", "holylogin06"])
        self.assertEqual(order, ["login", "fqdn"])
        self.assertEqual(self.resolved, ["work"])
        self.assertEqual(self.created, ["kept"])


class TestCrumbRemovalTidiesTheNodeDirectory(unittest.TestCase):
    def test_removing_the_last_crumb_removes_the_node_directory(self):
        """Otherwise `~/.cluster/sessions/` keeps a directory for every node
        that ever held a session, implying state that is not there."""
        from clustertool.backends import load
        from clustertool.sshmux import Logins
        from clustertool.tmuxlayer import Tmux

        tmux = Tmux(Logins(load("fasrc")))
        sent = []
        tmux._run = lambda login, snippet, timeout=60: (
            sent.append(snippet) or subprocess.CompletedProcess([], 0, "", ""))
        self.assertTrue(tmux.crumb_remove("main", "sim", node="boslogin08"))
        self.assertIn("rm -f ~/.cluster/sessions/boslogin08/sim", sent[0])
        self.assertIn("rmdir ~/.cluster/sessions/boslogin08", sent[0])
        # rmdir must never be able to fail the removal it follows.
        self.assertIn("|| true", sent[0])


class TestTmuxTargets(unittest.TestCase):
    """The ``-t`` operands every remote tmux command is built from."""

    def test_a_target_is_the_exact_session_and_a_home_path_is_spelled_from_home(self):
        from clustertool.remote_sh import home_path, split_target, tmux_target

        for got, want in (
                (tmux_target("api"), "=api:"), (tmux_target("api", 1), "=api:1"),
                (tmux_target("api", "logs", 0), "=api:logs.0"),
                (tmux_target("my work"), "'=my work:'"),
                (split_target("api"), "=api:"), (split_target("api:2"), "=api:2"),
                (split_target("api:2.1"), "=api:2.1"),
                (home_path("~"), '"$HOME"'), (home_path(""), '"$HOME"'),
                (home_path("~/"), '"$HOME"'), (home_path("~/a b"), '"$HOME"/\'a b\''),
                (home_path("/n/x"), "/n/x")):
            self.assertEqual(got, want)

    def test_an_option_name_is_checked_not_quoted(self):
        from clustertool.remote_sh import quote_opt

        self.assertEqual(quote_opt("@owner"), "@owner")
        self.assertEqual(quote_opt("status-left"), "status-left")
        for bad in ("", "@", "@a b", "@a;rm", "$(x)", "-t"):
            with self.subTest(option=bad):
                with self.assertRaises(ValueError):
                    quote_opt(bad)

    def test_a_bad_foreign_option_is_dropped_with_one_warning(self):
        from clustertool import tmuxlayer

        err = io.StringIO()
        with support._patched(tmuxlayer, "FOREIGN_OWNER_OPTIONS",
                              ("@agent", "@a;b")), \
                support._patched(tmuxlayer, "_warned_options", set()), \
                contextlib.redirect_stderr(err):
            first = tmuxlayer.list_sessions_snippet()
            tmuxlayer.list_sessions_snippet()
        self.assertIn("@agent", first)
        self.assertNotIn("@a;b", first)
        self.assertEqual(err.getvalue().count("'@a;b'"), 1)

    def test_each_session_is_one_word_however_it_is_spelled(self):
        from clustertool.remote_sh import for_each_session

        tmux = Path(support.SANDBOX) / "fake-bin"
        tmux.mkdir(exist_ok=True)
        (tmux / "tmux").write_text("#!/bin/sh\nprintf '%s\\n' 'my work' '-n' 'a*'\n")
        (tmux / "tmux").chmod(0o755)
        env = dict(os.environ, PATH=f"{tmux}{os.pathsep}{os.environ['PATH']}")
        out = subprocess.run(
            ["sh", "-c", for_each_session('printf "[%s]" "$s"')], env=env,
            stdout=subprocess.PIPE, universal_newlines=True, check=True).stdout
        self.assertEqual(out, "[my work][-n][a*]")


class _LocalShell:
    """A Logins stand-in whose cluster is ``sh`` on this machine."""

    def __init__(self, env):
        from types import SimpleNamespace

        self.env = env
        self.backend = SimpleNamespace(
            name="fake", reaps_on_logout=False,
            short=lambda node: (node or "").split(".")[0])
        self.state = SimpleNamespace(note_sessions=lambda *_a, **_k: None)
        self.settings = SimpleNamespace(flag=lambda _key: False)

    @staticmethod
    def node_of(_login):
        return "node1.example"

    def run_remote(self, _login, snippet, timeout=60, capture=True, idle=None):
        return subprocess.run(["sh", "-c", snippet], env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=timeout or idle)

    @staticmethod
    def command_timeout(own_connection=False):
        return 60

    def remote_value(self, login, snippet, timeout=60):
        proc = self.run_remote(login, snippet, timeout)
        return "" if proc.returncode else proc.stdout.strip()


@unittest.skipUnless(shutil.which("tmux"), "needs tmux")
class TestExactTargetsOnRealTmux(unittest.TestCase):
    """The remote commands, run against a private tmux server on this machine.

    Every session named here has a neighbour its name is a prefix of, which a
    bare ``-t api`` resolves to when ``api`` itself is missing.
    """

    def setUp(self):
        import tempfile
        from clustertool.tmuxlayer import Tmux

        root = tempfile.mkdtemp(prefix="tx", dir=str(support.SANDBOX))
        self.addCleanup(shutil.rmtree, root, True)
        self.home = Path(root) / "home"
        self.home.mkdir()
        self.env = dict(os.environ, HOME=str(self.home),
                        TMUX_TMPDIR=str(support.short_dir(self, "tx")))
        self.addCleanup(self.tmux, "kill-server")
        self.layer = Tmux(_LocalShell(self.env))

    def tmux(self, *args):
        return subprocess.run(["tmux"] + list(args), env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True)

    def start(self, *names):
        for name in names:
            self.assertEqual(self.tmux("new-session", "-d", "-s", name, "sh")
                             .returncode, 0, name)

    def names(self):
        return set(self.tmux("list-sessions", "-F", "#{session_name}")
                   .stdout.splitlines())

    def crumb(self, node, session, owner="main"):
        where = self.home / ".cluster" / "sessions" / node
        where.mkdir(parents=True, exist_ok=True)
        (where / session).write_text(owner + "\n")
        return where / session

    def test_a_kill_takes_only_the_exact_name_and_drops_only_its_crumbs(self):
        self.start("api", "api2", "worker")
        records = {name: self.crumb("node1", name) for name in ("api", "api2", "worker")}
        self.assertEqual(self.layer.kill_session_checked("main", "api"),
                         (True, True, ["api2", "worker"]))
        self.assertEqual(self.layer.kill_session_checked("main", "api"),
                         (True, False, ["api2", "worker"]),
                         "nothing by that name is gone, but nothing was killed")
        self.assertEqual(self.layer.kill_sessions("main", ["worker", "ap"]),
                         (["worker", "ap"], []))
        self.assertEqual(self.names(), {"api2"})
        self.assertEqual([name for name, path in records.items() if path.exists()],
                         ["api2"])

    def test_going_back_after_a_drop_never_makes_a_session(self):
        # Its neighbour api2 is no stand-in for it, and nothing makes a new,
        # empty api where the old one ended.
        self.start("api2")
        proc = subprocess.run(["sh", "-c", self.layer.reattach_argv("api")],
                              env=self.env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("session 'api' ended while the connection was down", proc.stderr)
        self.assertEqual(self.names(), {"api2"})

        self.start("api")
        proc = subprocess.run(["sh", "-c", self.layer.reattach_argv("api")],
                              env=self.env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True)
        self.assertNotIn("ended while", proc.stderr, "it went on to attach")

    def test_tags_are_read_and_written_on_the_exact_session(self):
        from clustertool.tmuxlayer import OWNER_OPTION

        self.start("api", "api2")
        self.assertTrue(self.layer.tag_owner("main", "api"))
        owners = {row.name: (row.owner, row.tagged)
                  for row in self.layer.list_sessions("main")}
        self.assertEqual(owners, {"api": ("main", True), "api2": ("api2", False)})
        self.tmux("kill-session", "-t", "=api")
        self.layer.tag_owner("main", "api", owner="other", force=True)
        self.assertEqual(self.tmux("show-options", "-qv", "-t", "=api2:",
                                   OWNER_OPTION).stdout.strip(), "")

    def test_keys_windows_and_renames_go_only_to_the_named_session(self):
        self.layer._direct_run = (lambda _node, snippet, timeout=120:
                                  self.layer._run("main", snippet, timeout))
        self.start("api2")
        self.assertFalse(self.layer.send("main", "api", "true"))
        self.assertFalse(self.layer.send("main", "api:0", "true"))
        self.assertFalse(self.layer.rename_session_direct("node1", "api", "web"))
        self.assertEqual(self.names(), {"api2"})
        self.start("api")
        self.assertTrue(self.layer.send("main", "api", "true"))
        self.assertTrue(self.layer.send("main", "api:0.0", "true"))
        self.assertTrue(self.layer.new_window("main", "api", "logs"))
        windows = self.tmux("list-windows", "-a", "-F",
                            "#{session_name} #{window_name}").stdout
        self.assertIn("api logs", windows)
        self.assertNotIn("api2 logs", windows)
        self.assertTrue(self.layer.rename_session_direct("node1", "api", "web"))
        self.assertEqual(self.names(), {"api2", "web"})

    def test_a_retag_moves_sessions_and_crumbs_whatever_their_names(self):
        from clustertool.tmuxlayer import OWNER_OPTION

        host = subprocess.run(["hostname", "-s"], stdout=subprocess.PIPE,
                              universal_newlines=True).stdout.strip()
        self.start("my work", "api", "api2")
        self.tmux("set-option", "-t", "=my work:", OWNER_OPTION, "old")
        self.tmux("set-option", "-t", "=api2:", OWNER_OPTION, "someone")
        # Untagged, but the breadcrumb says it is old's.
        self.crumb(host, "api", owner="old")
        elsewhere = self.crumb("node9", "sim", owner="old")
        self.assertTrue(self.layer.retag_owner("main", "old", "new"))
        owners = {row.name: row.owner for row in self.layer.list_sessions("main")}
        self.assertEqual(owners, {"my work": "new", "api": "new", "api2": "someone"})
        from clustertool import workstation

        self.assertEqual(elsewhere.read_text(), f"new\tws={workstation.ident()}\n")

    def test_another_workstations_sessions_are_listed_but_never_touched(self):
        from clustertool import workstation
        from clustertool.tmuxlayer import OWNER_OPTION, WS_OPTION

        host = subprocess.run(["hostname", "-s"], stdout=subprocess.PIPE,
                              universal_newlines=True).stdout.strip()
        self.start("theirs", "mine", "legacy")
        for name in ("theirs", "mine", "legacy"):
            self.tmux("set-option", "-t", f"={name}:", OWNER_OPTION, "old")
        self.tmux("set-option", "-t", "=theirs:", WS_OPTION, "laptop-0001")
        self.tmux("set-option", "-t", "=mine:", WS_OPTION, workstation.ident())
        # The other machine's record, as it writes one; an older version
        # reads only the owner before the tab.
        theirs = self.crumb(host, "theirs", owner="old\tws=laptop-0001")
        rows = {row.name: row for row in self.layer.list_sessions("main")}
        self.assertEqual(rows["theirs"].foreign, "workstation laptop-0001")
        self.assertEqual(rows["mine"].foreign, "")
        self.assertEqual(rows["legacy"].foreign, "")
        # A rename here does not move what the other machine owns...
        self.assertTrue(self.layer.retag_owner("main", "old", "new"))
        owners = {row.name: row.owner for row in self.layer.list_sessions("main")}
        self.assertEqual(owners, {"theirs": "old", "mine": "new", "legacy": "new"})
        self.assertEqual(theirs.read_text(), "old\tws=laptop-0001\n")
        # ...and a kill, however it was chosen, does not take it.
        killed, failed = self.layer.kill_sessions("main", ["theirs", "legacy"])
        self.assertEqual((killed, failed), (["legacy"], ["theirs"]))
        self.assertEqual(self.names(), {"theirs", "mine"})

    def test_a_breadcrumb_says_which_workstation_wrote_it(self):
        from clustertool import tmuxlayer, workstation

        self.start("api")
        self.assertTrue(self.layer.crumb_add("main", "api", node="node1"))
        record = self.home / ".cluster" / "sessions" / "node1" / "api"
        self.assertEqual(record.read_text(), f"main\tws={workstation.ident()}\n")
        crumbs = self.layer.crumbs("main")
        self.assertEqual(crumbs[("node1", "api")], "main")
        self.assertEqual(crumbs[("node1", "api")].workstation, workstation.ident())
        # A record from before workstations has none, and reads as before.
        self.crumb("node1", "old")
        self.assertEqual(self.layer.crumbs("main")[("node1", "old")].workstation, "")
        self.assertEqual(tmuxlayer.parse_crumbs("n\ts\tmain\n")[("n", "s")], "main")


class TestRefreshSettlesTheNodeItLeaves(unittest.TestCase):
    def test_a_disconnected_login_settles_its_old_node_in_the_kill(self):
        """With no connection there is no channel to settle over, so the one
        direct command that kills the old sessions settles the node too."""
        from types import SimpleNamespace
        from clustertool.lifecycle import refresh_login

        calls = []
        ctx = SimpleNamespace(
            backend=SimpleNamespace(short=lambda n: (n or "").split(".")[0],
                                    node_candidates_for=lambda _n, avoid: []),
            state=SimpleNamespace(pin_read=lambda _n: "holylogin05.rc",
                                  ledger_remove=lambda node: calls.append(
                                      ("forget node", node))),
            # A namespace, so a separate settle_node would be an AttributeError.
            tmux=SimpleNamespace(
                owned_sessions_direct_checked=lambda node, owner: (True, ["api"]),
                kill_sessions_direct=lambda node, names, settle=False: (
                    calls.append(("kill", node, tuple(names), settle))
                    or (list(names), []))),
            logins=SimpleNamespace(
                is_active=lambda _n: False,
                close=lambda *_a, **_k: None,
                ensure=lambda _n, preferred: None,
                node_of=lambda _n: "holylogin07.rc"),
            mounts=SimpleNamespace(is_mounted=lambda _n: False,
                                   watcher_running=lambda _n: False,
                                   stop_watcher=lambda *_a, **_k: None,
                                   unmount=lambda *_a, **_k: None),
            settings=SimpleNamespace(int=lambda _k: 1),
        )
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(refresh_login(ctx, "main"), 0)
        self.assertEqual(calls, [("kill", "holylogin05.rc", ("api",), True),
                                 ("forget node", "holylogin05.rc")])


class TestRefreshThatCannotReconnect(unittest.TestCase):
    """A refresh that closed the login and could not bring it back leaves it
    watched, if it was: the watcher is what reconnects it later."""

    def refresh(self, watched):
        from types import SimpleNamespace
        from clustertool import ui
        from clustertool.lifecycle import refresh_login

        started = []

        def ensure(_name, preferred):
            ui.die("could not open login 'main' on any FASRC node")

        ctx = SimpleNamespace(
            backend=SimpleNamespace(short=lambda n: (n or "").split(".")[0],
                                    node_candidates_for=lambda _n, avoid: []),
            state=SimpleNamespace(pin_read=lambda _n: "", ledger_remove=None),
            logins=SimpleNamespace(is_active=lambda _n: False,
                                   close=lambda *_a, **_k: None, ensure=ensure),
            mounts=SimpleNamespace(is_mounted=lambda _n: False,
                                   watcher_running=lambda _n: watched,
                                   stop_watcher=lambda *_a, **_k: None,
                                   unmount=lambda *_a, **_k: None,
                                   start_watcher=started.append),
            settings=SimpleNamespace(int=lambda _k: 2),
        )
        with contextlib.redirect_stderr(io.StringIO()) as said, \
                self.assertRaises(SystemExit):
            refresh_login(ctx, "main")
        self.assertIn("could not open login 'main'", said.getvalue())
        return started

    def test_a_watched_login_is_watched_again_and_only_that(self):
        for watched, started in ((True, ["main"]), (False, [])):
            with self.subTest(watched=watched):
                self.assertEqual(self.refresh(watched), started)


if __name__ == "__main__":
    unittest.main(verbosity=2)
