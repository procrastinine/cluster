"""Bulk data movement with rclone over sftp.

Design decisions, each for a reason:

* **A transfer opens its own connection by default.** A login's master gets only
  about ten sshd channels and already spends some on the mount, the watcher and
  tmux; rclone wants one channel per transfer, one per checker and one for its
  shell commands. ``--via LOGIN`` rides the login's master instead, at reduced
  concurrency.
* **The transfer connection carries no login state.** No pin, no ledger entry, no
  breadcrumb: storage is shared across the pool on both clusters, so a transfer
  has no node affinity. Its socket deliberately sits outside the login socket
  glob so login machinery ignores it.
* **Concurrency is bounded by the channel budget, not by rclone's defaults.**
  ``transfers + checkers + 1`` is what rclone actually asks for, and
  ``--multi-thread-streams`` must stay at 1 or a single large file quietly takes
  several channels while holding its transfer slot.
* **Path semantics are cp/rsync-like, not rclone-like.** rclone's ``copy`` always
  means "contents of", which silently flattens a directory into its parent. Here
  a source directory lands *inside* DEST unless it ends in ``/``.
* **Leases keep concurrent transfers from cutting each other off.** Each run
  holds an flock on a per-PID lease file, so tearing down a shared connection
  waits for the last user — and a run that dies leaves no lease behind, because
  the kernel drops the lock however the process ended.
* **A transfer that exits 0 has delivered the bytes.** rclone is honest about
  the errors it sees, but not every way of moving nothing is an error it sees,
  so the destination is checked afterwards; see :func:`verify`.
"""

from __future__ import annotations

import collections
import contextlib
import json
import os
import shutil
import socket
import tempfile
import time
from pathlib import Path

from . import platform as plat, ui
from .remote_sh import sftp_path
from .riding import Link, Ride
from .sshmux import discard, master_pid, rclone_ssh_value, rider_argv

#: The first rclone with --sftp-ssh, which every transfer here is built on.
MIN_RCLONE = (1, 64)

#: Where rclone is looked for after PATH, in order: the official installer's
#: home, Homebrew's on Apple silicon, and the distribution's.
RCLONE_FALLBACKS = ("/usr/local/bin/rclone", "/opt/homebrew/bin/rclone",
                    "/usr/bin/rclone")

#: What :func:`find_rclone` found. *path* is None when nothing was, *version*
#: is ``(major, minor)``, and *problem* is None, "not found", "did not report
#: an rclone version", or "X.Y is too old (need 1.64)".
RcloneFound = collections.namedtuple("RcloneFound", "path version problem")

#: rclone exit codes that mean "the path is not there" rather than "the question
#: could not be answered". Everything else non-zero is a failed probe.
RCLONE_ABSENT = (3, 4)  # directory not found, file not found

#: rclone's exit status for an error it did not categorise.
RCLONE_UNCATEGORISED = 1

#: Older rclones report a missing path as an uncategorised error, so with that
#: status rclone's own last word is read as well. Only then: the same words
#: come from a shell that cannot find rclone (127), from ssh (255) and from an
#: ssh warning printed before rclone ran, and none of those is an absence.
ABSENT_TEXT = ("directory not found", "object not found", "file not found",
               "no such file")

#: Flags that make "nothing arrived" a deliberate outcome. With one of these in
#: play a missing destination is reported but not treated as a failure.
FILTER_FLAGS = frozenset("""
    --exclude --exclude-from --filter --filter-from --files-from --include
    --include-from --ignore-existing --max-age --max-size --max-transfer
    --min-age --min-size
""".split())


def probe_limits(settings):
    """How the path questions of a transfer are bounded, from *settings*.

    A stat is one round trip, so it is timed (TRANSFER_PROBE_TIMEOUT) and
    asked again when unanswered (TRANSFER_PROBE_TRIES). A listing or a walk
    takes as long as the tree it reads, so it is never timed: it is stopped
    only after TRANSFER_IO_TIMEOUT seconds in which it printed nothing.
    """
    return dict(timeout=settings.int("TRANSFER_PROBE_TIMEOUT"),
                tries=settings.int("TRANSFER_PROBE_TRIES"),
                idle=settings.int("TRANSFER_IO_TIMEOUT"))

#: Set by a probe that could not reach the cluster at all. Deliberately distinct
#: from None ("definitely not there"): reading a failed probe as "not a
#: directory" silently turns "copy into DIR" into "rename to DIR", and rclone
#: reports that as success.
UNKNOWN = object()

#: flock handles for the leases this process holds, keyed by lease file. Kept at
#: module scope rather than on the instance because one process can reach the
#: same tag through two Transfers objects (crossxfer hands a connection to
#: relay), and both must mean the same lease.
_LEASE_LOCKS = {}


# --- rclone itself ------------------------------------------------------------
def version_text(version):
    return ".".join(str(part) for part in version)


def parse_rclone_version(text):
    """``(major, minor)`` from what ``rclone version`` prints, or None."""
    for line in (text or "").splitlines():
        if line.startswith("rclone v"):
            try:
                return tuple(int(part) for part in
                             line.split()[1].lstrip("v").split(".")[:2])
            except ValueError:
                return None
    return None


def rclone_version(binary):
    """*binary*'s ``(major, minor)``, or None if it does not run as an rclone.

    --config /dev/null: asking for a version must not read the user's rclone
    config, which may be password-encrypted and would then prompt.
    """
    try:
        text = plat.out([binary, "--config", "/dev/null", "version"], timeout=20)
    except OSError:
        return None
    return parse_rclone_version(text)


#: The first rclone that takes known_hosts_file "none"; 1.74 opens a file of
#: that name and fails.
QUIET_HOST_KEY_RCLONE = (1, 75)

_host_key_flags = {}


def host_key_flags(binary):
    """Flags that stop *binary* noting it does not check host keys itself.

    It is not meant to: every rclone here rides ssh through --sftp-ssh, and
    ssh checks the host key (StrictHostKeyChecking and the site's
    known_hosts). From 1.75 rclone still prints "No host key validation is
    being performed" on every run unless known_hosts_file is "none".
    """
    if binary not in _host_key_flags:
        version = rclone_version(binary)
        _host_key_flags[binary] = (
            ["--sftp-known-hosts-file", "none"]
            if version and version >= QUIET_HOST_KEY_RCLONE else [])
    return list(_host_key_flags[binary])


