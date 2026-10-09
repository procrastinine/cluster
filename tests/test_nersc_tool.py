#!/usr/bin/env python3
"""The NERSC companion, remote/nersc, and its project hooks.

Run: python3 -m unittest tests.test_nersc_tool
(or the whole suite: python3 -m unittest discover -s tests -p 'test_*.py')
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

# For `support`, which puts this checkout on sys.path and sets up the sandbox
# every test runs in, so it is imported before anything from clustertool.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import REPO_ROOT, FakeClock, _patched  # noqa: E402
from clustertool.companion import py36_problems  # noqa: E402

TOOL = REPO_ROOT / "remote" / "nersc"
EXAMPLE_HOOKS = REPO_ROOT / "remote" / "hooks.example.py"


def load_tool(config_text=None, env=None):
    """A fresh copy of the companion, reading *config_text* (or no config file
    at all) instead of this machine's real one, with *env* added."""
    import importlib.machinery
    import importlib.util

    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / "config"
        if config_text is not None:
            config.write_text(config_text)
        with mock.patch.dict(os.environ, dict(env or {}, NERSC_CONFIG=str(config))):
            spec = importlib.util.spec_from_loader(
                "nersctool",
                importlib.machinery.SourceFileLoader("nersctool", str(TOOL)))
            mod = importlib.util.module_from_spec(spec)
            with contextlib.redirect_stderr(io.StringIO()):
                spec.loader.exec_module(mod)
    return mod


def hold_sync_lock(test, path, seconds):
    """A process holding an flock on *path*, with its pid written there, for
    *seconds*; it is stopped and reaped when *test* ends."""
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl, os, sys, time\n"
         "f = open(sys.argv[1], 'a+')\n"
         "fcntl.flock(f, fcntl.LOCK_EX)\n"
         "f.write('%d\\n' % os.getpid()); f.flush()\n"
         "print('held', flush=True)\n"
         "time.sleep(float(sys.argv[2]))\n", str(path), str(seconds)],
        stdout=subprocess.PIPE, universal_newlines=True)
    test.addCleanup(holder.wait)
    test.addCleanup(holder.kill)
    test.addCleanup(holder.stdout.close)
    test.assertEqual(holder.stdout.readline().strip(), "held")
    return holder


