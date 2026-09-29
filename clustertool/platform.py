"""Platform layer (Linux and macOS).

macOS ships neither ``timeout(1)``, ``setsid(1)`` nor ``flock(1)``, and its
``stat``/``ps``/``find`` take different flags. Python's stdlib covers all of that
portably: ``subprocess(timeout=)``, ``start_new_session=True``, ``fcntl.flock``,
``os.stat``, ``socket.getaddrinfo``. What genuinely differs between the two
operating systems is the FUSE tooling, the mount table format, the process
listing and the clock-offset probe, and everything that differs lives here.

Nothing that runs on the cluster is affected: remote commands always execute on
a Linux login node.
"""

from __future__ import annotations

import errno
import fcntl
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import config

IS_MAC = sys.platform == "darwin"

# CLUSTER_FORCE_PORTABLE=1 forces the fallback branch wherever one exists, so
# the macOS-only paths can be exercised on Linux.
FORCE_PORTABLE = config.global_value("FORCE_PORTABLE", "0") == "1"


#: How long a child killed at its deadline is given to be reaped. SIGKILL takes
#: effect at once on anything not in uninterruptible sleep; a child that is in
#: it is blocked in the kernel on something like a wedged FUSE mount, where no
#: signal reaches it until the kernel lets go, and waiting to reap it would
#: hang this process on the very thing its deadline was guarding against.
REAP_GRACE = 5.0


def run(
    argv,
    timeout=None,
    check=False,
    capture=True,
    stdin=subprocess.DEVNULL,
    env=None,
    cwd=None,
    text=True,
    idle=None,
    enough=None,
):
    """Run a command with a deadline. Returns CompletedProcess.

    On timeout the child is killed and returncode 124 is reported, matching
    ``timeout(1)`` so callers can treat the two identically. A killed child
    that cannot be reaped within REAP_GRACE is left behind rather than waited
    for, so a deadline always ends the call. A program that is missing is 127
    and one that cannot be executed 126, as in a shell. Text is UTF-8 with
    undecodable bytes replaced, so no output can raise.

    *idle* watches the child instead of timing it, for work whose length
    depends on the data: it runs for as long as it keeps printing, and is
    stopped (124 again) only after *idle* seconds in which it printed nothing.
    *enough*(data) may say that what has been read already answers the
    question; it is given what arrived since it was last asked, and the child
    is then stopped, the result being 0 with ``enough`` True. Both need
    *capture*; *timeout*, if given as well, still ends the call.
    """
    watched = idle is not None or enough is not None
    kwargs = dict(stdin=stdin, env=env, cwd=cwd)
    if text:
        kwargs.update(encoding="utf-8", errors="replace")
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    empty = "" if text else b""
    try:
        child = subprocess.Popen(argv, **kwargs)
    except FileNotFoundError:
        return _checked(subprocess.CompletedProcess(argv, 127, empty, empty), check)
    except PermissionError:
        return _checked(subprocess.CompletedProcess(argv, 126, empty, empty), check)
    if watched and capture:
        code, stdout, stderr, answered = _watch(child, timeout, idle, enough)
        proc = subprocess.CompletedProcess(argv, code, _decoded(stdout, text),
                                           _decoded(stderr, text))
        proc.enough = answered
        return _checked(proc, check)
    try:
        stdout, stderr = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill_and_reap(child)
        # What was read before the kill is attached as bytes even in text mode.
        proc = subprocess.CompletedProcess(argv, 124, _decoded(exc.stdout, text),
                                           _decoded(exc.stderr, text))
    except BaseException:
        # Interrupted (Ctrl-C, a terminating signal): the child goes too.
        _kill_and_reap(child)
        raise
    else:
        proc = subprocess.CompletedProcess(argv, child.returncode, stdout, stderr)
    return _checked(proc, check)


