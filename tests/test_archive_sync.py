#!/usr/bin/env python3
"""extras/archive-sync and extras/archive-sync-cron, run for real against fakes.

Every run gets a temp HOME and a PATH that holds only the fakes a test writes
(`cluster`, `rclone`, an ssh stand-in) and ordinary tools taken from the
runner's PATH, so nothing can reach a real cluster or remote. The tools are
whatever the runner has: on a macOS runner, or with a PATH that imitates one,
the scripts run under that bash (3.2) and without flock(1).

Run: python3 -m unittest tests.test_archive_sync
(or the whole suite: python3 -m unittest discover -s tests -p 'test_*.py')
"""

from __future__ import annotations

import csv
import fcntl
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

# For `support`, which puts this checkout on sys.path and sets up the sandbox
# every test runs in, so it is imported before anything from clustertool.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import REPO_ROOT  # noqa: E402

SYNC = REPO_ROOT / "extras" / "archive-sync"
CRON = REPO_ROOT / "extras" / "archive-sync-cron"
SHIPPED_FILTER = REPO_ROOT / "extras" / "home.filter"

#: The sandbox's PATH, from which the tools each run may use are taken.
RUNNER_PATH = os.environ.get("PATH", os.defpath)
#: What the scripts and the fakes call, besides python3 and the fakes.
TOOLS = ("bash", "sh", "sed", "awk", "tr", "date", "cat", "mkdir", "mv", "rm",
         "dirname", "readlink", "find", "tee", "tail", "head", "grep", "wc",
         "sleep", "chmod", "ls", "touch", "cut", "sort")


def _header(script):
    """The help text: the header comment from line 2 to the first other line."""
    lines = script.read_text().splitlines()[1:]
    text = []
    for line in lines:
        if not line.startswith("#"):
            break
        text.append(line[2:] if line.startswith("# ") else line[1:])
    return text


def _find_has_printf():
    find = shutil.which("find", path=RUNNER_PATH)
    if not find:
        return False
    probe = subprocess.run([find, "/", "-maxdepth", "0", "-printf", ""],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return probe.returncode == 0


class _ScriptTest(unittest.TestCase):
    """A temp HOME, and a PATH of fakes and ordinary tools (see the module)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.bin = self.root / "bin"
        self.trace = self.root / "trace"
        self.home.mkdir()
        self.bin.mkdir()
        self.trace.write_text("")
        self.config_dir = self.home / ".config" / "cluster"
        self.state_dir = self.home / ".local" / "state" / "cluster" / "archive-sync"

    def tools(self, flock=True):
        path = self.root / ("tools" if flock else "tools-without-flock")
        if not path.is_dir():
            path.mkdir()
            for name in TOOLS + (("flock",) if flock else ()):
                found = shutil.which(name, path=RUNNER_PATH)
                if found:
                    (path / name).symlink_to(found)
            (path / "python3").symlink_to(sys.executable)
        return path

    def script(self, name, body, where=None):
        path = (where or self.bin) / name
        path.write_text("#!/usr/bin/env bash\n" + body)
        path.chmod(0o755)
        return path

    def environment(self, flock=True, env=None):
        full = dict(os.environ, HOME=str(self.home),
                    PATH=f"{self.bin}{os.pathsep}{self.tools(flock)}")
        for key, value in (env or {}).items():
            if value is None:
                full.pop(key, None)
            else:
                full[key] = value
        return full

    def run_script(self, script, *args, flock=True, env=None):
        proc = subprocess.run([str(script), *args],
                              env=self.environment(flock, env),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              encoding="utf-8", errors="replace", timeout=120)
        return proc.returncode, proc.stdout

    def traced(self):
        return self.trace.read_text()

    def hold_lock(self, path):
        """Hold an flock(2) lock on *path*, as a running archive-sync would."""
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "w")
        self.addCleanup(handle.close)
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


class _ArchiveSyncTest(_ScriptTest):
    """A configured archive-sync with a fake rclone on PATH."""

    def setUp(self):
        super().setUp()
        (self.config_dir / "archive").mkdir(parents=True)
        self.config_dir.chmod(0o700)
        self.config = self.config_dir / "archive-sync.conf"
        self.password = self.config_dir / "archive-sync.pass"
        self.password.write_text("not-a-real-password\n")
        self.password.chmod(0o600)
        self.filter("fasrc", "- /.cache/**\n")
        self.write_config(
            "REMOTE=fake:",
            "RCLONE_PASSWORD_FILE=~/.config/cluster/archive-sync.pass",
            "FALLBACK_LOGIN=archive",
            "TRANSPORT_fasrc=login",
            "LOGIN_fasrc=main",
            "TRANSFERS_fasrc=3",
            "CHECKERS_fasrc=3")
        self.rclone()

    def write_config(self, *lines):
        self.config.write_text("".join(line + "\n" for line in lines))
        self.config.chmod(0o600)

    def filter(self, backend, text):
        path = self.config_dir / "archive" / f"{backend}-home.filter"
        path.write_text(text)
        return path

    def rclone(self, version="1.66.0", sync="", where=None):
        """A fake rclone: answers `version`, and records every call."""
        return self.script("rclone", f"""
echo "rclone $*" >> {shlex.quote(str(self.trace))}
if [ "${{1-}}" = version ]; then
    echo "rclone v{version}"
    echo "- os/version: fake"
    exit 0
fi
{sync}
""", where=where)

    def good_ssh(self):
        # Answers the symlink scan: nothing to fence, status 0, the home.
        return self.script("ssh-good",
                           r"printf 'R\0%s\0\0H\0%s\0\0' 0 /fake/home" + "\n")

    def cluster(self, cases):
        """A fake `cluster` that records its calls and answers *cases*."""
        self.script("cluster", f"""
echo "cluster $*" >> {shlex.quote(str(self.trace))}
[ "${{1:-}}" = "--backend" ] && shift 2
case "${{1:-}}:${{2:-}}" in
{cases}
  config:get) ;;
  *) echo "fake cluster: unexpected command: $*" >&2; exit 3 ;;
esac
""")

    def login_fakes(self, *, main_usable=True, main_free="9", archive_exists=False):
        # Two stand-in "remote shells": one that answers the symlink scan, and
        # one that fails the way an exec-refusing node does.
        good = self.good_ssh()
        bad = self.script(
            "ssh-bad",
            'echo "channel 0: open failed: administratively prohibited" >&2\n'
            "exit 255\n")
        main_ssh = good if main_usable else bad
        if archive_exists:
            archive_channels = "echo 10"
        else:
            archive_channels = "echo \"cluster: no login named 'archive'\" >&2; exit 1"
        self.cluster(f"""
  ssh-command:main)    echo {main_ssh} ;;
  ssh-command:archive) echo {good} ;;
  channels:main)       echo {main_free} ;;
  channels:archive)    {archive_channels} ;;
  close:*|refresh:*)   ;;
