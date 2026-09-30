"""Logins: SSH control masters that are pinned to a node.

A *login* is a named, long-lived SSH control master. Its node is durable state:
once a login has landed somewhere, it always reconnects to that same node,
because the tmux sessions it owns are node-local. If the node is unreachable the
login fails loudly rather than quietly taking a different one — a silent move is
how you end up with empty stand-in sessions on a fresh node while the real work
sits unreachable on the old one.

The two backends reach that same end state from opposite directions:

* FASRC cannot ask for a node without spending a TOTP window, so a new login
  takes whatever the balancer gives it and adopts that node as its pin.
* NERSC can address any node for free through the pool jump, so a new login
  picks its node up front (deterministically from its name, so a login whose
  local state was lost returns to where its sessions are).
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from pathlib import Path

from . import backoff, config, linger, platform as plat, registry, ui
from .auth import (explain_failure, failure_text, is_rejection, is_transient_setup,
                   refused_by)
from .state import State, require_socket_path

#: Longest name a new login may have. The name is part of every control
#: socket path, and a sun_path holds 104 bytes on macOS; with this cap the
#: default CTL_DIR leaves room under an ordinary home directory.
LOGIN_NAME_MAX = 32


class MasterOpen(tuple):
    """What :meth:`Logins.open_master` reports: the pair ``(opened, detail)``.

    It unpacks and compares as that pair. ``another_node`` says whether a
    failure is worth trying on a different node: one that refused, dropped or
    timed out the connection is, while a credential that was rejected or
    cannot be used is not, because every node refuses it the same way, and on
    FASRC each try spends a TOTP window and adds to the failed logins that
    lock an account.
    """

    def __new__(cls, opened, detail, another_node=False):
        result = super().__new__(cls, (bool(opened), detail))
        result.another_node = bool(another_node) and not opened
        return result

    @property
    def opened(self):
        return self[0]

    @property
    def detail(self):
        return self[1]


def control(sock, operation, timeout=10):
    """Send ``ssh -O operation`` to the master at *sock*; True on success.

    A control command talks only to the local socket, so no ssh config file
    has anything to say to it; reading none keeps a ``Match exec`` stanza
    from running on every health check.
    """
    argv = ["ssh", "-F", os.devnull, "-O", operation,
            "-o", f"ControlPath={sock}", "dummy"]
    return plat.run(argv, timeout=timeout).returncode == 0


#: What master_pid says of a master that holds its socket but did not answer
#: in time: stopped, or starved of the machine, not gone. Not a number, so
#: that it can never be taken for a pid.
UNANSWERED = object()


def master_pid(sock, timeout=10):
    """The pid of the master answering at *sock*: None when none is there, 0
    when one answers without saying its pid, UNANSWERED when the socket takes
    the question and no answer comes within *timeout*.

    What a run riding a master compares, to know whether the master it
    started on is still the one there: a master that died, even one that
    something else has since replaced, took every channel on it along. A
    socket nothing holds refuses at once; the question waits only on a
    master that is there.
    """
    if not os.path.exists(sock):
        return None
    proc = plat.run(["ssh", "-F", os.devnull, "-O", "check",
                     "-o", f"ControlPath={sock}", "dummy"], timeout=timeout)
    if proc.returncode == 124:
        return UNANSWERED
    if proc.returncode != 0:
        return None
    found = re.search(r"\(pid=(\d+)\)", (proc.stderr or "") + (proc.stdout or ""))
    return int(found.group(1)) if found else 0


def discard(sock, timeout=10, grace=config.DEFAULTS["STOP_TIMEOUT"]):
    """Tell whatever master is at *sock* to exit, and remove the path.

    A master that came up after it was given up on is stopped here, rather
    than left running behind a socket path unlinked from it, where nothing
    can see it or close it. One that does not answer the exit within
    *timeout* is found by its command line and stopped (plat.stop, *grace*).
    """
    sock = Path(sock)
    if sock.exists():
        if not control(sock, "exit", timeout=timeout):
            for pid in masters_at(sock):
                plat.stop(pid, grace)
        sock.unlink(missing_ok=True)


def masters_at(sock):
    """Pids of this user's ssh masters on control path *sock*: the ssh
    processes whose own options (_ssh_options) make them that master. Words
    that only mention the options, in a remote command, name no master."""
    found = []
    for pid, argv in plat.own_process_argv():
        if not argv or Path(argv[0]).name not in ("ssh", "ssh.exe"):
            continue
        values, letters = _ssh_options(argv)
        options = {}
        for value in values:
            key, _, setting = value.partition("=")
            # ssh keeps the first value it is given for an option.
            options.setdefault(key.lower(), setting)
        if ("O" not in letters and options.get("controlpath") == str(sock)
                and options.get("controlmaster", "").lower() == "yes"):
            found.append(pid)
    return found


#: ssh's options that take an argument (ssh(1)); every other letter is a flag.
_SSH_ARGUMENT_OPTIONS = frozenset("BbcDEeFIiJLlmOoPpQRSWw")


def _ssh_options(argv):
    """``(-o values, option letters)`` that an ssh started with *argv* reads.

    As ssh reads them: before the destination and after it, up to the first
    other word, where the remote command starts, or up to ``--``. Nothing in
    the remote command is an option, whatever it looks like.
    """
    values, letters = [], set()
    words = iter(argv[1:])
    destination = False
    for word in words:
        if word == "--":
            break
        if len(word) < 2 or not word.startswith("-"):
            if destination:
                break
            destination = True
            continue
        for at, letter in enumerate(word[1:], 2):
            letters.add(letter)
            if letter in _SSH_ARGUMENT_OPTIONS:
                value = word[at:] or next(words, "")
                if letter == "o":
                    values.append(value)
                break
    return values, letters


#: The connection a rider makes when it cannot ride: it says so and fails.
#:
#: ControlMaster=no only stops ssh from becoming a master. When the socket is
#: gone, refuses the connection, or the master turns the session away, ssh
#: goes on to connect by itself — a fresh MFA prompt, a login on whatever
#: node the pool picks, or a hang on a firewalled one. A
#: ProxyCommand is only ever run for such a connection of its own, never
#: for a session over a master, and one given with -o wins over any
#: ProxyCommand or ProxyJump in the config file, so this reaches exactly the
#: case to stop. ssh then exits 255 as for any lost connection.
#:
#: ssh runs it with the user's $SHELL, so the redirection is left to
#: /bin/sh and the rest is quoted the same in sh, csh and fish. It holds no
#: comma, because sshfs splits its -o options on commas; %h is ssh's host
#: name. rclone does not pass ssh's stderr on, so there only the exit status
#: tells, and rclone needs the argv written with rclone_ssh_value().
NO_CONNECTION_OF_ITS_OWN = (
    "ProxyCommand=/bin/sh -c 'echo cluster: the connection to %h is gone"
    " or refused another session - not opening a new one >&2'")


#: The timeout of a command run on a cluster when none is given
#: (config.COMMAND_TIMEOUT), for the modules that pass one on to here.
COMMAND_TIMEOUT = config.COMMAND_TIMEOUT


def reconnect_memory(settings, limit_key):
    """The backoff.FailureMemory of a loop that reconnects a dropped connection.

    *limit_key* names the setting for how many drops in quick succession the
    loop rides out. The waits between reconnects, and how fast a working
    connection fades the drops before it, are the RECONNECT_* settings every
    such loop shares.
    """
    return backoff.FailureMemory(
        half_life=settings.int("RECONNECT_HALF_LIFE"),
        limit=settings.int(limit_key),
        delay=settings.int("RECONNECT_DELAY"),
        delay_max=settings.int("RECONNECT_DELAY_MAX"))


def ride_out(memory, what, attempt, limit_key, cause=lambda: "", to=""):
    """Wait out *memory*'s backoff, then *attempt*() a reconnect, until one works.

    *what* says what was lost, for the warning before each wait. A reconnect
    that fails for a reason that can pass (the network still down after a
    laptop wakes, a pool connection dropped before authentication, a node
    refusing new sessions for now) is one more failure in *memory*, and is
    tried again after a longer wait. A refused credential ends it at once:
    retrying one only repeats the refusal, and on FASRC each try counts
    toward locking the account. So does *memory* passing its limit (the
    setting *limit_key*), which says so. *cause*() is the master's own
    explanation of the last failure (Logins.last_failure), for a refusal that
    carries none, and *to* names what is reconnected, as in "reconnecting to
    'x' failed". Returns what *attempt* returned.
    """
    failures = connection_failures()
    while True:
        if memory.exhausted:
            ui.die(f"{what}; gave up reconnecting after {memory.fresh} "
                   "failures in quick succession",
                   f"check the network, then try again; {limit_key} "
                   f"({memory.limit}) is how many in quick succession are "
                   "ridden out")
        wait = memory.wait()
        ui.warn(f"{what}; reconnecting in {wait:.0f}s")
        time.sleep(wait)
        try:
            return attempt()
        except failures as exc:
            reason = failure_text(exc, cause())
            if refused_by(exc, cause()):
                raise
            memory.failed()
            what = f"reconnecting{to} failed ({reason})"


def connection_failures():
    """What a failed reconnect can raise, as the watcher counts them
    (watcher.FAILURES): a refusal (SystemExit, ui.Die), an unreadable secret
    (OSError, ValueError) and an sshproxy answer cut short. http.client is
    imported only here, on the way to a reconnect, not by every command."""
    import http.client

    return (SystemExit, OSError, ValueError, http.client.HTTPException)


#: The statuses that are not the far side's: ssh's own failure, a run given
#: up on at its deadline (plat.run, run_with_prompts), and a program that
#: could not be started at all.
NOT_THE_FAR_SIDE = (124, 127, 255)


def direct_outcome(proc):
    """``(authenticated, detail)`` of an ssh that authenticated by itself.

    Any status but ssh's own is the far side's answer, given once the
    credential was taken: a command that fails there still says the
    credential works. A 127 might be the far side's too, but is not taken as
    evidence either way.
    """
    if proc.returncode not in NOT_THE_FAR_SIDE:
        return True, ""
    text = f"{proc.stderr or ''}\n{proc.stdout or ''}"
    return False, explain_failure(text.splitlines())


def rider_argv(sock, target=None, options=(), remote=None):
    """An ssh argv that rides the master at *sock* and never opens its own.

    It reads the same config file as the master it rides (SSH_CONFIG). If
    that master is gone, ssh prints that the connection is gone and exits
    255 instead of authenticating afresh (NO_CONNECTION_OF_ITS_OWN).
    Without *target* it is the command alone, for a program that adds the
    host itself (rsync -e). rclone's --sftp-ssh does not: it takes the whole
    command, target included, written with rclone_ssh_value().
    """
    argv = ["ssh", "-F", config.global_value("SSH_CONFIG", "/dev/null"),
            "-o", f"ControlPath={sock}", "-o", "ControlMaster=no",
            "-o", NO_CONNECTION_OF_ITS_OWN, *options]
    if target is not None:
        argv.append(target)
        if remote is not None:
            argv.append(remote)
    return argv


def rclone_ssh_value(argv):
    """*argv* written the way rclone splits ``--sftp-ssh`` (or ``ssh =`` in
    an rclone config) back apart.

    rclone reads that value as one space-separated CSV record, not as shell
    words: a word holding a space or a double quote is wrapped in double
    quotes, with each double quote inside doubled. Shell quoting there breaks
    on any path with a space, and on the rider's own ProxyCommand.
    """
    def field(word):
        if word == "" or any(c in word for c in ' \t"\r\n'):
            return '"' + word.replace('"', '""') + '"'
        return word
    return " ".join(field(word) for word in argv)


def _resync_size():
    """Put the pty's window size back in step with the terminal, and say so.

    A pty can come back from a full-screen remote session still carrying the
    size it had when that session started — a resize that arrived while the
    alternate screen was up never reached it. Everything afterwards then wraps
    at the wrong column: text piles onto the last cell and the shell's line
    editor redraws in the wrong place, which is what "typing overwrites a ghost
    character" is. It survives detaching because the stale size is in the pty,
    not in anything the remote end owns.

    Silent unless it actually corrected a mismatch, which makes the one line it
    does print the answer to "why did my terminal go strange again".
    """
    fixed = plat.resync_terminal_size()
    if fixed:
        ui.info(f"terminal size was stale; corrected to {fixed[0]}x{fixed[1]}")


class Logins:
    def __init__(self, backend, state=None):
        self.backend = backend
        self.state = state or State(backend)
        self.settings = backend.settings
        #: Why the last master that failed to open failed, for a caller that
        #: only sees the refusal (the watcher, which must tell a rejected
        #: credential from a network blip).
        self.last_failure = ""
        #: Why the shared record of a refused credential held the last
        #: authentication before anything was sent, or "": a caller counting
        #: failed tries has none to count then.
        self.held = ""
        #: Logins whose live master just did not answer in time, by how many
        #: times in a row (_needs_no_rebuild).
        self._unanswered = {}

    # --- inspection ---------------------------------------------------------
    def is_active(self, name):
        """Is the master process there? Says NOTHING about spare channels.

        `ssh -O check` answers "Master running" in a tenth of a second while the
        server is refusing every new channel, so this is necessary but not
        sufficient for "usable" — see channels_free().
        """
        return self._socket_live(self.state.socket(name))

    def active_names(self):
        return [n for n in self.state.known_logins() if self.is_active(n)]

    def channel_clients(self, name):
        """[(pid, kind)] for every local process holding a channel on the master.

        One SSH connection carries at most SSH_MAX_SESSIONS channels, and
        everything riding this login shares that budget: each open `attach`, the
        sshfs mount, every rclone sftp connection. Overrun is reported by sshd
        only as "channel N: open failed: connect failed: open failed", so the
        count has to be kept here to be explainable at all.
        """
        sock = str(self.state.socket(name))
        clients = []
        for pid, cmd in plat.own_processes():
            if sock not in cmd:
                continue
            # Only an ssh client holds a channel. sshfs names the control path on
            # its own command line too, but the channel belongs to the `ssh -s
            # sftp` child it spawns — counting both double-counts the mount.
            if not _is_ssh(cmd):
                continue
            # `-O check`/`-O exit` are control-socket commands, not sessions.
            if " -O " in f" {cmd} ":
                continue
            # The master itself carries no session channel of its own.
            if "ControlMaster=yes" in cmd:
                continue
            if "ClearAllForwardings" in cmd and "sftp" in cmd:
                kind = "sshfs mount"
            elif "sftp" in cmd:
                kind = "sftp (rclone/transfer)"
            elif " -t " in f" {cmd} ":
                kind = "interactive session"
            else:
                kind = "remote command"
            clients.append((pid, kind))
        return clients

    def channels_in_use(self, name):
        return len(self.channel_clients(name))

    def channels_free(self, name):
        """Channels still openable on this login's master, by local accounting.

        A floor, not a guarantee: another host could hold channels this machine
        cannot see. Good enough to size a transfer's concurrency, which is the
        thing that otherwise silently overruns the limit.
        """
        limit = self.settings.int("SSH_MAX_SESSIONS")
        return max(0, limit - self.channels_in_use(name))

    def master_pids(self, name):
        sock = str(self.state.socket(name))
        pids = []
        for pid, cmd in plat.own_processes():
            if sock in cmd and " -O " not in f" {cmd} ":
                pids.append(pid)
        return pids

    def connection_count(self, active=None, transfers=None):
        """Logins + transfer masters + relocated mount masters.

        They all consume the same per-user connection budget on the cluster, so
        the MAX_LOGINS gate has to count all three. *active* and *transfers*
        are the live logins and transfer tags a report has just found and
        shows beside this count; the gate passes neither, and every master is
        asked afresh.
        """
        total = len(self.active_names() if active is None else active)
        for sock in self.state.ctl_dir.glob(f"cl-{self.backend.name}-mnt-*.sock"):
            if self._socket_live(sock):
                total += 1
        if transfers is not None:
            return total + len(transfers)
        for sock in self.state.ctl_dir.glob(f"cl-{self.backend.name}-xfer-*.sock"):
            if self._socket_live(sock):
                total += 1
        return total

    def _socket_live(self, sock):
        """Does a master answer at *sock*? No socket file is a quick no."""
        return os.path.exists(sock) and control(sock, "check")

    def wait_socket_live(self, sock, timeout=None):
        """Poll until a freshly opened master answers, or the deadline passes.

        ``ssh -f`` returns as soon as it has forked, so asking once immediately
        afterwards is a race the caller sometimes loses — and losing it would
        surface as a hard failure even though the master was seconds from being
        ready (or already gone). Polling makes the answer definitive either way:
        a master that is coming up is waited for, and one that died is still
        reported dead once the deadline passes.
        """
        if timeout is None:
            timeout = self.settings.int("MASTER_READY_WAIT")
        deadline = time.monotonic() + timeout
        delay = 0.1
        while True:
            if self._socket_live(sock):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(delay)
            delay = min(delay * 2, 0.8)

    # --- remote execution ---------------------------------------------------
    def command_timeout(self, own_connection=False):
        """REMOTE_COMMAND_TIMEOUT, and CONNECT_TIMEOUT more for a command that
        opens a connection of its own, which is let in before it runs."""
        return (self.settings.int("REMOTE_COMMAND_TIMEOUT")
                + (self.settings.int("CONNECT_TIMEOUT") if own_connection else 0))

    def run_remote(self, name, command, timeout=COMMAND_TIMEOUT, capture=True,
                   idle=None):
        """Run a shell command on the login's node over its existing master.

        *idle*, for a command whose work grows with what it finds and that
        prints as it goes, stops it only after that many seconds of silence
        (plat.run); give it with timeout=None."""
        if timeout is COMMAND_TIMEOUT:
            timeout = self.command_timeout()
        argv = rider_argv(self.state.socket(name),
                          self.backend.target(self.node_of(name) or None),
                          ["-o", "BatchMode=yes"], remote=command)
        if idle is None:
            return plat.run(argv, timeout=timeout, capture=capture)
        return plat.run(argv, timeout=timeout, capture=True, idle=idle)

    def remote_value(self, name, command, timeout=COMMAND_TIMEOUT):
        proc = self.run_remote(name, command, timeout=timeout)
        if proc.returncode != 0:
            return ""
        return (proc.stdout or "").strip()

    # --- node bookkeeping ---------------------------------------------------
    def node_of(self, name):
        """The node this login is pinned to (durable), if any."""
        return self.state.pin_read(name)

    def live_node(self, name):
        """Which node the master is actually on, asked over the master."""
        value = self.remote_value(name, "hostname -f")
        return value.splitlines()[-1].strip() if value else ""

    def refresh_meta(self, name):
        """Record where a live login is, adopting the node as its pin if new."""
        node = self.live_node(name)
        if not node:
            return ""
        pinned = self.state.pin_read(name)
        if not pinned:
            # Adopting the node reflects reality either way; but if another
            # login already holds it, say so loudly — the authentication is
            # already spent, so refusing here would only waste it.
            holder = self.state.login_pinned_to(
                node, exclude=name, short=self.backend.short)
            if holder and self.settings.flag("ONE_LOGIN_PER_NODE"):
                ui.warn(
                    f"login '{name}' landed on {self.backend.short(node)}, which is "
                    f"already login '{holder}'s node; sessions are node-local, so "
                    f"both will list the same tmux sessions — separate them with: "
                    f"cluster repin {name} NODE")
            self.state.pin_write(name, node)
        else:
            self.state.ledger_add(node)
        self.state.write_meta(name, node=node)
        return node

    # --- creating and destroying --------------------------------------------
    def ensure(self, name, quiet=False, preferred=None):
        """Make login *name* usable, creating its master if needed."""
        if self.is_active(name):
            return True

        lock = self.state.login_lock(
            name, announce=None if quiet else self._announce_wait(name))
        if lock is None:
            self.last_failure = self.state.login_lock_blocked
            ui.die(f"could not open login '{name}'", self.state.login_lock_blocked)
        try:
            return self._ensure_locked(name, quiet=quiet, preferred=preferred)
        finally:
            lock.release()

    def _announce_wait(self, name):
        """What to say when another process holds *name*'s login lock."""
        def announce(pid):
            ui.note(f"waiting for {plat.describe_pid(pid)} to finish with "
                    f"login '{name}'")
        return announce

    def _ensure_locked(self, name, quiet=False, preferred=None):
        """The create half of :meth:`ensure` for a caller holding login_lock.

        Repair holds the lock across teardown and rebuild so no watcher or
        foreground command can cut down its authentication. Calling ``ensure``
        from there would take the same non-reentrant flock a second time and
        reject itself as "another process" on every aggressive repair.
        """
        if self.is_active(name):
            return True
        return self._create(name, quiet=quiet, preferred=preferred)

    def check_new_login(self, name):
        """Refuse a name no new login can have on this machine.

        Too long for its sockets, or equal to an existing login but for case:
        on a case-insensitive file system (the macOS default) the two would
        share every state file while the registry counted two logins.
        """
        if len(name) > LOGIN_NAME_MAX:
            ui.die(f"login name '{name}' is {len(name)} characters long; "
                   f"the limit is {LOGIN_NAME_MAX}")
        folded = name.casefold()
        for backend, other in registry.all_logins():
            if other != name and other.casefold() == folded:
                ui.die(f"login '{other}' already exists on {backend}, and "
                       f"'{name}' differs from it only in case",
                       "names that differ only in case share their files on a "
                       "case-insensitive file system; pick another name, or "
                       f"use '{other}'")
        for sock in (self.state.socket(name), self.state.mount_socket(name)):
            require_socket_path(sock)

    def _create(self, name, quiet=False, preferred=None):
        if name not in self.state.known_logins():
            self.check_new_login(name)
        self.backend.ensure_credential(quiet=quiet)
        self._cleanup_stale(name)

        limit = self.settings.int("MAX_LOGINS")
        if self.connection_count() >= limit:
            ui.die(
                f"already at {limit} cluster connections",
                "close one first (cluster close NAME), or raise the limit: "
                "cluster config set MAX_LOGINS N",
            )

        pinned = self.state.pin_read(name)

        # Opening the master IS the reachability test. Probing first and then
        # connecting doubles the two-hop latency for no extra information.
        if pinned:
            candidates = [pinned]
        elif preferred:
            candidates = list(preferred)
        elif self.backend.node_choosable:
            # One login per node: another login's pinned node is not a
            # candidate for a new login (sessions are node-local, and the
            # deterministic draw would otherwise happily stack names).
            avoid = ()
            if self.settings.flag("ONE_LOGIN_PER_NODE"):
                avoid = tuple(
                    self.state.pin_read(other)
                    for other in self.state.known_logins()
                    if other != name and self.state.pin_read(other))
            candidates = self.backend.node_candidates_for(name, avoid=avoid)
        else:
            candidates = [None]  # the balancer decides

        sock = self.state.socket(name)
        log = self.state.master_log_path(name)
        tries = self.settings.int("POOL_OPEN_TRIES")
        last_error = ""
        for index, node in enumerate(candidates):
            attempt = self.open_master(sock, node, log, tries=tries, quiet=quiet)
            opened, last_error = attempt
            if opened:
                break
            more = index + 1 < len(candidates)
            if not attempt.another_node:
                ui.die(
                    f"could not open login '{name}'",
                    last_error or "no error output",
                    *(["that is the credential failing, which every node would "
                       "repeat, so the other nodes were not tried"] if more else []),
                    f"check it with: {self.backend.credentials_command()}",
                    f"master log: {log}",
                )
            if node is not None and not quiet:
                ui.warn(f"{self.backend.short(node)} did not accept a connection"
                        + ("; trying the next node" if more else ""))
            if pinned:
                ui.die(
                    f"login '{name}' is pinned to {self.backend.short(pinned)}, which is "
                    "not accepting connections",
                    last_error or "no error output",
                    "its tmux sessions live on that node, so no other node is a "
                    "substitute.",
                    f"wait for it to come back, or move the login deliberately: "
                    f"cluster --backend {self.backend.name} repin {name} NODE",
                    f"(release the pin with: cluster --backend {self.backend.name} "
                    f"unpin {name})",
                )
        else:
            ui.die(
                f"could not open login '{name}' on any {self.backend.label} node",
                last_error or "no error output",
                f"master log: {self.state.master_log_path(name)}",
            )

        landed = self.refresh_meta(name)
        if not landed:
            ui.warn(f"login '{name}' came up but would not report its node")
            # It has a master, it will get sessions, and it will lose them when
            # this machine goes. Not knowing the node's name is no reason to
            # leave its work unprotected — and the assertion rides the master,
            # so it lands on the right node whether or not we can name it.
            self._protect(name)
            return True

        if pinned and self.backend.short(landed) != self.backend.short(pinned):
            # Never adopt an unexpected node: drop the master instead, so the
            # pin keeps pointing at where the real sessions are.
            self.close(name, keep_tmux=True, keep_pin=True, quiet=True)
            ui.die(
                f"login '{name}' is pinned to {self.backend.short(pinned)} but landed on "
                f"{self.backend.short(landed)}; dropped that connection",
                *self.backend.pin_hint(pinned, landed),
            )

        # A new connection is the first chance to protect whatever will run on
        # this node, and the cheapest one: the master is up and the node is
        # known.
        self._protect(name)
        return True

    def _protect(self, name):
        """Make this node keep the login's work when the last client goes.

        Best effort: a node that refuses to linger is still a perfectly usable
        login, and `doctor`, `cluster linger` and the watcher are where that
        gets reported rather than here, in the middle of someone connecting.

        One round trip, which also reconciles the node-side keeper against the
        setting in both directions — so a connection is on its own enough to
        bring a node into line, whichever way the setting was last moved.
        """
        linger.apply(self, name)

    def open_master(self, sock, node, log, tries=1, forward_agent=False,
                    env=None, quiet=False):
        """Open a control master at *sock* on *node*: a :class:`MasterOpen`.

        That is ``(opened, error detail)``, and on failure ``another_node``,
        which says whether a caller choosing among nodes should try the next.

        The one way a master is opened, for a login, a relocated mount or a
        transfer. The caller makes sure no live master holds *sock*; whatever
        is left there is discarded. ssh's own log goes to *log* (-E), since a
        backend that authenticates under a pty otherwise reports only an exit
        status.

        *tries* bounds the attempts, and only a failure worth repeating uses
        another. A pool address that accepts the connection and then closes it
        (``is_transient_setup``) is a draw to redraw, not a down node or a bad
        credential; a master that authenticated and was gone before it
        answered is worth one more try too. A rejected password fails at once.

        Each attempt claims its own TOTP window, so a caller must not pace
        first. The window is claimed even though a connection dropped at the
        banner never sent a code, which costs up to 30s per retry, but the
        alternative is inferring from ssh's output whether the code was typed,
        and being wrong there means a *reused* code, which the cluster rejects
        and which looks exactly like a wrong password.

        The shared record of a refused credential (state.Refusals) is the
        password's: a backend whose connections present none (a NERSC
        certificate) neither waits on it nor adds to it. A credential such a
        backend presents and ssh refuses is replaced once instead
        (Backend.credential_refused), and the new one gets a try of its own.

        Not fatal, so a caller can try another node, except where no attempt
        could work: a socket path this system cannot bind, no TOTP window, a
        refusal on record that holds this process, or a refused credential
        whose replacement could not be had.
        """
        sock, log = Path(sock), Path(log)
        require_socket_path(sock)
        config.private_dir(sock.parent)
        where = self.backend.short(node) if node else self.backend.pool_host
        tries = max(1, tries)
        opened, detail = False, ""
        self.held = ""
        recorded = self.backend.records_refusals
        refusals = self.state.refusals
        attempt, replaced = 1, False
        while True:
            # Looked at before the TOTP window is waited for, so that a
            # refusal on record costs nobody else a window, and again once it
            # is had: a refusal can be recorded during that wait. The second
            # look takes the confirming try when it is due, and anything that
            # ends the attempt gives it back.
            if recorded:
                self._refusal_gate(where, claim=False)
            try:
                self.state.claim_totp_window(log=None if quiet else ui.note)
                if recorded:
                    self._refusal_gate(where)
                opened, detail, again = self._attempt_master(
                    sock, node, log, forward_agent, env, quiet)
            except BaseException:
                if recorded:
                    refusals.released()
                raise
            if recorded:
                refusals.settle(opened, detail)
            elif opened:
                self.backend.credential_accepted()
            if opened:
                break
            if not recorded and not replaced and is_rejection(detail):
                replaced = True
                if self.backend.credential_refused(detail, quiet=quiet):
                    continue        # a new credential: its own try, not a repeat
            if not again or attempt >= tries:
                break
            attempt += 1
            if not quiet:
                ui.warn(f"{where}: {detail}; trying again ({attempt}/{tries})")
        self.last_failure = "" if opened else detail
        return MasterOpen(opened, detail, another_node=not is_rejection(detail))

    def _refusal_hold(self, claim=True):
        """Why the shared refusal record says not to authenticate now, or "";
        with *claim*, the confirming try is taken when it is due."""
        why = self.state.refusals.blocks(by_hand=self.backend.by_hand, claim=claim)
        if why:
            self.last_failure = self.held = why
        return why

    def _refusal_gate(self, where, claim=True):
        """Die when the shared refusal record says not to authenticate now;
        with *claim*, take the confirming try when it is due."""
        why = self._refusal_hold(claim)
        if why:
            ui.die(f"not authenticating to {where}", why,
                   f"check them with: {self.backend.credentials_command()}",
                   "try them now with: cluster login NAME")

    def authenticate_directly(self, where, run, required=False):
        """*run*()'s CompletedProcess, from an ssh that authenticates by itself
        and opens no master: a command run on a node directly, a disposable
        shell, rescue, init's test login.

        Held to what a master's open is (open_master): where connections
        present the password, the shared record of refusals is looked at
        before the TOTP window and again once it is had, and told how the
        authentication went (direct_outcome), so a refusal here holds the
        watchers as one there would and a success here frees them. A person
        connecting by hand goes ahead, told what was refused. last_failure
        says why it failed, or is "".

        None when nothing was sent: a refusal on record held this process, or
        no TOTP window could be had, as last_failure says. With *required*,
        either dies instead.
        """
        recorded = self.backend.records_refusals
        self.last_failure = self.held = ""

        def held(claim=True):
            if not recorded:
                return False
            if required:
                self._refusal_gate(where, claim=claim)
                return False
            return bool(self._refusal_hold(claim=claim))

        if held(claim=False):
            return None
        try:
            if self.backend.paces_totp:
                if required:
                    self.state.claim_totp_window(log=ui.note)
                elif not self.state.totp_pace(log=ui.note):
                    blocked = self.state.totp_blocked
                    self.last_failure = ("could not reserve a TOTP window for "
                                         "authentication"
                                         + (f": {blocked}" if blocked else ""))
                    return None
            if held():
                return None
            result = run()
        except BaseException:
            if recorded:
                self.state.refusals.released()
            raise
        opened, detail = direct_outcome(result)
        if recorded:
            self.state.refusals.settle(opened, detail)
        self.last_failure = "" if opened else detail
        return result

    def exec_directly(self, argv, where):
        """Hand the terminal to *argv*, an ssh that authenticates by itself
        (authenticate_directly): its status."""
        def run():
            said = []
            rc = self.backend.exec_interactive(argv, transcript=said)
            text = "".join(said)
            return subprocess.CompletedProcess(argv, rc, text, text)

        return self.authenticate_directly(where, run, required=True).returncode

    def _attempt_master(self, sock, node, log, forward_agent, env, quiet):
        """One try at a master: (opened, error detail, worth another try)."""
        discard(sock, grace=self.settings.int("STOP_TIMEOUT"))
        plat.rotate_log(log)
        argv = self.backend.ssh_argv(
            node=node, sock=sock, master=True, persist="yes",
            forward_agent=forward_agent, extra=["-N", "-f", "-E", str(log)])
        proc = self.backend.run_ssh(argv, timeout=config.MASTER_OPEN_TIMEOUT,
                                    capture=True, quiet=quiet, env=env)
        if proc.returncode == 0 and self.wait_socket_live(sock):
            return True, "", False
        lines = (proc.stderr or proc.stdout or "").strip().splitlines()
        try:
            lines += log.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            pass
        discard(sock, grace=self.settings.int("STOP_TIMEOUT"))
        # Not simply the last line: the server's banner is the last thing
        # printed, so every failure would be reported as "the server may need
        # to be upgraded" no matter what actually went wrong.
        detail = explain_failure(lines)
        if proc.returncode == 0:
            if detail == "no error output":
                detail = "the connection came up and was gone before it answered"
            return False, detail, True
        return False, detail, is_transient_setup(detail)

    def _cleanup_stale(self, name):
        sock = self.state.socket(name)
        if sock.exists() and not self.is_active(name):
            for pid in self.master_pids(name):
                self._stop_pid(pid)
            sock.unlink(missing_ok=True)

    def _stop_pid(self, pid, timeout=None):
        plat.stop(pid, timeout or self.settings.int("STOP_TIMEOUT"))

    def close(self, name, keep_tmux=False, keep_pin=False, quiet=False):
        """Stop the master. Never touches the pin unless told to."""
        sock = self.state.socket(name)
        if self.is_active(name):
            # Dropping the last connection to a node is the same moment a
            # reboot is, except that this one is seen coming — so it is the
            # moment to settle what the node should do afterwards, while there
            # is still a channel to say it on. Deliberately not branching on
            # `keep_tmux`: that flag records why the caller is here, not what
            # is running on the node, and `repin` passes it merely to avoid
            # killing sessions twice. See clustertool.linger.SETTLE.
            linger.settle(self, name)
        # No `-O check` first: it would cost what the exit costs, and an exit
        # sent to a master that has gone fails without doing anything.
        if sock.exists():
            control(sock, "exit", timeout=15)
        for pid in self.master_pids(name):
            self._stop_pid(pid)
        sock.unlink(missing_ok=True)
        if not keep_pin:
            self.state.pin_clear(name)
            self.state.forget_login_files(name)
        if not quiet:
            ui.info(f"closed login '{name}'")

    # --- interactive --------------------------------------------------------
    def disposable_interactive(self):
        """Open one direct shell and retain no managed connection or pin.

        This is intentionally not implemented as a temporary named login:
        doing so would create registry files, a ControlMaster and cleanup paths
        whose failure could turn "disposable" into a hidden durable resource.
        The only state touched is FASRC's shared TOTP pacing record, which keeps
        two simultaneous authentications from reusing a code, and its record of
        a refused credential (authenticate_directly).
        """
        self.backend.ensure_credential()
        argv = self.backend.ssh_argv(
            extra=["-t", "-o", "ControlMaster=no", "-o", "ControlPath=none",
                   "-o", "ControlPersist=no"],
        )
        saved_tty = plat.save_tty()
        plat.restore_tty(saved_tty)
        try:
            return self.exec_directly(argv, self.backend.pool_host)
        finally:
            plat.restore_tty(saved_tty)
            _resync_size()

    def interactive(self, name, remote_argv=None, tty=True, again=None):
        """Run an interactive session over the login, reconnecting on drops.

        A drop is ssh's own 255. A clean exit, Ctrl-C and the remote command's
        own status are the session's answer, and end it. Drops are kept in a
        backoff.FailureMemory (reconnect_memory): the time a session then stays
        up fades them, so one that drops now and then over days keeps coming
        back, and only more than INTERACTIVE_RETRIES in quick succession end
        it. The first connection is not retried: whoever typed the command is
        there to read why it failed.

        *again* is what a session outliving its channel (tmux) runs after a
        drop, in place of *remote_argv*: an attach that created its session
        goes back to it, and never makes a new one where it ended meanwhile.
        Without it the session is the channel's own (a shell), so a 255 over
        a connection that still answers came from the far side, the shell's
        own status or a signal that ended it, and is returned: a new shell
        would not be the one that ended.
        """
        memory = reconnect_memory(self.settings, "INTERACTIVE_RETRIES")

        # A remote tmux leaves mouse reporting and friends enabled on *our*
        # terminal; if the connection dies mid-session, ssh restores tty flags
        # but not those modes, and the next scroll arrives as literal "0;48;27M".
        saved_tty = plat.save_tty()
        # Also clear modes an earlier, already-crashed session left behind.
        plat.restore_tty(saved_tty)
        try:
            self.ensure(name)
            # Whoever typed the command was there for that connection; a
            # reconnect is made with nobody asked.
            self.backend.by_hand = False
            while True:
                remote = None
                if remote_argv:
                    remote = (remote_argv if isinstance(remote_argv, str)
                              else quote_remote(remote_argv))
                argv = rider_argv(self.state.socket(name),
                                  self.backend.target(self.node_of(name) or None),
                                  ["-t"] if tty else [], remote=remote)

                started = time.monotonic()
                rc = subprocess.run(argv).returncode
                lasted = time.monotonic() - started
                plat.restore_tty(saved_tty)

                # 255 is ssh's own transport failure; anything else came from the
                # remote command and is the caller's business, not a drop.
                if rc != 255:
                    return rc
                if again is None and self.answers(name):
                    return rc
                memory.failed(healthy=lasted)
                self._reconnect(name, memory,
                                f"connection to '{name}' dropped after {lasted:.0f}s")
                if again is not None:
                    remote_argv = again
        finally:
            plat.restore_tty(saved_tty)
            if tty:
                _resync_size()

    def _reconnect(self, name, memory, what):
        """Wait out *memory*'s backoff and get *name* usable again (ride_out)."""
        def attempt():
            self.last_failure = ""
            self.restore(name)

        ride_out(memory, what, attempt, "INTERACTIVE_RETRIES",
                 lambda: self.last_failure, to=f" to '{name}'")

    def restore(self, name):
        """Get a login usable again after something riding it lost its channel.

        A single multiplexed channel can die while the master is perfectly fine —
        the cheapest and safest recovery is then to do *nothing* and let the
        caller retry the channel. Tearing the master down unconditionally costs
        a full reauthentication: on FASRC that is a password and a fresh
        30-second TOTP window, spent for no reason.

        A rebuild that fails raises what _create raised, for the caller to
        judge (see ride_out).
        """
        if self._needs_no_rebuild(name):
            return
        # The lock spans teardown *and* rebuild: close() kills any ssh bound to
        # this control path, which would otherwise cut down an authentication the
        # watcher has in flight — and its repair would do the same to ours. So
        # no teardown happens without it.
        lock = self.state.login_lock(name, announce=self._announce_wait(name))
        if lock is None:
            self.last_failure = self.state.login_lock_blocked
            ui.die(f"could not rebuild login '{name}'", self.state.login_lock_blocked)
        try:
            # Whoever held the lock may just have rebuilt this login; tearing
            # down that fresh master would spend another authentication and
            # drop whatever has started riding it. A master that is still
            # slow to answer is the one just judged, not a new one.
            if self._needs_no_rebuild(name, patient=False):
                return
            self.close(name, keep_tmux=True, keep_pin=True, quiet=True)
            self._create(name, quiet=True)
        finally:
            lock.release()

    def _round_trip(self, name):
        """printf ok over the login's master, given REMOTE_CHECK_TIMEOUT."""
        return self.run_remote(name, "printf ok",
                               timeout=self.settings.int("REMOTE_CHECK_TIMEOUT"))

    def answers(self, name):
        """Whether the login's connection carries a round trip now: a new
        channel, so a master on its way out, which closes what it carried a
        moment before its process ends, does not pass."""
        if not self.is_active(name):
            return False
        proc = self._round_trip(name)
        return proc.returncode == 0 and (proc.stdout or "").strip() == "ok"

    def _needs_no_rebuild(self, name, patient=True):
        """True when rebuilding the master would not help the login.

        It answers, or it is up but out of channels, or it is up and did not
        answer within REMOTE_CHECK_TIMEOUT for the first time in a row
        (*patient*). A node that is slow to start a session is not a broken
        connection: rebuilding it would drop everything else on it and spend a
        TOTP window, where trying the channel again costs nothing. Only a
        master that is gone, or a second no-answer in a row, is rebuilt.
        """
        if not self.is_active(name):
            self._unanswered.pop(name, None)
            return False
        timeout = self.settings.int("REMOTE_CHECK_TIMEOUT")
        proc = self._round_trip(name)
        if proc.returncode == 0 and (proc.stdout or "").strip() == "ok":
            self._unanswered.pop(name, None)
            return True
        if proc.returncode == 124 and patient and self.is_active(name):
            self._unanswered[name] = self._unanswered.get(name, 0) + 1
            if self._unanswered[name] < 2:
                ui.warn(f"login '{name}' did not answer within {timeout}s "
                        "(REMOTE_CHECK_TIMEOUT), but its connection is up; "
                        "trying again over it rather than rebuilding it")
                return True
            ui.warn(f"login '{name}' did not answer twice in a row; rebuilding "
                    "its connection")
        self._unanswered.pop(name, None)
        # A live master that cannot open a channel is out of sessions, not
        # broken, and rebuilding it is the worst available response: close()
        # kills every ssh on this control path, so it drops the user's other
        # attaches and breaks any transfer in flight — and it spends a TOTP
        # window to replace a connection that was fine. Say what is actually
        # wrong and let the caller's retry budget wait for a channel.
        if out_of_channels(proc):
            held = self.channel_clients(name)
            limit = self.settings.int("SSH_MAX_SESSIONS")
            ui.warn(f"login '{name}' has no free channels "
                    f"({len(held)} of ~{limit} in use on one connection)")
            for kind in sorted({k for _, k in held}):
                ui.note(f"  {sum(1 for _, k in held if k == kind)} x {kind}")
            ui.note("close an attach, or wait for the transfer to finish; "
                    f"see: cluster channels {name}")
            return True
        return False