def _watch(child, timeout, idle, enough):
    """Read *child* until it exits, and stop it for silence or a deadline.

    ``(returncode, stdout bytes, stderr bytes, answered)``; see :func:`run`.
    The pipes are read as they fill, rather than by communicate(), so there
    is output to judge progress by, and the end is the child exiting with
    nothing left to read rather than end-of-file: an ssh riding a master
    leaves the master holding copies of the pipes, which may stay open after
    the session has ended.
    """
    # Only a watched run needs it, so not every command pays for the import.
    import selectors

    buffers = {child.stdout: bytearray(), child.stderr: bytearray()}
    out, err = buffers[child.stdout], buffers[child.stderr]
    selector = selectors.DefaultSelector()
    for pipe in buffers:
        selector.register(pipe, selectors.EVENT_READ)
    started = moved = time.monotonic()
    checked = 0
    try:
        while True:
            ready = selector.select(timeout=0.5) if selector.get_map() else ()
            now = time.monotonic()
            for key, _events in ready:
                data = os.read(key.fd, 65536)
                if data:
                    buffers[key.fileobj].extend(data)
                    moved = now
                else:
                    selector.unregister(key.fileobj)
            if ready and enough is not None and len(out) > checked:
                # What arrived since the last look, and a little from before
                # it, so that a short marker split across two reads is found.
                if enough(bytes(out[max(0, checked - 256):])):
                    _kill_and_reap(child)
                    return 0, bytes(out), bytes(err), True
                checked = len(out)
            if not ready and child.poll() is not None:
                break
            if not selector.get_map():
                try:
                    child.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
            if timeout is not None and now - started >= timeout:
                _kill_and_reap(child)
                return 124, bytes(out), bytes(err), False
            if idle is not None and now - moved >= idle:
                _kill_and_reap(child)
                return 124, bytes(out), bytes(err), False
    except BaseException:
        # Interrupted (Ctrl-C, a terminating signal): the child goes too.
        _kill_and_reap(child)
        raise
    finally:
        selector.close()
        for pipe in buffers:
            try:
                pipe.close()
            except OSError:
                pass
    return child.returncode, bytes(out), bytes(err), False


def _checked(proc, check):
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, proc.args, proc.stdout,
                                            proc.stderr)
    return proc


def stop(target, grace):
    """Stop *target*, a Popen or a pid: SIGTERM, so it can tidy up, then
    SIGKILL once it has had *grace* seconds (at once when *grace* is 0).

    A Popen is then reaped if the kernel lets it die within REAP_GRACE; one
    still there after that stays unreaped, and subprocess collects it later
    if it ever exits. Its pipes are its owner's. An interruption while it is
    given its grace, a second Ctrl-C, kills it before going on, so nothing is
    left running.
    """
    if isinstance(target, int):
        _stop_pid(target, grace)
        return
    child = target
    if child.poll() is not None:
        return
    try:
        if grace > 0:
            try:
                child.terminate()
                child.wait(timeout=grace)
                return
            except (OSError, subprocess.TimeoutExpired):
                pass
    except BaseException:
        _kill(child)
        raise
    _kill(child)


def _kill(child):
    try:
        child.kill()
    except OSError:
        pass
    try:
        child.wait(timeout=REAP_GRACE)
    except subprocess.TimeoutExpired:
        pass


def _stop_pid(pid, grace):
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + grace
    try:
        while time.monotonic() < deadline:
            if not pid_alive(pid):
                return
            time.sleep(0.2)
    finally:
        if pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def _kill_and_reap(child):
    """SIGKILL *child* and reap it if the kernel lets it die within REAP_GRACE.

    Never waits on its pipes: a grandchild may hold them open for as long as it
    lives. A child still there after the grace stays unreaped; subprocess
    collects it later if it ever exits.
    """
    _kill(child)
    for pipe in (child.stdout, child.stderr, child.stdin):
        if pipe is not None:
            try:
                pipe.close()
            except OSError:
                pass


def _decoded(value, text):
    if value is None:
        return "" if text else b""
    if text and isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def out(argv, timeout=None, **kw):
    """Stdout of a command, stripped; empty string on any failure."""
    proc = run(argv, timeout=timeout, **kw)
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def ok(argv, timeout=None, **kw):
    return run(argv, timeout=timeout, **kw).returncode == 0


def spawn_detached(argv, log_path=None, env=None):
    """Start a long-lived child in its own session. Returns its Popen.

    Detached into a new session with inherited descriptors closed
    (``close_fds=True``), so a daemon never holds the caller's lock or pipe.
    The Popen is what tells a child that exited from one that is running: a
    dead child stays a zombie, and so "alive" to kill(0), until it is polled.
    """
    log = subprocess.DEVNULL
    if log_path is not None:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        log = open(log_path, "ab", buffering=0)
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
    finally:
        if log is not subprocess.DEVNULL:
            log.close()
    return proc


class Terminated(BaseException):
    """A terminating signal (SIGTERM, SIGHUP), raised wherever the process is.

    Raised rather than obeyed, so every ``finally`` between there and the top
    still runs: a transfer master is closed, a lease or lock let go, a child
    stopped rather than left running with nothing tracking it. It is what
    Ctrl-C already gets. A BaseException and not a SystemExit, so that no
    ``except SystemExit`` that retries or carries on can swallow a request to
    stop.
    """

    def __init__(self, signum):
        super().__init__(signum)
        self.signum = signum
        #: The exit status a shell reports for a process this signal ended.
        self.code = 128 + signum


