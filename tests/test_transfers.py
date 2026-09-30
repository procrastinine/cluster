#!/usr/bin/env python3
"""Transfers: rclone path semantics, delivery checks, leases and batching.

Ends with the transfer command line driven end to end, with only the
cluster stubbed out.

Run: python3 -m unittest tests.test_transfers
(or the whole suite: python3 -m unittest discover -s tests -p 'test_*.py')
"""

from __future__ import annotations

import contextlib
import csv
import io
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

# For `support`, which puts this checkout on sys.path and sets up the sandbox
# every test runs in, so it is imported before anything from clustertool.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import support  # noqa: E402
from support import FakeBackend, _patched, _refusal  # noqa: E402
from clustertool import platform as plat, riding, sshmux, transfer  # noqa: E402
from clustertool.transfer import LocalOps, RcloneOps, TransferSpec, Transfers  # noqa: E402
from clustertool.commands import transfers as cmds  # noqa: E402
from clustertool.commands.transfers import (  # noqa: E402
    _close_connections, _copy_on_cluster, _folds_case, cmd_ssh_command, cmd_transfer)
from clustertool.config import Settings  # noqa: E402
from clustertool.crossxfer import split_endpoint  # noqa: E402
from clustertool.remote_sh import remote_path, sftp_path  # noqa: E402
from clustertool.state import recorded_logins  # noqa: E402


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _rclone_runs(fake_run):
    """riding.run_riding for a fake rclone given its argv alone, whose
    connection is never lost."""
    return lambda argv, links, settings, env=None, heartbeat=False: (
        fake_run(argv).returncode, [])


class _Side:
    """A side of a transfer that answers from a table of ``{path: stat}``."""

    def __init__(self, stats=None, where="on the cluster", listing=None,
                 empty=False):
        self.stats = dict(stats or {})
        self.where = where
        self._listing = listing
        self.empty = empty
        self.asked = []

    def stat(self, path, tries=None):
        self.asked.append(path)
        return self.stats.get(path)

    probe = stat

    def listing(self, _path):
        return self._listing

    def holds_no_files(self, _path):
        return self.empty


class TestTransferPathSemantics(unittest.TestCase):
    """cp/rsync-like semantics, which is where rclone's defaults surprise people."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "dir" / "sub").mkdir(parents=True)
        (self.root / "dir" / "f.txt").write_text("x")
        (self.root / "file.txt").write_text("x")
        (self.root / "destdir").mkdir()

    def spec(self, source, dest, **kw):
        return TransferSpec(FakeBackend(), source, dest, **kw)

    def test_where_a_source_lands(self):
        r = str(self.root)
        up, down = {"up": True}, {"up": False}
        # source, dest, options, whether the cluster side is a directory,
        # what is asked of the resolved spec, and its answer.
        cases = [
            (f"{r}/dir", "remote:place", up, False, "remote_side", "place/dir"),
            (f"{r}/dir/", "remote:place", up, False, "remote_side", "place"),
            (f"{r}/dir", "remote:place", dict(up, contents=True), False,
             "remote_side", "place"),
            (f"{r}/file.txt", "remote:place/new.txt", up, False, "operation", "copyto"),
            (f"{r}/file.txt", "remote:place", up, True, "operation", "copy"),
            # A remote file taken for a directory would arrive as a directory
            # named after the file.
            ("remote:place/a.txt", f"{r}/destdir/n.txt", down, False,
             "operation", "copyto"),
            ("remote:place/tree", f"{r}/destdir", down, True,
             "local_side", f"{r}/destdir/tree"),
            ("remote:place/a.txt", f"{r}/destdir", down, False, "operation", "copy"),
            # Several sources can only mean "into here", so a destination that
            # is not there yet is a directory for rclone to create.
            ("remote:place/a.txt", f"{r}/new", dict(down, dest_is_dir=True), False,
             "operation", "copy"),
            (f"{r}/file.txt", "remote:~/place/n.txt", up, False,
             "remote_arg", ":sftp,shell_type=unix:place/n.txt"),
        ]
        for source, dest, options, is_dir, asked, want in cases:
            with self.subTest(source=source, dest=dest, options=options):
                spec = self.spec(source, dest, **options)
                spec.resolve(remote_is_dir=lambda p, is_dir=is_dir: is_dir)
                got = getattr(spec, asked)
                self.assertEqual(got() if callable(got) else got, want)

    def test_a_missing_source_is_named_where_it_is_missing(self):
        spec = self.spec("remote:place/gone", str(self.root / "destdir"), up=False)
        told = _refusal(lambda: spec.resolve(remote_is_dir=lambda p: None))
        self.assertIn("place/gone does not exist on the cluster", told)
        spec = self.spec("local:" + str(self.root / "gone"), "remote:place")
        told = _refusal(lambda: spec.resolve(remote_is_dir=lambda p: True))
        self.assertIn("gone does not exist here", told)

    def test_the_direction_is_the_side_that_exists_and_never_a_guess(self):
        self.assertTrue(self.spec(str(self.root / "dir"), "somewhere/else").up)
        self.assertFalse(self.spec("somewhere/else", str(self.root / "destdir")).up)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.spec("nowhere/a", "nowhere/b")

    def test_symlink_flags(self):
        spec = self.spec(str(self.root / "dir"), "remote:p", up=True)
        self.assertEqual(spec.symlink_flags(), ["--copy-links"])
        spec = self.spec(str(self.root / "dir"), "remote:p", up=True, symlinks="skip")
        self.assertEqual(spec.symlink_flags(), ["--skip-links"])


class TestRemotePaths(unittest.TestCase):
    """What a path means on a cluster, for its shell and for its sftp server."""

    def test_a_path_is_one_literal_word_relative_to_the_home_directory(self):
        for given, meant in (
                ("~/results/", "./results/"), ("~/results", "./results"), ("~", "."),
                ("~/", "."), ("", "."), ("~//x", "./x"), ("projects/x", "./projects/x"),
                ("./x", "./x"), ("../x", "../x"), ("/n/a b/it's", "'/n/a b/it'\"'\"'s'"),
                ("-rf", "./-rf"), ("~/-rf", "./-rf"), ("/n/$HOME/`x`", "'/n/$HOME/`x`'"),
                # Only a leading `~/` means home: anywhere else it is a name.
                ("/n/~/x", "'/n/~/x'"), ("~user/x", "'./~user/x'")):
            self.assertEqual(remote_path(given), meant, given)

    def test_a_shell_reads_each_word_back_as_the_path_meant(self):
        home = Path(tempfile.mkdtemp(dir=str(support.SANDBOX)))
        self.addCleanup(shutil.rmtree, str(home), True)
        for path, landed in (("~/a b", home / "a b"), ("~", home),
                             ("-rf", home / "-rf"), ("~user/x", home / "~user/x")):
            with self.subTest(path=path):
                got = subprocess.run(
                    ["sh", "-c", f"cd && printf %s {remote_path(path)}"],
                    env=dict(os.environ, HOME=str(home)), stdout=subprocess.PIPE,
                    universal_newlines=True).stdout
                self.assertEqual(os.path.normpath(os.path.join(str(home), got)),
                                 str(landed))

    def test_an_sftp_path_starts_in_the_home_directory(self):
        for given, meant in (("~/data/x", "data/x"), ("~", ""), ("~//x", "x"),
                             ("/abs/it's", "/abs/it's"), ("rel", "rel")):
            self.assertEqual(sftp_path(given), meant, given)
        self.assertEqual(transfer.sftp_arg("~/x"), ":sftp,shell_type=unix:x")


class TransferHarness(unittest.TestCase):
    """A Transfers wired to a temporary directory and no cluster at all."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def transfers(self, values=None):
        state = types.SimpleNamespace(
            dir=self.root / "state", ctl_dir=self.root / "ctl",
            xfer_socket=lambda tag: self.root / "ctl" / f"{tag}.sock",
            socket=lambda name: self.root / "ctl" / f"{name}.sock",
        )
        state.dir.mkdir(parents=True, exist_ok=True)
        state.ctl_dir.mkdir(parents=True, exist_ok=True)
        backend = types.SimpleNamespace(
            name="fake", user="u", short=lambda n: (n or "").split(".")[0],
            host_for=lambda n: n or "host", transfer_nodes=lambda: [],
            target=lambda n=None: f"u@{n or 'host'}", cli_flag=lambda: "--fake",
            credentials_command=lambda: "cluster --fake config credentials",
            node_choosable=False,
        )
        values = values or {}
        logins = types.SimpleNamespace(
            backend=backend, state=state,
            settings=types.SimpleNamespace(int=lambda key: values.get(key, 30),
                                           str=lambda key: ""),
        )
        return Transfers(logins)


