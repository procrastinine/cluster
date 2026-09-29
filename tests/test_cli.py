#!/usr/bin/env python3
"""The command line: verb dispatch, aliases, argument splitting and --help.

Run: python3 -m unittest tests.test_cli
(or the whole suite: python3 -m unittest discover -s tests -p 'test_*.py')
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

# For `support`, which puts this checkout on sys.path and sets up the sandbox
# every test runs in, so it is imported before anything from clustertool.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import _IsolatedMachine, _patched, short_dir  # noqa: E402
from clustertool import cli, platform as plat, transfer  # noqa: E402
from clustertool.backends.base import Backend  # noqa: E402
from clustertool.commands import transfers  # noqa: E402
from clustertool.transfer import RcloneFound  # noqa: E402


class TestVerbDispatch(unittest.TestCase):
    """The first argument is always a command; a bare name is refused.

    An implicit attach would make a login unreachable whenever its name
    collided with a verb, and turn a typo into a new session named after it.
    """

    def setUp(self):
        from clustertool.commands import sessions

        self.cli = cli
        self.sessions = sessions

    def _die(self, *argv):
        """Run main() expecting a refusal, returning what the user was told."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                self.cli.main(list(argv))
        self.assertNotEqual(caught.exception.code, 0)
        return err.getvalue()

    def test_a_bare_name_is_not_a_command(self):
        message = self._die("definitely-not-a-verb")
        self.assertIn("unknown command", message)
        # And it must point at the command that was meant, not just complain.
        self.assertIn("cluster login definitely-not-a-verb", message)
        self.assertIn("new-session", message)

    def test_an_unknown_flag_is_still_a_flag(self):
        # A stray flag taken as a login name could be created but never closed,
        # because close() parses '--resume' as an option.
        self.assertIn("unknown option", self._die("--resume"))

    def test_short_aliases_reach_their_commands(self):
        # `l` reads as login; `ls` is the understood short form for listing, so
        # giving `l` to list would shadow the more useful one.
        for alias, target in (("a", "attach"), ("n", "new"), ("w", "where"),
                              ("l", "login"), ("ls", "list"), ("ss", "sessions"),
                              ("r", "run"), ("sh", "shell"), ("m", "mount"),
                              ("st", "status"), ("k", "kill-session")):
            self.assertIn(alias, self.cli.COMMANDS, f"alias {alias!r} missing")
            self.assertEqual(self.cli.COMMANDS[alias].cmd_name, target)

    def test_every_alias_is_claimed_once(self):
        # Two commands sharing a key would make one of them unreachable, and the
        # decorator silently lets the later registration win.
        keys = [k for func in set(self.cli.COMMANDS.values())
                for k, f in self.cli.COMMANDS.items() if f is func]
        self.assertEqual(len(keys), len(set(keys)))

    def test_backend_flag_is_accepted_anywhere_before_the_remote_command(self):
        strip = self.cli.strip_backend_flag
        self.assertEqual(strip(["login", "gpu", "--nersc"]), ("nersc", ["login", "gpu"]))
        self.assertEqual(strip(["--fasrc", "ls"]), ("fasrc", ["ls"]))
        self.assertEqual(strip(["ls", "--perlmutter"]), ("nersc", ["ls"]))
        self.assertEqual(strip(["ls", "--fas"]), ("fasrc", ["ls"]))
        # Ordinary options are left alone, and `cluster run main -- echo
        # --nersc` must send --nersc to the cluster, not switch backend.
        for argv in (["clean", "--dry-run", "--all"], ["transfer", "--up", "--sync"],
                     ["forget", "--all-backends", "-y"],
                     ["run", "main", "--", "echo", "--nersc"]):
            self.assertEqual(strip(argv), (None, argv))

    def test_the_drift_warning_names_real_commands(self):
        for name in self.cli._DRIFT_WARNING_COMMANDS:
            with self.subTest(name=name):
                self.assertEqual(self.cli.COMMANDS[name].cmd_name, name)

    def test_new_dispatches_on_how_many_names(self):
        # No names at all: say both forms rather than guessing one.
        message = self._die("new")
        self.assertIn("one name opens its configured tmux/shell", message)
        self.assertIn("two always create a tmux session", message)
        self.assertIn("cluster sh", message)
        job = ["--", "python", "job.py"]
        for mode, argv, runs, wanted in (
                # One name: a same-named tmux session, options and command kept.
                ("tmux", ["main"], "cmd_task", ["main", "main"]),
                ("tmux", ["--detach", "--no-mount", "--cwd", "/work", "main"] + job,
                 "cmd_task", ["--detach", "--no-mount", "--cwd", "/work", "main", "main"]
                 + job),
                # ...or a managed plain shell, if so configured.
                ("shell", ["main"], "cmd_shell", ["main"]),
                # Two are tmux, whatever one name opens.
                ("shell", ["main", "api"], "cmd_task", ["main", "api"])):
            with self.subTest(mode=mode, argv=argv):
                seen = []
                ctx = SimpleNamespace(login=lambda name: name,
                                      settings=SimpleNamespace(str=lambda key: mode))
                with _patched(self.sessions, runs,
                              lambda _ctx, args: seen.append(args) or 7):
                    self.assertEqual(self.sessions.cmd_new(ctx, argv), 7)
                self.assertEqual(seen, [wanted])

    def test_a_session_needs_both_names(self):
        # A one-name form would mean "session on login main", silently putting
        # work on whatever node main happens to be pinned to.
        message = self._die("new-session", "api")
        self.assertIn("both a login and a session name", message)
        self.assertIn("cluster new api", message)

    def test_help_lists_every_command_and_alias_and_how_to_start(self):
        from clustertool import backends

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.cli.main(["--help"])
        text = err.getvalue()
        self.assertIn("attach (a)", text)
        self.assertIn("first word is always a command", text)
        self.assertIn("First time on this machine? Start with:  cluster init", text)
        for func in set(self.cli.COMMANDS.values()):
            self.assertIn(func.cmd_name, text)
        for alias in backends.ALIASES:
            self.assertIn(f"--{alias}", text)

    def test_commands_are_listed_in_help_order(self):
        # The help groups related commands, whichever command module happened
        # to be imported first — this test module imports some directly.
        order = [func.cmd_name for func in self.cli.listed_commands()]
        self.assertEqual(len(order), len(set(order)))
        self.assertEqual(order[:5], ["init", "config", "backends", "nodes", "auth"])
        self.assertLess(order.index("clean"), order.index("mount"))
        self.assertLess(order.index("boot"), order.index("run"))
        self.assertLess(order.index("unpin"), order.index("ssh-command"))
        self.assertEqual(order[-3:], ["nersc-tool", "setup", "strays"])