#: The signals that end a command by unwinding it.
ENDING_SIGNALS = (signal.SIGTERM, signal.SIGHUP)


def _raise_terminated(signum, _frame):
    raise Terminated(signum)


def unwind_on_signals(signals=ENDING_SIGNALS):
    """Make *signals* raise Terminated. Returns {signum: previous handler}.

    A signal this process was started ignoring stays ignored: under ``nohup``,
    SIGHUP is meant to change nothing. Only the main thread may set handlers,
    so anywhere else this does nothing.
    """
    previous = {}
    if threading.current_thread() is not threading.main_thread():
        return previous
    for signum in signals:
        try:
            if signal.getsignal(signum) == signal.SIG_IGN:
                continue
            previous[signum] = signal.signal(signum, _raise_terminated)
        except (OSError, ValueError):
            pass
    return previous


def restore_signals(previous):
    """Put back the handlers unwind_on_signals() replaced."""
    for signum, handler in previous.items():
        try:
            signal.signal(signum, handler)
        except (OSError, ValueError, TypeError):
            pass


class FileLock:
    """Exclusive lock held for the lifetime of this object.

    flock(2) locks the open file description, so the lock lives exactly as long
    as the descriptor. Python holds the fd itself, and it is not inheritable,
    so no subprocess can keep the lock alive after this object lets go.

    With *record_holder*, the holder writes its pid into the file, so that a
    process waiting for the lock can say what it is waiting for, and can tell
    a queue that is moving from one holder that is not.
    """

    def __init__(self, path, record_holder=False):
        self.path = Path(path)
        self.fd = None
        self.record_holder = record_holder
        #: Why acquire_queued gave up, when it did: ``("stopped", pid, seconds)``
        #: for a holder stopped that long, ``("still", pid, seconds)`` for a
        #: lock that sat that long with nothing moving.
        self.gave_up = None

    def acquire(self, wait=0.0):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + max(0.0, wait)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EWOULDBLOCK, errno.EACCES):
                        raise
                if time.monotonic() >= deadline:
                    os.close(fd)
                    return False
                time.sleep(0.1)
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        self._stamp()
        return True

    def acquire_queued(self, patience=None, announce=None, progress=None, poll=0.2,
                       stopped=None):
        """Take the lock, waiting for as long as other processes hold it.

        A held flock is a live process: the kernel drops the lock the moment
        its holder exits, however it exits, so nothing here can be stale and
        there is no deadline to guess. The work done under these locks is
        bounded by its own timeouts, so a held lock is work in progress, and
        giving up on it would only turn a slow step into a failed one.

        *patience*, when given, is how long the wait may go on with nothing
        moving before it stops: a queue that keeps changing hands is making
        progress however long it is, and only a lock that sits still is
        evidence of something stuck. What counts as movement is a change in
        *progress*() — by default the pid the holder recorded, which needs
        holders that record themselves (*record_holder*). *stopped*, when
        given, is how long a recorded holder may stay stopped (Ctrl-Z,
        SIGSTOP) before the wait gives up on it: a stopped process does no
        work and lets go of nothing until someone resumes it. *announce* is
        called once, with the holder's pid (or None), when the wait begins.
        True once held; after False, gave_up says why.
        """
        self.gave_up = None
        if self.acquire():
            return True
        progress = progress or self.holder
        if announce is not None:
            announce(self.holder())
        seen, since = progress(), time.monotonic()
        stopped_since = looked = None
        while not self.acquire(wait=poll):
            now = time.monotonic()
            current = progress()
            if current != seen:
                seen, since = current, now
            elif patience is not None and now - since >= patience:
                self.gave_up = ("still", self.holder(), now - since)
                return False
            if stopped is None or (looked is not None and now - looked < 1.0):
                continue
            # Looked at no more than once a second: on a Mac it costs a ps.
            looked = now
            holder = self.holder()
            if holder is None or not pid_stopped(holder):
                stopped_since = None
            elif stopped_since is None:
                stopped_since = (holder, now)
            elif stopped_since[0] != holder:
                stopped_since = (holder, now)
            elif now - stopped_since[1] >= stopped:
                self.gave_up = ("stopped", holder, now - stopped_since[1])
                return False
        return True

    def _stamp(self):
        if not self.record_holder:
            return
        try:
            os.ftruncate(self.fd, 0)
            os.pwrite(self.fd, f"{os.getpid()}\n".encode("ascii"), 0)
        except OSError:
            pass

    def holder(self):
        """The pid the current holder recorded, or None."""
        try:
            text = self.path.read_text(encoding="ascii", errors="replace").strip()
        except OSError:
            return None
        return int(text) if text.isdigit() else None

    def release(self):
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None

    def __enter__(self):
        if not self.acquire():
            raise BlockingIOError(f"could not lock {self.path}")
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def is_locked(path):
    """True if some other process holds the lock on *path*."""
    probe = FileLock(path)
    if probe.acquire():
        probe.release()
        return False
    return True