class TestTransferDeliveryCheck(TransferHarness):
    """A transfer that exits 0 must have delivered the bytes.

    rclone reports the errors it sees, and every case here is one it does not
    see: it exits 0 with nothing at the destination. That is the failure that
    costs a day, because the shell moves straight on to the next `&&` and the
    job runs against data that never arrived.
    """

    def spec(self, source, dest, **kw):
        return TransferSpec(FakeBackend(), source, dest, **kw)

    def test_delivered_names_what_arrives_where(self):
        (self.root / "f.txt").write_text("x")
        (self.root / "tree").mkdir()
        (self.root / "here").mkdir()
        r = str(self.root)
        for source, dest, up, is_dir, want in (
                (f"{r}/f.txt", "remote:place/", True, True, ("place/f.txt", False)),
                (f"{r}/f.txt", "remote:place/new.txt", True, None,
                 ("place/new.txt", False)),
                (f"{r}/tree", "remote:place", True, True, ("place/tree", True)),
                ("remote:place/a.txt", f"{r}/here", False, False,
                 (f"{r}/here/a.txt", False))):
            with self.subTest(source=source, dest=dest):
                spec = self.spec(source, dest, up=up)
                spec.resolve(remote_is_dir=lambda p, is_dir=is_dir: is_dir)
                self.assertEqual(spec.delivered(), want)

    def uploaded(self, text="x"):
        (self.root / "f.txt").write_text(text)
        spec = self.spec(str(self.root / "f.txt"), "remote:place/", up=True)
        spec.resolve(remote_is_dir=lambda p: True)
        return spec

    def test_what_is_at_the_destination_decides(self):
        spec = self.uploaded("x" * 100)
        expect = ("place/f.txt", False, 100)
        check = lambda stat: transfer.verify(  # noqa: E731
            spec, LocalOps(), _Side({"place/f.txt": stat}), expect)
        with _patched(time, "sleep", lambda _s: None):
            self.assertIn("place/f.txt is not on the cluster",
                          _refusal(lambda: check(None)))
        self.assertIn("cut short", _refusal(lambda: check({"IsDir": False, "Size": 40})))
        self.assertTrue(check({"IsDir": False, "Size": 100}), "checked, and passed")
        # A log still being appended to delivers more than it measured: a
        # destination that grew is not a truncated one.
        self.assertTrue(check({"IsDir": False, "Size": 140}))
        # The bytes did move; only the check failed. Inventing a failure here
        # would break the very pipelines this is meant to protect.
        with contextlib.redirect_stderr(io.StringIO()) as said:
            check(transfer.UNKNOWN)
        self.assertIn("could not confirm", said.getvalue())

    def test_what_is_expected_is_taken_from_the_source_before_it_moves(self):
        (self.root / "here").mkdir()
        spec = self.spec("remote:place/a.txt", str(self.root / "here"), up=False)
        spec.resolve(remote_is_dir=lambda p: False)
        far = _Side({"place/a.txt": {"IsDir": False, "Size": 5}})
        expect = transfer.expectation(spec, LocalOps(), far)
        self.assertEqual(expect, (str(self.root / "here" / "a.txt"), False, 5))
        (self.root / "here" / "a.txt").write_text("12")
        told = _refusal(lambda: transfer.verify(spec, LocalOps(), far, expect))
        self.assertIn("is 2 bytes here, expected 5", told, "checked on this machine")
        # The expected size is taken before a move deletes the source.
        source = self.root / "f.txt"
        source.write_text("x" * 64)
        spec = self.spec(str(source), "remote:place/", up=True, operation="move")
        spec.resolve(remote_is_dir=lambda p: True)
        expect = transfer.expectation(spec, LocalOps(), _Side())
        source.unlink()
        self.assertEqual(expect, ("place/f.txt", False, 64))

    def test_verification_that_cannot_run_never_fails_a_transfer(self):
        """Filters and --links both make the destination legitimately differ,
        and an empty source directory is nothing for rclone to copy."""
        (self.root / "f.txt").write_text("x" * 10)
        (self.root / "tree" / "empty").mkdir(parents=True)
        cases = [("f.txt", {"extra": ["--exclude", "*.log"]}, "allowed, because"),
                 ("f.txt", {"symlinks": "keep"}, "allowed, because"),
                 ("tree", {}, "nothing to copy")]
        for name, kw, said_so in cases:
            spec = self.spec(str(self.root / name), "remote:place/", up=True, **kw)
            spec.resolve(remote_is_dir=lambda p: True)
            self.assertEqual(bool(transfer.inexact(spec)), bool(kw), kw)
            expect = ("place/f.txt", False, 10) if kw else ("place/tree", True, None)
            with _patched(time, "sleep", lambda _s: None), \
                    contextlib.redirect_stderr(io.StringIO()) as said:
                transfer.verify(spec, LocalOps(), _Side(), expect)
            self.assertIn(said_so, said.getvalue())


class TestTransferProbeTriState(TransferHarness):
    """"Not there" and "could not ask" are different answers.

    Reading a failed probe as "not a directory" would silently rewrite the path
    semantics: `copy INTO place` would become `rename TO place`, which rclone
    carries out and exits 0 on.
    """

    def ops(self, *replies):
        """An RcloneOps whose commands get *replies* in turn, and what it ran."""
        calls, queue = [], list(replies)

        def run(argv, timeout, **watch):
            calls.append(argv)
            self.limits.append((timeout, watch))
            return queue.pop(0) if len(queue) > 1 else queue[0]

        self.limits = []
        return transfer.RcloneOps(["rclone", "--config", "/dev/null"], run=run), calls

    def test_what_an_answer_means(self):
        cases = [
            ((0, '{"IsDir": true, "Size": -1}', ""), {"IsDir": True, "Size": -1}),
            ((3, "", ""), None),
            # rclone's wording, even from an older exit code, is its verdict.
            ((1, "", "ERROR: directory not found"), None),
            ((1, "", "couldn't initialise SFTP"), transfer.UNKNOWN),
            ((124, "", ""), transfer.UNKNOWN),
            ((255, "", "Connection closed"), transfer.UNKNOWN),
            # A shell that cannot run rclone, and ssh, say "No such file" too.
            ((127, "", "bash: /opt/rclone/bin/rclone: No such file or directory"),
             transfer.UNKNOWN),
            ((255, "", "Control socket connect(/x.sock): No such file or directory"),
             transfer.UNKNOWN),
            # An ssh warning printed before rclone ran is not rclone's verdict.
            ((1, "", "Warning: Identity file /k not accessible: No such file or "
                     "directory.\nFailed to lsjson: couldn't connect SSH: EOF"),
             transfer.UNKNOWN),
        ]
        for reply, want in cases:
            with self.subTest(reply=reply):
                ops, _calls = self.ops(_proc(*reply))
                with _patched(time, "sleep", lambda _s: None):
                    got = ops.stat("some/path")
                if want is transfer.UNKNOWN:
                    self.assertIs(got, want)
                else:
                    self.assertEqual(got, want)

    def test_the_probe_names_the_path_as_rclone_should_see_it_once_per_path(self):
        ops, calls = self.ops(_proc(0, '{"IsDir": true}'))
        self.assertTrue(ops.is_dir("~/data"))
        self.assertTrue(ops.is_dir("~/data"))
        self.assertEqual(calls, [["rclone", "--config", "/dev/null", "lsjson",
                                  "--stat", "--log-level", "ERROR",
                                  ":sftp,shell_type=unix:data"]])
        ops.stat("~/data")
        self.assertEqual(len(calls), 2, "the check after a transfer asks afresh")

    def test_a_probe_is_tried_again_as_often_as_the_settings_say(self):
        """A busy login node answers nothing for a moment; that is not a verdict.

        Without the retry, one slow round trip would decide the fate of a
        transfer that is otherwise fine.
        """
        ops, calls = self.ops(_proc(124), _proc(1, stderr="connection reset"),
                              _proc(0, '{"IsDir": false, "Size": 7}'))
        with _patched(time, "sleep", lambda _s: None):
            self.assertEqual(ops.stat("some/path"), {"IsDir": False, "Size": 7})
        self.assertEqual(len(calls), 3)
        # rclone said "not there" definitively; asking again only costs time.
        ops, calls = self.ops(_proc(3, stderr="directory not found"))
        self.assertIsNone(ops.stat("gone"))
        self.assertEqual(len(calls), 1)
        ops, calls = self.ops(_proc(1, stderr="couldn't initialise SFTP"))
        with _patched(time, "sleep", lambda _s: None):
            self.assertIs(ops.stat("x"), transfer.UNKNOWN)
        self.assertEqual(len(calls), ops.tries)
        self.assertEqual({timeout for timeout, _watch in self.limits}, {ops.timeout})
        settings = Settings("fasrc")
        self.assertEqual(transfer.probe_limits(settings), {
            "timeout": settings.int("TRANSFER_PROBE_TIMEOUT"),
            "tries": settings.int("TRANSFER_PROBE_TRIES"),
            "idle": settings.int("TRANSFER_IO_TIMEOUT")})

    def test_a_listing_is_not_timed_only_watched_for_silence(self):
        """A directory with a million entries takes as long as it takes."""
        ops, _calls = self.ops(_proc(0, '[{"Name": "a", "IsDir": false}]'))
        self.assertEqual(set(ops.listing("place")), {"a"})
        self.assertEqual(self.limits, [(None, {"idle": ops.idle})])

    def test_a_silent_listing_falls_back_to_each_path_until_one_goes_unanswered(self):
        ops, calls = self.ops(_proc(124), _proc(0, '{"IsDir": false, "Size": 3}'))
        (self.root / "f.txt").write_text("abc")
        spec = TransferSpec(FakeBackend(), str(self.root / "f.txt"),
                            "remote:place/", up=True)
        spec.resolve(remote_is_dir=lambda p: True)
        transfer.verify_group([spec], LocalOps(), ops, [("place/f.txt", False, 3)])
        self.assertEqual(calls[0][3], "lsjson")
        self.assertEqual(calls[1][3:5], ["lsjson", "--stat"])
        # Each unanswered question has had its tries; the other 49 would
        # wait out as many again, one after the other.
        ops, calls = self.ops(_proc(124), *[_proc(124)] * 3)
        specs, expects = [], []
        for n in range(50):
            (self.root / f"f{n}").write_text("abc")
            spec = TransferSpec(FakeBackend(), str(self.root / f"f{n}"),
                                "remote:place/", up=True)
            spec.resolve(remote_is_dir=lambda p: True)
            specs.append(spec)
            expects.append((f"place/f{n}", False, 3))
        with _patched(time, "sleep", lambda _s: None), \
                contextlib.redirect_stderr(io.StringIO()) as said:
            transfer.verify_group(specs, LocalOps(), ops, expects)
        self.assertEqual(len(calls), 1 + ops.tries, "one listing, one path's tries")
        self.assertIn("the other 49 were not checked either", said.getvalue())

    def test_a_walk_stops_at_the_first_file_and_silence_is_not_empty(self):
        ops, calls = self.ops(types.SimpleNamespace(returncode=0, stdout="",
                                                    stderr="", enough=True))
        self.assertFalse(ops.holds_no_files("tree"))
        (_timeout, watch), = self.limits
        self.assertEqual(watch["idle"], ops.idle)
        self.assertTrue(watch["enough"](b'{"Path":"a/b","IsDir":false}'))
        self.assertFalse(watch["enough"](b'{"Path":"a","IsDir":true}'))
        self.assertNotIn("--files-only", calls[0],
                         "directories are listed too, so a long walk shows progress")
        self.assertTrue(self.ops(_proc(0, '[{"Path": "a", "IsDir": true}]'))[0]
                        .holds_no_files("tree"))
        self.assertFalse(self.ops(_proc(124))[0].holds_no_files("tree"),
                         "a walk given up for silence is not an empty tree")

    @unittest.skipUnless(support.usable_rclone(), "needs rclone 1.64 or newer")
    def test_the_walk_reads_rclones_own_output(self):
        tree = self.root / "tree"
        (tree / "a" / "b").mkdir(parents=True)
        ops = transfer.RcloneOps([support.usable_rclone(), "--config", "/dev/null"],
                                 fmt=str)
        self.assertTrue(ops.holds_no_files(str(tree)))
        (tree / "a" / "b" / "f").write_text("x")
        self.assertFalse(ops.holds_no_files(str(tree)))

    def test_a_side_it_could_not_check_is_read_the_recoverable_way(self):
        """Failing here loses a transfer to a hiccup; the two readings are not
        equally bad, so take the recoverable one.

        "Copy into DEST" costs one extra level of nesting if it was wrong.
        "Rename onto DEST" writes a whole source over a path and calls it a
        success, which is the misdelivery the probe exists to prevent. A
        download reads the source as a directory for the same reason.
        """
        (self.root / "f.txt").write_text("x")
        (self.root / "here").mkdir()
        up = TransferSpec(FakeBackend(), str(self.root / "f.txt"), "remote:place", up=True)
        down = TransferSpec(FakeBackend(), "remote:place/a.txt", str(self.root / "here"),
                            up=False)
        for spec in (up, down):
            with contextlib.redirect_stderr(io.StringIO()) as said:
                spec.resolve(remote_is_dir=lambda p: transfer.UNKNOWN)
            self.assertIn("could not tell", said.getvalue())
        self.assertEqual(up.operation_kind, "copy")
        self.assertEqual(up.delivered(), ("place/f.txt", False))
        self.assertTrue(down.source_is_dir)
        self.assertTrue(down.local_side.endswith("here/a.txt"))
        # An unanswerable probe and a definite "not there" are different
        # answers, and only one of them is a reason to stop.
        down = TransferSpec(FakeBackend(), "remote:place/a.txt", str(self.root / "here"),
                            up=False)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            down.resolve(remote_is_dir=lambda p: None)