class TestStartupImports(unittest.TestCase):
    """Every command imports the whole package before it starts, so a slow
    import the package makes at its top is paid by `cluster ls` and every
    other command, not only by the one that needs it.
    """

    #: the standard modules the package imports at its top: what they bring
    #: in is the floor, whichever Python this is.
    FLOOR = ("argparse", "base64", "codecs", "collections", "configparser",
             "contextlib", "datetime", "errno", "fcntl", "getpass", "json",
             "pathlib", "pty", "random", "re", "select", "shlex", "shutil",
             "signal", "socket", "struct", "subprocess", "tempfile", "textwrap",
             "threading", "time", "unicodedata", "warnings")
    #: each costs milliseconds and serves a command or two
    ONLY_WHERE_USED = ("ast", "concurrent.futures", "dataclasses", "difflib",
                       "email", "hashlib", "hmac", "http.client", "inspect",
                       "ssl", "tokenize", "urllib.request")

    def test_what_only_some_commands_need_is_imported_where_they_need_it(self):
        import subprocess

        code = ("import sys\n"
                f"import {', '.join(self.FLOOR)}\n"
                "floor = set(sys.modules)\n"
                f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
                "from clustertool import platform, cli\n"
                "print(' '.join(sorted(set(sys.modules) - floor)))\n")
        proc = subprocess.run([sys.executable, "-c", code], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        added = set(proc.stdout.split())
        self.assertIn("clustertool.cli", added)
        self.assertEqual(sorted(added & set(self.ONLY_WHERE_USED)), [])


class TestNothingSetUp(_IsolatedMachine):
    """A machine with no cluster set up gets told how to set one up."""

    def assertNothingCreated(self):
        for path in (self.config.STATE_ROOT, self.config.CTL_DIR,
                     self.config.MOUNT_ROOT):
            self.assertFalse(path.exists(), f"{path} was created")

    def test_every_command_says_how_to_set_one_up_and_creates_nothing(self):
        for argv, rc, says, never in (
                (["ls"], 0, ["no cluster is set up on this machine yet"], []),
                (["status"], 0, ["no cluster is set up",
                                 "cluster --nersc config credentials"], []),
                # One that needs a cluster names every site.
                (["new", "work"], 1, [
                    "no cluster is set up on this machine yet", "run `cluster init`",
                    "cluster --fasrc config credentials",
                    "cluster --nersc config credentials"], ["username"]),
                # A named backend says that backend.
                (["--nersc", "where", "work"], 1, [
                    "is not set up on this machine",
                    "cluster --nersc config credentials"], []),
                # A usage error comes before the missing backend.
                (["new-session", "api"], 1, ["both a login and a session name"],
                 ["set up"]),
                (["--nersc", "nodes"], 0, ["pool address"], []),
                # run without a login refuses a command word.
                (["run", "hostname", "-f"], 1, [
                    "no login named 'hostname'", "cluster run -- hostname -f",
                    "cluster login hostname"], [])):
            with self.subTest(argv=argv):
                got, out, err = self.run_cli(*argv)
                self.assertEqual(got, rc, err)
                for text in says:
                    self.assertIn(text, out + err)
                for text in never:
                    self.assertNotIn(text, err)
                self.assertNothingCreated()


#: A TOTP seed for the tests (the RFC 4648 example "Hello!\xde\xad\xbe\xef").
SEED = "JBSWY3DPEHPK3PXP"


class TestInit(_IsolatedMachine):
    """`cluster init` answered from a script, on a machine with ssh and
    nothing optional: no sshfs, rsync or rclone, and no network at all."""

    def setUp(self):
        import subprocess
        import urllib.request

        from clustertool import diagnostics

        super().setUp()
        self.home = self.root / "home"
        self.home.mkdir()
        tools = self.root / "tools"
        tools.mkdir()
        for name in ("ssh", "ssh-keygen"):
            (tools / name).write_text("#!/bin/sh\nexit 99\n")
            (tools / name).chmod(0o755)
        self.stack.enter_context(_patched(os, "environ", dict(
            os.environ, HOME=str(self.home), PATH=str(tools), SHELL="/bin/bash")))
        self.reached = []

        def offline(*args, **_options):
            self.reached.append(args)
            raise AssertionError("cluster init reached for the network")

        for owner, name in ((urllib.request, "urlopen"), (subprocess, "Popen"),
                            (Backend, "run_ssh")):
            self.stack.enter_context(_patched(owner, name, offline))
        # rclone is looked for in fixed places as well as on PATH.
        self.stack.enter_context(_patched(
            diagnostics, "_rclone_found",
            lambda: RcloneFound(None, None, "not found")))

    def tearDown(self):
        self.assertEqual(self.reached, [], "nothing may run or connect unasked")
        super().tearDown()

    def init(self, *flags, stdin=""):
        with _patched(sys, "stdin", io.StringIO(stdin)):
            return self.run_cli(*flags, "init")

    def cred(self, backend, filename):
        return self.config.CRED_ROOT / backend / filename

    def setting(self, key, backend=None):
        return self.config.file_value(key, backend)[0]

    @property
    def link(self):
        return self.home / ".local" / "bin" / "cluster"

    def enrolled(self, backend, user="someone"):
        directory = self.config.CRED_ROOT / backend
        directory.mkdir(parents=True)
        for filename, text in (("user", user), ("pass", "pw"), ("key.txt", SEED)):
            (directory / filename).write_text(text + "\n")
            (directory / filename).chmod(0o600)

    def test_one_cluster_from_a_script_with_nothing_optional_installed(self):
        from clustertool.commands.init import ENTRY

        # Past the credentials, the answers run out: every later question
        # takes its default, and only the ones that change nothing but this
        # machine default to yes.
        rc, out, err = self.init(stdin=f"fasrc\nsomeone\nhunter2\n{SEED}\ny\n")
        self.assertEqual(rc, 0, err)
        for filename, text in (("user", "someone"), ("pass", "hunter2"),
                               ("key.txt", SEED)):
            self.assertEqual(self.cred("fasrc", filename).read_text(), text + "\n")
        self.assertFalse((self.config.CRED_ROOT / "nersc").exists())
        self.assertEqual(self.setting("BACKEND"), "fasrc")
        self.assertEqual(self.setting("AUTO_MOUNT"), "0")
        self.assertEqual(self.link.resolve(), ENTRY.resolve())
        self.assertIn("export PATH=\"$HOME/.local/bin:$PATH\"' >> ~/.bashrc", out)
        for said in ("sshfs and fusermount not installed", "rsync not installed",
                     "rclone not found"):
            self.assertIn(said, out)
        self.assertIn("Try logging in to Harvard FASRC now?", err)
        self.assertIn("next: cluster --fasrc new work", out)
        self.assertIn("cluster doctor", out)
        self.assertNotIn("hunter2", out + err)

    def test_nersc_alone_with_its_flag(self):
        rc, out, err = self.init("--nersc", stdin=(
            f"someone\nhunter2\n{SEED}\ny\n"   # the credentials and their code
            "m1234\n"                          # COLLAB
            "n\n"                              # keep AUTO_MOUNT as it is
            "n\n"))                            # no link on PATH
        self.assertEqual(rc, 0, err)
        self.assertNotIn("Which clusters", err)
        self.assertTrue(self.cred("nersc", "key.txt").is_file())
        self.assertFalse((self.config.CRED_ROOT / "fasrc").exists())
        self.assertEqual(self.setting("BACKEND"), "nersc")
        self.assertEqual(self.setting("COLLAB", "nersc"), "m1234")
        self.assertIsNone(self.setting("AUTO_MOUNT"))
        self.assertFalse(self.link.exists())
        self.assertIn("run it as", out)
        self.assertIn("This contacts nersc and uses one TOTP code.", err)
        self.assertIn("next: cluster --nersc auth", out)

    def test_both_clusters(self):
        rc, out, err = self.init(stdin=(
            "both\n"
            f"someone\nhunter2\n{SEED}\ny\n"
            f"other\nsecret\n{SEED}\ny\n\n"
            "y\nn\nn\nn\n"))
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.cred("fasrc", "user").read_text(), "someone\n")
        self.assertEqual(self.cred("nersc", "user").read_text(), "other\n")
        self.assertIsNone(self.setting("BACKEND"), "with two, the choice is the user's")
        self.assertIn("new logins go to fasrc; to change that: "
                      "cluster config set BACKEND nersc", out)

    def test_an_answer_that_is_missing_or_wrong_changes_nothing(self):
        for stdin, said in (("", "no cluster was named; nothing was changed"),
                            ("mars\n", "name one or more of fasrc, nersc"),
                            ("fasrc\nsomeone\n", "nothing was saved"),
                            (f"fasrc\nsomeone\nhunter2\n{SEED}\nn\n",
                             "nothing was saved")):
            with self.subTest(stdin=stdin):
                rc, _out, err = self.init(stdin=stdin)
                self.assertEqual(rc, 1)
                self.assertIn(said, err)
                self.assertFalse(self.config.CRED_ROOT.exists())
                self.assertFalse(self.config.SETTINGS_FILE.exists())
                self.assertFalse(os.path.lexists(self.link))

    def test_a_cluster_that_is_set_up_is_kept_unless_asked(self):
        self.enrolled("fasrc")
        rc, _out, err = self.init(stdin="fasrc\n")
        self.assertEqual(rc, 0, err)
        self.assertIn("fasrc is set up as someone; go through its credentials "
                      "again? [y/N]", err)
        self.assertEqual(self.cred("fasrc", "pass").read_text(), "pw\n")

    def test_a_persisted_backend_that_is_not_set_up_is_offered_a_change(self):
        self.config.write_value("BACKEND", "nersc")
        rc, _out, err = self.init(stdin=f"fasrc\nsomeone\nhunter2\n{SEED}\ny\n")
        self.assertEqual(rc, 0, err)
        self.assertIn("BACKEND is nersc, which is not set up here; make it fasrc?", err)
        self.assertEqual(self.setting("BACKEND"), "fasrc")

    def try_login(self, rc, said):
        """init taking the test login it offers, which ssh answers with *said*."""
        import subprocess

        from clustertool.state import State

        ran = []

        def run_ssh(backend, argv, **_options):
            ran.append(argv)
            return subprocess.CompletedProcess(argv, rc, said, said)

        with _patched(Backend, "run_ssh", run_ssh), \
                _patched(State, "totp_pace", lambda self, **_options: True):
            return ran, self.init(stdin="fasrc\nn\nn\nn\ny\n")

    def test_the_test_login_is_one_throwaway_connection_on_a_yes(self):
        self.enrolled("fasrc")
        ran, (rc, _out, err) = self.try_login(
            0, "Password:\n__cluster_init__\nholylogin05.rc.fas.harvard.edu\n")
        self.assertEqual(rc, 0, err)
        self.assertIn("logged in to holylogin05.rc.fas.harvard.edu as someone", err)
        (argv,) = ran
        for option in ("ControlMaster=no", "ControlPath=none", "ControlPersist=no"):
            self.assertIn(option, argv)
        self.assertIn("echo __cluster_init__; hostname", argv[-1])
        # A refusal says what ssh said.
        _ran, (rc, _out, err) = self.try_login(
            255, "Password:\nPermission denied (keyboard-interactive).\n")
        self.assertEqual(rc, 1)
        self.assertIn("the login did not work (ssh exit 255): "
                      "Permission denied (keyboard-interactive).", err)

    def test_without_ssh_the_setup_is_saved_and_the_login_is_not_offered(self):
        (self.root / "tools" / "ssh").unlink()
        rc, out, err = self.init(stdin=f"fasrc\nsomeone\nhunter2\n{SEED}\ny\nn\nn\n")
        self.assertEqual(rc, 1)
        self.assertIn("ssh — not found; install OpenSSH's client", out)
        self.assertTrue(self.cred("fasrc", "key.txt").is_file())
        self.assertNotIn("Try logging in", err)
        self.assertIn("Harvard FASRC: a test login needs ssh, which is not installed", out)
        self.assertIn("first: ssh is not installed, and every connection needs it", out)


