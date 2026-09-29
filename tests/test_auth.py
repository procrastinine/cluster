#!/usr/bin/env python3
"""Authentication: TOTP, credential files, failures and prompt answering.

Where secrets live and how a TOTP seed is checked, what a failed login
says, and answering ssh's password and code prompts under a pty.

Run: python3 -m unittest tests.test_auth
(or the whole suite: python3 -m unittest discover -s tests -p 'test_*.py')
"""

from __future__ import annotations

import contextlib
import io
import json
import os
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
from clustertool.auth import explain_failure, totp, totp_window  # noqa: E402


class TestTotp(unittest.TestCase):
    #: RFC 6238 appendix B, SHA1, secret "12345678901234567890"
    SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"

    def test_rfc6238_vectors_formatted_secrets_and_the_30s_window(self):
        spaced = "GEZD GNBV GY3T-QOJQ GEZD GNBV GY3T QOJQ"
        for secret, when, code in ((self.SECRET, 59, "287082"),
                                   (self.SECRET, 1111111109, "081804"),
                                   (self.SECRET, 1111111111, "050471"),
                                   (spaced, 59, "287082")):
            self.assertEqual(totp(secret, when=when), code)
        self.assertEqual([totp_window(when=w) for w in (0, 29.9, 30)], [0, 0, 1])


class TestCredentialLocation(unittest.TestCase):
    """Secrets live in one private place, and nowhere else is searched."""

    class FakeSettings:
        def __init__(self, cred_dir=None):
            self._cred_dir = cred_dir

        def _raw(self, key):
            return self._cred_dir if key == "CRED_DIR" else None

    def setUp(self):
        from clustertool import config
        from clustertool.backends import base

        self.config, self.base = config, base
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._saved = config.CRED_ROOT
        config.CRED_ROOT = self.root / "config" / "cluster" / "credentials"
        self.home = self.root / "home"
        self.home.mkdir()

    def tearDown(self):
        self.config.CRED_ROOT = self._saved
        self.tmp.cleanup()

    def resolve(self, cred_dir=None):
        from unittest.mock import patch

        with patch.object(self.base.Path, "home", staticmethod(lambda: self.home)):
            return self.base.resolve_cred_dir("fasrc", self.FakeSettings(cred_dir))

    def test_the_setting_wins_else_the_preferred_place_never_a_home_dir(self):
        # The preferred place, so setup instructions and errors name where we
        # want them; ~/fasrc is as likely to be somebody's project as ours.
        self.assertEqual(self.resolve(), self.config.CRED_ROOT / "fasrc")
        (self.home / "fasrc").mkdir()
        self.assertEqual(self.resolve(), self.config.CRED_ROOT / "fasrc")
        self.assertEqual(self.resolve("/somewhere/else"), Path("/somewhere/else"))

    def test_secrets_are_named_and_are_a_subset_of_credential_files(self):
        self.assertTrue(set(self.base.CRED_SECRETS) <= set(self.base.CRED_FILES))
        self.assertIn("pass", self.base.CRED_SECRETS)
        self.assertIn("key.txt", self.base.CRED_SECRETS)

    def test_a_group_readable_secret_is_refused(self):
        # The mode check is what stops a shared-machine mistake becoming a leak.
        from clustertool.auth import read_secret_file

        path = self.root / "pass"
        path.write_text("hunter2\n")
        path.chmod(0o644)
        with self.assertRaises(PermissionError):
            read_secret_file(path, "password file")
        path.chmod(0o600)
        self.assertEqual(read_secret_file(path, "password file"), "hunter2")


class TestFailureExplanation(unittest.TestCase):
    """A failed connection has to say what went wrong, not what came last."""

    BANNER = ["** WARNING: connection is not using a post-quantum key exchange "
              "algorithm.",
              "** This session may be vulnerable to \"store now, decrypt later\".",
              "** The server may need to be upgraded. See https://openssh.com/pq.html"]

    def test_the_real_reason_outranks_the_banner(self):
        # The banner is the last thing the server prints, so the last line
        # would read "the server may need to be upgraded" even for a wrong
        # password.
        lines = ["(u@h) Password: ", "(u@h) VerificationCode: ",
                 "Permission denied (keyboard-interactive)."] + self.BANNER
        self.assertEqual(explain_failure(lines),
                         "Permission denied (keyboard-interactive).")

    def test_debug_chatter_the_banner_and_nothing_say_what_they_can(self):
        lines = ["debug1: Connecting to login.example.gov port 22.",
                 "ssh: connect to host login.example.gov port 22: No route to host",
                 "debug2: channel 0: free"]
        self.assertIn("No route to host", explain_failure(lines))
        # Better than quoting the banner: it invites debugging the wrong thing.
        for lines in (self.BANNER, [], ["", "   "]):
            self.assertEqual(explain_failure(lines), "no error output")


