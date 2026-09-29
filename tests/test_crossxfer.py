#!/usr/bin/env python3
"""Cross-cluster transfer: addressing, planning and engine fallback.

Run: python3 -m unittest tests.test_crossxfer
(or the whole suite: python3 -m unittest discover -s tests -p 'test_*.py')
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock
from pathlib import Path

# For `support`, which puts this checkout on sys.path and sets up the sandbox
# every test runs in, so it is imported before anything from clustertool.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import support  # noqa: E402
from support import FakeBackend, _patched, _refusal, temp_state  # noqa: E402
from clustertool import config, crossxfer, platform as plat, riding  # noqa: E402
from clustertool.backends.base import Backend  # noqa: E402
from clustertool.config import Settings  # noqa: E402
from clustertool.transfer import (HomeUnknown, RcloneOps, ShellHome,  # noqa: E402
                                  TransferSpec, Transfers)

DIR = {"IsDir": True, "Size": -1}


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class _FakeSide:
    """One side of a cross-cluster transfer, without touching a real backend."""

    def __init__(self, name, lendable):
        self.name = name
        self.path = "/p"
        self.backend = types.SimpleNamespace(
            label=name.upper(),
            lends_credential=lendable,
            agent_identities=lambda: (["/key"] if lendable else []),
        )

    def __repr__(self):
        return f"{self.name}:{self.path}"


def _cross_stub(src_lendable, dst_lendable, engine="auto", executor=None):
    return types.SimpleNamespace(
        source=_FakeSide("fasrc", src_lendable),
        dest=_FakeSide("nersc", dst_lendable),
        engine=engine,
        want_executor=executor,
        _lendable=crossxfer.CrossTransfer._lendable,
    )


def _stub_with_methods(*a, **kw):
    """A stub the real planning methods can be called on, unbound."""
    stub = _cross_stub(*a, **kw)
    stub.choose_executor = lambda: crossxfer.CrossTransfer.choose_executor(stub)
    return stub


def _settings(**values):
    """Settings answering *values*, and 30 for anything else."""
    return types.SimpleNamespace(int=lambda key: values.get(key, 30),
                                 str=lambda key: "")


class _Rclone:
    """What rclone's path questions see, keyed by rclone's name for a path.

    The first *flaky* questions get the answer a busy node gives: no answer.
    """

    def __init__(self, stats=None, flaky=0):
        self.stats = dict(stats or {})
        self.flaky = flaky
        self.asked = []

    def answer(self, words):
        self.asked.append(words[-1])
        if self.flaky:
            self.flaky -= 1
            return _proc(1, "", "couldn't connect SSH: connection reset by peer")
        stat = self.stats.get(words[-1])
        if stat is None:
            return _proc(3, "", "object not found")
        return _proc(0, json.dumps(stat))


class _ExecutorShell:
    """The shell on the executor cluster, with its home and its rclone."""

    def __init__(self, world, home="/global/u1/u/user", banner="", home_lost=0):
        self.world = world
        self.home = home
        self.banner = banner
        #: How many times the home question gets the answer of a lost connection.
        self.home_lost = home_lost
        self.commands = []
        self.prefix = ["ssh", "executor"]
        #: Whether a run's processes there have all gone (still_there).
        self.far_gone = True

    def __call__(self, command, timeout=600):
        self.commands.append(command)
        if command.startswith("pgrep "):
            return _proc(1 if self.far_gone else 0)
        if command == "cd && pwd -P":
            if self.home_lost:
                self.home_lost -= 1
                return _proc(255, "", "cluster: the connection to login01 is gone")
            return _proc(0, f"{self.banner}{self.home}\n")
        if command.endswith("version 2>/dev/null | head -1"):
            return _proc(0, "rclone v1.66.0\n")
        words = shlex.split(command)
        if "lsjson" in words:
            return self.world.answer(words)
        return _proc(127, "", f"unexpected: {command}")


class TestCrossClusterAddressing(unittest.TestCase):
    def test_a_backend_name_or_alias_qualifies_a_path_and_nothing_else_does(self):
        cases = [("fasrc:~/x", ("fasrc", "~/x")),
                 ("nersc:/global/x", ("nersc", "/global/x")),
                 ("perlmutter:/x", ("nersc", "/x")),
                 # These already mean something to an ordinary transfer and
                 # must not be mistaken for a cluster name.
                 ("local:/x", (None, "local:/x")), ("remote:x", (None, "remote:x")),
                 # A plain host:path or a path containing a colon is not ours
                 # to reinterpret.
                 ("somehost:/x", (None, "somehost:/x")),
                 ("/plain/path", (None, "/plain/path"))]
        for given, want in cases:
            self.assertEqual(crossxfer.split_endpoint(given), want, given)

    def test_cross_needs_two_different_clusters(self):
        self.assertTrue(crossxfer.is_cross("fasrc:/a", "nersc:/b"))
        self.assertFalse(crossxfer.is_cross("fasrc:/a", "fasrc:/b"))
        self.assertFalse(crossxfer.is_cross("./a", "nersc:/b"))
        self.assertFalse(crossxfer.is_cross("./a", "remote:b"))


class TestCrossClusterPlanning(unittest.TestCase):
    """Who runs the transfer follows from whose credential can travel."""

    def test_the_executor_is_the_side_whose_peer_can_lend_a_credential(self):
        # FASRC types its password into a pty and can lend nothing, so it must be
        # the executor; NERSC's certificate is a file, so it can be the peer.
        plan = crossxfer.CrossTransfer
        stub = _stub_with_methods(src_lendable=False, dst_lendable=True)
        executor, peer = plan.choose_executor(stub)
        self.assertEqual((executor.name, peer.name), ("fasrc", "nersc"))
        self.assertEqual(plan.plan(stub)[0], "direct")
        # With a real choice the reading side runs it.
        executor, _peer = plan.choose_executor(_cross_stub(True, True))
        self.assertEqual(executor.name, "fasrc")
        stub = _stub_with_methods(src_lendable=False, dst_lendable=False)
        self.assertEqual(plan.choose_executor(stub), (None, None))
        self.assertEqual(plan.plan(stub)[0], "relay")

    def test_a_route_or_executor_that_cannot_work_is_refused(self):
        plan = crossxfer.CrossTransfer
        stub = _stub_with_methods(False, False, engine="direct")
        self.assertIn("relay", _refusal(lambda: plan.plan(stub)))
        # Naming NERSC as the executor means FASRC would have to lend a password.
        stub = _cross_stub(False, True, executor="nersc")
        message = _refusal(lambda: plan.choose_executor(stub))
        self.assertIn("cannot authenticate", message)
        self.assertIn("relay", message)
        stub = _cross_stub(False, True, executor="elsewhere")
        self.assertTrue(_refusal(lambda: plan.choose_executor(stub)))


class _Bare(Backend):
    name = "bare"
    user = "u"
    pool_host = "host.example"


class TestAgentForwarding(unittest.TestCase):
    def test_forward_agent_is_set_in_place_not_appended(self):
        # ssh uses the FIRST value it is given for an option, so a trailing
        # "-o ForwardAgent=yes" after common_opts would be silently ignored.
        backend = _Bare(Settings("bare"))
        for opts, want in ((backend.ssh_opts(forward_agent=True), "ForwardAgent=yes"),
                           (backend.ssh_argv(forward_agent=True, extra=["-N", "-f"]),
                            "ForwardAgent=yes"),
                           (backend.ssh_opts(), "ForwardAgent=no")):
            self.assertEqual([o for o in opts if o.startswith("ForwardAgent=")], [want])

    def test_a_forwarding_master_gets_its_own_tag(self):
        # Reusing a plain master for a forwarded transfer would leave the far side
        # unauthenticated with no visible reason.
        root = Path(tempfile.mkdtemp(dir=str(support.SANDBOX)))
        self.addCleanup(shutil.rmtree, str(root), True)
        state = types.SimpleNamespace(
            dir=root, ctl_dir=root, xfer_socket=lambda tag: root / f"xfer-{tag}.sock")
        xfer = Transfers(types.SimpleNamespace(
            backend=_Bare(Settings("bare")), state=state, settings=Settings("bare"),
            _socket_live=lambda sock: True))
        self.assertEqual(xfer.open_connection(), "pool")
        self.assertEqual(xfer.open_connection(forward_agent=True), "pool-fwd")


class TestExecutorSidePathQuestions(unittest.TestCase):
    """The same cp-like semantics, asked on a cluster instead of this machine."""

    def _near(self, stats=None, home="/global/u1/u/user", banner=""):
        """The executor's own disk, as the rclone running there sees it."""
        shell = _ExecutorShell(_Rclone(stats), home, banner)
        home_ops = ShellHome(shell)
        near = RcloneOps(["rclone", "--config", "/dev/null", "--copy-links"],
                         home_ops.expand,
                         run=lambda argv, timeout, **_watch: shell(shlex.join(argv), timeout),
                         where="on FASRC")
        return home_ops, near, shell

    def test_home_is_resolved_on_the_far_side(self):
        home, _near, _shell = self._near(banner="Welcome to login01\n")
        # Not this machine's home, and physically resolved: NERSC's /global/homes
        # path is a symlink, which breaks anything comparing paths later. A
        # login banner is not mistaken for the home.
        self.assertEqual(home.expand("~/data"), "/global/u1/u/user/data")
        self.assertEqual(home.expand("~"), "/global/u1/u/user")
        self.assertEqual(home.expand("/abs/path"), "/abs/path")
        # A home that was not answered is asked again.
        home = ShellHome(_ExecutorShell(_Rclone(), home_lost=1))
        with self.assertRaises(HomeUnknown) as lost:
            home.expand("~/data")
        self.assertIn("exited 255: cluster: the connection to login01 is gone",
                      str(lost.exception))
        self.assertEqual(home.expand("~/data"), "/global/u1/u/user/data")
        for printed in ("", "Welcome to login01", "~"):
            with self.subTest(printed=printed), self.assertRaises(HomeUnknown):
                ShellHome(lambda _command: _proc(0, printed + "\n")).home()

    def test_where_a_source_lands_by_the_far_sides_answers(self):
        cases = [
            ("/scratch/tree", "remote:place", {"/scratch/tree": DIR}, False,
             "/scratch/tree", "place/tree"),
            # A home path is asked about where it lives.
            ("~/tree", "remote:place", {"/global/u1/u/user/tree": DIR}, False,
             "/global/u1/u/user/tree", "place/tree"),
            # A file to a new name still becomes copyto.
            ("/scratch/f.bin", "remote:place/n.bin",
             {"/scratch/f.bin": {"IsDir": False, "Size": 5}}, None,
             "/scratch/f.bin", "place/n.bin"),
        ]
        for source, dest, stats, dest_is_dir, asked, lands in cases:
            with self.subTest(source=source):
                _home, near, shell = self._near(stats)
                spec = TransferSpec(FakeBackend(), source, dest, up=True, ops=near)
                spec.resolve(remote_is_dir=lambda p, d=dest_is_dir: d)
                self.assertEqual(shlex.split(shell.commands[-1])[-1], asked)
                self.assertEqual((spec.local_arg(), spec.remote_side), (asked, lands))
        self.assertEqual(spec.operation, "copyto")
        _home, near, _shell = self._near()
        spec = TransferSpec(FakeBackend(), "/scratch/gone", "remote:place",
                            up=True, ops=near)
        told = _refusal(lambda: spec.resolve(remote_is_dir=lambda p: True))
        self.assertIn("/scratch/gone does not exist on FASRC", told)