def find_rclone(settings):
    """The rclone this machine would use, as an :data:`RcloneFound`.

    Never exits: "not found" and "too old" are answers, and each caller decides
    what they mean. An explicit RCLONE setting is the only candidate, and is
    checked like any other. Otherwise the one on PATH comes first and the usual
    homes of the official binary follow it, since a distribution's package is
    often too old for --sftp-ssh.
    """
    explicit = settings.str("RCLONE")
    if explicit:
        candidates = [explicit]
    else:
        found = shutil.which("rclone")
        candidates = [found] if found else []
        candidates += [path for path in RCLONE_FALLBACKS
                       if path not in candidates and os.access(path, os.X_OK)]
    too_old = unusable = None
    for candidate in candidates:
        version = rclone_version(candidate)
        if version is None:
            # There but not answering as an rclone (a broken install, or some
            # other program by that name): named, so it is not taken for absent.
            if unusable is None and os.path.exists(candidate):
                unusable = RcloneFound(candidate, None,
                                       "did not report an rclone version")
            continue
        if version >= MIN_RCLONE:
            return RcloneFound(candidate, version, None)
        too_old = too_old or RcloneFound(
            candidate, version,
            f"{version_text(version)} is too old (need {version_text(MIN_RCLONE)})")
    return too_old or unusable or RcloneFound(explicit or None, None, "not found")


def channel_ssh(backend, settings, sock, node=None):
    """The ssh command another program opens its channels with, via *sock*.

    It never becomes a master (ControlMaster=no) and never prompts
    (BatchMode=yes): with the master gone it fails, rather than authenticate
    behind the caller's back.
    """
    return rider_argv(sock, backend.target(node), [
        "-o", "BatchMode=yes",
        "-o", "ServerAliveInterval="
              f"{settings.int('EXTERNAL_SSH_SERVER_ALIVE_INTERVAL')}",
        "-o", "ServerAliveCountMax="
              f"{settings.int('EXTERNAL_SSH_SERVER_ALIVE_COUNT_MAX')}"])


def concurrency(settings, transfers=0, checkers=0, shared=False):
    """``(transfers, checkers)``: what was asked for, or the settings'.

    A login's master has far fewer channels to spare than a dedicated
    connection, so riding one (*shared*) reads the SHARED_TRANSFER_* values.
    """
    prefix = "SHARED_TRANSFER_" if shared else "TRANSFER_"
    return (transfers or settings.int(prefix + "TRANSFERS"),
            checkers or settings.int(prefix + "CHECKERS"))


def symlink_flags(symlinks):
    # rclone's sftp backend follows symlinks regardless; only the local side
    # is controllable, so --skip-links is the only real escape.
    return {"follow": ["--copy-links"], "keep": ["--links"]}.get(
        symlinks, ["--skip-links"])


def rclone_flags(settings, transfers, checkers, *, dry_run=False, progress=False,
                 quiet=False, symlinks="follow", extra=(), log_stats=False):
    """The flags every rclone run here carries, from the TRANSFER_* settings.

    *log_stats* prints a one-line summary every 30 seconds when there is no
    progress display, which reads well in the log of a long unattended run.
    """
    connections = (settings.int("TRANSFER_CONNECTIONS")
                   or transfers + checkers + 1)
    argv = ["--transfers", str(transfers),
            "--checkers", str(checkers),
            "--sftp-connections", str(connections),
            # One stream per file by default: rclone's own default of 4 turns
            # each transfer slot into four channel requests for a large file.
            "--multi-thread-streams",
            str(settings.int("TRANSFER_MULTI_THREAD_STREAMS")),
            "--timeout", f"{settings.int('TRANSFER_IO_TIMEOUT')}s",
            "--retries", str(settings.int("TRANSFER_RETRIES") + 1)]
    if dry_run:
        argv.append("--dry-run")
    if quiet:
        # ssh does the host key validation, so rclone's notice about not
        # doing it is misleading; drop it along with the stats noise.
        argv += ["--stats", "0", "--log-level", "ERROR"]
    elif progress:
        argv.append("--progress")
    elif log_stats:
        # Stats log at INFO, which the default NOTICE level would hide.
        argv += ["--stats", "30s", "--stats-one-line",
                 "--stats-log-level", "NOTICE"]
    return argv + symlink_flags(symlinks) + list(extra)


def sftp_arg(path):
    """rclone's on-the-fly sftp remote for *path*, home-relative."""
    return f":sftp,shell_type=unix:{sftp_path(path)}"


# --- the two sides of a transfer ----------------------------------------------
def _says_absent(proc):
    """Whether a failed rclone stat said that the path is not there."""
    if proc.returncode in RCLONE_ABSENT:
        return True
    if proc.returncode != RCLONE_UNCATEGORISED:
        return False
    lines = (proc.stderr or "").strip().splitlines()
    last = lines[-1].lower() if lines else ""
    return any(text in last for text in ABSENT_TEXT)


def _kind(stat):
    """True/False for a directory, None if not there, UNKNOWN if unanswered."""
    if stat is None or stat is UNKNOWN:
        return stat
    return bool(stat.get("IsDir"))


class LocalOps:
    """The side of a transfer this machine can answer for itself.

    Every side answers the same questions — :class:`RcloneOps` asks them of a
    cluster — so the path semantics and the delivery check are written once:
    ``stat`` gives rclone's stat dict or None, ``listing`` a directory's
    ``{name: stat}``, and ``holds_no_files`` whether a tree has nothing to copy.
    """

    where = "here"

    def expand(self, path):
        return str(Path(path).expanduser())

    def arg(self, path):
        return self.expand(path)

    def stat(self, path, tries=1):
        target = Path(self.expand(path))
        try:
            info = target.stat()
        except OSError:
            return None
        return {"IsDir": target.is_dir(), "Size": info.st_size}

    #: Nothing here is worth remembering: asking again costs no round trip.
    probe = stat

    def exists(self, path):
        return self.stat(path) is not None

    def is_dir(self, path):
        return _kind(self.stat(path))

    def listing(self, path):
        try:
            children = list(Path(self.expand(path)).iterdir())
        except OSError:
            return None
        return {child.name: self.stat(str(child)) for child in children}

    def holds_no_files(self, path):
        unreadable = []
        for _root, _dirs, files in os.walk(self.expand(path),
                                           onerror=unreadable.append):
            if files:
                return False
        return not unreadable