class TestRejectionIsToldFromABlip(unittest.TestCase):
    """The watcher stops retrying a refused credential and keeps retrying the rest."""

    def test_refusals_and_unusable_secrets_are_rejections_a_network_is_not(self):
        from clustertool.auth import is_rejection

        for detail in ("Permission denied (keyboard-interactive).",
                       "Too many authentication failures",
                       "sshproxy rejected the credentials: Authentication failed",
                       "missing password file: /nonexistent/pass",
                       "missing NERSC TOTP seed: /nonexistent/key.txt",
                       "password file /nonexistent/pass is mode 644; it holds a secret",
                       "TOTP seed /nonexistent/key.txt is empty",
                       "Non-base32 digit found"):
            self.assertTrue(is_rejection(detail), detail)
        for detail in ("Connection closed by 192.0.2.10 port 22",
                       "ssh: connect to host login01 port 22: No route to host",
                       "cannot reach https://sshproxy.example/: timed out",
                       "sshproxy returned HTTP 503: Service Unavailable",
                       "sshproxy did not answer completely: IncompleteRead(0 bytes read)",
                       "no error output", ""):
            self.assertFalse(is_rejection(detail), detail)


class TestFailureText(unittest.TestCase):
    """One line saying what failed, whatever form the failure took."""

    def test_a_refusal_a_die_and_any_other_exception(self):
        import http.client

        from clustertool import ui
        from clustertool.auth import failure_text

        # A refusal says it itself, without the prefix; a die that already
        # printed falls back to the detail; anything else is its message or type.
        self.assertEqual(
            failure_text(SystemExit("cluster: sshproxy rejected the credentials: x\n"
                                    "  This usually means a wrong password"),
                         "ignored"),
            "sshproxy rejected the credentials: x This usually means a wrong password")
        self.assertEqual(failure_text(ui.Die(1), "Permission denied (publickey)."),
                         "Permission denied (publickey).")
        self.assertEqual(failure_text(ui.Die(1)), "no error output")
        self.assertEqual(failure_text(OSError(5, "Input/output error")),
                         "[Errno 5] Input/output error")
        self.assertEqual(failure_text(http.client.HTTPException()), "HTTPException")

    def test_only_a_refusal_to_connect_is_a_refused_credential(self):
        from clustertool import ui
        from clustertool.auth import refused_by

        self.assertTrue(refused_by(ui.Die(1), "Permission denied (publickey)."))
        self.assertTrue(refused_by(SystemExit("cluster: sshproxy rejected the "
                                              "credentials: x")))
        self.assertFalse(refused_by(ui.Die(1), "Connection timed out"))
        # This machine's own file, not the cluster's answer.
        self.assertFalse(refused_by(PermissionError(13, "Permission denied"),
                                    "Permission denied (keyboard-interactive)."))
        self.assertFalse(refused_by(ValueError("Permission denied")))


