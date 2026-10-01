#!/usr/bin/env python3
"""Platform: locks, subprocesses, terminal repair and process names.

Run: python3 -m unittest tests.test_platform
(or the whole suite: python3 -m unittest discover -s tests -p 'test_*.py')
"""

from __future__ import annotations

import errno
import subprocess
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

# For `support`, which puts this checkout on sys.path and sets up the sandbox
# every test runs in, so it is imported before anything from clustertool.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import REPO_ROOT, _patched  # noqa: E402
from clustertool import platform as plat  # noqa: E402


class TestLocking(unittest.TestCase):
    def test_lock_excludes_a_second_holder_and_survives_child_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "l"
            first = plat.FileLock(path)
            self.assertTrue(first.acquire())
            second = plat.FileLock(path)
            self.assertFalse(second.acquire())
            self.assertTrue(plat.is_locked(path))
            self.assertIsNone(second.holder(), "an unrecorded lock names no holder")
            # Children must not inherit the lock; plat.run starts them with
            # close_fds=True, so no child holds the lock's descriptor.
            self.assertEqual(plat.run(["true"]).returncode, 0)
            self.assertTrue(plat.is_locked(path))
            first.release()
            self.assertTrue(second.acquire())
            second.release()
            self.assertFalse(plat.is_locked(path))


