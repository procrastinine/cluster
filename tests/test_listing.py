#!/usr/bin/env python3
"""`cluster ls`: round trips, concurrency, tables and saved evidence.

Run: python3 -m unittest tests.test_listing
(or the whole suite: python3 -m unittest discover -s tests -p 'test_*.py')
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# For `support`, which puts this checkout on sys.path and sets up the sandbox
# every test runs in, so it is imported before anything from clustertool.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import _patched  # noqa: E402
from clustertool import config, ui  # noqa: E402
from clustertool.backends import load  # noqa: E402
from clustertool import listing, registry  # noqa: E402
from clustertool.commands.connections import cmd_list  # noqa: E402
from clustertool.sshmux import Logins  # noqa: E402
from clustertool.tmuxlayer import (Tmux, crumbs_snippet,  # noqa: E402
                                   node_and_sessions_snippet)


class _Screen(io.StringIO):
    """A StringIO that claims to be a terminal."""

    def isatty(self):
        return True


class _RecordedShell:
    """A fasrc Tmux whose every remote shell answers *reply*.

    The snippets it was asked to run are kept, in order, in self.snippets.
    """

    def _tmux(self, reply):
        tmux = Tmux(Logins(load("fasrc")))
        self.snippets = []

        def sh(_login, snippet, timeout=60):
            self.snippets.append(snippet)
            return reply

        tmux._sh = sh
        return tmux


class TestListRoundTrips(_RecordedShell, unittest.TestCase):
    """`ls` asks each node once, and asks every node at the same time; and it
    learns about strays without a second channel or a credential.

    An SSH channel costs ~0.85s whatever it carries — sshd's per-session setup,
    not the work — so the only two numbers that matter for how long `ls` takes
    are how many channels it opens and how many it opens at once.
    """

    def test_node_sessions_and_crumbs_take_one_round_trip(self):
        crumbs = "__cluster_crumbs__\nboslogin08\tmain\tmain\nholylogin06\tx\tmain"
        cases = [
            ("__cluster_ls__\nboslogin08.rc.fas.harvard.edu\n"
             "main\t3\t1\tmain\t\ndev\t1\t0\t\t",
             ("boslogin08.rc.fas.harvard.edu", ["main", "dev"]), None),
            # FASRC's .bashrc prints a Slurm banner. Reading the first line would
            # make the banner the node name, and reading the last would make a
            # session name the node; the marker is what delimits the answer.
            ("+--- Slurm Stats ---+\n| GPU: 198 |\n__cluster_ls__\n"
             "boslogin08.rc.fas.harvard.edu\nmain\t3\t1\tmain\t",
             ("boslogin08.rc.fas.harvard.edu", ["main"]), None),
            ("", ("", []), None),
            # The hostname line is printed unconditionally, so a node that cannot
            # name itself costs the node column, not the session list.
            ("__cluster_ls__\n\nmain\t3\t1\tmain\t", ("", ["main"]), None),
            ("__cluster_ls__\nholylogin06.rc\nx\t1\t0\tmain\t\n" + crumbs,
             ("holylogin06.rc", ["x"]),
             {("boslogin08", "main"): "main", ("holylogin06", "x"): "main"}),
        ]
        for reply, (node, names), crumbs_read in cases:
            with self.subTest(reply=reply):
                tmux = self._tmux(reply)
                got_node, sessions = tmux.node_and_sessions("main")
                self.assertEqual((got_node, [s.name for s in sessions]),
                                 (node, names))
                got = tmux.node_sessions_and_crumbs("main")
                self.assertEqual(len(self.snippets), 2, "one round trip each")
                self.assertIn(crumbs_snippet(), self.snippets[1])
                # One failed read must not be reported as "every record vanished".
                self.assertEqual(got[2], crumbs_read)
        main, dev = self._tmux(cases[0][0]).node_and_sessions("main")[1]
        self.assertEqual((main.attached, dev.tagged), (True, False))

    def test_quiet_keeps_the_crumbs_and_does_not_ask_tmux_at_all(self):
        # -q never runs tmux, so a sick tmux server cannot block it.
        tmux = self._tmux("__cluster_ls__\nboslogin08.rc.fas.harvard.edu")
        node, sessions = tmux.node_and_sessions("main", with_sessions=False)
        self.assertEqual(len(self.snippets), 1)
        self.assertNotIn("tmux", self.snippets[0])
        self.assertEqual((node, sessions), ("boslogin08.rc.fas.harvard.edu", []))
        snippet = node_and_sessions_snippet(with_sessions=False, with_crumbs=True)
        self.assertNotIn("tmux", snippet)
        self.assertIn(".cluster/sessions", snippet)


class TestListIsConcurrent(unittest.TestCase):
    """Several logins are listed side by side, in a stable order."""

    def ctx(self, backend, node, delay):
        """A context for one login on *node*, whose live read takes *delay*."""
        from types import SimpleNamespace as NS

        def live(_login, with_sessions=True, with_crumbs=True, timeout=60):
            if getattr(self, "before_live", None):
                self.before_live()
            time.sleep(delay)
            return node, [], {}

        ctx = NS(
            backend=NS(name=backend, label=backend,
                       short=lambda node: (node or "").split(".")[0]),
            state=NS(pin_read=lambda _n: node, mountnode_read=lambda _n: "",
                     abandoned=list, known_logins=list, read_meta=lambda _n: {},
                     write_list_evidence=lambda _evidence: None,
                     read_list_evidence=lambda: ctx.state.cached,
                     cached={"updated": 0, "evidence": []}),
            logins=NS(is_active=lambda _n: True),
            mounts=NS(is_mounted=lambda _n: False, mounted_elsewhere=lambda _n: ""),
            explicit_backend=False, settings=NS(int=config.DEFAULTS.__getitem__),
            nothing_set_up=lambda: False, node_sessions_and_crumbs=live)
        ctx.tmux = ctx
        ctx.sibling = lambda _name: ctx
        return ctx

    def test_logins_are_listed_in_parallel_and_in_order(self):
        delay = 0.4
        ctxs = [self.ctx("fasrc", "boslogin08.rc", delay),
                self.ctx("nersc", "login35.chn", delay),
                self.ctx("nersc", "login12.chn", delay)]
        names = ["main", "work", "gpu"]
        top = ctxs[0]
        top.scope_all = lambda: list(zip(ctxs, names))
        top.scope = lambda: ["fasrc", "nersc"]

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with _patched(registry, "collisions", lambda: {}):
                start = time.monotonic()
                self.assertEqual(cmd_list(top, []), 0)
                elapsed = time.monotonic() - start

        printed = [line.split()[1] for line in out.getvalue().splitlines()[1:]]
        self.assertEqual(printed, names)          # input order, not finish order
        # Three logins, one delay: sequential would be 3x. The bound is loose
        # enough not to fail on a loaded machine but tight enough to catch
        # serial listing.
        self.assertLess(elapsed, delay * 2)

    def list_saved(self, sessions, row_sessions, out, active=True):
        """cmd_list over one login whose saved evidence names *sessions*."""
        ctx = self.ctx("fasrc", "boslogin08.rc", 0)
        ctx.logins.is_active = lambda _name: active
        old = ["fasrc", "main", "active", "oldnode", "oldnode", "-", row_sessions]
        ctx.state.cached = {"updated": 1, "evidence": [{
            "login": "main", "row": old, "sessions": sessions,
            "sessions_loaded": True,
        }]}
        ctx.scope = lambda: ["fasrc"]
        ctx.scope_all = lambda: [(ctx, "main")]
        with contextlib.redirect_stdout(out), \
                _patched(registry, "logins_of", lambda _backend: ["main"]), \
                _patched(registry, "collisions", lambda: {}):
            self.assertEqual(cmd_list(ctx, []), 0)
        return out.getvalue()

    def test_interactive_list_draws_saved_evidence_before_the_live_read(self):
        out = _Screen()
        self.before_live = lambda: self.assertIn("oldnode", out.getvalue())
        text = self.list_saved(["x"], "x", out)
        self.assertLess(text.index("oldnode"), text.index("boslogin08"))
        self.assertIn("(saved evidence; loading live evidence...)", text)
        self.assertTrue(text.rstrip().endswith("(live evidence loaded)"))

    def test_final_list_marks_saved_sessions_when_login_is_down(self):
        text = self.list_saved(["build", "notes"], "build*, notes*", io.StringIO(),
                               active=False)
        self.assertIn("down", text)
        self.assertIn("cached: build, notes", text)
        self.assertNotIn("build*", text)


class TestColourFollowsTheStream(unittest.TestCase):
    """Colour is decided by the stream the text goes to, when it goes."""

    def test_each_stream_is_asked_and_no_color_wins(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("NO_COLOR", None)
            # `cluster clean | tee log` with stderr still on the terminal.
            out, err = io.StringIO(), _Screen()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(ui.bold("kept"), "kept")
                self.assertEqual(ui.yellow("kept"), "kept")
                ui.warn("careful")
            self.assertIn("\033[33mwarning\033[0m", err.getvalue())
            out, err = _Screen(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(ui.bold("x"), "\033[1mx\033[0m")
                ui.warn("careful")
            self.assertEqual(err.getvalue(), "cluster: warning: careful\n")
            os.environ["NO_COLOR"] = "1"
            with contextlib.redirect_stdout(_Screen()):
                self.assertEqual(ui.bold("x"), "x")


class TestTableRendering(unittest.TestCase):
    """Tables are sized to the terminal they are printed into.

    Rendered at their natural width these run to about 110 columns. A narrow
    terminal (a VS Code panel beside an editor) hard-wraps each row at the
    edge with no indent, so consecutive rows run together unreadably.
    """

    HEADERS = ["BACKEND", "LOGIN", "STATE", "NODE", "PINNED", "MOUNT", "SESSIONS"]
    ROWS = [
        ["fasrc", "work", "active", "holy7c02107", "holy7c02107",
         "mounted via holy7c02107", "main*, build, notes"],
        ["fasrc", "scratch", "down", "-", "holy7c12309", "shares work",
         "cached: main, longrunning-fit"],
        ["nersc", "perlmutter", "active", "login23", "login23",
         "mounted via login23", "none"],
    ]

    def test_a_table_is_never_wider_than_the_terminal_and_a_pipe_is_never_trimmed(self):
        natural = ui.render_table(self.ROWS, self.HEADERS, width=200)
        self.assertIn("mounted via holy7c02107", natural)
        self.assertIn("cached: main, longrunning-fit", natural)
        self.assertEqual(len(natural.split("\n")), len(self.ROWS) + 1)
        for width in range(16, 130):
            rendered = ui.render_table(self.ROWS, self.HEADERS, width=width)
            with self.subTest(width=width):
                self.assertLessEqual(
                    max(len(ui.visible(line)) for line in rendered.split("\n")), width)
        # A pipe has no width, and whatever is reading wants every character.
        self.assertEqual(ui.terminal_size(io.StringIO()), (0, 0))
        self.assertEqual(ui.render_table(self.ROWS, self.HEADERS, width=0),
                         ui.render_table(self.ROWS, self.HEADERS, width=1000))

    def test_space_is_taken_from_the_widest_column_first(self):
        rendered = ui.render_table(self.ROWS, self.HEADERS, width=90)
        # Identifiers survive whole; the free-text columns pay for them, which
        # is the only place the loss does not destroy the meaning of a cell.
        self.assertIn("perlmutter", rendered)
        self.assertIn("holy7c02107", rendered)
        self.assertNotIn("mounted via holy7c02107", rendered)
        # Below the point where a squeezed cell would stop being recognisable
        # the table becomes stacked records, which lose nothing at any width.
        rendered = ui.render_table(self.ROWS, self.HEADERS, width=40)
        for row in self.ROWS:
            for cell in row:
                self.assertIn(cell, rendered)

    def test_space_the_other_columns_do_not_need_returns_to_the_key(self):
        # The key column's share is a floor, not a ceiling: a table of one wide
        # column and a few short ones must spend the whole line on the wide one.
        rendered = ui.render_table([["a-very-long-identifier-value", "1", "x"]],
                                   ["KEY", "N", "F"], width=40)
        self.assertIn("a-very-long-identifier-value", rendered)
        # Not the ellipsis character itself: that falls back to ASCII where the
        # encoding cannot carry it.
        only = ui.render_table([["x" * 60]], ["ONLY"], width=30).split("\n")[1]
        self.assertEqual(len(only), 30)
        self.assertTrue(only.startswith("x" * 29))
        rendered = ui.render_table([["a"], ["a", "b", "c", "d"]],
                                   ["ONE", "TWO", "THREE"], width=80)
        self.assertEqual(len(rendered.split("\n")), 3, "a row of the wrong length")

    def test_visual_lines_counts_what_the_terminal_shows(self):
        # Escape sequences occupy no columns, and no width means there is no
        # wrapping to account for.
        for text, width, rows in (("a\nb", 80, 2), ("x" * 80, 80, 1),
                                  ("x" * 81, 80, 2), ("", 80, 1),
                                  (ui.bold("x" * 80), 80, 1), ("x" * 200, 0, 1)):
            self.assertEqual(ui.visual_lines(text, width), rows, (text, width))


class TestOptimisticRepaint(unittest.TestCase):
    """`cluster ls` paints saved evidence, then takes it back."""

    def _paint_then_erase(self, text, painted_size, erase_size):
        screen = _Screen()
        painter = listing._Optimistic(True, screen)
        sizes = iter([painted_size, erase_size])
        with _patched(ui, "terminal_size", lambda stream=None: next(sizes)):
            painter.paint(text)
            erased = painter.erase()
        return erased, screen.getvalue()

    def test_a_repaint_is_given_up_on_rather_than_drawn_over_what_it_cannot_count(self):
        tall = "\n".join(f"row {index}" for index in range(30))
        cases = [
            # Relative movement: moving up by our own row count cannot be
            # invalidated by the block scrolling, as an absolute save/restore can.
            ("one\ntwo\nthree", (80, 24), (80, 24), True, "\r\033[3A\033[J"),
            # One 100-column line occupies two rows of an 80-column terminal.
            ("x" * 100 + "\nshort", (80, 24), (80, 24), True, "\033[3A"),
            # A resize while the live probe runs invalidates the painted row
            # count, and erasing by it would draw the live table over the cached
            # one. Leaving both tables in the scrollback is the poor result;
            # writing one over the other is the unreadable one.
            ("one\ntwo\nthree", (120, 24), (60, 24), False, None),
            # Taller than the screen, it would scroll, which is exactly when the
            # erase has to be given up on: it is never painted.
            (tall, (80, 24), (80, 24), False, ""),
        ]
        for text, painted, then, erased, out in cases:
            with self.subTest(text=text[:10], then=then):
                got, written = self._paint_then_erase(text, painted, then)
                self.assertEqual(got, erased)
                if out is None:
                    self.assertNotIn("\033[", written)
                    self.assertTrue(written.endswith("\n\n"))
                elif out:
                    self.assertIn(out, written)
                else:
                    self.assertEqual(written, "")

    def test_a_disabled_painter_writes_nothing_and_erases_nothing(self):
        screen = _Screen()
        painter = listing._Optimistic(False, screen)
        self.assertFalse(painter.paint("anything"))
        self.assertFalse(painter.erase())
        self.assertEqual(screen.getvalue(), "")


class TestSavedCrumbEvidence(unittest.TestCase):
    """`ls` remembers the catalogue, so strays stay visible with no master up."""

    def test_crumbs_survive_a_round_trip_and_a_hand_edited_file(self):
        def cached(evidence):
            return listing.cached_crumbs(SimpleNamespace(state=SimpleNamespace(
                read_list_evidence=lambda: {"evidence": evidence})))

        crumbs = {("boslogin08", "main"): "main", ("holylogin06", "x"): "main"}
        rows = listing._crumb_rows(crumbs)
        self.assertEqual(listing._crumb_map(rows), crumbs)
        self.assertEqual(cached([{"login": "down", "crumbs": []},
                                 {"login": "main", "crumbs": rows}]), crumbs)
        # A hand-edited file cannot crash the listing.
        self.assertEqual(cached([{"login": "main", "crumbs": [
            ["node"], "nonsense", [], None, ["n", "s"]]}]), {("n", "s"): ""})

    def test_a_live_read_is_preferred_over_the_saved_one(self):
        evidence = [{"login": "main", "row": ["fasrc"],
                     "crumbs": [["boslogin08", "main", "main"]]},
                    {"login": "work", "row": ["nersc"]}]
        self.assertEqual(listing._evidence_crumbs(evidence, "fasrc"),
                         {("boslogin08", "main"): "main"})
        # nersc read nothing this run: absent, which is not the same as empty.
        self.assertIsNone(listing._evidence_crumbs(evidence, "nersc"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