def _endpoint(name, path, settings):
    backend = types.SimpleNamespace(
        name=name, label=name.upper(), user="user",
        host_for=lambda node: node or f"{name}.example",
        inbound_transfer_host=lambda: f"dtn01.{name}.example",
        short=lambda node: (node or "").split(".")[0])
    return types.SimpleNamespace(name=name, path=path, backend=backend,
                                 settings=settings, logins=None)


def _steady_link(name):
    """A Link stand-in whose connection is never lost."""
    return types.SimpleNamespace(name=name, sock=Path(f"/nonexistent/{name}.sock"),
                                 node=None, lost=lambda: False)


def _rclone_runs(fake):
    """riding.run_riding for a fake rclone given argv (and env) alone."""
    return lambda argv, links, settings, env=None, heartbeat=False: (
        fake(argv, env=env).returncode, [])


def _untied(command):
    """The rclone argv inside a crossxfer.tied() command."""
    words = shlex.split(command)
    assert words[:3] == ["bash", "-c", crossxfer.TIED], words[:3]
    return words[6:]


def _cross(source, dest, **kw):
    """A CrossTransfer between two stand-in endpoints, without loading backends."""
    cross = object.__new__(crossxfer.CrossTransfer)
    cross.__dict__.update(
        source=source, dest=dest, engine="auto", operation="copy",
        contents=False, symlinks="follow", dry_run=False, progress=False,
        quiet=True, keep=True, transfers=0, checkers=0, extra=[],
        allow_agent=True, peer_node=None, executor_node=None, via=None,
        _inherited=None)
    cross.__dict__.update(kw)
    return cross


