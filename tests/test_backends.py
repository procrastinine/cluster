#!/usr/bin/env python3
"""Backends: node classes, per-site wiring and what a site needs set up.

Also a password backend's pty reads, backend names as reserved words,
and the NODES setting.

Run: python3 -m unittest tests.test_backends
(or the whole suite: python3 -m unittest discover -s tests -p 'test_*.py')
"""

from __future__ import annotations

import contextlib
import http.client
import io
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

# For `support`, which puts this checkout on sys.path and sets up the sandbox
# every test runs in, so it is imported before anything from clustertool.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import (  # noqa: E402
    COMPLETION_SCRIPT,
    REPO_ROOT,
    FakeClock,
    _IsolatedMachine,
    _patched,
    _refusal,
    temp_state,
)
from clustertool import backends, crossxfer  # noqa: E402
from clustertool.auth import is_rejection  # noqa: E402
from clustertool.backends import BACKENDS, load, resolve_name  # noqa: E402
from clustertool.config import Settings  # noqa: E402
from clustertool.context import Context  # noqa: E402
from clustertool.state import Refusals  # noqa: E402
from clustertool.nodes import LOGIN, MOUNT, NodeClass, NodeMap, TRANSFER  # noqa: E402


class TestNodeMap(unittest.TestCase):
    def setUp(self):
        self.map = NodeMap([
            NodeClass(name="login", routable=False, purposes=frozenset({LOGIN}),
                      template="login{n:02d}.example.gov", count=3),
            NodeClass(name="dtn", routable=True,
                      purposes=frozenset({TRANSFER, MOUNT}),
                      hosts=("dtn01.example.gov", "dtn02.example.gov")),
        ])

    def test_classify_by_short_name(self):
        self.assertEqual(self.map.classify("login02").name, "login")
        self.assertEqual(self.map.classify("dtn01.example.gov").name, "dtn")
        self.assertIsNone(self.map.classify("nope99"))

    def test_routability_drives_the_jump_decision(self):
        self.assertFalse(self.map.routable("login01"))
        self.assertTrue(self.map.routable("dtn02"))
        # An unknown host must not be assumed unroutable, or it becomes
        # unreachable for no reason.
        self.assertTrue(self.map.routable("some-other-host"))

    def test_purposes_and_zero_padded_templates(self):
        logins = ["login01.example.gov", "login02.example.gov", "login03.example.gov"]
        self.assertEqual(self.map.classes[0].members(), logins)
        self.assertEqual(self.map.for_purpose(LOGIN), logins)
        self.assertEqual(self.map.for_purpose(MOUNT),
                         ["dtn01.example.gov", "dtn02.example.gov"])


class TestBackendWiring(unittest.TestCase):
    def test_aliases_resolve(self):
        self.assertEqual(resolve_name("fas"), "fasrc")
        self.assertEqual(resolve_name("perlmutter"), "nersc")
        self.assertEqual(resolve_name("NERSC"), "nersc")
        with self.assertRaises(SystemExit):
            resolve_name("nope")

    def test_nersc_login_nodes_need_a_jump_and_dtns_do_not(self):
        backend = load("nersc")
        login = backend.pool_nodes()[0]
        self.assertTrue(any("ProxyCommand" in part
                            for part in backend.jump_opts(login)))
        dtn = backend.mount_nodes()[0]
        self.assertEqual(backend.jump_opts(dtn), [])

    def test_the_jump_hop_is_configured_like_every_other_connection(self):
        import shlex

        backend = load("nersc")
        (proxy,) = [part for part in backend.jump_opts(backend.pool_nodes()[0])
                    if part.startswith("ProxyCommand=")]
        inner = shlex.split(proxy[len("ProxyCommand="):])
        self.assertEqual(inner[:3], ["ssh", "-F", "/dev/null"])
        for option in ("BatchMode=yes", "ControlPath=none",
                       f"ServerAliveInterval={backend.settings.int('SSH_SERVER_ALIVE_INTERVAL')}",
                       f"ServerAliveCountMax={backend.settings.int('SSH_SERVER_ALIVE_COUNT_MAX')}"):
            self.assertIn(option, inner)
        self.assertEqual(inner[-3:], ["-W", "%h:%p", f"{backend.user}@{backend.pool_host}"])

    def test_nersc_pins_are_deterministic_per_login_name(self):
        backend = load("nersc")
        first = backend.node_candidates_for("main")
        again = backend.node_candidates_for("main")
        other = backend.node_candidates_for("other")
        self.assertEqual(first, again)
        self.assertNotEqual(first[0], other[0])

    def test_fasrc_lets_the_balancer_choose_and_mounts_through_a_login(self):
        backend = load("fasrc")
        self.assertEqual(backend.node_candidates_for("main"), [None])
        self.assertTrue(backend.interactive_auth)
        self.assertTrue(backend.paces_totp)
        self.assertEqual(backend.mount_via, "login")
        self.assertEqual(load("nersc").mount_via, "mount_node")

    def test_settings_scope_per_backend(self):
        from unittest import mock

        with mock.patch.dict(os.environ, {"CLUSTER_MAX_LOGINS": "7",
                                          "CLUSTER_NERSC_MAX_LOGINS": "9"}):
            self.assertEqual(Settings("fasrc").int("MAX_LOGINS"), 7)
            self.assertEqual(Settings("nersc").int("MAX_LOGINS"), 9)


