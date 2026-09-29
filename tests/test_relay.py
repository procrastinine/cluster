#!/usr/bin/env python3
"""The laptop-side relay client, bin/cluster-relay.

Run: python3 -m unittest tests.test_relay
(or the whole suite: python3 -m unittest discover -s tests -p 'test_*.py')
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import shlex
import shutil
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

from support import REPO_ROOT, FakeClock  # noqa: E402

RELAY_SCRIPT = REPO_ROOT / "bin" / "cluster-relay"


def load_relay(path=RELAY_SCRIPT, name="cluster_relay"):
    """bin/cluster-relay as a module; it has no .py name to import it by."""
    import importlib.machinery
    import importlib.util

    loader = importlib.machinery.SourceFileLoader(name, str(path))
    module = importlib.util.module_from_spec(
        importlib.util.spec_from_loader(name, loader))
    loader.exec_module(module)
    return module


def quietly(call, *args, **kw):
    """Run *call* with stderr captured; (result or SystemExit, stderr text)."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            got = call(*args, **kw)
        except SystemExit as exc:
            got = exc
    return got, err.getvalue()


def gnu(tool):
    """Whether *tool* here is the GNU one, as it is on the clusters."""
    try:
        got = subprocess.run([tool, "--version"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True)
    except OSError:
        return False
    return "GNU" in (got.stdout or "")


class RelayTest(unittest.TestCase):
    """One loaded copy of the script, with whatever a test patches put back."""

    RELAY_ENV = ("CLUSTER_RELAY_HOST", "CLUSTER_RELAY_BIN", "CLUSTER_RELAY_RETRIES",
                 "CLUSTER_RELAY_RETRY_DELAY", "CLUSTER_RELAY_RETRY_DELAY_MAX",
                 "CLUSTER_RELAY_RETRY_HALF_LIFE", "RSYNC_RSH")

    @classmethod
    def setUpClass(cls):
        cls.relay = load_relay()

    def setUp(self):
        saved = {key: os.environ.pop(key, None) for key in self.RELAY_ENV}

        def put_env_back():
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.addCleanup(put_env_back)
        box = tempfile.TemporaryDirectory()
        self.addCleanup(box.cleanup)
        self.tmp = Path(box.name)

    def patch(self, name, value):
        original = getattr(self.relay, name)
        setattr(self.relay, name, value)
        self.addCleanup(setattr, self.relay, name, original)


class TestRelayClient(RelayTest):
    """The laptop-side client: bin/cluster-relay.

    It is a separate script because the machine it runs on has none of this
    tool's state — no credentials, no control masters, no idea what `main:`
    means. What it does have to know is which of its arguments are paths, and
    that knowledge is duplicated from the real transfer command, so the copy is
    checked here rather than left to drift.
    """

    def test_it_knows_every_flag_that_swallows_its_value(self):
        from clustertool.commands.transfers import RCLONE_VALUE_FLAGS

        missing = RCLONE_VALUE_FLAGS - self.relay.VALUE_FLAGS
        self.assertEqual(missing, set(),
                         "bin/cluster-relay would read these flags' values as "
                         f"paths: {sorted(missing)}")
        args = ["--exclude", "*.log", "/here/data", "main:/there/"]
        self.assertEqual(self.relay.positional_indices(args), [2, 3])

    def test_the_local_side_is_the_one_that_names_no_cluster(self):
        for path, side in (("main:/n/scratch", "cluster"), ("fasrc:~/x", "cluster"),
                           ("remote:x", "cluster"), ("/home/me/x", "here"),
                           ("./out", "here")):
            self.assertEqual(self.relay.classify(path)[0], side, path)
        self.assertEqual(self.relay.classify("local:/home/me/x"), ("here", "/home/me/x"))
        # A relay prefix means a path the relay already sees.
        self.assertEqual(self.relay.classify("relay:/tmp/x"), ("relay", "/tmp/x"))
        self.assertEqual(self.relay.split_cluster("main:/n/x"), ("main", "/n/x"))
        self.assertEqual(self.relay.split_cluster("remote:/n/x"), ("", "/n/x"))
        # A trailing slash is meaning, not noise: it must survive the split.
        self.assertEqual(self.relay.split_cluster("main:/n/x/")[1], "/n/x/")

    def test_staging_is_not_on_the_relay_hosts_tmpfs(self):
        # /tmp is often a tmpfs, and a staged dataset there would sit in RAM.
        self.assertNotIn("/tmp", self.relay.STAGE_ROOT)

    # --- streaming through the relay, rather than staging on it -------------
    def test_a_manifest_is_read_name_for_name(self):
        for text, want in (
                # find prints "./.hidden"; only the literal "./" prefix goes.
                ("12 ./.hidden\0007 ./sub/a.txt\000", {".hidden": 12, "sub/a.txt": 7}),
                ("40 .", {".": 40}),
                # A newline in a name does not invent a file.
                ("5 ./odd\nname\0006 ./b\000", {"odd\nname": 5, "b": 6})):
            self.assertEqual(self.relay.manifest_of(text), want)
        inner = {"a.txt": 3, "sub/b": 4}
        self.assertEqual(self.relay.prefixed(inner, "tree"),
                         {"tree/a.txt": 3, "tree/sub/b": 4})
        self.assertEqual(self.relay.prefixed({".": 9}, "one.txt"), {"one.txt": 9})
        self.assertEqual(self.relay.prefixed(inner, ""), inner)

    def test_a_delivery_is_what_was_sent_arriving_whole(self):
        for sent, arrived in (({"a": 10}, {"a": 4}), ({"a": 10}, {})):
            self.assertIsInstance(
                quietly(self.relay.compare, sent, arrived, "on the cluster")[0],
                SystemExit)
        # Downloads land next to earlier ones; only what was sent is checked.
        self.assertTrue(self.relay.compare({"a": 4}, {"a": 4, "older": 99}, "here"))

    def test_a_stream_is_tried_again_before_it_is_called_a_failure(self):
        # Every --serve mode overwrites rather than appends, so a dropped
        # channel is worth another go; one that keeps failing still fails.
        self.patch("time", FakeClock())
        for codes, want in (([255, 0], 0), ([1] * 50, SystemExit)):
            calls = []

            def lossy(*a, tally=None, codes=codes, **_k):
                calls.append(a)
                tally["far"] = codes.pop(0)
                return tally["far"]

            self.patch("stream", lossy)
            got, _err = quietly(self.relay.streamed, "to /n/x", "host", ["recv"])
            if want is SystemExit:
                self.assertIsInstance(got, SystemExit)
            else:
                self.assertEqual((got, len(calls)), (0, 2))

    def test_a_download_of_the_root_or_the_home_directory_is_what_it_says(self):
        sent = []
        self.patch("serve_value", lambda _host, args:
                   "dir" if args[0] == "probe" else "1 ./a\000")
        self.patch("streamed", lambda _what, _host, args, **_kw: sent.append(args))
        self.patch("compare", lambda *_a: True)
        into = str(self.tmp / "out") + "/"
        for source, sends in (("/", "/"), ("~/", "~"), ("", None), ("~", None)):
            with self.subTest(source=source):
                got, err = quietly(self.relay.stream_down, "host", "main", [source],
                                   into, False, False)
                if sends is None:
                    self.assertIsInstance(got, SystemExit)
                    self.assertIn("write it as ~/", err)
                else:
                    self.assertEqual(got, 0, err)
                    args = sent.pop()
                    self.assertEqual(args[args.index("--from") + 1], sends)

    def test_each_source_arrives_as_its_trailing_slash_says(self):
        for flags, names, want in (
                # `--contents a b dest/` sends the contents of every source.
                (["--contents"], ["tree", "tree2"], {"a.txt", "c.txt"}),
                ([], ["tree/", "tree2"], {"a.txt", "tree2/c.txt"}),
                ([], ["tree", "tree2"], {"tree/a.txt", "tree2/c.txt"})):
            with self.subTest(flags=flags, names=names):
                self.assertEqual(set(self.run_up(flags, names)), want)

    def test_an_upload_is_one_tar_of_this_machine(self):
        seen = {}
        self.run_up([], ["-dash"], seen=seen)
        feed = seen["feed"]
        self.assertEqual(feed[:len(self.relay.tar_argv("-cf"))],
                         self.relay.tar_argv("-cf"))
        # A name that starts with a dash is still a name to tar.
        self.assertEqual(feed[-1], "./-dash")

    def run_up(self, flags, names, seen=None):
        """stream_up with the network replaced, returning what it says it sent."""
        root = Path(tempfile.mkdtemp(dir=str(self.tmp)))
        for name, member, text in (("tree", "a.txt", "aaa"), ("tree2", "c.txt", "cc"),
                                   ("-dash", "d.txt", "d")):
            (root / name).mkdir()
            (root / name / member).write_text(text)
        sources = [str(root / n.rstrip("/")) + ("/" if n.endswith("/") else "")
                   for n in names]
        seen = {} if seen is None else seen
        original = self.relay.local_manifest

        def watched(target, prefix=""):
            got = original(target, prefix)
            seen.setdefault("want", {}).update(got)
            return got

        self.patch("stream", lambda _host, args, feed=None, drain=None, **_kw:
                   seen.update(feed=feed) or 0)
        # The manifest the cluster would report back: what tar was given, so
        # the delivery check passes when the tar is right.
        self.patch("serve_value", lambda _host, args: "dir" if args[0] == "probe" else
                   "\000".join(f"{size} ./{name}" for name, size in seen["want"].items()))
        self.patch("local_manifest", watched)
        self.relay.stream_up("host", "main", sources, "/n/dest/",
                             "--contents" in flags, False)
        return seen["want"]


class TestRemotePaths(RelayTest):
    """What the cluster's shell is told, for each --serve mode. A path is
    spelled by clustertool.remote_sh.remote_path, whose own tests are in
    test_transfers."""

    def test_every_mode_quotes_every_path(self):
        from clustertool.remote_sh import remote_path

        odd = "~/-a b/it's"
        for mode in ("probe", "manifest", "recv", "read", "write"):
            with self.subTest(mode=mode):
                script = self.relay._remote_script(
                    self.opts(mode, path=odd, into=odd))
                self.assertIn(remote_path(odd), script)
                self.assertNotIn("~", script)
        script = self.relay._remote_script(
            self.opts("send", source="~", name=["-a b"]))
        self.assertEqual(shlex.split(script), ["tar", "-cf", "-", "-C", ".", "./-a b"])
        # The file mode makes its parent.
        script = self.relay._remote_script(self.opts("write", into="~/x y/f"))
        subprocess.run(["sh", "-c", script], input=b"hi", cwd=str(self.tmp), check=True)
        self.assertEqual((self.tmp / "x y" / "f").read_bytes(), b"hi")

    @staticmethod
    def opts(mode, path="", into="", source="", name=()):
        return argparse.Namespace(mode=mode, path=path, into=into, source=source,
                                  name=list(name), keep=False)


@unittest.skipUnless(gnu("tar") and gnu("find"),
                     "the cluster side runs GNU tar and find")
class TestRemoteScriptsRun(RelayTest):
    """The --serve scripts, run by a local sh in a home of their own.

    `cluster transfer ~/data main:~/results/` from a laptop must land in
    $HOME/results on the cluster, whatever the names hold.
    """

    ODD = "~/-odd dir/it's here.txt"

    def setUp(self):
        super().setUp()
        self.home = self.tmp / "home"
        self.home.mkdir()

    def run_mode(self, mode, stdin=b"", **fields):
        script = self.relay._remote_script(TestRemotePaths.opts(mode, **fields))
        got = subprocess.run(["sh", "-c", script], input=stdin, cwd=str(self.home),
                             env={"HOME": str(self.home), "PATH": os.environ["PATH"]},
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(got.returncode, 0, got.stderr.decode(errors="replace"))
        return got.stdout

    def test_an_upload_to_a_home_path_lands_in_the_home_directory(self):
        src = self.tmp / "src"
        (src / "sub").mkdir(parents=True)
        (src / "sub" / "a.txt").write_text("aaa")
        tar = subprocess.run(["tar", "-cf", "-", "-C", str(src), "."],
                             stdout=subprocess.PIPE, check=True).stdout
        self.run_mode("recv", stdin=tar, into="~/results/")
        self.assertEqual((self.home / "results" / "sub" / "a.txt").read_text(), "aaa")
        self.assertFalse((self.home / "~").exists())
        manifest = self.relay.manifest_of(
            self.run_mode("manifest", path="~/results").decode())
        self.assertEqual(manifest, {"sub/a.txt": 3})

    def test_a_file_with_an_odd_name_goes_and_comes_back(self):
        self.run_mode("write", stdin=b"hello", into=self.ODD)
        landed = self.home / "-odd dir" / "it's here.txt"
        self.assertEqual(landed.read_bytes(), b"hello")
        self.assertEqual(self.run_mode("probe", path=self.ODD).decode().split(),
                         ["file", "5"])
        self.assertEqual(self.run_mode("probe", path="~/-odd dir").strip(), b"dir")
        self.assertEqual(self.run_mode("probe", path="~/nothing").strip(), b"absent")
        self.assertEqual(self.run_mode("read", path=self.ODD), b"hello")
        self.assertEqual(self.relay.manifest_of(
            self.run_mode("manifest", path=self.ODD).decode()), {".": 5})

    def test_a_download_names_its_members_without_reading_them_as_options(self):
        (self.home / "-odd dir").mkdir()
        (self.home / "-odd dir" / "x").write_text("x")
        tar = self.run_mode("send", source="~", name=["-odd dir"])
        listing = subprocess.run(["tar", "-tf", "-"], input=tar,
                                 stdout=subprocess.PIPE, check=True).stdout.decode()
        self.assertIn("./-odd dir/x", listing.splitlines())


class TestRelaySettings(RelayTest):
    """Where the relay host is, and what to run there: [relay] in settings.ini."""

    def use_settings(self, text=None):
        settings = self.tmp / "settings.ini"
        if text is not None:
            settings.write_text(text)
        self.patch("SETTINGS_FILE", settings)
        self.relay._relay_section.cache_clear()
        self.addCleanup(self.relay._relay_section.cache_clear)
        return settings

    def test_an_empty_or_relative_xdg_directory_counts_as_unset(self):
        fallback = Path("/fallback")
        for value, expected in (("", fallback), ("relative/dir", fallback),
                                ("/abs/dir", Path("/abs/dir")), (None, fallback)):
            with mock.patch.dict(os.environ):
                os.environ.pop("XDG_TEST_DIR_FOR_RELAY", None)
                if value is not None:
                    os.environ["XDG_TEST_DIR_FOR_RELAY"] = value
                self.assertEqual(self.relay._xdg("XDG_TEST_DIR_FOR_RELAY", fallback),
                                 expected, value)

    def test_the_relay_section_of_the_shared_settings_file_and_the_environment(self):
        self.use_settings("[global]\nBACKEND = nersc\n\n"
                          "[relay]\nhost = user@relay.example.org\nBIN = ~/bin/cluster\n")
        self.assertEqual(self.relay.relay_setting("HOST"), "user@relay.example.org")
        self.assertEqual(self.relay.relay_setting("BIN"), "~/bin/cluster")
        self.assertEqual(self.relay.relay_setting("ROOT"), "")
        # The environment overrides one key for one run.
        os.environ["CLUSTER_RELAY_HOST"] = "other@relay2.example.org"
        self.assertEqual(self.relay.relay_host(), "other@relay2.example.org")
        os.environ["CLUSTER_RELAY_HOST"] = "  "
        self.assertEqual(self.relay.relay_host(), "user@relay.example.org")

    def test_help_works_before_a_relay_host_is_set_up(self):
        self.use_settings()
        for argv, code in ((["--help"], 0), (["-h"], 0), (["help"], 0), ([], 2)):
            with self.subTest(argv=argv):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(self.relay.main(argv), code)
                self.assertIn("[relay]", out.getvalue())
                self.assertIn("HOST", out.getvalue())

    def test_a_missing_host_says_how_to_set_it_and_that_command_works(self):
        # Also into a file that has a [relay] section already: appending makes
        # a second one, which is read along with the first.
        for before in (None, "[relay]\nBIN = ~/bin/cluster\n"):
            with self.subTest(before=before):
                settings = self.use_settings(before)
                got, err = quietly(self.relay.main, ["ls"])
                self.assertIsInstance(got, SystemExit)
                self.assertIn("[relay] HOST", err)
                self.assertIn(f">> {settings}", err)
                self.assertIn("CLUSTER_RELAY_HOST", err)
                command = next(line.strip() for line in err.splitlines()
                               if ">>" in line)
                subprocess.run(["sh", "-c", command], check=True)
                self.relay._relay_section.cache_clear()
                self.assertEqual(self.relay.relay_setting("HOST"),
                                 "user@relay.example.org")
                self.assertEqual(self.relay.relay_setting("BIN"),
                                 "~/bin/cluster" if before else "")

    def test_the_package_comes_from_the_checkout_the_script_belongs_to(self):
        # Through a symlink, as `ln -s .../bin/cluster-relay ~/.local/bin/...`
        # installs it, --serve still imports the package next to the real file.
        link = self.tmp / "bin" / "cluster"
        link.parent.mkdir()
        link.symlink_to(RELAY_SCRIPT)
        module = load_relay(link, "cluster_relay_linked")
        self.assertEqual(module.CLUSTER_ROOT, str(RELAY_SCRIPT.parents[1]))

    def test_every_name_the_serve_half_imports_exists(self):
        # --serve runs on the relay host against the package in its checkout;
        # a name that moved would only fail there, mid-transfer.
        import importlib
        import re

        imports = re.findall(r"^\s+from (clustertool[\w.]*) import (\w+)$",
                             RELAY_SCRIPT.read_text(), re.M)
        self.assertTrue(imports)
        for module, name in imports:
            self.assertTrue(hasattr(importlib.import_module(module), name),
                            f"{module}.{name}")


class TestTheServeHalfUnwinds(RelayTest):
    """A killed --serve still gives back its lease and closes its connection."""

    def test_a_terminating_signal_reaches_the_serve_halfs_finally(self):
        import signal

        for sig in (signal.SIGTERM, signal.SIGHUP):
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))
        # An ignored hangup stays ignored.
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        self.patch("serve", lambda _args: 0)
        self.assertEqual(self.relay.main(["--serve", "send"]), 0)
        self.assertEqual(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)
        unwound = []

        def serve(_args):
            try:
                os.kill(os.getpid(), signal.SIGTERM)
                signal.pause()
            finally:
                unwound.append(True)

        self.patch("serve", serve)
        with self.assertRaises(SystemExit) as ended:
            self.relay.main(["--serve", "send"])
        self.assertEqual(ended.exception.code, 128 + signal.SIGTERM)
        self.assertEqual(unwound, [True])