class TestLocalSide(unittest.TestCase):
    """This machine answers the same questions a cluster does."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=str(support.SANDBOX)))
        self.addCleanup(shutil.rmtree, str(self.root), True)

    def test_stat_listing_and_emptiness(self):
        ops = LocalOps()
        (self.root / "tree" / "sub").mkdir(parents=True)
        self.assertIsNone(ops.stat(str(self.root / "gone")))
        self.assertIsNone(ops.is_dir(str(self.root / "gone")))
        self.assertTrue(ops.is_dir(str(self.root / "tree")))
        self.assertTrue(ops.holds_no_files(str(self.root / "tree")),
                        "empty directories are nothing for rclone to copy")
        (self.root / "tree" / "sub" / "f").write_text("abc")
        self.assertFalse(ops.holds_no_files(str(self.root / "tree")))
        self.assertEqual(ops.stat(str(self.root / "tree" / "sub" / "f")),
                         {"IsDir": False, "Size": 3})
        self.assertEqual(set(ops.listing(str(self.root / "tree"))), {"sub"})
        self.assertIsNone(ops.listing(str(self.root / "gone")))


class TestFindRclone(unittest.TestCase):
    """One resolver, used by transfers and by doctor, that never exits."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=str(support.SANDBOX)))
        self.addCleanup(shutil.rmtree, str(self.root), True)

    def fake(self, where, version):
        """An rclone that reports *version*, and only with its config ignored."""
        path = self.root / where / "rclone"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n"
                        '[ "$*" = "--config /dev/null version" ] || exit 9\n'
                        f"echo 'rclone v{version}'\necho '- os/version: test'\n")
        path.chmod(0o755)
        return str(path)

    def find(self, explicit="", on_path=(), fallbacks=()):
        settings = types.SimpleNamespace(
            str=lambda key: explicit if key == "RCLONE" else "")
        path = os.pathsep.join(str(Path(p).parent) for p in on_path) or str(self.root)
        with mock.patch.dict(os.environ, {"PATH": path}), \
                _patched(transfer, "RCLONE_FALLBACKS", tuple(fallbacks)):
            return transfer.find_rclone(settings)

    def test_the_first_rclone_new_enough_is_taken_path_first(self):
        # The minimum is the first rclone with --sftp-ssh. Each fake exits 9
        # unless it is told to ignore the config, so finding one at all shows
        # the version check passes --config /dev/null.
        self.assertEqual(transfer.MIN_RCLONE, (1, 64))
        mine = self.fake("home/bin", "1.66.0")
        other = self.fake("usr/local/bin", "1.68.1")
        self.assertEqual(self.find(on_path=[mine], fallbacks=[other]),
                         (mine, (1, 66), None))
        # A distribution rclone that is too old is passed over.
        old = self.fake("usr/bin", "1.60.1")
        new = self.fake("opt/homebrew/bin", "1.64.0")
        self.assertEqual(self.find(on_path=[old], fallbacks=[new]),
                         (new, (1, 64), None))
        found = self.find(on_path=[old])
        self.assertEqual((found.path, found.problem),
                         (old, "1.60 is too old (need 1.64)"), "too old everywhere")
        self.assertEqual(self.find(), (None, None, "not found"))

    def test_an_explicit_rclone_is_checked_and_used_alone(self):
        good = self.fake("opt", "1.65.0")
        old = self.fake("old", "1.50.0")
        self.assertEqual(self.find(explicit=good), (good, (1, 65), None))
        found = self.find(explicit=old, fallbacks=[good])
        self.assertEqual((found.path, found.problem),
                         (old, "1.50 is too old (need 1.64)"))
        missing = str(self.root / "nowhere" / "rclone")
        self.assertEqual(self.find(explicit=missing, fallbacks=[good]),
                         (missing, None, "not found"))

    def test_an_rclone_that_does_not_answer_is_named_not_taken_for_absent(self):
        broken = str(self.root / "bin" / "rclone")
        Path(broken).parent.mkdir(parents=True, exist_ok=True)
        Path(broken).write_text("#!/bin/sh\nexit 1\n")
        Path(broken).chmod(0o755)
        self.assertEqual(self.find(on_path=[broken]),
                         (broken, None, "did not report an rclone version"))
        # A working one elsewhere still wins over it.
        good = self.fake("usr/local/bin", "1.66.0")
        self.assertEqual(self.find(on_path=[broken], fallbacks=[good]),
                         (good, (1, 66), None))

    def test_a_transfer_stops_before_connecting_and_says_how_to_fix_it(self):
        xfer = object.__new__(transfer.Transfers)
        xfer.settings = types.SimpleNamespace(
            str=lambda key: self.fake("old", "1.50.0") if key == "RCLONE" else "")
        told = _refusal(xfer.rclone_bin)
        self.assertIn("1.50 is too old (need 1.64)", told)
        self.assertIn("cluster config set RCLONE /path", told)