def certificate_pair(directory):
    """(private key, certificate line, public key) signed by a throwaway CA.

    The certificate is valid for a day, as sshproxy's are.
    """
    directory = Path(directory)
    for name in ("ca", "user"):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name,
                        "-f", str(directory / name)], check=True)
    subprocess.run(["ssh-keygen", "-q", "-s", str(directory / "ca"), "-I", "test",
                    "-n", "user", "-V", "+1d", str(directory / "user.pub")],
                   check=True)
    return ((directory / "user").read_text(),
            (directory / "user-cert.pub").read_text().strip(),
            (directory / "user.pub").read_text().strip())


#: What sshproxy says when it refuses the credentials.
REJECTED = b"Authentication failed. Check your password and OTP.\n"

needs_ssh_keygen = unittest.skipUnless(shutil.which("ssh-keygen"),
                                       "ssh-keygen is not installed")


class _SshproxyHarness(unittest.TestCase):
    """A nersc backend with its key and state in a temp dir, and a fake sshproxy."""

    #: 5 s into TOTP window 100, so a claim needs no wait.
    START = 3000.0 + 5

    def setUp(self):
        from clustertool.backends import nersc

        self.nersc = nersc
        self.root = temp_state(self)
        self.backend = load("nersc")
        self.backend.key_path = self.root / "ssh" / "nersc"
        self.requests = []
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.clock = FakeClock(self.START)
        self.stack.enter_context(_patched(nersc, "time", self.clock))
        self.stack.enter_context(_patched(self.backend, "ensure_known_hosts",
                                          lambda: False))

    def answer(self, *replies):
        """Make urlopen reply with *replies* in turn.

        Each is bytes, an exception urlopen raises, or ("cut", exception) for
        an answer whose read raises it.
        """
        pending = list(replies)

        class Response(io.BytesIO):
            def __init__(self, body, failure=None):
                super().__init__(body)
                self.failure = failure

            def read(self, *args):
                if self.failure is not None:
                    raise self.failure
                return super().read(*args)

            def __exit__(self, *exc):
                return False

        def urlopen(request, timeout=None, context=None):
            self.requests.append((request.full_url, context,
                                  request.get_header("Authorization")))
            reply = pending.pop(0)
            if callable(reply):
                reply = reply()
            if isinstance(reply, BaseException):
                raise reply
            if isinstance(reply, tuple):
                return Response(b"", reply[1])
            return Response(reply)

        # The backend imports urllib.request where it fetches, so the
        # module's own attribute is the one to replace.
        self.stack.enter_context(_patched(urllib.request, "urlopen", urlopen))

    def refusal(self):
        with contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as caught:
            self.backend.fetch_certificate(quiet=True)
        return str(caught.exception.code)

    def http_error(self, code, body=b"", reason="Error"):
        """An answer with HTTP status *code*, made afresh each time it is sent."""
        return lambda: urllib.error.HTTPError(self.nersc.SSHPROXY_URL, code, reason,
                                              {}, io.BytesIO(body))

    def unverifiable(self):
        return urllib.error.URLError(ssl.SSLCertVerificationError("unable to get issuer"))

    def pair(self):
        """(key, certificate, public key, sshproxy's answer for them)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        key, cert, pub = certificate_pair(tmp.name)
        return key, cert, pub, (key + cert + "\n").encode()

    def codes(self):
        """The TOTP code each request carried."""
        import base64

        return [base64.b64decode(header.split()[1]).decode()[-6:]
                for _url, _context, header in self.requests]


class TestSshproxyExchange(_SshproxyHarness):
    """What NERSC's certificate service answers, and what the tool makes of it."""

    def test_an_empty_answer_is_a_refusal_not_a_crash(self):
        for body in (b"", b"\n  \n"):
            with self.subTest(body=body):
                self.answer(body)
                self.assertIn("did not return a key", self.refusal())

    def test_an_answer_without_a_key_names_what_came_back(self):
        self.answer(b"Service unavailable\n")
        self.assertIn("Service unavailable", self.refusal())

    def test_an_unverifiable_certificate_on_linux_says_so(self):
        self.answer(self.unverifiable())
        with _patched(self.nersc.sys, "platform", "linux"):
            said = self.refusal()
        self.assertIn("cannot verify", said)
        self.assertNotIn("cannot reach", said)
        self.assertEqual(len(self.requests), 1, "nothing is retried off macOS")

    def test_on_macos_a_python_without_certificates_is_retried_then_explained(self):
        failure = self.unverifiable()
        with tempfile.NamedTemporaryFile(suffix=".pem") as bundle, \
                _patched(self.nersc.sys, "platform", "darwin"), \
                _patched(self.nersc, "MAC_CA_BUNDLE", bundle.name), \
                _patched(ssl, "create_default_context",
                         lambda cafile=None: ("context", cafile)):
            self.answer(failure, b"")
            self.refusal()
            self.assertEqual(len(self.requests), 2)
            self.assertEqual(self.requests[1][1], ("context", bundle.name))
            self.requests.clear()
            self.answer(failure, failure)
            said = self.refusal()
        self.assertIn("Install Certificates.command", said)
        self.assertIn(f"Python {sys.version_info[0]}.{sys.version_info[1]}", said)

    def test_every_way_an_answer_can_fail_is_one_line_saying_why(self):
        import socket

        for failure, cause in (
                (socket.timeout("timed out"),
                 "no answer within 90s (SSHPROXY_TIMEOUT)"),
                (("cut", socket.timeout("timed out")), "no answer within 90s"),
                (http.client.RemoteDisconnected(
                    "Remote end closed connection without response"),
                 "Remote end closed connection without response"),
                (("cut", http.client.IncompleteRead(b"-----BEGIN", 900)),
                 "IncompleteRead(10 bytes read, 900 more expected)"),
                (ConnectionResetError(104, "Connection reset by peer"),
                 "Connection reset by peer"),
                (http.client.BadStatusLine("HTTP/9.9 what"), "HTTP/9.9 what")):
            with self.subTest(failure=failure):
                self.answer(failure)
                said = self.refusal()
                self.assertIn("sshproxy did not answer completely", said)
                self.assertIn(cause, said)
                self.assertNotIn("\n", said)

    def test_an_error_status_whose_body_cannot_be_read_still_names_the_status(self):
        class Unreadable(io.BytesIO):
            def read(self, *args):
                raise http.client.IncompleteRead(b"", 10)

        self.answer(urllib.error.HTTPError(
            self.nersc.SSHPROXY_URL, 502, "Bad Gateway", {}, Unreadable()))
        self.assertIn("sshproxy returned HTTP 502", self.refusal())

    def test_a_refusal_reads_as_the_credentials_failing(self):
        self.answer(self.http_error(401, REJECTED))
        said = self.refusal()
        self.assertIn("sshproxy rejected the credentials", said)
        self.assertTrue(is_rejection(said))

    def test_a_seed_that_is_not_base32_is_refused_before_any_request(self):
        with _patched(self.backend, "read_credential",
                      lambda field: "not base32!" if field.filename == "key.txt"
                      else "password"):
            said = self.refusal()
        self.assertIn("TOTP seed is not base32", said)
        self.assertIn("cluster --nersc config credentials", said)
        self.assertTrue(is_rejection(said))
        self.assertEqual(self.requests, [])


