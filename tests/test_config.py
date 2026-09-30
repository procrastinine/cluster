#!/usr/bin/env python3
"""Configuration: the settings file, private files and public defaults.

Run: python3 -m unittest tests.test_config
(or the whole suite: python3 -m unittest discover -s tests -p 'test_*.py')
"""

from __future__ import annotations

import configparser
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# For `support`, which puts this checkout on sys.path and sets up the sandbox
# every test runs in, so it is imported before anything from clustertool.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import _IsolatedMachine, _patched  # noqa: E402
from clustertool import config  # noqa: E402
from clustertool.backends import BACKENDS, TYPES  # noqa: E402
from clustertool.config import Settings  # noqa: E402


class TestUnifiedConfiguration(unittest.TestCase):
    def setUp(self):
        from unittest import mock

        from clustertool import config as configmod

        self.config = configmod
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "settings.ini"
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(_patched(configmod, "SETTINGS_FILE", self.path))
        stack.enter_context(mock.patch.dict(os.environ))
        configmod._WARNED.clear()
        for key in ("CLUSTER_MAX_LOGINS", "CLUSTER_NERSC_MAX_LOGINS",
                    "CLUSTER_NEW_LOGIN_MODE"):
            os.environ.pop(key, None)

    def test_file_scopes_and_environment_precedence(self):
        self.config.write_value("MAX_LOGINS", "7")
        self.config.write_value("MAX_LOGINS", "9", backend="nersc")
        self.assertEqual(self.config.Settings("fasrc").int("MAX_LOGINS"), 7)
        self.assertEqual(self.config.Settings("nersc").int("MAX_LOGINS"), 9)
        os.environ["CLUSTER_MAX_LOGINS"] = "11"
        self.assertEqual(self.config.Settings("fasrc").int("MAX_LOGINS"), 11)
        # The backend-specific environment value remains the final override.
        os.environ["CLUSTER_NERSC_MAX_LOGINS"] = "13"
        self.assertEqual(self.config.Settings("nersc").int("MAX_LOGINS"), 13)

    def test_invalid_number_is_loud_and_uses_the_safe_default(self):
        os.environ["CLUSTER_MAX_LOGINS"] = "lots"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            settings = self.config.Settings("fasrc")
            self.assertEqual(settings.int("MAX_LOGINS"),
                             self.config.DEFAULTS["MAX_LOGINS"])
            self.assertEqual(settings.int("MAX_LOGINS"),
                             self.config.DEFAULTS["MAX_LOGINS"])
        text = err.getvalue()
        self.assertIn("invalid MAX_LOGINS='lots'", text)
        self.assertIn("CLUSTER_MAX_LOGINS", text)
        self.assertEqual(text.count("invalid MAX_LOGINS"), 1)

    def test_a_value_is_validated_as_it_is_written(self):
        # SETUP_DRIFT_CHECK_INTERVAL takes -1 only as its disable value.
        for key, good, read, bad in (("NEW_LOGIN_MODE", "shell", "str", ["maybe"]),
                                     ("SETUP_DRIFT_CHECK_INTERVAL", "-1", "int",
                                      ["0", "-2"])):
            self.config.write_value(key, good)
            self.assertEqual(str(getattr(self.config.Settings("fasrc"), read)(key)), good)
            for value in bad:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    self.config.write_value(key, value)

    def test_a_malformed_file_is_loud_and_never_rewritten(self):
        broken = "[global\nMAX_LOGINS = 17\n"
        self.path.write_text(broken)
        for change in (lambda: self.config.write_value("MAX_LOGINS", "9"),
                       lambda: self.config.unset_value("MAX_LOGINS")):
            with self.assertRaisesRegex(ValueError, "cannot read configuration"):
                change()
            self.assertEqual(self.path.read_text(), broken)

    def test_every_numeric_setting_is_named_and_documented(self):
        numeric = [key for key, value in self.config.DEFAULTS.items()
                   if isinstance(value, int)]
        self.assertTrue(numeric)
        for key in numeric:
            with self.subTest(key=key):
                self.assertTrue(self.config.describe(key))

    def test_config_command_does_not_need_a_backend_context(self):
        from clustertool import cli

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["config", "get", "NEW_LOGIN_MODE"]), 0)
        self.assertEqual(out.getvalue().strip(), "tmux")

    def test_secret_on_argv_is_refused(self):
        from clustertool import configcmd
        from clustertool.context import Invocation

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                configcmd.run(Invocation("fasrc", True),
                              ["set", "password", "visible-secret"])
        self.assertFalse((Path(self.tmp.name) / "credentials").exists())