class RcloneOps:
    """One side of a transfer as an rclone sees it.

    That covers an sftp remote reached from here, a remote named in a config
    file, and a cluster's own disk seen by the rclone running there. *base* is
    the rclone command without a subcommand, *fmt* turns a path into rclone's
    name for it, and ``run(argv, timeout, **watch)`` runs a command wherever
    that rclone lives, as plat.run does (*watch* is plat.run's idle and enough).
    A stat is a dict, None for "not there", or UNKNOWN for "could not ask", and
    the three never blur. *timeout*, *tries* and *idle* are probe_limits'.
    """

    def __init__(self, base, fmt=sftp_arg, run=None, timeout=45, tries=3, idle=120,
                 where="on the cluster"):
        self.base = list(base)
        self.fmt = fmt
        self.run = run or (lambda argv, timeout, **watch:
                           plat.run(argv, timeout=timeout, **watch))
        self.timeout = timeout
        self.tries = tries
        self.idle = idle
        self.where = where
        self._seen = {}

    def arg(self, path):
        return self.fmt(path)

    def _rclone(self, args, path, timeout=None, **watch):
        # ssh already validates the host key (StrictHostKeyChecking plus the
        # cluster's CA in known_hosts), so rclone's own "no host key
        # validation" notice is noise here and is silenced.
        return self.run(self.base + args + ["--log-level", "ERROR", self.fmt(path)],
                        timeout, **watch)

    def stat_once(self, path):
        proc = self._rclone(["lsjson", "--stat"], path, self.timeout)
        if proc.returncode != 0:
            return None if _says_absent(proc) else UNKNOWN
        try:
            value = json.loads(proc.stdout or "null")
        except ValueError:
            return UNKNOWN
        return value if isinstance(value, dict) else UNKNOWN

    def stat(self, path, tries=None):
        """rclone's stat for *path*, asked afresh.

        "Could not ask" is exactly the answer a busy login node gives for a
        moment, so it is retried, up to *tries* times (TRANSFER_PROBE_TRIES):
        a single slow round trip must not decide the fate of a transfer that
        is otherwise fine. Absence is not retried — rclone said so
        definitively.
        """
        tries = self.tries if tries is None else tries
        delay = 1.0
        for attempt in range(1, max(1, tries) + 1):
            got = self.stat_once(path)
            if got is not UNKNOWN:
                return got
            if attempt < tries:
                time.sleep(delay)
                delay *= 2
        return UNKNOWN

    def probe(self, path):
        """:meth:`stat`, asked once per path: what the path semantics and the
        expected size share, so twenty files into one directory ask about it
        once. The check after a transfer asks afresh."""
        if path not in self._seen:
            self._seen[path] = self.stat(path)
        return self._seen[path]

    def exists(self, path):
        return self.probe(path) is not None

    def is_dir(self, path):
        return _kind(self.probe(path))

    def listing(self, path):
        """``{name: stat}`` for the directory *path*, or None.

        Not timed: a directory with a million entries takes as long as it
        takes. rclone prints nothing until it has read the whole directory,
        though, so a listing still silent after *idle* seconds is given up and
        None returned, and the caller asks about each path it needed instead
        (see verify_group), which gets the same answers one bounded question
        at a time.
        """
        proc = self._rclone(["lsjson"], path, idle=self.idle)
        if proc.returncode != 0:
            return None
        try:
            rows = json.loads(proc.stdout or "[]")
        except ValueError:
            return None
        if not isinstance(rows, list):
            return None
        return {row.get("Name"): row for row in rows if isinstance(row, dict)}

    def holds_no_files(self, path):
        """Whether the tree at *path* has no file in it for rclone to copy.

        A walk takes as long as the tree, so it is not timed. It lists
        directories as well as files, which rclone prints as it reads each
        directory, so the output keeps moving for as long as the walk does,
        and it stops at the first file, which is the answer. Only a walk that
        prints nothing for *idle* seconds is given up, and then, as for any
        walk that could not finish, the tree is not known to be empty.
        """
        proc = self._rclone(["lsjson", "-R"], path, idle=self.idle,
                            enough=_holds_a_file)
        if getattr(proc, "enough", False) or proc.returncode != 0:
            return False
        try:
            rows = json.loads(proc.stdout or "[]")
        except ValueError:
            return False
        return not any(isinstance(row, dict) and not row.get("IsDir")
                       for row in rows)


def _holds_a_file(out):
    """Whether rclone's lsjson output so far lists a file: a first one is
    enough to say a tree is not empty."""
    return b'"IsDir":false' in out


class HomeUnknown(Exception):
    """The far side did not say where its home directory is."""


class ShellHome:
    """What ``~`` means where *run* runs shell commands.

    ``run(command)`` takes a shell command and returns a CompletedProcess. The
    home directory is resolved with ``pwd -P`` rather than read from $HOME
    because NERSC's /global/homes/<u>/<user> is a symlink, and a path built from
    the unresolved name confuses anything that later compares paths.
    """

    def __init__(self, run, home=None):
        self.run = run
        self._home = home

    def home(self):
        """The home directory, asked for until an answer comes.

        Only an absolute path from a command that succeeded is an answer; a
        startup file may print lines of its own first, so it is the last
        line. Anything else raises HomeUnknown: a path left starting with
        ``~`` would be quoted into the far side's commands as a directory
        literally named ``~``.
        """
        if self._home is None:
            got = self.run("cd && pwd -P")
            lines = (got.stdout or "").strip().splitlines()
            answer = lines[-1].strip() if lines else ""
            if got.returncode != 0 or not answer.startswith("/"):
                said = (got.stderr or "").strip().splitlines()
                why = (said[-1] if said else
                       f"it printed {answer!r}" if answer else "it printed nothing")
                raise HomeUnknown(f"`cd && pwd -P` exited {got.returncode}: {why}")
            self._home = answer
        return self._home

    def expand(self, path):
        if path == "~":
            return self.home()
        if path.startswith("~/"):
            return f"{self.home()}/{path[2:].lstrip('/')}"
        return path


# --- did the bytes arrive? ------------------------------------------------------
def _ends(spec, near, far):
    """``(source, dest)`` sides of *spec*."""
    return (near, far) if spec.up else (far, near)


def expectation(spec, near, far):
    """(path, is_dir, size) the destination must show once the bytes land.

    Taken *before* rclone runs, because --move deletes the source it would
    otherwise be measured from.
    """
    path, want_dir = spec.delivered()
    size = None
    if not want_dir:
        source, _dest = _ends(spec, near, far)
        stat = source.probe(spec.local_side if spec.up else spec.remote_side)
        if isinstance(stat, dict) and not stat.get("IsDir"):
            size = stat.get("Size") or None
    return path, want_dir, size


def inexact(spec):
    """Why this transfer's destination may legitimately not match its source.

    Verification is here to catch a transfer that quietly moved nothing, not
    to second-guess what was asked for. Where the request itself makes the
    destination differ, a mismatch is worth saying out loud and nothing more.
    """
    if any(flag.split("=", 1)[0] in FILTER_FLAGS for flag in spec.extra):
        return "this transfer filters what it copies"
    if spec.symlinks == "keep":
        return "--links stores a symlink as a .rclonelink file"
    return ""


