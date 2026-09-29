"""SSHFS mounts, and surviving a storage meltdown.

The case this module is built around: a login node's NFS collapses and its
sftp-server dies *unreaped*.

* **A sick mount must not tear down the login.** Repair is a ladder — unwedge and
  remount over the existing master (free, no authentication), then move the mount
  to another node, and only then rebuild the login. Rebuilding the login first
  would kill every session and command riding it to fix one mount, and spend an
  authentication doing so.
* **A wedged mount cannot be probed with stat(2) and cannot be killed.** Requests
  queue in ``request_wait_answer`` with SIGKILL pending and undeliverable, so
  ``timeout N stat`` never fires and every checker joins the pile. The prober is
  therefore *abandoned* on deadline rather than waited on, and recovery goes
  through the FUSE abort file.
* **The probe must be an uncached lookup.** ``stat $mp/.`` is served from FUSE's
  attribute cache and reports a dead mount healthy for ~20 seconds.
* **A mount has no node affinity.** Home, scratch and project space are shared
  across the pool on both clusters, so a mount whose node stops serving it can be
  re-established elsewhere while the login stays pinned with its tmux sessions.
"""

from __future__ import annotations

import os
import random
import re
import shlex
import subprocess
import time
from pathlib import Path

from . import config, platform as plat, ui
from .auth import is_rejection
from .sshmux import discard, rider_argv

#: BUSY is deliberately distinct from NO_ANSWER: a saturated mount and a wedged
#: one both fail to answer a probe in time, but only one of them is broken.
ANSWERED, ERRORED, NO_ANSWER, BUSY = 0, 1, 2, 3

#: One place to name these, so a new state cannot KeyError a display path.
STATUS_LABEL = {ANSWERED: "ok", ERRORED: "error", NO_ANSWER: "wedged", BUSY: "busy"}

#: States that need no intervention.
STATUS_OK = (ANSWERED, BUSY)

SSHFS_OPTS = [
    "reconnect",
    "ServerAliveInterval=15",
    "ServerAliveCountMax=3",
    "idmap=user",
    "follow_symlinks",
]

#: What a probe's mkdir says when the transport under the mount is gone:
#: ENOTCONN on Linux, ENXIO from a dead macFUSE daemon.
_DEAD_TRANSPORT = ("not connected", "transport endpoint", "connection reset",
                   "device not configured")