class TestSettingsAreDeclaredOnce(unittest.TestCase):
    """Each setting has one declaration: shared here, or on its backend."""

    def setUp(self):
        self.config = config

    def test_a_backend_only_setting_belongs_to_the_backends_that_declare_it(self):
        self.assertEqual(self.config.owners("KEY"), ["nersc"])
        self.assertEqual(self.config.owners("BRIDGE_LOGIN"), ["nersc"])
        self.assertEqual(self.config.owners("NODES"), sorted(BACKENDS))
        self.assertEqual(self.config.owners("MAX_LOGINS"), [])
        self.assertEqual(self.config.owners("BACKEND"), [])
        self.assertEqual(Settings("nersc").str("KEY"), "~/.ssh/nersc")
        with self.assertRaises(KeyError):
            Settings("fasrc").get("KEY")

    def test_every_setting_is_described_and_its_default_is_valid(self):
        for key in self.config.known_keys():
            with self.subTest(key=key):
                setting = self.config.lookup(key)
                self.assertTrue(setting.help)
                if setting.default != "" or not setting.required:
                    self.assertEqual(setting.parse(str(setting.default)),
                                     setting.default)

    def test_every_setting_is_in_the_usage_guide(self):
        # A limit nobody can find is as good as a constant.
        import re

        from support import REPO_ROOT

        usage = (REPO_ROOT / "USAGE.md").read_text()
        keys = set(self.config.SHARED) | set(self.config.GLOBAL) | set(self.config.RELAY)
        for cls in TYPES.values():
            keys |= set(cls.SETTINGS)
        missing = sorted(key for key in keys
                         if not re.search(r"`[^`\n]*\b%s\b[^`\n]*`" % key, usage))
        self.assertEqual(missing, [])

    def test_no_name_is_declared_twice(self):
        from clustertool.backends.base import Backend

        shared = set(self.config.SHARED) | set(self.config.GLOBAL) | set(self.config.RELAY)
        self.assertEqual(len(shared), len(self.config.SHARED) + len(self.config.GLOBAL)
                         + len(self.config.RELAY))
        for name, cls in BACKENDS.items():
            with self.subTest(backend=name):
                self.assertEqual(set(cls.SETTINGS) & shared, set())
                self.assertLessEqual(set(Backend.SETTINGS), set(cls.SETTINGS))

    def test_an_unregistered_backend_reads_what_every_backend_reads(self):
        self.assertEqual(Settings("elsewhere").str("NODES"), "")
        self.assertEqual(Settings("elsewhere").int("MAX_LOGINS"),
                         self.config.DEFAULTS["MAX_LOGINS"])