class TestOneTotpWindowPerFetch(_SshproxyHarness):
    """sshproxy refuses a code it has seen, and a refusal counts towards a lockout."""

    def claimed(self):
        return int((self.root / "state" / "nersc" / "sshproxy.window").read_text())

    def test_a_fetch_right_after_a_failed_one_waits_for_the_next_window(self):
        self.answer(self.http_error(500), self.http_error(500))
        self.refusal()
        self.assertEqual(self.claimed(), 100)
        self.refusal()
        self.assertEqual(self.claimed(), 101)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.clock.slept, [25.5])
        first, second = self.codes()
        self.assertNotEqual(first, second)

    def test_a_window_about_to_end_is_not_used(self):
        self.clock.now = 3000.0 + 28
        self.answer(self.http_error(500))
        self.refusal()
        self.assertEqual(self.clock.slept, [2.5])
        self.assertEqual(self.claimed(), 101)


@needs_ssh_keygen
class TestCertificateInstall(_SshproxyHarness):
    """The key and its certificate arrive together, private, and only whole."""

    def files(self):
        return sorted(path.name for path in self.backend.key_path.parent.iterdir())

    def test_a_fetch_installs_the_pair_and_its_public_half(self):
        key, cert, pub, body = self.pair()
        self.answer(body)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertTrue(self.backend.ensure_credential())
        self.assertEqual(out.getvalue(), "", "$(cluster ssh-command) reads stdout")
        self.assertIn("obtaining NERSC certificate", err.getvalue())
        self.assertIn("certificate installed at", err.getvalue())
        pub_path = self.backend.key_path.with_name("nersc.pub")
        self.assertEqual(self.backend.key_path.read_text(), key)
        self.assertEqual(self.backend.cert_path.read_text(), cert + "\n")
        self.assertEqual(pub_path.read_text().split()[:2], pub.split()[:2])
        for path in (self.backend.key_path, self.backend.cert_path, pub_path):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600, path.name)
        self.assertEqual(self.files(), ["nersc", "nersc-cert.pub", "nersc.pub"])
        self.assertGreater(self.backend.cert_seconds_left(), 80000)

    def test_a_public_half_of_an_older_key_is_not_left_beside_a_new_one(self):
        pub_path = self.backend.key_path.with_name("nersc.pub")
        pub_path.parent.mkdir(parents=True)
        pub_path.write_text("ssh-ed25519 AAAAold old\n")
        self.answer(b"-----BEGIN OPENSSH PRIVATE KEY-----\nnot a key\n"
                    b"-----END OPENSSH PRIVATE KEY-----\n"
                    b"ssh-ed25519-cert-v01@openssh.com AAAAcert user\n")
        with contextlib.redirect_stderr(io.StringIO()):
            self.backend.fetch_certificate(quiet=True)
        self.assertFalse(pub_path.exists())
        self.assertEqual(self.files(), ["nersc", "nersc-cert.pub"])

    def test_an_install_cut_short_leaves_no_certificate_rather_than_the_wrong_one(self):
        old_key, old_cert, _pub, _body = self.pair()
        self.backend.key_path.parent.mkdir(parents=True)
        self.backend.key_path.write_text(old_key)
        self.backend.cert_path.write_text(old_cert + "\n")
        *_, body = self.pair()
        self.answer(body)
        real_replace = os.replace

        def replace(source, destination):
            if Path(destination) == self.backend.cert_path:
                raise OSError(28, "No space left on device")
            real_replace(source, destination)

        with _patched(self.nersc.os, "replace", replace):
            said = self.refusal()
        self.assertIn("cannot install the NERSC certificate", said)
        self.assertIn("No space left on device", said)
        self.assertFalse(self.backend.cert_path.exists())
        self.assertEqual(self.backend._certificate()[0], "missing",
                         "the next use fetches a new pair")
        self.assertEqual(self.files(), ["nersc", "nersc.pub"], "no temp file is left")


