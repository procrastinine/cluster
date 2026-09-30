"""The per-login watcher.

It keeps one login's connection and mount alive, re-asserts the node's linger
state so the tmux server survives losing every connection at once, and
periodically records the node's tmux layout so a workspace can be rebuilt after
a reboot.

Two rules are load-bearing:

* **The watcher never gives up.** Failed ticks are kept in a
  backoff.FailureMemory that healthy ticks fade (WATCH_FAILURE_HALF_LIFE);
  after the WATCH_RETRIES-th of them in quick succession it waits longer
  between ticks, doubling up to WATCH_BACKOFF_MAX, but it keeps watching — a login
  that cannot be repaired for an hour must still recover on its own when the
  cluster does.
* **It skips a tick while the login lock is held.** A watcher repairing a login
  at the same moment as a foreground command would kill that command's
  in-flight authentication, and vice versa.

It does stop *reconnecting* while the backend's shared record of a refused
credential says to (state.Refusals): retrying a rejected password only
repeats the refusal, spends a TOTP window each time on FASRC, and is how an
account gets locked. A refusal is tried once more, by one process, after
REFUSAL_CONFIRM_DELAY; a second one holds until the credentials change or
the login is connected by hand. It says which, and a tick spent waiting on
it counts neither way.

One watcher per login: it holds ``watch-<login>.lock`` for its lifetime and
writes its own pid file once it has the lock.
"""

from __future__ import annotations

import http.client
import os
import signal
import sys
import time
from pathlib import Path

from . import backoff, linger, platform as plat
from .auth import failure_text
# Imported by name rather than as a module: `mounts` is also an attribute here,
# and `mounts.ANSWERED` next to `self.mounts` reads like a bug.
from .mounts import ANSWERED, BUSY

#: What a failed reconnect or repair can raise, all of it logged and counted
#: as one failed attempt: a refusal (SystemExit, ui.Die), an unreadable secret
#: (OSError, ValueError), and an sshproxy answer cut short
#: (http.client.HTTPException, which is not an OSError).
FAILURES = (SystemExit, OSError, ValueError, http.client.HTTPException)