def file_mode(path):
    try:
        return oct(os.stat(path).st_mode & 0o777)[2:]
    except OSError:
        return "?"


def file_size(path):
    try:
        return os.stat(path).st_size
    except OSError:
        return 0


def stage_text(path, text, mode=0o600):
    """A synced file beside *path* holding *text*, for ``os.replace`` onto it.

    mkstemp creates it owner-only, and *mode* is set before anything is
    written, so a private key is never readable by anyone else, not even for
    the instant between a write and a chmod. The caller replaces *path* with
    it or removes it.
    """
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    staged = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return staged


def sync_directory(path):
    """Make the renames in directory *path* durable.

    A file fsync makes the contents durable; syncing the directory makes the
    rename durable too. Some portable filesystems reject directory fsync, in
    which case the atomicity still holds and there is nothing useful to retry.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_text(path, text, mode=None):
    """Durably replace a small text file without exposing partial contents.

    State such as a login's pinned node is a recovery record: an interrupted
    in-place ``write_text`` can truncate it and make the next process choose a
    different node, stranding remote tmux sessions.  Write and fsync a private
    sibling first, then atomically replace the destination and sync its directory.

    Existing permissions are preserved. New state defaults to owner-only because
    none of it needs to be shared and some files describe credential timing.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        try:
            mode = os.stat(path).st_mode & 0o777
        except OSError:
            mode = 0o600
    staged = stage_text(path, text, mode)
    try:
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)
    sync_directory(path.parent)


def dns_ok(host):
    """Resolve a hostname. ``socket`` needs no getent/dscacheutil split."""
    try:
        socket.getaddrinfo(host, 22, proto=socket.IPPROTO_TCP)
        return True
    except socket.gaierror:
        return False