class TestPromptAnswering(unittest.TestCase):
    """The password is typed for ssh, never for something inside the session.

    Driven through a real pty against a stand-in for ssh that prints what the
    FASRC login pool prints, turns echo off the way readpassphrase does, and
    records every byte it was sent.
    """

    PASSWORD = "s3cret-pw"
    CODE = "123456"
    PW = "(user@holylogin06.rc.fas.harvard.edu) Password: "
    OTP = "(user@holylogin06.rc.fas.harvard.edu) VerificationCode: "

    CHILD = r'''
import json, os, select, sys, termios, time
steps, record = json.loads(sys.argv[1]), sys.argv[2]
got, raw = [], b""
for step in steps:
    if step[0] == "out":
        os.write(1, step[1].encode())
        continue
    prompt, wait = step[1], step[2]
    old = termios.tcgetattr(0)
    quiet = termios.tcgetattr(0)
    quiet[3] &= ~termios.ECHO
    termios.tcsetattr(0, termios.TCSANOW, quiet)
    os.write(1, prompt.encode())
    line, deadline = b"", time.monotonic() + wait
    while b"\n" not in line:
        left = deadline - time.monotonic()
        if left <= 0 or not select.select([0], [], [], left)[0]:
            break
        chunk = os.read(0, 1024)
        if not chunk:
            break
        line += chunk
    raw += line
    termios.tcsetattr(0, termios.TCSANOW, old)
    os.write(1, b"\n")
    got.append(line.decode().strip() if b"\n" in line else None)
if select.select([0], [], [], 0.3)[0]:
    raw += os.read(0, 4096)
with open(record, "w") as handle:
    json.dump({"got": got, "raw": raw.decode("utf-8", "replace")}, handle)
'''

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.script = self.root / "fake_ssh.py"
        self.script.write_text(self.CHILD)

    def tearDown(self):
        self.tmp.cleanup()

    def converse(self, *steps):
        from clustertool.auth import run_with_prompts

        record = self.root / "record.json"
        sink = io.BytesIO()
        with open(os.devnull) as devnull, _patched(sys, "stdin", devnull):
            rc = run_with_prompts(
                [sys.executable, str(self.script), json.dumps(steps), str(record)],
                self.PASSWORD, lambda: self.CODE, sink=sink)
        self.assertEqual(rc, 0, sink.getvalue())
        result = json.loads(record.read_text())
        return result["got"], result["raw"]

    def ask(self, prompt, wait=10.0):
        return ["ask", prompt, wait]

    def unanswered(self, prompt):
        # Short: the test waits this long for an answer that must not come,
        # and a wrong one would be typed as soon as the prompt was read. One
        # typed later still lands in the record, which counts the password.
        return ["ask", prompt, 0.25]

    def test_password_and_code_are_answered_and_answered_again_after_a_refusal(self):
        got, _raw = self.converse(
            ["out", "** WARNING: connection is not using a post-quantum key "
                    "exchange algorithm.\r\n** The server may need to be "
                    "upgraded. See https://openssh.com/pq.html\r\n"],
            self.ask(self.PW), self.ask(self.OTP),
            ["out", "Permission denied, please try again.\r\n"],
            self.ask(self.PW), self.ask(self.OTP),
            ["out", "Last login: Mon Sep 28 10:00:00 2026 from 10.0.0.1\r\n"])
        self.assertEqual(got, [self.PASSWORD, self.CODE] * 2)

    def test_prompts_inside_the_session_are_left_alone(self):
        got, raw = self.converse(
            self.ask(self.PW), self.ask(self.OTP),
            ["out", "Last login: Mon Sep 28 10:00:00 2026 from 10.0.0.1\r\n"
                    "[user@holylogin06 ~]$ "],
            self.unanswered("[sudo] password for user: "),
            self.unanswered("user@otherhost's password: "),
            self.unanswered("Password: "))
        self.assertEqual(got, [self.PASSWORD, self.CODE, None, None, None])
        self.assertEqual(raw.count(self.PASSWORD), 1,
                         "the password reached the session a second time")

    def test_a_shell_prompt_alone_is_enough_to_end_authentication(self):
        # No newline after the session starts: a bare shell prompt, then a
        # program asking on the same line.
        got, raw = self.converse(
            self.ask(self.PW), self.ask(self.OTP),
            ["out", "$ "], self.unanswered("Password: "))
        self.assertEqual(got, [self.PASSWORD, self.CODE, None])
        self.assertEqual(raw.count(self.PASSWORD), 1)

    def test_the_latch_judges_whole_lines_across_reads(self):
        from clustertool.auth import _AnswerLatch

        latch = _AnswerLatch()
        self.assertTrue(latch.may_answer(), "anything goes before an answer")
        latch.answering()
        for chunk in (b"\r", b"\n(user@h) Verif", b"icationCode: "):
            latch.feed(chunk)
        self.assertTrue(latch.may_answer())
        latch.answering()
        latch.feed(b"\r\nLast log")
        self.assertTrue(latch.open, "a line still arriving is not judged yet")
        latch.feed(b"in: today\r\n(user@h) Password: ")
        self.assertFalse(latch.may_answer())
        latch.answering()
        latch.feed(b"\r\nPassword: ")
        self.assertFalse(latch.may_answer(), "a closed latch never reopens")

    def test_a_failed_exec_leaves_no_forked_copy_behind(self):
        from clustertool.auth import run_with_prompts

        said = []
        parent = os.getpid()
        with open(os.devnull) as devnull, _patched(sys, "stdin", devnull):
            rc = run_with_prompts([str(self.root / "no-such-program")],
                                  self.PASSWORD, lambda: self.CODE,
                                  quiet=True, transcript=said)
        # Had the child fallen out of the exec, it would be the one running
        # this line, with the rest of the suite still ahead of it.
        self.assertEqual(os.getpid(), parent)
        self.assertEqual(rc, 127)
        self.assertIn("cannot run", "".join(said))