TRANSFER_SETTINGS = dict(
    TRANSFER_IO_TIMEOUT=77, TRANSFER_RETRIES=2, TRANSFER_CONNECTIONS=0,
    TRANSFER_MULTI_THREAD_STREAMS=1, TRANSFER_TRANSFERS=5, TRANSFER_CHECKERS=3,
    SHARED_TRANSFER_TRANSFERS=2, SHARED_TRANSFER_CHECKERS=1,
    PEER_CONNECT_TIMEOUT=10)


class TestDirectTransfer(unittest.TestCase):
    """The rclone on the executor, run as a transfer from here would be."""

    def run_direct(self, world, arrives=None, shell=None, **kw):
        """Run _direct_body; *arrives* is what the peer holds once rclone ran."""
        settings = _settings(**TRANSFER_SETTINGS)
        executor = _endpoint("fasrc", "/n/data/f.bin", settings)
        peer = _endpoint("nersc", "~/in/", settings)
        cross = _cross(executor, peer, **kw)
        shell = shell or _ExecutorShell(world)
        cross._on_executor = lambda *_a: shell
        cross._pick_peer_host = lambda run, _peer: ("dtn01.nersc.example", None)
        ran = []

        def rclone_there(argv, env=None):
            ran.append(_untied(argv[-1]))
            world.stats.update(arrives or {})
            return _proc(0)

        agent = types.SimpleNamespace(env=lambda: {}, loaded=lambda: "key")
        with _patched(riding, "run_riding", _rclone_runs(rclone_there)), \
                _patched(time, "sleep", lambda _s: None):
            rc = cross._direct_body(executor, peer, _steady_link("s"), agent)
        return rc, ran, shell

    FILE = {"IsDir": False, "Size": 5000}
    ARRIVED = {":sftp,shell_type=unix:in/f.bin": FILE}

    def test_the_transfer_settings_reach_the_rclone_there_and_what_arrived_is_checked(self):
        # A probe that gets no answer is asked again.
        world = _Rclone({"/n/data/f.bin": self.FILE}, flaky=2)
        rc, ran, shell = self.run_direct(world, arrives=self.ARRIVED)
        self.assertEqual(rc, 0)
        self.assertEqual(world.asked[:3], ["/n/data/f.bin"] * 3)
        self.assertEqual(world.asked[-1], ":sftp,shell_type=unix:in/f.bin")
        argv = ran[0]
        self.assertEqual(argv[1], "copy")
        for flag, value in (("--timeout", "77s"), ("--retries", "3"),
                            ("--transfers", "5"), ("--checkers", "3"),
                            ("--config", "/dev/null")):
            self.assertEqual(argv[argv.index(flag) + 1], value, flag)
        self.assertIn("--copy-links", argv)
        self.assertEqual(argv[-2:], ["/n/data/f.bin", ":sftp,shell_type=unix:in/"])
        ssh = argv[argv.index("--sftp-ssh") + 1]
        self.assertIn("ConnectTimeout=10", ssh)
        self.assertTrue(ssh.endswith("user@dtn01.nersc.example"), ssh)
        # The executor's own disk is asked with links followed.
        probe = shlex.split(next(c for c in shell.commands if "lsjson" in c))
        self.assertIn("--copy-links", probe)
        self.assertNotIn("--sftp-ssh", probe)
        told = _refusal(lambda: self.run_direct(_Rclone({"/n/data/f.bin": self.FILE})))
        self.assertIn("f.bin is not on NERSC", told)
        short = {":sftp,shell_type=unix:in/f.bin": {"IsDir": False, "Size": 10}}
        told = _refusal(lambda: self.run_direct(
            _Rclone({"/n/data/f.bin": self.FILE}), arrives=short))
        self.assertIn("is 10 bytes on NERSC, expected 5000", told)
        rc, ran, _shell = self.run_direct(_Rclone({"/n/data/f.bin": self.FILE}),
                                          dry_run=True)
        self.assertEqual(rc, 0, "a dry run is not checked")
        self.assertIn("--dry-run", ran[0])

    def test_a_home_it_cannot_find_stops_it_before_anything_moves(self):
        world = _Rclone({"/n/data/f.bin": self.FILE})
        shell = _ExecutorShell(world, home_lost=1)
        ran = []
        with _patched(crossxfer.CrossTransfer, "_remote_rclone",
                      lambda *_a: ran.append("rclone") or "rclone"), \
                self.assertRaises(crossxfer.DirectUnavailable) as stopped:
            self.run_direct(world, shell=shell)
        self.assertIn("could not find the home directory on FASRC",
                      str(stopped.exception))
        self.assertEqual(ran, [])
        self.assertEqual(world.asked, [])

    def test_a_side_that_never_answers_is_not_a_missing_source(self):
        """Unanswerable probes warn and carry on; only "not there" stops it."""
        world = _Rclone(flaky=99)
        with contextlib.redirect_stderr(io.StringIO()) as said:
            rc, ran, _shell = self.run_direct(world)
        self.assertEqual(rc, 0)
        self.assertEqual(len(ran), 1)
        self.assertIn("could not tell whether /n/data/f.bin is a directory",
                      said.getvalue())
        self.assertIn("could not confirm", said.getvalue())