class TestRcloneCommandLines(unittest.TestCase):
    """What every rclone run is told, wherever it runs."""

    def settings(self, **values):
        base = {"TRANSFER_CONNECTIONS": 0, "TRANSFER_MULTI_THREAD_STREAMS": 1,
                "TRANSFER_IO_TIMEOUT": 77, "TRANSFER_RETRIES": 2,
                "TRANSFER_TRANSFERS": 5, "TRANSFER_CHECKERS": 3,
                "SHARED_TRANSFER_TRANSFERS": 2, "SHARED_TRANSFER_CHECKERS": 1}
        base.update(values)
        return types.SimpleNamespace(int=lambda key: base[key])

    @staticmethod
    def value(argv, flag):
        return argv[argv.index(flag) + 1]

    def test_the_transfer_settings_reach_rclone_and_a_shared_master_gets_less(self):
        argv = transfer.rclone_flags(self.settings(), 5, 3)
        for flag, value in (("--timeout", "77s"), ("--retries", "3"),
                            ("--multi-thread-streams", "1"),
                            ("--sftp-connections", "9")):
            self.assertEqual(self.value(argv, flag), value, flag)
        argv = transfer.rclone_flags(self.settings(TRANSFER_CONNECTIONS=4), 5, 3)
        self.assertEqual(self.value(argv, "--sftp-connections"), "4")
        settings = self.settings()
        self.assertEqual(transfer.concurrency(settings), (5, 3))
        self.assertEqual(transfer.concurrency(settings, shared=True), (2, 1))
        self.assertEqual(transfer.concurrency(settings, 8, 0, shared=True), (8, 1))

    def test_output_follows_quiet_then_progress_then_log_stats(self):
        flags = transfer.rclone_flags
        settings = self.settings()
        self.assertIn("--log-level", flags(settings, 1, 1, quiet=True, progress=True))
        self.assertIn("--progress", flags(settings, 1, 1, progress=True,
                                          log_stats=True))
        stats = flags(settings, 1, 1, log_stats=True)
        self.assertIn("--stats-one-line", stats)
        self.assertEqual(stats[stats.index("--stats-log-level") + 1], "NOTICE",
                         "stats log at INFO, which rclone hides by default")
        self.assertEqual(flags(settings, 1, 1, symlinks="skip",
                               extra=["--checksum"])[-2:],
                         ["--skip-links", "--checksum"])

    def test_an_ssh_command_is_one_rclone_list_value(self):
        from clustertool.sshmux import rclone_ssh_value as words

        self.assertEqual(words(["ssh", "-o", "ControlPath=/a b/s"]),
                         'ssh -o "ControlPath=/a b/s"')
        self.assertEqual(words(['say "hi"', "", "it's"]),
                         '"say ""hi""" "" it\'s')

    @unittest.skipUnless(support.usable_rclone(), "needs rclone 1.64 or newer")
    def test_rclone_hands_ssh_exactly_the_words_given(self):
        """Run rclone for real against an ssh that only records its argv."""
        root = Path(tempfile.mkdtemp(prefix="with space ", dir=str(support.SANDBOX)))
        self.addCleanup(shutil.rmtree, str(root), True)
        fake = root / "fake ssh"
        record = root / "argv"
        fake.write_text("#!/bin/sh\n"
                        f'for a in "$@"; do printf "[%s]\\n" "$a"; done > "{record}"\n'
                        "exit 1\n")
        fake.chmod(0o755)
        wanted = [str(fake), "-o", "ControlPath=/a b/sock", 'q"uote', "", "it's"]
        subprocess.run(
            [support.usable_rclone(), "--config", "/dev/null", "--retries", "1",
             "--low-level-retries", "1", "--sftp-ssh", sshmux.rclone_ssh_value(wanted),
             "lsjson", "--stat", ":sftp,shell_type=unix:x"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        got = record.read_text().splitlines()
        self.assertEqual(got[:len(wanted) - 1], [f"[{w}]" for w in wanted[1:]])


class TestTransferLeases(TransferHarness):
    """A lease must not outlive the run that took it.

    The pid in a lease file is reused within days on a machine that has been up
    for months. A lease that outlived its run would then name some unrelated
    live process and pin the pool open for good, printing "still used by 1
    other run(s)" with nothing else running.
    """

    def test_a_lease_is_counted_while_its_run_holds_it_and_not_after(self):
        xfer = self.transfers()
        xfer.lease_take("pool")
        self.assertTrue(xfer.lease_file("pool").exists())
        xfer.lease_drop("pool")
        self.assertFalse(xfer.lease_file("pool").exists())
        leaked = xfer.lease_dir("pool") / "1"      # pid 1 is always alive
        leaked.write_text("0")
        self.assertEqual(xfer.lease_holders("pool"), [])
        self.assertFalse(leaked.exists(), "a leaked lease does not hold the pool open")
        path = xfer.lease_dir("pool") / "999999"
        path.write_text("0")
        holder = support.hold_flock(self, path, 30, record_pid=False)
        self.assertEqual(xfer.lease_holders("pool"), [("pid", 999999)])
        self.assertTrue(path.exists())
        holder.kill()
        holder.wait()
        # The kernel drops the lock when the holder dies, however it died.
        self.assertEqual(xfer.lease_holders("pool"), [])

    def test_reusing_a_connection_leases_it_under_its_lock(self):
        # Checked and leased in two unguarded steps, a teardown in between
        # would leave a lease on a master that is gone.
        xfer = self.transfers()
        xfer.is_active = lambda tag: tag == "pool"
        locked, real_lock = [], xfer.tag_lock

        @contextlib.contextmanager
        def tag_lock(tag):
            with real_lock(tag):
                locked.append(tag)
                yield

        xfer.tag_lock = tag_lock
        self.assertTrue(xfer.reuse("pool"))
        self.assertTrue(xfer.lease_file("pool").exists())
        self.assertFalse(xfer.reuse("pool-fwd"))
        self.assertFalse(xfer.lease_file("pool-fwd").exists())
        self.assertEqual(locked, ["pool", "pool-fwd"])
        xfer.lease_drop("pool")

    def test_a_failure_while_resolving_still_releases_the_lease(self):
        """resolve() talks to the cluster, so it runs inside the try that
        drops the lease."""
        (self.root / "f.txt").write_text("x")
        xfer = self.transfers()
        xfer.rclone_bin = lambda: "/bin/true"

        def open_connection(node=None, quiet=False):
            xfer.lease_take("pool")
            return "pool"

        xfer.open_connection = open_connection
        closed = []
        xfer.close_connection = lambda tag, force=False: closed.append(tag)

        # The source is not on the cluster, so resolve() reports it and the run
        # ends before rclone is started -- with the lease already taken.
        spec = TransferSpec(FakeBackend(), "remote:place/gone.txt",
                            str(self.root), up=False)
        with _patched(transfer.RcloneOps, "stat", lambda _s, path, tries=3: None), \
                contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            xfer.run(spec, quiet=True)
        self.assertEqual(list(xfer.lease_dir("pool").iterdir()), [])
        self.assertEqual(closed, ["pool"])


class TestCallerLeases(TransferHarness):
    """`ssh-command --transfer` hands its connection to a program that outlives it.

    The lease is held for the caller's process group: the subshell a `$(...)`
    runs in is gone as soon as the command exits, but the script it belongs to
    is not.
    """

    def caller(self):
        """A live process group, standing in for the calling script."""
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                  start_new_session=True)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        return holder

    def test_the_lease_lasts_as_long_as_the_callers_process_group(self):
        xfer = self.transfers()
        holder = self.caller()
        with _patched(transfer, "caller_group", lambda: None):
            self.assertFalse(xfer.lease_take_for_caller("pool"),
                             "a run that leads its own group has no caller")
        self.assertEqual(list(xfer.lease_dir("pool").iterdir()), [])
        with _patched(transfer, "caller_group", lambda: holder.pid):
            self.assertTrue(xfer.lease_take_for_caller("pool"))
        self.assertEqual(xfer.lease_holders("pool"), [("process group", holder.pid)])
        holder.kill()
        holder.wait()
        self.assertEqual(xfer.lease_holders("pool"), [])
        self.assertEqual(list(xfer.lease_dir("pool").iterdir()), [],
                         "a dead caller's lease is pruned")

    def test_a_group_lease_is_only_ever_the_callers_on_this_host(self):
        """A state directory in a shared home holds other machines' leases.

        Their connections are sockets on those machines, so they are neither
        counted here nor judged dead by a process table that is not theirs.
        """
        xfer = self.transfers()
        holder = self.caller()
        reused = xfer.lease_dir("pool") / f"group-{holder.pid}@{transfer._this_host()}"
        reused.write_text("12345\n")
        elsewhere = xfer.lease_dir("pool") / "group-1@elsewhere.example"
        elsewhere.write_text("12345\n")
        self.assertEqual(xfer.lease_holders("pool"), [], "a reused group number")
        self.assertTrue(elsewhere.exists())

    def closing(self, xfer, group):
        """`transfer --close pool` run from process group *group*."""
        closed = []
        xfer.active_tags = lambda: ["pool"]
        xfer._discard_connection = closed.append
        err = io.StringIO()
        with _patched(transfer, "caller_group", lambda: group), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(err):
            _close_connections(xfer, types.SimpleNamespace(close=["pool"],
                                                           force=False))
        return closed, err.getvalue()

    def test_another_run_leaves_it_open_and_the_caller_or_force_closes_it(self):
        for force in (False, True):
            xfer = self.transfers()
            holder = self.caller()
            with _patched(transfer, "caller_group", lambda: holder.pid):
                xfer.lease_take_for_caller("pool")
            if force:
                closed = []
                xfer._discard_connection = closed.append
                with contextlib.redirect_stderr(io.StringIO()) as said:
                    self.assertTrue(xfer.close_connection("pool", force=True))
                self.assertEqual(closed, ["pool"])
                self.assertIn(f"while process group {holder.pid} still hold(s) a "
                              "lease", said.getvalue())
                continue
            # A transfer that ends while the caller still uses the connection.
            with contextlib.redirect_stderr(io.StringIO()) as said:
                self.assertFalse(xfer.close_connection("pool"))
            self.assertIn(f"still used by 1 other run(s) (process group {holder.pid})",
                          said.getvalue())
            closed, _told = self.closing(xfer, group=None)
            self.assertEqual(closed, [], "--close from elsewhere respects the lease")
            closed, told = self.closing(xfer, group=holder.pid)
            self.assertEqual(closed, ["pool"])
            self.assertIn("closed transfer connection pool", told)


class TestSshCommandTransfer(TransferHarness):
    """The transport archive-sync asks for, and how it knows what to close."""

    def run_it(self, already_open, group, *args):
        xfer = self.transfers({"EXTERNAL_SSH_SERVER_ALIVE_INTERVAL": 15,
                               "EXTERNAL_SSH_SERVER_ALIVE_COUNT_MAX": 4})
        xfer.backend.fqdn = lambda n: n
        (xfer.state.dir / "transfer-pool.lastnode").write_text("dtn02.example\n")
        ctx = types.SimpleNamespace(backend=xfer.backend, state=xfer.state,
                                    settings=xfer.settings, logins=xfer.logins)

        def open_connection(_self, node=None, quiet=False):
            _self.lease_take("pool")
            return "pool"

        out, err = io.StringIO(), io.StringIO()
        with _patched(Transfers, "open_connection", open_connection), \
                _patched(Transfers, "active_tags",
                         lambda _self: ["pool"] if already_open else []), \
                _patched(transfer, "caller_group", lambda: group), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cmd_ssh_command(ctx, ["--transfer", *args]), 0)
        return xfer, out.getvalue(), err.getvalue()

    def test_it_says_which_connection_it_opened_and_holds_it_for_the_caller(self):
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                  start_new_session=True)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        xfer, out, err = self.run_it(already_open=False, group=holder.pid)
        self.assertIn("opened transfer connection pool", err)
        self.assertIn("transfer --close pool", err)
        self.assertEqual(xfer.lease_holders("pool"), [("process group", holder.pid)],
                         "its own flock is gone, the caller's lease is not")
        self.assertIn("u@dtn02.example", out, "the node the master is on")

    def test_the_rclone_form_is_the_same_command_as_rclone_splits_it(self):
        _xfer, shell, err = self.run_it(True, None)
        self.assertIn("reusing transfer connection pool", err)
        _xfer, value, _err = self.run_it(True, None, "--rclone")
        words = shlex.split(shell)
        self.assertTrue(any(" " in word for word in words),
                        "the ProxyCommand guard holds spaces")
        rclone, = csv.reader([value.rstrip("\n")], delimiter=" ", strict=True)
        self.assertEqual(rclone, words)
        self.assertIn(' "ProxyCommand=', value, "a word with spaces is quoted for rclone")