def _source_was_empty(spec, near, far):
    """True when the source held nothing, so nothing arriving is correct."""
    if not spec.source_is_dir or spec.operation == "move":
        return False
    source, _dest = _ends(spec, near, far)
    return source.holds_no_files(spec.local_side if spec.up else spec.remote_side)


def verify(spec, near, far, expect):
    """Confirm the destination really holds what was just sent.

    rclone is honest about the errors it sees, but not every way of moving
    nothing is an error it sees: an empty source, a filter that matches
    everything, or path semantics that put the bytes somewhere other than
    where they were asked for all end in exit 0. This is what makes "cluster
    transfer returned 0" mean "the bytes are there".

    A destination that cannot be checked is reported and let through: the
    transfer itself already succeeded, and failing it because the *check*
    could not reach the cluster would invent a failure of its own. Returns
    whether it could be checked.
    """
    path, want_dir, size = expect
    _source, dest = _ends(spec, near, far)
    stat = dest.stat(path)
    if stat is None:
        # Only reached when a success is about to be called a failure, so a
        # second look costs nothing on the path that matters and rules out a
        # destination that was momentarily not visible. One try: the first
        # call already retried an unreachable cluster three times.
        time.sleep(1)
        stat = dest.stat(path, tries=1)
    if stat is UNKNOWN:
        ui.warn(f"could not confirm that {path} arrived; rclone itself "
                "reported success")
        return False

    problem, hint = judge(path, dest.where, stat, want_dir, size)
    if problem and stat is None and _source_was_empty(spec, near, far):
        ui.warn(f"nothing to copy: {spec.describe()} moved no files")
        return True
    _report(spec, problem, hint)
    return True


def verify_group(group, near, far, expects):
    """Check a whole group against one listing of the destination."""
    spec = group[0]
    _source, dest = _ends(spec, near, far)
    listing = dest.listing(spec.remote_side if spec.up else spec.local_side)
    if listing is None:
        # No listing, no shortcut: fall back to asking about each of them,
        # until one goes unanswered. Each unanswered question has had its
        # tries already, and the rest would wait out as many again.
        for index, (one, expect) in enumerate(zip(group, expects)):
            if not verify(one, near, far, expect):
                left = len(group) - index - 1
                if left:
                    ui.warn(f"the other {left} were not checked either: the "
                            "cluster is not answering")
                return
        return
    for one, expect in zip(group, expects):
        path, want_dir, size = expect
        got = listing.get(path.rsplit("/", 1)[-1])
        problem, hint = judge(path, dest.where, got, want_dir, size)
        _report(one, problem, hint)


def _report(spec, problem, hint):
    if not problem:
        return
    allowed = inexact(spec)
    if allowed:
        ui.warn(f"{problem} — allowed, because {allowed}")
        return
    ui.die(f"rclone reported success but {problem}", hint)


def judge(path, where, stat, want_dir, size):
    """(problem, hint) for one arrival, or ("", "") if it is what was sent."""
    problem = hint = ""
    if stat is None:
        problem = f"{path} is not {where}"
        hint = "nothing was delivered, so this transfer did not happen"
    elif bool(stat.get("IsDir")) != want_dir:
        problem = (f"{path} arrived as a "
                   f"{'directory' if stat.get('IsDir') else 'file'}, expected "
                   f"a {'directory' if want_dir else 'file'}")
    else:
        arrived = stat.get("Size")
        # Short, not merely different. A source still being written to —
        # a log, a checkpoint — legitimately delivers more than it measured
        # before the copy started, and that is not a lost byte.
        if (size is not None and not want_dir and isinstance(arrived, int)
                and 0 <= arrived < size):
            problem = f"{path} is {arrived} bytes {where}, expected {size}"
            hint = "the transfer was cut short"
    return problem, hint


# --- leases held for a caller --------------------------------------------------
def _started(pid):
    """When process *pid* started, as text a reused pid cannot share, or ""."""
    if not plat.FORCE_PORTABLE and os.path.exists("/proc/self/stat"):
        try:
            text = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return ""
        # Field 22, counted past the command name, which may hold spaces.
        fields = text.rsplit(")", 1)[-1].split()
        return fields[19] if len(fields) > 19 else ""
    return plat.out(["ps", "-o", "lstart=", "-p", str(pid)], timeout=10)


def caller_group():
    """The process group of whatever ran this process, or None if it is ours.

    A script's commands share its process group, including the subshell a
    ``$(...)`` runs in, which is gone the moment the command in it exits. So
    the group, not the parent, is what outlives a ``cluster`` run on the
    caller's behalf. A process that leads its own group (typed at an
    interactive shell) has no caller to outlive it.
    """
    group = os.getpgrp()
    return None if group == os.getpid() else group


def _this_host():
    """This machine's name, for a lease a shared state directory can hold."""
    return socket.gethostname() or "localhost"


def _group_alive(group, leader_started):
    """Whether process group *group* is the one a lease recorded."""
    try:
        os.killpg(group, 0)
    except (ProcessLookupError, PermissionError):
        return False
    # While any member lives, the number stays taken, so a live group with no
    # leader is still the recorded one. A leader that started at another time
    # is a reused number.
    started = _started(group)
    return not started or not leader_started or started == leader_started