def refusal(callable_, *args):
    """Run something that must stop with die(); return what it said."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        with unittest.TestCase().assertRaises(SystemExit):
            callable_(*args)
    return err.getvalue()


class TestNerscTool(unittest.TestCase):
    """The hub's companion must run on its login nodes' python 3.6."""

    @classmethod
    def setUpClass(cls):
        cls.mod = load_tool()

    def test_no_syntax_beyond_python36(self):
        for path in (TOOL, EXAMPLE_HOOKS):
            with self.subTest(path=path.name):
                self.assertEqual(py36_problems(path.read_text()), [])

    def test_the_python36_check_finds_what_the_parser_lets_through(self):
        # ast.parse(feature_version=(3, 6)) accepts some of these, depending
        # on the interpreter running the check.
        newer = {
            "walrus": "if (n := 1):\n    pass\n",
            "positional-only": "def f(a, /):\n    pass\n",
            "f-string =": 'print(f"{value=}")\n',
            "f-string = with spaces and a spec": 'print(f"""\n{ value = !r:>8}""")\n',
            "future annotations": "from __future__ import annotations\n",
            "a 3.7 module": "import dataclasses\n",
            "capture_output": "subprocess.run(argv, capture_output=True)\n",
            "text=": "subprocess.run(argv, text=True)\n",
        }
        fine = {
            "an f-string with = in its text": 'print(f"a={a} b={b!r}")\n',
            "operators and keywords": 'print(f"{a == b} {a != b} {a <= b} {a >= b} '
                                      '{f(k=1)} {d[\'x\']} {v:>{w}} {{literal=}}")\n',
            "a plain string": 'print("{value=}")\n',
        }
        for name, source in newer.items():
            with self.subTest(newer=name):
                self.assertTrue(py36_problems(source), source)
        for name, source in fine.items():
            with self.subTest(fine=name):
                self.assertEqual(py36_problems(source), [], source)

    def test_expand_remote(self):
        mod = self.mod
        with _patched(mod, "CFG", dict(mod.CFG, scratch="/pscratch/sd/u/user",
                                       cfs="/global/cfs/cdirs/m0000/user")):
            for given, expanded in (
                    ("$PSCRATCH/runs", "/pscratch/sd/u/user/runs"),
                    ("$SCRATCH/runs", "/pscratch/sd/u/user/runs"),
                    ("$CFS/x", "/global/cfs/cdirs/m0000/user/x"),
                    ("~/project", "project"), ("~", "."), ("plain/path", "plain/path"),
                    # not mangled by the $PSCRATCH rule
                    ("$PSCRATCHY", "$PSCRATCHY")):
                self.assertEqual(mod.expand_remote(given), expanded, given)

    def test_slurm_verbs_cover_the_daily_set(self):
        for verb in ("sbatch", "squeue", "sacct", "scancel", "scontrol", "sinfo"):
            self.assertIn(verb, self.mod.SLURM_VERBS)

    def test_ca_line_matches_backends(self):
        from clustertool.backends.nersc import NerscBackend
        self.assertEqual(self.mod.CERT_AUTHORITY, NerscBackend.CERT_AUTHORITY)

    def test_ago_formatting(self):
        self.assertEqual(self.mod._ago(3900), "1h05m")
        self.assertEqual(self.mod._ago(120), "2m")
        self.assertEqual(self.mod._ago(9), "9s")

    def test_defaults_carry_no_project(self):
        """A fresh install has no mirror, no return root and no hooks: every
        project-specific behaviour is opt-in through the config."""
        mod = self.mod
        for key in ("mirror_src", "mirror_dest", "return_root", "scratch_link",
                    "hooks"):
            self.assertEqual(mod.DEFAULTS[key], "", key)
        self.assertIsNone(mod.HOOKS)
        self.assertIsNone(mod.HOOKS_ERROR)
        self.assertEqual(mod.SUBMIT_INPUT_EXCLUDES, ())
        self.assertEqual(mod.return_exclude_args(), [])
        self.assertIsNone(mod._default_submit_argv(["train.py", "--x"]))
        self.assertEqual(mod._discover_default_submit_inputs(["x"]), [])
        self.assertIsNone(mod.default_dest("/pscratch/sd/u/user/runs/a"))

    def test_config_and_state_locations_follow_the_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod = load_tool(env={"NERSC_STATE_DIR": tmp + "/state"})
            self.assertEqual(mod.STATE_DIR, Path(tmp) / "state")
            self.assertEqual(mod.EXCLUDE_PATH, mod.CONFIG_PATH.parent / "mirror.exclude")
        with mock.patch.dict(os.environ):
            os.environ.pop("NERSC_STATE_DIR", None)
            mod = load_tool()
        self.assertEqual(mod.STATE_DIR, Path.home() / ".local" / "state" / "nersc")

    def test_sync_without_a_mirror_names_the_two_keys(self):
        said = refusal(self.mod.cmd_sync, [])
        self.assertIn("mirror_src", said)
        self.assertIn("mirror_dest", said)

    def test_example_hooks_drive_the_default_submit_form(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "jobs").mkdir()
            (Path(root) / "jobs" / "train.py").write_text("pass\n")
            mod = load_tool("mirror_src = %s\nmirror_dest = proj\nhooks = %s\n"
                            % (root, EXAMPLE_HOOKS))
            self.assertIsNone(mod.HOOKS_ERROR)
            self.assertEqual(mod.SUBMIT_INPUT_EXCLUDES, ("*.log", "__pycache__/"))
            self.assertEqual(
                mod._default_submit_argv(["jobs/train.py", "--lr", "3"]),
                [".venv/bin/python", "jobs/train.py", "--lr", "3"])
            self.assertIsNone(mod._default_submit_argv(["echo", "hi"]))
            refusal(mod._default_submit_argv, ["jobs/nope.py"])
            self.assertEqual(
                mod._discover_default_submit_inputs(["x", "--input=/data/a.h5"]),
                ["/data/a.h5"])
            self.assertEqual(mod.return_exclude_args(),
                             ["--exclude=*.tmp", "--exclude=core.*"])
            # Hook excludes lead the filter, so a selective include cannot
            # pull an excluded file back in.
            args = mod._return_filter_args({"include_prefixes": ["point"]})
            self.assertEqual(args[:2], ["--exclude=*.tmp", "--exclude=core.*"])
            self.assertEqual(args[-1], "--exclude=*")
            line, bad = mod.describe_hooks()
            self.assertEqual(bad, 0)
            self.assertIn("submit_argv", line)

    def test_hooks_see_the_live_config(self):
        """A hook reads tool.CFG at call time, so a caller that swaps the
        config dict (as these tests do) is honoured."""
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "a.py").write_text("pass\n")
            mod = load_tool("hooks = %s\n" % EXAMPLE_HOOKS)
            refusal(mod._default_submit_argv, ["a.py"])
            mod.CFG = dict(mod.CFG, mirror_src=root)
            self.assertEqual(mod._default_submit_argv(["a.py"])[:2],
                             [".venv/bin/python", "a.py"])

    def test_a_broken_hooks_file_refuses_the_default_submit_form_only(self):
        with tempfile.TemporaryDirectory() as root:
            broken = Path(root) / "hooks.py"
            broken.write_text("raise RuntimeError('half-edited')\n")
            mod = load_tool("hooks = %s\n" % broken)
            self.assertIn("half-edited", mod.HOOKS_ERROR)
            self.assertIn("did not load",
                          refusal(mod.cmd_submit, ["--no-sync", "train.py"]))
            # The explicit form never consults the project, so it still runs.
            calls = []
            with _patched(mod, "remote_run",
                          lambda argv, cd=None: calls.append(argv) or 0):
                self.assertEqual(mod.cmd_submit(["--no-sync", "--", "true"]), 0)
            self.assertEqual(calls, [["true"]])
            # A return carries everything rather than failing.
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(mod.return_exclude_args(), [])
            self.assertEqual(mod.describe_hooks()[1], 1)

    def test_a_failing_return_excludes_hook_never_fails_the_return(self):
        with tempfile.TemporaryDirectory() as root:
            hooks = Path(root) / "hooks.py"
            hooks.write_text("def return_excludes(tool):\n    raise OSError('venv gone')\n")
            mod = load_tool("hooks = %s\n" % hooks)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(mod.return_exclude_args(), [])
            self.assertIn("venv gone", stderr.getvalue())

    def test_submit_flag_grammar_is_leading_only(self):
        """Tool flags are parsed from the front only: a command's own
        --no-sync survives, and --cd with the default submitter form is
        refused locally (the injected paths are mirror-root-relative)."""
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "jobs").mkdir()
            (Path(root) / "jobs" / "s.py").write_text("pass\n")
            mod = load_tool("mirror_src = %s\nmirror_dest = proj\nhooks = %s\n"
                            % (root, EXAMPLE_HOOKS))
            calls = []
            mod.cmd_sync = lambda a: self.fail("must not sync")
            mod.remote_run = lambda argv, cd=None: calls.append((list(argv), cd)) or 0

            # --cd + the default form: refused before any sync.
            refusal(mod.cmd_submit, ["--cd", "$PSCRATCH/x", "jobs/s.py"])
            self.assertEqual(calls, [])

            # Leading --no-sync skips the sync; the trailing one is the
            # remote command's own argument and passes through.
            mod.cmd_submit(["--no-sync", "--", "echo", "--no-sync"])
            self.assertEqual(calls[-1][0], ["echo", "--no-sync"])

            # Default form with leading --no-sync: rewritten argv, no sync,
            # run from the mirror.
            mod.cmd_submit(["--no-sync", "jobs/s.py", "--L", "6"])
            self.assertEqual(calls[-1],
                             ([".venv/bin/python", "jobs/s.py", "--L", "6"], "proj"))

    def test_pull_run_dir_concurrent_first_arrival_is_not_a_success(self):
        # A manual fetch and the reaper can both attempt the FIRST arrival of
        # the same run dir; the loser of the atomic rename must NOT report
        # success (the winner's copy may be an older mid-run snapshot, and a
        # success here would license --cleanup against it).
        mod = self.mod
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "runs", "job1")
            record = {"job": "1", "src": "/pscratch/sd/u/user/runs/job1",
                      "dest": dest, "cleanup": True}

            def rsync(argv, winner=True):
                os.makedirs(record["dest"] + ".nersc-part", exist_ok=True)
                if winner:   # the concurrent puller lands the real dir mid-flight
                    os.makedirs(dest, exist_ok=True)
                    Path(dest, "data").write_text("winner\n")
                return 0

            with _patched(mod, "pick_dtn", lambda: "dtn-fake"), \
                    _patched(mod, "_run_child", rsync), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertFalse(mod._pull_run_dir(record, "COMPLETED"))
            self.assertFalse(os.path.isdir(dest + ".nersc-part"))
            self.assertEqual(Path(dest, "data").read_text(), "winner\n")

            # And the plain fresh path renames into place.
            record["dest"] = os.path.join(tmp, "runs", "job2")
            with _patched(mod, "pick_dtn", lambda: "dtn-fake"), \
                    _patched(mod, "_run_child", lambda argv: rsync(argv, winner=False)):
                self.assertTrue(mod._pull_run_dir(record, "COMPLETED"))
            self.assertTrue(os.path.isdir(record["dest"]))
            self.assertFalse(os.path.isdir(record["dest"] + ".nersc-part"))

    def test_a_sync_lock_left_by_a_dead_sync_is_taken_at_once(self):
        mod = self.mod
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sync.lock").write_text("999999999\n")
            with _patched(mod, "sock_dir", lambda: root):
                lock = mod._sync_lock()
                self.addCleanup(lock.close)
            self.assertEqual((root / "sync.lock").read_text(), "%d\n" % os.getpid())

    def test_a_sync_waits_for_as_long_as_the_other_sync_runs(self):
        mod = self.mod
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            holder = hold_sync_lock(self, root / "sync.lock", 0.4)
            err = io.StringIO()
            started = time.monotonic()
            # Each second the lock waits takes a twentieth of one here.
            with _patched(mod, "sock_dir", lambda: root), \
                    _patched(mod, "time", FakeClock(time.time(), real=0.05)), \
                    _patched(mod, "SYNC_WAIT_NOTE_SECONDS", 1), \
                    contextlib.redirect_stderr(err):
                lock = mod._sync_lock()
            lock.close()
            self.assertGreaterEqual(time.monotonic() - started, 0.3,
                                    "taken only once the holder let go")
            self.assertIn("waiting for another mirror sync on this node (pid %d)"
                          % holder.pid, err.getvalue())
            self.assertGreaterEqual(err.getvalue().count("waiting for another"), 2)

    def test_sync_lock_fails_immediately_when_storage_is_unusable(self):
        mod = self.mod
        with tempfile.TemporaryDirectory() as tmp:
            gone = Path(tmp) / "gone"
            with _patched(mod, "sock_dir", lambda: gone):
                self.assertIn("cannot create sync lock", refusal(mod._sync_lock))

    def test_environment_snapshot_snippet_reports_installed_distributions(self):
        # ENV_SNAPSHOT_SNIPPET runs standalone in each environment (over ssh in
        # the NERSC venv) and prints the installed distributions as JSON.
        # Run it here.
        proc = subprocess.run([sys.executable, "-c", self.mod.ENV_SNAPSHOT_SNIPPET],
                              stdout=subprocess.PIPE, universal_newlines=True,
                              check=True)
        snapshot = json.loads(proc.stdout)
        self.assertIsInstance(snapshot, dict)
        self.assertTrue(snapshot)

    def test_submit_input_inventory_uses_exact_paths_and_prunes_redundant_dirs(self):
        mod = self.mod
        with tempfile.TemporaryDirectory() as tmp:
            mirror, scratch = Path(tmp) / "mirror", Path(tmp) / "scratch"
            child = mirror / "project" / "input.json"
            child.parent.mkdir(parents=True)
            child.write_text("{}\n")
            scratch.mkdir()
            run_input = scratch / "prepared.bin"
            run_input.write_bytes(b"x")
            with _patched(mod, "CFG", dict(mod.CFG, mirror_src=str(mirror),
                                           return_root=str(scratch))), \
                    contextlib.redirect_stderr(io.StringIO()):
                rows = mod._submit_input_inventory(
                    [str(child.parent), "--input=%s" % child, str(run_input)])
                self.assertEqual(
                    [(row[3], row[0], row[2]) for row in rows],
                    [("mirror", "project/input.json", False),
                     ("scratch", "prepared.bin", False)])
                self.assertEqual(
                    mod._submit_input_inventory([], discovered=[str(child)]),
                    [("project/input.json", str(child.resolve()), False, "mirror")])

    def test_selective_return_filters_include_parents_and_reject_escape(self):
        mod = self.mod
        args = mod._return_filter_args({
            "include_paths": ["results/final.json"],
            "include_prefixes": ["logs/train"],
        })
        self.assertIn("--include=/results/", args)
        self.assertIn("--include=/results/final.json", args)
        self.assertIn("--include=/logs/train.*", args)
        self.assertEqual(args[-1], "--exclude=*")
        with self.assertRaises(ValueError):
            mod._return_filter_args({"include_paths": ["../escape"]})

    def test_environment_drift_says_how_much_it_did_not_list(self):
        import json
        import types

        mod = self.mod
        here = {"pkg%d" % n: "1.0" for n in range(8)}
        there = dict({"pkg%d" % n: "2.0" for n in range(5)}, **{"nersc-pymon": "1"})
        with tempfile.TemporaryDirectory() as root:
            python = Path(root) / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("")
            with _patched(mod, "CFG", dict(mod.CFG, mirror_src=root, mirror_dest="project")), \
                    _patched(mod.subprocess, "run", lambda *a, **k: types.SimpleNamespace(
                        returncode=0, stdout=json.dumps(here))), \
                    _patched(mod, "remote_out", lambda argv: (0, json.dumps(there))):
                _local, remote = mod.check_env_parity()
        self.assertIn("missing: pkg5, pkg6, pkg7;", remote)
        self.assertIn("and 2 more", remote, "five drifted, three named")
        self.assertNotIn("nersc-pymon", remote, "NERSC's own package is not drift")

    def test_environment_parity_is_optional_until_a_project_opts_in(self):
        mod = self.mod
        with tempfile.TemporaryDirectory() as root, \
                _patched(mod, "CFG", dict(mod.CFG, mirror_src=root, env_parity="auto")):
            self.assertFalse(mod.env_parity_enabled())
            python = Path(root) / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("")
            self.assertTrue(mod.env_parity_enabled())
            mod.CFG["env_parity"] = "off"
            self.assertFalse(mod.env_parity_enabled())
            mod.CFG["env_parity"] = "on"
            python.unlink()
            self.assertTrue(mod.env_parity_enabled())

    def test_default_dest_maps_by_parity(self):
        mod = self.mod
        with _patched(mod, "CFG", dict(mod.CFG, scratch="/pscratch/sd/u/user",
                                       return_root="~/returns", scratch_link="scr")):
            base = os.path.expanduser("~/returns")
            self.assertEqual(
                mod.default_dest("/pscratch/sd/u/user/project/runs/foo"),
                base + "/project/runs/foo")
            self.assertEqual(mod.default_dest("scr/project/x"),
                             base + "/project/x")
            self.assertIsNone(mod.default_dest("/global/cfs/cdirs/m0000/y"))
            self.assertIsNone(mod.default_dest("/pscratch/sd/u/user"))
            # Without a scratch_link the home-relative form has no default.
            mod.CFG["scratch_link"] = ""
            self.assertIsNone(mod.default_dest("scr/project/x"))
            # Without a return_root nothing has a default.
            mod.CFG["return_root"] = ""
            self.assertIsNone(mod.default_dest("/pscratch/sd/u/user/a"))

    def test_extract_return_flags(self):
        for argv, left, tracking in (
                (["-q", "shared", "job.sh"], ["-q", "shared", "job.sh"], None),
                (["--return", "--chdir=/p/runs/a", "job.sh"], ["--chdir=/p/runs/a", "job.sh"],
                 {"run_dir": "/p/runs/a", "dest": None, "cleanup": False}),
                (["--cleanup", "--chdir", "/p/runs/b", "job.sh"],
                 ["--chdir", "/p/runs/b", "job.sh"],
                 {"run_dir": "/p/runs/b", "dest": None, "cleanup": True}),
                (["--run-dir=/x", "--dest", "/n/dest", "job.sh"], ["job.sh"],
                 {"run_dir": "/x", "dest": "/n/dest", "cleanup": False})):
            self.assertEqual(self.mod.extract_return_flags(argv), (left, tracking), argv)
        refusal(self.mod.extract_return_flags, ["--return", "job.sh"])

    def test_terminal_states(self):
        mod = self.mod
        for state in ("COMPLETED", "FAILED", "CANCELLED by 1000", "TIMEOUT",
                      "OUT_OF_MEMORY", "NODE_FAIL"):
            self.assertTrue(mod.is_terminal(state), state)
        for state in ("PENDING", "RUNNING", "REQUEUED", "SUSPENDED",
                      "COMPLETING"):
            self.assertFalse(mod.is_terminal(state), state)

    def test_job_id_validation(self):
        mod = self.mod
        for good in ("123", "123_4"):
            self.assertTrue(mod.JOB_ID_RE.match(good), good)
        for bad in ("abc", "123;rm -rf /", "123_", "_4", ""):
            self.assertFalse(mod.JOB_ID_RE.match(bad), bad)

    def test_parse_scontrol(self):
        text = ("JobId=12345678 JobName=smoke\n"
                "   JobState=RUNNING Reason=None Dependency=(null)\n"
                "   RunTime=00:01:02 TimeLimit=00:10:00\n"
                "   WorkDir=/pscratch/sd/u/user/runs/x\n"
                "   StdOut=/pscratch/sd/u/user/runs/x/o.12345678.out\n")
        info = self.mod.parse_scontrol(text)
        self.assertEqual(info["JobState"], "RUNNING")
        self.assertEqual(info["WorkDir"], "/pscratch/sd/u/user/runs/x")
        self.assertTrue(info["StdOut"].endswith(".out"))

    def test_job_states_aggregates_arrays(self):
        with _patched(self.mod, "remote_out", lambda *a, **k: (
                0, "900_1|COMPLETED\n900_2|RUNNING\n901|FAILED\n902_1|COMPLETED\n"
                   "902_2|TIMEOUT\n")):
            states = self.mod._job_states(["900", "901", "902"])
        self.assertEqual(states["900"], "RUNNING",
                         "an array is not terminal until every element is")
        self.assertEqual(states["901"], "FAILED")
        self.assertEqual(states["902"], "TIMEOUT",
                         "a terminal array surfaces its non-COMPLETED element")

    def test_every_sacct_query_names_its_start_time(self):
        """sacct defaults to midnight today without -S, which hides older jobs."""
        mod = load_tool()
        queries = []

        def remote_out(argv, **kwargs):
            if argv[0] == "sacct":
                queries.append(argv)
                return 0, "COMPLETED|00:01:00|/pscratch/sd/u/user/runs/a\n"
            return 1, ""

        with _patched(mod, "remote_out", remote_out):
            state, workdir, _, _ = mod._job_workdir("123")
        self.assertEqual(workdir, "/pscratch/sd/u/user/runs/a")
        self.assertEqual(len(queries), 2)
        for argv in queries:
            self.assertIn("-S", argv)
            self.assertEqual(argv[argv.index("-S") + 1], mod.SACCT_START)

    def test_malformed_queue_entries_are_skipped_not_fatal(self):
        mod = load_tool("scratch = /pscratch/sd/u/user\n")
        listing = ("OK\t1.json\t{\"job\": 1, \"src\": \"/p/a\", \"dest\": \"/d/a\"}\n"
                   "OK\t2.json\t{\"job\": \"2\"}\n"
                   "OK\t3.json\tnot json\n"
                   "OK\t4.json\t[1, 2]\n")
        err = io.StringIO()
        with _patched(mod, "remote_out", lambda *a, **k: (0, listing)), \
             contextlib.redirect_stderr(err):
            entries = mod._read_queue()
        self.assertEqual([(e["job"], e["_file"]) for e in entries], [("1", "1.json")])
        for name in ("2.json", "3.json", "4.json"):
            self.assertIn("skipping malformed queue entry %s" % name, err.getvalue())

    def test_a_stalled_queue_listing_names_the_file_it_stopped_on(self):
        # A file that was answered, even with nonsense, is not the stuck one.
        mod = load_tool("scratch = /pscratch/sd/u/user\n")
        listing = "AT\t1.json\nOK\t1.json\tnot json\nAT\t2.json\n"
        err = io.StringIO()
        with _patched(mod, "remote_out", lambda *a, **k: (124, listing)), \
             contextlib.redirect_stderr(err):
            self.assertEqual(mod._read_queue(), [])
        self.assertIn("stuck on: 2.json", err.getvalue())

    def _read_local_queue(self, readable, fifos=(), unkillable=()):
        """_read_queue's own script, run here over a queue whose *fifos* are
        FIFOs nobody writes, which block a reader the way a read of a file on
        a dead Lustre storage target does, and whose *unkillable* entries are
        read by a stand-in `cat` that no kill of its own stops."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        queue = Path(tmp) / "queue"
        queue.mkdir()
        for name in list(readable) + list(unkillable):
            (queue / ("%s.json" % name)).write_text(
                '{"job": "%s",\n "src": "/p/%s", "dest": "/d/%s"}\n'
                % (name, name, name))
        for name in fifos:
            os.mkfifo(str(queue / ("%s.json" % name)))
        env = dict(os.environ)
        if unkillable:
            # A read in uninterruptible I/O outlives SIGKILL and goes on
            # holding its output open; a process in a session of its own,
            # which outlives killing the reader, stands in for it.
            bindir, pids = Path(tmp) / "bin", Path(tmp) / "pids"
            bindir.mkdir()
            stub = bindir / "cat"
            stub.write_text(
                "#!/bin/sh\n"
                "case \"$2\" in\n"
                "  %s) setsid sleep 30 & echo $! >> %s; wait ;;\n"
                "  *) exec %s \"$@\" ;;\n"
                "esac\n" % ("|".join("%s.json" % n for n in unkillable),
                            shlex.quote(str(pids)), shutil.which("cat")))
            stub.chmod(0o755)
            env["PATH"] = "%s%s%s" % (bindir, os.pathsep, env["PATH"])

            def stop_stand_ins():
                for pid in pids.read_text().split() if pids.exists() else ():
                    with contextlib.suppress(OSError):
                        os.kill(int(pid), signal.SIGKILL)
            self.addCleanup(stop_stand_ins)
        mod = load_tool("scratch = /pscratch/sd/u/user\nreturn_queue = %s\n" % tmp)

        def remote_out(argv, **kwargs):
            # Its own session, so that what it leaves behind, as it does on
            # NERSC (readers still waiting), can be stopped afterwards.
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, env=env,
                                    universal_newlines=True,
                                    start_new_session=True)
            try:
                out, _ = proc.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
                self.fail("the queue listing hung on an unreadable file")
            finally:
                with contextlib.suppress(OSError):
                    os.killpg(proc.pid, signal.SIGKILL)
            return proc.returncode, out

        err = io.StringIO()
        with _patched(mod, "remote_out", remote_out), \
             mock.patch.dict(os.environ, {"NERSC_NO_PROGRESS_SECONDS": "2"}), \
             contextlib.redirect_stderr(err):
            entries = mod._read_queue()
        return [e["job"] for e in entries], err.getvalue()

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_an_unreadable_queue_file_is_passed_over(self):
        jobs, err = self._read_local_queue(["1", "3"], fifos=["2"])
        self.assertEqual(jobs, ["1", "3"])
        self.assertIn("2.json: gave nothing for 1s", err)
        self.assertNotIn("stopped early", err)

    @unittest.skipUnless(shutil.which("bash") and shutil.which("setsid"),
                         "needs bash and setsid")
    def test_a_reader_no_kill_can_stop_is_passed_over_too(self):
        jobs, err = self._read_local_queue(["1", "3"], unkillable=["2"])
        self.assertEqual(jobs, ["1", "3"])
        self.assertIn("2.json: gave nothing for 1s", err)

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_queue_listing_stops_when_storage_looks_down(self):
        jobs, err = self._read_local_queue(["1", "5"], fifos=["2", "3", "4"])
        self.assertEqual(jobs, ["1"])
        self.assertIn("stopped early (rc=75)", err)
        self.assertIn("3 entries in a row gave nothing", err)