class TestRenamingALoginInUse(_IsolatedMachine):
    """What rides a login reaches it by its name, so renaming it under them
    takes --force: their next connection would fail, and an attach that
    reconnects would open a new login under the old name."""

    def rename(self, *flags, riders=((4242, "interactive session"),)):
        from clustertool import mounts, sshmux, tmuxlayer

        self.enrol("fasrc")
        self.record_login("fasrc", "work")
        # The rename checks the new name's sockets fit: a short directory,
        # since TMPDIR can be long (macOS).
        with _patched(self.config, "CTL_DIR", short_dir(self, "ctl")), \
                _patched(sshmux.Logins, "is_active", lambda _self, _name: True), \
                _patched(sshmux.Logins, "channel_clients",
                         lambda _self, _name: list(riders)), \
                _patched(tmuxlayer.Tmux, "retag_owner", lambda *_a: True), \
                _patched(mounts.Mounts, "is_mounted", lambda _self, _name: False):
            return self.run_cli("rename", "work", "api", *flags)

    def recorded(self, name):
        return (self.config.STATE_ROOT / "fasrc" / f"{name}.json").exists()

    def test_a_login_in_use_is_not_renamed_under_its_riders(self):
        rc, _out, err = self.rename()
        self.assertEqual(rc, 1)
        self.assertIn("login 'work' is in use by 1 command(s): "
                      "pid 4242 (interactive session)", err)
        self.assertIn("opens a new login called 'work'", err)
        self.assertIn("--force", err)
        self.assertTrue(self.recorded("work"), "a refused rename moves nothing")

    def test_force_renames_it_and_says_who_was_riding(self):
        rc, _out, err = self.rename("--force")
        self.assertEqual(rc, 0, err)
        self.assertIn("renaming 'work' while pid 4242 (interactive session) "
                      "still use(s) it", err)
        self.assertTrue(self.recorded("api"))
        self.assertFalse(self.recorded("work"))

    def test_its_own_mount_is_no_reason_to_refuse(self):
        # The rename takes the mount down and puts it back itself.
        rc, _out, err = self.rename(riders=((4243, "sshfs mount"),))
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.recorded("api"))