def tcp_open(host, port=22, timeout=5.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def own_processes():
    """[(pid, full command line)] for this user's processes.

    Uses /proc where available (exact, no argv truncation); otherwise BSD ps
    with -U and -ww, since BSD ps means something else by -u and truncates argv
    to the terminal width, which silently breaks socket/monitor matching.
    """
    return [(pid, " ".join(argv)) for pid, argv in own_process_argv()]


def own_process_argv():
    """[(pid, argv)] for this user's processes: the words each was started
    with, exactly, from /proc. ps prints them joined by spaces, so there they
    are the command line split at whitespace, which is exact for every word
    that holds none (as an ssh option this tool writes never does)."""
    if not FORCE_PORTABLE and Path("/proc").is_dir():
        uid = os.getuid()
        result = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid != uid:
                    continue
                raw = (entry / "cmdline").read_bytes()
            except OSError:
                continue
            if not raw:
                continue
            argv = raw.rstrip(b"\0").decode("utf-8", "replace").split("\0")
            result.append((int(entry.name), argv))
        return result

    flag = "-U" if IS_MAC else "-u"
    text = out(["ps", flag, str(os.getuid()), "-ww", "-o", "pid=", "-o", "command="])
    result = []
    for line in text.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if pid.isdigit() and cmd.strip():
            result.append((int(pid), cmd.split()))
    return result


def process_args(pid):
    if not FORCE_PORTABLE and Path(f"/proc/{pid}/cmdline").exists():
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
            return raw.rstrip(b"\0").replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            return ""
    return out(["ps", "-ww", "-p", str(pid), "-o", "command="])


def describe_pid(pid, words=6):
    """``pid N (its command line, shortened)``, or "another process"."""
    if not pid:
        return "another process"
    what = " ".join(process_args(pid).split()[:words])
    return f"pid {pid} ({what})" if what else f"pid {pid}"


def pid_stopped(pid):
    """Whether *pid* is stopped (Ctrl-Z, SIGSTOP, a debugger): it holds what
    it holds, doing nothing, until something resumes it."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if not FORCE_PORTABLE and Path("/proc/self/stat").exists():
        try:
            stat = Path(f"/proc/{pid}/stat").read_bytes()
        except OSError:
            return False
        # The state follows the command name, which is in parentheses and
        # may itself hold any character, a ")" included.
        return stat[stat.rfind(b")") + 2:][:1] in (b"T", b"t")
    return out(["ps", "-o", "stat=", "-p", str(pid)])[:1] in ("T", "t")


def gave_up_text(lock, what):
    """Why a wait for *lock* (guarding *what*) gave up, as one line."""
    reason, pid, seconds = lock.gave_up or ("still", lock.holder(), 0)
    if reason == "stopped":
        return (f"{describe_pid(pid)} holds {what} and has been stopped for "
                f"{seconds:.0f}s (resume it with fg, or end it)")
    return (f"{describe_pid(pid)} has held {what} for {seconds:.0f}s with "
            "nothing moving; a process that is stopped or hung looks like this")


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # OverflowError: a number too large for a C int cannot be a live pid, but it
    # reaches here whenever a pid is parsed out of a filename rather than read
    # from the OS, and an uncaught one would take the caller down with it.
    except (ValueError, TypeError, OverflowError):
        return False


def _table_spellings(mountpoint):
    """The paths a mount table may list *mountpoint* under.

    The kernel records the canonical path, so a mountpoint below a symlinked
    directory (macOS's /tmp is /private/tmp) is listed with that directory
    resolved. Only the parent is resolved: resolving the mountpoint itself
    would stat it, and a wedged mount blocks every stat.
    """
    mp = os.path.abspath(str(mountpoint))
    parent, base = os.path.split(mp)
    return {mp, os.path.join(os.path.realpath(parent), base)}


def _mountinfo_fields(mountpoint):
    """The /proc/self/mountinfo fields for *mountpoint*, or None."""
    spellings = _table_spellings(mountpoint)
    try:
        text = Path("/proc/self/mountinfo").read_text(encoding="utf-8",
                                                      errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        fields = line.split()
        if len(fields) > 4 and _unescape_mount(fields[4]) in spellings:
            return fields
    return None


def mount_table_has(mountpoint):
    """Ask the kernel, never the filesystem.

    A wedged FUSE mount makes stat(2) block forever, so mount-point checks must
    read the mount table instead. mount(8)/mountinfo never touch the mount.
    """
    if not FORCE_PORTABLE and Path("/proc/self/mountinfo").exists():
        return _mountinfo_fields(mountpoint) is not None
    lines = [f"{line} " for line in out(["mount"], timeout=10).splitlines()]
    return any(f" on {mp} " in line
               for mp in _table_spellings(mountpoint) for line in lines)


def _unescape_mount(field):
    return (
        field.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def fuse_minor(mountpoint):
    """FUSE device minor for a mountpoint, for the abort path.

    mountinfo field 3 is ``major:minor`` — a colon, not a slash.
    """
    fields = _mountinfo_fields(mountpoint)
    if fields is None or ":" not in fields[2]:
        return None
    return fields[2].split(":", 1)[1]


def fuse_abort(mountpoint):
    """Abort a wedged FUSE connection so queued requests fail instead of hang.

    Without this, requests sit in ``request_wait_answer`` with SIGKILL pending
    and undeliverable, and every process that touches the mount joins the pile.
    Linux only: macOS publishes no FUSE connections to abort.
    """
    minor = fuse_minor(mountpoint)
    if minor is None:
        return False
    target = Path(f"/sys/fs/fuse/connections/{minor}/abort")
    try:
        target.write_text("1", encoding="ascii")
        return True
    except OSError:
        return False


def fuse_waiting(mountpoint):
    """Requests queued on the mount's FUSE connection, or None if unreadable.

    Only Linux publishes the queue (/sys/fs/fuse/connections). None means "no
    reading", which is not the same answer as an empty queue.
    """
    minor = fuse_minor(mountpoint)
    if minor is None:
        return None
    try:
        return int(Path(f"/sys/fs/fuse/connections/{minor}/waiting")
                   .read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def cpu_seconds(pids):
    """CPU time the processes *pids* have used between them, or None.

    Read with ``ps -o time=``, which Linux and macOS both print as
    ``[[dd-]hh:]mm:ss[.cc]``.
    """
    if not pids:
        return None
    text = out(["ps", "-o", "time=", "-p", ",".join(str(pid) for pid in pids)],
               timeout=10)
    readings = [_clock_seconds(word) for word in text.split()]
    readings = [value for value in readings if value is not None]
    return sum(readings) if readings else None


def _clock_seconds(text):
    days, _, clock = text.rpartition("-")
    try:
        seconds = 0.0
        for part in clock.split(":"):
            seconds = seconds * 60 + float(part)
        return seconds + (int(days) * 86400 if days else 0)
    except ValueError:
        return None


def unmount(mountpoint):
    """Release a FUSE mount, escalating until it lets go. True once it is gone.

    Linux: ``fusermount -u``, then the lazy ``-uz``, because a plain unmount
    hits EBUSY while anything still holds a descriptor (a VS Code file watcher
    is enough). macOS has no fusermount: ``umount``, then ``diskutil unmount
    force``, then ``umount -f``.
    """
    mp = str(mountpoint)
    if IS_MAC:
        attempts = (["umount", mp], ["diskutil", "unmount", "force", mp],
                    ["umount", "-f", mp])
    else:
        cmd = shutil.which("fusermount3") or shutil.which("fusermount")
        attempts = ([cmd, "-u", mp], [cmd, "-uz", mp]) if cmd else ()
    for argv in attempts:
        if not mount_table_has(mp):
            return True
        run(argv, timeout=20)
    return not mount_table_has(mp)


def have_unmount():
    if IS_MAC:
        return shutil.which("umount") is not None
    return shutil.which("fusermount3") is not None or shutil.which("fusermount") is not None


def sshfs_install_hint():
    """How to get what a mount needs on this operating system."""
    if IS_MAC:
        return ("install macFUSE and sshfs (https://github.com/macfuse/macfuse/"
                "wiki), then allow the macFUSE system extension in System "
                "Settings > Privacy & Security")
    return ("install sshfs (Debian/Ubuntu: apt install sshfs; Fedora/RHEL: "
            "dnf install fuse-sshfs)")


def mount_tools_missing():
    """The commands a mount needs that this machine lacks; [] when none are.

    sshfs everywhere, and on Linux the fusermount that releases a mount (macOS
    releases one with umount and diskutil, which it always has).
    """
    missing = [] if shutil.which("sshfs") else ["sshfs"]
    if not have_unmount():
        missing.append("fusermount")
    return missing


#: Size at which a log this tool appends to is rotated, keeping one previous
#: generation beside it as ``<name>.1``.
LOG_LIMIT = 1 << 20


def rotate_log(path, limit=None):
    """Move *path* to ``<path>.1`` once over *limit* (LOG_LIMIT) bytes. True if moved."""
    path = Path(path)
    if file_size(path) <= (LOG_LIMIT if limit is None else limit):
        return False
    try:
        os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        return False
    return True


_RSYNC_PROGRESS = None


def rsync_progress_flag():
    """``--info=progress2`` is rsync 3.1+; macOS ships 2.6.9 or openrsync."""
    global _RSYNC_PROGRESS
    if _RSYNC_PROGRESS is None:
        if not FORCE_PORTABLE and ok(["rsync", "--info=help"], timeout=10):
            _RSYNC_PROGRESS = "--info=progress2"
        elif "--progress" in out(["rsync", "--help"], timeout=10):
            _RSYNC_PROGRESS = "--progress"
        else:
            _RSYNC_PROGRESS = "-v"
    return _RSYNC_PROGRESS


_RSYNC_PARTIAL = None


def rsync_partial_flags():
    """What keeps a file cut short for the next run to finish, as this rsync
    spells it: ``--partial-dir=.rsync-partial`` where it has --partial-dir
    (rsync 2.6 and newer), else ``--partial``, else nothing.

    A relative partial directory is excluded from the transfer by rsync
    itself, so --delete never takes it, and a file cut short never stands
    in the destination as if it were whole. An rsync whose help cannot be
    read, one that fails or complains on stderr as openrsync may, is given
    neither.
    """
    global _RSYNC_PARTIAL
    if _RSYNC_PARTIAL is None:
        text = rsync_help()
        _RSYNC_PARTIAL = (["--partial-dir=.rsync-partial"] if "--partial-dir" in text
                          else ["--partial"] if "--partial" in text else [])
    return list(_RSYNC_PARTIAL)


def rsync_help():
    """What ``rsync --help`` prints, or "" when it fails or says anything on
    stderr: a probe that cannot be read offers nothing."""
    proc = run(["rsync", "--help"], timeout=10)
    if proc.returncode != 0 or (proc.stderr or "").strip():
        return ""
    return proc.stdout or ""


def clock_offset(ntp_server="pool.ntp.org"):
    """(offset_seconds, source) — TOTP breaks on a skewed clock.

    Returns (None, reason) when no probe is available.
    """
    if not FORCE_PORTABLE and shutil.which("timedatectl"):
        text = out(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], timeout=10)
        if text:
            return (0.0 if text.strip() == "yes" else None), "timedatectl"
    if shutil.which("sntp"):
        text = out(["sntp", ntp_server], timeout=20)
        match = re.search(r"([-+]\d+\.\d+)\s*\+/-", text)
        if match:
            return float(match.group(1)), f"sntp {ntp_server}"
    return None, "no clock probe available"


def terminal_attached():
    return sys.stdin.isatty() and sys.stdout.isatty()


# --- local terminal recovery -------------------------------------------------
# ssh restores tty *flags* after a disconnect, but it cannot undo terminal
# emulator modes that a remote full-screen program turned on. tmux enables mouse
# reporting; when the connection dies mid-session those modes stay set locally,
# and the next scroll or click arrives at your shell as literal text like
# "0;48;27M".
#
# Leaked *input* modes are the loud half. The quiet half is leaked *layout*
# state, which does not announce itself as garbage on screen — it just makes
# the terminal draw in the wrong place from then on. Auto-wrap left off is the
# worst of them: past the right margin every character lands on the same cell,
# so text piles up on itself and typing appears to overwrite a ghost. A scroll
# region left set confines everything to a band and runs the lines outside it
# together. Both hide until the window is narrow enough to reach the margin,
# which is why they surface on a resize.

#: Turn off, in order: X10 mouse (9), normal/hilite/button/any-event tracking
#: (1000-1003), focus reporting (1004), UTF-8/SGR/alternate-scroll/urxvt/pixel
#: mouse encodings (1005-1007, 1015, 1016), bracketed paste (2004), synchronized
#: output (2026), alternate-screen variants (47, 1047-1049), application cursor
#: keys (1), application keypad (ESC >), xterm modifyOtherKeys, and stacked kitty
#: keyboard enhancements. A local tmux is free to re-enable modes it owns after
#: reading these; this only removes state leaked by the remote client.
#:
#: Then put the drawing surface back: left/right margins (69) and the scroll
#: region, fenced in DECSC/DECRC because both home the cursor and the shell
#: prompt that follows should land where the session left off, not on top of
#: the top of the screen; then auto-wrap (7) on, insert mode off, the cursor
#: visible, attributes cleared and US-ASCII designated into G0 with G0 selected.
#: That tail has to come *after* DECRC, which restores the attributes, charset
#: and wrap flag that DECSC just captured from the broken state. Origin mode is
#: deliberately absent: resetting it homes the cursor too, and with the scroll
#: region back to the full screen its offset is already zero.
TERMINAL_RESET = (
    "\033[?9l\033[?1000l\033[?1001l\033[?1002l\033[?1003l\033[?1004l"
    "\033[?1005l\033[?1006l\033[?1007l\033[?1015l\033[?1016l"
    "\033[?2004l\033[?2026l\033[?47l\033[?1047l\033[?1048l\033[?1049l"
    "\033[?1l\033>\033[>4;0m" + ("\033[<u" * 8)
    + "\0337\033[?69l\033[r\0338"
    + "\033[?7h\033[4l\033[?25h\033[m\033(B\017"
)


#: Linux caps a process name at 16 bytes including the NUL (TASK_COMM_LEN).
PROC_NAME_MAX = 15


def set_process_name(name):
    """Rename this process, so `ps`/`top`/`pgrep` say what it is.

    Without this every cluster process shows up as a bare `python3`, which is
    useless when several are running and indistinguishable from any other Python
    on the machine. This sets the kernel's *comm* value (what `ps -o comm`,
    `pgrep`, `top` and `htop` display); the full command line is unchanged and
    still shows the arguments, and nothing in this tool matches on comm — the
    watcher is identified by its pid file and its argv — so renaming is safe.

    prctl(PR_SET_NAME) is Linux-only; elsewhere this is a silent no-op rather
    than an error, because a nicer label is never worth failing a command over.
    """
    if IS_MAC:
        return False
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        buf = ctypes.create_string_buffer(
            name.encode("ascii", "replace")[:PROC_NAME_MAX])
        return libc.prctl(15, ctypes.byref(buf), 0, 0, 0) == 0  # 15 = PR_SET_NAME
    except Exception:
        return False


def save_tty():
    """Snapshot the controlling terminal's attributes, or None if there is none."""
    try:
        import termios

        with open("/dev/tty", "rb", buffering=0) as tty:
            return (termios, termios.tcgetattr(tty.fileno()))
    except Exception:
        return None


#: Replies to the two size questions below.
_SIZE_REPLY = re.compile(rb"\033\[8;(\d+);(\d+)t")
_CURSOR_REPLY = re.compile(rb"\033\[(\d+);(\d+)R")


def _ask_terminal(handle, query, pattern, timeout):
    """Write `query` to a raw tty and read back the first match of `pattern`."""
    import select

    handle.write(query)
    handle.flush()
    deadline = time.monotonic() + timeout
    buf = b""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([handle], [], [], remaining)[0]:
            return None
        try:
            chunk = handle.read(64)
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
        found = pattern.search(buf)
        if found:
            return found


def terminal_report_size(timeout=0.3):
    """Ask the emulator itself how big it is: ``(columns, rows)`` or None.

    CSI 18 t is the direct question. Terminals that ignore it get the portable
    fallback: park the cursor far past the bottom right, where it clamps to the
    real last cell, and read back where it landed — fenced in DECSC/DECRC so the
    visible cursor never moves. Both are done in raw mode with a deadline, so a
    terminal that answers neither costs `timeout` and nothing else.
    """
    try:
        import termios
        import tty as ttymod
    except ImportError:
        return None
    try:
        handle = open("/dev/tty", "r+b", buffering=0)
    except OSError:
        return None
    saved = None
    try:
        saved = termios.tcgetattr(handle.fileno())
        ttymod.setraw(handle.fileno(), termios.TCSANOW)
        found = _ask_terminal(handle, b"\033[18t", _SIZE_REPLY, timeout)
        if not found:
            found = _ask_terminal(
                handle, b"\0337\033[9999;9999H\033[6n\0338",
                _CURSOR_REPLY, timeout)
        if not found:
            return None
        rows, columns = int(found.group(1)), int(found.group(2))
        return (columns, rows) if columns > 0 and rows > 0 else None
    except Exception:
        return None
    finally:
        if saved is not None:
            try:
                termios.tcsetattr(handle.fileno(), termios.TCSADRAIN, saved)
            except Exception:
                pass
        handle.close()


def resync_terminal_size(timeout=0.3, reported=None):
    """Make the kernel's window size agree with the emulator's again.

    Returns the corrected ``(columns, rows)``, or None when nothing was wrong
    or the terminal would not answer.

    The pty's idea of the window size and the emulator's can drift apart: a
    resize that arrived while a full-screen program owned the alternate screen,
    or a pty still carrying the size a previous session ended at. Everything
    downstream then wraps at the wrong column — text piles onto the last cell
    and the line editor redraws in the wrong place, which is what "typing
    overwrites a ghost character" is. Nothing here can notice by inspecting the
    pty, because the pty is the thing that is wrong; only the emulator knows.
    So ask it, and write the answer back where the kernel keeps it.
    """
    reported = reported or terminal_report_size(timeout)
    if not reported:
        return None
    columns, rows = reported
    try:
        import termios

        with open("/dev/tty", "r+b", buffering=0) as handle:
            packed = fcntl.ioctl(handle, termios.TIOCGWINSZ, b"\0" * 8)
            have_rows, have_columns = struct.unpack("HHHH", packed)[:2]
            if (have_rows, have_columns) == (rows, columns):
                return None
            fcntl.ioctl(handle, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, columns, 0, 0))
    except (OSError, ValueError, ImportError, struct.error):
        return None
    return (columns, rows)