class TestRelayTransfer(unittest.TestCase):
    """rclone here, one remote per cluster, and the same checks as any transfer."""

    def setUp(self):
        temp_state(self)

    def run_relay(self, world, arrives=None, **kw):
        settings = _settings(**TRANSFER_SETTINGS)
        cross = _cross(_endpoint("fasrc", "/n/data/f.bin", settings),
                       _endpoint("nersc", "~/in/", settings), **kw)
        cross._relay_link = lambda endpoint, opened: _steady_link(endpoint.name)
        ran, configs = [], []

        def rclone_here(argv, env=None):
            ran.append(argv)
            configs.append(Path(argv[argv.index("--config") + 1]).read_text())
            world.stats.update(arrives or {})
            return _proc(0)

        fake_transfers = lambda _logins: types.SimpleNamespace(  # noqa: E731
            rclone_bin=lambda: "rclone")
        with _patched(crossxfer, "Transfers", fake_transfers), \
                _patched(plat, "run", lambda argv, timeout=None, **_kw:
                         world.answer(argv)), \
                _patched(riding, "run_riding", _rclone_runs(rclone_here)), \
                _patched(time, "sleep", lambda _s: None):
            rc = cross._run_relay()
        return rc, ran, configs

    FILE = {"IsDir": False, "Size": 5000}

    def test_each_side_is_asked_through_its_own_remote_and_asked_again(self):
        world = _Rclone({"src:/n/data/f.bin": self.FILE}, flaky=1)
        arrives = {"dst:in/f.bin": self.FILE}
        rc, ran, configs = self.run_relay(world, arrives=arrives)
        self.assertEqual(rc, 0)
        self.assertEqual(world.asked, ["src:/n/data/f.bin", "src:/n/data/f.bin",
                                       "dst:in/f.bin"])
        argv = ran[0]
        self.assertEqual(argv[1], "copy")
        self.assertEqual(argv[-2:], ["src:/n/data/f.bin", "dst:in/"])
        self.assertIn("[src]", configs[0])
        self.assertIn("[dst]", configs[0])
        self.assertFalse(Path(argv[argv.index("--config") + 1]).exists(),
                         "the config is gone afterwards")
        for flag, value in (("--transfers", "5"), ("--checkers", "3"),
                            ("--timeout", "77s"), ("--retries", "3")):
            self.assertEqual(argv[argv.index(flag) + 1], value, flag)
        _rc, ran, _configs = self.run_relay(_Rclone({"src:/n/data/f.bin": self.FILE}),
                                            arrives=arrives, via="work")
        self.assertEqual(ran[0][ran[0].index("--transfers") + 1], "2",
                         "a login's master has fewer channels to spare")
        told = _refusal(lambda: self.run_relay(_Rclone({"src:/n/data/f.bin":
                                                        self.FILE})))
        self.assertIn("in/f.bin is not on NERSC", told)


class TestRelayConfig(unittest.TestCase):
    def setUp(self):
        temp_state(self)

    @staticmethod
    def side(user, host):
        return types.SimpleNamespace(
            backend=types.SimpleNamespace(user=user, host_for=lambda node: host),
            settings=_settings())

    def test_one_sftp_remote_per_cluster_with_its_own_transport(self):
        path = crossxfer._relay_config([
            ("src", self.side("a", "h1"), Path("/n/has space/s1.sock"), None),
            ("dst", self.side("b", "h2"), Path("/n/s2.sock"), None),
        ])
        text = path.read_text()
        for part in ("[src]", "[dst]", "a@h1", "b@h2",
                     # A space in an ssh argument is quoted for rclone.
                     '"ControlPath=/n/has space/s1.sock"'):
            self.assertIn(part, text)
        self.assertEqual(text.count("type = sftp"), 2)
        # A config naming two masters is a credential-adjacent file.
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_a_relay_that_was_killed_leaves_nothing_for_long(self):
        """A relay's own exit removes its config; SIGHUP or SIGTERM skip that,
        so the next relay removes what only a dead process could have left."""
        here = socket.gethostname() or "localhost"
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        config.STATE_ROOT.mkdir(parents=True, exist_ok=True)
        left = {name: config.STATE_ROOT / f"relay-{name}.conf"
                for name in (f"{here}-{dead.pid}", f"{here}-{live.pid}",
                             f"elsewhere.example-{dead.pid}")}
        for path in left.values():
            path.write_text("[src]\n")
        crossxfer._relay_config([("src", self.side("a", "h1"), Path("/n/s"), None)])
        self.assertFalse(left[f"{here}-{dead.pid}"].exists())
        self.assertTrue(left[f"{here}-{live.pid}"].exists(), "still running")
        self.assertTrue(left[f"elsewhere.example-{dead.pid}"].exists(),
                        "another host's process table is not this one")

    @unittest.skipUnless(support.usable_rclone(), "needs rclone 1.64 or newer")
    def test_rclone_reads_the_ssh_command_back_word_for_word(self):
        """Run rclone for real against an ssh that only records its argv."""
        root = Path(tempfile.mkdtemp(prefix="has space ", dir=str(support.SANDBOX)))
        self.addCleanup(shutil.rmtree, str(root), True)
        record = root / "argv"
        (root / "ssh").write_text(
            "#!/bin/sh\n"
            f'for a in "$@"; do printf "[%s]\\n" "$a"; done > "{record}"\nexit 1\n')
        (root / "ssh").chmod(0o755)
        sock = root / "it's a.sock"
        path = crossxfer._relay_config([("src", self.side("a", "h1"), sock, None)])
        env = dict(os.environ, PATH=f"{root}{os.pathsep}{os.environ.get('PATH', '')}")
        subprocess.run([support.usable_rclone(), "--config", str(path), "--retries", "1",
                        "--low-level-retries", "1", "lsjson", "--stat", "src:x"],
                       env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=60)
        got = record.read_text().splitlines()
        self.assertIn(f"[ControlPath={sock}]", got)
        self.assertIn("[a@h1]", got)