@needs_ssh_keygen
class TestConcurrentFetches(_SshproxyHarness):
    """Two processes that need a certificate at once spend one TOTP code."""

    def race(self, force):
        """Run two fetches, the second starting while the first's request is out."""
        import threading

        *_, body = self.pair()
        first_sent, second_waiting = threading.Event(), threading.Event()

        def slow_answer():
            first_sent.set()
            second_waiting.wait(30)
            return body

        self.answer(slow_answer, body)
        other = load("nersc")
        other.key_path = self.backend.key_path
        other.ensure_known_hosts = lambda: False
        said = []

        def info(message):
            said.append(message)
            if message.startswith("waiting for"):
                second_waiting.set()

        results = {}

        def run(name, backend):
            results[name] = backend.ensure_credential(force=force)

        with _patched(self.nersc.ui, "info", info), \
                contextlib.redirect_stderr(io.StringIO()):
            first = threading.Thread(target=run, args=("first", self.backend))
            first.start()
            self.assertTrue(first_sent.wait(30))
            second = threading.Thread(target=run, args=("second", other))
            second.start()
            first.join(60)
            second.join(60)
        self.assertTrue(second_waiting.is_set(), said)
        self.assertEqual(results, {"first": True, "second": True})
        return said

    def test_the_second_uses_the_certificate_the_first_fetched(self):
        said = self.race(force=False)
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(any("another process installed a NERSC certificate "
                            "meanwhile" in line for line in said), said)

    def test_so_does_a_second_forced_renewal(self):
        old_key, old_cert, _pub, _body = self.pair()
        self.backend.key_path.parent.mkdir(parents=True)
        self.backend.key_path.write_text(old_key)
        self.backend.key_path.chmod(0o600)
        self.backend.cert_path.write_text(old_cert + "\n")
        self.race(force=True)
        self.assertEqual(len(self.requests), 1)


class TestRenewalFailure(_SshproxyHarness):
    """A renewal that fails while the certificate still works is not the end of it."""

    DETAIL = {"expiring": "certificate expires in 42m",
              "missing": "no certificate yet",
              "ok": "certificate valid for 20h00m"}
    UNREACHABLE = ("cluster: cannot reach https://sshproxy.nersc.gov: "
                   "[Errno 101] Network is unreachable")

    def ensure(self, state, failure, force=False):
        self.by_hand = []

        def fetch(quiet=False, seen=None, by_hand=None):
            self.by_hand.append(by_hand)
            raise SystemExit(failure)

        err = io.StringIO()
        with _patched(self.backend, "_certificate",
                      lambda: (state, self.DETAIL[state])), \
                _patched(self.backend, "fetch_certificate", fetch), \
                contextlib.redirect_stderr(err):
            ok = self.backend.ensure_credential(force=force)
        return ok, err.getvalue()

    def test_a_failed_or_refused_renewal_of_a_working_certificate_carries_on(self):
        # Refused too: a connection presenting the certificate risks nothing
        # on the password; and nobody asked for the renewal, so it is made as
        # an unattended one is, and a refusal on record holds it.
        for failure in (self.UNREACHABLE, "cluster: sshproxy rejected the "
                                          "credentials: Authentication failed"):
            with self.subTest(failure=failure):
                ok, said = self.ensure("expiring", failure)
                self.assertTrue(ok)
                self.assertIn("could not renew the NERSC certificate: "
                              + failure.split(": ")[1], said)
                self.assertIn("carrying on with the current one "
                              "(certificate expires in 42m)", said)
                self.assertEqual(self.by_hand, [False])

    def test_a_renewal_that_was_asked_for_stops(self):
        for state in ("expiring", "ok"):
            with self.subTest(state=state), self.assertRaises(SystemExit):
                self.ensure(state, self.UNREACHABLE, force=True)

    def test_without_a_working_certificate_a_failed_fetch_stops(self):
        with self.assertRaises(SystemExit):
            self.ensure("missing", self.UNREACHABLE)
        self.assertEqual(self.by_hand, [None], "as the command is, by hand or not")