class TestWaitingForAHeldLock(unittest.TestCase):
    """A held flock is a live holder, so it is waited for, and its holder named."""

    HOLD = ("import fcntl, os, sys, time\n"
            "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "os.ftruncate(fd, 0); os.write(fd, b'%d\\n' % os.getpid())\n"
            "print('held', flush=True)\n"
            "time.sleep(float(sys.argv[2]))\n")

    def hold(self, path, seconds):
        import subprocess

        child = subprocess.Popen([sys.executable, "-c", self.HOLD, str(path),
                                  str(seconds)], stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.assertEqual(child.stdout.readline().strip(), "held")
        return child

    def stop(self, child):
        """SIGSTOP *child*, and wait until it shows as stopped: the kernel
        delivers the signal in its own time."""
        import signal

        os.kill(child.pid, signal.SIGSTOP)
        self.addCleanup(os.kill, child.pid, signal.SIGCONT)
        deadline = time.monotonic() + 10
        while not plat.pid_stopped(child.pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(plat.pid_stopped(child.pid))

    def test_the_wait_lasts_as_long_as_the_holder_does(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "l"
            child = self.hold(path, 0.5)
            told = []
            lock = plat.FileLock(path, record_holder=True)
            started = time.monotonic()
            self.assertTrue(lock.acquire_queued(announce=told.append))
            self.assertGreaterEqual(time.monotonic() - started, 0.3)
            self.assertEqual(told, [child.pid])
            self.assertEqual(path.read_text(), f"{os.getpid()}\n")
            lock.release()

    def test_patience_runs_out_on_one_holder_that_keeps_the_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "l"
            self.hold(path, 30)
            lock = plat.FileLock(path)
            started = time.monotonic()
            self.assertFalse(lock.acquire_queued(patience=0.3))
            self.assertLess(time.monotonic() - started, 10)
            self.assertIsNone(lock.fd)

    def test_a_queue_that_changes_hands_keeps_its_patience(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "l"
            first = self.hold(path, 30)
            lock = plat.FileLock(path)
            turns = {"n": 0}

            def holder():
                # Each look sees a different pid, as a moving queue would.
                turns["n"] += 1
                if turns["n"] > 8:
                    first.kill()
                return 100000 + turns["n"]

            # Eight hands at 0.05s a look outlast the patience, which each
            # change of hands renews.
            with _patched(lock, "holder", holder):
                self.assertTrue(lock.acquire_queued(patience=0.3, poll=0.05))
            lock.release()

    def test_a_stopped_holder_is_given_up_on_and_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "l"
            child = self.hold(path, 30)
            self.stop(child)
            lock = plat.FileLock(path, record_holder=True)
            started = time.monotonic()
            self.assertFalse(lock.acquire_queued(stopped=1.0))
            self.assertLess(time.monotonic() - started, 10)
            reason, pid, seconds = lock.gave_up
            self.assertEqual((reason, pid), ("stopped", child.pid))
            self.assertGreaterEqual(seconds, 1.0)
            told = plat.gave_up_text(lock, "the lock")
            self.assertIn(f"pid {child.pid}", told)
            self.assertIn("has been stopped for", told)

    def test_a_holder_resumed_in_time_is_waited_for(self):
        import signal
        import threading

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "l"
            child = self.hold(path, 0.6)
            os.kill(child.pid, signal.SIGSTOP)
            resume = threading.Timer(0.3, os.kill, (child.pid, signal.SIGCONT))
            resume.start()
            self.addCleanup(resume.cancel)
            lock = plat.FileLock(path, record_holder=True)
            self.assertTrue(lock.acquire_queued(stopped=5.0))
            self.assertIsNone(lock.gave_up)
            lock.release()

    def test_ps_says_stopped_by_a_signal_or_by_a_debugger(self):
        self.assertFalse(plat.pid_stopped(os.getpid()))
        self.assertFalse(plat.pid_stopped(None))
        # Where there is no /proc: T is Ctrl-Z or SIGSTOP, t a tracer's stop.
        for stat, stopped in (("T", True), ("T+", True), ("t", True),
                              ("S+", False), ("R", False), ("", False)):
            with self.subTest(stat=stat), \
                    _patched(plat, "FORCE_PORTABLE", True), \
                    _patched(plat, "out", lambda argv, s=stat, **_kw: s):
                self.assertEqual(plat.pid_stopped(4242), stopped)


class TestSignalsUnwind(unittest.TestCase):
    """SIGTERM and SIGHUP end a command the way Ctrl-C does: by unwinding."""

    def test_a_terminating_signal_runs_every_finally(self):
        import signal
        import subprocess

        script = ("import sys, time\n"
                  "from clustertool import platform as plat\n"
                  "plat.unwind_on_signals()\n"
                  "try:\n"
                  "    print('ready', flush=True)\n"
                  "    time.sleep(30)\n"
                  "except SystemExit:\n"
                  "    print('swallowed', flush=True)\n"
                  "except plat.Terminated as ended:\n"
                  "    print('unwound', ended.code, flush=True)\n")
        child = subprocess.Popen([sys.executable, "-c", script], cwd=str(REPO_ROOT),
                                 stdout=subprocess.PIPE, text=True)
        self.assertEqual(child.stdout.readline().strip(), "ready")
        child.send_signal(signal.SIGHUP)
        out, _ = child.communicate(timeout=20)
        self.assertEqual(out.strip(), f"unwound {128 + signal.SIGHUP}")

    def test_the_entry_point_exits_with_the_signal_status(self):
        import signal
        import subprocess

        script = ("import runpy, sys, time\n"
                  "import clustertool.cli as cli\n"
                  "def main():\n"
                  "    try:\n"
                  "        print('ready', flush=True)\n"
                  "        time.sleep(30)\n"
                  "    finally:\n"
                  "        print('cleaned up', flush=True)\n"
                  "cli.main = main\n"
                  "sys.argv = ['cluster']\n"
                  "runpy.run_path(%r, run_name='__main__')\n"
                  % str(REPO_ROOT / "bin" / "cluster"))
        child = subprocess.Popen([sys.executable, "-c", script], cwd=str(REPO_ROOT),
                                 stdout=subprocess.PIPE, text=True)
        self.assertEqual(child.stdout.readline().strip(), "ready")
        child.send_signal(signal.SIGTERM)
        out, _ = child.communicate(timeout=20)
        self.assertEqual(out.strip(), "cleaned up")
        self.assertEqual(child.returncode, 128 + signal.SIGTERM)

    def test_restoring_puts_back_what_was_there_and_an_ignored_hangup_stays_so(self):
        import signal

        for signum in (signal.SIGTERM, signal.SIGHUP):
            self.addCleanup(signal.signal, signum, signal.getsignal(signum))
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        previous = plat.unwind_on_signals()
        self.assertIsNot(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)
        self.assertNotIn(signal.SIGHUP, previous)
        self.assertEqual(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)
        plat.restore_signals(previous)
        self.assertIs(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)


class TestTerminalRecovery(unittest.TestCase):
    """What a crashed remote full-screen program leaves on the local terminal."""

    def test_leaked_input_and_layout_modes_are_cleared(self):
        reset = plat.TERMINAL_RESET
        for mode in ("\033[?1000l", "\033[?1006l", "\033[?2004l", "\033[?1049l"):
            self.assertIn(mode, reset)
        # Auto-wrap left off is the one that reads as "characters overlay and
        # repeat, and typing replaces a ghost": past the right margin every
        # character lands on the same cell. A scroll region left set confines
        # output to a band and runs the lines outside it together. Neither is
        # visible until the window is narrow enough to reach the margin.
        self.assertIn("\033[?7h", reset)     # DECAWM
        self.assertIn("\033[r", reset)       # DECSTBM, full screen
        self.assertIn("\033[?69l", reset)    # left/right margins
        self.assertIn("\033[4l", reset)      # insert mode
        self.assertIn("\033[?25h", reset)    # cursor visibility
        self.assertIn("\033[m", reset)       # attributes
        self.assertIn("\033(B", reset)       # US-ASCII into G0
        self.assertIn("\017", reset)         # and G0 selected

    def test_the_cursor_homing_resets_are_fenced_and_survive_the_fence(self):
        reset = plat.TERMINAL_RESET
        save, restore = reset.index("\0337"), reset.index("\0338")
        # DECSTBM and margin mode home the cursor, so without the fence the
        # shell prompt that follows would land on top of the top of the screen.
        self.assertLess(save, reset.index("\033[r"))
        self.assertLess(reset.index("\033[r"), restore)
        # DECRC restores the attributes, charset and wrap flag that DECSC just
        # captured from the broken state, so those repairs come after it.
        for repair in ("\033[?7h", "\033[m", "\033(B"):
            self.assertGreater(reset.index(repair), restore)

    def test_repairing_a_terminal_needs_no_backend_or_login(self):
        # The program that wedged the terminal is usually not one of ours, so
        # the repair must not require a connection to be reachable first.
        from clustertool.cli import COMMANDS

        self.assertFalse(COMMANDS["fixterm"].needs_context)
        self.assertFalse(COMMANDS["fixterm"].needs_login)

    def test_sane_flags_and_restoring_are_safe_without_a_terminal(self):
        # Restoring is called both after every disconnect and once before
        # connecting.
        self.assertIn(plat.sane_tty(), (True, False))
        self.assertIn(plat.restore_tty(plat.save_tty()), (True, False))


class TestTerminalSizeResync(unittest.TestCase):
    """A pty and its emulator can disagree about the window size.

    That disagreement is what "typing overwrites a ghost character" is: every
    wrap lands at the wrong column, so text piles onto the last cell and the
    line editor redraws in the wrong place. Nothing can spot it by inspecting
    the pty, because the pty is the thing that is wrong — only the emulator
    knows. It also survives detaching, because the staleness is local.
    """

    CHILD = (
        "import sys; sys.path.insert(0, %r)\n"
        "import fcntl, struct, termios\n"
        "from clustertool import platform as plat\n"
        "print('SIZE', plat.resync_terminal_size(%r), flush=True)\n"
        "with open('/dev/tty', 'rb') as handle:\n"
        "    print('KERNEL', struct.unpack('HHHH',\n"
        "          fcntl.ioctl(handle, termios.TIOCGWINSZ, b'\\0' * 8))[:2],\n"
        "          flush=True)\n"
    )

    def _run(self, answer, timeout=0.6):
        """Run the resync in a pty whose master side plays the emulator."""
        import fcntl
        import pty
        import select
        import struct
        import termios

        root = str(REPO_ROOT)
        pid, fd = pty.fork()
        if pid == 0:                                    # pragma: no cover
            os.execvp(sys.executable,
                      [sys.executable, "-c", self.CHILD % (root, timeout)])
        # The pty claims 24x80. The emulator will say it is really 40x100.
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        out, deadline, replied = b"", time.monotonic() + 30, False
        while time.monotonic() < deadline:
            if not select.select([fd], [], [], 0.1)[0]:
                continue
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
            if not replied and answer == "size" and b"\033[18t" in out:
                os.write(fd, b"\033[8;40;100t")
                replied = True
            elif not replied and answer == "cursor" and b"\033[6n" in out:
                os.write(fd, b"\033[40;100R")
                replied = True
        os.waitpid(pid, 0)
        os.close(fd)
        return out.decode("utf-8", "replace")

    def test_the_emulators_answer_corrects_the_pty_and_silence_changes_nothing(self):
        # CSI 18 t answered; or unanswered, and the fallback parks the cursor
        # past the bottom right and reads back where it clamped; or nothing,
        # which costs the deadline and nothing else, never a hang or a guess.
        for answer, size, kernel in (("size", "(100, 40)", "(40, 100)"),
                                     ("cursor", "(100, 40)", "(40, 100)"),
                                     ("none", "None", "(24, 80)")):
            with self.subTest(answer=answer):
                out = self._run(answer, timeout=0.3)
                self.assertIn(f"SIZE {size}", out)
                self.assertIn(f"KERNEL {kernel}", out)


class TestPlatform(unittest.TestCase):
    def test_mount_table_lookup_finds_root(self):
        self.assertTrue(plat.mount_table_has("/"))
        self.assertFalse(plat.mount_table_has("/definitely/not/mounted"))

    def test_own_processes_are_listed_by_their_words(self):
        # /bin/sh, not sys.executable: macOS's /usr/bin/python3 is a shim that
        # runs another binary, which ps then reports instead. Two commands, so
        # the shell cannot exec itself into the last one.
        child = subprocess.Popen(["/bin/sh", "-c", "sleep 30; :", "sh", "one word"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.assertEqual(dict(plat.own_processes())[child.pid], " ".join(child.args))
        words = dict(plat.own_process_argv())[child.pid]
        exact = not plat.FORCE_PORTABLE and Path("/proc").is_dir()
        self.assertEqual(words, child.args if exact else " ".join(child.args).split())

    def test_dns_and_tcp_helpers_are_safe_on_garbage(self):
        def unknown(*_args, **_kwargs):
            raise plat.socket.gaierror(plat.socket.EAI_NONAME, "Name or service not known")

        with _patched(plat.socket, "getaddrinfo", unknown):
            self.assertFalse(plat.dns_ok("no-such-host.invalid"))
        self.assertTrue(plat.dns_ok("localhost"))
        self.assertFalse(plat.tcp_open("127.0.0.1", 1, timeout=0.5))

    def test_atomic_write_replaces_the_file_in_utf8_or_keeps_the_old_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state"
            path.write_text("old\n")

            def fail_replace(_source, _target):
                raise OSError(errno.EIO, "injected replace failure")

            with _patched(plat.os, "replace", fail_replace):
                with self.assertRaises(OSError):
                    plat.atomic_write_text(path, "new\n")
            self.assertEqual(path.read_text(), "old\n")
            self.assertEqual(list(path.parent.glob(".state.*")), [])
            # UTF-8 whatever the locale.
            plat.atomic_write_text(path, "café\n")
            self.assertEqual(path.read_bytes(), "café\n".encode("utf-8"))


class TestRun(unittest.TestCase):
    """plat.run never raises over what a command printed or how it ended."""

    def test_output_read_before_a_timeout_is_text(self):
        proc = plat.run([sys.executable, "-c",
                         "import sys, time; print('partial', flush=True); "
                         "time.sleep(5)"], timeout=0.3)
        self.assertEqual(proc.returncode, 124)
        self.assertIsInstance(proc.stdout, str)
        self.assertIn("partial", proc.stdout)

    def test_bytes_that_are_not_utf8_are_replaced(self):
        proc = plat.run([sys.executable, "-c",
                         "import sys; sys.stdout.buffer.write(b'ok \\xff\\xfe')"])
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "ok ��")

    def test_a_missing_or_unexecutable_program_is_a_status_not_a_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "not-executable"
            script.write_text("#!/bin/sh\n")
            script.chmod(0o644)
            self.assertEqual(plat.run([str(script)]).returncode, 126)
        self.assertEqual(plat.run(["definitely-not-a-real-binary"]).returncode, 127)
        proc = plat.run(["true"], capture=False)
        self.assertEqual(proc.returncode, 0)
        self.assertIsNone(proc.stdout, "left uncaptured when asked")

    def test_a_child_that_cannot_be_reaped_does_not_hold_the_caller(self):
        """A child blocked in the kernel ignores SIGKILL until it is let go."""
        import subprocess

        killed = []

        class Wedged:
            def __init__(self, argv, **_kw):
                self.args, self.returncode = argv, None
                self.stdout = self.stderr = self.stdin = None

            def communicate(self, timeout=None):
                raise subprocess.TimeoutExpired(self.args, timeout, output=b"so far")

            def kill(self):
                killed.append(True)

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired(self.args, timeout)

        with _patched(plat.subprocess, "Popen", Wedged), \
                _patched(plat, "REAP_GRACE", 0.1):
            proc = plat.run(["stat", "/wedged/mount"], timeout=1)
        self.assertEqual(proc.returncode, 124)
        self.assertEqual(proc.stdout, "so far")
        self.assertEqual(killed, [True])

    def test_an_interrupted_run_takes_its_child_with_it(self):
        import signal
        import subprocess

        started = []
        real_popen = subprocess.Popen

        def popen(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            started.append(child)
            return child

        def interrupt(_signum, _frame):
            raise KeyboardInterrupt

        before = signal.signal(signal.SIGALRM, interrupt)
        try:
            with _patched(plat.subprocess, "Popen", popen):
                signal.setitimer(signal.ITIMER_REAL, 0.2)
                with self.assertRaises(KeyboardInterrupt):
                    plat.run([sys.executable, "-c", "import time; time.sleep(30)"])
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, before)
        self.assertIsNotNone(started[0].poll(), "the child was left running")


def _python(code):
    return [sys.executable, "-c", code]


class TestWatchedRun(unittest.TestCase):
    """plat.run judged by what the child does, not by how long it takes."""

    def test_a_child_that_keeps_printing_runs_past_its_idle_limit(self):
        started = time.monotonic()
        proc = plat.run(_python("import time\nfor i in range(6):\n"
                                "    print(i, flush=True); time.sleep(0.1)"),
                        idle=0.3)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.split(), [str(i) for i in range(6)])
        self.assertGreater(time.monotonic() - started, 0.45)
        self.assertFalse(proc.enough)

    def test_a_silent_child_is_stopped_after_its_idle_limit(self):
        started = time.monotonic()
        proc = plat.run(_python("import time; print('so far', flush=True); "
                                "time.sleep(30)"), idle=0.2)
        self.assertEqual(proc.returncode, 124)
        self.assertEqual(proc.stdout, "so far\n")
        self.assertLess(time.monotonic() - started, 5)

    def test_a_deadline_still_ends_a_child_that_keeps_printing(self):
        proc = plat.run(_python("import time\nwhile True:\n"
                                "    print('.', flush=True); time.sleep(0.05)"),
                        timeout=0.3, idle=5)
        self.assertEqual(proc.returncode, 124)

    def test_enough_stops_the_child_once_the_answer_is_in_even_split_across_reads(self):
        seen = []

        def enough(data):
            seen.append(data)
            return b"FOUND" in data

        started = time.monotonic()
        proc = plat.run(_python("import sys, time\nprint('a', flush=True)\n"
                                "time.sleep(0.1); print('FOUND', flush=True)\n"
                                "time.sleep(30)"), idle=60, enough=enough)
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(proc.enough)
        self.assertIn("FOUND", proc.stdout)
        self.assertEqual(seen[0], b"a\n")
        proc = plat.run(_python("import sys, time\nsys.stdout.write('FOU')\n"
                                "sys.stdout.flush(); time.sleep(0.1)\n"
                                "print('ND', flush=True); time.sleep(30)"),
                        idle=60, enough=lambda data: b"FOUND" in data)
        self.assertTrue(proc.enough)
        self.assertLess(time.monotonic() - started, 20)

    def test_without_the_answer_the_childs_own_ending_stands(self):
        proc = plat.run(_python("import sys; print('rows'); sys.exit(3)"),
                        idle=5, enough=lambda data: False)
        self.assertEqual(proc.returncode, 3)
        self.assertFalse(proc.enough)
        self.assertEqual(proc.stdout, "rows\n")

    def test_the_end_is_the_child_exiting_not_its_pipes_closing(self):
        """What an ssh riding a master does: the master keeps copies of them."""
        started = time.monotonic()
        proc = plat.run(_python("import subprocess\n"
                                "print(subprocess.Popen(['sleep', '30']).pid)"),
                        idle=20)
        holder = int(proc.stdout.split()[0])
        try:
            self.assertEqual(proc.returncode, 0)
            self.assertLess(time.monotonic() - started, 10)
        finally:
            os.kill(holder, 9)


class TestMountTableSpellings(unittest.TestCase):
    def test_a_symlinked_parent_is_resolved_but_never_the_mountpoint(self):
        # The kernel lists a mount by its canonical path. Resolving the
        # mountpoint itself would stat it, and a wedged mount blocks every stat.
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp, "real")
            real.mkdir()
            Path(tmp, "link").symlink_to(real)
            spellings = plat._table_spellings(Path(tmp, "link", "gpu"))
            self.assertIn(str(Path(tmp, "link", "gpu")), spellings)
            self.assertIn(os.path.join(os.path.realpath(real), "gpu"), spellings)
            Path(tmp, "mp").symlink_to(real)
            spellings = plat._table_spellings(Path(tmp, "mp"))
            self.assertNotIn(os.path.realpath(real), spellings)


class TestLogRotation(unittest.TestCase):
    def test_a_log_over_the_limit_keeps_one_previous_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "watch-main.log"
            self.assertFalse(plat.rotate_log(log, limit=0), "a missing log is fine")
            log.write_text("x" * 20)
            self.assertFalse(plat.rotate_log(log, limit=100))
            self.assertTrue(plat.rotate_log(log, limit=10))
            self.assertFalse(log.exists())
            self.assertEqual(Path(tmp, "watch-main.log.1").read_text(), "x" * 20)
            log.write_text("y" * 20)
            self.assertTrue(plat.rotate_log(log, limit=10))
            self.assertEqual(Path(tmp, "watch-main.log.1").read_text(), "y" * 20,
                             "only one generation is kept")


class TestMacBranches(unittest.TestCase):
    """The macOS branches, driven on any machine.

    plat.IS_MAC is patched on, the /proc paths are forced off, and mount,
    umount, diskutil and ps are fakes on PATH that print what macOS prints
    and record how they were called.
    """

    FAKES = {
        "mount": '#!/bin/sh\ncat "$MOUNTS" 2>/dev/null\n',
        "umount": '#!/bin/sh\necho "umount $*" >> "$TRACE"\n'
                  '[ "${RELEASE_BY-}" = "umount $*" ] && : > "$MOUNTS"\n'
                  'exit "${UMOUNT_RC:-1}"\n',
        "diskutil": '#!/bin/sh\necho "diskutil $*" >> "$TRACE"\n'
                    '[ "${RELEASE_BY-}" = "diskutil $*" ] && : > "$MOUNTS"\n'
                    'exit 0\n',
        "ps": '#!/bin/sh\necho "ps $*" >> "$TRACE"\ncat "$PS_OUT"\n',
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self.tmp.name))
        fakes = self.root / "bin"
        fakes.mkdir()
        for name, body in self.FAKES.items():
            (fakes / name).write_text(body)
            (fakes / name).chmod(0o755)
        self.mounts = self.root / "mounts.txt"
        self.trace = self.root / "trace"
        self.ps_out = self.root / "ps.txt"
        self.env = {"PATH": f"{fakes}{os.pathsep}{os.environ['PATH']}",
                    "MOUNTS": str(self.mounts), "TRACE": str(self.trace),
                    "PS_OUT": str(self.ps_out)}
        self.saved = {key: os.environ.get(key) for key in self.env}
        os.environ.update(self.env)
        self.patches = [_patched(plat, "IS_MAC", True),
                        _patched(plat, "FORCE_PORTABLE", True)]
        for patch in self.patches:
            patch.__enter__()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.__exit__(None, None, None)
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        os.environ.pop("RELEASE_BY", None)
        os.environ.pop("UMOUNT_RC", None)
        self.tmp.cleanup()

    def mounted(self, *paths):
        self.mounts.write_text("".join(
            f"user@host:. on {path} (macfuse, nodev, nosuid, synchronous, "
            f"mounted by user)\n" for path in paths))

    def calls(self):
        return self.trace.read_text().splitlines() if self.trace.exists() else []

    def test_the_mount_table_is_read_from_mount8(self):
        mp = self.root / "cluster_mounts" / "fasrc" / "main"
        spaced = self.root / "cluster mounts" / "sp ace"
        self.mounted(mp, spaced)
        self.assertTrue(plat.mount_table_has(mp))
        self.assertTrue(plat.mount_table_has(spaced))
        self.assertFalse(plat.mount_table_has(mp.with_name("mai")),
                         "a prefix of a mounted path is not mounted")

    def test_a_mount_under_a_symlinked_directory_is_found(self):
        # macOS lists /tmp/x as /private/tmp/x; any symlinked parent does this.
        (self.root / "private").mkdir()
        (self.root / "tmp").symlink_to(self.root / "private")
        self.mounted(self.root / "private" / "gpu")
        self.assertTrue(plat.mount_table_has(self.root / "tmp" / "gpu"))

    def test_there_is_no_fuse_queue_to_read_or_abort(self):
        mp = self.root / "main"
        self.mounted(mp)
        self.assertIsNone(plat.fuse_waiting(mp))
        self.assertFalse(plat.fuse_abort(mp))

    def test_unmount_escalates_and_stops_at_the_first_form_that_releases_it(self):
        mp = self.root / "main"
        self.mounted(mp)
        self.assertFalse(plat.unmount(mp), "nothing released it")
        self.assertEqual(self.calls(), [f"umount {mp}",
                                        f"diskutil unmount force {mp}",
                                        f"umount -f {mp}"])
        self.trace.unlink()
        os.environ["RELEASE_BY"] = f"diskutil unmount force {mp}"
        self.assertTrue(plat.unmount(mp))
        self.assertEqual(self.calls(), [f"umount {mp}",
                                        f"diskutil unmount force {mp}"])

    def test_the_process_list_is_bsd_ps_by_uid_and_untruncated(self):
        self.ps_out.write_text(f"  {os.getpid()} /usr/bin/python3 cluster monitor main\n"
                               "  77 sshfs -o volname=x user@h:. /mp\n")
        self.assertEqual(plat.own_processes(), [
            (os.getpid(), "/usr/bin/python3 cluster monitor main"),
            (77, "sshfs -o volname=x user@h:. /mp")])
        call, = self.calls()
        self.assertIn(f"-U {os.getuid()}", call, "BSD ps means something else by -u")
        self.assertIn("-ww", call, "BSD ps truncates argv to the terminal width")

    def test_cpu_time_is_read_in_the_bsd_format(self):
        self.ps_out.write_text("   0:01.50\n   1:02.25\n")
        self.assertAlmostEqual(plat.cpu_seconds([11, 12]), 63.75)
        self.assertEqual(self.calls(), ["ps -o time= -p 11,12"])
        self.assertIsNone(plat.cpu_seconds([]))

    def test_the_process_name_is_left_alone_and_the_install_hint_names_macfuse(self):
        self.assertFalse(plat.set_process_name("cluster:w:main"))
        self.assertIn("macFUSE", plat.sshfs_install_hint())


class TestClockSeconds(unittest.TestCase):
    def test_linux_and_macos_forms(self):
        for text, seconds in (("00:00:07", 7), ("1:02:03", 3723),
                              ("0:01.50", 1.5), ("2-00:00:01", 172801)):
            with self.subTest(text=text):
                self.assertAlmostEqual(plat._clock_seconds(text), seconds)
        self.assertIsNone(plat._clock_seconds("TIME"))


class TestProcessLabel(unittest.TestCase):
    """`ps` should say which login a cluster process belongs to, not 'python3'."""

    def setUp(self):
        from clustertool import platform as plat, processname

        self.processname, self.plat = processname, plat

    def label(self, *argv):
        return self.processname.process_label(argv[0], list(argv[1:]))

    def test_every_label_fits_the_kernel_field(self):
        # Anything longer is silently truncated by prctl, so the truncation has
        # to happen here where we can choose what survives.
        for argv in (("attach", "main"), ("attach", "averyverylonglogin",
                                          "averyverylongsession"),
                     ("monitor", "a-long-login-name"), ("run", "x" * 40),
                     ("new-session", "work", "y" * 40), ("list",)):
            with self.subTest(argv=argv):
                self.assertLessEqual(len(self.label(*argv)), self.plat.PROC_NAME_MAX)

    def test_what_each_command_is_labelled(self):
        for argv, want in (
                # Both names, when both names fit — which, at 7 characters,
                # means short ones. This is the widest pair the field can hold.
                (("attach", "gpu", "api"), "cluster:gpu/api"),
                (("n", "gpu", "api"), "cluster:gpu/api"),
                # `attach main` lands on session 'main'; "cluster:main/main"
                # wastes the budget saying it twice.
                (("attach", "main"), "cluster:main"),
                # When the pair does not fit the login is shortened, not
                # dropped: dropping it would make `main/dev` and `gpu/api` look
                # like two different schemes, when the only difference is one
                # character of name length.
                (("a", "production", "api"), "cluster:pro/api"),
                (("attach", "main", "dev"), "cluster:mai/dev"),
                (("attach", "main", "dev2"), "cluster:ma/dev2"),
                # The login shrinks to a letter before it disappears.
                (("a", "work", "shell"), "cluster:w/shell"),
                # The one case with no room for the login at all. The session
                # is the specific half, so it is what survives.
                (("a", "work", "buildall"), "cluster:buildal"),
                (("shell", "work"), "cluster:work"), (("mount", "work"), "cluster:work"),
                # Watchers are long-lived and unattended, so they are the
                # processes most likely to be found in `ps` by someone
                # wondering what they are.
                (("monitor", "work"), "cluster:w:work"),
                (("watch", "work"), "cluster:w:work"),
                # Flags are not mistaken for names, nor a remote command for
                # a session: in `cluster run main -- echo hi`, 'echo' is not one.
                (("attach", "-q", "work"), "cluster:work"),
                (("run", "main", "--", "echo", "hi"), "cluster:main"),
                (("list",), "cluster"), (("status",), "cluster"),
                (("attach",), "cluster"), (("doctor",), "cluster")):
            with self.subTest(argv=argv):
                self.assertEqual(self.label(*argv), want)
        # Filling from the right should leave no slack: a short session must
        # buy a longer login, not a shorter name.
        for session in ("api", "dev2", "shell"):
            with self.subTest(session=session):
                self.assertEqual(
                    len(self.label("a", "a-long-login-name", session)),
                    self.plat.PROC_NAME_MAX)

    def test_setting_the_name_does_not_raise(self):
        # It is cosmetic: on a platform without prctl it must decline quietly
        # rather than take the command down with it.
        self.assertIn(self.plat.set_process_name("cluster:test"), (True, False))

    @unittest.skipUnless(os.path.exists("/proc/self/comm"), "Linux only")
    def test_the_name_actually_reaches_the_kernel(self):
        with open("/proc/self/comm") as handle:
            was = handle.read().strip()
        try:
            self.assertTrue(self.plat.set_process_name("cluster:test"))
            with open("/proc/self/comm") as handle:
                self.assertEqual(handle.read().strip(), "cluster:test")
        finally:
            self.plat.set_process_name(was)


class TestStoppingAProcess(unittest.TestCase):
    """plat.stop: a SIGTERM, a SIGKILL after the grace, and nothing left running."""

    STUBBORN = ("import signal, sys, time; signal.signal(signal.SIGTERM, "
                "signal.SIG_IGN); print('up', flush=True); time.sleep(60)")

    def start(self, script):
        import subprocess

        child = subprocess.Popen([sys.executable, "-c", script],
                                 stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.stdout.close)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        self.assertEqual(child.stdout.readline().strip(), "up")
        return child

    def test_a_child_or_a_pid_that_ignores_the_term_is_killed_after_the_grace(self):
        for by_pid in (False, True):
            with self.subTest(by_pid=by_pid):
                child = self.start(self.STUBBORN)
                started = time.monotonic()
                plat.stop(child.pid if by_pid else child, 0.2)
                child.wait(timeout=5)
                self.assertEqual(child.returncode, -9)
                self.assertLess(time.monotonic() - started, 5)

    def test_a_second_interrupt_during_the_grace_still_kills_it(self):
        import signal

        child = self.start(self.STUBBORN)

        def interrupt(*_a):
            raise KeyboardInterrupt

        previous = signal.signal(signal.SIGALRM, interrupt)
        self.addCleanup(signal.signal, signal.SIGALRM, previous)
        signal.setitimer(signal.ITIMER_REAL, 0.15)
        with self.assertRaises(KeyboardInterrupt):
            plat.stop(child, 30)
        self.assertIsNotNone(child.poll(), "the child was left running")


if __name__ == "__main__":
    unittest.main(verbosity=2)