class TestQualifiedPaths(unittest.TestCase):
    """A cluster-qualified path means the same thing wherever it appears."""

    def test_a_qualified_side_becomes_the_remote_side(self):
        from clustertool.commands.transfers import _qualified_sides as qualify

        self.assertEqual(qualify("nersc:~/x", "./here"), ("remote:~/x", "./here", "nersc"))
        self.assertEqual(qualify("./here", "fasrc:~/x"), ("./here", "remote:~/x", "fasrc"))
        self.assertEqual(qualify("./a", "remote:b"), ("./a", "remote:b", None))


class TestDirectFallbacks(unittest.TestCase):
    """What happens when the direct route cannot be set up."""

    def setUp(self):
        self.peer = types.SimpleNamespace(
            name="nersc",
            backend=types.SimpleNamespace(
                label="NERSC", user="u",
                inbound_transfer_hosts=lambda: ["dtn01.x", "dtn02.x", "dtn03.x"],
                inbound_transfer_host=lambda: "dtn01.x"),
            settings=_settings(PEER_CONNECT_TIMEOUT=10),
        )
        self.stub = types.SimpleNamespace(peer_node=None, quiet=True)
        self.stub._peer_ssh = lambda peer, host=None: crossxfer.CrossTransfer._peer_ssh(
            self.stub, peer, host)
        self.stub._peer_candidates = lambda peer: (
            crossxfer.CrossTransfer._peer_candidates(self.stub, peer))
        self.stub._port_check = crossxfer.CrossTransfer._port_check
        self.waits = []

    def _runner(self, reachable=(), authenticates=()):
        """A fake executor shell: which hosts answer, and which accept us."""

        def run(command, timeout=600):
            if "/dev/tcp/" in command:
                self.waits.append((shlex.split(command)[:2], timeout))
                answered = shlex.split(command)[-1] in reachable
                return _proc(0 if answered else 1)
            for host in authenticates:
                if f"@{host}" in command:
                    return _proc(0)
            return _proc(255, "", "Permission denied")

        return run

    def _pick(self, run):
        return crossxfer.CrossTransfer._pick_peer_host(self.stub, run, self.peer)

    def test_the_first_host_that_answers_and_accepts_wins(self):
        # A dead transfer node costs a probe, not the transfer.
        for reachable, want in ((("dtn02.x", "dtn03.x"), "dtn02.x"),
                                (("dtn01.x", "dtn02.x"), "dtn01.x")):
            self.assertEqual(self._pick(self._runner(reachable, reachable)),
                             (want, None))
        self.stub.peer_node = "dtn04.x"
        self.assertEqual(self.stub._peer_candidates(self.peer), ["dtn04.x"],
                         "an explicit peer node is used alone")

    def test_nothing_answering_is_recoverable_says_firewall_and_is_bounded(self):
        host, problem = self._pick(self._runner())
        self.assertIsNone(host)
        self.assertIsInstance(problem, crossxfer.DirectUnavailable)
        self.assertIn("firewall", " ".join(problem.hints))
        # A host behind a filtering firewall never refuses; it never answers,
        # so each connect has its own deadline.
        self.assertEqual(self.waits, [(["timeout", "10"], 40)] * 3)
        # Reachable but rejecting us: a different symptom needing a different fix.
        host, problem = self._pick(self._runner(reachable=("dtn01.x",)))
        self.assertIsNone(host)
        self.assertIn("refused", str(problem))

    def test_the_host_reaches_bash_as_one_argument(self):
        """The port check runs in the executor's shell, so a host name must stay
        data: run it here with a `timeout` that only prints its arguments."""
        bindir = Path(tempfile.mkdtemp(dir=str(support.SANDBOX)))
        self.addCleanup(shutil.rmtree, str(bindir), True)
        (bindir / "timeout").write_text(
            '#!/bin/sh\nfor a in "$@"; do printf "[%s]\\n" "$a"; done\n')
        (bindir / "timeout").chmod(0o755)
        host = "dtn01.x; touch pwned $(touch pwned2)"
        got = subprocess.run(
            ["sh", "-c", crossxfer.CrossTransfer._port_check(host, 10)], cwd=str(bindir),
            env=dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}"),
            stdout=subprocess.PIPE, universal_newlines=True).stdout
        self.assertEqual(got.splitlines(), ["[10]", "[bash]", "[-c]",
                                            "[exec 3<>/dev/tcp/$1/22]", "[_]",
                                            f"[{host}]"])
        self.assertEqual(sorted(p.name for p in bindir.iterdir()), ["timeout"])