class TestTransferNodeRotation(TransferHarness):
    """Where nodes are addressable, one being down costs an attempt, not the run.

    Each attempt is one Logins.open_master, which says whether its failure is
    worth another node; a refused credential is not.
    """

    DOWN = "ssh: connect to host dtn01.x port 22: Connection refused"
    DENIED = "Permission denied (publickey)."

    def opener(self, nodes, up, choosable=True, failure=DOWN):
        xfer = self.transfers({"MAX_LOGINS": 10, "TRANSFER_OPEN_TRIES": 3})
        tried, credentials = [], []

        def open_master(sock, node, log, tries=1, forward_agent=False, env=None,
                        quiet=False):
            tried.append((node or "pool", tries))
            if (node or "pool") in up:
                return sshmux.MasterOpen(True, "")
            return sshmux.MasterOpen(False, failure,
                                     another_node=failure != self.DENIED)

        xfer.backend.__dict__.update(
            node_choosable=choosable, transfer_nodes=lambda: list(nodes),
            ensure_credential=lambda quiet=False: credentials.append(quiet))
        xfer.logins.__dict__.update(
            connection_count=lambda: 0, open_master=open_master,
            _socket_live=lambda _sock: False)
        self.credentials = credentials
        return xfer, tried

    @staticmethod
    def nodes(tried):
        return [node for node, _tries in tried]

    def test_a_node_that_is_down_is_passed_over_and_the_one_that_works_kept(self):
        nodes = ["dtn01.x", "dtn02.x", "dtn03.x", "dtn04.x"]
        xfer, tried = self.opener(nodes, up={"dtn03.x"})
        self.assertEqual(xfer.open_connection(quiet=True), "pool")
        self.assertEqual(tried, [("dtn01.x", 1), ("dtn02.x", 1), ("dtn03.x", 1)])
        self.assertEqual(xfer.node_of("pool"), "dtn03.x")
        self.assertTrue(xfer.lease_file("pool").exists(), "this run holds a lease")
        self.assertEqual(self.credentials, [True], "the credential is checked first")
        self.assertEqual(recorded_logins(xfer.backend.name, xfer.state.dir,
                                         xfer.state.ctl_dir), [],
                         "the node kept for a transfer is not taken for a login")
        tried.clear()
        xfer.open_connection(quiet=True)
        self.assertEqual(self.nodes(tried), ["dtn03.x"], "the last good node goes first")

    def test_every_node_is_tried_before_giving_up_unless_the_credential_is_refused(self):
        nodes = ["dtn01.x", "dtn02.x", "dtn03.x", "dtn04.x"]
        xfer, tried = self.opener(nodes, up=())
        told = _refusal(lambda: xfer.open_connection(quiet=True))
        self.assertEqual(self.nodes(tried), nodes)
        self.assertIn("on dtn01, dtn02, dtn03, dtn04", told)
        self.assertIn("Connection refused", told)
        self.assertIn("master-xfer-pool.log", told)
        # Every node refuses it the same way, and on a TOTP cluster each try
        # spends a window and counts towards locking the account.
        xfer, tried = self.opener(nodes[:3], up={"dtn02.x"}, failure=self.DENIED)
        told = _refusal(lambda: xfer.open_connection(quiet=True))
        self.assertEqual(self.nodes(tried), ["dtn01.x"])
        self.assertIn(self.DENIED, told)
        self.assertIn("no other node was tried", told)
        self.assertIn("config credentials", told)
        self.assertIsNone(xfer.node_of("pool"))

    def test_a_node_asked_for_is_used_alone(self):
        # Only a failure worth repeating on that node is tried again there,
        # which is open_master's call, so it gets the whole budget.
        xfer, tried = self.opener(["dtn01.x", "dtn02.x"], up=())
        told = _refusal(lambda: xfer.open_connection(node="dtn02.x", quiet=True))
        self.assertEqual(tried, [("dtn02.x", 3)])
        self.assertIn("on dtn02", told)

    def test_a_pool_behind_a_balancer_is_left_to_it_and_each_try_is_a_fresh_draw(self):
        xfer, tried = self.opener(["login01.x"], up={"pool"}, choosable=False)
        xfer.open_connection(quiet=True)
        self.assertEqual(tried, [("pool", 1)])
        self.assertIsNone(xfer.node_of("pool"))
        xfer, tried = self.opener([], up=(), choosable=False)
        told = _refusal(lambda: xfer.open_connection(quiet=True))
        self.assertEqual(tried, [("pool", 1)] * 3)
        self.assertIn("after 3 attempt(s)", told)
        xfer, tried = self.opener([], up=(), choosable=False, failure=self.DENIED)
        told = _refusal(lambda: xfer.open_connection(quiet=True))
        self.assertEqual(tried, [("pool", 1)], "a refused credential is not drawn again")
        self.assertIn(self.DENIED, told)


class TestTransferManySources(TransferHarness):
    """A glob expands to many names, and cp and rsync both take them.

    Truncating that list silently would lose data; refusing it outright would
    only move the damage into the user's habits. All of them arrive, over one
    connection, and — because an sftp channel and an rclone startup are not
    cheap — in as few invocations as their shape allows.
    """

    def batch(self, sources, dest, stat="dir", codes=None, listing="complete",
              **kw):
        """Run run_all on real local files, with the cluster replaced by bookkeeping."""
        xfer = self.transfers()
        xfer.rclone_bin = lambda: "/bin/true"
        opened, probed, ran, listed = [], [], [], []
        remaining = list(codes or [])
        dest_path = dest.split(":", 1)[-1].rstrip("/")
        paths = []
        for source in sources:
            local = self.root / source
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_text("")
            paths.append(str(local))

        def open_connection(node=None, quiet=False):
            opened.append(node)
            xfer.lease_take("pool")
            return "pool"

        def remote_stat(_ops, path, tries=None):
            probed.append(path)
            if path.rstrip("/") != dest_path:
                return {"IsDir": False, "Size": 0}
            if stat == "absent":
                return None
            return {"IsDir": True, "Size": -1} if stat == "dir" else stat

        def fake_listing(_ops, _path):
            if listing == "none":
                return None
            if listing == "complete":
                return {Path(p).name: {"IsDir": False, "Size": 0} for p in paths}
            return listing

        def fake_run(argv, **_kw):
            ran.append(argv)
            if "--files-from" in argv:
                names = Path(argv[argv.index("--files-from") + 1]).read_text()
                listed.append(names.split())
            return types.SimpleNamespace(
                returncode=remaining.pop(0) if remaining else 0)

        xfer.open_connection = open_connection
        xfer.close_connection = lambda tag, force=False: None
        with _patched(transfer.RcloneOps, "stat", remote_stat), \
                _patched(transfer.RcloneOps, "listing", fake_listing), \
                _patched(riding, "run_riding", _rclone_runs(fake_run)), \
                _patched(riding, "master_pid", lambda _sock: os.getpid()):
            specs = [TransferSpec(FakeBackend(), path, dest, up=True, **kw)
                     for path in paths]
            rc = xfer.run_all(specs, quiet=True)
        return types.SimpleNamespace(rc=rc, ran=ran, opened=opened,
                                     probed=probed, listed=listed)

    def test_one_directory_of_sources_is_one_rclone_run_over_one_connection(self):
        # Separate copies and checks would each open a fresh channel and
        # handshake on the sftp connection.
        names = [f"out_{n}.json" for n in range(12)]
        got = self.batch([f"here/{name}" for name in names], "remote:place")
        self.assertEqual(got.rc, 0)
        self.assertEqual(got.listed, [names])
        self.assertEqual(len(got.ran), 1)
        self.assertEqual(got.ran[0][-2], str(self.root / "here"))
        self.assertEqual(got.opened, [None])
        self.assertEqual(got.probed, ["place"], "the destination is asked about once")
        # One source still takes the single source path.
        got = self.batch(["here/only.bin"], "remote:place/")
        self.assertEqual(got.rc, 0)
        self.assertEqual(len(got.ran), 1)
        self.assertNotIn("--files-from", got.ran[0])
        self.assertEqual(got.ran[0][-2], str(self.root / "here" / "only.bin"))

    def test_sources_from_different_directories_all_arrive_until_one_fails(self):
        got = self.batch(["one/a", "two/b", "three/c"], "remote:place/")
        self.assertEqual((got.rc, len(got.ran)), (0, 3))
        with contextlib.redirect_stderr(io.StringIO()) as said:
            got = self.batch(["one/a", "two/b", "three/c"], "remote:place/",
                             codes=[0, 3, 0])
        self.assertEqual((got.rc, len(got.ran)), (3, 2))
        self.assertIn("stopped after 1 of 3", said.getvalue())
        told = _refusal(lambda: self.batch(
            ["here/a", "here/b"], "remote:place/",
            listing={"a": {"IsDir": False, "Size": 0}}))
        self.assertIn("place/b is not on the cluster", told, "a missing arrival")

    def test_many_sources_go_into_a_directory_never_into_one_file(self):
        """N files cannot become one file, and any invention there overwrites
        something, so the message has to carry the whole answer. A
        destination that does not exist yet can only mean "put these in
        there", so rclone is left to create it."""
        got = self.batch(["here/a", "here/b"], "remote:newdir", stat="absent",
                         listing="none")
        self.assertEqual(got.rc, 0)
        told = _refusal(lambda: self.batch(["here/a", "here/b"],
                                           "remote:place/one.txt",
                                           stat={"IsDir": False, "Size": 12}))
        self.assertIn("regular file", told)
        self.assertIn("place/one.txt", told)
        self.assertIn("place/one.txt/", told)   # the directory to use instead
        self.assertIn("remove", told)


