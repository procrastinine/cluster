"""Helpers shared by the unit test modules, and the sandbox every test runs in.

Not a test module itself: discovery collects only test_*.py. Importing it puts
this checkout first on sys.path, so every test module imports this clustertool
however it is run: by discovery, as tests.test_<subject>, or as a script.

Importing it also moves the whole run into a sandbox (below). That is why every
test module imports it before anything from clustertool.
"""

from __future__ import annotations

import atexit
import contextlib
import functools
import io
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

#: The checkout the tests belong to.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

#: Sourced by the completion tests, and by the NODES test that checks the
#: completion reads the same setting.
COMPLETION_SCRIPT = REPO_ROOT / "completions" / "cluster.bash"

# --- the operating system the suite runs on ------------------------------------
# Most macOS behaviour is tested everywhere, by patching platform.IS_MAC: the
# decisions are plain Python. A test that asks the real system something only
# one of them has (getfsstat, BSD ps, a Linux /proc, a shell's startup file)
# carries one of these, so each platform runs what it can answer.
IS_MACOS = sys.platform == "darwin"
MACOS_ONLY = unittest.skipUnless(IS_MACOS, "asks macOS itself")
LINUX_ONLY = unittest.skipUnless(sys.platform.startswith("linux"),
                                 "asks Linux itself")

# --- the sandbox -------------------------------------------------------------
# The suite never touches the machine that runs it: not its credentials,
# settings, state, ~/.ssh or mounts, and not the environment that names them.
# A choice made in the runner's settings (VSCODE_TAB_TITLE = 1, say) must not
# change what a test sees, and a test must never write where the runner's own
# `cluster` would look.
#
# clustertool fixes its paths from HOME and the environment when it is
# imported (config.HOME, SETTINGS_FILE, STATE_ROOT, CTL_DIR, MOUNT_ROOT,
# CRED_ROOT, platform.FORCE_PORTABLE and more). Tests also start bash, sh and
# Python children that read those again. So the sandbox is set up here, once
# per run, before clustertool is imported and before any child starts, and
# the children inherit it. Tests that need settings or other paths patch them
# on top of it.
#
# XDG_CONFIG_HOME, XDG_STATE_HOME and the other base directories are unset,
# not pointed into the sandbox, so they follow HOME. A test that gives a child
# its own HOME (the completion, archive-sync and linger tests) then gives it
# its own settings and state as well.

if any(name == "clustertool" or name.startswith("clustertool.")
       for name in sys.modules):
    raise RuntimeError(
        "tests/support.py was imported after clustertool, which has already "
        "fixed its paths from the real HOME; import support first")

#: The runner's settings for this tool, its companions and the XDG base
#: directories. Every one of them names a real place.
_RUNNER_PREFIXES = ("CLUSTER_", "XDG_", "ARCHIVE_SYNC_", "NERSC_")
#: Kept on purpose: the portable run sets it.
_KEPT = frozenset({"CLUSTER_FORCE_PORTABLE"})
#: The runner's live ssh-agent, tmux session and session bus (a tmux built
#: with systemd support asks the bus to put each pane in a scope of its own).
_RUNNER_SESSION = frozenset({"SSH_AUTH_SOCK", "SSH_AGENT_PID", "TMUX", "TMUX_PANE",
                             "DBUS_SESSION_BUS_ADDRESS"})

#: Scratch space for the whole run, removed when the run exits.
SANDBOX = Path(tempfile.mkdtemp(prefix="cluster-tests-"))
#: HOME for the whole run, in this process and in every child.
HOME = SANDBOX / "home"
_RUNTIME = SANDBOX / "runtime"
_SANDBOX_OWNER = os.getpid()


def _remove_sandbox():
    # A forked child that exits normally runs atexit handlers too, and the
    # sandbox belongs to the process that made it.
    if os.getpid() == _SANDBOX_OWNER:
        shutil.rmtree(SANDBOX, ignore_errors=True)


atexit.register(_remove_sandbox)


def _outside(path, home):
    # Lexical on purpose: resolving a path under HOME would read that HOME.
    return os.path.commonpath([home, os.path.abspath(path)]) != home