class TestEngineFallback(unittest.TestCase):
    """auto falls back to relay; an explicitly chosen engine does not."""

    def test_only_auto_falls_back_to_relay_and_either_way_says_why(self):
        # Relay can be orders of magnitude slower; substituting it unasked is a
        # worse surprise than failing.
        for engine, ran, said in (("auto", ["relay"], "cross this machine"),
                                  ("direct", [], "--engine relay")):
            with self.subTest(engine=engine):
                stub = types.SimpleNamespace(engine=engine, quiet=True, ran=[])
                stub.plan = lambda: ("direct", object(), object())
                stub.describe_plan = lambda: "direct"
                stub._run_direct = lambda executor, peer: (_ for _ in ()).throw(
                    crossxfer.DirectUnavailable("no route", "a hint"))
                stub._run_relay = lambda stub=stub: stub.ran.append("relay") or 7
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    try:
                        result = crossxfer.CrossTransfer.run(stub)
                    except SystemExit:
                        result = None
                self.assertEqual(stub.ran, ran)
                self.assertEqual(result, 7 if ran else None)
                for part in ("no route", said) + (("a hint",) if ran else ()):
                    self.assertIn(part, err.getvalue())


class TestReusedForwardingMaster(unittest.TestCase):
    """A forwarding master that was already up is judged by its agent.

    Its agent belongs to whichever run opened it. Whether that agent answers
    is asked on the executor; the peer is not the question, since one peer
    host being down would look the same. And a connection other runs lease
    is never closed under them.
    """

    def forwarding(self, reused, agent_rc, others=()):
        """Run _open_forwarding; (result or exception, what the xfer saw)."""
        seen = types.SimpleNamespace(opened=0, closed=[], dropped=[], asked=[])

        def open_connection(node=None, quiet=False, forward_agent=False,
                            agent_env=None):
            seen.opened += 1
            return "pool-fwd"

        def close_connection(tag, force=False):
            seen.closed.append((tag, force))
            return not others

        xfer = types.SimpleNamespace(
            is_active=lambda tag: reused, open_connection=open_connection,
            close_connection=close_connection, lease_drop=seen.dropped.append)
        executor = types.SimpleNamespace(
            name="fasrc", backend=types.SimpleNamespace(short=lambda n: n),
            settings=Settings("fasrc"),
            state=types.SimpleNamespace(xfer_socket=lambda tag: Path(f"/x/{tag}.sock")))
        cross = _cross(executor, None)

        def on_executor(*_a):
            def run(command, timeout=600):
                seen.asked.append(command)
                return _proc(agent_rc, "", "error fetching identities")
            return run

        cross._on_executor = on_executor
        agent = types.SimpleNamespace(env=lambda: {})
        try:
            got = cross._open_forwarding(xfer, executor, agent)
        except crossxfer.DirectUnavailable as exc:
            got = exc
        return got, seen

    def test_a_master_is_kept_unless_its_agent_is_certainly_gone(self):
        # A fresh master carries this run's agent and is not asked. ssh's 255
        # or a timeout says nothing about the agent, and replacing a master on
        # no evidence would cost an authentication.
        for reused, rc, asked in ((False, 1, []), (True, 0, ["ssh-add -l"]),
                                  (True, 255, ["ssh-add -l"]), (True, 124, ["ssh-add -l"]),
                                  (True, 127, ["ssh-add -l"])):
            with self.subTest(reused=reused, rc=rc):
                got, seen = self.forwarding(reused=reused, agent_rc=rc)
                self.assertEqual(got, ("pool-fwd", Path("/x/pool-fwd.sock")))
                self.assertEqual(seen.asked, asked, "the agent is what is asked")
                self.assertEqual((seen.opened, seen.closed), (1, []))

    def test_a_master_whose_agent_is_gone_is_replaced_when_nobody_rides_it(self):
        for rc in (1, 2):
            with self.subTest(rc=rc):
                got, seen = self.forwarding(reused=True, agent_rc=rc)
                self.assertEqual(got[0], "pool-fwd")
                self.assertEqual(seen.closed, [("pool-fwd", False)],
                                 "closed as any run closes it, never forced")
                self.assertEqual(seen.opened, 2)
        got, seen = self.forwarding(reused=True, agent_rc=1, others=["pid 42"])
        self.assertIsInstance(got, crossxfer.DirectUnavailable)
        self.assertIn("another run still uses the connection", str(got))
        self.assertIn("transfer --close pool-fwd", " ".join(got.hints))
        self.assertEqual(seen.closed, [("pool-fwd", False)])
        self.assertEqual(seen.opened, 1, "a master another run rides is left to it")
        self.assertEqual(seen.dropped, ["pool-fwd"], "this run's own lease is let go")


class TestConnectionHandover(unittest.TestCase):
    """A connection direct paid for must not be stranded or paid for twice."""

    def test_only_a_fallback_to_come_inherits_it(self):
        # With no fallback coming, a connection held open would be stranded:
        # nothing is left to close it, and on FASRC opening it cost a TOTP window.
        executor = types.SimpleNamespace(
            name="fasrc", logins=object(), backend=types.SimpleNamespace(label="FASRC"))
        peer = types.SimpleNamespace(backend=types.SimpleNamespace(label="NERSC"))
        agent = types.SimpleNamespace(start=lambda: None, close=lambda: None,
                                      env=lambda: {})
        for engine, inherited, closed in (("auto", ("fasrc", "pool-fwd"), []),
                                          ("direct", None, ["pool-fwd"])):
            with self.subTest(engine=engine):
                seen = types.SimpleNamespace(closed=[], dropped=[])
                xfer = types.SimpleNamespace(
                    lease_drop=seen.dropped.append,
                    close_connection=lambda tag, force=False: seen.closed.append(tag))
                cross = _cross(executor, peer, engine=engine, keep=False)
                cross._open_forwarding = lambda *_a: (
                    "pool-fwd", Path("/nonexistent/pool-fwd.sock"))
                cross._direct_body = lambda *_a: (_ for _ in ()).throw(
                    crossxfer.DirectUnavailable("nope"))
                with _patched(crossxfer.agentscope, "borrow", lambda _b: agent), \
                        _patched(crossxfer, "Transfers", lambda _logins: xfer), \
                        self.assertRaises(crossxfer.DirectUnavailable):
                    cross._run_direct(executor, peer)
                self.assertEqual(cross._inherited, inherited)
                self.assertEqual(seen.closed, closed, "relay closes what it inherits")
                self.assertEqual(seen.dropped, ["pool-fwd"])