class TestTransferCommandLine(unittest.TestCase):
    """argv in, rclone command line out — the whole plumbing, executed.

    The spec-level tests build a TransferSpec by hand and so never run
    cmd_transfer or the code that drives it. A green suite alongside a
    NameError in exactly that plumbing is what this is here to prevent, so it
    stubs only the network boundary and lets everything else really run.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "a.txt").write_text("aaa")
        (self.root / "b.txt").write_text("bbbb")
        for tree, name in (("tree", "c.txt"), ("tree2", "d.txt")):
            (self.root / tree).mkdir()
            (self.root / tree / name).write_text("cc")

    def invoke(self, argv, listing="complete"):
        """Run cmd_transfer for real, with only the cluster replaced.

        What it told the user is kept in self.said.
        """
        ran = []
        state = types.SimpleNamespace(
            dir=self.root / "state", ctl_dir=self.root / "ctl",
            xfer_socket=lambda tag: self.root / "ctl" / f"{tag}.sock",
            socket=lambda name: self.root / "ctl" / f"{name}.sock")
        state.dir.mkdir(parents=True, exist_ok=True)
        state.ctl_dir.mkdir(parents=True, exist_ok=True)
        backend = types.SimpleNamespace(
            name="fake", user="u", fqdn=lambda n: n,
            host_for=lambda n: n or "host", short=lambda n: (n or "").split(".")[0],
            target=lambda n=None: f"u@{n or 'host'}", cli_flag=lambda: "--fake")
        ctx = types.SimpleNamespace(
            backend=backend, explicit_backend=False,
            logins=types.SimpleNamespace(
                backend=backend, state=state,
                settings=types.SimpleNamespace(int=lambda k: 4, str=lambda k: "")))

        def fake_run(argv_, **_kw):
            ran.append(argv_)
            return types.SimpleNamespace(returncode=0)

        def fake_listing(_self, _path):
            if listing != "complete":
                return listing
            return {"a.txt": {"IsDir": False, "Size": 3},
                    "b.txt": {"IsDir": False, "Size": 4},
                    "tree": {"IsDir": True, "Size": -1}}

        # A stand-in cluster: a name with a dot in it is a file, anything else
        # is a directory. Enough to answer both questions resolve() asks.
        def fake_stat(_self, path, tries=None):
            if "." in path.rstrip("/").rsplit("/", 1)[-1]:
                return {"IsDir": False, "Size": 3}
            return {"IsDir": True, "Size": -1}

        self.said = io.StringIO()
        with _patched(Transfers, "open_connection",
                      lambda _s, node=None, quiet=False: "pool"), \
                _patched(Transfers, "close_connection",
                         lambda _s, tag, force=False: True), \
                _patched(Transfers, "rclone_bin", lambda _s: "/bin/true"), \
                _patched(RcloneOps, "stat", fake_stat), \
                _patched(RcloneOps, "listing", fake_listing), \
                _patched(riding, "run_riding", _rclone_runs(fake_run)), \
                _patched(riding, "master_pid", lambda _sock: os.getpid()), \
                contextlib.redirect_stderr(self.said):
            rc = cmd_transfer(ctx, argv)
        return rc, ran

    def test_what_reaches_rclone(self):
        a, b, tree, tree2 = (str(self.root / name)
                             for name in ("a.txt", "b.txt", "tree", "tree2"))
        place = ":sftp,shell_type=unix:place"
        cases = [
            ([a], [[a, place + "/"]], [], ["--files-from"]),
            ([a, b], [[str(self.root), place + "/"]], ["--files-from"],
             ["--delete-excluded"]),
            ([tree], [[tree, place + "/tree"]], [], []),
            (["-n", a], [[a, place + "/"]], ["--dry-run"], []),
            # A flag value is not read as a path.
            (["--exclude", "*.log", a], [[a, place + "/"]], ["--exclude"], []),
            # Sequential syncs would each mirror the destination, so every
            # source but the last would be deleted by the one after it. One
            # run over the whole list is what makes the delete pass see all
            # of them at once. --files-from on its own narrows the delete pass
            # to the listed files, quietly turning the mirror into a copy;
            # --delete-excluded is what puts the rest of the destination back
            # in scope.
            (["--sync", "-y", a, b], [[str(self.root), place + "/"]],
             ["sync", "--files-from", "--delete-excluded"], []),
            # These cannot collide: each lands in place/<name>, so neither
            # delete pass can see the other's files.
            (["--sync", "-y", tree, tree2],
             [[tree, place + "/tree"], [tree2, place + "/tree2"]], ["sync"], []),
        ]
        for args, ends, present, absent in cases:
            with self.subTest(args=args):
                rc, ran = self.invoke([*args, "remote:place/"])
                self.assertEqual(rc, 0)
                self.assertEqual([argv[-2:] for argv in ran], ends)
                for word in present:
                    self.assertIn(word, ran[0])
                for word in absent:
                    self.assertNotIn(word, ran[0])
        ssh = ran[0][ran[0].index("--sftp-ssh") + 1]
        self.assertTrue(ssh.startswith("ssh -F "), ssh)
        # Read back the way rclone reads it: one space-separated CSV record.
        words = next(csv.reader([ssh], delimiter=" "))
        self.assertIn(f"ControlPath={self.root / 'ctl' / 'pool.sock'}", words)
        self.assertTrue(any(w.startswith("ProxyCommand=/bin/sh -c '") for w in words),
                        "the rider's guard reaches ssh as one word")

    def test_two_mirrors_of_one_directory_are_reported(self):
        """--contents puts both sources at the same destination root, and there
        rclone really would have each sync delete the other's files."""
        with self.assertRaises(SystemExit):
            self.invoke(["--sync", "-y", "--contents", str(self.root / "tree"),
                         str(self.root / "tree2"), "remote:place/"])
        told = self.said.getvalue()
        self.assertIn("mirror", told)
        self.assertIn("one source at a time", told)


class TestTransferBatchGuards(unittest.TestCase):
    def _ctx(self):
        return types.SimpleNamespace(logins=types.SimpleNamespace(
            backend=FakeBackend(), state=None, settings=None))

    def test_sources_on_different_clusters_run_one_group_each_until_one_fails(self):
        """The user types one command, so sources on different clusters are
        gathered by cluster and run in the order they were typed."""
        for status, groups in ((0, [["nersc:/a", "nersc:/c"], ["fasrc:/b"]]),
                               (1, [["nersc:/a", "nersc:/c"]])):
            seen = []
            with _patched(cmds, "_transfer_from", lambda ctx, sources, *a, **k: (
                    seen.append(list(sources)), status)[1]), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = cmds.cmd_transfer(self._ctx(),
                                      ["nersc:/a", "fasrc:/b", "nersc:/c", "/here/"])
            self.assertEqual((rc, seen), (status, groups))


class TestCopyOnCluster(unittest.TestCase):
    """Both sides on one cluster: the copy runs there, in its shell."""

    def copy(self, sources, dest, operation="copy", passthrough=(), **flags):
        ran = []
        ctx = types.SimpleNamespace(
            resolve_login=lambda head: ("work", []),
            logins=types.SimpleNamespace(
                ensure=lambda _name: None,
                run_remote=lambda name, script, **_kw: (
                    ran.append(script), _proc(0))[1]))
        opts = types.SimpleNamespace(contents=False, via=None, dry_run=False,
                                     quiet=True)
        opts.__dict__.update(flags)
        _named, dest_path = split_endpoint(dest)
        rc = _copy_on_cluster(ctx, sources, dest, "fasrc", dest_path, opts,
                              list(passthrough), operation)
        return rc, ran

    def test_home_paths_land_in_the_home_directory(self):
        rc, ran = self.copy(["fasrc:~/results/"], "fasrc:~/backup/")
        self.assertEqual(rc, 0)
        self.assertEqual(ran, ["mkdir -p ./backup/ && cp -a ./results/. ./backup/"])
        home = Path(tempfile.mkdtemp(dir=str(support.SANDBOX)))
        self.addCleanup(shutil.rmtree, str(home), True)
        (home / "results").mkdir()
        (home / "results" / "r.txt").write_text("r")
        subprocess.run(["sh", "-c", "cd && " + ran[0]], check=True,
                       env=dict(os.environ, HOME=str(home)))
        self.assertTrue((home / "backup" / "r.txt").is_file())
        self.assertFalse((home / "~").exists())

    def test_the_hint_for_what_cannot_be_translated_leaves_no_tilde(self):
        told = _refusal(lambda: self.copy(["fasrc:~/a b"], "fasrc:~/dest",
                                          operation="move"))
        self.assertIn("cluster run LOGIN -- mv './a b' ./dest", told)