""")

    def transfer_fakes(self, said="opened", rc=0):
        good = self.good_ssh()
        self.filter("nersc", "- /.cache/**\n")
        self.cluster(f"""
  ssh-command:--transfer)
      echo "cluster: {said} transfer connection pool" >&2
      echo "  close it when done: cluster --nersc transfer --close pool" >&2
      echo {good}; exit {rc} ;;
  transfer:--close) ;;
""")

    def sync(self, *args, flock=True, env=None):
        rc, out = self.run_script(SYNC, *args, flock=flock, env=env)
        return rc, out, self.traced()

    def rclone_calls(self, trace):
        return [line for line in trace.splitlines() if line.startswith("rclone sync ")]


class TestArchiveSyncTransportLadder(_ArchiveSyncTest):
    """archive-sync escalating off an unusable shared master.

    The thing worth testing is behaviour, not wording. A login master can be
    up and still useless two ways, both seen in practice: its channels are
    spent, or its node accepts the connection and refuses per-user exec
    (while still passing the login balancer's health check). Either way the
    run must open a login of its own rather than skip the day — and must close
    only a login it opened itself.
    """

    def test_exec_refusing_node_escalates_to_a_login_of_our_own(self):
        self.login_fakes(main_usable=False)
        rc, out, trace = self.sync("--backend", "fasrc")
        self.assertEqual(rc, 0, out)
        self.assertIn("ssh-command archive", trace,
                      "a scan that cannot run must escalate, not give up")
        self.assertTrue(self.rclone_calls(trace), "the archive still has to be made")
        self.assertIn("close archive", trace,
                      "a login we opened is ours to close")
        self.assertIn("administratively prohibited", out,
                      "the remote's own words are the difference between "
                      "'the node refused' and 'the connection died'")

    def test_spent_channels_escalate_instead_of_skipping_the_day(self):
        self.login_fakes(main_usable=True, main_free="2")
        rc, out, trace = self.sync("--backend", "fasrc")
        self.assertEqual(rc, 0, out)
        self.assertIn("ssh-command archive", trace)
        # A connection of our own has the whole budget, so the scaling that
        # exists only for a shared master must not follow us onto it.
        rclone = self.rclone_calls(trace)[0]
        self.assertIn("--transfers 3", rclone)
        self.assertIn("--checkers 3", rclone)

    def test_a_login_we_did_not_open_is_not_ours_to_close(self):
        self.login_fakes(main_usable=False, archive_exists=True)
        rc, out, trace = self.sync("--backend", "fasrc")
        self.assertEqual(rc, 0, out)
        self.assertIn("ssh-command archive", trace)
        self.assertNotIn("close archive", trace)
        self.assertNotIn("refresh archive", trace,
                         "somebody else's login is not ours to move either")

    def test_a_healthy_master_is_left_alone(self):
        self.login_fakes(main_usable=True)
        rc, out, trace = self.sync("--backend", "fasrc")
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.rclone_calls(trace))
        for spent in ("ssh-command archive", "channels archive",
                      "close archive", "refresh archive"):
            self.assertNotIn(spent, trace,
                             "no credential may be spent while the master works")
        # A stamp per success, so "when did this last actually work?" does not
        # mean reading a month of logs.
        stamp = self.state_dir / "last-success-fasrc"
        self.assertTrue(stamp.read_text().strip().isdigit())

    def test_a_signal_closes_the_login_the_run_opened(self):
        self.login_fakes(main_usable=False)
        self.rclone(sync="sleep 1")
        proc = subprocess.Popen([str(SYNC), "--backend", "fasrc"],
                                env=self.environment(),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 60
        while not self.rclone_calls(self.traced()):
            if proc.poll() is not None or time.monotonic() > deadline:
                proc.kill()
                self.fail("archive-sync never reached rclone")
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=60), 128 + signal.SIGTERM)
        self.assertIn("close archive", self.traced())
        self.assertFalse((self.state_dir / "last-success-fasrc").exists())

    def test_login_flag_selects_the_login_transport(self):
        self.filter("nersc", "- /.cache/**\n")
        good = self.good_ssh()
        self.cluster(f"""
  ssh-command:work) echo {good} ;;
  channels:work)    echo 9 ;;