class TestSocketDirectory(unittest.TestCase):
    """The control socket's directory must be ours alone.

    Without $XDG_RUNTIME_DIR it is /tmp/nersc-<uid>, which anyone on the node
    could create first; the same check applies to either location.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.runtime = Path(tmp.name)
        env = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.runtime)})
        env.start()
        self.addCleanup(env.stop)
        self.mod = load_tool()
        self.mod._SOCK_DIR = None

    def test_a_missing_directory_is_created_private(self):
        path = self.mod.sock_dir()
        self.assertEqual(path, self.runtime / "nersc")
        self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.mod.sock_path(), path / "mux.sock")

    def test_an_open_directory_of_ours_is_tightened(self):
        (self.runtime / "nersc").mkdir(mode=0o755)
        os.chmod(str(self.runtime / "nersc"), 0o777)
        path = self.mod.sock_dir()
        self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_a_symlink_is_refused_with_a_remedy(self):
        target = self.runtime / "elsewhere"
        target.mkdir(mode=0o700)
        (self.runtime / "nersc").symlink_to(target)
        said = refusal(self.mod.sock_dir)
        self.assertIn("it is a symlink", said)
        self.assertIn("rm -rf %s" % (self.runtime / "nersc"), said)
        self.assertIn("XDG_RUNTIME_DIR", said)

    def test_a_file_is_refused(self):
        (self.runtime / "nersc").write_text("")
        self.assertIn("not a directory", refusal(self.mod.sock_dir))

    def test_someone_elses_directory_is_refused(self):
        (self.runtime / "nersc").mkdir(mode=0o700)
        real = os.getuid()
        with _patched(self.mod.os, "getuid", lambda: real + 1):
            said = refusal(self.mod.sock_dir)
        self.assertIn("belongs to uid %d" % real, said)
        self.assertIsNone(self.mod._SOCK_DIR, "a refused directory is never used")


class TestCleanupPath(unittest.TestCase):
    """One rule decides what --cleanup may delete, at registration and at reap."""

    SCRATCH = "/pscratch/sd/u/user"

    def setUp(self):
        self.mod = load_tool("scratch = %s/\n" % self.SCRATCH)

    def test_only_paths_strictly_inside_scratch_may_be_deleted(self):
        problem = self.mod.cleanup_problem
        for good in (self.SCRATCH + "/runs/a", self.SCRATCH + "/runs/a/",
                     self.SCRATCH + "//runs/./a"):
            self.assertIsNone(problem(good), good)
        for bad in (self.SCRATCH, self.SCRATCH + "/", self.SCRATCH + "/.",
                    self.SCRATCH + "/runs/..", self.SCRATCH + "/../user/runs",
                    self.SCRATCH + "/runs/../../other", self.SCRATCH + "2/runs",
                    "runs/a", "/global/cfs/cdirs/m0000", "", None):
            self.assertIsNotNone(problem(bad), bad)

    def test_without_scratch_nothing_may_be_deleted(self):
        mod = load_tool()
        self.assertIn("no `scratch`", mod.cleanup_problem("/pscratch/sd/u/user/runs/a"))

    def test_registration_refuses_scratch_itself(self):
        with _patched(self.mod, "remote_run", lambda *a, **k: self.fail("registered")):
            said = refusal(self.mod.register_return, "123", "$PSCRATCH/",
                           "/tmp/dest", True)
        self.assertIn("--cleanup is only allowed", said)
        self.assertIn("not inside scratch", said)


class TestReap(unittest.TestCase):
    """cmd_reap's pass: debounce, the cleanup gate, and failure reporting.

    Everything that touches NERSC is faked; the queue, the job states, the
    pull and the remote mv/rm are set per test.
    """

    SCRATCH = "/pscratch/sd/u/user"
    QUEUE = SCRATCH + "/.nersc-return"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.mod = load_tool("scratch = %s\n" % self.SCRATCH)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.clock = FakeClock()
        self.entries = []
        self.states = {}
        self.rechecks = None
        self.pull_ok = True
        self.mv_rc = 0
        self.rm_result = (0, 3)
        self.pulled, self.moved, self.removed = [], [], []
        self.on_pull = None
        mod = self.mod
        for name, value in (
                ("STATE_DIR", Path(tmp.name) / "state"),
                ("time", self.clock),
                ("_read_queue", lambda: [dict(e) for e in self.entries]),
                ("_job_states", self._job_states),
                ("_pull_run_dir", self._pull),
                ("remote_run", self._remote_run),
                ("remote_count", self._remote_count)):
            stack.enter_context(_patched(mod, name, value))

    def _job_states(self, jobs, strict=True):
        if not strict and self.rechecks is not None:
            return {job: self.rechecks[job] for job in jobs if job in self.rechecks}
        return {job: self.states[job] for job in jobs if job in self.states}

    def _pull(self, record, state):
        self.pulled.append(record["job"])
        if self.on_pull:
            self.on_pull(record)
        return self.pull_ok

    def _remote_run(self, argv, **kwargs):
        self.assertEqual(argv[0], "mv", argv)
        self.moved.append(argv[1:])
        return self.mv_rc

    def _remote_count(self, argv):
        self.removed.append(argv)
        return self.rm_result

    def track(self, job, src, cleanup=False):
        self.entries.append({"job": job, "src": src, "dest": "/hub/runs/" + job,
                             "cleanup": cleanup, "_file": job + ".json"})

    def reap(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self.mod.cmd_reap(list(args))
        self.clock.now += 300
        return rc, out.getvalue(), err.getvalue()

    def test_a_job_is_returned_only_on_its_second_terminal_pass(self):
        self.track("101", self.SCRATCH + "/runs/a")
        self.states["101"] = "COMPLETED"
        rc, out, _ = self.reap()
        self.assertEqual(rc, 0)
        self.assertIn("101 newly terminal (COMPLETED); acting next pass", out)
        self.assertEqual(self.pulled, [])
        rc, out, _ = self.reap()
        self.assertEqual(rc, 0)
        self.assertEqual(self.pulled, ["101"])
        self.assertIn("returned 101 (COMPLETED) -> /hub/runs/101\n", out)
        self.assertIn("reap: 1 returned, 0 failed, 0 still running, 1 tracked total", out)
        self.assertEqual(self.moved, [[self.QUEUE + "/queue/101.json",
                                       self.QUEUE + "/done/101.json"]])

    def test_a_job_seen_running_again_starts_its_debounce_over(self):
        self.track("102", self.SCRATCH + "/runs/b")
        self.states["102"] = "TIMEOUT"
        self.reap()
        self.states["102"] = "PENDING"
        rc, out, _ = self.reap()
        self.assertIn("0 returned, 0 failed, 1 still running", out)
        self.states["102"] = "TIMEOUT"
        rc, out, _ = self.reap()
        self.assertIn("102 newly terminal", out)
        self.assertEqual(self.pulled, [])

    def _two_passes(self):
        self.reap()
        return self.reap()

    def test_cleanup_deletes_the_normalised_run_dir_after_retiring_the_entry(self):
        self.track("103", self.SCRATCH + "/runs/./c/", cleanup=True)
        self.states["103"] = "COMPLETED"
        rc, out, err = self._two_passes()
        self.assertEqual(rc, 0)
        self.assertEqual(self.removed, [["rm", "-rfv", "--", self.SCRATCH + "/runs/c"]])
        self.assertIn("returned 103 (COMPLETED) -> /hub/runs/103, scratch cleaned\n", out)
        self.assertIn("removed %s (3 paths)" % (self.SCRATCH + "/runs/c"), err)
        self.assertEqual(len(self.moved), 1)

    def test_cleanup_never_touches_scratch_itself_or_an_escape(self):
        for job, src in (("104", self.SCRATCH + "/"), ("105", self.SCRATCH + "/."),
                         ("106", self.SCRATCH + "/runs/../../../etc")):
            self.track(job, src, cleanup=True)
            self.states[job] = "COMPLETED"
        rc, out, err = self._two_passes()
        self.assertEqual(rc, 0)
        self.assertEqual(self.removed, [])
        self.assertIn("3 returned, 0 failed", out)
        for job in ("104", "105", "106"):
            self.assertIn("not cleaning %s off scratch" % job, err)

    def test_a_failed_delete_is_reported_and_fails_the_pass(self):
        self.track("107", self.SCRATCH + "/runs/d", cleanup=True)
        self.states["107"] = "COMPLETED"
        self.rm_result = (1, 2)
        rc, out, err = self._two_passes()
        self.assertEqual(rc, 1)
        self.assertIn("returned 107 (COMPLETED) -> /hub/runs/107\n", out)
        self.assertIn("returned, but not cleaned off scratch (see above): 107", out)
        self.assertIn("cleaning 107 off scratch FAILED (rm rc=1 after 2 paths)", err)
        self.assertIn("nersc run rm -rf -- %s/runs/d" % self.SCRATCH, err)
        self.assertEqual(len(self.moved), 1, "the return itself is recorded")

    def test_a_failed_move_keeps_the_entry_and_deletes_nothing(self):
        self.track("108", self.SCRATCH + "/runs/e", cleanup=True)
        self.states["108"] = "COMPLETED"
        self.mv_rc = 1
        rc, out, err = self._two_passes()
        self.assertEqual(rc, 1)
        self.assertEqual(self.removed, [])
        self.assertIn("reap: 0 returned, 1 failed", out)
        self.assertIn("failed this pass (kept in the queue, retried next pass): 108", out)
        self.assertIn("could not be moved to done/ (mv rc=1)", err)
        # Still debounced as terminal: the next pass acts at once.
        self.mv_rc = 0
        rc, out, _ = self.reap()
        self.assertEqual(rc, 0)
        self.assertIn("returned 108", out)

    def test_an_unanswered_recheck_keeps_the_entry_queued(self):
        self.track("109", self.SCRATCH + "/runs/f", cleanup=True)
        self.states["109"] = "COMPLETED"
        self.rechecks = {}
        rc, out, err = self._two_passes()
        self.assertEqual(rc, 1)
        self.assertEqual((self.moved, self.removed), ([], []))
        self.assertIn("could not recheck 109", err)

    def test_a_job_requeued_since_the_pass_began_stays_queued(self):
        self.track("110", self.SCRATCH + "/runs/g", cleanup=True)
        self.states["110"] = "TIMEOUT"
        self.rechecks = {"110": "PENDING"}
        rc, out, _ = self._two_passes()
        self.assertEqual(rc, 0)
        self.assertEqual((self.moved, self.removed), ([], []))
        self.assertIn("reap: 0 returned, 0 failed, 1 still running", out)
        # Once it has really finished, it goes through the debounce again and
        # is returned and cleaned.
        self.rechecks = None
        rc, out, _ = self.reap()
        self.assertIn("110 newly terminal", out)
        self.assertEqual(self.moved, [])
        rc, out, _ = self.reap()
        self.assertEqual(rc, 0)
        self.assertIn("returned 110 (TIMEOUT) -> /hub/runs/110, scratch cleaned\n", out)
        self.assertEqual(len(self.moved), 1)
        self.assertEqual(self.removed, [["rm", "-rfv", "--", self.SCRATCH + "/runs/g"]])

    def test_a_failed_pull_is_retried_and_nothing_is_retired(self):
        self.track("111", self.SCRATCH + "/runs/h", cleanup=True)
        self.states["111"] = "FAILED"
        self.pull_ok = False
        rc, out, _ = self._two_passes()
        self.assertEqual(rc, 1)
        self.assertEqual((self.moved, self.removed), ([], []))
        self.assertIn("0 returned, 1 failed", out)

    def test_a_bad_budget_or_unknown_argument_is_refused_without_a_traceback(self):
        self.track("112", self.SCRATCH + "/runs/i")
        self.states["112"] = "COMPLETED"
        for args, said in (
                (["--max-seconds=soon"], "--max-seconds needs a whole number"),
                (["--max-seconds", "soon"], "--max-seconds needs a whole number"),
                (["--lsit"], "usage: nersc reap [--list] [--max-seconds=N]"),
                (["--list", "now"], "usage: nersc reap"), (["--max-seconds"], "usage:"),
                (["540"], "usage:")):
            self.assertIn(said, refusal(self.mod.cmd_reap, args), args)
        self.assertEqual(self.pulled, [])

    def test_the_budget_may_be_given_as_a_separate_argument(self):
        for job in ("115", "116"):
            self.track(job, self.SCRATCH + "/runs/" + job)
            self.states[job] = "COMPLETED"
        self.reap()

        def slow(record):
            self.clock.now += 100
        self.on_pull = slow
        rc, out, _ = self.reap("--max-seconds", "50")
        self.assertEqual(self.pulled, ["115"])
        self.assertIn("time budget spent; remaining jobs next pass", out)
        self.assertIn("reap: 1 returned", out)

    def test_a_long_pass_keeps_its_lock_fresh(self):
        """A lock left untouched for reap_lock_stale_seconds is judged
        abandoned, so a pass refreshes its own after each entry."""
        for job in ("117", "118"):
            self.track(job, self.SCRATCH + "/runs/" + job)
            self.states[job] = "COMPLETED"
        self.reap()
        lock = self.mod.STATE_DIR / "reap.lock.d"
        mtimes = []

        def pull(record):
            mtimes.append(lock.stat().st_mtime)
            os.utime(str(lock), (0, 0))   # as if this pull took hours
        self.on_pull = pull
        self.reap()
        self.assertEqual(len(mtimes), 2)
        self.assertGreater(mtimes[1], time.time() - 60)

    def _ready(self, *jobs):
        for job in jobs:
            self.track(job, self.SCRATCH + "/runs/" + job)
            self.states[job] = "COMPLETED"
        self.reap()

        def slow(record):
            self.clock.now += 3 * 3600
        self.on_pull = slow

    def test_without_a_budget_every_ready_pull_starts(self):
        self._ready("119", "120", "121")
        rc, out, _ = self.reap()
        self.assertEqual(rc, 0)
        self.assertEqual(self.pulled, ["119", "120", "121"])
        self.assertNotIn("time budget spent", out)

    def test_a_zero_budget_is_no_budget(self):
        self._ready("122", "123")
        rc, out, _ = self.reap("--max-seconds=0")
        self.assertEqual(self.pulled, ["122", "123"])
        self.assertNotIn("time budget spent", out)

    def test_a_pass_keeps_its_lock_fresh_inside_one_long_pull(self):
        self._ready("126")
        lock = self.mod.STATE_DIR / "reap.lock.d"
        fresh = []

        def pull(record):
            os.utime(str(lock), (0, 0))   # hours into one pull
            self.mod.ON_TICK()            # what a wait does on each tick
            fresh.append(lock.stat().st_mtime > time.time() - 60)
        self.on_pull = pull
        self.reap()
        self.assertEqual(fresh, [True])
        self.assertIsNone(self.mod.ON_TICK, "nothing is touched after the pass")

    def test_a_stopped_pass_saves_what_it_saw_and_lets_go(self):
        """A pass a scheduler always stops part way must still get somewhere:
        the first sighting of a finished job survives the stop."""
        self.track("124", self.SCRATCH + "/runs/124")
        self.states["124"] = "COMPLETED"
        self.reap()
        self.track("125", self.SCRATCH + "/runs/125")
        self.states["125"] = "COMPLETED"
        self.entries.reverse()   # 125, newly terminal, comes first

        def stopped(record):
            raise self.mod.Terminated(15)
        self.on_pull = stopped
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(self.mod.Terminated):
                self.mod.cmd_reap([])
        self.clock.now += 300
        self.assertIn("125 newly terminal", out.getvalue())
        self.assertIn("reap: 0 returned, 0 failed", out.getvalue())
        self.assertIn("stopped by SIGTERM part way through", out.getvalue())
        self.assertFalse((self.mod.STATE_DIR / "reap.lock.d").exists())
        seen = json.loads((self.mod.STATE_DIR / "reap-seen.json").read_text())
        self.assertEqual(sorted(seen), ["124.json", "125.json"])
        self.on_pull = None
        rc, out, _ = self.reap()
        self.assertEqual(rc, 0)
        self.assertIn("reap: 2 returned", out)

    def test_a_job_whose_pull_failed_is_tried_after_the_others(self):
        for job in ("201", "202", "203"):
            self.track(job, self.SCRATCH + "/runs/" + job)
            self.states[job] = "RUNNING"
        self.states["201"] = "COMPLETED"
        self.reap()
        broken = {"201"}

        def pull(record):
            self.pull_ok = record["job"] not in broken
        self.on_pull = pull
        self.states["202"] = self.states["203"] = "COMPLETED"
        rc, out, _ = self.reap()
        self.assertEqual(rc, 1)
        self.assertEqual(self.pulled, ["201"])
        self.pulled = []
        rc, out, _ = self.reap()
        self.assertEqual(self.pulled, ["202", "203", "201"])
        self.assertIn("/hub/runs/201  [last pull failed]\n", out)
        broken.clear()
        self.pulled = []
        rc, out, _ = self.reap()
        self.assertEqual(self.pulled, ["201"])
        failed = self.mod.STATE_DIR / "reap-failed.json"
        self.assertEqual(json.loads(failed.read_text()), {})

    def test_failure_marks_leave_with_their_entries(self):
        self.track("204", self.SCRATCH + "/runs/a")
        self.states["204"] = "COMPLETED"
        self.pull_ok = False
        self._two_passes()
        failed = self.mod.STATE_DIR / "reap-failed.json"
        self.assertEqual(sorted(json.loads(failed.read_text())), ["204.json"])
        self.entries = []
        self.track("205", self.SCRATCH + "/runs/b")
        self.reap()
        self.assertEqual(json.loads(failed.read_text()), {})

    def track_unknown(self, job, days_ago):
        self.clock.now = max(self.clock.now, 1790000000.0)   # October 2026
        self.track(job, self.SCRATCH + "/runs/" + job)
        if days_ago is not None:
            self.entries[-1]["tracked_at"] = self.clock.now - days_ago * 86400

    def test_reap_names_jobs_accounting_has_forgotten(self):
        self.track_unknown("301", 30)
        self.track_unknown("302", 1)      # may not have reached sacct yet
        self.track_unknown("303", None)   # does not say when it was tracked
        self.track_unknown("304", 30)
        self.states["304"] = "PENDING"
        rc, out, _ = self.reap()
        self.assertIn("1 tracked job unknown to Slurm's accounting for over "
                      "7 days; `nersc untrack --forgotten` sets it aside", out)
        _, listed, _ = self.reap("--list")
        self.assertIn("1 tracked job unknown", listed)

    def untrack(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self.mod.cmd_untrack(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_untrack_forgotten_moves_only_old_entries_accounting_lacks(self):
        self.track_unknown("301", 30)
        self.track_unknown("302", 1)
        self.track_unknown("303", None)
        self.track_unknown("304", 30)
        self.states["304"] = "PENDING"
        self.rm_result = (0, 1)
        rc, out, _ = self.untrack("--forgotten")
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.removed), 1)
        argv = self.removed[0]
        self.assertEqual(argv[:2], ["bash", "-c"])
        self.assertEqual(argv[3:], ["untrack", self.QUEUE, "301.json"])
        self.assertIn("untracking 301  %s/runs/301 -> /hub/runs/301  (tracked "
                      % self.SCRATCH, out)
        self.assertIn("2 more unknown to accounting were tracked in the last 7 "
                      "days, or do not say when", out)
        self.assertIn("untracked 1 of 1 entry into %s/untracked/" % self.QUEUE, out)
        self.assertFalse((self.mod.STATE_DIR / "reap.lock.d").exists())

    def test_untrack_by_name_and_dry_run(self):
        self.track_unknown("401", None)
        self.track_unknown("402", None)
        rc, out, err = self.untrack("--dry-run", "401", "499")
        self.assertEqual(rc, 1)
        self.assertIn("would untrack 401", out)
        self.assertIn("499 is not in the return queue", err)
        self.assertEqual(self.removed, [])
        self.rm_result = (0, 1)
        rc, out, _ = self.untrack("402")
        self.assertEqual(rc, 0)
        self.assertEqual(self.removed[0][3:], ["untrack", self.QUEUE, "402.json"])

    def test_untrack_refuses_bad_arguments_and_a_running_reap(self):
        for args in ([], ["--forgotten", "401"], ["--all"]):
            self.assertIn("untrack", refusal(self.mod.cmd_untrack, args))
        self.mod.STATE_DIR.mkdir(parents=True, exist_ok=True)
        (self.mod.STATE_DIR / "reap.lock.d").mkdir()
        self.assertIn("a reap pass is running",
                      refusal(self.mod.cmd_untrack, ["401"]))
        self.assertEqual(self.removed, [])

    @unittest.skipUnless(
        subprocess.run(["mv", "--version"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL).returncode == 0,
        "needs GNU mv, as NERSC has")
    def test_untrack_moves_files_aside_and_keeps_an_earlier_one(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        queue = Path(tmp) / "queue"
        queue.mkdir()
        (Path(tmp) / "untracked").mkdir()
        (Path(tmp) / "untracked" / "-1.json").write_text("old\n")
        for name in ("-1.json", "2 x.json", "3.json"):
            (queue / name).write_text("new\n")

        def remote_count(argv):
            done = subprocess.run(argv, stdout=subprocess.PIPE,
                                  universal_newlines=True, timeout=30)
            return done.returncode, done.stdout.count("\n")

        entries = [{"_file": name} for name in ("-1.json", "2 x.json")]
        with _patched(self.mod, "remote_count", remote_count):
            self.assertEqual(self.mod._move_aside(tmp, entries), 2)
        self.assertEqual(sorted(p.name for p in queue.iterdir()), ["3.json"])
        aside = Path(tmp) / "untracked"
        self.assertEqual(sorted(p.name for p in aside.iterdir()),
                         ["-1.json", "-1.json.~1~", "2 x.json"])
        self.assertEqual((aside / "-1.json.~1~").read_text(), "old\n")


class TestScratchStorageCheck(unittest.TestCase):
    """`nersc doctor` naming a Lustre storage target that stopped answering."""

    HEAD = ("UUID 1K-blocks Used Available Use% Mounted on\n"
            "scratch-MDT0000_UUID 1 1 1 1% /pscratch[MDT:0]\n"
            "scratch-OST003b_UUID 1 1 1 71% /pscratch[OST:59]\n")

    def setUp(self):
        self.mod = load_tool("scratch = /pscratch/sd/u/user\n")

    def test_every_target_answering(self):
        line, bad = self.mod.describe_lfs_df(
            self.HEAD + "scratch-OST003c_UUID 1 1 1 69% /pscratch[OST:60]\n"
            "\nfilesystem_summary: 1 1 1 70% /pscratch\n", 0)
        self.assertEqual((line, bad), ("scratch storage: every target answered", 0))

    def test_a_target_that_stopped_answering_is_named(self):
        line, bad = self.mod.describe_lfs_df(
            self.HEAD + "scratch-OST003c_UUID 1 1 1 69% /pscratch[OST:60]\n", 137)
        self.assertEqual(bad, 1)
        self.assertIn("OST 61 is NOT ANSWERING (lfs df stopped after OST 60", line)
        line, bad = self.mod.describe_lfs_df("", 137)
        self.assertIn("a storage target is NOT ANSWERING (lfs df stopped before "
                      "printing any target", line)

    def test_inactive_targets_are_listed_and_a_failed_check_is_not_a_problem(self):
        line, bad = self.mod.describe_lfs_df(
            self.HEAD + "scratch-OST000a_UUID : inactive device\n"
            "filesystem_summary: 1 1 1 70% /pscratch\n", 0)
        self.assertEqual((line, bad), (
            "scratch storage: every target answered; inactive: OST 10", 0))
        self.assertEqual(self.mod.describe_lfs_df("lfs: error\n", 2),
                         ("scratch storage: not checked (lfs df rc=2)", 0))

    def test_the_check_reads_its_own_exit_code(self):
        said = self.HEAD + "LFSDF_RC\t137\n"
        with _patched(self.mod, "remote_out", lambda argv, **kw: (0, said)):
            line, bad = self.mod.check_scratch_storage()
        self.assertEqual(bad, 1)
        self.assertIn("OST 60 is NOT ANSWERING", line)
        with _patched(self.mod, "remote_out", lambda argv, **kw: (3, "")):
            self.assertEqual(self.mod.check_scratch_storage(),
                             ("scratch storage: not checked (NERSC has no lfs)", 0))


class TestStoppingAndWaiting(unittest.TestCase):
    """How the companion waits on its children, and how it stops them."""

    def setUp(self):
        self.mod = load_tool()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(_patched(self.mod, "PROGRESS_TICK_SECONDS", 0.1))
        self.stack.enter_context(_patched(self.mod, "ON_TICK", None))
        self.err = io.StringIO()
        self.stack.enter_context(contextlib.redirect_stderr(self.err))

    def python(self, code):
        return [sys.executable, "-c", code]

    def test_a_long_child_is_ticked_through(self):
        ticks = []
        self.mod.ON_TICK = lambda: ticks.append(1)
        self.assertEqual(self.mod._run_child(self.python(
            "import time; time.sleep(0.3); raise SystemExit(3)")), 3)
        self.assertGreaterEqual(len(ticks), 2)

    def test_a_stopped_wait_asks_the_child_to_stop(self):
        # rsync keeps its partial file (or removes its temporary one) on
        # SIGTERM; on SIGKILL it can do neither.
        with tempfile.TemporaryDirectory() as tmp:
            said = Path(tmp) / "said"
            code = ("import signal, sys, time\n"
                    "def stop(*_):\n"
                    "    open(sys.argv[1], 'w').write('asked to stop')\n"
                    "    raise SystemExit(20)\n"
                    "signal.signal(signal.SIGTERM, stop)\n"
                    "open(sys.argv[1], 'w').write('ready')\n"
                    "time.sleep(30)\n")

            def interrupt():
                if said.exists():
                    raise KeyboardInterrupt
            self.mod.ON_TICK = interrupt
            with self.assertRaises(KeyboardInterrupt):
                self.mod._run_child(self.python(code) + [str(said)])
            self.assertEqual(said.read_text(), "asked to stop")

    def test_a_step_that_closes_its_output_is_waited_for(self):
        rc, _ = self.mod._stream(self.python(
            "import os, time\n"
            "print('working', flush=True)\n"
            "os.close(1); os.close(2)\n"
            "time.sleep(0.3)\n"
            "raise SystemExit(3)\n"), "closes early", capture=True)
        self.assertEqual(rc, 3)
        self.assertIn("waiting on closes early", self.err.getvalue())

    def test_a_step_with_closed_output_is_still_abandoned_when_silent(self):
        with _patched(os, "environ", dict(os.environ, NERSC_NO_PROGRESS_SECONDS="0.2")):
            rc, _ = self.mod._stream(self.python(
                "import os, time\n"
                "os.close(1); os.close(2)\n"
                "time.sleep(30)\n"), "never ends", capture=False)
        self.assertEqual(rc, 124)
        said = self.err.getvalue()
        self.assertIn("NO PROGRESS", said)
        self.assertIn("may still be running on NERSC", said)

    def test_a_captured_stall_does_not_warn_about_the_remote_side(self):
        self.mod._stall_report("squeue --me", 200, capture=True)
        self.assertNotIn("may still be running", self.err.getvalue())

    def test_a_terminating_signal_stops_the_whole_step(self):
        # The step is held here: dropped, a Popen reaps its child itself, and
        # whether a waitpid of our own came first would be a race.
        started, real = [], subprocess.Popen

        def popen(*args, **kwargs):
            started.append(real(*args, **kwargs))
            return started[-1]

        def stop():
            if said:
                raise self.mod.Terminated(15)
        said = []
        self.mod.ON_TICK = stop
        with _patched(self.mod.subprocess, "Popen", popen), \
                self.assertRaises(self.mod.Terminated):
            self.mod._stream(self.python(
                "import time\n"
                "print('working', flush=True)\n"
                "time.sleep(30)\n"), "sleeps", capture=True, on_output=said.append)
        self.assertEqual(started[0].wait(timeout=10), -signal.SIGKILL)

    def test_the_entry_point_unwinds_on_sigterm(self):
        script = ("import importlib.machinery, importlib.util, sys, time\n"
                  "loader = importlib.machinery.SourceFileLoader('nersctool', %r)\n"
                  "spec = importlib.util.spec_from_loader('nersctool', loader)\n"
                  "mod = importlib.util.module_from_spec(spec)\n"
                  "loader.exec_module(mod)\n"
                  "def main(argv):\n"
                  "    try:\n"
                  "        print('ready', flush=True)\n"
                  "        time.sleep(30)\n"
                  "    finally:\n"
                  "        print('cleaned up', flush=True)\n"
                  "mod.main = main\n"
                  "sys.exit(mod._entry([]))\n" % str(TOOL))
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, NERSC_CONFIG=str(Path(tmp) / "config"))
            child = subprocess.Popen([sys.executable, "-c", script], env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     universal_newlines=True)
            self.assertEqual(child.stdout.readline().strip(), "ready")
            child.send_signal(signal.SIGTERM)
            out, err = child.communicate(timeout=20)
        self.assertEqual(out.strip(), "cleaned up")
        self.assertIn("nersc: stopped by SIGTERM", err)
        self.assertEqual(child.returncode, 128 + signal.SIGTERM)

    def test_an_ignored_hangup_stays_ignored(self):
        before = {signum: signal.getsignal(signum)
                  for signum in (signal.SIGTERM, signal.SIGHUP)}
        for signum, handler in before.items():
            self.addCleanup(signal.signal, signum, handler)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        self.mod.unwind_on_signals()
        self.assertEqual(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)
        self.assertIsNot(signal.getsignal(signal.SIGTERM), before[signal.SIGTERM])


class TestReapLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mod = load_tool()
        self.mod.STATE_DIR = Path(self.tmp.name)
        self.lock = Path(self.tmp.name) / "reap.lock.d"

    def age(self, seconds):
        old = time.time() - seconds
        os.utime(str(self.lock), (old, old))

    def test_a_live_lock_is_honoured(self):
        self.assertEqual(self.mod._reap_lock(), self.lock)
        self.assertIsNone(self.mod._reap_lock())

    def test_a_stale_lock_is_broken_and_taken(self):
        self.lock.mkdir()
        self.age(2 * 3600)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.mod._reap_lock(), self.lock)
        self.assertEqual([p.name for p in Path(self.tmp.name).iterdir()], ["reap.lock.d"])

    def test_breaking_never_steals_a_lock_another_pass_just_took(self):
        """Another pass breaks the stale lock and takes a fresh one between
        this pass's staleness check and its rename."""
        self.lock.mkdir()
        self.age(2 * 3600)
        real_rename = os.rename
        lock = str(self.lock)

        def racing_rename(src, dst):
            if src == lock:
                os.rmdir(lock)
                os.mkdir(lock)   # the other pass's fresh lock
            real_rename(src, dst)

        with _patched(self.mod.os, "rename", racing_rename):
            self.assertIsNone(self.mod._reap_lock())
        self.assertTrue(self.lock.is_dir(), "the other pass keeps its lock")
        self.assertEqual([p.name for p in Path(self.tmp.name).iterdir()], ["reap.lock.d"])