class TestPullOntoACaseFoldingDisk(unittest.TestCase):
    """macOS stores `README` and `readme` as one file; rsync would keep one."""

    def pull(self, listing, folds=True, args=()):
        ran = []
        ctx = types.SimpleNamespace(
            login=lambda name=None: name or "work",
            logins=types.SimpleNamespace(
                ensure=lambda _n: None, node_of=lambda _n: "login01.x",
                run_remote=lambda name, cmd, timeout=None: (
                    ran.append(cmd), listing)[1]),
            state=types.SimpleNamespace(socket=lambda n: Path("/nonexistent/s")),
            backend=types.SimpleNamespace(user="user", host_for=lambda n: n,
                                          target=lambda n=None: f"user@{n}"),
            settings=types.SimpleNamespace(int=lambda k: 4))
        rsynced = []
        with _patched(plat, "IS_MAC", True), \
                _patched(cmds, "_folds_case", lambda _p: folds), \
                _patched(cmds, "need_rsync", lambda _w: None), \
                _patched(riding, "run_riding", _rclone_runs(
                    lambda argv: rsynced.append(argv) or _proc(0))):
            rc = cmds.cmd_pull(ctx, [*args, "~/data/", "/tmp/somewhere"])
        return rc, ran, rsynced

    def test_names_that_differ_only_by_case_are_refused_and_named(self):
        listing = _proc(0, "banner\n__cluster_names__\0./data/\0./data/README\0"
                           "./data/readme\0./data/Café\0./data/CAFÉ\0"
                           "./data/other\0")
        told = _refusal(lambda: self.pull(listing))
        self.assertIn("./data/README and ./data/readme", told)
        self.assertIn("./data/CAFÉ and ./data/Café", told)
        self.assertNotIn("other", told)

    def test_the_source_is_listed_as_the_home_path_it_names_on_a_folding_disk(self):
        rc, ran, rsynced = self.pull(_proc(0, "__cluster_names__\0./data/a\0"))
        self.assertEqual(rc, 0)
        self.assertIn("find ./data/ -print0", ran[0])
        self.assertEqual(len(rsynced), 1)
        rc, ran, _rsynced = self.pull(_proc(0, ""), folds=False)
        self.assertEqual((rc, ran), (0, []), "a case-sensitive disk is not asked about")

    def test_a_file_cut_short_is_kept_aside_for_the_next_run(self):
        # A resumed pull finishes a big file instead of starting it again,
        # and --delete never takes the partial directory (rsync excludes a
        # relative one itself). rsync refuses --partial-dir beside any of the
        # options that treat a file cut short their own way.
        for own in ("", "--inplace", "--append", "--append-verify",
                    "--partial-dir=keep", "--write-devices"):
            with self.subTest(own=own), \
                    _patched(plat, "_RSYNC_PARTIAL", ["--partial-dir=.rsync-partial"]):
                _rc, _ran, rsynced = self.pull(_proc(0, "__cluster_names__\0"),
                                               args=[own] if own else [])
                self.assertEqual("--partial-dir=.rsync-partial" in rsynced[0], not own)
                if own:
                    self.assertIn(own, rsynced[0])

    def test_a_pull_that_reads_standard_input_is_not_run_again(self):
        made = []

        class Ride:
            def __init__(self, links, settings, once=None):
                made.append(once)

            def run(self, argv):
                return 0

        with _patched(cmds, "Ride", Ride), \
                _patched(plat, "_RSYNC_PARTIAL", ["--partial-dir=.rsync-partial"]):
            self.pull(_proc(0, "__cluster_names__\0"), args=["--files-from=-"])
            self.pull(_proc(0, "__cluster_names__\0"), args=["--exclude=-"])
        self.assertEqual(made, ["its --files-from was standard input, which a "
                                "second run would find empty", None])

    def test_what_reads_standard_input_is_told_apart(self):
        from clustertool.commands.transfers import rsync_reads_stdin as reads

        for extra in (["--files-from=-"], ["--exclude-from=-"], ["--include-from=-"],
                      ["--read-batch=-"], ["--filter=merge -"], ["-f. -"],
                      ["--filter=:- -"], ["--files-from=/dev/stdin"],
                      ["--exclude-from=/dev/fd/0"], ["--filter=merge /dev/stdin"],
                      ["-f. /proc/self/fd/0"]):
            self.assertIsNotNone(reads(extra), extra)
        for extra in ([], ["--files-from=list"], ["--exclude=-"], ["-f- -"],
                      ["--files-from=/dev/stdin.txt"], ["--exclude=/dev/stdin"],
                      ["--filter=merge rules"], ["--files-from=/dev/fd/01"],
                      ["--filter=merge rules"], ["-v"], ["--delete"]):
            self.assertIsNone(reads(extra), extra)

    def test_the_partial_directory_is_what_this_rsync_can_say(self):
        dir_help = "  --partial-dir=DIR  put a partially ...\n"
        for rc, text, err, flags in (
                (0, dir_help, "", ["--partial-dir=.rsync-partial"]),
                (0, "  --partial  keep partially transferred files\n", "",
                 ["--partial"]),
                (0, "", "", []),
                # An rsync whose help fails, or complains, as openrsync may,
                # is asked for neither.
                (1, dir_help, "", []),
                (0, dir_help, "rsync: unknown option -- -\n", []),
                (127, "", "", [])):
            with self.subTest(rc=rc, err=err), \
                    _patched(plat, "_RSYNC_PARTIAL", None), \
                    _patched(plat, "run", lambda *_a, **_kw: _proc(rc, text, err)):
                self.assertEqual(plat.rsync_partial_flags(), flags)

    def test_a_listing_it_could_not_finish_checks_what_it_got(self):
        with contextlib.redirect_stderr(io.StringIO()) as said:
            rc, _ran, rsynced = self.pull(_proc(255, "", "Connection lost\n"))
        self.assertEqual((rc, len(rsynced)), (0, 1))
        self.assertIn("(Connection lost); pulling without that check", said.getvalue())
        # find lists what it can read and exits 1 for the rest.
        denied = "find: './data/private': Permission denied\n"
        with contextlib.redirect_stderr(io.StringIO()) as said:
            rc, _ran, rsynced = self.pull(
                _proc(1, "__cluster_names__\0./data/a\0./data/b\0", denied))
        self.assertEqual((rc, len(rsynced)), (0, 1))
        self.assertIn("listed only part of ~/data/ to look for names", said.getvalue())
        self.assertIn("Permission denied); the rest is pulled unchecked",
                      said.getvalue())
        told = _refusal(lambda: self.pull(
            _proc(1, "__cluster_names__\0./data/A\0./data/a\0", denied)))
        self.assertIn("./data/A and ./data/a", told)

    def test_a_big_tree_is_listed_for_as_long_as_it_takes(self):
        timeouts = []
        ctx = types.SimpleNamespace(logins=types.SimpleNamespace(
            run_remote=lambda name, cmd, timeout="unset": (
                timeouts.append(timeout), _proc(0, "__cluster_names__\0"))[1]))
        with _patched(cmds, "_folds_case", lambda _p: True):
            cmds._refuse_case_clashes(ctx, "work", "~/data/", "/tmp/somewhere")
        self.assertEqual(timeouts, [None])

    def test_the_file_system_itself_is_asked(self):
        where = Path(tempfile.mkdtemp(dir=str(support.SANDBOX)))
        self.addCleanup(shutil.rmtree, str(where), True)
        probe = where / "Probe"
        probe.write_text("")
        expected = (where / "probe").exists()
        probe.unlink()
        self.assertEqual(_folds_case(str(where / "not" / "yet")), expected)
        self.assertEqual(list(where.iterdir()), [], "the probe file is removed")


class TestTransferLock(TransferHarness):
    """Opening and tearing down a transfer master wait for each other."""

    def test_a_live_holder_is_waited_for_and_named(self):
        # A holder is an open in progress, which may take minutes across
        # several nodes; going ahead without the lock would throw its master
        # away, and giving up would fail a transfer over a queue.
        xfer = self.transfers()
        holder = support.hold_flock(self, xfer.state.dir / "transfer-pool.lock", 0.4)
        start = time.monotonic()
        with contextlib.redirect_stderr(io.StringIO()) as said:
            with xfer.tag_lock("pool"):
                waited = time.monotonic() - start
        self.assertGreater(waited, 0.2)
        self.assertIn(f"waiting for pid {holder.pid}", said.getvalue())
        self.assertIn("to finish with transfer connection pool", said.getvalue())

    def test_a_killed_holder_leaves_no_wedge(self):
        xfer = self.transfers()
        holder = support.hold_flock(self, xfer.state.dir / "transfer-pool.lock", 60)
        holder.kill()
        holder.wait()
        start = time.monotonic()
        with xfer.tag_lock("pool"):
            pass
        self.assertLess(time.monotonic() - start, 5)


class _Link:
    """A connection a transfer rides, lost and restored on cue."""

    def __init__(self, name="transfer connection pool", fails=()):
        self.name, self.node, self.sock = name, None, Path("/nonexistent/s.sock")
        self.logins = types.SimpleNamespace(last_failure="")
        self.gone = False
        self.fails = list(fails)
        self.restored = 0

    def lost(self):
        return self.gone

    def answers(self):
        return not self.gone

    def broken(self):
        return self.lost() or not self.answers()

    def restore(self):
        if self.fails:
            raise self.fails.pop(0)
        self.restored += 1
        self.gone = False


