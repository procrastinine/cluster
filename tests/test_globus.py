#!/usr/bin/env python3
"""The Globus engine: path rules, and telling its auth errors apart.

Run: python3 -m unittest tests.test_globus
(or the whole suite: python3 -m unittest discover -s tests -p 'test_*.py')
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

# For `support`, which puts this checkout on sys.path and sets up the sandbox
# every test runs in, so it is imported before anything from clustertool.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import support  # noqa: E402
from support import _patched, _refusal  # noqa: E402
from clustertool import config, globuslayer, platform as plat  # noqa: E402


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _side(path, excluded=(), example="/good/path/...", name="nersc"):
    def problem(p):
        for prefix, why in excluded:
            if p.startswith(prefix):
                return why
        return None

    return types.SimpleNamespace(
        name=name, path=path,
        settings=types.SimpleNamespace(str=lambda key: ""),
        backend=types.SimpleNamespace(
            label="Test Cluster", globus_path_example=example,
            globus_path_problem=problem, globus_collection=f"uuid-{name}",
            cli_flag=lambda: f"--{name}"),
    )


class TestGlobusPaths(unittest.TestCase):
    """Path rules, checked here before a task can fail at Globus minutes later."""

    def test_a_path_that_cannot_work_is_refused_with_the_reason(self):
        self.assertEqual(globuslayer.check_path(_side("/global/u1/u/user/data")),
                         "/global/u1/u/user/data")
        for path in ("~/data", "data", "./data"):
            message = _refusal(lambda: globuslayer.check_path(_side(path)))
            self.assertIn("absolute", message)
            # And it must show a path shape that works on *this* cluster.
            self.assertIn("/good/path/...", message)
        # No network: a collection is not the whole filesystem, and a path that can
        # never work should not cost a round trip to learn that.
        side = _side("/n/home01/user/x",
                     excluded=(("/n/home", "home is not exported"),))
        message = _refusal(lambda: globuslayer.check_path(side))
        self.assertIn("home is not exported", message)
        self.assertIn("--engine direct", message)  # the thing that does work

    def test_where_a_source_lands(self):
        # Globus recursion copies *contents*, so landing SRC inside DEST has to be
        # spelled out in the destination path.
        cases = [
            (("/a/tree", "/b", False, True), ("/a/tree", "/b/tree", True)),
            # Contents of does not extend the destination.
            (("/a/tree/", "/b", True, True), ("/a/tree/", "/b", True)),
            (("/a/f.h5", "/b/g.h5", False, False), ("/a/f.h5", "/b/g.h5", False)),
            # A file sent into a directory keeps its name.
            (("/a/f.h5", "/b", False, False, True), ("/a/f.h5", "/b/f.h5", False)),
            (("/a/f.h5", "/b/", False, False, True), ("/a/f.h5", "/b/f.h5", False)),
            # A trailing slash marks a directory whatever the listing.
            (("/a/v1.2/", "/b", False, False), ("/a/v1.2/", "/b/v1.2", True)),
        ]
        for given, want in cases:
            with self.subTest(given=given):
                got = globuslayer.plan_paths(*given)
                self.assertEqual(got, want)


class TestGlobusSourceKind(unittest.TestCase):
    """What the source is comes from the listing, not from its name."""

    LISTING = [{"name": "v1.2", "type": "dir"}, {"name": "Makefile", "type": "file"},
               {"name": "gone-link", "type": "invalid_symlink"}]

    def kind(self, path, listing=LISTING):
        return globuslayer.source_is_dir(_side(path), path, listing)

    def test_a_dot_is_not_a_file_and_no_dot_is_not_a_directory(self):
        self.assertTrue(self.kind("/a/v1.2"))
        self.assertFalse(self.kind("/a/Makefile"))
        self.assertTrue(self.kind("/a/anything/", listing=None),
                        "a trailing slash needs no listing")

    def test_what_the_listing_cannot_show_is_said_before_anything_is_submitted(self):
        told = _refusal(lambda: self.kind("/a/nothing"))
        self.assertIn("/a/nothing does not exist on Test Cluster", told)
        self.assertIn("not in the Globus listing of /a/", told)
        self.assertIn("symlink to nothing", _refusal(lambda: self.kind("/a/gone-link")))
        told = _refusal(lambda: self.kind("/a/v1.2", listing=None))
        self.assertIn("end the path with /", told)


class TestGlobusSubmission(unittest.TestCase):
    """run_cross against a stand-in for the globus CLI."""

    def run_cross(self, source, dest, listings=None, **kw):
        listings = listings or {}
        ran = []

        def fake_run(argv, timeout=None, **_kw):
            ran.append((argv, timeout))
            if argv[1] == "whoami":
                return _proc(0, "user@example.org\n")
            if argv[1] == "ls":
                where = argv[-1].split(":", 1)[1]
                return _proc(0, json.dumps({"DATA": listings.get(where, [])}))
            if argv[1] == "transfer":
                return _proc(0, json.dumps({"task_id": "task-1"}))
            return _proc(2, "", "unexpected")

        cross = types.SimpleNamespace(
            source=_side(source, name="fasrc"), dest=_side(dest), operation="copy",
            contents=False, symlinks="follow", extra=[], dry_run=False, quiet=True,
            keep=True)
        cross.__dict__.update(kw)
        out = io.StringIO()
        try:
            with _patched(globuslayer, "cli", lambda: "globus"), \
                    _patched(plat, "run", fake_run), \
                    contextlib.redirect_stderr(out), contextlib.redirect_stdout(out):
                rc = globuslayer.run_cross(cross)
        finally:
            self.said = out.getvalue()
        return rc, ran

    def test_what_is_submitted_follows_the_listings(self):
        lab = {"/n/lab/": [{"name": "v1.2", "type": "dir"},
                           {"name": "Makefile", "type": "file"},
                           {"name": "f.h5", "type": "file"}]}
        there = dict(lab, **{"/global/cfs/": [{"name": "in", "type": "dir"}]})
        cases = [
            # A directory named like a file is sent recursively into dest.
            ("/n/lab/v1.2", "/global/cfs/in", lab,
             ["uuid-fasrc:/n/lab/v1.2", "uuid-nersc:/global/cfs/in/v1.2"], True),
            ("/n/lab/Makefile", "/global/cfs/in/", lab,
             ["uuid-fasrc:/n/lab/Makefile", "uuid-nersc:/global/cfs/in/Makefile"],
             False),
            # A file into a directory that is there keeps its name.
            ("/n/lab/f.h5", "/global/cfs/in", there,
             ["uuid-fasrc:/n/lab/f.h5", "uuid-nersc:/global/cfs/in/f.h5"], False),
            ("/n/lab/f.h5", "/global/cfs/new.h5", lab,
             ["uuid-fasrc:/n/lab/f.h5", "uuid-nersc:/global/cfs/new.h5"], False),
        ]
        for source, dest, listings, ends, recursive in cases:
            with self.subTest(source=source, dest=dest):
                rc, ran = self.run_cross(source, dest, listings=listings)
                self.assertEqual(rc, 0)
                # Both sides are listed before the task is submitted, and no
                # call is given a deadline: the CLI's SDK times out a request
                # the service stops answering, and a big listing or a slow
                # submission is left to finish.
                self.assertEqual([argv[1] for argv, _t in ran],
                                 ["whoami", "ls", "ls", "transfer"])
                self.assertEqual({t for _argv, t in ran}, {None})
                argv = ran[-1][0]
                self.assertEqual(argv[2:4], ends)
                self.assertEqual("--recursive" in argv, recursive)

    def test_a_dry_run_lists_and_does_not_submit(self):
        rc, ran = self.run_cross(
            "/n/lab/f.h5", "/global/cfs/in/", dry_run=True,
            listings={"/n/lab/": [{"name": "f.h5", "type": "file"}]})
        self.assertEqual(rc, 0)
        self.assertNotIn("transfer", [argv[1] for argv, _t in ran])
        self.assertIn("would submit: globus transfer", self.said)

    def test_symlink_choices_globus_cannot_honour_are_refused(self):
        for choice, flag in (("skip", "--skip-symlinks"), ("keep", "-l")):
            with self.subTest(choice=choice):
                with self.assertRaises(SystemExit):
                    self.run_cross("/n/lab/x", "/global/cfs/in", symlinks=choice)
                self.assertIn(f"{flag} is not available over Globus", self.said)
                self.assertIn("--engine direct", self.said)


class TestGlobusSetupHints(unittest.TestCase):
    """Every hint names the setting to change, not an environment variable."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=str(support.SANDBOX)))
        self.addCleanup(shutil.rmtree, str(self.root), True)

    def fake(self, where):
        path = self.root / where / "globus"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
        return str(path)

    def find(self, on_path=None, fallbacks=()):
        path = str(Path(on_path).parent) if on_path else str(self.root / "empty")
        with _patched(config, "global_value", lambda key, default="": default), \
                _patched(globuslayer, "_fallbacks", lambda: tuple(fallbacks)), \
                mock.patch.dict(os.environ, {"PATH": path}):
            return globuslayer.find_cli()

    def test_the_cli_on_path_comes_first_then_homebrew_and_pipx(self):
        mine, brew = self.fake("bin"), self.fake("opt/homebrew/bin")
        self.assertEqual(self.find(on_path=mine, fallbacks=[brew]), mine)
        self.assertEqual(self.find(fallbacks=["/nonexistent/globus", brew]), brew)
        places = globuslayer._fallbacks()
        self.assertIn("/opt/homebrew/bin/globus", places)
        self.assertIn("/usr/local/bin/globus", places)
        self.assertIn(str(Path.home() / ".local" / "bin" / "globus"), places)

    def test_a_missing_cli_names_the_setting(self):
        values = {"GLOBUS": ""}
        with _patched(config, "global_value",
                      lambda key, default="": values.get(key, default)), \
                _patched(globuslayer, "_fallbacks", lambda: ()), \
                mock.patch.dict(os.environ, {"PATH": str(self.root)}):
            told = _refusal(globuslayer.cli)
            self.assertIn("the globus CLI is not installed", told)
            self.assertIn("cluster config set GLOBUS /path/to/globus", told)
            values["GLOBUS"] = str(self.root / "nowhere")
            told = _refusal(globuslayer.cli)
            self.assertIn("which is not an executable file", told)
            self.assertIn("cluster config set GLOBUS /path/to/globus", told)

    def test_a_collection_hint_names_the_setting_for_that_cluster(self):
        side = _side("/x")
        side.backend.globus_collection = ""
        told = _refusal(lambda: globuslayer.collection_for(side))
        self.assertIn("cluster --nersc config set GLOBUS_COLLECTION <uuid>", told)
        hints = " ".join(globuslayer.COLLECTIONS)
        self.assertIn("cluster --BACKEND config set GLOBUS_COLLECTION", hints)
        self.assertNotIn("export", hints)

    def test_a_collection_setting_wins_over_the_declared_one(self):
        side = _side("/x")
        side.settings = types.SimpleNamespace(
            str=lambda key: " set-uuid " if key == "GLOBUS_COLLECTION" else "")
        self.assertEqual(globuslayer.collection_for(side), "set-uuid")
        self.assertEqual(globuslayer.collection_for(_side("/x")), "uuid-nersc")


