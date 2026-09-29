#!/usr/bin/env python3
"""The NERSC bridge, and installing and syncing the companion.

Run: python3 -m unittest tests.test_bridge
(or the whole suite: python3 -m unittest discover -s tests -p 'test_*.py')
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# For `support`, which puts this checkout on sys.path and sets up the sandbox
# every test runs in, so it is imported before anything from clustertool.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import REPO_ROOT, _patched, temp_state  # noqa: E402
from clustertool import bridge, companion, config, ui  # noqa: E402
from clustertool.config import Settings  # noqa: E402


def load_companion(config_text=None):
    """remote/nersc as a module, reading *config_text* as its config file."""
    import importlib.machinery
    import importlib.util

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config"
        if config_text is not None:
            path.write_text(config_text)
        with mock.patch.dict(os.environ, {"NERSC_CONFIG": str(path)}):
            loader = importlib.machinery.SourceFileLoader(
                "nersc_companion", str(companion.SOURCE))
            spec = importlib.util.spec_from_loader("nersc_companion", loader)
            module = importlib.util.module_from_spec(spec)
            with contextlib.redirect_stderr(io.StringIO()):
                spec.loader.exec_module(module)
    return module


class Source:
    """A stand-in for the nersc backend, as the bridge uses it."""

    user = "user"
    key_path = Path("/tmp/nersc-key")
    cert_path = Path("/tmp/nersc-key-cert.pub")
    CERT_AUTHORITY = "@cert-authority *.nersc.gov ssh-rsa AAAA"
    lends_credential = True

    def __init__(self, left=80000, fetch=None):
        self.left = left
        #: What a fetch does to this source; None: no fetch may happen.
        self.fetch = fetch
        self.fetches = 0
        self.settings = SimpleNamespace(
            int=lambda key: {"BRIDGE_VERIFY_TIMEOUT": 300}.get(key, 72000),
            str=lambda key: "")

    def cert_seconds_left(self):
        return self.left

    def cert_mark(self):
        return self.left

    def fetch_certificate(self, quiet=False, min_left=None, seen=None):
        if self.fetch is None:
            raise AssertionError("no certificate fetch expected")
        self.fetches += 1
        self.fetch(self)


def hub_context(run_remote, backend="fasrc", drives=False):
    return SimpleNamespace(
        backend=SimpleNamespace(name=backend, short=lambda node: node,
                                companion_drives=drives),
        login=lambda name=None: name or "work",
        logins=SimpleNamespace(ensure=lambda _name: None,
                               node_of=lambda _name: "login01",
                               run_remote=run_remote),
    )


def companion_context(text, rc=0, adopt=None):
    """A hub login whose companion reads back as *text* (the remote read exits
    *rc*). *adopt* stands in for COMPANION_ADOPT_HUB_EDITS; None means its
    real default."""
    if adopt is None:
        adopt = str(config.DEFAULTS["COMPANION_ADOPT_HUB_EDITS"]) == "1"
    return SimpleNamespace(
        backend=SimpleNamespace(name="fasrc", companion_drives=False),
        login=lambda name=None: name or "work",
        logins=SimpleNamespace(
            ensure=lambda _name: None,
            run_remote=lambda _name, _command, timeout=60:
                subprocess.CompletedProcess([], rc, text, "")),
        settings=SimpleNamespace(
            int=lambda key: {"COMPANION_SYNC_TIMEOUT": 60,
                             "COMPANION_MAX_BYTES": 1048576}[key],
            flag=lambda _key: adopt))


def plan_for(action):
    plan = dict.fromkeys(("local_version", "hub_version", "hash", "detail"), "")
    plan.update({"action": action, "saved": None, "backup": None})
    return plan


class TestBridge(unittest.TestCase):
    def test_min_cert_left_default_matches_cron_cadence(self):
        # 8h cron + 20h threshold keeps the pushed cert always >=16h from expiry.
        self.assertEqual(Settings("nersc").int("BRIDGE_MIN_CERT_LEFT"), 72000)

    def test_remote_paths_are_home_relative_and_the_sources_are_shipped(self):
        for rel in (bridge.REMOTE_KEY, bridge.REMOTE_CERT, bridge.REMOTE_TOOL,
                    bridge.REMOTE_CONFIG, bridge.REMOTE_EXCLUDE):
            self.assertFalse(rel.startswith(("/", "~")), rel)
        for path in (companion.SOURCE, companion.EXCLUDE_SOURCE, companion.LOCAL_ENTRY):
            self.assertTrue(path.is_file(), path)
        self.assertNotEqual(companion.LOCAL_ENTRY, companion.SOURCE)

    def test_the_companion_settings_live_only_on_the_hub(self):
        for key in config.DEFAULTS:
            if key.startswith("BRIDGE_"):
                self.assertIn(key, ("BRIDGE_MIN_CERT_LEFT", "BRIDGE_LOGIN",
                                    "BRIDGE_VERIFY_TIMEOUT"), key)

    RSYNC_CTX = SimpleNamespace(
        state=SimpleNamespace(socket=lambda name: Path("/tmp/cl-fasrc-work.sock")),
        logins=SimpleNamespace(node_of=lambda name: "login01"),
        backend=SimpleNamespace(user="user", host_for=lambda node: node + ".example"),
        settings=SimpleNamespace(int=lambda key: {"TRANSFER_IO_TIMEOUT": 77}[key]),
    )

    def test_rsync_file_ships_to_a_temp_name_over_the_login_master(self):
        seen = []

        def fake_run(argv, **kwargs):
            seen.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, "", "")

        with _patched(bridge.plat, "run", fake_run), \
                _patched(bridge.plat, "rsync_progress_flag", lambda: "--progress"):
            tmp = bridge._rsync_file(self.RSYNC_CTX, "work", Path("/l/nersc"),
                                     bridge.REMOTE_TOOL)
        self.assertEqual(tmp, ".local/bin/.nersc.bridge-tmp")
        ((argv, kwargs),) = seen
        self.assertEqual(argv[:3], ["rsync", "--progress", "-e"])
        self.assertFalse([a for a in argv if a.startswith(("--chmod", "--perms"))],
                         "modes are set on the hub, so any rsync version works")
        self.assertIn("ControlPath=/tmp/cl-fasrc-work.sock", argv[3])
        self.assertEqual(kwargs, {"idle": 77}, "stopped for silence, not timed")
        self.assertEqual(argv[-2:], ["/l/nersc", "user@login01.example:" + tmp])

    def test_rsync_that_fails_or_stalls_says_which(self):
        for rc, stderr, expected in (
                (124, "", "rsync reported no progress for 77s"),
                (12, "rsync: connection unexpectedly closed\n",
                 "connection unexpectedly closed"),
                (12, "", "rsync rc=12")):
            err = io.StringIO()

            def fake_run(argv, **kwargs):
                return subprocess.CompletedProcess(argv, rc, "", stderr)

            with self.subTest(rc=rc, stderr=stderr), \
                    _patched(bridge.plat, "run", fake_run), \
                    contextlib.redirect_stderr(err), self.assertRaises(ui.Die):
                bridge._rsync_file(self.RSYNC_CTX, "work", Path("/l/nersc"),
                                   bridge.REMOTE_TOOL)
            self.assertIn("could not ship nersc to hub 'work'", err.getvalue())
            self.assertIn(expected, err.getvalue())

    def test_moving_a_shipped_file_into_place_sets_its_mode(self):
        """Run the hub-side move for real, in a scratch home."""
        with tempfile.TemporaryDirectory() as home:
            Path(home, ".ssh").mkdir(mode=0o700)
            Path(home, ".local", "bin").mkdir(parents=True)
            for remote_rel, mode in ((bridge.REMOTE_KEY, "600"),
                                     (bridge.REMOTE_TOOL, "755")):
                parent, _, base = remote_rel.rpartition("/")
                tmp = f"{parent}/.{base}.bridge-tmp"
                Path(home, tmp).write_text("payload\n")
                Path(home, tmp).chmod(0o644)
                subprocess.run(["bash", "-c", bridge._move_into_place(tmp, remote_rel, mode)],
                               env=dict(os.environ, HOME=home), check=True)
                with self.subTest(remote_rel=remote_rel):
                    self.assertFalse(Path(home, tmp).exists())
                    self.assertEqual(Path(home, remote_rel).stat().st_mode & 0o777,
                                     int(mode, 8))
                    self.assertEqual(Path(home, remote_rel).read_text(), "payload\n")

    def test_hub_config_is_a_template_of_every_companion_setting(self):
        tool = load_companion()
        keys = [key for key, _example, _meaning in companion.CONFIG_KEYS if key]
        self.assertEqual(sorted(keys), sorted(tool.DEFAULTS))
        for key, example, _meaning in companion.CONFIG_KEYS:
            if key and tool.DEFAULTS[key]:
                self.assertEqual(example, tool.DEFAULTS[key], key)

    def test_hub_config_fills_what_the_bridge_knows_and_comments_the_rest(self):
        text = bridge.hub_config_text(Source())
        tool = load_companion(text)
        self.assertEqual(tool.CFG["user"], "user")
        self.assertEqual(tool.CFG["scratch"], "/pscratch/sd/u/user")
        self.assertEqual(tool.CFG["key"], "~/" + bridge.REMOTE_KEY)
        # Every other setting is present, explained, and not in effect.
        for key in ("mirror_src", "mirror_dest", "return_root", "scratch_link",
                    "hooks", "cfs", "pool", "dtns", "return_queue", "env_parity",
                    "refresh_hint"):
            self.assertRegex(text, r"(?m)^# %s = " % key)
            self.assertEqual(tool.CFG[key], tool.DEFAULTS[key], key)

    def test_install_default_writes_only_a_missing_file(self):
        """Run the remote command for real, in a scratch home."""
        with tempfile.TemporaryDirectory() as home:
            calls = []

            def run_remote(name, command, timeout=60):
                calls.append(command)
                return subprocess.run(["bash", "-c", command], env=dict(os.environ, HOME=home),
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      universal_newlines=True)

            ctx = hub_context(run_remote)
            Path(home, "x").mkdir()
            content = "# a comment with $PSCRATCH and 'quotes'\n\nkey = value\n"
            bridge._install_default(ctx, "work", "x/conf", content)
            self.assertEqual(Path(home, "x", "conf").read_text(), content)
            Path(home, "x", "conf").write_text("mine\n")
            bridge._install_default(ctx, "work", "x/conf", content)
            self.assertEqual(Path(home, "x", "conf").read_text(), "mine\n")
            self.assertEqual(len(calls), 2, "one remote command per file")

    def _install(self, action, *, tool_only, verify=True, which="/usr/bin/rsync",
                 record=None, check_rc=0):
        """Run a push or a companion-only install against fakes.

        The push is recorded at *record* (by default, not at all), and the
        end-to-end check exits *check_rc*. Returns (rc, files shipped, remote
        commands run).
        """
        commands, shipped = [], []
        self.waited, self.said = [], io.StringIO()

        def run_remote(name, command, timeout=60):
            commands.append(command)
            if command.endswith(" run true"):
                self.waited.append(timeout)
                return subprocess.CompletedProcess([], check_rc, "", "")
            return subprocess.CompletedProcess([], 0, "2.1", "")

        ctx = hub_context(run_remote)
        with contextlib.ExitStack() as stack:
            if record is None:
                stack.enter_context(_patched(bridge, "_record", lambda *a, **k: None))
            else:
                stack.enter_context(_patched(bridge, "_state_path", lambda: record))
            stack.enter_context(_patched(bridge, "_source_backend", lambda: Source()))
            stack.enter_context(_patched(bridge.shutil, "which", lambda name: which))
            stack.enter_context(_patched(bridge, "_reconcile_tool",
                                         lambda *a, **k: plan_for(action)))
            stack.enter_context(_patched(
                bridge, "_rsync_file",
                lambda ctx, name, local, rel: shipped.append(rel) or "tmp"))
            stack.enter_context(_patched(companion, "record_installed",
                                         lambda *a, **k: None))
            stack.enter_context(contextlib.redirect_stderr(self.said))
            rc = bridge.install(ctx, "work", tool_only=tool_only, verify=verify,
                                quiet=True)
        return rc, shipped, commands

    def test_a_push_is_recorded_once_in_place_and_the_check_fills_in_verified(self):
        for check_rc, verify, verified in ((0, True, True), (1, True, False),
                                           (124, True, False), (0, False, False)):
            with self.subTest(check_rc=check_rc, verify=verify), \
                    tempfile.TemporaryDirectory() as tmp:
                record = Path(tmp) / "bridge.record"
                try:
                    self._install("keep", tool_only=False, verify=verify,
                                  record=record, check_rc=check_rc)
                except ui.Die:
                    self.assertNotEqual(check_rc, 0)
                pushed = json.loads(record.read_text())
                self.assertEqual(pushed["verified"], verified)
                self.assertEqual(pushed["login"], "work")
                self.assertEqual(pushed["node"], "login01")

    def test_a_check_that_runs_out_of_time_says_so_rather_than_unreachable(self):
        with self.assertRaises(ui.Die):
            self._install("keep", tool_only=False, check_rc=124)
        self.assertEqual(self.waited, [300], "the check waits BRIDGE_VERIFY_TIMEOUT")
        self.assertIn("had not finished after 300s", self.said.getvalue())
        self.assertIn("the files are in place", self.said.getvalue())
        self.assertNotIn("could not reach", self.said.getvalue())

    def test_a_companion_only_install_ships_nothing_while_a_hub_edit_is_unresolved(self):
        for action, expect_ship in (("conflict", False), ("blocked", False),
                                    ("keep", False), ("adopt", False),
                                    ("push", True), ("install", True)):
            rc, shipped, commands = self._install(action, tool_only=True)
            self.assertEqual(rc, 0)
            self.assertEqual(shipped, [bridge.REMOTE_TOOL] if expect_ship else [], action)
            self.assertEqual(any("mv -f" in c for c in commands), expect_ship, action)
            self.assertTrue(any(c.endswith("nersc --version") for c in commands), action)

    def test_a_push_ships_the_credential_whatever_the_companion_does(self):
        for action, expect_tool in (("conflict", False), ("push", True)):
            rc, shipped, commands = self._install(action, tool_only=False)
            self.assertEqual(rc, 0)
            expected = [bridge.REMOTE_KEY, bridge.REMOTE_CERT]
            self.assertEqual(shipped, expected + ([bridge.REMOTE_TOOL] if expect_tool else []))
            (move,) = [c for c in commands if "mv -f" in c]
            self.assertEqual(move.count("mv -f"), len(shipped), "one move for everything")
            self.assertTrue(any(c.endswith("nersc run true") for c in commands))

    def test_a_missing_rsync_stops_before_any_certificate_or_connection(self):
        def untouched(*_a, **_k):
            raise AssertionError("nothing may be contacted without rsync")

        ctx = hub_context(untouched)
        ctx.logins.ensure = untouched
        err = io.StringIO()
        with _patched(bridge.shutil, "which", lambda name: None), \
             _patched(bridge, "_source_backend", untouched), \
             contextlib.redirect_stderr(err):
            with self.assertRaises(ui.Die):
                bridge.push(ctx, "work")
        self.assertIn("rsync is not installed", err.getvalue())

    def test_a_nersc_login_is_not_a_hub(self):
        ctx = hub_context(lambda *a, **k: None, backend="nersc", drives=True)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(ui.Die):
                bridge.push(ctx, "gpu")
        self.assertIn("on NERSC itself", err.getvalue())

    def status(self, ctx, login, record=None, find=None):
        """What `bridge status` prints, with the last push recorded as *record*
        (a directory there when it is an OSError)."""
        source = SimpleNamespace(credential_state=lambda: ("valid", "20h left"))
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
                _patched(bridge, "_source_backend", lambda: source), \
                _patched(bridge, "_state_path", lambda: Path(tmp) / "bridge.record"), \
                _patched(bridge, "_tool_status", lambda: None), \
                _patched(bridge.registry, "find", lambda name: find), \
                contextlib.redirect_stdout(out):
            path = Path(tmp) / "bridge.record"
            if record is OSError:
                path.mkdir()
            elif record is not None:
                path.write_text(record)
            self.assertEqual(bridge.status(ctx, login), 0)
        return out.getvalue()

    def test_status_shows_the_hubs_effective_config(self):
        outputs = {
            "--version": "Valid: from 2026-09-01T00:00:00 to 2026-09-02T00:00:00\n2.1\n",
            "config": "config: /home/user/.config/nersc/config\n  user = user\n",
        }

        def run_remote(name, command, timeout=60):
            key = "config" if command.endswith(" config") else "--version"
            return subprocess.CompletedProcess([], 0, outputs[key], "")

        hub = hub_context(run_remote)
        hub.logins.is_active = lambda name: True
        out = self.status(SimpleNamespace(sibling=lambda name: hub), "work", find="fasrc")
        self.assertIn("hub [work]: 2.1", out)
        self.assertIn("hub [work]: config: /home/user/.config/nersc/config", out)
        self.assertIn("hub [work]:   user = user", out)

    def test_status_says_whether_the_last_push_was_verified(self):
        import time

        for verified, expected in ((True, "verified against NERSC"),
                                   (False, "NOT verified end to end")):
            with self.subTest(verified=verified):
                out = self.status(SimpleNamespace(), None, json.dumps({
                    "pushed_at": int(time.time()) - 60, "login": "work",
                    "node": "login01", "cert_valid_until": int(time.time()) + 72000,
                    "verified": verified}))
                self.assertIn(expected, out)
                self.assertEqual("run it with: cluster run work -- "
                                 "~/.local/bin/nersc run true" in out, not verified)

    def test_status_reports_an_unreadable_push_record_instead_of_failing(self):
        for record in (OSError, "not json\n", "[1, 2]\n"):
            with self.subTest(record=record):
                out = self.status(SimpleNamespace(), None, record)
                self.assertIn("last push: unknown", out)
                self.assertIn("unreadable", out)


class TestCronPush(unittest.TestCase):
    """The unattended push: one certificate fetch, and no login after a refusal."""

    DENIED = "Permission denied (keyboard-interactive)."
    DROPPED = "Connection closed by 192.0.2.10 port 22"

    def setUp(self):
        temp_state(self)
        self.attempts = []
        self.hub = SimpleNamespace(settings=SimpleNamespace(str=lambda key: ""),
                                   logins=SimpleNamespace(last_failure=""))
        self.ctx = SimpleNamespace(sibling=lambda name: self.hub)

    def cron(self, outcomes, source=None, force=False):
        """push_cron over hub logins a, b and c of one cluster.

        *outcomes* maps a login to what its push fails with (its master's
        explanation, as a failed login leaves it); any other login succeeds.
        Returns (rc, what was said).
        """
        source = source or Source()

        def install(ctx, login, credential, force=False, **kwargs):
            self.attempts.append(login)
            # As the real one does: bring the certificate up to date first.
            bridge._ensure_fresh_cert(source, force=force)
            if login not in outcomes:
                return 0
            ctx.logins.last_failure = outcomes[login]
            raise ui.Die(1)

        err = io.StringIO()
        with _patched(bridge, "_source_backend", lambda: source), \
                _patched(bridge, "_install", install), \
                _patched(bridge, "_hub_backends", lambda: ["fasrc"]), \
                _patched(bridge.registry, "logins_by_backend",
                         lambda: {"fasrc": ["a", "b", "c"]}), \
                _patched(bridge.registry, "find", lambda login: "fasrc"), \
                contextlib.redirect_stderr(err):
            rc = bridge.push_cron(self.ctx, force=force)
        return rc, err.getvalue()

    def test_a_refused_credential_stops_after_one_login(self):
        rc, said = self.cron({"a": self.DENIED, "b": self.DENIED})
        self.assertEqual(rc, 1)
        self.assertEqual(self.attempts, ["a"])
        self.assertIn(f"bridge push via 'a' was refused: {self.DENIED}", said)
        self.assertIn("every fasrc login would repeat", said)
        self.assertIn("cluster --fasrc config credentials", said)
        self.assertIn("failed on every login tried (a)", said)

    def test_a_node_that_fails_falls_through_to_the_next_login(self):
        rc, said = self.cron({"a": self.DROPPED})
        self.assertEqual(rc, 0)
        self.assertEqual(self.attempts, ["a", "b"])
        self.assertIn("bridge push via 'a' failed; trying the next login", said)

    def test_force_fetches_one_certificate_however_many_logins_are_tried(self):
        source = Source(fetch=lambda source: setattr(source, "left", 86000))
        rc, _said = self.cron({"a": self.DROPPED, "b": self.DROPPED}, source,
                              force=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.attempts, ["a", "b", "c"])
        self.assertEqual(source.fetches, 1)

    def test_without_a_certificate_to_push_no_login_is_tried(self):
        def refused(_source):
            raise SystemExit("cluster: sshproxy rejected the credentials: "
                             "Authentication failed")

        rc, said = self.cron({}, Source(left=None, fetch=refused))
        self.assertEqual(rc, 1)
        self.assertEqual(self.attempts, [])
        self.assertIn("bridge: no certificate to push: sshproxy rejected", said)

    def test_an_overlapping_run_is_skipped_and_named(self):
        from clustertool import platform as plat

        held = plat.FileLock(config.state_dir("nersc") / "bridge.lock",
                             record_holder=True)
        self.assertTrue(held.acquire())
        self.addCleanup(held.release)
        rc, said = self.cron({})
        self.assertEqual(rc, 0)
        self.assertEqual(self.attempts, [])
        self.assertIn(f"another bridge push (pid {os.getpid()}", said)


class TestNerscCompanionInstall(unittest.TestCase):
    def test_local_files_live_in_clusters_own_trees(self):
        self.assertEqual(companion.LOCAL_CONFIG,
                         config.CONFIG_ROOT / "companion" / "config")
        self.assertEqual(companion.LOCAL_STATE, config.STATE_ROOT / "companion")
        # STATE_ROOT/nersc is the nersc backend's login state.
        self.assertNotEqual(companion.LOCAL_STATE, config.STATE_ROOT / "nersc")
        env = companion.local_environment({"PATH": "/bin"})
        self.assertEqual(env["NERSC_CONFIG"], str(companion.LOCAL_CONFIG))
        self.assertEqual(env["NERSC_STATE_DIR"], str(companion.LOCAL_STATE))
        self.assertEqual(env["PATH"], "/bin")
        explicit = companion.local_environment({"NERSC_CONFIG": "/elsewhere"})
        self.assertEqual(explicit["NERSC_CONFIG"], "/elsewhere")

    def test_bin_nersc_runs_the_companion_with_those_locations(self):
        """End to end, in a scratch home: `config` reads no network."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "empty-path").mkdir()
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("CLUSTER_", "NERSC_", "XDG_"))}
            env.update(HOME=str(root), PATH=str(root / "empty-path"),
                       XDG_CONFIG_HOME=str(root / "config"),
                       XDG_STATE_HOME=str(root / "state"))
            proc = subprocess.run([sys.executable, str(REPO_ROOT / "bin" / "nersc"), "config"],
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  universal_newlines=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("config: %s" % (root / "config" / "cluster" / "companion" / "config"),
                      proc.stdout)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def install_local(self, load=lambda _name: Source()):
        destination = self.root / "bin" / "nersc"
        config_path = self.root / "config" / "companion" / "config"
        with _patched(companion.backends, "load", load), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = companion.install_local(destination=destination, config_path=config_path)
        return rc, destination, config_path

    def test_local_install_links_to_the_entry_point_and_writes_a_template(self):
        rc, destination, config_path = self.install_local()
        self.assertEqual(rc, 0)
        self.assertTrue(destination.is_symlink())
        self.assertEqual(destination.resolve(), companion.LOCAL_ENTRY.resolve())
        text = config_path.read_text()
        for line in ("\nuser = user\n", "\nkey = /tmp/nersc-key\n",
                     "\nscratch = /pscratch/sd/u/user\n", "\n# mirror_src = "):
            self.assertIn(line, text)
        self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(config_path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual((config_path.parent / "mirror.exclude").read_text(),
                         companion.EXCLUDE_SOURCE.read_text())
        # A config that is there is never rewritten.
        config_path.write_text("mine = yes\n")
        self.install_local()
        self.assertEqual(config_path.read_text(), "mine = yes\n")

    def test_local_install_without_nersc_credentials_creates_nothing(self):
        def unavailable(_name):
            raise SystemExit("cluster: no NERSC username")

        with self.assertRaises(SystemExit):
            self.install_local(unavailable)
        self.assertEqual(list(self.root.iterdir()), [])

    def sync(self, hub, local, rc=0):
        """sync_from_hub of a hub copy *hub* over a local source *local*:
        (the local source after it, what was said)."""
        destination = self.root / "nersc"
        destination.write_text(local)
        self.said = io.StringIO()
        with contextlib.redirect_stderr(self.said):
            self.assertEqual(companion.sync_from_hub(
                companion_context(hub, rc), destination=destination,
                backup_dir=self.root / "backups", lock_path=self.root / "lock",
                baseline_path=self.root / "installed.json"), 0)
        return destination.read_text(), self.said.getvalue()

    def test_sync_is_a_noop_when_the_hub_matches(self):
        text = companion.SOURCE.read_text()
        self.assertEqual(self.sync(text, text)[0], text)
        self.assertFalse((self.root / "backups").exists())

    def test_sync_validates_backs_up_and_atomically_updates(self):
        old = companion.SOURCE.read_text()
        version = companion._validated_version(old)
        new = old.replace(f'VERSION = "{version}"', f'VERSION = "{version}-agent"', 1)
        self.assertEqual(self.sync(new, old)[0], new)
        backups = list((self.root / "backups").iterdir())
        self.assertEqual([backup.read_text() for backup in backups], [old])

    def test_sync_refuses_a_hub_copy_that_is_missing_unreadable_or_not_the_companion(self):
        # rc 8 and 9: the same distinct exit codes that reconcile_hub reads (_READ_HUB).
        for hub, rc, said in (
                ("this is not the tool\n", 0, ""),
                ("", 8, "install it first: cluster nersc-tool install work"),
                ("", 9, "cannot be read")):
            with self.subTest(rc=rc):
                with self.assertRaises(ui.Die):
                    self.sync(hub, "keep me\n", rc)
                self.assertIn(said, self.said.getvalue())
                self.assertEqual((self.root / "nersc").read_text(), "keep me\n")

    def test_a_local_source_mid_edit_does_not_stop_a_sync(self):
        hub = companion.SOURCE.read_text()
        now, said = self.sync(hub, "#!/usr/bin/env python3\ndef broken(:\n")
        self.assertEqual(now, hub)
        self.assertIn("v? -> v", said)


class TestNerscCompanionReconcile(unittest.TestCase):
    """A push must never cost a hub agent its edits to the companion.

    Coding agents on the hub edit ~/.local/bin/nersc in place, and the bridge
    pushes from cron three times a day, so a blind overwrite would keep an
    edit only until the next tick. Reconciliation decides against the hash of
    what was last installed there, which is what separates "the hub was
    edited" from "this machine's source is newer".
    """

    BASE = companion.SOURCE.read_text()

    @classmethod
    def _variant(cls, text, tag=None, version=None):
        """*text* with its VERSION tagged "+tag", or set to *version*."""
        if not hasattr(cls, "VERSION"):
            cls.VERSION = companion.inspect(cls.BASE)[0]
        return text.replace(f'VERSION = "{cls.VERSION}"',
                            f'VERSION = "{version or cls.VERSION + "+" + tag}"', 1)

    def reconcile(self, hub, local=BASE, installed=None, rc=0, adopt=None, **options):
        """reconcile_hub of a hub copy *hub* over a redirected source tree
        holding *local*, with *installed* on its own install record: (the
        plan, the tree's paths). The source after it is self.local()."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        paths = {"destination": root / "nersc",
                 "backup_dir": root / "backups",
                 "conflict_dir": root / "conflicts",
                 "baseline_path": root / "installed.json",
                 "lock_path": root / "lock"}
        paths["destination"].write_text(local)
        if installed is not None:
            companion.record_installed("fasrc", installed, "work", paths["baseline_path"])
        self.local = paths["destination"].read_text
        plan = companion.reconcile_hub(companion_context(hub, rc, adopt), "work",
                                       **paths, **options)
        return plan, paths

    def test_adoption_is_off_by_default(self):
        self.assertEqual(config.DEFAULTS["COMPANION_ADOPT_HUB_EDITS"], 0)
        with mock.patch.dict(os.environ):
            os.environ.pop("CLUSTER_COMPANION_ADOPT_HUB_EDITS", None)
            self.assertFalse(Settings("fasrc").flag("COMPANION_ADOPT_HUB_EDITS"))

    def test_by_default_a_hub_edit_is_kept_there_and_reported(self):
        edited = self._variant(self.BASE, "agent")
        plan, _paths = self.reconcile(edited, installed=self.BASE)
        self.assertEqual(plan["action"], "conflict")
        self.assertNotIn(plan["action"], companion.SHIP_ACTIONS)
        self.assertIn("COMPANION_ADOPT_HUB_EDITS", plan["detail"])
        self.assertEqual(self.local(), self.BASE)
        self.assertEqual(Path(plan["saved"]).read_text(), edited)

    def test_with_adoption_on_a_hub_edit_is_adopted_when_this_source_is_untouched(self):
        edited = self._variant(self.BASE, "agent")
        plan, paths = self.reconcile(edited, installed=self.BASE, adopt=True)
        self.assertEqual(plan["action"], "adopt")
        self.assertEqual(self.local(), edited)
        # Nothing is lost: the source it replaced stays on this machine.
        self.assertEqual([path.read_text() for path in paths["backup_dir"].iterdir()],
                         [self.BASE])
        record = companion.installed_record("fasrc", paths["baseline_path"])
        self.assertEqual(record["hash"], companion.digest(edited))
        self.assertNotIn("conflict", record)

    def test_a_local_edit_is_pushed_when_the_hub_is_as_installed(self):
        newer = self._variant(self.BASE, "local")
        plan, _paths = self.reconcile(self.BASE, newer, installed=self.BASE)
        self.assertEqual(plan["action"], "push")
        self.assertIn(plan["action"], companion.SHIP_ACTIONS)
        self.assertEqual(self.local(), newer)

    def test_two_sided_edits_conflict_and_ship_nothing(self):
        theirs = self._variant(self.BASE, "agent")
        mine = self._variant(self.BASE, "local")
        plan, paths = self.reconcile(theirs, mine, installed=self.BASE, adopt=True)
        self.assertEqual(plan["action"], "conflict")
        self.assertEqual(self.local(), mine)
        # Both versions survive: mine in place, theirs saved here.
        self.assertEqual(Path(plan["saved"]).read_text(), theirs)
        conflict = companion.installed_record("fasrc", paths["baseline_path"])["conflict"]
        self.assertEqual(conflict["hub_hash"], companion.digest(theirs))

    def test_an_identical_hub_ships_nothing_and_records_the_install(self):
        plan, paths = self.reconcile(self.BASE)
        self.assertEqual(plan["action"], "keep")
        self.assertEqual(companion.installed_record("fasrc", paths["baseline_path"])["hash"],
                         companion.digest(self.BASE))

    def test_a_hub_without_the_companion_gets_it_installed_and_an_unreadable_one_is_left(self):
        # rc 8 is the remote probe's "no such file", not a failure.
        for rc, action in ((8, "install"), (9, "blocked")):
            with self.subTest(rc=rc):
                plan, _paths = self.reconcile("", rc=rc)
                self.assertEqual(plan["action"], action)
                self.assertEqual(self.local(), self.BASE)

    def test_without_an_install_record_the_higher_version_wins_and_a_tie_goes_to_the_hub(self):
        older = self._variant(self.BASE, version="0.1")
        plan, _paths = self.reconcile(older)
        self.assertEqual(plan["action"], "push")
        self.assertEqual(self.local(), self.BASE)
        # An agent may tag its work "<version>+local4" without moving the base
        # version. With no install record, that tie goes to the hub.
        edited = self._variant(self.BASE, "local4")
        for adopt, action, local in ((True, "adopt", edited), (False, "conflict", self.BASE)):
            with self.subTest(adopt=adopt):
                plan, _paths = self.reconcile(edited, adopt=adopt)
                self.assertEqual(plan["action"], action)
                self.assertEqual(self.local(), local)

    def test_what_is_not_the_companion_or_is_oversized_is_never_adopted(self):
        for hub, kept in (("#!/bin/sh\necho hi\n", True),
                          (self.BASE + "# " + "x" * 1048577 + "\n", False)):
            with self.subTest(size=len(hub)):
                plan, _paths = self.reconcile(hub, installed=self.BASE, adopt=True)
                self.assertEqual(plan["action"], "blocked")
                self.assertEqual(self.local(), self.BASE)
                if kept:
                    self.assertEqual(Path(plan["saved"]).read_text(), hub)

    def test_overwrite_replaces_the_hubs_copy_but_keeps_a_copy_of_it(self):
        edited = self._variant(self.BASE, "agent")
        plan, _paths = self.reconcile(edited, installed=self.BASE, overwrite=True)
        self.assertEqual(plan["action"], "push")
        self.assertEqual(self.local(), self.BASE)
        self.assertEqual(Path(plan["saved"]).read_text(), edited)

    def test_a_redirected_source_never_writes_the_real_install_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "nersc"
            destination.write_text(self.BASE)
            with _patched(companion, "BASELINE_PATH", root / "must-not-exist.json"):
                plan = companion.reconcile_hub(
                    companion_context(self.BASE), "work", destination=destination,
                    backup_dir=root / "b", conflict_dir=root / "c",
                    lock_path=root / "lock")
            self.assertEqual(plan["action"], "keep")
            self.assertFalse((root / "must-not-exist.json").exists())

    def test_every_action_has_a_bridge_report_and_only_two_ship(self):
        actions = {"install", "push", "keep", "adopt", "conflict", "blocked"}
        self.assertEqual(set(bridge.TOOL_NOTES), actions)
        self.assertEqual(set(companion.SHIP_ACTIONS), {"install", "push"})

    def test_a_conflict_report_says_how_to_compare_and_resolve(self):
        plan = plan_for("conflict")
        plan.update(detail="both changed", saved=Path("/state/nersc-abc"))
        err = io.StringIO()
        with _patched(companion, "reconcile_hub", lambda *a, **k: plan), \
             contextlib.redirect_stderr(err):
            bridge._reconcile_tool(SimpleNamespace(), "work")
        self.assertIn(f"diff -u {companion.SOURCE} /state/nersc-abc", err.getvalue())
        self.assertIn("cluster nersc-tool sync work", err.getvalue())
        self.assertIn("cluster bridge push work --overwrite-tool", err.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