class TestPeek(unittest.TestCase):
    """peek's remote script, run here in bash against a scratch run dir."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workdir = Path(self.tmp.name) / "run dir"
        self.workdir.mkdir()
        self.mod = load_tool()
        self.scripts = []

    def peek(self, *args, stdout_path=None):
        def remote_run(argv, **kwargs):
            self.assertEqual(argv[:2], ["bash", "-c"])
            self.scripts.append(argv[2])
            return 0

        info = ("RUNNING", str(self.workdir), stdout_path, "runtime 1/10 on nid1")
        with _patched(self.mod, "_job_workdir", lambda job: info), \
             _patched(self.mod, "remote_run", remote_run), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.mod.cmd_peek(list(args)), 0)
        proc = subprocess.run(["bash", "-c", self.scripts[-1]], cwd=self.tmp.name,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True)
        return proc.stdout

    def test_the_banner_carries_any_file_name_literally(self):
        for name in ("it's.log", "$(touch pwned).log", "a b`id`.log"):
            (self.workdir / name).write_text("one\ntwo\n")
            out = self.peek("123", name, "-n", "1")
            self.assertIn("----- %s/%s (last 1 lines) -----" % (self.workdir, name), out)
            self.assertTrue(out.endswith("two\n"), out)
        self.assertFalse((self.workdir / "pwned").exists())
        self.assertFalse((Path(self.tmp.name) / "pwned").exists())

    def test_a_job_stdout_path_is_tailed(self):
        log = self.workdir / "o.123.out"
        log.write_text("x\ny\n")
        out = self.peek("123", stdout_path=str(log))
        self.assertIn("----- %s (last 40 lines) -----" % log, out)

    def test_a_bad_line_count_is_refused_without_a_traceback(self):
        self.assertIn("-n needs a whole number", refusal(self.mod.cmd_peek,
                                                         ["123", "-n", "lots"]))


class TestRsyncArguments(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_tool("user = user\nscratch = /pscratch/sd/u/user\n")
        cls.mod._RSYNC_HELP[:] = ["  --partial-dir=DIR  put a partially ...\n"]

    def test_option_values_are_never_taken_for_paths(self):
        paths = ["src", "dst"]
        for options in (["--exclude", "*.tmp", "-av"], ["--exclude=*.tmp", "-e", "ssh -x"],
                        ["-avf", "- *.o"], ["-f- *.o"], ["-@", "3", "-a@", "3"],
                        ["--zl", "9", "--zc", "zstd", "--cc", "xxh64", "--log-format",
                         "%n", "--early-input", "in.txt"]):
            self.assertEqual(self.mod.split_rsync_args(options + paths), (options, paths))
        self.assertEqual(self.mod.split_rsync_args(["-a", "--", "-odd", "dst"]),
                         (["-a"], ["-odd", "dst"]))

    def test_dry_run_is_recognised_in_every_spelling(self):
        dry = self.mod.rsync_dry_run
        for args in (["-n"], ["--dry-run"], ["-avn"], ["-na"], ["-v", "-n"]):
            self.assertTrue(dry(args), args)
        for args in ([], ["-av"], ["--exclude", "-n"], ["-e", "ssh -n"], ["-fn"]):
            self.assertFalse(dry(args), args)

    def test_push_passes_separated_option_values_through(self):
        mod = self.mod
        seen = []
        with _patched(mod, "require_cert", lambda: None), \
             _patched(mod, "ensure_known_hosts", lambda: None), \
             _patched(mod, "pick_dtn", lambda: "dtn01.nersc.gov"), \
             _patched(mod, "_run_child", lambda argv: seen.append(argv) or 0):
            self.assertEqual(mod.rsync_transfer(
                ["--exclude", "*.tmp", "local/", "$PSCRATCH/in/"], up=True), 0)
        (argv,) = seen
        self.assertEqual(argv[-4:], ["--exclude", "*.tmp", "local/",
                                     "user@dtn01.nersc.gov:/pscratch/sd/u/user/in/"])

    def test_without_a_dtn_a_transfer_rides_the_master_with_its_credentials(self):
        # So that a master gone since ensure_master(), or out of sessions,
        # means a direct connection instead of a refused one.
        mod = self.mod
        seen = []
        with _patched(mod, "require_cert", lambda: None), \
             _patched(mod, "ensure_known_hosts", lambda: None), \
             _patched(mod, "ensure_master", lambda **k: None), \
             _patched(mod, "pick_dtn", lambda: None), \
             _patched(mod, "_run_child", lambda argv: seen.append(argv) or 0), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(mod.rsync_transfer(["local/", "in/"], up=True), 0)
        (argv,) = seen
        shell = shlex.split(argv[argv.index("-e") + 1])
        self.assertEqual(shell[:len(mod.base_ssh())], mod.base_ssh())
        self.assertIn("ControlMaster=auto", shell)
        self.assertNotIn(mod.target(), shell, "rsync adds the host itself")
        self.assertEqual(argv[-1], "%s:in/" % mod.target())


class TestConfigVerb(unittest.TestCase):
    def test_every_setting_is_shown_with_where_it_came_from(self):
        text = "user = someone\nbogus = 1\n# pool = commented.example\n"
        mod = load_tool(text)
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config"
            config.write_text(text)
            with _patched(mod, "CONFIG_PATH", config), contextlib.redirect_stdout(out):
                self.assertEqual(mod.main(["config"]), 0)
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], "config: %s" % config)
        self.assertIn("  user = someone", lines)
        self.assertIn("  pool = perlmutter.nersc.gov  (default)", lines)
        self.assertIn("  bogus = 1  (not a setting this tool reads)", lines)
        shown = [line.split(" = ")[0].strip() for line in lines[1:]]
        self.assertEqual(shown[:len(mod.DEFAULTS)], list(mod.DEFAULTS))



class TestTheCompanionsLimits(unittest.TestCase):
    """Every wait the companion gives up on is a config key, and connections
    are tried again by the rule the workstation's reconnects follow."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.mod = load_tool("user = user\n")
        # The rsync here, as far as --partial-dir goes, whatever is installed.
        self.mod._RSYNC_HELP[:] = ["  --partial-dir=DIR  put a partially ...\n"]
        self.clock = FakeClock()
        self.slept = self.clock.slept

    def configure(self, **values):
        for key, value in values.items():
            self.mod.CFG[key] = value

    def test_the_rule_is_the_workstations_own(self):
        from clustertool.backoff import FailureMemory

        here = self.mod.FailureMemory(300, 3, 2, 60)
        there = FailureMemory(300, limit=3, delay=2, delay_max=60)
        for healthy in (0, 0, 10, 400, 0, 0, 0, 5000, 0) + (0,) * 20:
            here.failed(healthy)
            there.failed(healthy)
            self.assertAlmostEqual(here.score, there.score)
            self.assertAlmostEqual(here.wait(), there.wait())
            self.assertEqual(here.exhausted, there.exhausted)
            self.assertEqual(here.fresh, there.fresh)

    def test_a_number_that_is_not_one_is_said_once_and_the_default_used(self):
        self.configure(connect_timeout="7")
        self.assertEqual(self.mod.cfg_number("connect_timeout"), 7)
        self.assertIn("ConnectTimeout=7", self.mod.base_ssh())
        self.configure(connect_timeout="soon")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.mod.cfg_number("connect_timeout"), 25)
            self.assertEqual(self.mod.cfg_number("connect_timeout"), 25)
        self.assertEqual(err.getvalue().count("connect_timeout = 'soon'"), 1)

    def test_the_silence_window_is_configured_and_the_environment_wins(self):
        self.configure(no_progress_seconds="42")
        with _patched(os, "environ", {k: v for k, v in os.environ.items()
                                      if k != "NERSC_NO_PROGRESS_SECONDS"}):
            self.assertEqual(self.mod.no_progress_seconds(), 42)
            self.assertEqual(self.mod.rsync_stall_flag(), "--timeout=42")
        with _patched(os, "environ", dict(os.environ, NERSC_NO_PROGRESS_SECONDS="9")):
            self.assertEqual(self.mod.no_progress_seconds(), 9)

    def test_a_dtn_is_given_the_configured_time_to_answer(self):
        self.configure(dtn_probe_timeout="3")
        asked = []

        def connect(address, timeout):
            asked.append(timeout)
            raise OSError("no")

        with _patched(self.mod.socketlib, "create_connection", connect):
            self.assertFalse(self.mod._tcp_ok("dtn01.nersc.gov"))
        self.assertEqual(asked, [3])

    # --- opening the connection ---------------------------------------------
    def opening(self, attempts):
        """ensure_master() over fake attempts, each what ssh logged (None: up)."""
        attempts = list(attempts)
        tried = []

        def attempt(_log):
            tried.append(1)
            return attempts.pop(0)

        err = io.StringIO()
        with _patched(self.mod, "master_alive", lambda: False), \
                _patched(self.mod, "require_cert", lambda: None), \
                _patched(self.mod, "ensure_known_hosts", lambda: None), \
                _patched(self.mod, "sock_dir", lambda: self.root), \
                _patched(self.mod, "_open_master", attempt), \
                _patched(self.mod, "time", self.clock), \
                contextlib.redirect_stderr(err):
            try:
                self.mod.ensure_master()
                got = 0
            except SystemExit as exc:
                got = exc
        return got, err.getvalue(), len(tried)

    def test_a_connection_that_fails_is_tried_again_after_longer_waits(self):
        got, err, tried = self.opening(["Connection timed out", "", None])
        self.assertEqual(got, 0)
        self.assertEqual(tried, 3)
        self.assertEqual(self.slept, [2, 4])
        self.assertIn("could not open a connection to NERSC (Connection timed "
                      "out); trying again in 2s", err)

    def test_a_connection_that_keeps_failing_ends_it(self):
        self.configure(connect_retries="3")
        got, err, tried = self.opening(["Connection refused"] * 10)
        self.assertIsInstance(got, SystemExit)
        self.assertEqual(tried, 4)
        self.assertEqual(self.slept, [2, 4, 8])
        self.assertIn("after 4 tries in quick succession", err)
        self.assertIn("connect_retries (3)", err)

    def test_a_refused_connection_is_not_tried_again(self):
        got, err, tried = self.opening(["user@perlmutter: Permission denied "
                                        "(publickey).", None])
        self.assertIsInstance(got, SystemExit)
        self.assertEqual(tried, 1)
        self.assertIn("NERSC refused the connection", err)

    def fake_ssh(self, script):
        """An ssh first on PATH, running *script* with $log the -E log."""
        ssh = self.root / "bin" / "ssh"
        ssh.parent.mkdir(exist_ok=True)
        ssh.write_text('#!/bin/sh\n'
                       'for arg in "$@"; do [ "$prev" = -E ] && log=$arg; prev=$arg; done\n'
                       + script)
        ssh.chmod(0o755)
        return {"PATH": "%s:%s" % (ssh.parent, os.environ["PATH"])}

    def test_a_refusal_is_found_among_what_ssh_logged_after_it(self):
        # sshd's MaxAuthTries: ssh logs the refusal, then that it was
        # disconnected, and it is the refusal that counts.
        log = self.root / "master.log"
        env = self.fake_ssh(
            'echo "Received disconnect from 128.55.1.1 port 22:2: Too many '
            'authentication failures" >> "$log"\n'
            'echo "Disconnected from 128.55.1.1 port 22" >> "$log"\nexit 255\n')
        self.configure(key=str(self.root / "k"))
        with mock.patch.dict(os.environ, env), \
                _patched(self.mod, "master_alive", lambda: False), \
                _patched(self.mod, "time", self.clock):
            said = self.mod._open_master(log)
        self.assertEqual(said, "Received disconnect from 128.55.1.1 port 22:2: Too "
                               "many authentication failures")
        got, err, tried = self.opening([said, None])
        self.assertIsInstance(got, SystemExit)
        self.assertEqual(tried, 1)
        self.assertIn("NERSC refused the connection: Received disconnect", err)

    def test_a_refusal_through_a_real_rsync_is_not_resumed(self):
        # The reviewer's fake: ssh refuses (exit 255), rsync reports it.
        if not shutil.which("rsync"):
            self.skipTest("no rsync here")
        env = self.fake_ssh(
            'echo "user@dtn01.nersc.gov: Permission denied (publickey)." >> "$log"\n'
            'exit 255\n')
        self.configure(key=str(self.root / "k"), dtns="dtn01.nersc.gov")
        (self.root / "local").mkdir()
        err = io.StringIO()
        said = []

        def run(argv):
            # The real rsync, with what it says kept out of the test's output.
            proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  universal_newlines=True, timeout=60)
            said.append(proc.stderr)
            return proc.returncode

        with mock.patch.dict(os.environ, env), \
                _patched(self.mod, "_run_child", run), \
                _patched(self.mod, "require_cert", lambda: None), \
                _patched(self.mod, "ensure_known_hosts", lambda: None), \
                _patched(self.mod, "describe_cert", lambda: "expired"), \
                _patched(self.mod, "pick_dtn", lambda: "dtn01.nersc.gov"), \
                _patched(self.mod, "sock_dir", lambda: self.root), \
                _patched(self.mod, "time", self.clock), \
                contextlib.redirect_stderr(err):
            code = self.mod.rsync_transfer([str(self.root / "local") + "/", "in/"],
                                           up=True)
        self.assertIn(code, self.mod.RSYNC_CONNECTION_LOST)
        self.assertEqual(len(said), 1, "not resumed")
        self.assertIn("rsync", said[0])
        self.assertIn("nersc: push: NERSC refused the connection: "
                      "user@dtn01.nersc.gov: Permission denied (publickey).",
                      err.getvalue())

    def test_one_attempt_is_what_ssh_logged_this_time(self):
        log = self.root / "master.log"
        log.write_text("an old failure\n")
        args = self.root / "args"
        # The fake ssh is found first, and nothing here reaches the network.
        env = dict(self.fake_ssh('printf "%s\\n" "$@" > "$ARGS"\n'
                                 'echo "Permission denied (publickey)." >> "$log"\n'
                                 'exit 255\n'), ARGS=str(args))
        self.configure(alive_interval="11", alive_count_max="4", key=str(self.root / "k"))
        with mock.patch.dict(os.environ, env), \
                _patched(self.mod, "master_alive", self.fail):
            said = self.mod._open_master(log)
        self.assertEqual(said, "Permission denied (publickey).")
        sent = args.read_text().splitlines()
        self.assertIn("ServerAliveInterval=11", sent)
        self.assertIn("ServerAliveCountMax=4", sent)

    # --- transfers ----------------------------------------------------------
    def picker(self):
        """A pick_dtn that answers dtn01, dtn02, ... in turn, noting in
        self.avoided which one each pick was asked to avoid."""
        self.avoided = []
        dtns = iter(["dtn01.nersc.gov", "dtn02.nersc.gov"] * 20)

        def pick(avoid=None):
            self.avoided.append(avoid)
            return next(dtns)
        return pick

    def transferring(self, runs, args=("local/", "in/"), up=True):
        """rsync_transfer() over fake rsync runs, each (exit, seconds) or
        (exit, seconds, what ssh logs)."""
        runs = list(runs)
        seen = []

        def run(argv):
            code, seconds, *said = runs.pop(0)
            seen.append(argv)
            self.clock.now += seconds
            shell = shlex.split(argv[argv.index("-e") + 1])
            with open(shell[shell.index("-E") + 1], "a") as log:
                log.writelines(line + "\n" for line in said)
            return code

        err = io.StringIO()
        with _patched(self.mod, "require_cert", lambda: None), \
                _patched(self.mod, "ensure_known_hosts", lambda: None), \
                _patched(self.mod, "describe_cert", lambda: "valid for 11h"), \
                _patched(self.mod, "pick_dtn", self.picker()), \
                _patched(self.mod, "sock_dir", lambda: self.root), \
                _patched(self.mod, "_run_child", run), \
                _patched(self.mod, "time", self.clock), \
                contextlib.redirect_stderr(err):
            code = self.mod.rsync_transfer(list(args), up=up)
        return code, err.getvalue(), seen

    def test_a_transfer_that_loses_its_connection_is_resumed(self):
        code, err, seen = self.transferring([
            (255, 60, "Connection to dtn01.nersc.gov closed by remote host."),
            (12, 60), (0, 60)])
        self.assertEqual(code, 0)
        self.assertEqual(len(seen), 3)
        self.assertEqual(seen[1][-1], "user@dtn02.nersc.gov:in/",
                         "each run picks its DTN again")
        self.assertEqual(self.avoided, [None, "dtn01.nersc.gov", "dtn02.nersc.gov"],
                         "the one that dropped is tried last")
        # What ssh said comes before what the companion makes of it.
        self.assertIn("Connection to dtn01.nersc.gov closed by remote host.\n"
                      "nersc: push: rsync lost its connection (exit 255); "
                      "resuming in 2s", err)

    def test_drops_spread_over_a_long_transfer_never_add_up(self):
        code, err, seen = self.transferring([(255, 7200)] * 12 + [(0, 1)])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(seen), 13)

    def test_a_transfer_whose_connection_will_not_stay_up_ends(self):
        code, err, seen = self.transferring([(255, 1)] * 10)
        self.assertEqual(code, 255)
        self.assertEqual(len(seen), 3)
        self.assertIn("3 times in quick succession", err)

    def test_a_stall_or_another_failure_is_not_retried(self):
        for status in (30, 23, 1):
            with self.subTest(status=status):
                code, _err, seen = self.transferring([(status, 1), (0, 1)])
                self.assertEqual(code, status)
                self.assertEqual(len(seen), 1)

    def test_pick_dtn_puts_the_one_to_avoid_last(self):
        self.configure(dtns="dtn01 dtn02 dtn03")
        asked = []

        def answers(host, port=22, timeout=None):
            asked.append(host)
            return True

        with _patched(self.mod, "STATE_DIR", self.root), \
                _patched(self.mod, "_tcp_ok", answers):
            (self.root / "dtn").write_text("dtn02\n")
            self.assertEqual(self.mod.pick_dtn(), "dtn02")
            self.assertEqual(self.mod.pick_dtn("dtn02"), "dtn01")
            self.assertEqual((self.root / "dtn").read_text(), "dtn01\n")
            with _patched(self.mod, "_tcp_ok",
                          lambda host, port=22, timeout=None: host == "dtn01"):
                self.assertEqual(self.mod.pick_dtn("dtn01"), "dtn01",
                                 "the one to avoid, when it is all that answers")

    def test_a_refusal_seen_through_rsync_is_not_resumed(self):
        # ssh said Permission denied, and rsync reported its stream cut (12).
        code, err, seen = self.transferring([
            (12, 1, "user@dtn01.nersc.gov: Permission denied (publickey)."),
            (0, 1)])
        self.assertEqual(code, 12)
        self.assertEqual(len(seen), 1)
        self.assertIn("user@dtn01.nersc.gov: Permission denied (publickey).\n"
                      "nersc: push: NERSC refused the connection: "
                      "user@dtn01.nersc.gov: Permission denied (publickey).", err)
        self.assertIn("certificate: valid for 11h", err)
        self.assertNotIn("resuming", err)
        self.assertEqual(list(self.root.glob("rsync-ssh-*.log")), [],
                         "the log of what ssh said goes with the transfer")

    def test_the_ssh_log_of_a_transfer_killed_outright_is_swept(self):
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        for pid in (gone.pid, live.pid):
            (self.root / ("rsync-ssh-%d.log" % pid)).write_text("said\n")
        (self.root / "rsync-ssh-x.log").write_text("not one of ours\n")
        code, _err, _seen = self.transferring([(0, 1)])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(p.name for p in self.root.glob("rsync-ssh-*.log")),
                         ["rsync-ssh-%d.log" % live.pid, "rsync-ssh-x.log"])

    def test_a_transfer_that_read_standard_input_is_not_resumed(self):
        # A second run would find stdin empty: no file list, or, with
        # --exclude-from=- under --delete, nothing protected.
        for args, option in ((["--files-from=-", "local/", "in/"], "--files-from"),
                             (["--exclude-from", "-", "local/", "in/"], "--exclude-from"),
                             (["-f", ". -", "local/", "in/"], "-f"),
                             (["--filter=merge -", "local/", "in/"], "--filter")):
            with self.subTest(args=args):
                code, err, seen = self.transferring([(12, 1), (0, 1)], args=args)
                self.assertEqual(code, 12)
                self.assertEqual(len(seen), 1)
                self.assertIn("push: rsync lost its connection (exit 12); not "
                              "resumed, since its %s was standard input, which a "
                              "second run would find empty" % option, err)

    def syncing(self, runs, args=()):
        """cmd_sync() over fake rsync runs, each (exit, seconds)."""
        runs = list(runs)
        seen = []
        (self.root / "tree").mkdir(exist_ok=True)
        self.configure(mirror_src=str(self.root / "tree"), mirror_dest="code")

        def run(argv):
            code, seconds = runs.pop(0)
            seen.append(argv)
            self.clock.now += seconds
            return code

        err = io.StringIO()
        with _patched(self.mod, "require_cert", lambda: None), \
                _patched(self.mod, "ensure_known_hosts", lambda: None), \
                _patched(self.mod, "pick_dtn", self.picker()), \
                _patched(self.mod, "sock_dir", lambda: self.root), \
                _patched(self.mod, "STATE_DIR", self.root / "state"), \
                _patched(self.mod, "EXCLUDE_PATH", self.root / "no-excludes"), \
                _patched(self.mod, "_sync_lock", lambda: io.StringIO()), \
                _patched(self.mod, "_run_child", run), \
                _patched(self.mod, "time", self.clock), \
                contextlib.redirect_stderr(err):
            code = self.mod.cmd_sync(list(args))
        return code, err.getvalue(), seen

    def test_a_sync_resumes_on_another_dtn_keeping_what_was_cut_short(self):
        code, err, seen = self.syncing([(10, 30), (0, 30)])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(seen), 2)
        self.assertEqual(self.avoided, [None, "dtn01.nersc.gov"])
        for argv in seen:
            self.assertIn("--delete", argv)
            self.assertIn("--partial-dir=.rsync-partial", argv)
        self.assertEqual(seen[1][-1], "user@dtn02.nersc.gov:code/")

    def test_a_sync_whose_excludes_came_from_standard_input_is_not_resumed(self):
        # Run again, it would read no excludes, and --delete would remove what
        # they protected.
        code, err, seen = self.syncing([(12, 30), (0, 30)], args=["--exclude-from=-"])
        self.assertEqual(code, 12)
        self.assertEqual(len(seen), 1)
        self.assertIn("sync: rsync lost its connection (exit 12); not resumed, since "
                      "its --exclude-from was standard input", err)

    def test_what_reads_standard_input_is_told_apart(self):
        reads = self.mod.rsync_reads_stdin
        for args in (["--files-from=-"], ["--include-from", "-"], ["--read-batch=-"],
                     ["-f. -"], ["-avf", "merge -"], ["--filter", ":- -"],
                     ["--files-from=/dev/stdin"], ["--exclude-from", "/dev/fd/0"],
                     ["--filter=merge /dev/stdin"], ["-f", ". /proc/self/fd/0"]):
            self.assertIsNotNone(reads(args), args)
        for args in ([], ["--files-from=list"], ["--exclude", "-"], ["--exclude=-"],
                     ["-f", "- -"], ["-f", "merge rules"], ["-e", "ssh -"], ["-"],
                     ["--files-from=/dev/stdin.txt"], ["--exclude=/dev/stdin"],
                     ["--", "--files-from=-"]):
            self.assertIsNone(reads(args), args)

    def test_what_keeps_a_file_cut_short_is_what_this_rsync_can_say(self):
        rsync = self.root / "bin" / "rsync"
        rsync.parent.mkdir()
        dir_help = "  --partial-dir=DIR  put a partially transferred file into DIR"
        env = {"PATH": "%s:%s" % (rsync.parent, os.environ["PATH"])}
        for script, flags in (
                ('echo "%s"' % dir_help, ["--partial-dir=.rsync-partial"]),
                ('echo "  --partial  keep partially transferred files"',
                 ["--partial"]),
                # openrsync, or any rsync whose help fails or complains, is
                # asked for neither.
                ('echo "%s"; exit 1' % dir_help, []),
                ('echo "%s"; echo "rsync: unknown option" >&2' % dir_help, []),
                ("exit 0", [])):
            with self.subTest(script=script), mock.patch.dict(os.environ, env):
                rsync.write_text("#!/bin/sh\n%s\n" % script)
                rsync.chmod(0o755)
                self.mod._RSYNC_HELP[:] = []
                self.assertEqual(self.mod.rsync_partial_flags([]), flags)
        with mock.patch.dict(os.environ, {"PATH": str(self.root / "none")}):
            self.mod._RSYNC_HELP[:] = []
            self.assertEqual(self.mod.rsync_partial_flags([]), [], "no rsync at all")

    def test_a_resumable_transfer_keeps_a_file_cut_short_aside(self):
        code, _err, seen = self.transferring([(0, 1)])
        self.assertIn("--partial-dir=.rsync-partial", seen[0])
        for own in ("--inplace", "--append", "--append-verify", "--partial-dir=keep",
                    "--write-devices"):
            with self.subTest(own=own):
                code, _err, seen = self.transferring([(0, 1)],
                                                     args=(own, "local/", "in/"))
                self.assertEqual(code, 0)
                self.assertNotIn("--partial-dir=.rsync-partial", seen[0],
                                 "rsync refuses it beside %s" % own)

    # --- locks --------------------------------------------------------------
    def test_a_sync_held_by_a_stopped_sync_is_given_up_on_naming_it(self):
        holder = hold_sync_lock(self, self.root / "sync.lock", 30)
        self.assertFalse(self.mod.pid_stopped(holder.pid))
        os.kill(holder.pid, signal.SIGSTOP)
        self.addCleanup(os.kill, holder.pid, signal.SIGCONT)
        # A stopped process is told apart from one that is only waiting.
        deadline = time.monotonic() + 10
        while not self.mod.pid_stopped(holder.pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.mod.pid_stopped(holder.pid))
        self.configure(lock_patience="1")
        with _patched(self.mod, "sock_dir", lambda: self.root), \
                _patched(self.mod, "time", FakeClock()):
            said = refusal(self.mod._sync_lock)
        self.assertIn("(pid %d) holds the sync lock and has been stopped for"
                      % holder.pid, said)
        self.assertIn("lock_patience (1)", said)

    def test_a_reap_lock_is_judged_by_the_configured_staleness_above_a_floor(self):
        lock = self.root / "reap.lock.d"
        floor = self.mod.REAP_LOCK_STALE_FLOOR

        def aged(seconds):
            lock.mkdir(exist_ok=True)
            old = time.time() - seconds
            os.utime(str(lock), (old, old))

        err = io.StringIO()
        with _patched(self.mod, "STATE_DIR", self.root), contextlib.redirect_stderr(err):
            aged(600)
            self.assertIsNone(self.mod._reap_lock(), "900 s by default")
            self.configure(reap_lock_stale_seconds="400")
            self.assertEqual(self.mod._reap_lock(), lock)
            # 0 would take every live pass's lock from it.
            self.configure(reap_lock_stale_seconds="0")
            aged(floor - 30)
            self.assertIsNone(self.mod._reap_lock())
            self.assertIsNone(self.mod._reap_lock())
            aged(floor + 30)
            self.assertEqual(self.mod._reap_lock(), lock)
        self.assertEqual(err.getvalue().count(
            "reap_lock_stale_seconds = 0 in %s is less than %d" % (
                self.mod.CONFIG_PATH, floor)), 1)

    def test_a_pass_holds_its_lock_while_it_waits_to_connect(self):
        ticks = []
        self.configure(connect_retry_delay="40", connect_retry_delay_max="60")
        with _patched(self.mod, "ON_TICK", lambda: ticks.append(self.clock.now)):
            got, _err, tried = self.opening(["Connection timed out", None])
        self.assertEqual(got, 0)
        self.assertEqual(tried, 2)
        self.assertEqual(self.slept, [15, 15, 10], "a tick at least every 15 s")
        self.assertEqual(len(ticks), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
