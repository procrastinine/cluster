#!/usr/bin/env python3
"""`cluster setup`: merging tmux and VS Code configuration.

Run: python3 -m unittest tests.test_setup
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
import unittest
from pathlib import Path
from unittest import mock

# For `support`, which puts this checkout on sys.path and sets up the sandbox
# every test runs in, so it is imported before anything from clustertool.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import _patched, short_dir  # noqa: E402
from clustertool import config, setup, ui  # noqa: E402


class TestClusterSetup(unittest.TestCase):
    def test_tmux_merge_preserves_user_config_and_adds_only_missing_units(self):
        original = (
            "set -g mouse on\n"
            "bind r source-file ~/.tmux.conf\n"
            "if-shell \"tmux -V | grep 3\" \"set -s extended-keys on\"\n"
            "if-shell \"tmux -V | grep 3\" \"set -as terminal-features 'xterm*:extkeys'\"\n"
            "if-shell \"tmux -V | grep 3\" \"set -g allow-passthrough on\"\n"
        )
        missing = ["terminal title publishing", "session/window terminal title",
                   "window sizing for resized clients", "focus events",
                   "OSC 52 clipboard"]
        rendered, found = setup.render_tmux_config(original)
        self.assertIn("set -g mouse on", rendered)
        self.assertIn("bind r source-file", rendered)
        self.assertEqual(found, missing)
        self.assertEqual(rendered.count("extended-keys on"), 1)
        self.assertEqual(rendered.count(setup.TMUX_BEGIN), 1)
        self.assertEqual(setup.render_tmux_config(rendered), (rendered, missing))

    def test_tmux_merge_rejects_a_damaged_managed_block(self):
        with self.assertRaisesRegex(ValueError, "incomplete or duplicated"):
            setup.render_tmux_config(setup.TMUX_BEGIN + "\nset -g focus-events on\n")

    def test_vscode_merge_preserves_unrelated_settings_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "Machine" / "settings.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "editor.fontSize": 15,
                "files.watcherExclude": {"**/node_modules/**": True},
            }) + "\n")
            with _patched(config, "STATE_ROOT", root / "state"), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertTrue(setup.configure_vscode(
                    settings_path=path, mount_root=root / "cluster_mounts",
                    tab_title=True))
                first = path.read_text()
                self.assertTrue(setup.configure_vscode(
                    settings_path=path, mount_root=root / "cluster_mounts",
                    tab_title=True))
            document = json.loads(first)
            self.assertEqual(document["editor.fontSize"], 15)
            self.assertTrue(document["files.watcherExclude"]["**/node_modules/**"])
            self.assertTrue(document["files.watcherExclude"]["**/cluster_mounts/**"])
            self.assertEqual(document["terminal.integrated.tabs.title"], "${shellCommand}")
            self.assertEqual(document["terminal.integrated.tabs.description"], "")
            self.assertEqual(path.read_text(), first)
            self.assertEqual(len(list((root / "state/setup-backups").iterdir())), 1)

    def test_vscode_merge_refuses_jsonc_without_rewriting_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            original = '{\n  // keep this comment\n  "editor.fontSize": 15,\n}\n'
            path.write_text(original)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertFalse(setup.configure_vscode(settings_path=path))
            self.assertEqual(path.read_text(), original)
            self.assertIn("will not rewrite it", err.getvalue())
            self.assertIn("files.watcherExclude by hand", err.getvalue())

    def test_minus_one_disables_even_the_cached_automatic_check(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"CLUSTER_SETUP_DRIFT_CHECK_INTERVAL": "-1"}):
            (Path(tmp) / "main").mkdir()
            with _patched(config, "MOUNT_ROOT", Path(tmp)), \
                    _patched(setup, "vscode_installed", lambda *a: True), \
                    _patched(setup, "vscode_status",
                             lambda: self.fail("disabled check touched VS Code")):
                setup.warn_if_local_drift()

    def test_remote_install_validates_on_an_isolated_socket_and_never_restarts(self):
        commands = []
        ctx = remote_tmux_context(lambda command: commands.append(command) or
                                  subprocess.CompletedProcess(
                                      [], 0, "cluster-setup-moving\ncluster-setup-"
                                      "installed\ncluster-setup-reloaded=yes\n", ""),
                                  version="2.7")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(setup.configure_remote_tmux(ctx, "main"))
        (install,) = commands
        self.assertIn('tmux -L "$sock"', install)
        self.assertIn('test -s "$errors"', install)
        self.assertNotIn("tmux kill-server", install)
        self.assertNotIn("ssh -O exit", install)
        self.assertIn('tmux source-file "$dest"', install)


def remote_tmux_context(install, version="3.2a"):
    """A login whose home has a ~/.tmux.conf, running tmux *version*; *install*
    answers the install command."""
    from types import SimpleNamespace

    def run_remote(_name, command, timeout=60):
        if setup._TMUX_CONTENT in command:
            return subprocess.CompletedProcess(
                [], 0, f"tmux {version}\n" + setup._TMUX_CONTENT
                + "\npresent\nset -g mouse on\n", "")
        return install(command)

    return SimpleNamespace(
        login=lambda name: name or "main",
        backend=SimpleNamespace(name="fasrc", short=lambda node: node),
        settings=SimpleNamespace(int=lambda key: 60),
        state=SimpleNamespace(),
        logins=SimpleNamespace(ensure=lambda _name: True,
                               node_of=lambda _name: "login01",
                               run_remote=run_remote),
    )


class TestRemoteTmuxInstallSaysWhatHappened(unittest.TestCase):
    """Installed (with a backup), left in place, or not known: never a guess."""

    def configure(self, rc, stdout, stderr=""):
        """(returned, what was said), or (ui.Die, what was said)."""
        ctx = remote_tmux_context(
            lambda command: subprocess.CompletedProcess([], rc, stdout, stderr))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                result = setup.configure_remote_tmux(ctx, "main")
            except ui.Die:
                result = ui.Die
        return result, err.getvalue()

    def test_installed_and_reloaded(self):
        result, said = self.configure(0, "cluster-setup-moving\ncluster-setup-installed\n"
                                         "cluster-setup-reloaded=yes\n")
        self.assertTrue(result)
        self.assertIn("remote tmux configured on 'main'", said)
        self.assertIn("live server reloaded", said)
        self.assertIn("previous remote config retained at ~/.tmux.conf.cluster-backup-", said)

    def test_installed_but_the_live_server_refused_it(self):
        result, said = self.configure(
            0, "cluster-setup-moving\ncluster-setup-installed\n"
               "cluster-setup-reloaded=failed\n",
            "/home/user/.tmux.conf:12: unknown option: allow-passthrough\n")
        self.assertTrue(result, "the file is in place")
        self.assertIn("the live server did not reload it", said)
        self.assertIn("unknown option: allow-passthrough", said)
        self.assertIn("tmux source-file ~/.tmux.conf", said)
        self.assertIn("cluster-backup-", said)

    def test_installed_and_then_the_connection_went(self):
        result, said = self.configure(
            255, "cluster-setup-moving\ncluster-setup-installed\n",
            "mux_client_read_packet: read header failed: Broken pipe\n")
        self.assertTrue(result)
        self.assertIn("remote tmux configured on 'main'", said)
        self.assertIn("the connection ended before the live server's reload", said)

    def test_a_move_begun_and_not_reported_or_no_word_back_is_unknown(self):
        for rc, stdout in ((255, "cluster-setup-moving\n"), (124, ""), (255, "")):
            with self.subTest(rc=rc, stdout=stdout):
                result, said = self.configure(rc, stdout)
                self.assertIs(result, ui.Die)
                self.assertIn("could not tell whether ~/.tmux.conf on 'main' was "
                              "replaced", said)
                self.assertIn("cluster setup --remote-only --check main", said)
                self.assertNotIn("left in place", said)

    def test_a_config_that_fails_validation_is_left_in_place(self):
        result, said = self.configure(65, "", "/home/user/.tmux.conf.cluster-new:3: "
                                              "unknown command: bogus\n")
        self.assertIs(result, ui.Die)
        self.assertIn("remote tmux validation/install failed on 'main'", said)
        self.assertIn("unknown command: bogus", said)
        self.assertIn("the existing ~/.tmux.conf and live tmux server were left in place",
                      said)


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("tmux"),
                     "runs the remote install as a cluster login node would: "
                     "GNU coreutils and tmux")
class TestRemoteTmuxInstallScript(unittest.TestCase):
    """The install command itself, run in a scratch home."""

    def setUp(self):
        self.home = short_dir(self)
        self.runtime = short_dir(self)

    def run_install(self, current, rendered):
        """What _install_remote_tmux returns, with *current* as ~/.tmux.conf."""
        (self.home / ".tmux.conf").write_text(current)

        def run_remote(command):
            return subprocess.run(
                ["bash", "-c", command], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, universal_newlines=True, timeout=60,
                env=dict(os.environ, HOME=str(self.home),
                         TMUX_TMPDIR=str(self.runtime)))

        with contextlib.redirect_stderr(io.StringIO()):
            return setup._install_remote_tmux(remote_tmux_context(run_remote), "main",
                                              rendered, current, True)

    def left(self):
        return sorted(path.name for path in self.home.iterdir())

    def test_a_valid_config_is_installed_with_a_backup_and_nothing_left_over(self):
        reloaded, backup, _detail = self.run_install(
            "set -g mouse on\n", "set -g mouse on\nset -g focus-events on\n")
        self.assertEqual(reloaded, "none", "no live server in a scratch home")
        self.assertEqual((self.home / ".tmux.conf").read_text(),
                         "set -g mouse on\nset -g focus-events on\n")
        backup = backup.split("/")[-1]
        self.assertEqual((self.home / backup).read_text(), "set -g mouse on\n")
        self.assertEqual(self.left(), [".tmux.conf", backup])

    def test_a_config_tmux_rejects_is_never_installed(self):
        with self.assertRaises(ui.Die):
            self.run_install("set -g mouse on\n",
                             "set -g mouse on\nthis-is-not-a-tmux-command\n")
        self.assertEqual((self.home / ".tmux.conf").read_text(), "set -g mouse on\n")
        self.assertEqual(self.left(), [".tmux.conf"])


class TestVSCodeIsConfiguredOnlyWhereItIs(unittest.TestCase):
    """Setup edits an editor that is installed, and only the parts it owns."""

    def setUp(self):
        self.setup = setup
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.server = self.root / ".vscode-server"
        self.path = self.server / "data" / "Machine" / "settings.json"
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(_patched(config, "STATE_ROOT", self.root / "state"))
        stack.enter_context(_patched(config, "MOUNT_ROOT", self.root / "mounts"))
        stack.enter_context(mock.patch.dict(os.environ, {
            "CLUSTER_VSCODE_SETTINGS": str(self.path)}))
        os.environ.pop("CLUSTER_VSCODE_TAB_TITLE", None)

    def quietly(self, call, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            result = call(*args, **kwargs)
        return result, out.getvalue() + err.getvalue()

    def test_a_machine_without_vs_code_is_left_without_it(self):
        ready, said = self.quietly(self.setup.configure_vscode)
        self.assertTrue(ready)
        self.assertIn("no VS Code Server", said)
        self.assertFalse(self.server.exists(), "setup must not create an editor's dir")
        ready, _said = self.quietly(self.setup.configure_vscode, check=True)
        self.assertTrue(ready, "nothing to configure is not drift")
        self.assertTrue(self.setup.vscode_status()[0])

    def test_the_drift_warning_needs_an_editor_and_a_mount(self):
        (self.root / "mounts").mkdir()
        with _patched(self.setup, "vscode_status",
                      lambda: self.fail("no VS Code here, so nothing to check")):
            _result, said = self.quietly(self.setup.warn_if_local_drift)
        self.assertEqual(said, "")
        self.server.mkdir()
        with _patched(self.setup, "vscode_status",
                      lambda: self.fail("nothing is mounted yet")):
            _result, said = self.quietly(self.setup.warn_if_local_drift)
        (self.root / "mounts" / "main").mkdir()
        _result, said = self.quietly(self.setup.warn_if_local_drift)
        self.assertIn("needs attention", said)

    def test_an_installed_server_gets_the_exclusion_but_keeps_its_tab_title(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps(
            {"terminal.integrated.tabs.title": "${process}"}) + "\n")
        ready, _said = self.quietly(self.setup.configure_vscode)
        self.assertTrue(ready)
        document = json.loads(self.path.read_text())
        self.assertTrue(document["files.watcherExclude"]["**/mounts/**"])
        self.assertEqual(document["terminal.integrated.tabs.title"], "${process}")
        self.assertNotIn("terminal.integrated.tabs.description", document)
        self.assertTrue(self.setup.vscode_status()[0])

    def test_each_installed_editor_is_configured_where_it_keeps_settings(self):
        from clustertool import config, platform as plat

        os.environ.pop("CLUSTER_VSCODE_SETTINGS")
        home = self.root / "home"
        with _patched(os, "environ", dict(os.environ, HOME=str(home),
                                          XDG_CONFIG_HOME="")):
            with _patched(plat, "IS_MAC", True):
                self.assertEqual(self.setup.vscode_targets(), [
                    home / "Library/Application Support/Code/User/settings.json"])
            with _patched(plat, "IS_MAC", False):
                server = home / ".vscode-server/data/Machine/settings.json"
                desktop = home / ".config/Code/User/settings.json"
                self.assertEqual(self.setup.vscode_targets(), [server, desktop])
                self.assertFalse(self.setup.vscode_installed())
                (home / ".config/Code/agent-host").mkdir(parents=True)
                self.assertFalse(self.setup.vscode_installed(),
                                 "another program's folder there is not an editor")
                (home / ".config/Code/User").mkdir()
                ready, said = self.quietly(self.setup.configure_vscode)
                self.assertTrue(ready, said)
                self.assertTrue(json.loads(desktop.read_text())
                                ["files.watcherExclude"]["**/mounts/**"])
                self.assertFalse(server.exists(), "no server here, so none is made")
                self.assertTrue(self.setup.vscode_status()[0])
            with _patched(config, "global_value",
                          lambda key, default=None: str(self.path)
                          if key == "VSCODE_SETTINGS" else default):
                self.assertEqual(self.setup.vscode_targets(), [self.path])

    def test_the_tab_title_is_opt_in(self):
        from clustertool import config

        self.server.mkdir()
        self.assertEqual(config.GLOBAL_DEFAULTS["VSCODE_TAB_TITLE"], "0")
        os.environ["CLUSTER_VSCODE_TAB_TITLE"] = "on"      # put back by the fixture
        self.assertFalse(self.setup.vscode_status()[0])
        ready, _said = self.quietly(self.setup.configure_vscode)
        self.assertTrue(ready)
        document = json.loads(self.path.read_text())
        self.assertEqual(document["terminal.integrated.tabs.title"], "${shellCommand}")
        self.assertEqual(config.parse_value("VSCODE_TAB_TITLE", "yes"), 1)
        with self.assertRaises(ValueError):
            config.parse_value("VSCODE_TAB_TITLE", "sometimes")


if __name__ == "__main__":
    unittest.main(verbosity=2)