def _enter_sandbox():
    """Point this process's environment, and so every child's, at the sandbox."""
    HOME.mkdir(mode=0o700)
    _RUNTIME.mkdir(mode=0o700)
    real_home = os.path.abspath(os.path.expanduser("~"))
    for key in list(os.environ):
        if (key.startswith(_RUNNER_PREFIXES) and key not in _KEPT) \
                or key in _RUNNER_SESSION:
            del os.environ[key]
    os.environ["HOME"] = str(HOME)
    # Programs installed under the runner's HOME (~/.local/bin/cluster, say)
    # are not the ones under test, and even looking for them reads that HOME.
    if real_home != os.path.dirname(real_home):
        os.environ["PATH"] = os.pathsep.join(
            entry for entry in os.environ.get("PATH", os.defpath).split(os.pathsep)
            if entry and _outside(entry, real_home))
    # Unlike the base directories, this one has no default under HOME, and
    # without it the nersc companion falls back to /tmp. Any tmux started by
    # code under test gets its own server here instead of joining the runner's.
    os.environ["XDG_RUNTIME_DIR"] = str(_RUNTIME)
    os.environ["TMUX_TMPDIR"] = str(_RUNTIME)


_enter_sandbox()


def _loopback(host):
    host = str(host or "").strip("[]").lower()
    return host == "localhost" or host == "::1" or host.startswith("127.")


#: Every host something in this run tried to reach, in order: the unit suite
#: has no network (tests/live_check.sh is what talks to real clusters).
REACHED = []


def _no_network():
    """Keep code under test on this machine.

    Resolving or connecting to any host but loopback fails as it would with
    no network, and the test that did it fails too (_offline_test), naming the
    host: a path a test leaves unpatched (an sshproxy fetch, a reachability
    probe) would otherwise reach a real cluster with the sandbox's made-up
    credentials, or pass only because this machine happens to be offline.
    """
    import socket

    resolve, connect = socket.getaddrinfo, socket.create_connection

    def getaddrinfo(host, *args, **kwargs):
        if not _loopback(host):
            REACHED.append(str(host))
            raise socket.gaierror(socket.EAI_NONAME,
                                  f"the test suite has no network ({host})")
        return resolve(host, *args, **kwargs)

    def create_connection(address, *args, **kwargs):
        if not _loopback(address[0]):
            REACHED.append(str(address[0]))
            raise OSError(101, f"the test suite has no network ({address[0]})")
        return connect(address, *args, **kwargs)

    socket.getaddrinfo, socket.create_connection = getaddrinfo, create_connection

    run = unittest.TestCase.run

    def offline_run(self, result=None):
        # Added before the test's own cleanups, so it runs after them all.
        self.addCleanup(_offline_test, self, len(REACHED))
        return run(self, result)

    unittest.TestCase.run = offline_run


def _offline_test(test, before):
    reached = REACHED[before:]
    if reached:
        test.fail(f"reached for {', '.join(sorted(set(reached)))}: the unit suite "
                  "has no network; patch that path, or check it in "
                  "tests/live_check.sh against a real cluster")


_no_network()

from clustertool import config as _config  # noqa: E402
from clustertool.backends import BACKENDS as _BACKENDS  # noqa: E402

#: What every backend finds in its credential directory, so load("fasrc") and
#: load("nersc") work as they do on an enrolled machine. None of it is a secret.
FAKE_USER = "tester"
FAKE_PASSWORD = "not-a-password"
#: A well-formed base32 TOTP seed that belongs to nobody.
FAKE_TOTP_SEED = "JBSWY3DPEHPK3PXP"


def _enrol_every_backend():
    """Write each backend's credential files the way an enrolled machine has them."""
    root = _config.CRED_ROOT
    if HOME not in root.parents:
        raise RuntimeError(f"credentials resolve outside the sandbox: {root}")
    root.mkdir(parents=True)
    root.chmod(_config.SECRET_DIR_MODE)
    for name in _BACKENDS:
        directory = root / name
        directory.mkdir()
        directory.chmod(_config.SECRET_DIR_MODE)
        for filename, text in (("user", FAKE_USER), ("pass", FAKE_PASSWORD),
                               ("key.txt", FAKE_TOTP_SEED)):
            fd = os.open(directory / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                         _config.SECRET_MODE)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text + "\n")


_enrol_every_backend()


# --- helpers -----------------------------------------------------------------

@contextlib.contextmanager
def _patched(obj, attr, value):
    original = getattr(obj, attr)
    setattr(obj, attr, value)
    try:
        yield
    finally:
        setattr(obj, attr, original)


class FakeBackend:
    name = "fake"
    user = "u"


