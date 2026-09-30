#!/usr/bin/env python3
"""Profiles and the ssh type: a backend of your own, for any host ssh reaches.

Run: python3 -m unittest tests.test_ssh_backend
(or the whole suite: python3 -m unittest discover -s tests -p 'test_*.py')
"""

from __future__ import annotations

import io
import os
import shutil
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import _IsolatedMachine, _patched  # noqa: E402
from clustertool import backends, cli, sshmux  # noqa: E402
from clustertool.config import Settings  # noqa: E402


class _Profiles(_IsolatedMachine):
    def settings(self, text):
        # Replaced rather than rewritten, as the tool writes it: a new inode,
        # whatever the clock's resolution, for the signature parses are
        # cached by.
        fresh = self.config.SETTINGS_FILE.with_name("settings.new")
        fresh.write_text(text)
        os.replace(fresh, self.config.SETTINGS_FILE)

    def lab(self, host="lab-login", extra=""):
        self.settings(f"[lab]\nTYPE = ssh\nHOST = {host}\n{extra}")
        return backends.load("lab")


class TestProfiles(_Profiles):
    def test_a_profile_is_a_section_with_a_type_named_with_backend_only(self):
        self.lab()
        self.assertIn("lab", backends.BACKENDS)
        self.assertEqual(self.config.parse_value("BACKEND", "lab"), "lab")
        os.environ["CLUSTER_BACKEND"] = "lab"      # put back by the fixture
        self.assertEqual(backends.default_name(), "lab")
        # Only a built-in backend takes words from every command line.
        self.assertEqual(cli.strip_backend_flag(["ls", "--lab"]), (None, ["ls", "--lab"]))
        self.assertEqual(cli.split_backend_prefix("lab:ls"), (None, "lab:ls"))
        self.assertEqual(cli.split_backend_prefix("fas:ls"), ("fasrc", "ls"))
        self.assertEqual(backends.flag("lab"), "--backend lab")
        backends.refuse_backend_name("lab")        # a login may be called lab

    def test_a_section_that_cannot_be_a_profile_is_left_out_saying_why(self):
        err = io.StringIO()
        with _patched(sys, "stderr", err):
            self.settings("[mount]\nTYPE = ssh\n[fas]\nTYPE = ssh\n"
                          "[odd]\nTYPE = telnet\n[fasrc]\nTYPE = fasrc\n")
            names = sorted(backends.BACKENDS)
        self.assertEqual(names, ["fasrc", "nersc"])
        for said in ("CLUSTER_MOUNT_NODES", "names a built-in", "unknown TYPE"):
            self.assertIn(said, err.getvalue())

    def test_a_type_can_be_a_file_of_your_own(self):
        path = self.root / "mytype.py"
        path.write_text("from clustertool.backends.ssh import SshBackend\n"
                        "class Mine(SshBackend):\n    label = 'Mine'\n"
                        "BACKEND = Mine\n")
        path.chmod(0o644)
        self.settings(f"[mine]\nTYPE = {path}\nHOST = box\n")
        backend = backends.load("mine")
        self.assertEqual((backend.name, backend.type_name, type(backend).__mro__[1].__name__),
                         ("mine", "mytype", "Mine"))
        path.chmod(0o666)
        with self.assertRaisesRegex(ValueError, "changed by others"):
            backends._type(str(path))

    def test_add_writes_the_section_and_remove_refuses_a_backend_in_use(self):
        rc, _out, err = self.run_cli("backends", "add", "lab", "alice@lab-login")
        self.assertEqual(rc, 0, err)
        self.assertIn("[lab]\nTYPE = ssh\nHOST = alice@lab-login\n",
                      self.config.SETTINGS_FILE.read_text())
        rc, _out, err = self.run_cli("backends", "add", "lab2", "--", "-oProxyCommand=x")
        self.assertEqual(rc, 1)
        self.assertIn("cannot begin with '-'", err)
        self.record_login("lab", "work")
        rc, _out, err = self.run_cli("backends", "remove", "lab")
        self.assertEqual(rc, 1)
        self.assertIn("close them first: cluster --backend lab close --all", err)
        (self.config.STATE_ROOT / "lab" / "work.json").unlink()
        rc, _out, err = self.run_cli("backends", "remove", "lab")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("lab", self.config.SETTINGS_FILE.read_text())