""")
        rc, out, trace = self.sync("--backend", "nersc", "--login", "work")
        self.assertEqual(rc, 0, out)
        self.assertIn("ssh-command work", trace)
        self.assertNotIn("ssh-command --transfer", trace)


class TestArchiveSyncTransferConnection(_ArchiveSyncTest):
    """A dedicated transfer connection: no ladder, and only our own is closed."""

    def test_a_dedicated_transfer_connection_needs_no_ladder(self):
        self.transfer_fakes()
        rc, out, trace = self.sync("--backend", "nersc")
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.rclone_calls(trace))
        # No channel arithmetic, no fallback login, and the connection we
        # opened is the one we hand back, by name.
        self.assertNotIn("channels", trace)
        self.assertNotIn("ssh-command archive", trace)
        self.assertIn("transfer --close pool", trace)
        self.assertEqual(trace.count("transfer --close"), 1)

    def test_a_connection_that_was_already_open_is_left_open(self):
        self.transfer_fakes(said="reusing")
        rc, out, trace = self.sync("--backend", "nersc")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("transfer --close", trace,
                         "a connection somebody else opened is theirs to close")

    def test_a_connection_opened_by_a_failing_command_is_closed(self):
        self.transfer_fakes(rc=1)
        rc, out, trace = self.sync("--backend", "nersc")
        self.assertEqual(rc, 1, out)
        self.assertIn("transfer --close pool", trace)
        self.assertFalse(self.rclone_calls(trace))


class TestArchiveSyncConfiguration(_ArchiveSyncTest):
    """Where the settings and state live, and what is checked before connecting."""

    def show(self, *args, env=None):
        rc, out, _trace = self.sync("--backend", "fasrc", "--show-config", *args, env=env)
        self.assertEqual(rc, 0, out)
        return dict(line.split(":", 1) for line in out.splitlines()
                    if ":" in line and not line.startswith(" "))

    def test_unconfigured_is_a_polite_refusal_that_touches_nothing(self):
        self.config.unlink()
        # A config where some other tool keeps its own is not this one's.
        other = self.home / ".config" / "archive-sync" / "config"
        other.parent.mkdir(parents=True)
        other.write_text("REMOTE=fake:\n")
        self.login_fakes()
        rc, out, trace = self.sync("--backend", "fasrc")
        self.assertEqual(rc, 3, out)
        self.assertIn("~/.config/cluster/archive-sync.conf", out)
        self.assertIn("extras/README.md", out)
        self.assertEqual(trace, "", "an unconfigured run must not reach out")
        self.assertFalse((self.home / ".local").exists(),
                         "an unconfigured run must create nothing")

    def test_the_config_and_state_follow_the_xdg_directories(self):
        xdg_config = self.root / "xdg-config"
        xdg_state = self.root / "xdg-state"
        (xdg_config / "cluster").mkdir(parents=True)
        self.config.rename(xdg_config / "cluster" / "archive-sync.conf")
        self.login_fakes()
        env = {"XDG_CONFIG_HOME": str(xdg_config), "XDG_STATE_HOME": str(xdg_state)}
        shown = self.show(env=env)
        self.assertIn(str(xdg_config / "cluster" / "archive-sync.conf"), shown["config"])
        self.assertIn(str(xdg_state / "cluster" / "archive-sync"), shown["state"])
        rc, out, _trace = self.sync("--backend", "fasrc", env=env)
        self.assertEqual(rc, 0, out)
        self.assertTrue((xdg_state / "cluster/archive-sync/last-success-fasrc").is_file())
        self.assertFalse(self.state_dir.exists())
        # An empty or relative one counts as unset.
        (xdg_config / "cluster" / "archive-sync.conf").rename(self.config)
        for value in ("", "relative/dir"):
            with self.subTest(value=value):
                shown = self.show(env={"XDG_CONFIG_HOME": value, "XDG_STATE_HOME": value})
                self.assertIn("~/.config/cluster/archive-sync.conf (present)",
                              shown["config"])
                self.assertIn("~/.local/state/cluster/archive-sync", shown["state"])

    def test_without_a_filter_of_its_own_the_shipped_default_is_used(self):
        (self.config_dir / "archive" / "fasrc-home.filter").unlink()
        # Where rclone keeps its own files is nothing to do with this tool.
        stray = self.home / ".config" / "rclone" / "fasrc-home.filter"
        stray.parent.mkdir(parents=True)
        stray.write_text("- /**\n")
        self.login_fakes()
        shown = self.show()
        self.assertIn(str(SHIPPED_FILTER), shown["filter"])
        self.assertIn("default shipped with archive-sync", shown["filter"])
        hint = "to change it, copy it to ~/.config/cluster/archive/fasrc-home.filter"
        self.assertIn(hint, self.sync("--backend", "fasrc", "--show-config")[1])
        rc, out, trace = self.sync("--backend", "fasrc", "--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertIn("default shipped with archive-sync", out)
        self.assertIn(hint, out)
        self.assertIn("dry run: nothing was changed", out)
        rclone = self.rclone_calls(trace)[0]
        self.assertIn(f"--filter-from {SHIPPED_FILTER}", rclone)
        self.assertNotIn(str(stray), trace)
        # It keeps the scan's prunes to what it excludes: it keeps .git, so a
        # scan of .git trees is not skipped.
        prunes = shown["scan prunes"]
        for tree in ('"$H/.conda"', '"$H/.cache"', '"$H/.vscode-server"',
                     "-name node_modules", "-name __pycache__"):
            self.assertIn(tree, prunes)
        self.assertNotIn("-name .git", prunes)

    def test_a_filter_of_its_own_wins_over_the_shipped_default(self):
        self.login_fakes()
        shown = self.show()
        self.assertEqual(shown["filter"].strip(),
                         "~/.config/cluster/archive/fasrc-home.filter (present)")

    def test_what_is_missing_is_refused_before_connecting(self):
        self.login_fakes()
        for config, says in (
                (["FILTER=~/nowhere.filter"], ["~/nowhere.filter"]),
                # A named rclone that does not exist names the setting.
                (["RCLONE=~/nowhere/rclone"],
                 ["not found", "fix RCLONE in ~/.config/cluster/archive-sync.conf"]),
                (["RCLONE_PASSWORD_FILE=~/.config/cluster/no-such.pass"],
                 ["~/.config/cluster/no-such.pass"])):
            with self.subTest(config=config):
                self.write_config("REMOTE=fake:", *config)
                self.trace.write_text("")
                rc, out, trace = self.sync("--backend", "fasrc")
                self.assertEqual(rc, 2, out)
                for text in says:
                    self.assertIn(text, out)
                self.assertNotIn("ssh-command", trace)

    def test_an_old_rclone_is_refused_before_connecting(self):
        self.rclone(version="1.60.1-DEV")
        self.login_fakes()
        env = {"ARCHIVE_SYNC_RCLONE": str(self.bin / "rclone")}
        rc, out, trace = self.sync("--backend", "fasrc", env=env)
        self.assertEqual(rc, 2, out)
        self.assertIn("rclone 1.60, which is too old (need 1.64 or newer)", out)
        self.assertNotIn("ssh-command", trace)
        self.assertIn("PROBLEM:", self.show(env=env)["rclone"])

    def test_the_rclone_version_is_shown_and_asked_without_an_rclone_conf(self):
        self.login_fakes()
        shown = self.show()
        self.assertIn(str(self.bin / "rclone"), shown["rclone"])
        self.assertIn("rclone 1.66", shown["rclone"])
        self.assertIn("rclone version --config /dev/null", self.traced())

    def test_the_cluster_rclone_setting_is_used_when_there_is_no_own(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        self.rclone(where=elsewhere)
        (self.bin / "rclone").unlink()
        self.login_fakes()
        # The fake answers `config get RCLONE` with the binary off PATH.
        cluster = self.bin / "cluster"
        cluster.write_text(cluster.read_text().replace(
            "  config:get) ;;",
            f'  config:get) [ "${{3-}}" != RCLONE ] || echo {elsewhere / "rclone"} ;;'))
        shown = self.show()
        self.assertIn(str(elsewhere / "rclone"), shown["rclone"])
        self.assertIn("--backend fasrc config get RCLONE", self.traced())

    def test_too_old_everywhere_says_how_to_point_at_another(self):
        if any(os.path.exists(p) for p in ("/opt/homebrew/bin/rclone",
                                           "/usr/local/bin/rclone")):
            self.skipTest("this machine has an rclone where the search looks next")
        self.rclone(version="1.63.0")
        self.login_fakes()
        rc, out, _trace = self.sync("--backend", "fasrc")
        self.assertEqual(rc, 2, out)
        self.assertIn("1.63, which is too old", out)
        self.assertIn("cluster config set RCLONE /path/to/rclone", out)

    def test_the_password_file_reaches_rclone_quoted_for_its_parser(self):
        odd = self.config_dir / 'a "quoted" dir with spaces' / "pass"
        odd.parent.mkdir()
        odd.write_text("s3cret\n")
        odd.chmod(0o600)
        self.write_config("REMOTE=fake:", "LOGIN_fasrc=main",
                          f"RCLONE_PASSWORD_FILE='{odd}'")
        seen = self.root / "password-command"
        self.rclone(sync=f'printf "%s" "$RCLONE_PASSWORD_COMMAND" > {seen}')
        self.login_fakes()
        rc, out, _trace = self.sync("--backend", "fasrc")
        self.assertEqual(rc, 0, out)
        # rclone splits the command the way encoding/csv splits a line with
        # spaces for commas.
        argv = next(csv.reader([seen.read_text()], delimiter=" "))
        self.assertEqual(argv, ["/bin/cat", str(odd)])
        self.assertEqual(subprocess.run(argv, stdout=subprocess.PIPE).stdout,
                         b"s3cret\n")

    def test_the_ssh_command_reaches_rclone_split_the_way_rclone_splits_it(self):
        # `cluster ssh-command` prints shell words; rclone reads --sftp-ssh
        # as a CSV record with spaces for commas. A path with a space has to
        # come through as one word, and so does the rider's ProxyCommand.
        where = self.root / "bin dir"
        where.mkdir()
        ssh = self.script("ssh-good", r"printf 'R\0%s\0\0H\0%s\0\0' 0 /fake/home" + "\n",
                          where=where)
        words = [str(ssh), "-o", "ControlPath=/home/user/state dir/s.sock",
                 "-o", 'Tag="quoted"',
                 "-o", "ProxyCommand=/bin/sh -c 'echo gone >&2'", "user@login01"]
        printed = self.root / "ssh-command"
        printed.write_text(shlex.join(words) + "\n")
        self.cluster(f"""
  ssh-command:main)    cat {shlex.quote(str(printed))} ;;
  channels:main)       echo 9 ;;
  close:*|refresh:*)   ;;