def sane_tty():
    """Put the local terminal's *flags* back to sane defaults.

    restore_tty() replays a snapshot, which is the right thing around a
    session — the flags were fine when it started. It is useless when the flags
    themselves are what broke: a program killed while in raw mode leaves echo
    off and newlines untranslated, and there is no good snapshot to go back to.
    `stty sane` is the portable spelling of "whatever this platform considers
    normal", and it is the half of the repair TERMINAL_RESET cannot do, because
    tty flags live in the kernel while the modes above live in the emulator.
    """
    try:
        with open("/dev/tty", "r+b", buffering=0) as tty:
            return subprocess.run(
                ["stty", "sane"], stdin=tty,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ).returncode == 0
    except (OSError, ValueError):
        return False


def restore_tty(saved=None):
    """Put the local terminal back the way it was, then clear emulator modes.

    Safe to call when there is no tty, when nothing was saved, and repeatedly —
    it is used both after each disconnect and once before connecting, to clear
    modes an earlier crashed session left behind.
    """
    try:
        handle = open("/dev/tty", "r+b", buffering=0)
    except OSError:
        return False
    try:
        if saved is not None:
            termios, attrs = saved
            try:
                # TCSADRAIN, not TCSANOW: let pending output flush first, or the
                # tail of the remote session's writes is lost.
                termios.tcsetattr(handle.fileno(), termios.TCSADRAIN, attrs)
            except Exception:
                pass
        try:
            handle.write(TERMINAL_RESET.encode())
            handle.flush()
        except OSError:
            pass
    finally:
        handle.close()
    return True