class TestGlobusAuthErrors(unittest.TestCase):
    """Globus reports two different problems in similar words.

    Both messages are real, captured from the live service on 2026-08-08. They
    need *different* commands, and confusing them wastes a browser round trip.
    """

    CONSENT = (
        "The collection you are trying to access data on requires you to grant "
        "consent for the Globus CLI to access it.\n\nPlease run:\n\n  globus "
        "session consent 'urn:globus:auth:scope:transfer.api.globus.org:all"
        "[*https://auth.globus.org/scopes/abc/data_access]'\n"
    )
    SESSION = (
        "The resource you are trying to access requires you to re-authenticate.\n"
        "message: Session reauthentication required (Globus Transfer)\n\n"
        "Please run:\n\n    globus session update globus.rc.fas.harvard.edu\n"
    )

    def setUp(self):
        self.side = types.SimpleNamespace(
            name="fasrc",
            backend=types.SimpleNamespace(
                label="Harvard FASRC",
                globus_session_domain="globus.rc.fas.harvard.edu"),
        )

    def test_a_missing_consent_asks_for_consent(self):
        problem, *hints = globuslayer.auth_remedy(self.CONSENT, self.side, "abc")
        self.assertIn("consent", problem)
        joined = " ".join(hints)
        self.assertIn("session consent", joined)
        self.assertIn("--no-local-server", joined)  # headless: no browser here
        self.assertIn("/scopes/abc/data_access", joined)

    def test_a_session_policy_asks_for_a_session_update_not_a_consent(self):
        problem, *hints = globuslayer.auth_remedy(self.SESSION, self.side, "abc")
        self.assertIn("session policy", problem)
        joined = " ".join(hints)
        self.assertIn("session update", joined)
        self.assertNotIn("session consent", joined)
        self.assertIn("--no-local-server", joined)
        # And it must say why this engine is wrong for cron.
        self.assertIn("direct", joined)

    def test_the_domain_comes_from_what_globus_actually_said(self):
        # Preferred over the declared one: the service knows, and a site can
        # change its policy without this tool being updated.
        text = self.SESSION.replace("globus.rc.fas.harvard.edu", "elsewhere.example")
        _problem, *hints = globuslayer.auth_remedy(text, self.side, "abc")
        self.assertIn("elsewhere.example", " ".join(hints))

    def test_an_unrelated_failure_is_not_misread_as_an_auth_problem(self):
        denied = ("Globus CLI Error: A Transfer API Error Occurred.\n"
                  "code: EndpointPermissionDenied\nmessage: Denied by endpoint")
        self.assertIsNone(globuslayer.auth_remedy(denied, self.side, "abc"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