class TestDirectReadsOnAPasswordBackend(unittest.TestCase):
    """A pty has one stream, and the marker is what separates payload from chatter."""

    def test_the_transcript_is_stdout_as_well_as_stderr(self):
        """A marker-framed read over a fresh FASRC connection finds its
        payload on stdout: `clean`'s direct-node sweep and `strays check`
        depend on it for the nodes that have no login on them."""
        backend = load("fasrc")
        self.assertTrue(backend.interactive_auth)
        with _patched(type(backend), "_run_interactive",
                      lambda self, argv, transcript=None, **_options:
                      (transcript.append("Password: __cluster_ls__\r\n"
                                         "main\t1\t0\tmain\t\t\r\n"), 0)[1]):
            proc = backend.run_ssh(["ssh", "node", "true"])
        self.assertEqual(proc.stdout, proc.stderr)
        self.assertIn("__cluster_ls__", proc.stdout)
        self.assertNotIn("\r", proc.stdout, "pty CRLF must be normalised")

    def test_a_direct_catalogue_parses_through_the_pty_path(self):
        from clustertool.sshmux import Logins
        from clustertool.tmuxlayer import Tmux

        tmux = Tmux(Logins(load("fasrc")))
        tmux._direct_run = lambda node, snippet, timeout=120: \
            subprocess.CompletedProcess(
                [], 0,
                "Password: \n__cluster_ls__\nmain\t1\t0\tmain\t\t\n",
                "chatter")
        complete, rows, why = tmux.list_sessions_direct_explained("node")
        self.assertTrue(complete)
        self.assertEqual([r.name for r in rows], ["main"])
        self.assertEqual(why, "")

    def test_the_reason_distinguishes_the_three_refusals(self):
        from clustertool.tmuxlayer import explain_remote_failure

        for rc, said, reason in (
                (255, "Permission denied (keyboard-interactive).", "credential was refused"),
                (255, "Connection closed by 140.247.139.195 port 22", "closed the connection"),
                (255, "ssh: connect to host x: No route to host", "not routable"),
                # The boslogin08 shape: authenticated, then no session.
                (254, "some banner text", "refused to start a session"),
                (254, "some banner text", "already running there is unaffected")):
            self.assertIn(reason, explain_remote_failure(
                subprocess.CompletedProcess([], rc, "", said)))