class TestSettingsFile(unittest.TestCase):
    """Changes keep what the user wrote; reads are cheap and warn once."""

    def setUp(self):
        self.config = config
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "cluster" / "settings.ini"
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(_patched(config, "SETTINGS_FILE", self.path))
        config._WARNED.clear()

    def test_a_writer_that_keeps_the_lock_is_named(self):
        from clustertool import platform as plat

        self.path.parent.mkdir()
        holder = plat.FileLock(self.path.with_name(".settings.ini.lock"),
                               record_holder=True)
        self.assertTrue(holder.acquire())
        self.addCleanup(holder.release)
        with _patched(self.config, "global_value",
                      lambda key, default=None: "0" if key == "LOCK_PATIENCE"
                      else default), \
                self.assertRaises(ValueError) as refused:
            self.config.write_value("MAX_LOGINS", "7")
        self.assertIn(f"pid {os.getpid()}", str(refused.exception))
        self.assertIn("with nothing moving", str(refused.exception))

    def test_comments_and_order_survive_a_set_and_an_unset(self):
        self.path.parent.mkdir()
        self.path.write_text(
            "# my settings\n"
            "[global]\n"
            "; why five\n"
            "max_logins = 5\n"
            "LINGER = 1\n"
            "\n"
            "# NERSC only\n"
            "[nersc]\n"
            "SCOPE = default\n")
        self.config.write_value("MAX_LOGINS", "7")
        self.config.write_value("CONNECT_TIMEOUT", "30", backend="nersc")
        text = self.path.read_text()
        self.assertEqual(text,
                         "# my settings\n"
                         "[global]\n"
                         "; why five\n"
                         "MAX_LOGINS = 7\n"
                         "LINGER = 1\n"
                         "\n"
                         "# NERSC only\n"
                         "[nersc]\n"
                         "SCOPE = default\n"
                         "CONNECT_TIMEOUT = 30\n")
        self.assertTrue(self.config.unset_value("SCOPE", backend="nersc"))
        self.assertTrue(self.config.unset_value("CONNECT_TIMEOUT", backend="nersc"))
        self.assertNotIn("[nersc]", self.path.read_text(), "an emptied section goes")
        self.assertIn("# my settings", self.path.read_text())
        self.assertFalse(self.config.unset_value("SCOPE", backend="nersc"))

    def test_a_new_file_and_its_directory_are_private(self):
        self.config.write_value("MAX_LOGINS", "6", backend="fasrc")
        self.assertEqual(self.path.read_text(), "[fasrc]\nMAX_LOGINS = 6\n")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)

    def test_a_value_may_not_span_lines(self):
        with self.assertRaisesRegex(ValueError, "span lines"):
            self.config.write_value("DEFAULT_LOGIN", "a\nb")

    def test_any_name_can_be_removed_even_one_nothing_reads(self):
        self.path.parent.mkdir()
        self.path.write_text("[global]\nNO_SUCH_SETTING = 1\nLINGER = 0\n")
        self.assertIn(("global", "NO_SUCH_SETTING"), self.config.file_entries())
        self.assertTrue(self.config.unset_value("NO_SUCH_SETTING"))
        self.assertEqual(self.path.read_text(), "[global]\nLINGER = 0\n")

    def test_relay_settings_live_where_the_relay_reads_them(self):
        self.config.write_value("RELAY_HOST", "user@relay.example.org")
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(self.path)
        self.assertEqual(parser.get("relay", "HOST"), "user@relay.example.org")
        self.assertEqual(self.config.resolve("RELAY_HOST")[0], "user@relay.example.org")
        with _patched(os, "environ", dict(os.environ, CLUSTER_RELAY_HOST="other@h")):
            self.assertEqual(self.config.resolve("RELAY_HOST"),
                             ("other@h", "CLUSTER_RELAY_HOST"))
        self.assertTrue(self.config.unset_value("RELAY_HOST"))
        self.assertEqual(self.path.read_text(), "")

    def test_a_malformed_file_warns_once_however_often_it_is_read(self):
        self.path.parent.mkdir()
        self.path.write_text("[global\nMAX_LOGINS = 17\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for _ in range(5):
                self.assertEqual(Settings("fasrc").int("MAX_LOGINS"),
                                 self.config.DEFAULTS["MAX_LOGINS"])
                self.config.global_value("STATE_ROOT")
        self.assertEqual(err.getvalue().count("cannot read configuration"), 1,
                         err.getvalue())

    def test_a_section_or_name_given_twice_is_read_as_the_relay_reads_it(self):
        self.path.parent.mkdir()
        self.path.write_text(
            "[relay]\nHOST = first@relay\n\n"
            "[global]\nMAX_LOGINS = 5\nMAX_LOGINS = 6\n\n"
            "[relay]\nHOST = second@relay\n")
        relay = configparser.ConfigParser(interpolation=None, strict=False)
        relay.read(self.path)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for _ in range(3):
                self.assertEqual(self.config.resolve("RELAY_HOST")[0],
                                 relay.get("relay", "HOST"))
                self.assertEqual(Settings("fasrc").int("MAX_LOGINS"), 6)
            self.config.write_value("RELAY_HOST", "third@relay")
        self.assertEqual(err.getvalue().count("the later value is used"), 1,
                         err.getvalue())
        self.assertEqual(self.config.resolve("RELAY_HOST")[0], "third@relay")
        self.assertEqual(self.path.read_text().count("HOST ="), 1,
                         "a change leaves the name given once")

    def test_the_file_is_parsed_again_only_when_it_changes(self):
        self.config.write_value("MAX_LOGINS", "7")
        with patch.object(self.config, "_parse", wraps=self.config._parse) as parse:
            for _ in range(5):
                self.assertEqual(Settings("fasrc").int("MAX_LOGINS"), 7)
            self.assertLessEqual(parse.call_count, 1)
            self.config.write_value("MAX_LOGINS", "8")
            self.assertEqual(Settings("fasrc").int("MAX_LOGINS"), 8)

    def test_an_empty_or_relative_xdg_directory_is_ignored(self):
        fallback = Path("/fallback")
        for value in ("", "relative/dir"):
            with self.subTest(value=value), \
                    _patched(os, "environ", dict(os.environ, XDG_CONFIG_HOME=value)):
                self.assertEqual(self.config.xdg_dir("XDG_CONFIG_HOME", fallback),
                                 fallback)
        with _patched(os, "environ", dict(os.environ, XDG_CONFIG_HOME="/abs")):
            self.assertEqual(self.config.xdg_dir("XDG_CONFIG_HOME", fallback),
                             Path("/abs"))


SEED = "JBSWY3DPEHPK3PXP"


class _ConfigCommand(_IsolatedMachine):
    def cli(self, *argv, stdin=""):
        """Run `cluster ARGV` with *stdin* as what the person or script types."""
        with _patched(sys, "stdin", io.StringIO(stdin)):
            return self.run_cli(*argv)

    def cred(self, backend, filename):
        return self.config.CRED_ROOT / backend / filename

    def enrolled(self, backend, user="someone"):
        directory = self.config.CRED_ROOT / backend
        directory.mkdir(parents=True, exist_ok=True)
        for filename, text in (("user", user), ("pass", "pw"), ("key.txt", SEED)):
            (directory / filename).write_text(text + "\n")
            (directory / filename).chmod(0o600)


class TestConfigCredentials(_ConfigCommand):
    """`config credentials`: every answer first, then one write, then the state."""

    def test_answers_from_a_script_are_saved_together_and_privately(self):
        rc, out, err = self.cli("--fasrc", "config", "credentials",
                                stdin=f"someone\nhunter2\n{SEED}\ny\n")
        self.assertEqual(rc, 0, err)
        for filename, text in (("user", "someone"), ("pass", "hunter2"),
                               ("key.txt", SEED)):
            with self.subTest(filename=filename):
                path = self.cred("fasrc", filename)
                self.assertEqual(path.read_text(), text + "\n")
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        for directory in (self.config.CRED_ROOT, self.config.CRED_ROOT / "fasrc"):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertIn("HarvardKey", out, "the site says where the answers come from")
        self.assertRegex(err, r"code right now: \d{6} ")
        self.assertNotRegex(out, r"\b\d{6}\b", "a live code never goes to stdout")
        self.assertNotIn("hunter2", out + err)
        self.assertNotIn("Warning", err)
        self.assertRegex(out, r"fasrc\s+someone\s+set\s+set")
        self.assertIn("next: cluster --fasrc new work", out)

    def test_an_unfinished_or_refused_answer_saves_nothing(self):
        for stdin, said in (("someone\nhunter2\n", "no answer for the TOTP seed"),
                            (f"someone\nhunter2\n{SEED}\nn\n",
                             "not the TOTP seed your app has"),
                            ("someone\nhunter2\n123456\n", "6-digit code"),
                            ("someone\n\n", "a password is required"),
                            ("\n", "a username is required")):
            with self.subTest(stdin=stdin):
                rc, _out, err = self.cli("--fasrc", "config", "credentials",
                                         stdin=stdin)
                self.assertEqual(rc, 1)
                self.assertIn(said, err)
                self.assertIn("nothing was saved", err)
                self.assertFalse((self.config.CRED_ROOT / "fasrc").exists())

    def test_enter_keeps_what_is_stored(self):
        self.enrolled("fasrc")
        before = {name: self.cred("fasrc", name).read_text()
                  for name in ("user", "pass", "key.txt")}
        rc, out, err = self.cli("--fasrc", "config", "credentials", stdin="\n\n\n")
        self.assertEqual(rc, 0, err)
        self.assertIn("nothing changed for fasrc", err)
        self.assertIn("Enter keeps the saved one", err)
        self.assertEqual({name: self.cred("fasrc", name).read_text()
                          for name in before}, before)
        rc, _out, err = self.cli("--fasrc", "config", "credentials",
                                 stdin="other\n\n\n")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.cred("fasrc", "user").read_text(), "other\n")
        self.assertIn("saved the username for fasrc", err)

    def test_the_link_in_a_qr_code_is_taken_for_its_seed(self):
        link = f"otpauth://totp/FASRC:someone?secret={SEED}&issuer=FASRC"
        rc, _out, err = self.cli("--fasrc", "config", "credentials",
                                 stdin=f"someone\nhunter2\n{link}\n\n")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.cred("fasrc", "key.txt").read_text(), SEED + "\n")

    def test_with_no_flag_it_asks_which_cluster(self):
        rc, out, err = self.cli("config", "credentials",
                                stdin=f"perlmutter\nsomeone\nhunter2\n{SEED}\ny\nproj\n")
        self.assertEqual(rc, 0, err)
        self.assertIn("Which cluster", err)
        self.assertEqual(self.cred("nersc", "user").read_text(), "someone\n")
        self.assertIn("Iris", out)
        self.assertEqual(Settings("nersc").str("COLLAB"), "proj")
        self.assertIn("[nersc]\nCOLLAB = proj", self.config.SETTINGS_FILE.read_text())
        self.assertIn("next: cluster --nersc auth", out)
        # "-" removes an optional answer; Enter keeps it.
        rc, _out, err = self.cli("--nersc", "config", "credentials",
                                 stdin="\n\n\n\n")
        self.assertEqual(Settings("nersc").str("COLLAB"), "proj")
        rc, _out, err = self.cli("--nersc", "config", "credentials",
                                 stdin="\n\n\n-\n")
        self.assertEqual(rc, 0, err)
        self.assertEqual(Settings("nersc").str("COLLAB"), "")

    def test_no_cluster_named_changes_nothing(self):
        for stdin in ("", "saturn\n"):
            with self.subTest(stdin=stdin):
                rc, _out, err = self.cli("config", "credentials", stdin=stdin)
                self.assertEqual(rc, 1)
                self.assertIn("no cluster was named", err)
        self.assertFalse(self.config.CRED_ROOT.exists())

    def test_single_credentials_are_read_and_written_by_name(self):
        rc, out, _err = self.cli("--fasrc", "config", "get", "username")
        self.assertEqual((rc, out), (1, "\n"))
        rc, _out, err = self.cli("--fasrc", "config", "set", "username", "someone")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.cred("fasrc", "user").read_text(), "someone\n")
        for key, expected in (("user", "someone"), ("password", "missing"),
                              ("totp", "missing")):
            with self.subTest(key=key):
                rc, out, _err = self.cli("--fasrc", "config", "get", key)
                self.assertEqual((rc, out.strip()), (0, expected))
        rc, _out, err = self.cli("--fasrc", "config", "unset", "user")
        self.assertIn("refusing to delete", err)
        self.assertTrue(self.cred("fasrc", "user").exists())