class Mounts:
    def __init__(self, logins, tmux=None):
        self.logins = logins
        self.backend = logins.backend
        self.state = logins.state
        self.settings = logins.settings
        self.tmux = tmux

    # --- inspection ---------------------------------------------------------
    def mountpoint(self, name):
        meta = self.state.read_meta(name)
        return Path(meta.get("mountpoint") or self.state.default_mountpoint(name))

    def remote_path(self, name):
        meta = self.state.read_meta(name)
        return meta.get("remote") or self.backend.home_remote()

    def is_mounted(self, name):
        return plat.mount_table_has(self.mountpoint(name))

    def mounted_elsewhere(self, name):
        """Another login of this backend already mounting the same filesystem.

        Every login node of a backend serves one home, so a second sshfs is a
        duplicate: the same bytes under a second path, paid for with one of that
        connection's ten channels — and on a busy day the channel budget is what
        actually runs out. Returns the holder's name, or "".

        Reads the kernel's mount table and local state only: no probe and no
        round trip, because whether that mount is *healthy* is its own login's
        watcher's business, not this login's.
        """
        if not self.settings.flag("ONE_MOUNT_PER_BACKEND"):
            return ""
        default = self.settings.str("DEFAULT_LOGIN")
        others = [other for other in self.state.known_logins() if other != name]
        # When more than one login is mounted, name the default one: its path is
        # what everything else already references (archive-sync filters, editor
        # excludes), and it outlives the scratch logins around it.
        others.sort(key=lambda other: (other != default, other))
        for other in others:
            if plat.mount_table_has(self.mountpoint(other)):
                return other
        return ""

    def home_mount_node(self, name):
        """Where this mount belongs when nothing is wrong.

        For a backend that mounts over the login (FASRC) that is the login's own
        node. For one that mounts on a dedicated I/O node (NERSC) it is a
        MOUNT-purpose node, chosen deterministically from the login name so a
        given login keeps using the same DTN.
        """
        import hashlib

        if self.backend.mount_via != "mount_node":
            return None
        candidates = self.backend.mount_nodes()
        if not candidates:
            return None
        index = int(hashlib.sha256(name.encode()).hexdigest()[:8], 16) % len(candidates)
        return candidates[index]

    def sshfs_pids(self, name, mountpoint=None):
        """sshfs daemons serving this mount.

        The mountpoint must be a whole argument, so the daemon of
        ``…/fasrc/main2`` is never taken for that of ``…/fasrc/main``.
        """
        mp = str(mountpoint or self.mountpoint(name))
        return [pid for pid, cmd in plat.own_processes()
                if _program(cmd) == "sshfs" and _has_argument(cmd, mp)]

    def sftp_channel_pids(self, name):
        """ssh processes carrying *this mount's* sftp channel.

        Matching only ``-s sftp`` on the socket would also match rclone's
        ``--sftp-ssh`` channels riding the same master, and tearing a mount down
        would kill an unrelated data sync. Only sshfs adds ClearAllForwardings,
        so require it. The socket is matched as the whole ControlPath value,
        so another login's socket that merely starts the same way is not.
        """
        paths = [re.escape(str(sock)) for sock in
                 (self.state.socket(name), self.state.mount_socket(name))]
        rides = re.compile(rf"(?:^|\s|-o)ControlPath=(?:{'|'.join(paths)})(?:\s|$)")
        return [pid for pid, cmd in plat.own_processes()
                if _program(cmd) == "ssh" and "ClearAllForwardings" in cmd
                and "sftp" in cmd.split() and rides.search(cmd)]

    # --- health -------------------------------------------------------------
    def probe(self, name, timeout=None):
        """(status, detail) without ever blocking on the mount.

        The prober is orphaned deliberately: on a wedged mount it can never be
        reaped, so waiting on it is the bug this avoids.
        """
        timeout = timeout or self.settings.int("MOUNT_CHECK_TIMEOUT")
        mp = self.mountpoint(name)
        if not plat.mount_table_has(mp):
            return ERRORED, "not mounted"

        # Each probe gets its own result file. A shared path is unsafe two ways:
        # the watcher and a hand-run `cluster mounts` probe concurrently, and —
        # worse — a prober orphaned on an earlier wedged probe can unblock
        # minutes later and write it, so the *next* probe would read a stale
        # "rc=0" and call a dead mount healthy. Reading only a path nobody else
        # can write makes a late answer land where no one is listening.
        result = self.state.dir / f"probe-{name}-{os.getpid()}-{_token()}.result"
        self._sweep_probe_results(name, keep=result)
        self._spawn_probe(mp, result)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            answer = _probe_answer(result)
            if answer is not None:
                return answer
            time.sleep(0.15)
        # Distinguish "slow because saturated" from "dead". Both miss the
        # deadline, but a wedged connection is *not serving requests at all* —
        # one observed stuck at exactly 13 for days — while a saturated one keeps
        # turning over. A recursive scanner is enough to cause this: VS Code's
        # file search spawns ripgreps that walk the whole cluster home (and, with
        # search.followSymlinks on, the lab storage it points at) and leave a
        # dozen requests permanently in flight. Remounting for that is pure
        # churn, so a queue that is merely deep is reported BUSY and left alone.
        #
        # Depth is a level, not a rate, and the kernel exposes no served-request
        # counter — so liveness is read off *movement* in the depth. A
        # continuously saturated queue never dips below where it started: it is
        # refilled as fast as it drains, and was measured holding 14,14,…,15,14
        # for fifteen seconds on a mount that was answering (slowly) the whole
        # time. Any movement, in either direction, means requests are completing.
        waiting = plat.fuse_waiting(mp)
        if waiting is None:
            return self._judge_by_cpu(name, mp, result, timeout)
        if waiting:
            seen = {waiting}
            deadline = time.monotonic() + self.settings.int("MOUNT_BUSY_GRACE")
            while time.monotonic() < deadline:
                time.sleep(0.25)
                # Must be the *same* test as the main loop: a bare
                # `result.is_file()` is true from the instant the prober's `2>`
                # redirect creates the file, so it would count a stale or
                # unfinished result as an answer and call a wedged mount healthy.
                answer = _probe_answer(result)
                if answer is not None:
                    return answer
                now = plat.fuse_waiting(mp)
                if now is None:
                    break
                if now == 0:
                    return ANSWERED, "ok (queue drained)"
                seen.add(now)
                if len(seen) > 1:
                    return BUSY, (
                        f"busy, not wedged: {waiting} request(s) queued and "
                        f"turning over (depth {min(seen)}-{max(seen)})")
        return NO_ANSWER, f"no answer in {timeout}s ({waiting} request(s) queued)"

    def _judge_by_cpu(self, name, mp, result, timeout):
        """Busy or wedged, told apart where no FUSE queue can be read.

        macFUSE publishes no queue, so liveness is read off the daemons
        instead: a saturated sshfs keeps spending CPU on replies, while a
        wedged one waits on a connection that sends nothing. Without this, a
        mount under a recursive scanner on a Mac would be called wedged and
        remounted on every watcher tick.
        """
        pids = self.sshfs_pids(name, mp) + self.sftp_channel_pids(name)
        start = plat.cpu_seconds(pids)
        if start is None:
            return NO_ANSWER, f"no answer in {timeout}s"
        deadline = time.monotonic() + self.settings.int("MOUNT_BUSY_GRACE")
        while time.monotonic() < deadline:
            time.sleep(0.5)
            answer = _probe_answer(result)
            if answer is not None:
                return answer
            now = plat.cpu_seconds(pids)
            if now is None:
                break
            if now > start:
                return BUSY, (f"busy, not wedged: sshfs is still working "
                              f"({now - start:.2f}s of CPU since the deadline)")
        return NO_ANSWER, f"no answer in {timeout}s (sshfs idle)"

    def _sweep_probe_results(self, name, keep=None):
        """Drop result files from finished probes — but only ones nobody awaits.

        Orphaned probers may recreate one of these after it is removed; that is
        fine, because nothing reads a path but its own creator.

        What is *not* fine is deleting a file another live probe is waiting on.
        The watcher probes on every tick, so a hand-run probe often runs beside
        it, and a probe whose result file is deleted never sees `rc=` and
        reports a live mount as wedged. Only results whose owning process is
        gone are removed, plus this process's own finished ones. The glob keeps
        the `-` after the name, so it never sweeps probe-main2-… for 'main'.
        """
        stale = list(self.state.dir.glob(f"probe-{name}-*.result"))
        mine = f"probe-{name}-{os.getpid()}-"
        for path in stale:
            if keep is not None and path == keep:
                continue
            if not path.name.startswith(mine) and _probe_owner_alive(path):
                continue
            try:
                path.unlink()
            except OSError:
                pass

    def _spawn_probe(self, mp, result):
        """Start the orphaned prober that writes *result* when the mount answers.

        The probe must be a *mutating* request. A lookup of a random name looks
        uncached but is not: sshfs's directory cache answers it ENOENT without
        ever contacting the server, so a frozen transport still reports healthy
        (measured — the wedged mount answered every read in 0.1s). mkdir cannot
        be satisfied from any cache, so it always round-trips; any reply at all,
        even EROFS or EEXIST, proves the server is answering.
        """
        token = f".cluster-probe-{os.getpid()}-{random.randint(1000, 999999)}"
        probe_path = shlex.quote(str(mp / token))
        inner = (
            f"mkdir {probe_path} 2>{shlex.quote(str(result))}; "
            f"rc=$?; rmdir {probe_path} 2>/dev/null; "
            f"printf 'rc=%s\\n' $rc >> {shlex.quote(str(result))}"
        )
        subprocess.run(["sh", "-c", f"({inner}) &"], timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def healthy(self, name):
        """Is this mount usable? BUSY counts — it is slow, not broken.

        Every caller uses this predicate, so the watcher, `doctor` and `status`
        cannot disagree about health. Demanding ANSWERED would remount a mount
        that is merely saturated, and a remount is destructive (it aborts the
        FUSE connection out from under whatever is reading) and does nothing
        about the load, which simply re-saturates the fresh mount.
        """
        return self.probe(name)[0] in STATUS_OK

    def wedged(self, name):
        return self.probe(name)[0] == NO_ANSWER

    # --- unwedging ----------------------------------------------------------
    def unwedge(self, name, quiet=True):
        """Force a stuck mount to let go. True once it is out of the mount table.

        Queued requests have to fail before anything piled up on the mount is
        released, and only then can the unmount succeed. On Linux, aborting the
        FUSE connection fails them, and the unmount still needs its lazy form,
        because anything holding a descriptor (a VS Code file watcher is
        enough) keeps a plain one at EBUSY. macFUSE has no connection to abort:
        there it is the daemon's death that fails the queue (ENXIO), so sshfs
        is stopped first, and umount then diskutil release the mount.
        """
        mp = self.mountpoint(name)
        if plat.IS_MAC:
            self._stop_daemons(name, mp)
            plat.unmount(mp)
        else:
            if plat.fuse_abort(mp) and not quiet:
                ui.info(f"aborted the wedged FUSE connection at {short_path(mp)}")
            plat.unmount(mp)
            self._stop_daemons(name, mp)
        return not plat.mount_table_has(mp)

    def _stop_daemons(self, name, mp):
        for pid in self.sshfs_pids(name, mp) + self.sftp_channel_pids(name):
            self.logins._stop_pid(pid)

    # --- mounting -----------------------------------------------------------
    def mount(self, name, remote=None, mountpoint=None, quiet=False, node=None,
              health_checked=False):
        """Mount the cluster. Raises ui.Die on failure.

        *node* forces a specific host; otherwise the backend's mount policy
        decides between riding the login's master and opening one on an
        I/O node.

        *health_checked* says the caller has already probed and found the mount
        unusable, so this skips re-probing it. On a saturated mount a probe costs
        MOUNT_CHECK_TIMEOUT + MOUNT_BUSY_GRACE, so `try_mount` pays it once.

        The mount table is read before anything touches the mountpoint: a
        wedged FUSE mount blocks every stat of it, and a dead macFUSE one fails
        each with ENXIO.
        """
        missing = plat.mount_tools_missing()
        if missing:
            ui.die(f"cannot mount: {' and '.join(missing)} is not installed",
                   plat.sshfs_install_hint(),
                   "or turn mounts off: cluster config set AUTO_MOUNT 0")
        own = self.mountpoint(name)
        mp = Path(mountpoint) if mountpoint else own
        remote = remote or self.remote_path(name)

        if plat.mount_table_has(mp):
            if mp != own:
                ui.die(f"{short_path(mp)} is already a mount point",
                       "unmount it first, or mount somewhere else")
            if not health_checked and self.healthy(name):
                return True
            if not self.unwedge(name, quiet=quiet):
                ui.die(f"the mount at {short_path(mp)} is stuck and would not let go",
                       "close whatever is using it and try again")
        self._make_mountpoint(mp)

        if node is None:
            node = self.state.mountnode_read(name) or self.home_mount_node(name)
        if node:
            opened, detail = self._open_mount_master(name, node)
            if not opened:
                ui.die(f"could not open a connection to {self.backend.short(node)} "
                       "for the mount", detail or "no error output",
                       f"master log: {self.state.mount_master_log_path(name)}")
            self.state.mountnode_write(name, node)
        sock = self.state.mount_socket(name) if node else self.state.socket(name)
        target_node = node or self.logins.node_of(name)
        if not node and not self.logins.is_active(name):
            ui.die(f"login '{name}' is not connected; cannot mount over it")

        log = self.state.sshfs_log_path(name)
        opts = list(SSHFS_OPTS)
        if plat.IS_MAC:
            # macFUSE wants a volume name, and the AppleDouble/permission
            # defaults make a remote Linux tree behave oddly without these.
            opts += [f"volname=cluster-{self.backend.name}-{name}",
                     "defer_permissions", "noappledouble"]
        argv = ["sshfs"]
        for opt in opts:
            argv += ["-o", opt]
        # The mount always rides a master that is already authenticated, so it
        # needs no identity and no jump options — and with no master to ride,
        # its ssh fails at once saying so (NO_CONNECTION_OF_ITS_OWN) instead of
        # authenticating by itself; BatchMode keeps any prompt away as well.
        # The rider's -F (passed on to sshfs's ssh) keeps ~/.ssh/config out of
        # it, as for every other ssh this tool runs: a `Host *` stanza's
        # forwards or RemoteCommand would otherwise be requested through the
        # shared master. sshfs runs ssh itself, so it takes the rider's options
        # without the program name, and splits each -o at its commas, which
        # NO_CONNECTION_OF_ITS_OWN has none of.
        argv += rider_argv(sock, options=["-o", "BatchMode=yes"])[1:]
        argv.append(f"{self.backend.user}@{self.backend.host_for(target_node)}:{remote}")
        argv.append(str(mp))

        proc = plat.run(argv, timeout=90)
        if proc.returncode != 0 or not plat.mount_table_has(mp):
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            # A rider that found no master says so on a line of its own, and
            # the line sshfs adds after it ("read: Connection reset") says less.
            detail = [line for line in detail if line.startswith("cluster: ")] or detail
            plat.rotate_log(log)
            with log.open("a", encoding="utf-8") as handle:
                handle.write(f"--- {time.strftime('%F %T')} mount failed ---\n")
                handle.write((proc.stderr or "") + (proc.stdout or "") + "\n")
            ui.die(f"could not mount {name} at {short_path(mp)}",
                   detail[-1] if detail else "no error output",
                   f"sshfs log: {log}")

        self.state.write_meta(name, mountpoint=str(mp), remote=remote)
        if not quiet:
            where = f" via {self.backend.short(target_node)}" if node else ""
            ui.info(f"mounted {name} at {short_path(mp)}{where}")
        return True

    def _make_mountpoint(self, mp):
        """Create *mp*, found absent from the mount table, without a stat.

        The mount root is created here, by the first mount that needs it, and
        private like the rest of this tool's directories.
        """
        if config.MOUNT_ROOT in mp.parents:
            config.private_dir(config.MOUNT_ROOT)
        try:
            mp.parent.mkdir(parents=True, exist_ok=True)
            os.mkdir(mp)
        except FileExistsError:
            pass
        except OSError as exc:
            ui.die(f"cannot create the mount point {short_path(mp)}: "
                   f"{exc.strerror or exc}")

    def try_mount(self, name, quiet=True):
        """Mount, but never let a mount failure kill the caller's command.

        ``mount`` dies on failure; auto-mount on attach must warn and carry on,
        or a storage problem makes the tool unusable for work that needs no
        mount at all. An OSError is a failure like any other here.
        """
        try:
            health_checked = False
            if self.is_mounted(name):
                if self.healthy(name):
                    return True
                # Tell mount() not to ask again: this is the answer it would get.
                health_checked = True
            return self.mount(name, quiet=quiet, health_checked=health_checked)
        except ui.Die:
            ui.warn(f"could not mount {name}; continuing without it")
        except OSError as exc:
            ui.warn(f"could not mount {name} ({exc}); continuing without it")
        return False

    def unmount(self, name, quiet=False):
        mp = self.mountpoint(name)
        if not plat.mount_table_has(mp):
            self.state.mountnode_clear(name)
            return True
        self.unwedge(name, quiet=True)
        gone = not plat.mount_table_has(mp)
        if self.state.mountnode_read(name):
            self.close_mount_master(name)
        self.state.mountnode_clear(name)
        if not quiet:
            ui.info(f"unmounted {short_path(mp)}" if gone
                    else f"could not fully unmount {short_path(mp)}")
        return gone

    # --- failover -----------------------------------------------------------
    def _ask_node(self, name, node, command):
        """Stdout of *command* run on *node* ("" if it failed), or None if not asked.

        Over the login's own master when *node* is the login's node, which is
        free on any backend. Otherwise over a new BatchMode connection, which
        only a backend without interactive authentication can make. On one
        that types a password, BatchMode always fails, and any other way
        would spend an authentication (a TOTP window on FASRC) to learn what
        the mount attempt itself will show, so the node is not asked.
        """
        own = self.logins.node_of(name)
        if own and self.backend.short(own) == self.backend.short(node) \
                and self.logins.is_active(name):
            proc = self.logins.run_remote(name, command)
            return (proc.stdout or "").strip() if proc.returncode == 0 else ""
        if self.backend.interactive_auth:
            return None
        argv = self.backend.ssh_argv(
            node=node, extra=["-o", "BatchMode=yes", "-o", "ControlPath=none"],
            remote=command)
        return plat.out(argv, timeout=self.logins.command_timeout(own_connection=True))

    def node_storage_ok(self, name, node):
        """Does *node* serve the home directory? None when it was not asked.

        ssh answering proves nothing about storage, so this lists the home
        directory, which hangs or fails on a node whose file system is sick.
        """
        answer = self._ask_node(name, node, "cd && ls -f . >/dev/null && printf ok")
        return None if answer is None else answer == "ok"

    def node_overloaded(self, name, node):
        """A node in a storage meltdown still answers ssh; count D-state procs.

        False when the node was not asked: unknown is not overloaded.
        """
        answer = self._ask_node(name, node, "ps -eo stat= | grep -c '^D' || true")
        try:
            return int(answer or 0) > self.settings.int("MOUNT_NODE_MAX_DSTATE")
        except ValueError:
            return False

    def failover_candidates(self, name, avoid=()):
        explicit = self.settings.str("MOUNT_NODES").split()
        if explicit:
            ordered = [self.backend.fqdn(n) for n in explicit]
        else:
            # Ledger first: nodes this machine has actually reached before.
            ordered = list(self.state.ledger_nodes()) + list(self.backend.mount_nodes())
        skip = {self.backend.short(n) for n in avoid if n}
        seen, result = set(), []
        for node in ordered:
            short = self.backend.short(node)
            if short in skip or short in seen:
                continue
            seen.add(short)
            result.append(node)
        return result

    def failover(self, name, quiet=False):
        """Move the mount (only the mount) to another node.

        The login keeps its pin and its tmux sessions; the relocated mount gets
        its own master outside the login socket glob, so login machinery ignores
        it, and costs one connection slot until it fails back.
        """
        if not self.settings.flag("MOUNT_FAILOVER"):
            return False
        current = self.state.mountnode_read(name) or self.logins.node_of(name)
        tries = self.settings.int("MOUNT_FAILOVER_TRIES")
        reserve = []
        attempted = 0
        noted = False

        self.logins.last_failure = ""
        for node in self.failover_candidates(name, avoid=[current]):
            if attempted >= tries:
                break
            attempted += 1
            storage = self.node_storage_ok(name, node)
            if storage is False:
                continue
            if storage is None:
                if not quiet and not noted:
                    ui.note(f"moving the mount without probing nodes first: on "
                            f"{self.backend.label} a probe costs an "
                            "authentication, as the mount itself does")
                    noted = True
            elif self.node_overloaded(name, node):
                # Usable, but a poor host: keep it only as a last resort.
                reserve.append(node)
                continue
            if self._mount_via(name, node, quiet=quiet):
                return True
            if self._credential_refused(node, quiet):
                return False
        for node in reserve:
            if self._mount_via(name, node, quiet=quiet):
                ui.warn(f"mount moved to {self.backend.short(node)}, which is busy "
                        "with stuck I/O; nothing better was available")
                return True
            if self._credential_refused(node, quiet):
                return False
        return False

    def _credential_refused(self, node, quiet):
        """Did the mount master on *node* fail on the credential itself?

        Then every other node refuses it the same way, and trying them would
        spend an authentication each (a TOTP window on FASRC).
        """
        failure = self.logins.last_failure
        self.logins.last_failure = ""
        if not is_rejection(failure):
            return False
        if not quiet:
            ui.warn(f"{self.backend.short(node)} refused the credential ({failure}); "
                    "not trying other nodes with it")
        return True

    def _mount_via(self, name, node, quiet=False):
        self.unwedge(name, quiet=True)
        try:
            self.mount(name, quiet=quiet, node=node)
        except ui.Die:
            self.close_mount_master(name)
            return False
        if not quiet:
            ui.info(f"mount for '{name}' now rides {self.backend.short(node)} "
                    "(the login stays pinned)")
        return True

    def _open_mount_master(self, name, node):
        """(opened, error detail), reusing a live mount master already on *node*."""
        sock = self.state.mount_socket(name)
        if self.logins._socket_live(sock):
            if self.state.mountnode_read(name) == node:
                return True, ""
            self.close_mount_master(name)
        limit = self.settings.int("MAX_LOGINS")
        if self.logins.connection_count() >= limit:
            return False, f"already at {limit} cluster connections"
        self.backend.ensure_credential(quiet=True)
        return self.logins.open_master(
            sock, node, self.state.mount_master_log_path(name),
            tries=self.settings.int("POOL_OPEN_TRIES"), quiet=True)

    def close_mount_master(self, name):
        discard(self.state.mount_socket(name), timeout=15,
                grace=self.settings.int("STOP_TIMEOUT"))

    def home_mount_target(self, name):
        """The node this mount belongs on, or "" if it is already there.

        "Belongs" is the backend's mount policy: the login's own node for FASRC
        (which also frees the extra connection slot), or the login's designated
        I/O node for NERSC, where a DTN mount is the *normal* state rather than
        a displacement. Callers use this to avoid announcing a move that policy
        does not actually want.
        """
        current = self.state.mountnode_read(name)
        if not current:
            # No recorded mount node means it rides the login master: home.
            return ""
        if self.backend.mount_via == "mount_node":
            home_node = self.home_mount_node(name)
            return "" if not home_node or current == home_node else home_node
        home_node = self.logins.node_of(name)
        if not home_node or not self.logins.is_active(name):
            return ""
        return home_node

    def failback(self, name, quiet=False):
        """Return a displaced mount to where it belongs."""
        current = self.state.mountnode_read(name)
        home_node = self.home_mount_target(name)
        if not home_node:
            return False
        storage = self.node_storage_ok(name, home_node)
        if storage is False or (storage and self.node_overloaded(name, home_node)):
            return False

        self.unwedge(name, quiet=True)
        self.state.mountnode_clear(name)
        self.close_mount_master(name)
        try:
            self.mount(name, quiet=True, node=home_node)
        except ui.Die:
            # Put it back where it was working rather than leaving nothing.
            if not self._mount_via(name, current, quiet=True):
                ui.warn(f"mount for '{name}' is down after a failed failback")
            return False
        if not quiet:
            ui.info(f"mount for '{name}' moved back to {self.backend.short(home_node)}")
        return True

    # --- the repair ladder --------------------------------------------------
    def repair(self, name, attempt=1, quiet=False, log=None):
        """Fix a broken mount, escalating only as far as necessary."""
        emit = log or (lambda m: None if quiet else ui.info(m))
        failover_after = self.settings.int("MOUNT_FAILOVER_AFTER")

        # Rung 1: unwedge and remount over the master that already exists.
        # Costs no authentication, and repairs in about a second when it works.
        if self.logins.is_active(name) or self.state.mountnode_read(name):
            emit(f"repair {name}: remounting over the existing connection")
            self.unwedge(name, quiet=True)
            try:
                self.mount(name, quiet=True,
                           node=self.state.mountnode_read(name) or None)
                if self.healthy(name):
                    emit(f"repair {name}: mount healthy again")
                    return True
            except ui.Die:
                pass

        # Rung 2: the node is not serving storage; move the mount, not the login.
        if attempt >= failover_after:
            emit(f"repair {name}: moving the mount to another node")
            if self.failover(name, quiet=quiet) and self.healthy(name):
                return True

        # Rung 3: only now is the login itself suspect.
        if attempt > failover_after:
            emit(f"repair {name}: rebuilding the login")
            lock = self.state.login_lock(name, wait=5)
            if lock is None:
                emit(f"repair {name}: another process holds the login lock; skipping")
                return False
            try:
                self.unwedge(name, quiet=True)
                self.logins.close(name, keep_tmux=True, keep_pin=True, quiet=True)
                self.logins._ensure_locked(name, quiet=True)
                self.mount(name, quiet=True)
                return self.healthy(name)
            except ui.Die:
                return False
            finally:
                lock.release()
        return False

    # --- watcher ------------------------------------------------------------
    def watcher_pid(self, name):
        """The pid of this login's running watcher, or None.

        The pid file is only a claim: the process must be alive and be a
        `monitor` of this very login, both as whole arguments.
        """
        path = self.state.watch_pid_path(name)
        try:
            pid = int(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None
        if not plat.pid_alive(pid):
            return None
        args = plat.process_args(pid).split()
        if "monitor" not in args or name not in args:
            return None
        return pid

    def watcher_running(self, name):
        return self.watcher_pid(name) is not None

    def start_watcher(self, name, quiet=False):
        """Start this login's watcher unless one runs. True once one does.

        The watcher takes its lock and writes its pid file itself, so of two
        started at once the second exits and the first is the one recorded.
        This waits up to WATCH_START_TIMEOUT to see which: a child that
        exited without another watcher in its place is reported, with its
        exit status and log.
        """
        if self.watcher_running(name):
            return True
        log = self.state.watch_log_path(name)
        plat.rotate_log(log)
        argv = [str(Path(__file__).resolve().parents[1] / "bin" / "cluster"),
                "--backend", self.backend.name, "monitor", name]
        proc = plat.spawn_detached(argv, log_path=log)
        deadline = time.monotonic() + self.settings.int("WATCH_START_TIMEOUT")
        while proc.poll() is None and time.monotonic() < deadline:
            if self.watcher_pid(name) == proc.pid:
                break
            time.sleep(0.05)
        status = proc.poll()
        if status is not None:
            if self.watcher_running(name):
                return True
            if not quiet:
                ui.warn(f"watcher for '{name}' exited at once (status {status}); "
                        f"see {log}")
            return False
        if not quiet:
            ui.info(f"watching '{name}' (pid {proc.pid})")
        return True

    def stop_watcher(self, name, quiet=False):
        pid = self.watcher_pid(name)
        if pid is None:
            self.state.watch_pid_path(name).unlink(missing_ok=True)
            return False
        self.logins._stop_pid(pid)
        self.state.watch_pid_path(name).unlink(missing_ok=True)
        if not quiet:
            ui.info(f"stopped watching '{name}'")
        return True


def _token():
    return f"{random.randint(1000, 999999)}"


def _safe_read(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _probe_owner_alive(path):
    """Is the process that created this probe result file still running?

    The pid is in the name (`probe-{login}-{pid}-{token}.result`). An unparseable
    name is treated as nobody's.
    """
    parts = path.stem.split("-")
    if len(parts) < 3 or not parts[-2].isdigit():
        return False
    return plat.pid_alive(int(parts[-2]))


def _probe_answer(result):
    """(status, detail) once the prober has finished, else None.

    One copy of this test, used by every caller, so they cannot drift apart.
    The prober's `mkdir … 2>result` creates *result* the moment the shell sets
    up the redirect, so file existence says nothing at all; only the trailing
    `rc=` line means the request came back.
    """
    if not result.is_file():
        return None
    text = _safe_read(result)
    if "rc=" not in text:
        return None
    lowered = text.lower()
    if any(marker in lowered for marker in _DEAD_TRANSPORT):
        return ERRORED, text.strip().splitlines()[0]
    return ANSWERED, "ok"


def _program(cmd):
    """The basename of a command line's program."""
    return Path(cmd.split(" ", 1)[0]).name


def _has_argument(cmd, value):
    """Is *value* one whole argument of the space-joined command line *cmd*?"""
    return f" {value} " in f" {cmd} "


def short_path(path):
    home = str(Path.home())
    text = str(path)
    return text.replace(home, "~", 1) if text.startswith(home) else text