class TestPtyRunIsBounded(unittest.TestCase):
    """A pty run has a deadline and a transcript of bounded size, like plat.run."""

    def run_child(self, code, **kwargs):
        from clustertool.auth import run_with_prompts

        said = []
        with open(os.devnull) as devnull, _patched(sys, "stdin", devnull):
            rc = run_with_prompts([sys.executable, "-c", code], "pw",
                                  lambda: "123456", quiet=True,
                                  transcript=said, **kwargs)
        return rc, "".join(said)

    def test_at_the_deadline_the_child_and_all_it_started_are_killed(self):
        code = ("import subprocess, time; "
                "p = subprocess.Popen(['sleep', '60']); "
                "print('grandchild', p.pid, flush=True); time.sleep(60)")
        start = time.monotonic()
        rc, said = self.run_child(code, timeout=0.5)
        self.assertEqual(rc, 124, "the same code plat.run reports")
        self.assertLess(time.monotonic() - start, 10)
        self.assertIn("gave up after 0.5s", said)
        grandchild = int(said.split("grandchild", 1)[1].split()[0])
        deadline = time.monotonic() + 5
        while _alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(_alive(grandchild), "the process group was killed")

    def test_no_deadline_waits_for_the_child(self):
        rc, said = self.run_child("print('done')")
        self.assertEqual(rc, 0)
        self.assertIn("done", said)

    def test_a_reply_of_a_few_hundred_kilobytes_is_kept_whole(self):
        # The transcript is the stdout of a marker-framed read on a password
        # backend, so the marker at its start must survive a long reply.
        rc, said = self.run_child(
            "import sys; print('__marker__'); "
            "sys.stdout.write(('x' * 99 + '\\n') * 4000)")
        self.assertEqual(rc, 0)
        self.assertIn("__marker__", said)
        self.assertGreaterEqual(said.count("x" * 99), 4000)

    def test_an_endless_session_keeps_only_its_end(self):
        from clustertool.auth import TRANSCRIPT_LIMIT

        rc, said = self.run_child(
            "import sys; sys.stdout.write('start\\n' + ('y' * 1023 + '\\n') "
            "* 3072 + 'the end\\n')")
        self.assertEqual(rc, 0)
        self.assertLessEqual(len(said), TRANSCRIPT_LIMIT + 8192)
        self.assertNotIn("start", said)
        self.assertIn("the end", said)

    def test_a_character_split_across_reads_survives(self):
        rc, said = self.run_child(
            "import os, time; os.write(1, b'caf\\xc3'); time.sleep(0.3); "
            "os.write(1, b'\\xa9\\n')")
        self.assertEqual(rc, 0)
        self.assertIn("café", said)

    def test_run_ssh_hands_its_timeout_to_the_pty_run(self):
        from clustertool.backends import base, load

        backend = load("fasrc")
        seen = {}

        def fake(argv, password, otp, **kwargs):
            seen.update(kwargs)
            return 0

        with _patched(base, "run_with_prompts", fake), \
                _patched(type(backend), "_password", lambda self: "pw"):
            backend.run_ssh(["ssh", "host", "true"], timeout=7)
        self.assertEqual(seen["timeout"], 7)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie is dead for this purpose: it only waits to be reaped.
    try:
        with open(f"/proc/{pid}/stat") as handle:
            return handle.read().split(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


class TestTotpSeedIsCheckedBeforeSaving(unittest.TestCase):
    def setUp(self):
        from clustertool import config

        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {k: os.environ.pop(k, None)
                      for k in ("CLUSTER_CRED_DIR", "CLUSTER_FASRC_CRED_DIR")}
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(_patched(config, "CRED_ROOT", Path(self.tmp.name)))
        self.seed_file = Path(self.tmp.name) / "fasrc" / "key.txt"

    def tearDown(self):
        self.stack.close()
        for key, value in self.saved.items():
            if value is not None:
                os.environ[key] = value
        self.tmp.cleanup()

    def set_seed(self, typed):
        from unittest.mock import patch

        from clustertool import configcmd

        invocation = types.SimpleNamespace(backend_name="fasrc", explicit_backend=True)
        out, err = io.StringIO(), io.StringIO()
        with patch.object(configcmd.getpass, "getpass", lambda prompt="": typed), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = configcmd._set(invocation, "TOTP", None)
            except SystemExit as exc:
                rc = exc.code
        return rc, out.getvalue(), err.getvalue()

    def test_a_seed_that_is_not_base32_is_not_saved(self):
        for typed in ("123456", "not a seed!", "GEZDGNBV1"):
            with self.subTest(typed=typed):
                rc, _out, err = self.set_seed(typed)
                self.assertNotEqual(rc, 0)
                self.assertIn("not a base32 TOTP seed", err)
                self.assertFalse(self.seed_file.exists())

    def test_a_good_seed_is_saved_and_its_code_shown_on_stderr_only(self):
        typed = "gezd gnbv-gy3t qojq gezd gnbv gy3t qojq"
        rc, out, err = self.set_seed(typed)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.seed_file.read_text().strip(), typed)
        self.assertRegex(err, r"code right now: \d{6} ")
        self.assertNotRegex(out, r"\d{6}", "a live code never goes to stdout")


if __name__ == "__main__":
    unittest.main(verbosity=2)