""")
        seen = self.root / "sftp-ssh"
        self.rclone(sync=f"""
while [ $# -gt 0 ]; do
    [ "$1" = --sftp-ssh ] && printf "%s" "$2" > {shlex.quote(str(seen))}
    shift
done
""")
        rc, out, _trace = self.sync("--backend", "fasrc")
        self.assertEqual(rc, 0, out)
        value = seen.read_text()
        self.assertEqual(next(csv.reader([value], delimiter=" ", strict=True)), words)
        # Go's reader refuses a quote inside an unquoted field (Python's
        # does not), so the quoted form is checked as written.
        self.assertIn(' "Tag=""quoted""" ', value)

    def test_a_password_file_others_can_read_is_pointed_out_and_the_state_is_private(self):
        self.password.chmod(0o644)
        self.login_fakes()
        rc, out, _trace = self.sync("--backend", "fasrc")
        self.assertEqual(rc, 0, out)
        self.assertIn("readable by other users", out)
        self.assertEqual(self.state_dir.stat().st_mode & 0o777, 0o700)

    def test_a_dry_run_leaves_the_success_stamp_alone(self):
        self.login_fakes()
        for args in (("--dry-run",), ("-n",), ("--dry-run=true",)):
            with self.subTest(args=args):
                rc, out, trace = self.sync("--backend", "fasrc", *args)
                self.assertEqual(rc, 0, out)
                self.assertIn("--dry-run", self.rclone_calls(trace)[-1])
                self.assertFalse((self.state_dir / "last-success-fasrc").exists())


class TestArchiveSyncExitStatus(_ArchiveSyncTest):
    """The exit status tells archive-sync-cron whether a retry can help."""

    def test_a_run_already_going_is_exit_4_with_or_without_flock(self):
        self.login_fakes()
        # Without flock, a free lock is taken.
        rc, out, trace = self.sync("--backend", "fasrc", flock=False)
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.rclone_calls(trace))
        self.hold_lock(self.state_dir / "fasrc.lock")
        for flock in (True, False):
            with self.subTest(flock=flock):
                self.trace.write_text("")
                rc, out, trace = self.sync("--backend", "fasrc", flock=flock)
                self.assertEqual(rc, 4, out)
                self.assertIn("already running", out)
                self.assertNotIn("ssh-command", trace)

    def test_an_rclone_failure_is_worth_a_retry_and_a_usage_error_is_not(self):
        self.login_fakes()
        for rclone_rc, expected in ((5, 1), (7, 1), (1, 1), (2, 2)):
            with self.subTest(rclone_rc=rclone_rc):
                self.rclone(sync=f"exit {rclone_rc}")
                rc, out, _trace = self.sync("--backend", "fasrc")
                self.assertEqual(rc, expected, out)
                self.assertIn(f"rclone exited {rclone_rc}", out)
                self.assertFalse((self.state_dir / "last-success-fasrc").exists())

    def test_no_cluster_on_path_a_config_error_or_a_usage_error_is_exit_2(self):
        for args, config, says in (
                (["--backend", "fasrc"], None, "`cluster` is not on PATH"),
                (["--backend", "fasrc"], "NOT_A_KEY=1", "unknown key 'NOT_A_KEY'"),
                (["--backend"], None, "--backend needs a name")):
            with self.subTest(says=says):
                if config:
                    self.config.write_text("REMOTE=fake:\n%s\n" % config)
                rc, out, trace = self.sync(*args)
                self.assertEqual(rc, 2, out)
                self.assertIn(says, out)
                self.assertEqual(trace, "", "nothing runs")
                self.assertFalse(self.state_dir.exists())

    def test_help_does_not_start_a_sync(self):
        self.login_fakes()
        rc, out, trace = self.sync("--help")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("rclone", trace)
        self.assertIn("This is an OPTIONAL extra", out)
        # The whole header, through its last line.
        self.assertEqual(out.splitlines(), _header(SYNC))