class Transfers:
    def __init__(self, logins):
        self.logins = logins
        self.backend = logins.backend
        self.state = logins.state
        self.settings = logins.settings

    # --- rclone -------------------------------------------------------------
    def rclone_bin(self):
        """The rclone to run, or a stop that says how to get one."""
        found = find_rclone(self.settings)
        if found.problem is None:
            return found.path
        where = f" ({found.path})" if found.path else ""
        ui.die(f"rclone{where}: {found.problem}",
               f"transfers need rclone {version_text(MIN_RCLONE)} or newer, for "
               "--sftp-ssh; the official binary is at https://rclone.org/downloads/",
               "to use one that is not on PATH: cluster config set RCLONE /path")

    # --- connections --------------------------------------------------------
    def tags(self):
        prefix = f"cl-{self.backend.name}-xfer-"
        return sorted(
            path.stem[len(prefix):]
            for path in self.state.ctl_dir.glob(f"{prefix}*.sock")
        )

    def is_active(self, tag):
        return self.logins._socket_live(self.state.xfer_socket(tag))

    def active_tags(self):
        return [tag for tag in self.tags() if self.is_active(tag)]

    def lease_dir(self, tag):
        path = self.state.dir / f"transfer-{tag}.users"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def lease_file(self, tag):
        return self.lease_dir(tag) / str(os.getpid())

    def lease_take(self, tag):
        """Claim a share of *tag*, and hold the claim in the kernel.

        The file name still carries the pid because that is what makes a lease
        readable in `ls`, but what makes it *true* is the flock: it exists for
        exactly as long as this process does.
        """
        path = self.lease_file(tag)
        held = _LEASE_LOCKS.get(str(path))
        if held is None:
            lock = plat.FileLock(path)
            if lock.acquire():
                _LEASE_LOCKS[str(path)] = lock
        path.write_text(str(int(time.time())))

    def lease_drop(self, tag):
        path = self.lease_file(tag)
        held = _LEASE_LOCKS.pop(str(path), None)
        if held is not None:
            held.release()
        path.unlink(missing_ok=True)

    def _group_lease(self, tag, group):
        return self.lease_dir(tag) / f"group-{group}@{_this_host()}"

    def lease_take_for_caller(self, tag):
        """Claim a share of *tag* for the caller, who uses it after this exits.

        That is `ssh-command --transfer`: it prints a command line, and the
        program that asked keeps using the connection long after this process
        is gone, so a flock of ours would protect nothing. The lease names the
        caller's process group instead, and lasts as long as that group does.
        Returns whether a lease was taken.
        """
        group = caller_group()
        if group is None:
            return False
        self._group_lease(tag, group).write_text(_started(group) + "\n")
        return True

    def lease_drop_for_caller(self, tag):
        """Release what :meth:`lease_take_for_caller` took for this caller."""
        group = caller_group()
        if group is not None:
            self._group_lease(tag, group).unlink(missing_ok=True)

    def lease_holders(self, tag):
        """Live leases on *tag* held by other runs, as ``(kind, number)``
        pairs: ``("pid", N)`` or ``("process group", N)``. Dead ones are pruned.

        Liveness is the lock, not the pid in the name. A pid is reused within
        days on a busy machine, so a lease that outlived its run would sooner
        or later name some unrelated live process and hold the connection open
        for good, "still used by 1 other run(s)" with nothing else running. An
        flock cannot outlive its holder, however that holder ended. A lease
        held for a caller has no flock: it names the caller's process group
        and host, and records when the group's leader started. A lease from
        another host is that host's business, since the connection it keeps
        open is a socket there.
        """
        holders = []
        host = _this_host()
        for entry in sorted(self.lease_dir(tag).iterdir()):
            name = entry.name
            if name.isdigit():
                if int(name) == os.getpid():
                    continue
                kind, number, live = "pid", int(name), plat.is_locked(entry)
            elif name.startswith("group-"):
                group, _at, where = name[len("group-"):].partition("@")
                if not group.isdigit() or where != host:
                    continue
                try:
                    recorded = entry.read_text().strip()
                except OSError:
                    recorded = ""
                kind, number = "process group", int(group)
                live = _group_alive(number, recorded)
            else:
                continue
            if live:
                holders.append((kind, number))
            else:
                entry.unlink(missing_ok=True)
        return holders

    @contextlib.contextmanager
    def tag_lock(self, tag):
        """Serialize opening and tearing down one transfer master.

        "Is the socket live?" and "claim a lease on it" are two steps, and a
        teardown landing between them leaves the second run holding a lease on a
        master that has already been killed; two opens at once would each throw
        away the other's half-made master. So the lock is waited for, for as
        long as its holder lives (plat.FileLock.acquire_queued), and the wait
        names the holder, even for a quiet caller: a wait of unknown length
        that says nothing is indistinguishable from a hang.

        No patience for stillness, unlike the TOTP lock's: that one is held
        for at most one window, so stillness there is evidence of a stopped
        holder. This one is held across an open, which may try several nodes
        with an authentication each, every one under its own deadline; a clock
        short enough to catch a stuck holder would cut those off. A holder
        that is stopped (Ctrl-Z) is seen as such, and after LOCK_PATIENCE
        seconds of it the wait ends, saying so.
        """
        lock = plat.FileLock(self.state.dir / f"transfer-{tag}.lock",
                             record_holder=True)

        def announce(pid):
            ui.note(f"waiting for {plat.describe_pid(pid)} to finish with "
                    f"transfer connection {tag}")

        if not lock.acquire_queued(announce=announce,
                                   stopped=self.settings.int("LOCK_PATIENCE")):
            ui.die(f"could not use transfer connection {tag}",
                   plat.gave_up_text(lock, f"transfer connection {tag}'s lock"))
        try:
            yield
        finally:
            lock.release()

    def _node_file(self, tag):
        return self.state.dir / f"transfer-{tag}.lastnode"

    def node_of(self, tag):
        """The node *tag*'s master was last opened on, or None if the pool chose."""
        try:
            return self._node_file(tag).read_text().strip() or None
        except OSError:
            return None

    def _targets(self, tag, node):
        """Nodes to open *tag*'s master on, best first.

        A node that was asked for is used alone. Where nodes are addressable,
        every transfer node is a candidate and the last one that worked goes
        first, so one being down costs an attempt rather than the transfer.
        Elsewhere the pool's balancer decides.
        """
        if node is not None or not self.backend.node_choosable:
            return [node]
        candidates = list(self.backend.transfer_nodes())
        last = self.node_of(tag)
        if last in candidates:
            candidates.remove(last)
            candidates.insert(0, last)
        return candidates or [None]

    def open_connection(self, node=None, quiet=False, forward_agent=False,
                        agent_env=None):
        """Open (or reuse) a dedicated transfer master. Returns its tag.

        An agent-forwarding master gets its own tag: forwarding is fixed when the
        master is created (the master is what proxies the agent), so silently
        reusing a plain one would leave the far side unauthenticated with no
        obvious reason why.
        """
        tag = self.backend.short(node) if node else "pool"
        if forward_agent:
            tag += "-fwd"
        # Held for the whole open: two runs must not both conclude the socket is
        # missing and authenticate, and a run about to claim a lease must not
        # have the master torn down between the check and the claim.
        with self.tag_lock(tag):
            return self._open_locked(tag, node, quiet, forward_agent, agent_env)

    def reuse(self, tag):
        """Lease *tag*'s master if it is up; whether it was.

        Under the tag's lock, like an open: checked and leased in two unguarded
        steps, it could be torn down in between, leaving a lease on nothing.
        """
        with self.tag_lock(tag):
            return self._lease_if_live(tag)

    def reopen(self, tag, lost_pid, node=None, quiet=False, forward_agent=False,
               agent_env=None):
        """Get *tag*'s master back for a run that lost it; the node it is on.

        Under the tag's lock, like an open. Any master there is taken as it
        is, answering or slow to: another run riding it may have got there
        first, or the one this run took for lost (*lost_pid*) is there after
        all, and either way others ride it, whom a new one would cut off, and
        on FASRC it would spend a TOTP window.
        """
        with self.tag_lock(tag):
            if master_pid(self.state.xfer_socket(tag)) is not None:
                self.lease_take(tag)
            else:
                self._discard_connection(tag)
                self._open_locked(tag, node, quiet, forward_agent, agent_env)
        return node or self.node_of(tag)

    def _lease_if_live(self, tag):
        if not self.is_active(tag):
            return False
        self.lease_take(tag)
        return True

    def _open_locked(self, tag, node, quiet, forward_agent, agent_env):
        if self._lease_if_live(tag):
            return tag

        limit = self.settings.int("MAX_LOGINS")
        if self.logins.connection_count() >= limit:
            ui.die(f"already at {limit} cluster connections",
                   "use --via LOGIN to ride an existing one, or close something")

        self.backend.ensure_credential(quiet=quiet)
        sock = self.state.xfer_socket(tag)
        log = self.state.dir / f"master-xfer-{tag}.log"
        tries = max(1, self.settings.int("TRANSFER_OPEN_TRIES"))
        targets = self._targets(tag, node)
        if node is not None:
            # The one node asked for: only a failure worth repeating there is
            # tried again (Logins.open_master decides which).
            schedule, tries_each = targets, tries
        else:
            # Each attempt goes to the next node, or is a fresh draw from the
            # pool's balancer, so a node that is down costs one attempt.
            schedule = [targets[index % len(targets)]
                        for index in range(max(tries, len(targets)))]
            tries_each = 1
        for attempt, target in enumerate(schedule, 1):
            result = self.logins.open_master(
                sock, target, log, tries=tries_each, forward_agent=forward_agent,
                env=agent_env, quiet=quiet)
            if result.opened:
                self.lease_take(tag)
                plat.atomic_write_text(self._node_file(tag), (target or "") + "\n")
                if not quiet:
                    ui.info(f"opened a transfer connection ({tag})"
                            + (f" on attempt {attempt}" if attempt > 1 else ""))
                return tag
            if not result.another_node:
                break
            if target is not None and attempt < len(schedule) and not quiet:
                ui.note(f"{self.backend.short(target)} did not open a transfer "
                        "connection; trying the next node")

        nodes = [self.backend.short(target)
                 for target in dict.fromkeys(schedule[:attempt]) if target]
        hints = [result.detail or "no error output"]
        if not result.another_node:
            if attempt < len(schedule):
                hints.append("that is the credential failing, which every node "
                             "would repeat, so no other node was tried")
            hints.append(f"check it with: {self.backend.credentials_command()}")
        ui.die("could not open a transfer connection "
               + (f"on {', '.join(nodes)}" if nodes else
                  f"after {attempt} attempt(s)"),
               *hints, f"master log: {log}")

    def _discard_connection(self, tag):
        """Tear down whatever is left of a transfer master, alive or half-dead."""
        discard(self.state.xfer_socket(tag), grace=self.settings.int("STOP_TIMEOUT"))

    def close_connection(self, tag, force=False):
        with self.tag_lock(tag):
            holders = self.lease_holders(tag)
            named = ", ".join(f"{kind} {number}" for kind, number in holders)
            if holders and not force:
                ui.info(f"transfer connection {tag} is still used by "
                        f"{len(holders)} other run(s) ({named}); leaving it open")
                return False
            if holders:
                ui.warn(f"closing transfer connection {tag} while {named} "
                        "still hold(s) a lease on it")
            self._discard_connection(tag)
            return True

    def ssh_command_for(self, sock, node):
        """The ssh command rclone should use for its sftp channels."""
        return channel_ssh(self.backend, self.settings, sock, node)

    # --- path semantics -----------------------------------------------------
    def run(self, spec, quiet=False):
        """Execute a TransferSpec."""
        return self.run_all([spec], quiet=quiet)

    def run_all(self, specs, quiet=False):
        """Execute one or more specs that share a destination, on one connection.

        cp and rsync both take several sources and a directory, and a shell glob
        makes that the ordinary way to type it. Each source keeps exactly the
        semantics it would have had on its own; what sharing buys is that the
        expensive part — the connection, which on FASRC costs a TOTP window —
        is opened once for all of them, and that the question "is the
        destination a directory" is asked once rather than once per file.
        """
        rclone = self.rclone_bin()
        first = specs[0]
        if first.via:
            via = first.via
            self.logins.ensure(via)
            tag = None

            def restore(_lost_pid):
                self.logins.restore(via)
                return self.logins.node_of(via)

            link = Link(f"login '{via}'", self.state.socket(via), restore,
                        self.logins, self.logins.node_of(via))
        else:
            tag = self.open_connection(node=first.node, quiet=quiet)
            link = Link(f"transfer connection {tag}", self.state.xfer_socket(tag),
                        lambda lost: self.reopen(tag, lost, first.node, quiet),
                        self.logins, first.node or self.node_of(tag))

        # Everything from here on is inside the lease: resolving paths talks to
        # the cluster and can fail, and an exit that skipped the release would
        # leave both the lease file and the master behind for good.
        try:
            return self._run_leased(specs, rclone, link, quiet)
        finally:
            if tag is not None:
                self.lease_drop(tag)
                if not first.keep:
                    self.close_connection(tag)

    def _base(self, rclone, link):
        """The rclone command, without a subcommand, that rides *link*."""
        # --config /dev/null: the user's rclone config may be password-encrypted
        # and would then prompt, even though every remote here is given on the
        # command line.
        return [rclone, "--config", "/dev/null", *host_key_flags(rclone),
                "--sftp-ssh",
                rclone_ssh_value(self.ssh_command_for(link.sock, link.node))]

    def _run_leased(self, specs, rclone, link, quiet):
        far = RcloneOps(self._base(rclone, link), sftp_arg,
                        **probe_limits(self.settings))
        ride = Ride([link], self.settings, quiet=quiet)

        for spec in specs:
            # Several sources can only mean "put these in there", so a
            # destination that is merely not there yet is a directory waiting to
            # be created rather than an error. Only one that already exists as a
            # file is ambiguous, and that is what resolve() still reports.
            spec.dest_is_dir = spec.dest_is_dir or len(specs) > 1
            spec.resolve(far.is_dir)
            if len(specs) > 1 and spec.operation_kind == "copyto":
                side = spec.remote_side if spec.up else spec.local_side
                ui.die(f"{len(specs)} sources cannot be copied into one file",
                       f"{side} already exists and is a regular file",
                       "give a directory instead — a trailing slash names one "
                       f"that will be created: {side}/",
                       f"or remove {side} first")

        groups = self._grouped(specs)
        clash = self._mirror_clash(groups)
        if clash:
            mirror, other, side = clash
            ui.die(f"--sync would mirror {side} twice over",
                   f"{mirror.raw_source} makes it match that source exactly, so "
                   f"it would delete what {other.raw_source} delivers there",
                   "drop --contents (and any trailing slash) so each source "
                   "lands in its own directory, or mirror one source at a time")

        delivered = 0
        for group in groups:
            rc = self._run_group(group, far, ride, quiet)
            if rc != 0:
                if len(specs) > 1:
                    ui.warn(f"stopped after {delivered} of {len(specs)} sources")
                return rc
            delivered += len(group)
        return 0

    @staticmethod
    def _mirror_clash(groups):
        """A destination one run would mirror while another writes into it.

        rclone's sync makes the destination match *its* source and nothing
        else, so two mirrors of one directory each delete the other's files.
        Everything that is not that runs: directory sources land in their own
        subdirectories, and several files under one root become a single run
        whose delete pass sees all of them at once. Only a genuine collision is
        reported, and it names both sides of it.
        """
        mirrors, writers = {}, {}
        for group in groups:
            spec = group[0]
            side = spec.remote_side if spec.up else spec.local_side
            writers.setdefault(side, []).append(spec)
            # A sync whose source is a single file behaves as a copy -- rclone
            # deletes nothing for it -- so it is not a mirror and cannot clash.
            if spec.operation == "sync" and (spec.source_is_dir or len(group) > 1):
                mirrors.setdefault(side, []).append(spec)
        for side, owners in mirrors.items():
            for other in writers[side]:
                if other is not owners[0]:
                    return owners[0], other, side
        return None

    def _run_group(self, group, far, ride, quiet):
        spec = group[0]
        near = spec.ops
        link, = ride.links

        def base():
            # A restored link may be on another node, which the ssh command
            # names; the path questions after the run ride it too.
            far.base = self._base(far.base[0], link)
            return far.base

        if len(group) == 1:
            if not quiet:
                ui.info(f"{spec.describe()}")
            # resolve() already asked about a download's source, so the probe
            # answers without a second round trip.
            expect = expectation(spec, near, far)
            rc = ride.run(lambda: self._rclone_argv(spec, base(), quiet))
            if rc == 0 and not spec.dry_run:
                verify(spec, near, far, expect)
            return rc

        root, _name = spec.batch_split()
        expects = [expectation(one, near, far) for one in group]
        if not quiet:
            ui.info(f"{spec.describe_group(len(group))}")

        names = tempfile.NamedTemporaryFile("w", suffix=".files", delete=False)
        try:
            for one in group:
                names.write(one.batch_split()[1] + "\n")
            names.close()
            source = near.arg(root) if spec.up else far.arg(root)
            rc = ride.run(lambda: self._rclone_argv(
                spec, base(), quiet, source=source, listing=names.name))
        finally:
            Path(names.name).unlink(missing_ok=True)
        if rc == 0 and not spec.dry_run:
            verify_group(group, near, far, expects)
        return rc

    def _rclone_argv(self, spec, base, quiet, source=None, listing=None):
        transfers, checkers = concurrency(self.settings, spec.transfers,
                                          spec.checkers, shared=bool(spec.via))
        argv = [base[0], spec.operation] + base[1:]
        argv += rclone_flags(self.settings, transfers, checkers,
                             dry_run=spec.dry_run, progress=spec.progress,
                             quiet=quiet or spec.quiet, symlinks=spec.symlinks,
                             extra=spec.extra)
        if listing is not None:
            argv += ["--files-from", str(listing)]
            if spec.operation == "sync":
                # --files-from alone narrows the delete pass to the listed
                # files, which quietly turns a mirror into a copy. This is what
                # makes one run over several sources mean what --sync says.
                argv.append("--delete-excluded")
        argv += [source or spec.source_arg(), spec.dest_arg()]
        return argv

    # --- batching -----------------------------------------------------------
    @staticmethod
    def _grouped(specs):
        """Runs of specs that can share one rclone invocation.

        rclone takes a single source, but ``--files-from`` takes a list of names
        under a common root — which is exactly the shape a shell glob produces.
        Twelve files from one directory then cost one copy and one check rather
        than twenty-four rclone startups, and on an sftp connection each startup
        is a new channel and a new handshake, not a cheap fork.
        """
        groups, run, key = [], [], object()
        for spec in specs:
            this = spec.batch_key()
            if run and this is not None and this == key:
                run.append(spec)
                continue
            if run:
                groups.append(run)
            run, key = [spec], this
        if run:
            groups.append(run)
        return groups