def _is_ssh(cmd):
    """Is this command line an ssh client invocation?

    Matched on argv[0]'s basename, not on "ssh" appearing anywhere: sshfs, and
    any wrapper that passes -o ControlPath along, mention it too.
    """
    head = cmd.split(" ", 1)[0]
    return Path(head).name in ("ssh", "ssh.exe")


def out_of_channels(proc):
    """Did this ssh fail because the server's session table is full?

    sshd refuses the overrun with SSH2_OPEN_CONNECT_FAILED and the message
    "open failed", which ssh renders as

        channel 22: open failed: connect failed: open failed

    naming neither MaxSessions nor sessions. Older sshd, and a server with
    `MaxSessions 0`, say "administratively prohibited" instead. Matching the
    rendered text is unlovely but it is the only signal that crosses the wire.
    """
    text = f"{getattr(proc, 'stderr', '') or ''}{getattr(proc, 'stdout', '') or ''}"
    lowered = text.lower()
    if "open failed" not in lowered:
        return False
    return ("connect failed" in lowered
            or "administratively prohibited" in lowered
            or "open failed: open failed" in lowered)


def quote_remote(argv):
    """Build a remote command line from an argv list.

    Note the consequence: quoting means ``~`` reaches the cluster literally and does *not* expand.
    Callers who want shell expansion must ask for a shell explicitly.
    """
    return " ".join(shlex.quote(str(a)) for a in argv)