class TestTheExecutorsRcloneIsTiedToThisMachine(unittest.TestCase):
    """crossxfer.TIED, run here by bash as the executor's shell would run it."""

    def tie(self, script, silence, stdin=subprocess.PIPE, grace=5):
        return subprocess.Popen(["sh", "-c", crossxfer.tied(
            [sys.executable, "-c", script], silence, grace)], stdin=stdin)

    def test_rclones_own_status_is_the_answer_a_failure_of_its_own_included(self):
        for script, status in (("import sys; sys.exit(3)", 3),
                               ("import sys, time; time.sleep(0.2); sys.exit(4)", 4)):
            child = self.tie(script, 30)
            self.addCleanup(child.stdin.close)
            self.assertEqual(child.wait(timeout=20), status)

    def test_it_stops_when_this_machine_goes_and_its_name_goes_with_it(self):
        name = f"cluster-transfer-test{os.getpid()}"
        child = subprocess.Popen(["sh", "-c", crossxfer.tied(
            [sys.executable, "-c", "import time; time.sleep(30)"], 30, 5, name)],
            stdin=subprocess.PIPE)
        asked = lambda: subprocess.run(  # noqa: E731
            ["sh", "-c", crossxfer.still_there(name)]).returncode
        self.assertEqual(asked(), 0, "its processes go by its name")
        started = time.monotonic()
        child.stdin.close()
        self.assertNotEqual(child.wait(timeout=20), 0)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(asked(), 1, "and none is left once it has stopped")

    def test_a_silent_machine_stops_it_even_one_that_ignores_the_stop(self):
        script = ("import signal, time; signal.signal(signal.SIGTERM, "
                  "signal.SIG_IGN); time.sleep(60)")
        child = self.tie(script, 1, grace=1)
        self.addCleanup(child.stdin.close)
        started = time.monotonic()
        self.assertEqual(child.wait(timeout=30), crossxfer.SILENCED,
                         "its own status, not the 137 of the SIGKILL")
        self.assertLess(time.monotonic() - started, 15)

    def test_a_heartbeat_keeps_it_going(self):
        child = self.tie("import time; time.sleep(1.6)", 1)
        deadline = time.monotonic() + 20
        while child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.25)
            try:
                child.stdin.write(b"\n")
                child.stdin.flush()
            except BrokenPipeError:
                break
        with contextlib.suppress(BrokenPipeError):
            child.stdin.close()
        self.assertEqual(child.wait(timeout=20), 0)


#: Relays stdin to the command after it, holding back what arrives from
#: $1 to $1 + $2 seconds in and delivering it after: a channel that stalls
#: while its connection stays up.
STALL_RELAY = r"""
import os, subprocess, sys, threading, time
start, length = float(sys.argv[1]), float(sys.argv[2])
child = subprocess.Popen(sys.argv[3:], stdin=subprocess.PIPE)
t0, held = time.monotonic(), []
def pump():
    while True:
        data = os.read(0, 4096)
        if not data:
            break
        if start <= time.monotonic() - t0 < start + length:
            held.append(data)
            continue
        try:
            for chunk in held + [data]:
                child.stdin.write(chunk)
            child.stdin.flush()
            held.clear()
        except BrokenPipeError:
            break
threading.Thread(target=pump, daemon=True).start()
sys.exit(child.wait())
"""


class TestAStallTheConnectionSurvivesIsRiddenOut(unittest.TestCase):
    """The channel carries nothing for longer than the far side waits for a
    heartbeat, then carries again; the master was never lost. The far rclone
    was stopped by TIED, so the transfer resumes rather than ending."""

    def test_the_transfer_is_resumed_at_once(self):
        runs = []

        def command():
            runs.append(time.monotonic())
            far = [sys.executable, "-c",
                   f"import time; time.sleep({8 if len(runs) == 1 else 0})"]
            stall = ["0.5", "3"] if len(runs) == 1 else ["0", "0"]
            return [sys.executable, "-c", STALL_RELAY, *stall,
                    "bash", "-c", crossxfer.tied(far, 2, 1)]

        link = _steady_link("s")
        link.restore = lambda: self.fail("the connection was never lost")
        link.logins = types.SimpleNamespace(last_failure="")
        err = io.StringIO()
        started = time.monotonic()
        with _patched(riding, "LINK_POLL", 0.5), contextlib.redirect_stderr(err):
            ride = riding.Ride([link], Settings("fasrc"), heartbeat=True,
                               settle=600, silenced=crossxfer.SILENCED)
            rc = ride.run(command)
        self.assertEqual(rc, 0, err.getvalue())
        self.assertEqual(len(runs), 2)
        self.assertLess(time.monotonic() - started, 60,
                        "no settle: the far side had stopped already")
        self.assertIn("heard nothing from this machine", err.getvalue())
        self.assertNotIn("waiting", err.getvalue())