class TestConfigSettings(_ConfigCommand):
    """`config get/set/unset/show` for settings."""

    def test_a_setting_only_one_backend_reads_goes_to_its_section(self):
        rc, _out, err = self.cli("config", "set", "KEY", "~/.ssh/other")
        self.assertEqual(rc, 0, err)
        self.assertIn("[nersc]", err)
        self.assertEqual(self.config.SETTINGS_FILE.read_text(),
                         "[nersc]\nKEY = ~/.ssh/other\n")
        rc, _out, err = self.cli("--fasrc", "config", "set", "KEY", "x")
        self.assertEqual(rc, 1)
        self.assertIn("fasrc does not read it", err)
        self.assertIn("cluster --nersc config set KEY VALUE", err)
        rc, _out, err = self.cli("config", "set", "KEY", "x", "--global")
        self.assertEqual(rc, 1)
        self.assertIn("cannot be global", err)
        rc, out, _err = self.cli("config", "get", "KEY")
        self.assertEqual((rc, out), (0, "~/.ssh/other\n"))

    def test_a_setting_every_backend_keeps_for_itself_needs_a_backend(self):
        rc, _out, err = self.cli("config", "set", "NODES", "a b")
        self.assertEqual(rc, 1)
        self.assertIn("cluster --fasrc config set NODES VALUE", err)
        self.assertIn("cluster --nersc config set NODES VALUE", err)
        self.assertFalse(self.config.SETTINGS_FILE.exists())
        rc, _out, err = self.cli("--nersc", "config", "set", "NODES", "a b")
        self.assertEqual(rc, 0, err)
        self.assertEqual(Settings("nersc").str("NODES"), "a b")

    def test_a_shared_setting_is_global_unless_a_backend_is_named(self):
        self.cli("config", "set", "MAX_LOGINS", "7")
        self.cli("--nersc", "config", "set", "MAX_LOGINS", "9")
        self.assertEqual(self.config.SETTINGS_FILE.read_text(),
                         "[global]\nMAX_LOGINS = 7\n\n[nersc]\nMAX_LOGINS = 9\n")
        rc, out, _err = self.cli("--nersc", "config", "get", "MAX_LOGINS")
        self.assertEqual(out, "9\n")
        rc, out, err = self.cli("config", "unset", "MAX_LOGINS")
        self.assertIn("removed MAX_LOGINS from [global]", err)
        rc, out, err = self.cli("config", "unset", "MAX_LOGINS")
        self.assertIn("no persisted value in [global]", out)
        self.assertIn("cluster --nersc config unset MAX_LOGINS", err)

    def test_get_prints_an_empty_line_for_an_empty_value(self):
        # extras/archive-sync reads these, and relies on exit 0 either way.
        for argv in (("config", "get", "RCLONE"),
                     ("--backend", "nersc", "config", "get", "RCLONE")):
            with self.subTest(argv=argv):
                self.assertEqual(self.cli(*argv)[:2], (0, "\n"))
        self.cli("--nersc", "config", "set", "RCLONE", "/opt/rclone")
        self.assertEqual(self.cli("--backend", "nersc", "config", "get", "RCLONE")[:2],
                         (0, "/opt/rclone\n"))
        self.assertEqual(self.cli("config", "get", "RCLONE")[:2], (0, "\n"))

    def test_get_backend_names_the_backend_commands_use(self):
        self.assertEqual(self.cli("config", "get", "BACKEND")[:2], (0, "fasrc\n"))
        self.enrol("nersc")
        self.assertEqual(self.cli("config", "get", "BACKEND")[:2], (0, "nersc\n"))
        self.cli("config", "set", "BACKEND", "fasrc")
        self.assertEqual(self.cli("config", "get", "BACKEND")[:2], (0, "fasrc\n"))

    def test_show_on_a_new_machine_names_the_first_command(self):
        rc, out, _err = self.cli("config", "show")
        self.assertEqual(rc, 0)
        self.assertIn("run `cluster init`", out)
        self.assertNotIn("backend: fasrc", out)
        self.assertRegex(out, r"nersc\s+missing\s+missing\s+missing")
        self.enrol("nersc")
        rc, out, _err = self.cli("config", "show")
        self.assertIn("backend: nersc", out)

    def test_names_nothing_reads_are_shown_and_can_be_removed(self):
        self.config.SETTINGS_FILE.write_text(
            "# mine\n[global]\nMAX_LOGIN = 6\n[FASRC]\nLINGER = 1\n[fasrc]\nKEY = x\n")
        rc, out, _err = self.cli("config", "show")
        for name, section in (("MAX_LOGIN", "global"), ("LINGER", "FASRC"),
                              ("KEY", "fasrc")):
            with self.subTest(name=name):
                self.assertRegex(out, rf"{name}\s.*\[{section}\]\s+not a setting "
                                      "this tool reads")
        rc, _out, err = self.cli("config", "unset", "MAX_LOGIN")
        self.assertEqual(rc, 0, err)
        self.assertIn("removed MAX_LOGIN from [global]", err)
        rc, _out, err = self.cli("config", "unset", "no_such_thing")
        self.assertEqual(rc, 1)
        self.assertIn("unknown setting", err)
        self.assertEqual(self.config.SETTINGS_FILE.read_text(),
                         "# mine\n[FASRC]\nLINGER = 1\n[fasrc]\nKEY = x\n")