@unittest.skipUnless(_find_has_printf(), "needs a find with -printf, as on the cluster")
class TestArchiveSyncSymlinkFence(_ArchiveSyncTest):
    """The symlink scan, run by a stand-in remote shell on a real directory tree.

    The stand-in runs the remote command with sh, in a temp directory that
    plays the cluster home, so the find expression, its NUL framing and the
    fence built from it are the real ones.
    """

    def setUp(self):
        super().setUp()
        (self.config_dir / "archive" / "fasrc-home.filter").unlink()  # the shipped one
        self.remote = self.root / "remote" / "user"
        self.remote.mkdir(parents=True)
        remote_ssh = self.script("ssh-local", """
cd "$FAKE_REMOTE_HOME" || exit 255
HOME="$FAKE_REMOTE_HOME" exec sh -c "$*"
""")
        self.cluster(f"""
  ssh-command:main) echo {remote_ssh} ;;
  channels:main)    echo 9 ;;
""")

    def fence(self, home=None):
        env = {"FAKE_REMOTE_HOME": str(home or self.remote)}
        rc, out, _trace = self.sync("--backend", "fasrc", "--fence-only", env=env)
        path = self.state_dir / "fasrc-symlink-fence.filter"
        rules = None
        if path.exists():
            rules = {line for line in path.read_text().splitlines()
                     if not line.startswith("#")}
        return rc, out, rules

    def link(self, name, target):
        raw = name if isinstance(name, bytes) else os.fsencode(name)
        os.symlink(os.fsencode(target), os.path.join(os.fsencode(self.remote), raw))

    def test_links_out_of_the_home_are_fenced_whatever_their_names(self):
        (self.remote / "sub").mkdir()
        (self.remote / "sub" / ".nosync").write_text("")
        (self.remote / ".cache").mkdir()
        self.link("ext", "/outside/place")
        self.link("inner", "sub")
        self.link("up", "../..")
        self.link("tab\there", "/outside")
        self.link("new\nline", "/outside")
        self.link("space ", "/outside")
        self.link("glob*[x]{y}", "/outside")
        # Pruned: the shipped filter excludes /.cache/**, so it is never read.
        self.link(".cache/hidden", "/outside")
        expected = {"- /ext", "- /up", "- /sub", "- /tab?here", "- /new?line",
                    "- /space?", "- /glob\\*\\[x\\]\\{y\\}"}
        fenced = 6
        if sys.platform.startswith("linux"):
            self.link(b"bad\xff", "/outside")
            expected.add("- /bad?")
            fenced += 1
        rc, out, rules = self.fence()
        self.assertEqual(rc, 0, out)
        self.assertEqual(rules, expected | {rule + "/**" for rule in expected})
        self.assertIn(f"fenced {fenced} external symlinks, 1 .nosync directories", out)

    def test_a_link_that_leaves_through_another_link_is_fenced(self):
        (self.remote / "sub").mkdir()
        self.link("hop", "/outside")
        self.link("chain", "hop/data")           # out, through hop
        self.link("relay", "chain")              # out, through chain and hop
        self.link("home-to-sub", "sub")          # in
        self.link("via", "home-to-sub/../sub")   # in, through an in-home link
        self.link("loop-a", "loop-b")            # nowhere: a loop
        self.link("loop-b", "loop-a")
        rc, out, rules = self.fence()
        self.assertEqual(rc, 0, out)
        fenced = {"- /hop", "- /chain", "- /relay", "- /loop-a", "- /loop-b"}
        self.assertEqual(rules, fenced | {rule + "/**" for rule in fenced})

    def test_a_home_reached_through_a_symlink_is_scanned_resolved(self):
        self.link("ext", "/outside")
        alias = self.root / "homes-alias"
        alias.symlink_to(self.remote)
        rc, out, rules = self.fence(home=alias)
        self.assertEqual(rc, 0, out)
        self.assertEqual(rules, {"- /ext", "- /ext/**"})

    def test_a_scan_that_cannot_read_everything_writes_no_fence(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root reads every directory")
        locked = self.remote / "locked"
        locked.mkdir()
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        rc, out, rules = self.fence()
        self.assertEqual(rc, 1, out)
        self.assertIsNone(rules)
        # The remote's own words, not only "find exited 1".
        self.assertIn("scan: ", out)
        self.assertIn("Permission denied", out)
        self.assertIn("no fence written", out)


class TestArchiveSyncCron(_ScriptTest):
    """archive-sync-cron around a fake archive-sync that records each call."""

    def setUp(self):
        super().setUp()
        self.here = self.root / "extras"
        self.here.mkdir()
        self.cron = self.here / "archive-sync-cron"
        shutil.copy(CRON, self.cron)
        self.counts = self.root / "counts"
        self.counts.mkdir()
        self.path_seen = self.root / "path-seen"
        trace = shlex.quote(str(self.trace))
        self.script("archive-sync", f"""
echo "archive-sync $*" >> {trace}
echo "$PATH" > {shlex.quote(str(self.path_seen))}
case " $* " in
    *" --list-backends "*)
        if [ -n "${{FAKE_LIST_RC-}}" ]; then echo "fake: list refused" >&2; exit "$FAKE_LIST_RC"; fi
        echo "${{FAKE_BACKENDS-fasrc nersc}}"
        exit 0 ;;
esac
backend=""; prev=""
for arg in "$@"; do [ "$prev" = --backend ] && backend="$arg"; prev="$arg"; done
count="{self.counts}/$backend"
n=$(( $(cat "$count" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$count"
eval "further=\\${{FAKE_FURTHER_$backend-}}"
if [ -n "$further" ]; then
    # A run's own log, named as archive-sync names them, recording a copy or
    # not. The time the try took comes from the clock cron_run installs.
    state={shlex.quote(str(self.state_dir))}
    mkdir -p "$state"
    log="$state/sync-$backend-20990101-$(printf %06d "$n").log"
    if [ "$(echo "$further" | awk -v n="$n" '{{ print (n <= NF) ? $n : $NF }}')" = 1 ]; then
        echo "2099/01/01 00:00:00 INFO  : a/file: Copied (new)" > "$log"
    else
        echo "2099/01/01 00:00:00 INFO  : There was nothing to transfer" > "$log"
    fi
    # The file this call fails on, if any ("-" for none), as rclone logs it.
    eval "failed=\\${{FAKE_FAILED_$backend-}}"
    failed="$(echo "$failed" | awk -v n="$n" '{{ print (n <= NF) ? $n : $NF }}')"
    case "$failed" in
        ''|-) ;;
        dir:*) echo "2099/01/01 00:00:01 ERROR : ${{failed#dir:}}: error reading source" \
                    "directory: error listing \"${{failed#dir:}}\": permission denied" >> "$log" ;;
        *) echo "2099/01/01 00:00:01 ERROR : $failed: Failed to copy: can't copy" \
                "- source file is being updated (size changed from 1 to 2)" >> "$log" ;;
    esac
fi
eval "statuses=\\${{FAKE_RC_$backend-0}}"
exit "$(echo "$statuses" | awk -v n="$n" '{{ print (n <= NF) ? $n : $NF }}')"
""", where=self.here)

    def cron_run(self, *args, flock=True, env=None):
        full = {"ARCHIVE_SYNC_RETRY_WAIT": "0"}
        full.update(env or {})
        if any(key.startswith("FAKE_FURTHER_") for key in full):
            self.moving_clock()
        return self.run_script(self.cron, *args, flock=flock, env=full)

    def moving_clock(self):
        """A date first on the cron's PATH whose every `date +%s` reads a
        second later than the one before: a try that gets further takes time
        (the cron counts whole seconds) without the test waiting for it."""
        local_bin = self.home / ".local" / "bin"
        local_bin.mkdir(parents=True, exist_ok=True)
        ticks = shlex.quote(str(self.root / "ticks"))
        real = shlex.quote(shutil.which("date", path=RUNNER_PATH) or "/bin/date")
        self.script("date", f"""
if [ "$*" = "+%s" ]; then
    n=$(( $(cat {ticks} 2>/dev/null || echo 0) + 1 )); echo "$n" > {ticks}
    echo $(( $({real} +%s) + n ))
else
    exec {real} "$@"
fi
""", where=local_bin)

    def calls(self, backend):
        return [line for line in self.traced().splitlines()
                if line.endswith(f"--backend {backend}")]

    def order(self):
        """The backends tried, in the order they were."""
        return [line.rsplit(" ", 1)[1] for line in self.traced().splitlines()
                if "--list-backends" not in line]

    def fake_sleep(self):
        """A sleep first on the cron's PATH (it puts ~/.local/bin first) that
        only writes down how long it was asked for; the file it writes to."""
        slept = self.root / "slept"
        local_bin = self.home / ".local" / "bin"
        local_bin.mkdir(parents=True, exist_ok=True)
        self.script("sleep", f'echo "$1" >> {shlex.quote(str(slept))}\n',
                    where=local_bin)
        return slept

    def log(self):
        return (self.state_dir / "cron.log").read_text()

    def fresh(self):
        """Forget the calls so far and what the runs logged (a held lock
        stays), for the next case of a table."""
        logged = [path for path in self.state_dir.glob("*") if path.suffix != ".lock"]
        for path in [*self.counts.iterdir(), *logged]:
            path.unlink()
        self.trace.write_text("")

    def test_unconfigured_says_so_on_stderr_and_a_configuration_error_is_logged(self):
        rc, out = self.cron_run(env={"FAKE_LIST_RC": "3"})
        self.assertEqual(rc, 3, out)
        self.assertIn("fake: list refused", out)
        self.assertFalse((self.home / ".local").exists(), "and creates nothing")
        rc, out = self.cron_run(env={"FAKE_LIST_RC": "2"})
        self.assertEqual(rc, 2, out)
        self.assertIn("configuration error (exit 2)", self.log())
        self.assertIn("fake: list refused", self.log())

    def test_the_log_lives_in_the_cluster_state_directory_or_an_absolute_xdg_one(self):
        rc, out = self.cron_run(env={"XDG_STATE_HOME": "relative"})
        self.assertEqual(rc, 0, out)
        self.assertIn("archive-sync-cron finished exit=0", self.log())
        self.assertEqual(self.state_dir.stat().st_mode & 0o777, 0o700)
        xdg_state = self.root / "xdg-state"
        rc, out = self.cron_run(env={"XDG_STATE_HOME": str(xdg_state)})
        self.assertEqual(rc, 0, out)
        self.assertTrue((xdg_state / "cluster/archive-sync/cron.log").is_file())

    def test_a_transient_failure_is_retried_after_every_backend_has_had_its_first_try(self):
        rc, out = self.cron_run(env={"FAKE_RC_fasrc": "1 0",
                                     "FAKE_BACKENDS": "fasrc nersc gpu"})
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.order(), ["fasrc", "nersc", "gpu", "fasrc"])
        self.assertIn("retrying", self.log())

    def test_what_a_retry_cannot_fix_is_not_retried(self):
        for status in ("2", "3", "4"):
            with self.subTest(status=status):
                self.fresh()
                rc, _out = self.cron_run(env={"FAKE_RC_fasrc": status})
                self.assertEqual(rc, int(status))
                self.assertEqual(len(self.calls("fasrc")), 1)
                self.assertEqual(len(self.calls("nersc")), 1,
                                 "one backend failing must not stop the others")

    def test_backend_names_one_and_passes_the_rest_through(self):
        rc, out = self.cron_run("--backend", "nersc", "--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.calls("fasrc"), [])
        sync = [line for line in self.traced().splitlines()
                if "--list-backends" not in line]
        self.assertEqual(sync, ["archive-sync --progress=false --stats 5m "
                                "--stats-one-line --backend nersc --dry-run"])

    def test_staleness_is_judged_for_every_configured_backend(self):
        self.state_dir.mkdir(parents=True)
        day = 86400
        (self.state_dir / "last-success-nersc").write_text(f"{int(time.time()) - 10 * day}\n")
        (self.state_dir / "last-success-gpu").write_text("")
        (self.state_dir / "last-success-fresh").write_text(f"{int(time.time())}\n")
        (self.state_dir / "last-success-gone").write_text(f"{int(time.time()) - 99 * day}\n")
        rc, out = self.cron_run(env={"FAKE_BACKENDS": "fasrc nersc gpu fresh"})
        self.assertEqual(rc, 0, out)
        stale = [line for line in self.log().splitlines() if line.startswith("STALE")]
        self.assertEqual(stale, [
            "STALE: the fasrc archive has no successful sync on record",
            "STALE: the nersc archive last succeeded 10d ago — every run since then has failed",
            f"STALE: the gpu archive's success stamp is unreadable "
            f"({self.state_dir / 'last-success-gpu'})",
        ])

    def test_a_run_already_going_is_exit_4_with_or_without_flock(self):
        rc, out = self.cron_run(flock=False)
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.calls("fasrc")), 1, "without flock the wrapper runs")
        self.hold_lock(self.state_dir / "cron.lock")
        for flock in (True, False):
            with self.subTest(flock=flock):
                self.fresh()
                rc, out = self.cron_run(flock=flock)
                self.assertEqual(rc, 4, out)
                self.assertIn("still running", self.log())
                self.assertEqual(self.calls("fasrc"), [])

    # --- trying again -------------------------------------------------------
    def test_a_backend_that_keeps_failing_is_left_for_the_next_run(self):
        rc, out = self.cron_run(env={"FAKE_RC_fasrc": "1"})
        self.assertEqual(rc, 1, out)
        self.assertEqual(len(self.calls("fasrc")), 3, "ARCHIVE_SYNC_RETRIES is 2")
        self.assertEqual(len(self.calls("nersc")), 1)
        self.assertIn("attempt 3 failed (exit 1); 3 failures in quick succession, "
                      "so it is left for the next run (ARCHIVE_SYNC_RETRIES=2)",
                      self.log())

    def test_the_wait_doubles_up_to_its_ceiling(self):
        slept = self.fake_sleep()
        rc, out = self.cron_run(env={
            "FAKE_RC_fasrc": "1", "ARCHIVE_SYNC_RETRIES": "3",
            "ARCHIVE_SYNC_RETRY_WAIT": "600", "ARCHIVE_SYNC_RETRY_WAIT_MAX": "1000"})
        self.assertEqual(rc, 1, out)
        # Each sleep is what is left of the wait when the next try comes up,
        # so a second may have gone to the tries in between.
        waits = [int(word) for word in slept.read_text().split()]
        self.assertEqual(len(waits), 3, waits)
        for got, wanted in zip(waits, [600, 1000, 1000]):
            self.assertTrue(wanted - 2 <= got <= wanted, waits)
        self.assertIn("attempt 1 failed (exit 1) — retrying in 600s", self.log())

    def test_a_backend_failing_on_the_same_file_does_not_starve_the_rest(self):
        # A live home: every try copies something (a shell history) and fails
        # on a file being written as it is read, or (as real sftp logs it) on
        # the same unreadable directory. That copy is not progress, so the
        # backend is left for the next run after its tries, and nersc has had
        # its own first try right after fasrc's. Crediting it would retry
        # fasrc forever with a half-life this short.
        for failed in ("jobs/run.out", "dir:projects/locked"):
            with self.subTest(failed=failed):
                self.fresh()
                rc, out = self.cron_run(env={
                    "FAKE_RC_fasrc": "1", "FAKE_FURTHER_fasrc": "1",
                    "FAKE_FAILED_fasrc": failed, "ARCHIVE_SYNC_RETRY_HALF_LIFE": "1"})
                self.assertEqual(rc, 1, out)
                self.assertEqual(self.order(), ["fasrc", "nersc", "fasrc", "fasrc"])
                log = self.log()
                self.assertIn("fasrc attempt 1 failed (exit 1) after getting further", log)
                self.assertIn("fasrc attempt 2 failed (exit 1) — retrying", log)
                self.assertIn("fasrc attempt 3 failed (exit 1); 3 failures in quick "
                              "succession, so it is left for the next run", log)
                self.assertEqual(list(self.state_dir.glob("cron-failed-*")), [],
                                 "what a run failed on is forgotten when it ends")

    def test_the_failures_are_read_as_rclone_logs_them(self):
        text = CRON.read_text()
        start = text.index("failed_paths() {")
        function = text[start:text.index("# got_further BACKEND")]
        log = self.root / "sync.log"
        # From an rclone sync over sftp with an unreadable file and directory.
        log.write_text(
            "2026/09/29 18:14:51 NOTICE: :sftp{kXWfm}: No host key validation is "
            "being performed.\n"
            "2026/09/29 18:14:51 ERROR : lockeddir: error reading source directory: "
            'error listing "lockeddir": permission denied\n'
            "2026/09/29 18:14:51 INFO  : a: Copied (new)\n"
            "2026/09/29 18:14:51 ERROR : unreadable: Failed to copy: failed to open "
            "source object: Open failed: permission denied\n"
            "2026/09/29 18:14:51 ERROR : Local file system at /x/d5: not deleting "
            "files as there were IO errors\n"
            "2026/09/29 18:14:51 ERROR : Attempt 1/1 failed with 2 errors and: "
            "failed to open source object: Open failed: permission denied\n")
        got = subprocess.run(
            [shutil.which("bash", path=RUNNER_PATH), "-c",
             function + 'failed_paths "$1"', "bash", str(log)],
            stdout=subprocess.PIPE, universal_newlines=True, check=True).stdout
        self.assertEqual(got.split("\n"), ["lockeddir", "unreadable", ""])


    def test_a_retry_past_the_window_is_left_for_the_next_run(self):
        slept = self.fake_sleep()
        rc, out = self.cron_run(env={
            "FAKE_RC_fasrc": "1 0", "ARCHIVE_SYNC_RETRY_WAIT": "600",
            "ARCHIVE_SYNC_RETRY_WINDOW": "300"})
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.order(), ["fasrc", "nersc"])
        self.assertFalse(slept.exists())
        self.assertIn("fasrc attempt 1 failed (exit 1); a try in 600s would start "
                      "past this run's ARCHIVE_SYNC_RETRY_WINDOW (300s), so it is "
                      "left for the next run", self.log())

    def test_the_window_never_cuts_a_try_short(self):
        # The fake's try takes a second on the clock, past a window of none.
        rc, out = self.cron_run("--backend", "fasrc", env={
            "FAKE_FURTHER_fasrc": "1", "ARCHIVE_SYNC_RETRY_WINDOW": "0"})
        self.assertEqual(rc, 0, out)
        self.assertIn("] finished exit=0", self.log())

    def test_a_run_stopped_by_a_signal_is_not_retried(self):
        for status in ("129", "130", "143"):
            with self.subTest(status=status):
                self.fresh()
                rc, _out = self.cron_run(env={"FAKE_RC_fasrc": status})
                self.assertEqual(rc, int(status))
                self.assertEqual(self.order(), ["fasrc"])
                log = self.log()
                self.assertIn(f"fasrc stopped by a signal (exit {status}), so "
                              "nothing more is tried in this run", log)
                self.assertIn("nersc not tried, since this run was stopped", log)

    def test_a_signal_leaves_the_retries_that_were_due(self):
        rc, out = self.cron_run(env={"FAKE_RC_fasrc": "1", "FAKE_RC_nersc": "143"})
        self.assertEqual(rc, 143, out)
        self.assertEqual(self.order(), ["fasrc", "nersc"])
        self.assertIn("fasrc not tried again, since this run was stopped", self.log())
        self.assertIn("archive-sync-cron finished exit=143", self.log())

    def test_a_run_that_keeps_getting_further_is_carried_on(self):
        # Failing on new files each time is still getting further.
        for further, failed, rc_wanted, tries in (("1", None, 0, 5),
                                                  ("1", "a b c d -", 0, 5),
                                                  ("0", None, 1, 3)):
            with self.subTest(further=further, failed=failed):
                self.fresh()
                env = {"FAKE_RC_fasrc": "1 1 1 1 0", "FAKE_FURTHER_fasrc": further,
                       "ARCHIVE_SYNC_RETRY_HALF_LIFE": "0"}
                if failed:
                    env["FAKE_FAILED_fasrc"] = failed
                rc, out = self.cron_run("--backend", "fasrc", env=env)
                self.assertEqual(rc, rc_wanted, out)
                self.assertEqual(len(self.calls("fasrc")), tries)
                self.assertEqual("attempt 4 failed (exit 1) after getting further"
                                 in self.log(), further == "1")

    def test_a_setting_that_is_not_a_number_ends_the_run(self):
        for key in ("ARCHIVE_SYNC_RETRIES", "ARCHIVE_SYNC_RETRY_HALF_LIFE",
                    "ARCHIVE_SYNC_RETRY_WINDOW"):
            with self.subTest(key=key):
                rc, out = self.cron_run(env={key: "lots"})
                self.assertEqual(rc, 2, out)
                self.assertIn(f"{key}=lots is not a number of 0 or more", self.log())

    def test_the_rule_is_the_cluster_tools_own(self):
        from clustertool.backoff import FailureMemory

        text = CRON.read_text()
        start = text.index("memory_fail() {")
        functions = text[start:text.index("# number NAME VALUE")]
        bash = shutil.which("bash", path=RUNNER_PATH)
        for delay, delay_max, limit in ((2, 60, 3), (600, 3600, 2), (600, 1000, 3),
                                        (8, 2, 1), (0, 60, 2), (1, 64, 0)):
            ceiling = subprocess.run(
                [bash, "-c", functions + f"memory_ceiling {delay} {delay_max} {limit}"],
                stdout=subprocess.PIPE, universal_newlines=True, check=True).stdout
            self.assertAlmostEqual(
                float(ceiling),
                FailureMemory(0, limit=limit, delay=delay, delay_max=delay_max).ceiling)
        # Enough failures in a row to reach the ceiling and stay there.
        steps = [(0, 300), (0, 300), (100, 300), (900, 300), (0, 0), (50, 0),
                 (0, 300)] + [(0, 300)] * 6 + [(200, 300)]
        script = functions + 'c=$(memory_ceiling 2 60 3); s=0\n' + "".join(
            f'n=$(memory_fail "$s" {healthy} {half} "$c"); s=$n; '
            'echo "$s $(memory_wait "$s" 2 60) $(memory_fresh "$s") '
            '$(memory_exhausted "$s" 3 && echo y || echo n)"\n'
            for healthy, half in steps)
        got = subprocess.run([bash, "-c", script],
                             stdout=subprocess.PIPE, universal_newlines=True,
                             check=True).stdout.splitlines()
        self.assertEqual(len(got), len(steps))
        memory = FailureMemory(300, limit=3, delay=2, delay_max=60)
        self.assertIn(memory.ceiling, [float(line.split()[0]) for line in got])
        for line, (healthy, half) in zip(got, steps):
            memory.half_life = half
            memory.failed(healthy)
            score, wait, fresh, exhausted = line.split()
            self.assertAlmostEqual(float(score), memory.score, places=5)
            self.assertEqual(int(wait), int(memory.wait() + 0.5))
            self.assertEqual(int(fresh), memory.fresh)
            self.assertEqual(exhausted == "y", memory.exhausted)

    def test_cron_path_finds_homebrew_and_local_installs(self):
        rc, out = self.cron_run()
        self.assertEqual(rc, 0, out)
        path = self.path_seen.read_text().strip().split(os.pathsep)
        for entry in (str(self.home / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin"):
            self.assertIn(entry, path)
        self.assertLess(path.index("/opt/homebrew/bin"), path.index("/usr/bin"))

    def test_help_prints_the_header_and_runs_nothing(self):
        rc, out = self.cron_run("--help")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.splitlines(), _header(CRON))
        self.assertEqual(self.traced(), "")

    def test_a_symlinked_wrapper_runs_the_archive_sync_it_was_shipped_with(self):
        linked = self.root / "linkbin"
        linked.mkdir()
        (linked / "archive-sync-cron").symlink_to(os.path.relpath(CRON, linked))
        # No archive-sync on PATH: only resolving the link finds the real one,
        # which, with nothing configured, refuses with exit 3. Started by a
        # relative path, with an exported CDPATH that `cd` would search.
        (self.here / "archive-sync").unlink()
        proc = subprocess.run(["linkbin/archive-sync-cron"], cwd=str(self.root),
                              env=self.environment(env={"CDPATH": str(self.root)}),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              encoding="utf-8", errors="replace", timeout=120)
        rc, out = proc.returncode, proc.stdout
        self.assertEqual(rc, 3, out)
        self.assertIn("archive-sync: not configured", out)
        self.assertFalse((self.home / ".local").exists())


class TestArchiveSyncIdleLimit(_ArchiveSyncTest):
    """rclone's --timeout, which watches the destination's connections, is a
    setting (IO_TIMEOUT)."""

    def idle_limit(self, env=None):
        rc, out, _trace = self.sync("--backend", "fasrc", "--show-config", env=env)
        self.assertEqual(rc, 0, out)
        return next(line for line in out.splitlines() if line.startswith("idle limit:"))

    def test_it_is_two_minutes_unless_set(self):
        self.login_fakes()
        self.assertIn("2m", self.idle_limit())
        rc, out, trace = self.sync("--backend", "fasrc")
        self.assertEqual(rc, 0, out)
        self.assertIn("--timeout 2m ", self.rclone_calls(trace)[0])

    def test_the_config_sets_it_and_the_environment_wins(self):
        self.login_fakes()
        self.config.write_text(self.config.read_text() + "IO_TIMEOUT=5m\n")
        self.assertIn("5m", self.idle_limit())
        self.assertIn("90s", self.idle_limit(env={"ARCHIVE_SYNC_IO_TIMEOUT": "90s"}))
        rc, out, trace = self.sync("--backend", "fasrc",
                                   env={"ARCHIVE_SYNC_IO_TIMEOUT": "90s"})
        self.assertEqual(rc, 0, out)
        self.assertIn("--timeout 90s ", self.rclone_calls(trace)[0])

    def test_a_value_that_is_no_duration_is_a_configuration_error(self):
        self.login_fakes()
        rc, out, trace = self.sync("--backend", "fasrc",
                                   env={"ARCHIVE_SYNC_IO_TIMEOUT": "two minutes"})
        self.assertEqual(rc, 2, out)
        self.assertIn("IO_TIMEOUT is 'two minutes'", out)
        self.assertFalse(self.rclone_calls(trace))


if __name__ == "__main__":
    unittest.main(verbosity=2)
