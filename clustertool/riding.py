"""Riding out a lost connection: a transfer that runs for hours or days
over a master, stopped when the master is lost and run again once it is back.

Losses are judged by evidence: the master's process gone, or a socket nothing
holds. rclone's exit status says nothing about which, and a master slow to
answer has not gone. The waits between reconnects and how many losses in
quick succession end a transfer are the reconnect backoff's
(sshmux.reconnect_memory), which the settings name.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time

from . import platform as plat, ui
from .sshmux import UNANSWERED, master_pid, reconnect_memory, ride_out


#: Seconds between looks at the masters a running transfer rides. A look is
#: whether the master's process is still there, which asks nothing of it.
LINK_POLL = 10

#: The status a direct transfer's rclone ends with when the executor stopped
#: it for hearing nothing from this machine (crossxfer.TIED) while its
#: connection stayed up. rclone's own statuses run from 0 to 10.
SILENCED = 75


class Link:
    """A master that a transfer rides, and how to get it back.

    Lost means the master the run started on is gone, or has been replaced:
    every channel on it went with it, the transfer's included. rclone would
    notice only after spending all its retries on a connection that cannot
    come back, which takes minutes, so a running transfer watches its links
    itself (Ride). *restore*(lost_pid) gets a usable master back at *sock*,
    raising what an open raises when it cannot, and returns the node it is
    on (None for the pool's choice). *logins* is whose last_failure explains
    a refused open.
    """

    def __init__(self, name, sock, restore, logins, node=None):
        self.name, self.sock, self.node = name, sock, node
        self.logins = logins
        self._restore = restore
        self.master = None
        self._take()

    def _take(self):
        """Note the pid of the master at the socket, if it says it; False
        when nothing is there."""
        now = master_pid(self.sock)
        if now is not UNANSWERED and now:
            self.master = now
        return now is not None

    def lost(self):
        """Whether the master the run started on has exited.

        By its pid, read from the master when the link was taken: a look is a
        kill(0), which asks nothing of the master, so a slow machine cannot
        make a live one look lost. A master that did not say its pid then is
        asked for it now, and is lost only if nothing holds its socket: one
        slow to answer is still there.
        """
        if not self.master:
            return not self._take()
        return not plat.pid_alive(self.master)

    def answers(self):
        """Whether the master still holds its socket as the one the run
        started on: asked when a run has failed, since a master on its way
        out closes what it carried a moment before its process ends. One
        there but slow to answer has not gone, so the failure is the run's
        own, and the master is not replaced for it."""
        now = master_pid(self.sock)
        if now is UNANSWERED:
            return True
        return now is not None and (not self.master or now == self.master)

    def broken(self):
        """Lost, or no longer answering as the master the run started on."""
        return self.lost() or not self.answers()

    def restore(self):
        self.node = self._restore(self.master)
        self.master = None
        self._take()


class Ride:
    """The connections one transfer rides, and the losses it has ridden out.

    A run whose connection is lost is stopped, the connection restored after
    the reconnect backoff (sshmux.ride_out), and the transfer (rclone, or
    rsync for push and pull) run again, which carries on where it stopped:
    what arrived intact is not sent again.
    Losses are kept in one backoff.FailureMemory for the whole transfer,
    faded by the time each run works, so a transfer that runs for days rides
    out a loss now and then, and only more than TRANSFER_RECONNECTS in quick
    succession end it. A refused credential ends it at once. A run that
    fails while its connections are fine is the program's answer, and
    stands, whatever its status: the links are asked, not the status read.

    *heartbeat* and *settle* are for an rclone on the far side of a link,
    which stops itself once this machine has been silent for a while: it is
    sent a line at every look (run_riding), and after a loss the next run
    starts no sooner than *settle* seconds later, when the one before it has
    stopped, so that two never copy the same files at once. *settled*(),
    when given, is whether the one before has certainly stopped already,
    asked at once and at every look while the settle lasts, and a yes ends
    the wait there. A run that ends
    with *silenced* was stopped that way while its link stayed up, as when
    this machine is suspended: it is resumed at once, since the far side
    has stopped already and there is nothing to reconnect. *quiet* keeps
    the notes about resuming to itself. *once*, when given, is why the
    transfer cannot be run again, and a lost connection then ends it, saying
    so.
    """

    def __init__(self, links, settings, env=None, heartbeat=False, settle=0,
                 silenced=None, quiet=False, once=None, settled=None):
        self.links = list(links)
        self.once = once
        self.settings = settings
        self.env = env
        self.heartbeat = heartbeat
        self.settle = settle
        self.settled = settled
        self.silenced = silenced
        self.quiet = quiet
        self.memory = reconnect_memory(settings, "TRANSFER_RECONNECTS")

    def run(self, command):
        """The transfer's status, from as many runs as it takes; *command*()
        is its argv, built afresh for each run so it names the node a
        restored link is on."""
        while True:
            started = time.monotonic()
            rc, lost = run_riding(command(), self.links, self.settings, self.env,
                                  heartbeat=self.heartbeat)
            lost_at = time.monotonic()
            lasted = lost_at - started
            if not lost and self.silenced is not None and rc == self.silenced:
                self._resume_silenced(lasted)
                continue
            if not lost and rc != 0:
                # Lost after the last look, on its way out, or with nothing
                # left to look at.
                lost = [link for link in self.links if link.broken()]
            if not lost:
                return rc
            if self.once:
                names = " and ".join(link.name for link in lost)
                ui.warn(f"{names} was lost {lasted:.0f}s into the transfer; it is "
                        f"not resumed, since {self.once}")
                return rc
            self.memory.failed(healthy=lasted)
            self._restore(lost, lasted)
            self._settle(lost_at + self.settle)
            self._info("resuming the transfer; what arrived intact is not sent again")

    def _settle(self, until):
        """Wait until *until* for the run that lost its connection to stop
        itself, or until settled() says it has."""
        noted = False
        while True:
            left = until - time.monotonic()
            if left <= 0 or (self.settled is not None and self.settled()):
                return
            if not noted:
                self._note(f"waiting up to {left:.0f}s for the rclone that lost "
                           "its connection to stop itself there")
                noted = True
            time.sleep(min(left, LINK_POLL))

    def _resume_silenced(self, lasted):
        self.memory.failed(healthy=lasted)
        if self.memory.exhausted:
            ui.die(f"the far side's rclone stopped {self.memory.fresh} times in "
                   "quick succession, each time hearing nothing from this machine "
                   "while the connection stayed up",
                   "this machine may be suspending, or its network stalling",
                   f"TRANSFER_RECONNECTS ({self.memory.limit}) is how many in "
                   "quick succession are ridden out")
        ui.warn(f"the far side's rclone heard nothing from this machine for too "
                f"long and stopped, {lasted:.0f}s into the run; the connection "
                "is up")
        self._info("resuming the transfer; what arrived intact is not sent again")

    def _note(self, text):
        if not self.quiet:
            ui.note(text)

    def _info(self, text):
        if not self.quiet:
            ui.info(text)

    def _restore(self, lost, lasted):
        names = " and ".join(link.name for link in lost)
        trying = []

        def attempt():
            for link in lost:
                if link.broken():
                    trying[:] = [link]
                    link.logins.last_failure = ""
                    link.restore()

        ride_out(self.memory, f"{names} was lost {lasted:.0f}s into the transfer",
                 attempt, "TRANSFER_RECONNECTS",
                 lambda: trying[0].logins.last_failure if trying else "",
                 to=f" {names}")


def run_riding(argv, links, settings, env=None, heartbeat=False):
    """``(status, links lost)`` of one run of *argv*, stopped if a link is lost.

    Not captured: the program's progress is the point, and a transfer that
    runs for hours must not be buffered until it ends. With *heartbeat*, its
    stdin is a pipe given a line at every look (see Ride).
    """
    child = subprocess.Popen(argv, env=env,
                             stdin=subprocess.PIPE if heartbeat else None)
    if heartbeat:
        # A beat that cannot be written now is skipped, never waited for:
        # the pipe fills only when the far side has stopped reading.
        os.set_blocking(child.stdin.fileno(), False)
    # Waited for in a thread, so the looks sleep between them: a wait with a
    # timeout polls the child many times a second, for as long as it runs.
    ended = threading.Event()
    waiter = threading.Thread(target=lambda: (child.wait(), ended.set()),
                              daemon=True)
    waiter.start()
    try:
        while not ended.wait(LINK_POLL):
            if heartbeat:
                _beat(child.stdin)
            lost = [link for link in links if link.lost()]
            if lost:
                plat.stop(child, settings.int("STOP_TIMEOUT"))
                return child.returncode, lost
        return child.returncode, []
    except BaseException:
        # Interrupted (Ctrl-C, a terminating signal): the program goes too.
        plat.stop(child, settings.int("STOP_TIMEOUT"))
        raise
    finally:
        if child.stdin is not None:
            try:
                child.stdin.close()
            except OSError:
                pass


def _beat(pipe):
    try:
        os.write(pipe.fileno(), b"\n")
    except OSError:
        pass