class TestATransferRidesOutALostConnection(unittest.TestCase):
    """A lost connection is restored and the transfer resumed; a burst of
    losses ends it, losses spread over days never do, and rclone's own
    failures on a connection that is fine are its answer."""

    def setUp(self):
        self.clock = support.FakeClock()
        self.said = io.StringIO()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(_patched(time, "monotonic", self.clock.monotonic))
        stack.enter_context(_patched(time, "sleep", self.clock.sleep))
        stack.enter_context(contextlib.redirect_stderr(self.said))

    def ride(self, runs, link=None, **kw):
        """Ride.run over *runs*: ``(seconds, status, lost?)`` for each rclone run."""
        link = link or _Link()
        queue = list(runs)
        started = []

        def run_riding(argv, links, settings, env=None, heartbeat=False):
            seconds, rc, lose = queue.pop(0)
            started.append(argv)
            self.clock.now += seconds
            if lose:
                link.gone = True
            return rc, ([link] if lose and lose != "end" else [])

        ride = riding.Ride([link], Settings("fasrc"), **kw)
        with _patched(riding, "run_riding", run_riding):
            rc = ride.run(lambda: ["rclone", "copy", str(len(started))])
        return rc, started, link

    def test_a_lost_connection_is_restored_and_the_transfer_resumed(self):
        rc, started, link = self.ride([(3600, -15, True), (600, 0, False)])
        self.assertEqual((rc, len(started), link.restored), (0, 2, 1))
        self.assertEqual(started[1][-1], "1", "the argv is built afresh")
        said = self.said.getvalue()
        self.assertIn("transfer connection pool was lost 3600s into the transfer",
                      said)
        self.assertIn("resuming the transfer", said)
        rc, started, link = self.ride([(60, 12, True), (60, 0, False)],
                                      once="its --files-from was standard input")
        self.assertEqual((rc, len(started), link.restored), (12, 1, 0))
        self.assertIn("transfer connection pool was lost 60s into the transfer; it "
                      "is not resumed, since its --files-from was standard input",
                      self.said.getvalue())

    def test_the_links_are_asked_not_the_status_read(self):
        """A path not found, a fatal error, a limit the caller set, rsync's
        partial transfer: whatever the status, a failure on a connection that
        is fine stands."""
        def on_its_way_out():
            # Its process is still there a moment after it closed the
            # channels, but it no longer answers at its socket.
            link = _Link()
            link.answers = lambda: False
            return link

        cases = [([(60, status, False)], None, (status, 1, 0))
                 for status in (1, 3, 4, 5, 9, 10, 12, 23, 255)]
        cases += [
            # A connection lost after the last look is found at the end.
            ([(60, 5, "end"), (60, 0, False)], None, (0, 2, 1)),
            ([(60, 12, False), (60, 0, False)], on_its_way_out, (0, 2, 1)),
            ([(6 * 3600, 1, True)] * 40 + [(60, 0, False)], None, (0, 41, 40)),
        ]
        for runs, link, want in cases:
            with self.subTest(runs=runs[:2], link=link):
                rc, started, got = self.ride(runs, link=link and link())
                self.assertEqual((rc, len(started), got.restored), want)

    def test_a_success_asks_nothing(self):
        link = _Link()
        link.lost = lambda: self.fail("asked about the connection")
        rc, started, _link = self.ride([(60, 0, False)], link=link)
        self.assertEqual((rc, len(started)), (0, 1))

    def test_a_burst_of_losses_ends_it_and_says_so(self):
        limit = Settings("fasrc").int("TRANSFER_RECONNECTS")
        runs = [(5, 1, True)] * (limit + 1)
        with self.assertRaises(SystemExit):
            self.ride(runs)
        said = self.said.getvalue()
        self.assertIn(f"gave up reconnecting after {limit + 1} failures", said)
        self.assertIn(f"TRANSFER_RECONNECTS ({limit})", said)
        waits = [s for s in self.clock.slept if s]
        self.assertEqual(waits, sorted(waits), "the waits grow")

    def test_a_reconnect_that_fails_for_now_is_tried_again_unless_refused(self):
        link = _Link(fails=[OSError("Network is unreachable")])
        rc, started, link = self.ride([(600, 1, True), (60, 0, False)], link=link)
        self.assertEqual((rc, len(started), link.restored), (0, 2, 1))
        self.assertIn("reconnecting transfer connection pool failed "
                      "(Network is unreachable)", self.said.getvalue())
        link = _Link(fails=[SystemExit("cluster: Permission denied (password)")])
        with self.assertRaises(SystemExit):
            self.ride([(600, 1, True), (60, 0, False)], link=link)
        self.assertEqual(link.restored, 0, "a refused credential ends it at once")

    def test_a_far_side_rclone_is_given_time_to_stop_itself(self):
        rc, _started, _link = self.ride([(600, 255, True), (60, 0, False)],
                                        settle=130)
        self.assertEqual(rc, 0)
        # The backoff before the reconnect counts towards it.
        self.assertEqual(sum(self.clock.slept), 130)
        self.assertIn("to stop itself", self.said.getvalue())

    def test_the_wait_ends_once_the_far_side_says_it_has_stopped(self):
        answers = [False, False, True]
        rc, _started, _link = self.ride([(600, 255, True), (60, 0, False)],
                                        settle=130, settled=lambda: answers.pop(0))
        self.assertEqual((rc, answers), (0, []))
        # Asked at once, and at each look after.
        self.assertEqual(self.clock.slept[-2:], [riding.LINK_POLL] * 2)
        self.assertLess(sum(self.clock.slept), 130)


class TestTheLinksATransferRides(TransferHarness):
    """What says a connection was lost, and what gets it back."""

    def test_the_master_is_named_by_its_pid(self):
        sock = self.root / "m.sock"
        sock.write_text("")
        answers = [_proc(0, stderr="Master running (pid=4321)\r\n"), _proc(255),
                   _proc(124)]
        with _patched(plat, "run", lambda argv, timeout=None: answers.pop(0)):
            self.assertEqual(sshmux.master_pid(sock), 4321)
            self.assertIsNone(sshmux.master_pid(sock))
            self.assertIs(sshmux.master_pid(sock), sshmux.UNANSWERED,
                          "a master that holds its socket and is slow is there")
        self.assertIsNone(sshmux.master_pid(self.root / "none.sock"))

    def link(self, pids):
        sock = self.root / "m.sock"
        sock.write_text("")
        pids = list(pids)
        asked = []

        def master_pid(_sock):
            asked.append(True)
            return pids.pop(0) if len(pids) > 1 else pids[0]

        with _patched(riding, "master_pid", master_pid):
            link = riding.Link("x", sock, lambda lost: None, None)
        return link, asked, master_pid

    def test_a_look_is_whether_the_masters_process_is_there(self):
        link, asked, master_pid = self.link([100])
        alive = {100: True}
        with _patched(riding, "master_pid", master_pid), \
                _patched(plat, "pid_alive", lambda pid: alive.get(pid, False)):
            self.assertFalse(link.lost())
            self.assertEqual(len(asked), 1, "the master itself is not asked")
            alive[100] = False
            self.assertTrue(link.lost(), "the master it started on is gone")
        # One that had not said its pid, just opened and not answering yet,
        # then slow to, is asked for it: not a loss to restart over.
        link, asked, master_pid = self.link([None, sshmux.UNANSWERED, 100])
        with _patched(riding, "master_pid", master_pid), \
                _patched(plat, "pid_alive", lambda pid: pid == 100):
            for master in (None, 100, 100):
                self.assertFalse(link.lost())
                self.assertEqual(link.master, master)
        self.assertEqual(len(asked), 3)
        link, _asked, master_pid = self.link([None, None])
        with _patched(riding, "master_pid", master_pid):
            self.assertTrue(link.lost())

    def test_after_a_failure_a_replaced_or_silent_master_does_not_answer(self):
        link, _asked, master_pid = self.link([100, 100, sshmux.UNANSWERED, None,
                                              200])
        with _patched(riding, "master_pid", master_pid):
            self.assertTrue(link.answers())
            self.assertTrue(link.answers(), "a slow master has not gone")
            self.assertFalse(link.answers(), "nothing holds the socket")
            self.assertFalse(link.answers(), "a replaced master took its channels")

    def reopen(self, now):
        xfer = self.transfers()
        opened, discarded = [], []
        xfer._open_locked = lambda *a: opened.append(a)
        xfer._discard_connection = lambda tag: discarded.append(tag)
        with _patched(transfer, "master_pid", lambda _sock: now):
            node = xfer.reopen("pool", 100)
        xfer.lease_drop("pool")
        return opened, discarded, node

    def test_a_lost_master_is_opened_again_and_any_other_taken(self):
        # A master another run already reopened, or the one taken for lost
        # answering after all: others ride it, and a new one would cut them
        # off and spend a TOTP window on FASRC. So would killing one that is
        # only slow.
        for now, reopened in ((200, False), (100, False), (sshmux.UNANSWERED, False),
                              (None, True)):
            opened, discarded, _node = self.reopen(now=now)
            self.assertEqual((len(opened), discarded),
                             (1, ["pool"]) if reopened else (0, []), now)


class TestRunningRclone(unittest.TestCase):
    """riding.run_riding, with a real child standing in for rclone."""

    def setUp(self):
        self.settings = Settings("fasrc")
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(_patched(riding, "LINK_POLL", 0.1))

    def test_its_own_status_when_its_links_hold_and_a_heartbeat_reaches_it(self):
        rc, lost = riding.run_riding(
            [sys.executable, "-c", "import time, sys; time.sleep(0.4); sys.exit(3)"],
            [_Link()], self.settings)
        self.assertEqual((rc, lost), (3, []))
        rc, _lost = riding.run_riding(
            [sys.executable, "-c",
             "import sys\nfor _ in range(3): sys.stdin.readline()"],
            [_Link()], self.settings, heartbeat=True)
        self.assertEqual(rc, 0)

    def test_it_is_stopped_once_a_link_is_lost(self):
        link = _Link()
        looks = []

        def lost():
            looks.append(True)
            return len(looks) >= 2

        link.lost = lost
        started = time.monotonic()
        rc, gone = riding.run_riding(
            [sys.executable, "-c", "import time; time.sleep(30)"], [link],
            self.settings)
        self.assertEqual(gone, [link])
        self.assertNotEqual(rc, 0)
        self.assertLess(time.monotonic() - started, 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