class TestCompleteData(unittest.TestCase):
    """`cluster _complete-data`: the completion's data, printed or put in place."""

    def test_it_prints_the_generated_part_and_rewrites_only_that(self):
        import tempfile

        from clustertool.commands import init

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["_complete-data"]), 0)
        self.assertEqual(out.getvalue(), cli.completion_data())
        self.assertNotIn("_complete-data", out.getvalue().split("\n", 1)[1])
        self.assertNotIn("_complete-data", cli.COMMANDS, "it is not a listed command")

        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "cluster.bash"
            script.write_text(f"before\n{cli.COMPLETION_BEGIN}\nold\n"
                              f"{cli.COMPLETION_END}\nafter\n")
            err = io.StringIO()
            with _patched(init, "COMPLETION", script), contextlib.redirect_stderr(err):
                self.assertEqual(cli.main(["_complete-data", "--update"]), 0)
            self.assertEqual(script.read_text(),
                             f"before\n{cli.completion_data()}after\n")
            self.assertIn("updated", err.getvalue())


class TestArgumentSplittingAndTerminalRecovery(unittest.TestCase):
    """rclone argument splitting, and putting a terminal back afterwards."""

    def test_rclone_flags_are_split_from_the_paths(self):
        # `--exclude '*.log'` must not be split, or the flag would reach rclone
        # with no value and '*.log' would become a third path.
        from clustertool.commands.transfers import split_rclone_args

        paths = ["./src", "remote:dst"]
        for argv, passthrough in (
                (paths + ["--exclude", "*.log"], ["--exclude", "*.log"]),
                (["--checksum"] + paths + ["--update"], ["--checksum", "--update"]),
                (paths + ["--exclude=*.log"], ["--exclude=*.log"])):
            self.assertEqual(split_rclone_args(argv), (paths, passthrough))
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            split_rclone_args(paths + ["--exclude"])

    def test_terminal_reset_disables_every_mouse_mode(self):
        # A dropped tmux session leaves these on locally, and the next scroll
        # arrives at the shell as literal text like "0;48;27M".
        reset = plat.TERMINAL_RESET
        for mode in (9, 47, 1000, 1001, 1002, 1003, 1004, 1005, 1006,
                     1007, 1015, 1016, 1047, 1048, 1049, 2004, 2026):
            self.assertIn(f"\033[?{mode}l", reset, f"mode {mode} not disabled")
        self.assertIn("\033[?1l", reset)     # application cursor keys
        self.assertIn("\033>", reset)        # application keypad
        self.assertIn("\033[>4;0m", reset)   # xterm modifyOtherKeys
        self.assertGreaterEqual(reset.count("\033[<u"), 2)  # kitty keyboard stack

    def test_terminal_restore_is_safe_with_no_saved_state(self):
        # Called on paths that may have had no tty at all; must never raise.
        plat.restore_tty(None)
        plat.restore_tty(plat.save_tty())

    def test_every_backend_interactive_path_restores_on_an_exception(self):
        from clustertool.backends import base

        restored = []
        backend = base.Backend.__new__(base.Backend)
        backend.interactive_auth = False

        def crash(_argv):
            raise RuntimeError("injected disconnect")

        with _patched(base.plat, "save_tty", lambda: "saved"), \
                _patched(base.plat, "restore_tty", restored.append), \
                _patched(base.subprocess, "run", crash):
            with self.assertRaisesRegex(RuntimeError, "injected disconnect"):
                backend.exec_interactive(["ssh"])
        self.assertEqual(restored, ["saved", "saved"])