class TransferSpec:
    """Which way data moves, and what the paths mean.

    Semantics are deliberately cp/rsync-like: rclone's ``copy`` means "the
    *contents* of SRC", which silently merges a directory into DEST's parent.
    Here ``cluster transfer dir remote:place`` puts ``dir`` *inside* ``place``,
    and ``dir/`` (or ``--contents``) asks for the contents instead.
    """

    def __init__(self, backend, source, dest, *, up=None, contents=False,
                 operation="copy", symlinks="follow", dry_run=False,
                 progress=False, quiet=False, keep=False, via=None, node=None,
                 transfers=0, checkers=0, extra=(), ops=None, remote_fmt=None,
                 dest_is_dir=False):
        self.backend = backend
        #: how to answer questions about the near side, and how to name the far
        #: side to rclone. The defaults are "this machine" and "an sftp remote",
        #: which is every ordinary transfer; a cross-cluster one substitutes a
        #: cluster for the near side and a second named remote for the far one.
        self.ops = ops or LocalOps()
        self.remote_fmt = remote_fmt or sftp_arg
        self.raw_source = source
        self.raw_dest = dest
        self.contents = contents or source.rstrip().endswith("/")
        #: Set when the destination can only be a directory — several sources
        #: into one place. It settles the one case the probe cannot: a path that
        #: is not there yet, which rclone creates. A path that is already a file
        #: is still reported, because that one is genuinely ambiguous.
        self.dest_is_dir = dest_is_dir
        self.operation = operation
        self.symlinks = symlinks
        self.dry_run = dry_run
        self.progress = progress
        self.quiet = quiet
        self.keep = keep
        self.via = via
        self.node = node
        self.transfers = transfers
        self.checkers = checkers
        self.extra = list(extra)
        self.up = self._infer_direction(up)
        self.resolved = False
        self.operation_kind = "copy"
        self.local_side = ""
        self.remote_side = ""
        #: what resolve() decided the source was, kept so the destination can be
        #: named afterwards and checked.
        self.source_is_dir = False
        self.source_name = ""

    def _infer_direction(self, up):
        if up is not None:
            return up
        source_tagged = self.raw_source.startswith(("local:", "remote:"))
        dest_tagged = self.raw_dest.startswith(("local:", "remote:"))
        if source_tagged or dest_tagged:
            if self.raw_source.startswith("remote:"):
                return False
            if self.raw_dest.startswith("remote:"):
                return True
            if self.raw_source.startswith("local:"):
                return True
            return False
        # Otherwise: whichever side exists here is the local one.
        if self.ops.exists(self._strip(self.raw_source)):
            return True
        if self.ops.exists(self._strip(self.raw_dest)):
            return False
        ui.die(
            "cannot tell which side is local",
            f"neither {self.raw_source!r} nor {self.raw_dest!r} exists here",
            "say local:PATH / remote:PATH, or pass --up / --down",
        )

    @staticmethod
    def _strip(value):
        for prefix in ("local:", "remote:"):
            if value.startswith(prefix):
                return value[len(prefix):]
        return value

    def resolve(self, remote_is_dir, where="on the cluster"):
        """Work out the real rclone paths and operation.

        *remote_is_dir* answers whether a path on the far side is a directory,
        as the near side's ``is_dir`` does for this one: True or False, None
        when it is not there, or UNKNOWN when it could not be asked. *where*
        names the far side in messages.

        Both sides are asked, because guessing costs data either way: a source
        guessed to be a directory turns a single-file fetch into a directory
        named after the file, and a destination guessed not to be one turns
        "copy into DEST" into "rename to DEST", which rclone carries out and
        calls a success.
        """
        source = self._strip(self.raw_source)
        dest = self._strip(self.raw_dest)
        self.local_side = source if self.up else dest
        self.remote_side = dest if self.up else source
        source_name = Path(source.rstrip("/")).name

        if self.up:
            probed, source_where = self.ops.is_dir(source), self.ops.where
        else:
            probed, source_where = remote_is_dir(source), where
        if probed is None:
            ui.die(f"{source} does not exist {source_where}")
        if probed is UNKNOWN:
            # Not knowing is not a reason to refuse a transfer. Read as a
            # directory it costs, at worst, one extra level of nesting; read as
            # a file it renames whatever arrives onto a single path, which is
            # the silent misdelivery this whole probe exists to prevent. Take
            # the harmless reading.
            ui.warn(f"could not tell whether {source} is a directory (it "
                    "did not answer); copying it as one")
            probed = True
        source_is_dir = probed

        if source_is_dir:
            # A directory lands *inside* DEST, like cp/rsync — unless its
            # contents were asked for with a trailing slash or --contents.
            if not self.contents:
                if self.up:
                    self.remote_side = str(Path(self.remote_side) / source_name)
                else:
                    self.local_side = str(Path(self.local_side) / source_name)
            self.operation_kind = "copy"
        else:
            # A file keeps its own name only when DEST names a directory;
            # otherwise DEST *is* the new name.
            if dest.endswith("/"):
                dest_is_dir = True
            else:
                probed = remote_is_dir(dest) if self.up else self.ops.is_dir(dest)
                if probed is UNKNOWN:
                    # Same choice as above, for the same reason: "copy into it"
                    # is recoverable, "rename onto it" is a misdelivery.
                    ui.warn(f"could not tell whether {dest} is a directory (it "
                            "did not answer); copying into it rather than "
                            "renaming onto it")
                    dest_is_dir = True
                else:
                    dest_is_dir = bool(probed) or (probed is None
                                                   and self.dest_is_dir)
            self.operation_kind = "copy" if dest_is_dir else "copyto"

        if self.operation == "copy" and self.operation_kind == "copyto":
            self.operation = "copyto"
        self.source_is_dir = source_is_dir
        self.source_name = source_name
        self.resolved = True

    def delivered(self):
        """The one path that must exist afterwards, and whether it is a
        directory.

        The path semantics above have already decided this: a copyto writes DEST
        itself, a directory lands at the adjusted destination, and a file copied
        into a directory keeps its own name.
        """
        side = self.remote_side if self.up else self.local_side
        if self.operation_kind == "copyto":
            return side, False
        if self.source_is_dir:
            return side, True
        return f"{side.rstrip('/')}/{self.source_name}", False

    def batch_split(self):
        """(directory, name) for the side this transfer reads from."""
        if self.up:
            source = Path(self.ops.expand(self.local_side))
            return str(source.parent), source.name
        root, _sep, name = self.remote_side.rpartition("/")
        return (root or "."), name

    def batch_key(self):
        """What lets two transfers share one rclone run, or None if they cannot.

        Only plain file-into-directory copies qualify, and only when they agree
        on everything one invocation carries: the same source directory, the
        same destination, the same operation, the same flags. Anything else runs
        on its own, which is always correct and merely slower.
        """
        if not self.resolved or self.source_is_dir or self.contents:
            return None
        if self.operation_kind != "copy" or self.operation not in (
                "copy", "move", "sync"):
            return None
        if any(f.split("=", 1)[0] == "--files-from" for f in self.extra):
            return None
        root, _name = self.batch_split()
        return (self.up, self.operation, root, self.symlinks, tuple(self.extra),
                self.remote_side if self.up else self.local_side)

    def describe_group(self, count):
        """describe(), for several sources that share one destination."""
        root, _name = self.batch_split()
        right = self.remote_side if self.up else self.local_arg()
        # A batched sync deletes at the destination, so it must not announce
        # itself as a copy.
        what = {"move": "move", "sync": "MIRROR"}.get(self.operation, "copy")
        note = " (dry run)" if self.dry_run else ""
        return f"{what} {count} files from {root} -> {right}{note}"

    def remote_arg(self):
        return self.remote_fmt(self.remote_side)

    def local_arg(self):
        return self.ops.arg(self.local_side)

    def source_arg(self):
        return self.local_arg() if self.up else self.remote_arg()

    def dest_arg(self):
        return self.remote_arg() if self.up else self.local_arg()

    def symlink_flags(self):
        return symlink_flags(self.symlinks)

    def describe(self):
        arrow = "->"
        left = self.local_arg() if self.up else self.remote_side
        right = self.remote_side if self.up else self.local_arg()
        what = {"copy": "copy", "copyto": "copy", "move": "move", "sync": "MIRROR"}[
            self.operation if self.operation in ("move", "sync") else self.operation_kind]
        note = " (dry run)" if self.dry_run else ""
        return f"{what} {left} {arrow} {right}{note}"