class TestTheEnginesRideTheirConnections(unittest.TestCase):
    """Direct and relay hand their connections to riding.Ride."""

    def setUp(self):
        temp_state(self)
        self.rides = []
        outer = self

        class Ride:
            def __init__(self, links, settings, env=None, heartbeat=False,
                         settle=0, silenced=None, quiet=False, settled=None):
                outer.rides.append(dict(links=links, heartbeat=heartbeat,
                                        settle=settle, silenced=silenced,
                                        settled=settled))

            def run(self, command):
                outer.rides[-1].update(argv=command(), command=command)
                return 0

        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(_patched(crossxfer, "Ride", Ride))

    def test_direct_sends_a_heartbeat_and_waits_out_a_lost_rclone(self):
        settings = _settings(**TRANSFER_SETTINGS)
        executor = _endpoint("fasrc", "/n/data/f.bin", settings)
        peer = _endpoint("nersc", "~/in/", settings)
        cross = _cross(executor, peer, dry_run=True)
        shell = _ExecutorShell(_Rclone({"/n/data/f.bin": {"IsDir": False, "Size": 5}}))
        cross._on_executor = lambda *_a: shell
        cross._pick_peer_host = lambda run, _peer: ("dtn01.nersc.example", None)
        agent = types.SimpleNamespace(env=lambda: {}, loaded=lambda: "key")
        link = _steady_link("s")
        with _patched(time, "sleep", lambda _s: None):
            cross._direct_body(executor, peer, link, agent)
        ride, = self.rides
        self.assertEqual(ride["links"], [link])
        self.assertTrue(ride["heartbeat"])
        # The master's own tolerance of silence (30 x 30 here) and a look
        # more, then the SIGKILL's grace (30), then the last beat's look.
        silence = 30 * 30 + crossxfer.LINK_POLL
        self.assertEqual(ride["settle"], silence + 30 + crossxfer.LINK_POLL)
        self.assertEqual(ride["silenced"], crossxfer.SILENCED)
        words = shlex.split(ride["argv"][-1])
        self.assertEqual(words[:3] + words[4:6],
                         ["bash", "-c", crossxfer.TIED, str(silence), "30"])
        name = words[3]
        self.assertRegex(name, r"^cluster-transfer-[0-9a-f]{8}$")
        # The wait ends early on the word of the node the run went to alone.
        settled = ride["settled"]
        self.assertFalse(settled(), "where it ran is not known")
        link.node = "n1"
        self.assertFalse(settled(), "a master on another node cannot say")
        ride["command"]()
        self.assertTrue(settled())
        self.assertEqual(shell.commands[-1], crossxfer.still_there(name))
        shell.far_gone = False
        self.assertFalse(settled())

    def test_relay_rides_both_clusters(self):
        settings = _settings(**TRANSFER_SETTINGS)
        cross = _cross(_endpoint("fasrc", "/n/data/f.bin", settings),
                       _endpoint("nersc", "~/in/", settings), dry_run=True)
        cross._relay_link = lambda endpoint, opened: _steady_link(endpoint.name)
        world = _Rclone({"src:/n/data/f.bin": {"IsDir": False, "Size": 5}})
        fake_transfers = lambda _logins: types.SimpleNamespace(  # noqa: E731
            rclone_bin=lambda: "rclone")
        with _patched(crossxfer, "Transfers", fake_transfers), \
                _patched(plat, "run", lambda argv, timeout=None, **_kw:
                         world.answer(argv)), \
                _patched(time, "sleep", lambda _s: None):
            cross._run_relay()
        ride, = self.rides
        self.assertEqual([link.name for link in ride["links"]], ["fasrc", "nersc"])
        self.assertFalse(ride["heartbeat"])


class TestTheRelaysLinkKeepsItsTag(unittest.TestCase):
    """A relay's link reopens a master with the kind its tag names.

    A "-fwd" master is one a direct run reusing it counts on to forward, so
    it forwards when reopened; the user's own agent never goes with it.
    """

    def test_a_forwarding_tag_reopens_forwarding_without_the_users_agent(self):
        endpoint = types.SimpleNamespace(
            backend=types.SimpleNamespace(label="FASRC"), logins=object(),
            state=types.SimpleNamespace(
                xfer_socket=lambda t: Path(f"/nonexistent/{t}.sock")))
        cross = _cross(endpoint, None)
        env = {"SSH_AUTH_SOCK": "/agent.sock", "SSH_AGENT_PID": "7", "HOME": "/h"}
        for tag, forwards in (("pool-fwd", True), ("pool", False)):
            calls = []
            xfer = types.SimpleNamespace(reopen=lambda *a, **kw: calls.append((a, kw)))
            with mock.patch.dict(os.environ, env):
                link = cross._transfer_link(xfer, endpoint, tag)
                link.master = 4242
                link.restore()
            (args, kw), = calls
            self.assertEqual(args, (tag, 4242))
            self.assertEqual(kw["forward_agent"], forwards)
            if not forwards:
                self.assertIsNone(kw["agent_env"])
                continue
            self.assertNotIn("SSH_AUTH_SOCK", kw["agent_env"])
            self.assertNotIn("SSH_AGENT_PID", kw["agent_env"])
            self.assertEqual(kw["agent_env"]["HOME"], "/h")


class TestTheExecutorsCommandsSayHowLongTheyTake(unittest.TestCase):
    """What runs on the executor ranges from a version check to the whole
    transfer, so no timeout is assumed: every caller gives its own."""

    def test_a_command_there_is_given_its_timeout(self):
        executor = types.SimpleNamespace(
            backend=types.SimpleNamespace(target=lambda node: "user@node"))
        agent = types.SimpleNamespace(env=lambda: {})
        run = _cross(executor, None)._on_executor(
            executor, Path("/nonexistent/x.sock"), None, agent)
        with self.assertRaises(TypeError):
            run("true")
        seen = []
        with _patched(crossxfer.plat, "run",
                      lambda argv, timeout, **_kw: seen.append(timeout) or _proc()):
            run("true", timeout=None)
            run("true", timeout=7)
        self.assertEqual(seen, [None, 7])


if __name__ == "__main__":
    unittest.main(verbosity=2)