class TestTheRelayHostsCommandLine(RelayTest):
    """relay_line, run by a local sh standing in for the relay host's shell."""

    def setUp(self):
        super().setUp()
        self.home = self.tmp / "home"
        checkout = self.home / "src" / "cluster" / "bin"
        checkout.mkdir(parents=True)
        for name in ("cluster", "cluster-relay"):
            script = checkout / name
            script.write_text(f'#!/bin/sh\nprintf "{name}"\n'
                              'for a in "$@"; do printf "|%s" "$a"; done\n')
            script.chmod(0o755)
        (self.home / ".local" / "bin").mkdir(parents=True)
        os.symlink("../../src/cluster/bin/cluster",
                   self.home / ".local" / "bin" / "cluster")
        self.patch("_relay_section", lambda: None)

    def on_relay(self, line):
        # python3 for bin/cluster, which --relay-serve runs.
        path = os.path.dirname(sys.executable) + ":/usr/bin:/bin"
        got = subprocess.run(line, shell=True, cwd=str(self.home),
                             env={"HOME": str(self.home), "PATH": path},
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return got.returncode, got.stdout, got.stderr

    def test_arguments_reach_the_tool_unchanged_and_a_missing_one_is_named(self):
        args = ["transfer", "a b", "it's", "$HOME", "~/x", "-n"]
        self.assertEqual(self.on_relay(self.relay.relay_line(args)),
                         (0, "cluster|" + "|".join(args), ""))
        # Configured paths may start at the relay user's home.
        os.environ["CLUSTER_RELAY_BIN"] = "~/src/cluster/bin/cluster-relay"
        self.assertEqual(self.on_relay(self.relay.relay_line(["ls"]))[1],
                         "cluster-relay|ls")
        for bin_ in ("no-such-cluster", "~/no/such/cluster"):
            with self.subTest(bin=bin_):
                os.environ["CLUSTER_RELAY_BIN"] = bin_
                rc, _out, err = self.on_relay(self.relay.relay_line(["ls"]))
                self.assertEqual(rc, self.relay.NO_TOOL)
                self.assertIn("[relay] BIN", err)

    def test_the_serve_half_is_the_cluster_relay_of_the_same_checkout(self):
        # BIN is this repository's bin/cluster, copied into a checkout of its
        # own and reached through a symlink, as an install makes it.
        real = self.home / "real" / "bin"
        real.mkdir(parents=True)
        shutil.copy(REPO_ROOT / "bin" / "cluster", real / "cluster")
        (real / "cluster-relay").write_text(
            "import sys\nprint('|'.join(['cluster-relay'] + sys.argv[1:]), end='')\n")
        (self.home / "linked").symlink_to(real / "cluster")
        os.environ["CLUSTER_RELAY_BIN"] = "~/linked"
        line = self.relay.serve_argv("host", ["probe", "--path", "~/a b"])[-1]
        self.assertEqual(self.on_relay(line),
                         (0, "cluster-relay|--serve|probe|--path|~/a b", ""))

    def test_every_ssh_gives_up_on_an_unreachable_host(self):
        argv = self.relay.ssh_argv("user@relay.example.org", "true")
        self.assertIn("ConnectTimeout=25", argv)
        self.assertIn("ServerAliveInterval=15", argv)
        self.assertNotIn("BatchMode=yes", argv, "a person may be asked")
        self.assertIn("BatchMode=yes", self.relay.ssh_argv("h", "true", batch=True))


needs_rsync = unittest.skipUnless(shutil.which("rsync"), "rsync is not installed")


class TestRsync(RelayTest):
    """The staging hop: rsync between this machine and the relay host."""

    @needs_rsync
    def test_remote_ends_go_as_they_are_over_an_ssh_that_gives_up(self):
        argv = self.relay.rsync_argv(["/src/"], ("h", "/stage/"), False)
        self.assertEqual(argv[-2:], ["/src/", "h:/stage/"])
        self.assertNotIn("-s", argv)
        # rsync's ssh gives up on an unreachable host, unless told otherwise.
        self.assertIn("ssh -o ConnectTimeout=25", argv[argv.index("-e") + 1])
        os.environ["RSYNC_RSH"] = "ssh -p 2222"
        self.assertNotIn("-e", self.relay.rsync_argv(["/src"], ("h", "/stage/"), False))

    def test_progress_is_the_best_this_rsync_lists(self):
        for listed, flag in (("--info=FLAGS --progress", "--info=progress2"),
                             ("--progress", "--progress"), ("", "-v")):
            with self.subTest(flag=flag):
                fake = self.tmp / flag.strip("-")
                fake.write_text(f"#!/bin/sh\necho '{listed}'\n")
                fake.chmod(0o755)
                self.assertEqual(self.relay.rsync_progress(str(fake)), flag)


@needs_rsync
class TestStagedFetch(RelayTest):
    """A staged download's last hop, for real: this machine's rsync talking to
    the relay host's through an ssh that runs the command here, in a shell.

    With TEST_OLD_RSYNC set to an older rsync (2.6.9 is the one macOS ships),
    every case runs again with that one at each end in turn.
    """

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        ssh = self.tmp / "ssh"
        ssh.write_text('#!/bin/sh\nshift\nPATH=$RELAY_PATH:$PATH exec sh -c "$*"\n')
        ssh.chmod(0o755)
        os.environ["RSYNC_RSH"] = str(ssh)
        self.patch("ssh_argv", lambda host, command, **_kw: ["sh", "-c", command])

    def rsyncs(self):
        """(this machine's rsync, the relay host's), each as a directory that
        holds it."""
        found = {}
        for name, program in (("new", shutil.which("rsync")),
                              ("old", os.environ.get("TEST_OLD_RSYNC"))):
            if program:
                found[name] = self.tmp / name
                found[name].mkdir()
                (found[name] / "rsync").symlink_to(program)
        yield found["new"], found["new"]
        if "old" in found:
            yield found["old"], found["new"]
            yield found["new"], found["old"]

    def fetch(self, cluster_path, staged, dest):
        """stage_down of *cluster_path*, which the relay host's `cluster
        transfer` stages as *staged*, {path: text}."""
        stage = Path(tempfile.mkdtemp(dir=self.tmp))

        def transfer(_host, _args):
            for name, text in staged.items():
                (stage / name).parent.mkdir(parents=True, exist_ok=True)
                (stage / name).write_text(text)
            return 0

        self.patch("forward", transfer)
        got, err = quietly(self.relay.stage_down, "host", str(stage), str(dest),
                           [cluster_path, "DEST"], 0, [cluster_path],
                           [cluster_path, "DEST"], False)
        self.assertEqual(got, 0, err)

    def test_odd_names_arrive_whichever_rsync_is_at_either_end(self):
        path = os.environ["PATH"]
        for here, there in self.rsyncs():
            with self.subTest(here=here, there=there):
                os.environ["PATH"] = f"{here}:{path}"
                os.environ["RELAY_PATH"] = str(there)
                out = Path(tempfile.mkdtemp(dir=self.tmp))
                out.chmod(0o751)
                (out / "older").write_text("kept")

                self.fetch("main:/n/it's here", {"it's here": "one"}, out / "as.txt")
                self.assertEqual((out / "as.txt").read_text(), "one")
                self.fetch("main:/n/it's here", {"it's here": "one"}, out)
                self.assertEqual((out / "it's here").read_text(), "one")
                self.fetch("main:/n/a b", {"a b/it's here": "deep"}, out / "new")
                self.assertEqual((out / "new" / "a b" / "it's here").read_text(),
                                 "deep")
                self.fetch("main:/n/d/", {"-x": "x", "a b/c": "c"}, out)
                self.assertEqual((out / "-x").read_text(), "x")
                self.assertEqual((out / "a b" / "c").read_text(), "c")

                # What was there stays, and so does the directory's own mode.
                self.assertEqual((out / "older").read_text(), "kept")
                self.assertEqual(out.stat().st_mode & 0o777, 0o751)


class TestStageDown(RelayTest):
    """The staged download's last hop, and the byte check behind it."""

    def fetch(self, cluster_path, staged, fetched, dest_holds=None):
        """stage_down with the relay host faked; *fetched* is what rsync delivers."""
        dest = Path(tempfile.mkdtemp(dir=self.tmp)) / "out"
        if dest_holds is not None:
            dest.mkdir()
            for name, size in dest_holds.items():
                (dest / name).write_bytes(b"x" * size)
        self.patch("forward", lambda host, args: 0)
        self.patch("stage_report",
                   lambda host, stage: (sorted(staged), set(), sum(staged.values())))
        calls = []

        def fake_rsync(sources, target, _progress, names=()):
            calls.append((sources, target, names))
            where = Path(target.rstrip("/"))
            for name in names or [sources[0][1].rsplit("/", 1)[1]]:
                (where / name if names else where).write_bytes(b"y" * fetched[name])
            return 0

        self.patch("rsync", fake_rsync)
        got, err = quietly(self.relay.stage_down, "host", "/stage", str(dest),
                           ["--exclude", "*.log", cluster_path, "DEST"], 3,
                           [cluster_path], ["--exclude", "*.log"], False)
        return got, err, calls

    def test_what_arrived_is_counted_where_it_lands_and_nothing_else(self):
        held = {"older.bin": 10000}
        # path, what rsync delivers, what the destination already held, and
        # whether it passes. What the destination already held does not count
        # either way.
        cases = [("main:/n/r/", {"a.txt": 5}, {"a.txt": 5}, held, True),
                 ("main:/n/r/", {"a.txt": 5}, {"a.txt": 3}, held, False),
                 ("main:/n/f.txt", {"f.txt": 4}, {"f.txt": 4}, held, True),
                 ("main:/n/f.txt", {"f.txt": 4}, {"f.txt": 4}, None, True),
                 ("main:/n/f.txt", {"f.txt": 4}, {"f.txt": 1}, held, False)]
        for path, staged, fetched, holds, ok in cases:
            with self.subTest(path=path, fetched=fetched, holds=holds):
                got, err, calls = self.fetch(path, staged, fetched, dest_holds=holds)
                if not ok:
                    self.assertIsInstance(got, SystemExit)
                    self.assertIn("cut short", err)
                    continue
                self.assertEqual(got, 0, err)
                if path.endswith("f.txt"):
                    # One item is counted where it lands.
                    self.assertEqual(calls[0][2] if holds else calls[0][0],
                                     ["f.txt"] if holds else [("host", "/stage/f.txt")])


class TestDryRun(RelayTest):
    def test_a_dry_run_moves_nothing_even_when_it_would_stage(self):
        def refuse(*_a, **_k):
            raise AssertionError("a dry run touched the relay host")

        for name in ("make_stage", "forward", "stream", "rsync", "serve_value"):
            self.patch(name, refuse)
        for args in (["-n", "--exclude", "*.log", "/here/x", "main:/n/y/"],
                     ["--dry-run", "--sync", "main:/n/y/", "/here/x"],
                     ["-n", "/here/x", "main:/n/y/"]):
            with self.subTest(args=args):
                got, err = quietly(self.relay.relay_transfer, "host", args)
                self.assertEqual(got, 0)
                self.assertIn("would", err)


class TestThisMachinesTar(RelayTest):
    """macOS's tar, bsdtar, would carry Apple metadata as files of its own."""

    def as_mac(self, accepts):
        self.patch("IS_MAC", True)
        self.patch("_tar_accepts", lambda flags: tuple(flags) in accepts)
        self.relay.mac_tar_flags.cache_clear()
        self.addCleanup(self.relay.mac_tar_flags.cache_clear)

    def test_a_mac_tar_is_told_to_leave_out_what_it_accepts_leaving_out(self):
        self.as_mac({("--no-mac-metadata", "--no-xattrs")})
        self.assertEqual(self.relay.tar_argv("-xf", "-C", "/d"),
                         ["tar", "--no-mac-metadata", "--no-xattrs", "-xf", "-",
                          "-C", "/d"])
        self.assertEqual(self.relay.tar_env()["COPYFILE_DISABLE"], "1")
        for accepts, flags in (({("--no-mac-metadata",)}, ["--no-mac-metadata"]),
                               (set(), [])):
            self.as_mac(accepts)
            self.assertEqual(self.relay.tar_argv("-cf"), ["tar", *flags, "-cf", "-"])
        # Elsewhere tar is left as it is.
        self.patch("IS_MAC", False)
        self.relay.mac_tar_flags.cache_clear()
        self.assertEqual(self.relay.tar_argv("-cf"), ["tar", "-cf", "-"])

    def test_the_local_end_of_a_stream_runs_with_that_environment(self):
        sink = self.tmp / "far-end"
        py = sys.executable
        self.patch("serve_argv", lambda host, args: [
            py, "-c", f"import sys; open({str(sink)!r}, 'wb').write(sys.stdin.buffer.read())"])
        feed = [py, "-c", "import os; print(os.environ.get('COPYFILE_DISABLE'))"]
        self.assertEqual(self.relay.stream("host", ["recv"], feed=feed), 0)
        self.assertEqual(sink.read_text().strip(), "1")


class TestStreamEnds(RelayTest):
    def test_a_far_end_that_cannot_start_takes_the_near_end_down(self):
        started = []
        spawn = self.relay._spawn

        def recorded(argv, partner=None, **kw):
            proc = spawn(argv, partner, **kw)
            started.append(proc)
            return proc

        self.patch("_spawn", recorded)
        self.patch("serve_argv", lambda host, args: [str(self.tmp / "no-such-ssh")])
        feed = [sys.executable, "-c", "import time; time.sleep(60)"]
        got, err = quietly(self.relay.stream, "host", ["recv"], feed=feed)
        self.assertIsInstance(got, SystemExit)
        self.assertIn("could not run", err)
        self.assertEqual(len(started), 1)
        self.assertIsNotNone(started[0].returncode, "the near end was left running")


class TestCaseFolding(RelayTest):
    """A download into a file system that folds case, as macOS's does."""

    def folding(self, folds=True):
        self.patch("folds_case", lambda directory: folds)

    def test_names_that_differ_only_in_case_are_found_before_writing(self):
        self.folding()
        into = self.tmp / "out"
        for names, clashes in ((["Makefile", "makefile", "a"], ["Makefile", "makefile"]),
                               (["A/x", "a/y"], ["A", "a"]), (["a/x", "a/y"], [])):
            self.assertEqual(self.relay.case_clashes(into, names), clashes)
        # A name already there counts too.
        into.mkdir()
        (into / "makefile").write_text("")
        self.assertEqual(self.relay.case_clashes(into, ["Makefile"]),
                         ["Makefile", "makefile"])
        self.assertEqual(self.relay.case_clashes(into, ["makefile"]), [])
        self.folding(False)
        self.assertEqual(self.relay.case_clashes(into, ["Makefile"]), [],
                         "a case-sensitive file system holds both")

    def test_the_file_system_itself_is_asked(self):
        probe = self.tmp / "probe"
        probe.mkdir()
        folds = self.relay.folds_case(probe)
        self.assertIsInstance(folds, bool)
        if sys.platform.startswith("linux"):
            self.assertFalse(folds)
        self.assertEqual(list(probe.iterdir()), [], "the probe file was left behind")

    def test_a_clashing_download_stops_with_nothing_written(self):
        self.folding()

        def answers(_host, args):
            if args[0] == "probe":
                return "dir"
            return "1 ./Makefile\0001 ./makefile\000"

        def refuse(*_a, **_k):
            raise AssertionError("streamed a download that could not land")

        self.patch("serve_value", answers)
        self.patch("stream", refuse)
        into = self.tmp / "out"
        got, err = quietly(self.relay.stream_down, "host", "main", ["/n/src/"],
                           str(into) + "/", False, False)
        self.assertIsInstance(got, SystemExit)
        self.assertIn("Makefile, makefile", err)
        self.assertFalse(into.exists())


class TestTryingAgain(RelayTest):
    """A relayed transfer rides out failures by clustertool.backoff's rule:
    each failure counts, progress fades the count, the wait doubles, and too
    many in quick succession, or a refused credential, end it."""

    def setUp(self):
        super().setUp()
        self.patch("SETTINGS_FILE", self.tmp / "settings.ini")
        self.relay._relay_section.cache_clear()
        self.addCleanup(self.relay._relay_section.cache_clear)
        self.clock = FakeClock()
        self.patch("time", self.clock)

    def outcome(self, got, err, calls, want):
        """Check (result, how many calls, the waits, what was said) against
        *want*: (0 or SystemExit or a status, calls, waits or None, phrases)."""
        result, count, waits, phrases = want
        if result is SystemExit:
            self.assertIsInstance(got, SystemExit, err)
        else:
            self.assertEqual(got, result, err)
        self.assertEqual(len(calls), count)
        if waits is not None:
            self.assertEqual(self.clock.slept, waits)
        for phrase in phrases:
            self.assertIn(phrase, err)

    def test_the_defaults_are_the_ones_cluster_config_declares(self):
        from clustertool import config

        declared = {key[len("RELAY_"):]: setting.default
                    for key, setting in config.RELAY.items()
                    if key.startswith("RELAY_RETR")}
        self.assertEqual(declared, self.relay.RETRY_DEFAULTS)
        for key in declared:
            self.assertRegex(self.relay.usage(), rf"(?m)^  {key}\b")

    def test_a_setting_is_read_and_a_bad_one_named(self):
        os.environ["CLUSTER_RELAY_RETRIES"] = "5"
        self.assertEqual(self.relay.relay_number("RETRIES"), 5)
        self.assertEqual(self.relay.retry_memory().limit, 5)
        for bad in ("lots", "-1", "nan", "inf"):
            with self.subTest(bad=bad):
                os.environ["CLUSTER_RELAY_RETRIES"] = bad
                got, err = quietly(self.relay.relay_number, "RETRIES")
                self.assertIsInstance(got, SystemExit)
                self.assertIn(f"[relay] RETRIES is '{bad}'", err)
                self.assertIn("its default is 3", err)

    def test_a_question_is_asked_again_only_over_a_lost_connection(self):
        """A probe or a manifest: exits of the command the relay host runs."""
        cases = [
            ([255, 255, 0], ("dir", 3, [2, 4], [
                "did not answer 'probe': its connection was lost (exit 255); "
                "asking again in 2s"])),
            ([255] * 10, (SystemExit, 4, [2, 4, 8], [
                "its connection was lost 4 times in quick succession",
                "[relay] RETRIES (3)"])),
            # find exits 1 over one unreadable directory: that is the answer.
            ([1, 0], (SystemExit, 1, None, ["could not answer 'probe' (exit 1)",
                                             "the connection held"])),
            ([self.relay.REFUSED, 0], (SystemExit, 1, None, ["refused the credentials"])),
            ([self.relay.NO_TOOL, 0], (SystemExit, 1, None, ["no cluster to run"])),
        ]
        for codes, want in cases:
            with self.subTest(codes=codes[:3]):
                self.clock.slept.clear()
                asked = []

                def argv(_host, args, codes=codes):
                    asked.append(args)
                    return [sys.executable, "-c",
                            f"print('dir'); raise SystemExit({codes.pop(0)})"]

                self.patch("serve_argv", argv)
                got, err = quietly(self.relay.serve_value, "host",
                                   ["probe", "--path", "/x"])
                self.outcome(got, err, asked, want)
        self.assertIn("`cluster login`, run from here, connects by hand on host",
                      quietly(self.relay.stopped_by_the_relay_host, "host",
                              self.relay.REFUSED, "x")[1])

    def test_a_stream_is_run_again_only_after_a_lost_connection_while_it_can_finish(self):
        """Each try is (exit, bytes moved, seconds spent moving them, the far
        end's exit[, the near end stopped first])."""
        drops = [(255, 10_000 * (n + 1), 3600, 255) for n in range(10)]
        cases = [
            (drops[:3] + [(0, 200_000, 3600, 0)], (0, 4, None, [
                "lost its connection with 9.8 KiB moved; starting it again "
                "from the first byte in 2s"])),
            # However long each try ran and however far it got, the next one
            # sends it all again: a stream that can never finish stops.
            (drops, (SystemExit, 4, [2, 4, 8], [
                "lost its connection 4 times in quick succession",
                "starts again from its first byte"])),
            ([(255, 0, 0.5, 255)] * 10, (SystemExit, 4, [2, 4, 8], [])),
            ([(1, 5000, 3600, 1)] * 10, (SystemExit, 1, None, [
                "the stream to /n/x failed (exit 1)", "the connection held"])),
            # GNU tar exits 2 at the end when one file could not be read; the
            # far end got everything else. Nothing about the connection failed.
            ([(2, 10**9, 3600, 0)] * 10, (SystemExit, 1, None, [])),
            # A full disk here: the drain stops, and the far end, left with no
            # reader, fails as a lost connection would.
            ([(2, 10**6, 60, 255, True)] * 10, (SystemExit, 1, None, ["failed (exit 2)"])),
            # The near end dies of the broken pipe first; the far end says why.
            ([(141, 0, 0, self.relay.REFUSED)] * 3, (SystemExit, 1, None, [
                "the cluster refused the credentials, so the stream to /n/x "
                "was not sent"])),
        ]
        for tries, want in cases:
            with self.subTest(first=tries[0]):
                self.clock.slept.clear()
                tries, calls = list(tries), []

                def fake(*args, tally=None, tries=tries, **_kw):
                    rc, moved, moving, far, *near = tries.pop(0)
                    calls.append(args)
                    self.clock.now += moving
                    tally.update(moved=moved, moving=moving, far=far)
                    tally["near stopped"] = bool(near and near[0])
                    return rc

                self.patch("stream", fake)
                got, err = quietly(self.relay.streamed, "to /n/x", "host", ["recv"])
                self.outcome(got, err, calls, want)

    def test_a_real_stream_reports_what_moved_and_the_far_ends_status(self):
        self.patch("time", __import__("time"))
        sink = self.tmp / "far-end"
        py = sys.executable
        self.patch("serve_argv", lambda host, args: [
            py, "-c", f"import sys; open({str(sink)!r}, 'wb').write("
                      "sys.stdin.buffer.read()); raise SystemExit(3)"])
        tally = {}
        rc = self.relay.stream("host", ["recv"], feed=[py, "-c", "print('x' * 99)"],
                               tally=tally)
        self.assertEqual((rc, tally["moved"], tally["far"]), (3, 100, 3))
        self.assertGreaterEqual(tally["moving"], 0)
        self.assertFalse(tally["near stopped"])

    def test_a_real_download_says_when_its_own_end_stopped_first(self):
        self.patch("time", __import__("time"))
        py = sys.executable
        self.patch("serve_argv", lambda host, args: [py, "-c", (
            "import os, sys\n"
            "try:\n"
            "    for _ in range(4096): sys.stdout.buffer.write(b'x' * 65536)\n"
            "    sys.stdout.flush()\n"
            "except BrokenPipeError:\n"
            "    os._exit(255)\n")])
        tally = {}
        rc = self.relay.stream("host", ["read"], drain=[py, "-c", "raise SystemExit(2)"],
                               tally=tally)
        self.assertEqual(rc, 2)
        self.assertEqual(tally["far"], 255, "the far end looks like a lost connection")
        self.assertTrue(tally["near stopped"])

    def test_a_staging_copy_resumes_on_rsyncs_statuses_for_a_lost_connection(self):
        # Socket I/O, the protocol stream cut off, its timeouts, and ssh's.
        self.assertEqual(self.relay.RSYNC_RESUMABLE, (10, 12, 30, 35, 255))
        cases = [
            ([(255, 5), (12, 5), (0, 5)], (0, 3, None, [
                "staging: rsync lost its connection (exit 255); resuming in 2s"])),
            ([(23, 5), (0, 5)], (23, 1, None, [])),
            # Drops spread over a long copy never add up.
            ([(255, 3600)] * 20 + [(0, 1)], (0, 21, None, [])),
            ([(255, 1)] * 10, (255, 4, None, ["4 times in quick succession"])),
        ]
        for runs, want in cases:
            with self.subTest(first=runs[:2]):
                runs, calls = list(runs), []

                def fake(sources, _dest, _progress, names=(), runs=runs):
                    rc, seconds = runs.pop(0)
                    calls.append(sources)
                    self.clock.now += seconds
                    return rc

                self.patch("rsync", fake)
                got, err = quietly(self.relay.rsync_riding, "staging", ["/src/"],
                                   ("h", "/stage/"), False)
                self.outcome(got, err, calls, want)


class TestTheStageKeeper(RelayTest):
    """A staged transfer's directory on the relay host looks used while it is,
    so another run's sweep, which goes by age, never takes it."""

    def stage(self):
        stage = self.tmp / "xfer-test"
        stage.mkdir()
        os.utime(stage, (0, 0))
        return stage

    @staticmethod
    def until(check, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if check():
                return True
            time.sleep(0.05)
        return False

    def test_the_keeper_touches_the_stage_and_ends_with_its_stdin(self):
        stage = self.stage()
        proc = subprocess.Popen([sys.executable, "-c", self.relay.STAGE_KEEPER,
                                 str(stage), "0.2"], stdin=subprocess.PIPE)
        self.addCleanup(proc.kill)
        self.assertTrue(self.until(lambda: stage.stat().st_mtime > 1e9))
        os.utime(stage, (0, 0))
        self.assertTrue(self.until(lambda: stage.stat().st_mtime > 1e9),
                        "touched again, not once")
        proc.stdin.close()
        self.assertEqual(proc.wait(timeout=10), 0)

    def test_a_keeper_whose_connection_drops_is_started_again(self):
        stage = self.stage()
        self.patch("ssh_argv", lambda host, command, **_kw: ["sh", "-c", command])
        keeper = self.relay.StageKeeper("host", str(stage), every=0.2)
        self.addCleanup(keeper.stop)
        first = keeper.proc
        self.assertTrue(self.until(lambda: stage.stat().st_mtime > 1e9))
        first.kill()
        first.wait()
        self.assertTrue(self.until(lambda: keeper.proc is not first
                                   and keeper.proc.poll() is None))
        os.utime(stage, (0, 0))
        self.assertTrue(self.until(lambda: stage.stat().st_mtime > 1e9))
        keeper.stop()
        self.assertIsNotNone(keeper.proc.poll(), "the keeper outlived the run")

    def test_a_keeper_that_cannot_start_costs_the_transfer_nothing(self):
        self.patch("ssh_argv", lambda host, command, **_kw:
                   [str(self.tmp / "no-such-ssh")])
        keeper = self.relay.StageKeeper("host", "/nowhere", every=0.2)
        self.assertIsNone(keeper.proc)
        keeper.stop()


class TestTheServeHalfSaysARefusal(RelayTest):
    """--serve exits REFUSED when the cluster rejected the credentials, so the
    client does not ask again, and LOST for a connection it could not open
    otherwise, which the client asks again as for any lost connection."""

    def test_a_refusal_has_its_own_exit_status_and_anything_else_is_lost(self):
        import http.client
        from clustertool import ui

        refused, lost = self.relay.REFUSED, self.relay.LOST
        cases = [
            (ui.Die(1), "Permission denied (keyboard-interactive).", refused, ""),
            # A SystemExit's own text is not lost.
            (SystemExit("cluster: sshproxy rejected the credentials"), "", refused,
             "rejected the credentials"),
            # A credential on record as refused is a refusal.
            (ui.Die(1), "the credentials were refused at 10:00; trying them once "
                        "more at 10:02", refused, ""),
            (ui.Die(1), "ssh: connect to host x port 22: Connection timed out", lost,
             ""),
            # What a reconnect can raise is said, not a traceback.
            (ValueError("the TOTP secret is not base32"), "", lost,
             "could not open a connection to the cluster"),
            (OSError(13, "Permission denied: 'secret'"), "", lost,
             "could not open a connection to the cluster"),
            (http.client.IncompleteRead(b""), "", lost,
             "could not open a connection to the cluster"),
        ]
        for exc, failure, status, said in cases:
            with self.subTest(exc=repr(exc), failure=failure):
                got, err = quietly(self.relay._open_failed, exc,
                                   SimpleNamespace(last_failure=failure))
                self.assertEqual(got, status)
                self.assertIn(said, err)
        self.assertEqual(lost, 255, "read as ssh's own lost connection")
        self.assertNotIn(refused, (0, 1, 2, 124, 127, 130, 255, self.relay.NO_TOOL))

    def test_serve_says_a_secret_it_cannot_read_rather_than_a_traceback(self):
        class Opening:
            def __init__(self, logins):
                self.logins = logins

            def open_connection(self, quiet=False):
                raise ValueError("the TOTP secret is not base32")

        ctx = SimpleNamespace(logins=SimpleNamespace(last_failure=""))
        with mock.patch("clustertool.context.Context", lambda *a, **k: ctx), \
                mock.patch("clustertool.transfer.Transfers", Opening):
            got, err = quietly(self.relay.serve, ["probe", "--path", "/x"])
        self.assertEqual(got, self.relay.LOST)
        self.assertIn("not base32", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
