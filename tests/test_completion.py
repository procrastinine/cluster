#!/usr/bin/env python3
"""Bash completion (completions/cluster.bash), driven through real bash.

Run: python3 -m unittest tests.test_completion
(or the whole suite: python3 -m unittest discover -s tests -p 'test_*.py')
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

# For `support`, which puts this checkout on sys.path and sets up the sandbox
# every test runs in, so it is imported before anything from clustertool.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import COMPLETION_SCRIPT  # noqa: E402


class TestBashCompletion(unittest.TestCase):
    """Every completion comes from local state saved by `cluster ls`."""

    SCRIPT = COMPLETION_SCRIPT

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        state = self.root / "cluster"
        (state / "fasrc").mkdir(parents=True)
        (state / "nersc").mkdir(parents=True)
        (state / "fasrc" / "main.node").write_text("boslogin08\n")
        (state / "nersc" / "work.node").write_text("login35\n")
        (state / "fasrc" / "sessions.cache").write_text(
            "main\tx\nmain\talpha\n")
        (state / "nersc" / "sessions.cache").write_text("work\tgpu\n")
        (state / "fasrc" / "abandoned.tsv").write_text(
            "boslogin07\tghost\tmain\nboslogin07\twraith\tmain\n")
        (state / "nersc" / "abandoned.tsv").write_text("login35\tnfrag\tgone\n")
        # What the bridge keeps beside the logins; neither is a login.
        (state / "nersc" / "bridge.record").write_text("{}\n")
        (state / "nersc" / "bridge.lock").write_text("")

    def tearDown(self):
        self.tmp.cleanup()

    def env(self):
        # A developer's own settings must not decide what these tests see: the
        # completion narrows to a named backend (CLUSTER_BACKEND) and finds
        # state through CLUSTER_STATE_ROOT or settings.ini. support.py's sandbox
        # has taken the runner's CLUSTER_* and XDG_* variables out of
        # os.environ, so settings.ini is looked for under this HOME, and state
        # is here.
        return dict(os.environ, XDG_STATE_HOME=str(self.root), HOME=str(self.root))

    def _run(self, script):
        return self._run_all([script])[0]

    def _run_all(self, scripts, prelude=""):
        """The lines each of *scripts* prints, as sets, from one bash that
        sources the completion once (then runs *prelude*)."""
        import shlex
        import subprocess

        end = "--end-of-reply--"
        body = "".join(f"{script}\necho {end}\n" for script in scripts)
        proc = subprocess.run(
            ["bash", "--noprofile", "--norc", "-c",
             f"source {shlex.quote(str(self.SCRIPT))}\n{prelude}\n{body}"],
            env=self.env(), text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=True)
        replies = proc.stdout.split(f"{end}\n")[:len(scripts)]
        return [{line for line in reply.splitlines() if line} for reply in replies]

    #: For a test of which slot a verb completes: its logins, and the default
    #: login that resolving costs most of a call.
    STAND_INS = ("_cluster_logins() { printf '%s\\n' main work; }\n"
                 "_cluster_default_login() { printf main; }")

    def complete(self, *words):
        return self.complete_all(words)[0]

    def complete_all(self, *cases, prelude=""):
        """complete() for each word list in *cases*, in one bash."""
        import shlex

        scripts = []
        for words in cases:
            array = " ".join(shlex.quote(word) for word in ("cluster", *words))
            scripts.append(f"COMPREPLY=(); COMP_WORDS=({array}); COMP_CWORD={len(words)}; "
                           "_cluster; printf '%s\\n' \"${COMPREPLY[@]}\"")
        return self._run_all(scripts, prelude)

    def test_state_root_is_found_the_three_ways_the_tool_finds_it(self):
        """`STATE_ROOT` is relocatable by env var or settings.ini, and a
        relocated state directory completes just as the default one does."""
        import shlex

        moved = self.root / "elsewhere"
        (moved / "fasrc").mkdir(parents=True)
        (moved / "fasrc" / "moved.node").write_text("holylogin06\n")

        def logins(prefix):
            return self._run(f"{prefix} _cluster_logins")

        # 1. the environment, with ~ expanded as the tool expands it
        self.assertEqual(
            logins(f"export CLUSTER_STATE_ROOT={shlex.quote(str(moved))};"),
            {"moved"})
        self.assertEqual(logins("export CLUSTER_STATE_ROOT='~/elsewhere/';"), {"moved"})
        # 2. [global] in settings.ini: the later of two values, and only in a
        # section named exactly so
        config = self.root / "conf"
        (config / "cluster").mkdir(parents=True)
        (config / "cluster" / "settings.ini").write_text(
            f"[Global]\nSTATE_ROOT = {self.root}/wrong\n"
            f"[global]\nstate_root = {self.root}/wrong\n"
            "[global]\nSTATE_ROOT: ~/elsewhere\n")
        self.assertEqual(
            logins(f"export XDG_CONFIG_HOME={shlex.quote(str(config))};"),
            {"moved"})
        # 3. the XDG default, which is what setUp arranges; an empty or
        # relative XDG_CONFIG_HOME is ignored, as the tool ignores it
        self.assertEqual(logins(""), {"main", "work"})
        (self.root / ".config").symlink_to(config)
        for value in ("", "conf"):
            with self.subTest(XDG_CONFIG_HOME=value):
                self.assertEqual(logins(f"cd {shlex.quote(str(self.root))}; "
                                        f"export XDG_CONFIG_HOME={value};"), {"moved"})

    def complete_line(self, line):
        """Complete *line* the way a terminal really does.

        Bash splits COMP_WORDS at COMP_WORDBREAKS, which holds ':' and '=', and
        readline then replaces only the fragment after the break — so the
        matches for `cluster nersc:s` come back as `sh`, not `nersc:sh`. The
        array-only helper above cannot show that, and every colon form in this
        completion depends on getting it right.
        """
        import re
        import shlex

        words = []
        for token in line.split():
            words += [part for part in re.split(r"([:=])", token) if part]
        if line.endswith(" "):
            words.append("")
        array = " ".join(shlex.quote(word) for word in words)
        return self._run(
            f"COMP_WORDS=({array}); COMP_CWORD={len(words) - 1}; "
            f"COMP_LINE={shlex.quote(line)}; COMP_POINT={len(line)}; "
            "_cluster; printf '%s\\n' \"${COMPREPLY[@]}\"")

    def test_attach_completes_login_then_that_logins_sessions(self):
        self.assertEqual(self.complete("a", ""), {"main", "work"})
        self.assertEqual(self.complete("a", "main", ""), {"alpha", "x"})
        self.assertEqual(self.complete("a", "work", ""), {"gpu"})

    def test_a_session_name_is_offered_as_it_is_and_never_run(self):
        # Session names come from the cluster; a TAB must not expand one.
        ran = self.root / "ran"
        (self.root / "cluster" / "nersc" / "sessions.cache").write_text(
            f"work\t$(:>{ran})\nwork\t*\n")
        self.assertEqual(self.complete("a", "work", ""), {f"$(:>{ran})", "*"})
        self.assertFalse(ran.exists())

    def test_kill_completes_login_then_that_logins_sessions(self):
        self.assertEqual(self.complete("k", ""), {"main", "work"})
        self.assertEqual(self.complete("k", "work", ""), {"gpu"})

    def test_new_completes_only_the_login_slot(self):
        self.assertEqual(self.complete("n", ""), self.complete("a", ""))
        self.assertEqual(self.complete("n", "main", ""), set())
        self.assertNotEqual(self.complete("a", "main", ""), set())

    def test_every_login_first_command_and_alias_completes_connections(self):
        verbs = """
            login l open where w node channels ch run r shell sh ssh
            sessions ss tmux-list attach a tmux new n new-session task session
            kill-session k close logout mount m umount unmount repair watch
            unwatch monitor boot ssh-command forget reset-state rename refresh
            reconnect pin pins unpin setup
        """.split()
        # Which slot each verb completes is what is checked here, so the
        # logins and the default login come from stand-ins: reading them from
        # state and settings is checked by the other tests, and costs a dozen
        # processes a call.
        replies = self.complete_all(*[(verb, "") for verb in verbs],
                                    prelude=self.STAND_INS)
        for verb, reply in zip(verbs, replies):
            with self.subTest(verb=verb):
                self.assertEqual(reply, {"main", "work"})

    def test_completion_audit_accounts_for_every_registered_alias(self):
        from clustertool.cli import COMMANDS

        login_first = set("""
            login l open where w node channels ch run r shell sh ssh
            sessions ss tmux-list attach a tmux new n new-session task session
            kill-session k close logout mount m umount unmount repair watch
            unwatch monitor boot ssh-command forget reset-state rename refresh
            reconnect pin pins unpin setup
        """.split())
        special = set("""
            bridge window new-window send rescue restore-layout restore repin move
            push pull transfer xfer copy cp list ls auth cert clean backends nodes
            mounts status st doctor fixterm config cfg nersc-tool nt strays stray
            linger init
        """.split())
        self.assertEqual(login_first | special, set(COMMANDS))

    def test_backend_and_remote_command_values_complete(self):
        self.assertIn("--backend=nersc", self.complete("--backend=n"))
        self.assertIn("nersc:sh", self.complete("nersc:s"))
        self.assertIn("echo", self.complete("run", "main", "--", ""))

    def test_completion_command_catalog_matches_the_cli(self):
        import subprocess
        from clustertool.cli import COMMANDS

        proc = subprocess.run(
            ["bash", "--noprofile", "--norc", "-c",
             f"source {self.SCRIPT}; _cluster_command_names"],
            text=True, stdout=subprocess.PIPE, check=True)
        self.assertEqual(set(proc.stdout.split()), set(COMMANDS))

    def test_the_generated_part_matches_the_code(self):
        # Backend names, aliases, built-in nodes and setting names come from the
        # code; after changing any of them, `cluster _complete-data --update`.
        from clustertool.cli import COMPLETION_BEGIN, COMPLETION_END, completion_data

        text = self.SCRIPT.read_text()
        start = text.index(COMPLETION_BEGIN)
        end = text.index(COMPLETION_END + "\n", start) + len(COMPLETION_END) + 1
        self.assertEqual(text[start:end], completion_data(),
                         "run: cluster _complete-data --update")

    def test_the_backend_list_is_the_registry(self):
        from clustertool.backends import ALIASES, BACKENDS

        self.assertEqual(self.complete("--backend", ""), set(BACKENDS) | set(ALIASES))
        self.assertEqual(self.complete("transfer", "--executor", ""), set(BACKENDS))
        self.assertTrue({f"--{name}" for name in (*BACKENDS, *ALIASES)}
                        <= self.complete("--"))
        self.assertEqual(self.complete("--perlmutter", "attach", ""), {"work"})
        self.assertEqual(self.complete("fas:attach", ""), {"main"})

    def test_new_logins_go_to_the_persisted_backend(self):
        # With BACKEND=nersc in [global], a new login's nodes are NERSC's
        # unless the line names another backend.
        config = self.root / ".config" / "cluster"
        config.mkdir(parents=True)
        (config / "settings.ini").write_text("[global]\nBACKEND = nersc\n")
        nersc = self.complete("rescue", "")
        self.assertIn("login40", nersc)
        self.assertIn("dtn01", nersc)
        self.assertNotIn("holylogin05", nersc)
        self.assertIn("holylogin05", self.complete("--fasrc", "rescue", ""))

    def test_a_backend_of_your_own_is_named_with_backend_only(self):
        config = self.root / ".config" / "cluster"
        config.mkdir(parents=True)
        (config / "settings.ini").write_text(
            "[my-lab]\nTYPE = ssh\nHOST = lab-login\nNODE_HOSTS = n1=a n2=b\n")
        self.assertIn("my-lab", self.complete("--backend", ""))
        self.assertIn("my-lab", self.complete("transfer", "--executor", ""))
        self.assertNotIn("--my-lab", self.complete("--"))
        self.assertEqual(self.complete("--backend", "my-lab", "rescue", ""), {"n1", "n2"})
        self.assertEqual(self.complete("backends", ""), {"add", "remove"})
        self.assertEqual(self.complete("backends", "remove", ""), {"my-lab"})

    def test_a_filename_with_a_space_is_one_reply(self):
        folder = self.root / "files"
        folder.mkdir()
        (folder / "two words.txt").write_text("")
        (folder / "plain.txt").write_text("")
        for verb in ("push", "transfer"):
            with self.subTest(verb=verb):
                offered = self.complete(verb, str(folder) + "/")
                self.assertEqual(offered, {f"{folder}/two words.txt", f"{folder}/plain.txt"})

    def test_completion_never_walks_mounts_or_invokes_ssh(self):
        text = "\n".join(
            line for line in self.SCRIPT.read_text().splitlines()
            if not line.lstrip().startswith("#"))
        self.assertNotIn("cluster_mounts", text)
        self.assertNotIn("sshfs", text.lower())
        self.assertNotIn("ControlPath", text)

    #: Options a command accepts that declared_options — which reads
    #: add_argument calls — cannot see. Anything else offered but not
    #: accepted is drift.
    UNDECLARED = {}

    def test_every_command_offers_exactly_the_options_it_accepts(self):
        # The completion's option table is written by hand, so the only thing
        # keeping it honest is reading the parsers back.
        from clustertool.backends import ALIASES, BACKENDS
        from clustertool.cli import COMMANDS, declared_options

        always = {"--backend", "--help"} | {f"--{name}" for name in (*BACKENDS, *ALIASES)}
        for verb in sorted({func.cmd_name for func in COMMANDS.values()}):
            func = COMMANDS[verb]
            accepted = {flag.strip()
                        for spec, _help in declared_options(func)
                        for flag in spec.split(",")}
            # Help is the one option every command answers, so the completion
            # offers it for all of them rather than per command; it is counted
            # in `always` above.
            accepted -= {"-h", "--help"}
            offered = self.complete(verb, "-") - always
            with self.subTest(verb=verb):
                self.assertEqual(accepted - offered, set(),
                                 f"{verb} accepts options completion never offers")
                self.assertEqual(offered - accepted - self.UNDECLARED.get(verb, set()),
                                 set(),
                                 f"{verb} completes options it does not accept")

    def test_a_named_backend_narrows_the_logins_offered(self):
        # `ctx.login()` refuses a name that lives on another backend, so
        # offering one is offering an error.
        self.assertEqual(self.complete("attach", ""), {"main", "work"})
        self.assertEqual(self.complete("--nersc", "attach", ""), {"work"})
        self.assertEqual(self.complete("--backend", "fasrc", "attach", ""), {"main"})
        self.assertEqual(self.complete_line("cluster nersc:attach "), {"work"})

    def test_the_bridge_record_is_never_offered_as_a_login(self):
        self.assertNotIn("bridge", self.complete("attach", ""))
        self.assertNotIn("bridge", self.complete("--nersc", "close", ""))

    def test_strays_offers_its_verbs_and_then_a_target(self):
        from clustertool.commands.strays import VERBS

        first = self.complete("strays", "")
        self.assertEqual(set(VERBS) - first, set())
        # A verb is optional — a bare `cluster strays` lists — so slot one
        # holds targets too.
        self.assertIn("boslogin07", first)
        replies = self.complete_all(*[("strays", verb, "") for verb in VERBS],
                                    prelude=self.STAND_INS)
        for verb, reply in zip(VERBS, replies):
            with self.subTest(verb=verb):
                self.assertIn("boslogin07", reply)

    def test_stray_sessions_come_from_the_local_abandonment_record(self):
        # The other source of strays is the breadcrumb catalogue in the cluster
        # home, and reading that would mean a connection.
        for verb in ("check", "clear", "kill"):
            with self.subTest(verb=verb):
                self.assertLessEqual({"ghost", "wraith"},
                                     self.complete("stray", verb, ""))
        # A session recorded under a live login is not a stray.
        self.assertNotIn("alpha", self.complete("strays", "check", ""))
        # Naming a backend narrows strays to it, as it narrows everything else.
        both = self.complete("strays", "check", "")
        self.assertLessEqual({"ghost", "nfrag"}, both)
        self.assertNotIn("nfrag", self.complete("--fasrc", "strays", "check", ""))
        self.assertNotIn("ghost", self.complete("--nersc", "strays", "check", ""))

    def test_rename_completes_a_target_but_not_the_new_name(self):
        self.assertIn("boslogin07", self.complete("strays", "rename", ""))
        self.assertEqual(self.complete("strays", "rename", "boslogin07:ghost", ""),
                         set())

    def test_a_node_session_stray_target_completes_that_nodes_sessions(self):
        self.assertEqual(self.complete("strays", "check", "boslogin07:"),
                         {"boslogin07:ghost", "boslogin07:wraith"})
        self.assertEqual(self.complete("strays", "kill", "boslogin08:"), set())
        # In a terminal the colon is a word break, so only the fragment after
        # it may be handed back.
        self.assertEqual(self.complete_line("cluster strays check boslogin07:"),
                         {"ghost", "wraith"})
        self.assertEqual(self.complete_line("cluster strays check boslogin07:w"),
                         {"wraith"})

    def test_adopting_under_a_new_name_completes_nothing_and_keeps_its_place(self):
        # --as names a login that does not exist yet. It also takes a value,
        # so it must not be counted as the target.
        self.assertEqual(self.complete("strays", "adopt", "boslogin07", "--as", ""),
                         set())
        self.assertIn("boslogin07",
                      self.complete("strays", "adopt", "--as", "rescued", ""))

    def test_a_terminals_word_break_still_completes_whole_tokens(self):
        # Bash splits at ':' and '=', so these forms only work if the words are
        # put back together first.
        self.assertEqual(self.complete_line("cluster nersc:doc"), {"doctor"})
        self.assertEqual(self.complete_line("cluster --backend=n"), {"nersc"})
        self.assertIn("gpu", self.complete_line("cluster attach work "))

    def test_bridge_completes_a_login_for_either_of_its_verbs(self):
        self.assertEqual(self.complete("bridge", ""), {"push", "status"})
        for verb in ("push", "status"):
            with self.subTest(verb=verb):
                self.assertEqual(self.complete("bridge", verb, ""), {"main", "work"})

    def test_config_completes_every_settable_key(self):
        from clustertool import config
        from clustertool.configcmd import credential_keys

        from clustertool.backends import TYPES

        offered = self.complete("config", "get", "")
        settable = set(config.known_keys(classes=TYPES.values()))
        self.assertEqual(settable - offered, set())
        self.assertEqual(offered - settable - set(credential_keys()), set())

    def test_every_subverb_list_matches_its_command(self):
        from clustertool.commands.strays import VERBS

        self.assertEqual(self.complete("strays", "") & set(VERBS), set(VERBS))
        self.assertEqual(self.complete("nt", ""),
                         {"path", "install-local", "install", "sync"})
        self.assertEqual(self.complete("cfg", ""),
                         {"show", "list", "get", "set", "unset", "path",
                          "credentials"})


class TestCompletionCost(unittest.TestCase):
    """A TAB waits for every process the completion starts."""

    def test_a_tab_reads_settings_and_state_once(self):
        """One awk for settings.ini, one for each kind of state file and one
        sort, however many backends and settings a TAB looks at; offering
        the verbs starts none."""
        import shlex
        import shutil
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            for backend, login in (("fasrc", "main"), ("nersc", "work")):
                state = home / ".local" / "state" / "cluster" / backend
                state.mkdir(parents=True)
                (state / f"{login}.node").write_text("n\n")
                (state / "sessions.cache").write_text(f"{login}\ts\n")
                (state / "abandoned.tsv").write_text(f"n\tghost\t{login}\n")
            (home / ".config" / "cluster").mkdir(parents=True)
            (home / ".config" / "cluster" / "settings.ini").write_text(
                "[global]\nDEFAULT_LOGIN = main\n[fasrc]\nNODES = a b\n")
            # The only commands on PATH count themselves, so one started
            # uncounted is not found.
            log, tools = home / "started", home / "tools"
            tools.mkdir()
            for name in ("awk", "sort", "printenv"):
                (tools / name).write_text(
                    f"#!/bin/sh\necho {name} >> {shlex.quote(str(log))}\n"
                    f"exec {shlex.quote(shutil.which(name))} \"$@\"\n")
                (tools / name).chmod(0o755)
            env = dict(os.environ, HOME=str(home), PATH=str(tools))
            for words, offered, most in ((("",), {"attach"}, 0),
                                         (("attach", ""), {"main", "work"}, 2),
                                         (("attach", "main", ""), {"s"}, 3),
                                         (("window", ""), {"main", "work", "s"}, 4),
                                         (("strays", ""), {"a", "b", "ghost"}, 3)):
                with self.subTest(words=words):
                    log.write_text("")
                    array = " ".join(shlex.quote(word) for word in ("cluster", *words))
                    proc = subprocess.run(
                        [shutil.which("bash"), "--noprofile", "--norc", "-c",
                         f"source {shlex.quote(str(COMPLETION_SCRIPT))}; "
                         f"COMP_WORDS=({array}); COMP_CWORD={len(words)}; "
                         "_cluster; printf '%s\\n' \"${COMPREPLY[@]}\""],
                        env=env, text=True, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, check=True)
                    self.assertEqual(proc.stderr, "")
                    self.assertLessEqual(offered, set(proc.stdout.split()))
                    started = log.read_text().split()
                    self.assertLessEqual(len(started), most, started)


if __name__ == "__main__":
    unittest.main(verbosity=2)