class TestOneBackendMachine(_IsolatedMachine):
    """A machine enrolled on one cluster must not need the other one set up."""

    def test_whole_fleet_commands_cover_only_the_enrolled_backend(self):
        self.enrol("fasrc")
        self.assertEqual(Context().scope(), ["fasrc"])
        for argv in (["ls"], ["status"], ["close", "--all"]):
            with self.subTest(argv=argv):
                rc, _out, err = self.run_cli(*argv)
                self.assertEqual(rc, 0, err)
                self.assertNotIn("username unknown", err)

    def test_a_backend_with_logins_but_no_username_is_skipped_with_the_fix(self):
        self.enrol("fasrc")
        self.record_login("nersc", "gpu")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ctx = Context()
            self.assertEqual(ctx.scope(), ["fasrc"])
            self.assertEqual(ctx.scope(), ["fasrc"])
        self.assertEqual(err.getvalue().count("skipping nersc"), 1,
                         "said once, however many times the scope is asked")
        self.assertIn("cluster --nersc config credentials", err.getvalue())

    def test_the_missing_username_error_names_the_command_that_fixes_it(self):
        from clustertool.backends import BackendUnavailable, load

        for backend in ("fasrc", "nersc"):
            with self.subTest(backend=backend):
                with self.assertRaises(BackendUnavailable) as caught:
                    load(backend)
                self.assertIn(f"cluster --{backend} config credentials",
                              str(caught.exception.code))

    def test_backends_answers_on_a_machine_with_nothing_set_up(self):
        rc, out, err = self.run_cli("backends")
        self.assertEqual(rc, 0, err)
        self.assertIn("cluster --fasrc config credentials", out)
        self.assertIn("cluster --nersc config credentials", out)
        rc, out, err = self.run_cli("--nersc", "backends")
        self.assertEqual(rc, 0, err)
        self.assertFalse(self.config.STATE_ROOT.exists(),
                         "listing backends must not need or make any state")

    def test_backends_shows_a_refused_credential_as_refused(self):
        self.enrol("fasrc")
        rc, out, _err = self.run_cli("backends")
        self.assertNotIn("refused", out)
        backend = backends.load("fasrc")
        Refusals(backend).refused("Permission denied (keyboard-interactive)")
        rc, out, err = self.run_cli("backends")
        self.assertEqual(rc, 0, err)
        row = next(line for line in out.splitlines() if line.startswith("fasrc"))
        self.assertIn("refused", row.split())
        self.assertIn("trying once more at", row)
        (self.config.CRED_ROOT / "fasrc" / "pass").write_text("changed\n")
        rc, out, _err = self.run_cli("backends")
        self.assertNotIn("refused at", out, "a changed credential is not the one refused")

    def test_the_builtin_default_gives_way_to_the_only_enrolled_backend(self):
        self.enrol("nersc")
        self.assertEqual(backends.default_name(), "nersc")
        rc, _out, err = self.run_cli("ls")
        self.assertEqual(rc, 0, err)
        # An instruction is followed, and fails loudly if it cannot be.
        os.environ["CLUSTER_BACKEND"] = "fasrc"
        self.assertEqual(backends.default_name(), "fasrc")
        rc, _out, err = self.run_cli("ls")
        self.assertEqual(rc, 1)
        self.assertIn("cluster --fasrc config credentials", err)

    def test_backend_names_come_from_the_registry(self):
        with self.assertRaisesRegex(ValueError, "expected one of: fasrc, nersc"):
            self.config.parse_value("BACKEND", "slurmland")
        self.assertEqual(self.config.parse_value("BACKEND", "Perlmutter"), "nersc")

        class Other(backends.Backend):
            name, label = "other", "Some Other Cluster"

        with patch.dict(backends.BACKENDS, {"other": Other}):
            self.assertEqual(self.config.parse_value("BACKEND", "other"), "other")
            os.environ["CLUSTER_BACKEND"] = "other"   # put back by the fixture
            self.assertEqual(self.config.global_value("BACKEND"), "other")

    def test_an_old_python_is_told_so_in_one_line(self):
        entry = REPO_ROOT / "bin" / "cluster"
        proc = subprocess.run(
            [sys.executable, "-c",
             "import runpy, sys; sys.version_info = (3, 7, 9); "
             f"runpy.run_path({str(entry)!r}, run_name='__main__')"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stderr.count("\n"), 1, proc.stderr)
        self.assertIn("needs Python 3.8 or newer", proc.stderr)


class TestBackendNamesAreReserved(_IsolatedMachine):
    """Every backend alias is a word taken from the command line."""

    def test_only_backend_names_and_their_aliases_are_reserved(self):
        self.assertEqual(set(backends.ALIASES), {"fas", "perlmutter"})
        for word in ("work", "gpu", "main"):
            with self.subTest(word=word):
                self.assertIsNone(backends.as_backend(word),
                                  "a login name is not a backend")

    def test_an_option_another_tool_owns_is_not_stolen(self):
        from clustertool.cli import strip_backend_flag

        argv = ["transfer", "main:~/x", "./x", "--rc"]
        self.assertEqual(strip_backend_flag(argv), (None, argv))

    def test_a_short_login_name_addresses_the_login(self):
        from clustertool import registry

        self.assertEqual(crossxfer.split_endpoint("gpu:/x"), (None, "gpu:/x"))
        with patch.object(registry, "find", lambda name: "nersc" if name == "gpu" else None):
            self.assertEqual(crossxfer.split_endpoint("gpu:/x"), ("nersc", "/x"))

    def test_a_new_login_may_not_be_named_after_a_backend(self):
        self.enrol("fasrc")
        ctx = Context()
        for name in ("fas", "nersc", "Perlmutter"):
            with self.subTest(name=name):
                message = _refusal(lambda: ctx.login(name))
                self.assertIn("names a backend", message)
        self.assertEqual(ctx.login("gpu"), "gpu")
        rc, _out, err = self.run_cli("rename", "main", "fasrc")
        self.assertEqual(rc, 1)
        self.assertIn("'fasrc' cannot be a login name", err)

    def test_a_rename_is_held_to_the_rules_for_a_new_name(self):
        self.enrol("fasrc")
        self.record_login("fasrc", "gpu")
        for name, why in (("g" * 40, "the limit is"), ("GPU", "differs from it only in case")):
            rc, _out, err = self.run_cli("rename", "gpu", name)
            self.assertEqual(rc, 1)
            self.assertIn(why, err)
        self.assertTrue((self.config.STATE_ROOT / "fasrc" / "gpu.json").exists(),
                        "a refused rename moves nothing")


class TestNodesSettingIsTheLoginNodeList(_IsolatedMachine):
    """NODES replaces the login nodes everywhere the tool lists them."""

    ENV = _IsolatedMachine.ENV + ("CLUSTER_NODES", "CLUSTER_FASRC_NODES",
                                  "CLUSTER_NERSC_NODES")

    def test_every_question_about_nodes_gets_the_same_answer(self):
        self.enrol("fasrc")
        os.environ["CLUSTER_FASRC_NODES"] = "holylogin05 boslogin07.rc.fas.harvard.edu"
        backend = load("fasrc")
        wanted = ["holylogin05.rc.fas.harvard.edu", "boslogin07.rc.fas.harvard.edu"]
        self.assertEqual(backend.pool_nodes(), wanted)
        self.assertEqual(backend.transfer_nodes(), wanted)
        self.assertEqual(backend.mount_nodes(), wanted)
        self.assertIn("list set by NODES", backend.node_classes[0].note)
        self.assertEqual(len(type(backend).node_classes[0].members()), 7,
                         "the override belongs to one instance, not the class")
        rc, out, err = self.run_cli("--fasrc", "nodes")
        self.assertEqual(rc, 0, err)
        self.assertIn("holylogin05, boslogin07", out)
        self.assertNotIn("holylogin06", out)
        self.assertIn("list set by NODES", out)

    def test_only_the_login_class_is_replaced(self):
        self.enrol("nersc")
        os.environ["CLUSTER_NODES"] = "login03 login04"
        backend = load("nersc")
        self.assertEqual(backend.pool_nodes(), ["login03.chn.perlmutter.nersc.gov",
                                                "login04.chn.perlmutter.nersc.gov"])
        self.assertEqual(backend.mount_nodes()[0], "dtn01.nersc.gov")
        self.assertFalse(backend.nodes.routable("login04"),
                         "still reached through the pool address")
        del os.environ["CLUSTER_NODES"]
        self.assertEqual(len(load("nersc").pool_nodes()), 40)

    def nodes_completed(self, backend_flag, settings="", **env):
        script = COMPLETION_SCRIPT
        config_home = self.root / "xdg"
        (config_home / "cluster").mkdir(parents=True, exist_ok=True)
        (config_home / "cluster" / "settings.ini").write_text(settings)
        environ = {k: v for k, v in os.environ.items() if not k.startswith("CLUSTER_")}
        environ.update(env, XDG_CONFIG_HOME=str(config_home), HOME=str(self.root))
        proc = subprocess.run(
            ["bash", "--noprofile", "--norc", "-c",
             f"source '{script}'; COMP_WORDS=(cluster {backend_flag} login); "
             "_cluster_nodes; echo \"default=$(_cluster_default_login)\""],
            env=environ, text=True, stdout=subprocess.PIPE, check=True)
        lines = proc.stdout.split()
        return set(lines[:-1]), lines[-1]

    def test_the_completion_reads_the_same_setting(self):
        nodes, default = self.nodes_completed("--nersc")
        self.assertEqual((len(nodes), default), (44, "default=main"))
        settings = ("[global]\nNODES = a b\n[nersc]\n"
                    "NODES = login03 login04.chn.perlmutter.nersc.gov\n"
                    "DEFAULT_LOGIN = gpu\n")
        for flag, env, wanted in (
                ("--fasrc", {}, ({"a", "b"}, "default=main")),
                ("--nersc", {}, ({"login03", "login04", "dtn01", "dtn02", "dtn03",
                                  "dtn04"}, "default=gpu")),
                ("--fasrc", {"CLUSTER_NODES": "x"}, ({"x"}, "default=main")),
                ("--fasrc", {"CLUSTER_NODES": "x", "CLUSTER_FASRC_NODES": "y"},
                 ({"y"}, "default=main"))):
            with self.subTest(flag=flag, env=env):
                self.assertEqual(self.nodes_completed(flag, settings, **env), wanted)


class TestTotpSeedsAreCheckedAsTyped(unittest.TestCase):
    """What `config credentials` and `init` accept as a TOTP seed."""

    SEED = "JBSWY3DPEHPK3PXP"

    def setUp(self):
        from clustertool.backends.base import totp_seed

        self.totp_seed = totp_seed

    def refusal(self, text):
        with self.assertRaises(ValueError) as caught:
            self.totp_seed(text)
        return str(caught.exception)

    def test_a_seed_is_kept_as_it_was_shown(self):
        for typed in (self.SEED, "jbsw y3dp-ehpk 3pxp", " MFRGG=== "):
            with self.subTest(typed=typed):
                self.assertEqual(self.totp_seed(typed), typed.strip())

    def test_the_link_in_a_qr_code_gives_its_secret(self):
        link = ("otpauth://totp/Site:someone?secret=" + self.SEED
                + "&issuer=Site&algorithm=SHA1&digits=6&period=30")
        self.assertEqual(self.totp_seed(link), self.SEED)
        self.assertEqual(self.totp_seed(f"otpauth://totp/x?SECRET={self.SEED}"),
                         self.SEED)

    def test_a_link_for_codes_the_tool_does_not_make_is_refused(self):
        base = f"otpauth://totp/x?secret={self.SEED}"
        for extra, word in (("&algorithm=SHA256", "algorithm"),
                            ("&digits=8", "digits"), ("&period=60", "period")):
            with self.subTest(extra=extra):
                self.assertIn(word, self.refusal(base + extra))
        self.assertIn("time-based", self.refusal(f"otpauth://hotp/x?secret={self.SEED}"))
        self.assertIn("no secret", self.refusal("otpauth://totp/x?issuer=Site"))
        self.assertIn("export", self.refusal("otpauth-migration://offline?data=AAAA"))

    def test_what_is_not_a_seed_says_what_a_seed_looks_like(self):
        self.assertIn("6-digit code", self.refusal("234567"))
        for typed in ("not a seed!", "GEZDGNBV1"):
            with self.subTest(typed=typed):
                self.assertIn("not a base32 TOTP seed", self.refusal(typed))


class TestCredentialFields(_IsolatedMachine):
    """One layout: credentials/<backend>/{user,pass,key.txt}."""

    def write(self, backend, **files):
        directory = self.config.CRED_ROOT / backend
        directory.mkdir(parents=True, exist_ok=True)
        for filename, text in files.items():
            path = directory / filename.replace("_", ".")
            path.write_text(text + "\n")
            path.chmod(0o600)

    def test_the_files_are_the_declared_fields(self):
        from clustertool.backends import base

        self.assertEqual(base.CRED_FILES, ("user", "pass", "key.txt"))
        self.assertEqual(base.CRED_SECRETS, ("pass", "key.txt"))
        for name, cls in BACKENDS.items():
            with self.subTest(backend=name):
                self.assertTrue(cls.enroll_hint, "a site says where its answers come from")
                for key in cls.enroll_settings:
                    self.assertEqual(self.config.owners(key), [name])

    def test_the_username_is_the_user_file_or_the_backend_override(self):
        fasrc = BACKENDS["fasrc"]
        self.write("fasrc", **{"user_txt": "old", "login": "old", "id": "old"})
        os.environ["CLUSTER_USER"] = "everyone"
        self.assertEqual(fasrc.local_username(Settings("fasrc")), "",
                         "only the user file and CLUSTER_FASRC_USER name you")
        self.write("fasrc", user="someone")
        self.assertEqual(fasrc.local_username(Settings("fasrc")), "someone")
        os.environ["CLUSTER_FASRC_USER"] = "other"
        self.assertEqual(fasrc.local_username(Settings("fasrc")), "other")
        self.assertEqual(fasrc.missing_credentials(Settings("fasrc")),
                         ["password", "TOTP seed"])

    def test_a_fresh_nersc_machine_is_ready_before_its_first_certificate(self):
        self.write("nersc", user="someone", key_txt="JBSWY3DPEHPK3PXP")
        key = self.root / "ssh" / "nersc"
        self.config.write_value("KEY", str(key), backend="nersc")
        backend = load("nersc")
        state, detail = backend.credential_state()
        self.assertEqual(state, "missing")
        self.assertIn("password", detail)
        self.assertIn("cluster --nersc config credentials", detail)

        def no_network(*_args, **_kwargs):
            raise AssertionError("fetched a certificate without the password")

        with _patched(urllib.request, "urlopen", no_network), \
                self.assertRaises(SystemExit) as caught:
            backend.fetch_certificate(quiet=True)
        self.assertIn("cluster --nersc config credentials", str(caught.exception.code))

        self.write("nersc", **{"pass": "secret"})
        state, detail = backend.credential_state()
        self.assertEqual(state, "ready")
        self.assertIn("fetched on first use", detail)
        self.assertIn("cluster --nersc auth", detail)
        rc, out, err = self.run_cli("--nersc", "auth", "--status")
        self.assertEqual(rc, 0, err)
        self.assertIn("ready", out)

    def test_a_password_backend_names_the_command_for_a_missing_secret(self):
        self.write("fasrc", user="someone", key_txt="JBSWY3DPEHPK3PXP")
        state, detail = load("fasrc").credential_state()
        self.assertEqual(state, "missing")
        self.assertIn("cluster --fasrc config credentials", detail)
        self.write("fasrc", **{"pass": "secret"})
        (self.config.CRED_ROOT / "fasrc" / "pass").chmod(0o644)
        state, detail = load("fasrc").credential_state()
        self.assertEqual(state, "missing")
        self.assertIn("chmod 600", detail)



class TestSshproxyRefusalsAreConfirmedOnce(_SshproxyHarness):
    """sshproxy's refusal goes on the backend's shared record, which every
    unattended fetch then waits on (state.Refusals)."""

    def record(self):
        return Refusals(self.backend)

    def test_a_refusal_is_not_asked_again_by_an_unattended_fetch(self):
        self.answer(self.http_error(401, REJECTED), self.http_error(401, REJECTED))
        self.refusal()
        self.assertIn("sshproxy rejected", self.record().current()["detail"])
        said = self.refusal()
        self.assertEqual(len(self.requests), 1, "no second code spent")
        self.assertIn("not asking sshproxy for a certificate", said)
        self.assertIn("trying them once more at", said)
        self.assertIn("cluster --nersc auth", said)
        self.assertTrue(is_rejection(said), "every caller stops on it at once")

    def test_by_hand_it_is_asked_again(self):
        *_, body = self.pair()
        self.answer(self.http_error(401, REJECTED), body)
        self.refusal()
        with _patched(self.backend, "by_hand", True), \
                contextlib.redirect_stderr(io.StringIO()):
            self.backend.fetch_certificate(quiet=True)
        self.assertEqual(len(self.requests), 2)
        self.assertIsNone(self.record().current(), "a success clears the record")

    def test_a_failure_that_is_no_refusal_goes_on_no_record(self):
        self.answer(self.http_error(502))
        self.refusal()
        self.assertIsNone(self.record().current())


if __name__ == "__main__":
    unittest.main(verbosity=2)