class TestCommandHelp(unittest.TestCase):
    """--help is a question. No command may answer it with an action."""

    def setUp(self):
        self.cli = cli

    def help_for(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self.cli.main(list(argv))
        self.assertEqual(rc, 0)
        return out.getvalue()

    def test_every_command_explains_itself(self):
        # The safety net. `cluster close --help` falling through to "no name
        # given" would close the default connection, and nothing but a sweep
        # like this catches the next command that would do the same.
        for name in sorted({f.cmd_name for f in self.cli.COMMANDS.values()}):
            with self.subTest(command=name):
                self.assertTrue(self.help_for(name, "--help").startswith(
                    f"cluster {name}"))

    def test_short_flag_aliases_and_options_with_their_meaning(self):
        self.assertTrue(self.help_for("close", "-h").startswith("cluster close"))
        self.assertIn("also: a", self.help_for("attach", "--help"))
        text = self.help_for("list", "--help")
        self.assertIn("-q, --quiet", text)
        self.assertIn("connections only", text)

    def test_a_command_with_subverbs_names_them(self):
        for argv, usage in ((("bridge",), "Usage: cluster bridge push|status [LOGIN]"),
                            (("nersc-tool",), "Usage: cluster nersc-tool ACTION [LOGIN]"),
                            (("config",), "Usage: cluster [--BACKEND] config [show|list|"),
                            (("init",), "Usage: cluster [--BACKEND] init")):
            with self.subTest(command=argv[0]):
                self.assertIn(usage, self.help_for(*argv, "--help"))

    def test_options_from_a_shared_parser_are_found(self):
        # repin and unpin share _move_parser, so their options live in another
        # function entirely; reporting "no options" for them would be a lie.
        text = self.help_for("repin", "--help")
        for flag in ("--migrate", "--abandon", "-y, --yes"):
            self.assertIn(flag, text)

    def test_help_past_a_double_dash_belongs_to_the_remote_command(self):
        self.assertFalse(self.cli.wants_help(["main", "--", "rclone", "--help"]))
        self.assertTrue(self.cli.wants_help(["main", "--help", "--", "rclone"]))

    def test_no_option_is_left_unexplained(self):
        # An option with no help text is a listing that tells you nothing.
        for name in sorted({f.cmd_name for f in self.cli.COMMANDS.values()}):
            func = self.cli.COMMANDS[name]
            for flags, text in self.cli.declared_options(func):
                with self.subTest(command=name, option=flags):
                    self.assertTrue(text, f"{name} {flags} has no help text")

    def test_the_help_says_what_the_code_does(self):
        helps = {verb: dict(self.cli.declared_options(self.cli.COMMANDS[verb]))
                 for verb in ("new", "new-session", "attach", "close")}
        for verb in ("new", "new-session", "attach"):
            self.assertIn("registered on another node", helps[verb]["--here"])
        self.assertIn("every backend unless one is named", helps["close"]["--all"])


class TestTransferCommandLine(unittest.TestCase):
    """`transfer`, `ssh-command`, `push` and `pull`, with no connection behind them."""

    def fake_transfers(self, open_tags, opened="pool"):
        closed = []

        class Fake:
            def __init__(self, _logins):
                pass

            def active_tags(self):
                return list(open_tags)

            def close_connection(self, tag, force=False):
                closed.append((tag, force))
                return True

            def open_connection(self, node=None, quiet=False):
                return opened

            def lease_drop(self, _tag):
                pass

            def lease_take_for_caller(self, _tag):
                return False

            def lease_drop_for_caller(self, _tag):
                pass

            def node_of(self, _tag):
                return None

        return Fake, closed

    def run_transfer(self, *args, open_tags=("pool", "login01")):
        from clustertool.commands.transfers import cmd_transfer

        fake, closed = self.fake_transfers(open_tags)
        said = io.StringIO()
        with _patched(transfer, "Transfers", fake), \
                contextlib.redirect_stdout(said), contextlib.redirect_stderr(said):
            try:
                rc = cmd_transfer(SimpleNamespace(logins=None), list(args))
            except SystemExit as exc:
                rc = exc.code
        return rc, closed, said.getvalue()

    def test_close_closes_the_one_named_or_every_open_one(self):
        for args, wanted in ((["--close", "login01"], [("login01", False)]),
                             (["--force", "--close"], [("pool", True), ("login01", True)]),
                             (["--close", "gone"], [])):
            with self.subTest(args=args):
                rc, closed, said = self.run_transfer(*args)
                self.assertEqual((rc, closed), (0, wanted))
        self.assertIn("no open transfer connection gone", said)

    def test_an_unattended_sync_needs_yes(self):
        ran = []
        with _patched(transfers, "_transfer_from",
                      lambda *a, **k: ran.append(a[1]) or 0), \
                _patched(plat, "terminal_attached", lambda: False):
            rc, _closed, said = self.run_transfer("--sync", "./a", "remote:b")
            self.assertEqual(rc, 1)
            self.assertIn("refusing to change anything unattended", said)
            self.assertIn("pass -y", said)
            self.assertEqual(ran, [])
            rc, _closed, _said = self.run_transfer("--sync", "-y", "./a",
                                                   "remote:b")
        self.assertEqual(rc, 0)
        self.assertEqual(ran, [["./a"]])

    def ssh_command(self, *args, open_tags=()):
        from pathlib import Path as P

        from clustertool.commands.transfers import cmd_ssh_command

        fake, _closed = self.fake_transfers(open_tags)
        ctx = SimpleNamespace(
            logins=None,
            backend=SimpleNamespace(name="nersc", user="user",
                                    fqdn=lambda n: n, transfer_nodes=lambda: [],
                                    node_choosable=False,
                                    host_for=lambda _node: "dtn.example.gov"),
            state=SimpleNamespace(xfer_socket=lambda tag: P(f"/tmp/{tag}.sock")),
            settings=SimpleNamespace(int=lambda _key: 30))
        out, err = io.StringIO(), io.StringIO()
        with _patched(transfer, "Transfers", fake), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cmd_ssh_command(ctx, list(args)), 0)
        return out.getvalue(), err.getvalue()

    def test_ssh_command_names_the_connection_it_hands_out(self):
        out, err = self.ssh_command("--transfer")
        self.assertEqual(out.count("\n"), 1, "stdout stays one clean line")
        self.assertIn("user@dtn.example.gov", out)
        self.assertIn("opened transfer connection pool", err)
        self.assertIn("cluster --nersc transfer --close pool", err)
        _out, err = self.ssh_command("--transfer", open_tags=("pool",))
        self.assertIn("reusing transfer connection pool", err)
        _out, err = self.ssh_command("--transfer", "--quiet")
        self.assertEqual(err, "")

    def test_push_and_pull_need_rsync_before_any_connection(self):
        from clustertool.commands import configure, transfers

        ctx = SimpleNamespace(login=lambda *a: self.fail("resolved a login"))
        for command, verb in ((transfers.cmd_push, "push"),
                              (transfers.cmd_pull, "pull")):
            with self.subTest(verb=verb):
                err = io.StringIO()
                with _patched(transfers.shutil, "which", lambda _name: None), \
                        contextlib.redirect_stderr(err):
                    with self.assertRaises(SystemExit):
                        command(ctx, ["src", "dest"])
                self.assertIn(f"cluster {verb} needs rsync", err.getvalue())
                self.assertIn("apt install rsync", err.getvalue())
        err = io.StringIO()
        with _patched(transfers.shutil, "which", lambda _name: None), \
                contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit):
                configure.cmd_bridge(None, ["push"])
        self.assertIn("cluster bridge push needs rsync", err.getvalue())

    def test_a_missing_destination_is_a_usage_error_first(self):
        err = io.StringIO()
        with _patched(transfers.shutil, "which", lambda _name: None), \
                contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit):
                transfers.cmd_pull(SimpleNamespace(), ["only-one"])
        self.assertIn("usage: cluster pull [LOGIN] SRC DEST", err.getvalue())
        self.assertNotIn("rsync", err.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