class TestSshType(_Profiles):
    def test_ssh_is_left_its_own_configuration_and_a_person_their_prompts(self):
        backend = self.lab()
        argv = backend.ssh_argv(sock="/tmp/s", master=True)
        self.assertNotIn("-F", argv)
        self.assertFalse(any(word.startswith(("User=", "StrictHostKeyChecking="))
                             for word in argv))
        self.assertEqual(argv[-1], "lab-login")
        self.assertIn("BatchMode=yes", argv)       # nobody is there to answer
        backend.by_hand = True
        self.assertNotIn("BatchMode=yes", backend.ssh_argv())
        backend = self.lab(host="alice@lab-login")
        self.assertEqual(backend.target(), "alice@lab-login")
        self.assertIn("User=alice", backend.ssh_argv())

    def test_an_alias_is_dialled_not_the_name_its_node_answers_to(self):
        backend = self.lab()
        self.assertEqual(backend.host_for("labbox.cs.example.org"), "lab-login")
        # What the connection reached is only known once there: a command
        # for a node over a connection of its own checks first.
        remote = backend.ssh_argv(node="labbox.cs.example.org", remote="tmux ls")[-1]
        self.assertIn('!= labbox ]; then', remote)
        self.assertTrue(remote.endswith("exit 255; fi; tmux ls"))
        backend = self.lab(extra="NODE_HOSTS = labbox=lab-box2 other=lab-o\n")
        self.assertEqual(backend.host_for("labbox.cs.example.org"), "lab-box2")
        self.assertEqual(backend.pool_nodes(), ["labbox", "other"])

    def test_a_reconnect_landing_elsewhere_is_dropped_saying_what_to_set(self):
        backend = self.lab()
        logins = sshmux.Logins(backend, state=SimpleNamespace(
            known_logins=lambda: ["work"], pin_read=lambda name: "labbox.example.org",
            socket=lambda name: self.root / "s", master_log_path=lambda n: self.root / "l"))
        closed = []
        for attr, value in (
                ("_cleanup_stale", lambda name: None), ("connection_count", lambda: 0),
                ("open_master", lambda *a, **k: sshmux.MasterOpen(True, "")),
                ("refresh_meta", lambda name: "otherbox.example.org"),
                ("close", lambda name, **kw: closed.append(kw))):
            setattr(logins, attr, value)
        err = io.StringIO()
        with _patched(sys, "stderr", err), self.assertRaises(SystemExit):
            logins._create("work")
        self.assertEqual(closed, [{"keep_tmux": True, "keep_pin": True, "quiet": True}])
        self.assertIn("pinned to labbox but landed on otherbox", err.getvalue())
        self.assertIn("NODE_HOSTS 'labbox=DESTINATION'", err.getvalue())

    @unittest.skipUnless(shutil.which("ssh"), "needs OpenSSH's ssh -G")
    def test_a_jump_host_on_another_port_is_what_the_network_must_reach(self):
        cfg = self.root / "ssh_config"
        cfg.write_text("Host lab\n  ProxyJump gate\nHost gate\n  HostName gate.example.org\n"
                       "  Port 2222\nHost viacmd\n  ProxyCommand nc %h %p\n")
        backend = self.lab(host="lab", extra=f"SSH_CONFIG = {cfg}\n"
                                             "NODE_HOSTS = n1=viacmd\n")
        self.assertEqual(backend.ssh_argv()[:3], ["ssh", "-F", str(cfg)])
        self.assertEqual(backend.reach_host(), ("gate.example.org", 2222))
        self.assertIsNone(backend.node_probe_host("lab"))   # only a connection can tell
        self.assertIsNone(backend.node_probe_host("n1"))
        self.assertTrue(backend.node_reachable("n1"))

    def test_a_refusal_holds_until_someone_connects_or_the_setup_changes(self):
        backend = self.lab()
        self.assertTrue(backend.refusal_holds_connections())
        before = backend.credential_marks()
        ssh_config = Path.home() / ".ssh" / "config"
        ssh_config.parent.mkdir(exist_ok=True)
        ssh_config.write_text("Host lab-login\n  IdentityFile ~/.ssh/lab\n")
        self.addCleanup(ssh_config.unlink)
        edited = backend.credential_marks()
        self.assertNotEqual(edited, before)
        self.assertNotEqual(self.lab(host="lab2").credential_marks(), edited)

    def test_nothing_is_taken_to_be_shared_between_nodes(self):
        self.lab()
        for key in ("AUTO_MOUNT", "ONE_MOUNT_PER_BACKEND", "MOUNT_FAILOVER", "LINGER"):
            with self.subTest(key=key):
                self.assertEqual(Settings("lab").flag(key), key == "LINGER")
                self.assertTrue(Settings("fasrc").flag(key))
        self.assertFalse(backends.load("lab").reaps_on_logout)
        self.settings("[global]\nAUTO_MOUNT = 1\n[lab]\nTYPE = ssh\nHOST = x\n"
                      "REAPS_ON_LOGOUT = 1\n")
        self.assertTrue(Settings("lab").flag("AUTO_MOUNT"))
        self.assertTrue(backends.load("lab").reaps_on_logout)

    def test_without_a_host_it_says_how_to_set_one(self):
        self.settings("[lab]\nTYPE = ssh\n")
        self.assertFalse(backends.configured("lab"))
        with self.assertRaises(backends.BackendUnavailable) as caught:
            backends.load("lab")
        self.assertIn("cluster --backend lab config set HOST", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