class TestPrivateFiles(unittest.TestCase):
    """Control sockets, state and keys are nobody else's business."""

    def setUp(self):
        self.config = config
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.addCleanup(os.umask, os.umask(0o002))

    def mode(self, path):
        return path.stat().st_mode & 0o777

    def test_the_tools_directories_are_created_private(self):
        home = self.root / "home"
        home.mkdir(mode=0o755)
        home.chmod(0o755)
        state = home / ".local/state/cluster"
        ctl = home / ".ssh/controlmasters"
        mounts = home / "cluster_mounts"
        with _patched(self.config, "STATE_ROOT", state), \
                _patched(self.config, "CTL_DIR", ctl), \
                _patched(self.config, "MOUNT_ROOT", mounts):
            self.config.ensure_dirs()
        for path in (state, ctl):
            self.assertEqual(self.mode(path), 0o700, path)
        self.assertFalse(mounts.exists(), "the first mount makes the mount root")
        self.assertEqual(self.mode(home / ".ssh"), 0o700,
                         "a ~/.ssh this created must not be group-writable")
        self.assertEqual(self.mode(home), 0o755, "an existing parent is left alone")
        # An existing loose one of its own is tightened.
        ctl.chmod(0o775)
        self.config.private_dir(ctl)
        self.assertEqual(self.mode(ctl), 0o700)

    def test_a_private_key_is_never_written_readable(self):
        from clustertool.backends import load

        os.umask(0)
        backend = load("nersc")
        backend.key_path = self.root / "nersc"
        backend.key_path.write_text("old\n")
        backend.key_path.chmod(0o644)
        backend._install_pair("-----BEGIN OPENSSH PRIVATE KEY-----\n",
                              "ssh-ed25519-cert-v01@openssh.com AAAA user\n")
        for path in (backend.key_path, backend.cert_path):
            self.assertEqual(self.mode(path), 0o600, path.name)
        self.assertEqual(backend.key_path.read_text(),
                         "-----BEGIN OPENSSH PRIVATE KEY-----\n")

    def test_trusting_nersc_host_ca_is_announced_once(self):
        from clustertool.backends.nersc import NerscBackend

        backend = NerscBackend.__new__(NerscBackend)
        home = self.root / "home"
        home.mkdir()
        err = io.StringIO()
        with patch.object(Path, "home", staticmethod(lambda: home)), \
                contextlib.redirect_stderr(err):
            self.assertTrue(backend.ensure_known_hosts())
            self.assertFalse(backend.ensure_known_hosts())
        self.assertEqual(err.getvalue().count("known_hosts"), 1, err.getvalue())
        self.assertIn("@cert-authority *.nersc.gov",
                      (home / ".ssh/known_hosts").read_text())


class TestPublicDefaults(unittest.TestCase):
    """Defaults that must not assume one person's machine or habits."""

    def test_defaults_name_nothing_site_or_vendor_specific(self):
        self.assertEqual(config.GLOBAL_DEFAULTS["FOREIGN_OWNER_OPTIONS"], "")
        self.assertEqual(config.DEFAULTS["NTP_SERVER"], "pool.ntp.org")
        self.assertEqual(config.DEFAULTS["SETUP_SYNC_NERSC_TOOL"], 0)
        self.assertEqual(config.DEFAULTS["LINGER"], 1,
                         "linger is what keeps tmux alive on FASRC")


if __name__ == "__main__":
    unittest.main(verbosity=2)