class Watcher:
    def __init__(self, logins, mounts, tmux, name):
        self.logins = logins
        self.mounts = mounts
        self.tmux = tmux
        self.name = name
        self.state = logins.state
        self.settings = logins.settings
        self.stop = False
        #: Which login this one is borrowing the backend's mount from, so the
        #: reason for not remounting is logged on change rather than every tick.
        self._sharing = ""
        #: Stray records already reported, for the same reason.
        self._strays_logged = []
        #: Last linger verdict, so the steady state stays silent. None until
        #: the first assertion, which is why a watcher start says it once.
        self._linger_ok = None
        #: Why reconnecting waits on a refused credential ("" when it does
        #: not), so the reason is logged when it changes.
        self._refused = ""
        #: Whether "mounts are off" has been said.
        self._mounts_off = False

    def log(self, message):
        self._keep_log_bounded()
        print(f"[{time.strftime('%F %T')}] {self.name}: {message}", flush=True)

    def _keep_log_bounded(self):
        """Start a fresh watch log once this one passes plat.LOG_LIMIT.

        The log is this process's stdout and stderr (see start_watcher), and a
        watcher runs for months. The full log becomes ``<log>.1`` and the
        descriptors are pointed at a new file, which nothing else can do for a
        process that holds the old one open.
        """
        path = self.state.watch_log_path(self.name)
        try:
            held = os.fstat(sys.stdout.fileno())
            named = os.stat(path)
        except (OSError, ValueError):
            return
        if (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino):
            return
        if held.st_size <= plat.LOG_LIMIT or not plat.rotate_log(path):
            return
        sys.stdout.flush()
        sys.stderr.flush()
        fresh = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.dup2(fresh, sys.stdout.fileno())
            os.dup2(fresh, sys.stderr.fileno())
        finally:
            os.close(fresh)

    def run(self):
        """Watch until SIGTERM or SIGHUP. 1 if another watcher already has this login."""
        lock = plat.FileLock(self.state.watch_lock_path(self.name))
        if not lock.acquire():
            self.log("another watcher is already watching this login; exiting")
            return 1
        pid_path = self.state.watch_pid_path(self.name)
        plat.atomic_write_text(pid_path, f"{os.getpid()}\n")
        try:
            return self._watch()
        finally:
            if _recorded_pid(pid_path) == os.getpid():
                pid_path.unlink(missing_ok=True)
            lock.release()

    def _watch(self):
        # Stop between ticks rather than unwinding from inside one, so the
        # linger assertion on the way down still runs. SIGHUP too: the watcher
        # has no terminal, so a hangup can only be someone asking it to go.
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(signum, self._on_signal)

        interval = self.settings.int("WATCH_INTERVAL")
        # No limit: it backs off, and goes on. The first WATCH_RETRIES - 1
        # failures in quick succession cost no extra wait, the WATCH_RETRIES-th
        # one interval more, and each after that twice the one before.
        failures = backoff.FailureMemory(
            half_life=self.settings.int("WATCH_FAILURE_HALF_LIFE"),
            delay=interval, delay_max=self.settings.int("WATCH_BACKOFF_MAX"),
            grace=self.settings.int("WATCH_RETRIES") - 1)
        layout_every = self.settings.int("LAYOUT_INTERVAL")
        linger_every = self.settings.int("LINGER_INTERVAL")
        failback_ticks = self.settings.int("MOUNT_FAILBACK_TICKS")

        repair_attempt = 0
        healthy_streak = 0
        last_layout = 0.0
        last_linger = 0.0

        self.log("watcher started")
        while not self.stop:
            slept = interval

            if self.state.login_lock_busy(self.name):
                self.log("login is busy elsewhere; skipping this tick")
                self._sleep(slept)
                continue

            ok = self._tick()
            if ok is None:
                # Waiting on a refused credential: not a failure to back off
                # from, and not health either.
                self._sleep(slept)
                continue

            if ok:
                repair_attempt = 0
                healthy_streak += 1
                failures.healthy(interval)
            else:
                failures.failed()
                repair_attempt += 1
                healthy_streak = 0
                extra = failures.wait()
                if extra:
                    slept = interval + extra
                    self.log(f"{failures.fresh} failed ticks in quick succession; "
                             f"backing off {extra:.0f}s (still watching)")

            if ok and healthy_streak and healthy_streak % max(1, failback_ticks) == 0:
                # Ask policy where the mount belongs rather than assuming it is
                # the login node: on NERSC a DTN mount is the normal state, so
                # announcing a move back to the login would be both wrong and
                # noise.
                target = self.mounts.home_mount_target(self.name)
                if target:
                    self.log("mount is off its home node; moving it to "
                             f"{self.mounts.backend.short(target)}")
                    self.mounts.failback(self.name, quiet=False)

            # Before the snapshot, and on its own shorter cadence: a snapshot
            # describes work that linger is what keeps alive.
            if ok and time.monotonic() - last_linger >= linger_every:
                self._assert_linger()
                last_linger = time.monotonic()

            if ok and time.monotonic() - last_layout >= layout_every:
                self._snapshot()
                last_layout = time.monotonic()

            self._repair_if_needed(ok, repair_attempt)
            self._sleep(slept)

        # The one disconnect this machine announces. A reboot SIGTERMs the
        # watcher seconds before every connection to the node goes away, which
        # is the exact instant the node decides whether to keep the tmux
        # server — so the last thing done here is to make sure it will. An
        # abrupt loss still falls back to the timer above, and to luck.
        #
        # That ordering comes from the systemd units. A Mac has no equivalent:
        # launchd may stop the watcher after the network is already gone, so
        # this assertion can find no master there, and linger on a Mac rests
        # on the periodic LINGER_INTERVAL assertions made while it was up.
        self._assert_linger(force=True)
        self.log("watcher stopping")
        return 0

    def _on_signal(self, *_):
        self.stop = True

    def _sleep(self, seconds):
        deadline = time.monotonic() + seconds
        while not self.stop and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))

    def _tick(self):
        """True when login and mount are both healthy; None when the login is
        down and waits on a refused credential."""
        if not self.logins.is_active(self.name):
            if self._waiting_on_refusal():
                return None
            self.log("login is down; reconnecting")
            self.logins.last_failure = self.logins.held = ""
            try:
                self.logins.ensure(self.name, quiet=True)
            except FAILURES as exc:
                # Held before anything was sent (another process took the
                # confirming try, say) is no failed reconnect; and one refused
                # now waits on the record like any other.
                held = self.logins.held
                if not held:
                    self.log("reconnect failed: "
                             + failure_text(exc, self.logins.last_failure))
                return None if self._waiting_on_refusal() or held else False
            if not self.logins.is_active(self.name):
                return False
            self.log("login restored")

        if not self.settings.flag("AUTO_MOUNT"):
            return True
        missing = plat.mount_tools_missing()
        if missing:
            if not self._mounts_off:
                self.log(f"{' and '.join(missing)} is not installed, so mounts are off")
                self._mounts_off = True
            return True
        if not self.mounts.is_mounted(self.name):
            holder = self.mounts.mounted_elsewhere(self.name)
            if holder:
                # This login has no mount because the backend's one mount lives
                # on `holder`. Remounting would recreate exactly the duplicate
                # auto_mount declined to make — and would do it every tick.
                if self._sharing != holder:
                    self.log(f"mount shared with login '{holder}'; "
                             f"nothing to remount here")
                    self._sharing = holder
                return True
            self._sharing = ""
            self.log("mount is gone; remounting")
            return self.mounts.try_mount(self.name, quiet=True)
        self._sharing = ""

        status, detail = self.mounts.probe(self.name)
        if status == ANSWERED:
            return True
        if status == BUSY:
            # Saturated, not broken. Remounting would abandon in-flight work and
            # the queue would refill the moment whatever is scanning resumes, so
            # this counts as healthy and is only noted.
            self.log(f"mount {detail}; leaving it alone")
            return True
        self.log(f"mount unhealthy: {detail}")
        return False

    def _repair_if_needed(self, ok, attempt):
        if ok or attempt == 0:
            return
        self.logins.last_failure = ""
        try:
            if self.mounts.repair(self.name, attempt=attempt, quiet=True, log=self.log):
                self.log("repair succeeded")
        except FAILURES as exc:
            self.log(f"repair aborted: {failure_text(exc, self.logins.last_failure)}")

    def _waiting_on_refusal(self):
        """Whether the shared refusal record says not to reconnect now, logged
        when what it says changes. The confirming try, when it is due, is
        this watcher's to take: its reconnect claims it."""
        holds = self.logins.backend.refusal_holds_connections()
        why = self.state.refusals.blocks(claim=False) if holds else ""
        if why != self._refused:
            if why:
                self.log(f"not reconnecting: {why}; check them with: "
                         f"{self.logins.backend.credentials_command()}")
            elif not holds:
                self.log("a reconnect needs nothing that was refused; "
                         "reconnecting")
            elif self.state.refusals.current() is not None:
                self.log("trying the refused credentials once more")
            else:
                self.log("the refused credential is no longer on record "
                         "(changed, or a connection worked); reconnecting")
            self._refused = why
        return bool(why)

    def _assert_linger(self, force=False):
        """Keep the node willing to hold this login's tmux past the last client.

        Logged on change, or always when *force* — the steady state is an
        assertion that changes nothing and must not narrate itself every
        minute, but the one made on the way down is the record of whether the
        work was protected before the machine went, which is precisely the
        question asked afterwards.
        """
        if not linger.required(self.logins):
            return
        if not self.logins.is_active(self.name):
            # Nothing to assert over, and nothing useful to claim about the
            # node: a stopped watcher whose login was already down would
            # otherwise report "could not assert" and read, afterwards, like
            # the reason the work went.
            if force:
                self.log("no connection left to assert linger over")
            return
        # On the way down, take the time: that assertion is the last one there
        # will be. On a tick, do not — a login node slow enough to sit on the
        # generous default would halve this watcher's responsiveness every
        # minute, and a tick that misses is retried in another LINGER_INTERVAL.
        timeout = ({} if force else
                   {"timeout": self.settings.int("WATCH_INTERVAL")})
        # Reconcile the node-side keeper against the setting on the first
        # assertion and on the last one, and only assert linger in between. A
        # watcher is started by every boot and every reconnect, so "first
        # tick" is often enough to pick up a changed LINGER_KEEPER without
        # putting a `crontab -l` on the node once a minute forever for a
        # setting that changes about twice a year.
        act = linger.apply if (force or self._linger_ok is None) \
            else linger.assert_enabled
        ok = act(self.logins, self.name, **timeout)
        if ok == self._linger_ok and not force:
            return
        self._linger_ok = ok
        # Not "linger is off": a node too busy to answer in time looks the same
        # from here, and this line is read after the fact by someone asking why
        # their work went. It says what was observed, and where to go to find
        # out which it was.
        self.log("linger asserted; tmux here outlives this connection" if ok else
                 "could not assert linger (node refusing or too slow); tmux "
                 "here may NOT survive losing every connection — cluster doctor")

    def _snapshot(self):
        if self.tmux.layout_save(self.name):
            added, removed = self.tmux.crumb_sync(self.name)
            if added or removed:
                self.log(f"breadcrumbs reconciled (+{added} -{removed})")
            # Sessions recorded where no login is looking are never pruned from
            # here — see crumb_sync — but they must not stay silent either: a
            # leak that only shows up when a later command refuses is a leak
            # nobody can date. Logged on change, not every tick.
            seen = sorted(f"{s.node}:{s.session}" for s in self.tmux.last_strays)
            if seen != self._strays_logged:
                self._strays_logged = seen
                if seen:
                    self.log(f"{len(seen)} stray record(s): {', '.join(seen)}"
                             " — reconcile with: cluster strays")


def _recorded_pid(path):
    try:
        return int(Path(path).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