def _refusal(callable_):
    """Run something that must refuse; return what the user was told."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            callable_()
        except SystemExit:
            return err.getvalue()
    raise AssertionError("expected a refusal")


def short_dir(test, prefix="t"):
    """A new directory under /tmp for *test*, removed after it.

    For a real unix socket, such as a tmux server's: TMPDIR on macOS lies deep
    under /var/folders, and a socket path there can pass the 104 bytes a
    sun_path holds.
    """
    path = Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))
    test.addCleanup(shutil.rmtree, str(path), True)
    return path


@functools.lru_cache(maxsize=None)
def usable_rclone():
    """The rclone a transfer would pick if it is new enough, or None."""
    from clustertool import transfer

    found = transfer.find_rclone(types.SimpleNamespace(str=lambda _key: ""))
    return found.path if found.problem is None else None


def temp_state(test):
    """Point STATE_ROOT, CTL_DIR and MOUNT_ROOT into a new temp dir for *test*.

    Returns the temp dir, with CTL_DIR already created. The paths are restored
    and the dir removed when *test* finishes, after its tearDown.
    """
    from clustertool import config

    stack = contextlib.ExitStack()
    test.addCleanup(stack.close)
    root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
    for attr, sub in (("STATE_ROOT", "state"), ("CTL_DIR", "ctl"),
                      ("MOUNT_ROOT", "mounts")):
        stack.enter_context(_patched(config, attr, root / sub))
    config.CTL_DIR.mkdir(parents=True)
    return root


class _IsolatedMachine(unittest.TestCase):
    """Config, state and credentials in a temp dir, and no identity from the env."""

    ENV = ("CLUSTER_USER", "CLUSTER_FASRC_USER", "CLUSTER_NERSC_USER",
           "CLUSTER_BACKEND", "CLUSTER_CRED_DIR", "CLUSTER_FASRC_CRED_DIR",
           "CLUSTER_NERSC_CRED_DIR")

    def setUp(self):
        from clustertool import config, setup

        self.config = config
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.saved_env = {key: os.environ.pop(key, None) for key in self.ENV}
        self.stack = contextlib.ExitStack()
        for attr, sub in (("STATE_ROOT", "state"), ("CTL_DIR", "ctl"),
                          ("MOUNT_ROOT", "mounts"), ("CRED_ROOT", "credentials")):
            self.stack.enter_context(_patched(config, attr, self.root / sub))
        self.stack.enter_context(
            _patched(config, "SETTINGS_FILE", self.root / "settings.ini"))
        self.stack.enter_context(_patched(setup, "warn_if_local_drift", lambda: None))

    def tearDown(self):
        self.stack.close()
        for key, value in self.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def enrol(self, backend, user="someone"):
        directory = self.config.CRED_ROOT / backend
        directory.mkdir(parents=True)
        (directory / "user").write_text(user + "\n")

    def record_login(self, backend, name):
        directory = self.config.STATE_ROOT / backend
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name}.json").write_text("{}\n")

    def run_cli(self, *argv):
        from clustertool import cli

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(list(argv))
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else 1
                err.write(str(exc.code if not isinstance(exc.code, int) else ""))
        return rc, out.getvalue(), err.getvalue()


# --- a clock for code that waits ---------------------------------------------
class FakeClock:
    """A `time` for code that waits: monotonic(), time() and sleep(), with no
    real waiting.

    Patched in as a module's ``time`` (``_patched(module, "time", clock)``), so
    the code under test keeps its own settings and loop shape. Every sleep is
    remembered in *slept* and moves the clock on. With *real*, each sleep also
    lets that many real seconds pass, for a wait on something that runs
    meanwhile (a lock held by another process).
    """

    def __init__(self, now=1000000.0, real=0.0):
        self.now = now
        self.real = real
        self.slept = []

    def monotonic(self):
        return self.now

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds
        if self.real:
            import time

            time.sleep(self.real)


# --- a process holding a lock ------------------------------------------------
def hold_flock(test, path, seconds, record_pid=True):
    """A child process holding the flock at *path* for *seconds*, its pid
    written there unless *record_pid* is false; stopped at *test*'s cleanup."""
    import subprocess

    script = ("import fcntl, os, sys, time\n"
              "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)\n"
              "fcntl.flock(fd, fcntl.LOCK_EX)\n"
              "if sys.argv[3] == '1':\n"
              "    os.ftruncate(fd, 0); os.write(fd, b'%d\\n' % os.getpid())\n"
              "print('held', flush=True)\n"
              "time.sleep(float(sys.argv[2]))\n")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(path), str(seconds),
         "1" if record_pid else "0"],
        stdout=subprocess.PIPE, universal_newlines=True)
    test.addCleanup(child.stdout.close)
    test.addCleanup(child.wait)
    test.addCleanup(child.kill)
    test.assertEqual(child.stdout.readline().strip(), "held")
    return child
