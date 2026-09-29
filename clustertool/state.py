"""On-disk state for one backend.

Two rules shape this module:

1. **A login's node is durable state, not a hint.** It lives in its own file and
   only the explicit pin/repin/unpin/refresh/close paths may move it. Internal
   teardown must never delete it, because a login that forgets its node will take
   a fresh one from the pool and strand the tmux sessions it owned.
2. **A tmux kill must be confirmed before any record is dropped.** If the remote
   end cannot be reached, the records stay so the work stays findable.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path

from . import config, platform as plat, ui
from .auth import is_rejection, seconds_left_in_window, totp_window

#: Seconds a claimed TOTP window must still have to run. The code is generated
#: when ssh's prompt for it arrives, a few seconds after the claim; a window
#: claimed any later risks typing the next window's code, which another
#: process may claim and type as well, and the cluster rejects the reuse as a
#: failed login.
TOTP_MIN_LEFT = 8

#: OpenSSH binds a new master's socket at ControlPath plus "." and 16 random
#: characters, then renames it into place, so the path needs this much room.
SOCKET_SUFFIX = 17


def socket_path_limit():
    """Bytes in a unix socket address here, the terminating NUL included."""
    return 104 if plat.IS_MAC else 108


def socket_path_problem(sock):
    """Why *sock* cannot be a control socket on this system, or None."""
    needed = len(os.fsencode(str(sock))) + SOCKET_SUFFIX
    limit = socket_path_limit()
    if needed < limit:
        return None
    return (f"the control socket {sock} is too long for this system: ssh needs "
            f"{needed} bytes for it and a socket path holds {limit - 1}")


def require_socket_path(sock):
    """Die, naming the limit and CTL_DIR, if *sock* is too long to bind."""
    problem = socket_path_problem(sock)
    if problem:
        ui.die(problem,
               "use a shorter login name, or a shorter control directory "
               f"(now {config.CTL_DIR}): cluster config set CTL_DIR ~/.ssh/cm")


def recorded_logins(backend_name, state_dir, ctl_dir):
    """Sorted login names one backend has local records of.

    Metadata (``<login>.json``) and pins (``<login>.node``) in its state
    directory, and control sockets, since a master can outlive its state
    files. Mount and transfer sockets share the directory under their own
    prefixes and are not logins. Nothing is created and no backend is needed,
    so this answers for a backend nobody has set up.
    """
    names = set()
    for pattern in ("*.json", "*.node"):
        names.update(path.stem for path in Path(state_dir).glob(pattern))
    prefix = f"cl-{backend_name}-"
    for path in Path(ctl_dir).glob(f"{prefix}*.sock"):
        stem = path.stem[len(prefix):]
        if stem and not stem.startswith(("mnt-", "xfer-")):
            names.add(stem)
    return sorted(names)


def _clock(when):
    """*when*, an epoch time, as the hour and minute (with the day if not today)."""
    moment = time.localtime(when)
    if time.strftime("%F", moment) == time.strftime("%F"):
        return time.strftime("%H:%M", moment)
    return time.strftime("%b %d %H:%M", moment)


class Refusals:
    """The backend's record of a refused credential, shared by every process.

    Trying a refused credential again only repeats the refusal: on FASRC each
    try spends a TOTP window, and repeated failures are how an account is
    locked. A refusal can still pass: a code another authentication had just
    used, or a clock that is off for a minute after a laptop wakes. So a
    refusal is confirmed once. REFUSAL_CONFIRM_DELAY seconds after it (two
    TOTP windows by default), one process tries again, and the others wait
    for what it finds. A second refusal stops every unattended attempt (a
    watcher, boot, a reconnecting attach or shell, a transfer's restore, the
    relay's --serve) until the credential files change or a person connects
    by hand (`cluster login`, `attach`), which says that it is trying them.

    The record holds for as long as the credential is the one refused, as
    backend.credential_marks tells (a stat of the files, never a read), and
    the next success clears it. The confirming try is claimed by a pid and
    for as long as the claimant's authentication can take, so a claimant
    that died is passed over, whether or not its pid has been reused since.
    """

    def __init__(self, backend, directory=None):
        self.backend = backend
        directory = Path(directory or config.state_dir(backend.name))
        # Not *.json: known_logins reads those as logins.
        self.path = directory / "credentials.refused"
        self.lock_path = directory / "credentials.refused.lock"

    def _marks(self):
        marks = getattr(self.backend, "credential_marks", None)
        return marks() if marks else None

    def _delay(self):
        return self.backend.settings.int("REFUSAL_CONFIRM_DELAY")

    def _claim_bound(self):
        """How long a master's open, the confirming try's usual form, can take
        from its claim: the ssh run, the discards on either side of it, and
        the wait for its socket."""
        settings = self.backend.settings
        return (config.MASTER_OPEN_TIMEOUT + 2 * settings.int("STOP_TIMEOUT")
                + settings.int("MASTER_READY_WAIT"))

    def _lock(self):
        return _queued(self.lock_path, self.backend.settings.int("LOCK_PATIENCE"),
                       "the record of refused credentials")

    def _read(self):
        try:
            record = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return record if isinstance(record, dict) else None

    def _write(self, record):
        try:
            plat.atomic_write_text(self.path, json.dumps(record) + "\n")
        except OSError:
            pass

    def current(self):
        """The record, while the credential is still the one refused; else None."""
        record = self._read()
        if record is None or record.get("marks") != self._marks():
            return None
        return record

    def blocks(self, by_hand=False, claim=True, bound=None):
        """Why this process must not authenticate now, or "" when it may.

        One by hand always may, and is told what was refused. An unattended
        one may when nothing was refused, or when the one confirming try is
        due and nobody else has it: with *claim* it then takes that try, for
        *bound* seconds (by default, as long as a master's open can take).
        """
        with self._lock():
            record = self.current()
            if record is None:
                return ""
            first = record.get("first", 0)
            seen = (f"the credentials were refused at {_clock(first)} "
                    f"({record.get('detail') or 'no detail'})")
            if by_hand:
                ui.note(f"{seen}; trying them again, as you asked")
                return ""
            if record.get("confirmed"):
                return (f"{seen} and again at {_clock(record.get('last', first))}; "
                        "not trying them again until they change or someone "
                        "connects by hand")
            due = first + self._delay()
            if time.time() < due:
                return f"{seen}; trying them once more at {_clock(due)}"
            holder = record.get("claim")
            if (holder and holder != os.getpid()
                    and time.time() < (record.get("claim_until") or 0)
                    and plat.pid_alive(holder)):
                return f"{seen}; {plat.describe_pid(holder)} is trying them once more"
            if claim:
                record["claim"] = os.getpid()
                record["claim_until"] = time.time() + (
                    self._claim_bound() if bound is None else bound)
                self._write(record)
            return ""

    def settle(self, opened, detail):
        """Record how an authentication ended: *opened*, or failed with *detail*."""
        if opened:
            self.succeeded()
        elif is_rejection(detail):
            self.refused(detail)
        else:
            self.released()

    def refused(self, detail):
        """A refusal: the first, or a second once the confirming delay is over."""
        with self._lock():
            now = time.time()
            record = self.current()
            if record is None:
                record = {"marks": self._marks(), "first": now, "confirmed": False}
            elif now - record.get("first", now) >= self._delay():
                record["confirmed"] = True
            record.update(last=now, detail=" ".join(str(detail).split()), claim=None,
                          claim_until=None)
            self._write(record)

    def succeeded(self):
        with self._lock():
            self.path.unlink(missing_ok=True)

    def released(self):
        """This process's confirming try ended in neither answer; another may."""
        with self._lock():
            record = self._read()
            if record and record.get("claim") == os.getpid():
                record["claim"] = record["claim_until"] = None
                self._write(record)

    def status(self):
        """One line for `cluster status`, or "" when nothing is refused."""
        record = self.current()
        if record is None:
            return ""
        first = _clock(record.get("first", 0))
        if record.get("confirmed"):
            return (f"refused at {first} and {_clock(record.get('last', 0))}; "
                    "not retrying until the credentials change or you connect "
                    "by hand")
        return (f"refused at {first}; trying once more at "
                f"{_clock(record.get('first', 0) + self._delay())}")


class State:
    def __init__(self, backend):
        self.backend = backend
        self.dir = config.state_dir(backend.name)
        self.ctl_dir = config.CTL_DIR
        #: Created by the first mount that needs it, never by looking.
        self.mount_root = config.MOUNT_ROOT / backend.name
        #: What totp_pace found when it last could not claim a window.
        self.totp_blocked = ""
        #: A refused credential, shared by every process on this machine.
        self.refusals = Refusals(backend, self.dir)

    # --- paths --------------------------------------------------------------
    def socket(self, name):
        return self.ctl_dir / f"cl-{self.backend.name}-{name}.sock"

    def mount_socket(self, name):
        """Deliberately outside the login socket glob so login code ignores it."""
        return self.ctl_dir / f"cl-{self.backend.name}-mnt-{name}.sock"

    def xfer_socket(self, tag):
        return self.ctl_dir / f"cl-{self.backend.name}-xfer-{tag}.sock"

    def meta_path(self, name):
        return self.dir / f"{name}.json"

    def pin_path(self, name):
        return self.dir / f"{name}.node"

    def mountnode_path(self, name):
        return self.dir / f"{name}.mountnode"

    def watch_pid_path(self, name):
        return self.dir / f"watch-{name}.pid"

    def watch_lock_path(self, name):
        return self.dir / f"watch-{name}.lock"

    def watch_log_path(self, name):
        return self.dir / f"watch-{name}.log"

    def master_log_path(self, name):
        return self.dir / f"master-{name}.log"

    def mount_master_log_path(self, name):
        return self.dir / f"master-mnt-{name}.log"

    def sshfs_log_path(self, name):
        return self.dir / f"sshfs-{name}.log"

    def login_lock_path(self, name):
        return self.dir / f"login-{name}.lock"

    @property
    def ledger_path(self):
        return self.dir / "nodes.seen"

    @property
    def abandoned_path(self):
        return self.dir / "abandoned.tsv"

    @property
    def abandoned_lock_path(self):
        return self.dir / "abandoned.lock"

    @property
    def totp_window_path(self):
        return self.dir / "totp.window"

    @property
    def totp_lock_path(self):
        return self.dir / "totp.lock"

    @property
    def list_evidence_path(self):
        # Deliberately not *.json: registry/known_logins use that suffix for
        # per-login metadata.
        return self.dir / "list-evidence.cache"

    @property
    def completion_sessions_path(self):
        return self.dir / "sessions.cache"

    @property
    def list_evidence_lock_path(self):
        return self.dir / "list-evidence.lock"

    def default_mountpoint(self, name):
        return self.mount_root / name

    # --- login metadata ----------------------------------------------------
    def read_meta(self, name):
        path = self.meta_path(name)
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def write_meta(self, name, **fields):
        meta = self.read_meta(name)
        meta.update(fields)
        meta["updated"] = int(time.time())
        plat.atomic_write_text(
            self.meta_path(name), json.dumps(meta, indent=2, sort_keys=True) + "\n")
        return meta

    def drop_meta(self, name):
        self.meta_path(name).unlink(missing_ok=True)

    def rename(self, old, new):
        """Move every per-login file and socket from *old* to *new*.

        The control sockets move too, and the running masters keep serving: a
        unix socket's listener is bound to the inode, not to the path, so a
        rename leaves it reachable at the new name. That is what makes renaming
        a login free of a reconnection — which on FASRC would cost a TOTP window.

        Returns the number of paths moved.
        """
        moved = 0
        makers = (
            self.meta_path, self.pin_path, self.mountnode_path,
            self.watch_pid_path, self.watch_lock_path, self.watch_log_path,
            self.master_log_path, self.mount_master_log_path,
            self.sshfs_log_path, self.login_lock_path,
            self.socket, self.mount_socket,
        )
        for maker in makers:
            src, dst = maker(old), maker(new)
            if src.exists() or src.is_socket():
                dst.parent.mkdir(parents=True, exist_ok=True)
                src.rename(dst)
                moved += 1

        # Probe results are per-probe and transient: drop rather than move.
        for path in self.dir.glob(f"probe-{old}-*.result"):
            path.unlink(missing_ok=True)

        # The saved `ls` row and the completion names are keyed by login name,
        # not stored in a file named after it, so they are rewritten in place
        # rather than moved.
        self.note_renamed_login(old, new)

        # The recorded mountpoint embeds the old name, and it is what
        # mountpoint() returns — leaving it stale would point the new login at
        # the old directory.
        meta = self.read_meta(new)
        old_mp = meta.get("mountpoint")
        if old_mp and Path(old_mp) == self.default_mountpoint(old):
            self.write_meta(new, mountpoint=str(self.default_mountpoint(new)))
        # Leave no empty directory named after a login that no longer exists;
        # rmdir refuses if anything is still mounted or stored there.
        try:
            self.default_mountpoint(old).rmdir()
        except OSError:
            pass
        return moved

    def known_logins(self):
        return recorded_logins(self.backend.name, self.dir, self.ctl_dir)

    # --- saved list/completion evidence -------------------------------------
    def read_list_evidence(self):
        """Return the last successful local `cluster ls` snapshot."""
        try:
            value = json.loads(self.list_evidence_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"updated": 0, "evidence": []}
        if not isinstance(value, dict) or not isinstance(value.get("evidence"), list):
            return {"updated": 0, "evidence": []}
        return value

    def read_completion_sessions(self):
        """Return {login: [sessions]} from the shell-friendly local cache."""
        found = {}
        try:
            lines = self.completion_sessions_path.read_text(
                encoding="utf-8").splitlines()
        except OSError:
            return found
        for line in lines:
            login, separator, session = line.partition("\t")
            if separator and login and session:
                found.setdefault(login, []).append(session)
        return found

    def _patience(self, key="LOCK_PATIENCE"):
        return self.backend.settings.int(key)

    def _evidence_lock(self):
        """Serialize the writers of the saved snapshot.

        `ls` replaces the file wholesale while a session command merges one
        login's row into it, so the two must not interleave. It is still only a
        cache, though: a writer whose way is held by one that is stuck goes
        ahead anyway, saying so (_queued), rather than hang an interactive
        command on bookkeeping, and the worst case is one row that stays stale
        until the next `ls`.
        """
        return _queued(self.list_evidence_lock_path, self._patience(),
                       "the saved session listing")

    def _write_completion_sessions(self, by_login):
        """Replace the shell-friendly names file from {login: [sessions]}."""
        plat.atomic_write_text(self.completion_sessions_path, "".join(
            f"{login}\t{session}\n"
            for login in sorted(by_login)
            for session in sorted(set(by_login[login]))))

    def write_list_evidence(self, evidence):
        """Atomically save rows plus per-login session names.

        A disconnected login supplies no new session evidence. Preserve its
        last known names for completion until a later live query succeeds (an
        empty successful result intentionally clears them).
        """
        with self._evidence_lock():
            return self._save_evidence(evidence)

    def _save_evidence(self, evidence, previous=None, prune=True):
        """Write both cache files. Call under :meth:`_evidence_lock`.

        *previous* supplies the last known names, read from disk when omitted.
        *prune* drops names for logins *evidence* says nothing about, which is
        what a whole-fleet `ls` wants and what a merge about a single login must
        not do: a login `ls` has never listed has no row to carry its names.
        """
        previous_sessions = (self.read_completion_sessions() if previous is None
                             else previous)
        saved = []
        by_login = {} if prune else dict(previous_sessions)
        for original in evidence:
            if not isinstance(original, dict) or not original.get("login"):
                continue
            item = dict(original)
            item["row"] = list(item.get("row", []))
            login = item["login"]
            loaded = bool(item.get("sessions_loaded"))
            sessions = list(item.get("sessions", [])) if loaded else list(
                previous_sessions.get(login, item.get("sessions", [])))
            item["sessions"] = sessions
            if not loaded:
                # Do not reuse the old rendered cell: it may contain '*' from a
                # formerly attached client and looks like current evidence.  The
                # names remain valuable, but their provenance must be explicit.
                if len(item["row"]) >= 7 and sessions:
                    item["row"][6] = "cached: " + ", ".join(sessions)
            saved.append(item)
            by_login[login] = sessions
        payload = {"updated": int(time.time()), "evidence": saved}
        plat.atomic_write_text(
            self.list_evidence_path,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
        )
        self._write_completion_sessions(by_login)
        return payload

    def _saved_items(self):
        """The saved rows, tolerant of a hand-edited or half-written file."""
        return [item for item in self.read_list_evidence().get("evidence", [])
                if isinstance(item, dict) and item.get("login")]

    def note_sessions(self, login, add=(), remove=()):
        """Fold a *confirmed* session change into this backend's saved evidence.

        `cluster ls` paints saved evidence before it can reach a node, and TAB
        completion reads nothing else, so a session this tool just created or
        killed has to land here at once. Leaving it to the next `ls` round trip
        would leave the local record contradicting what the user had just
        watched happen: the session would be missing from the first table and
        appear only when live evidence replaced it.

        Only confirmed changes may be passed in: recording a create that was not
        acknowledged would invent work, and recording an unconfirmed kill would
        hide it. Callers therefore pass the name only once the remote end has
        said the session exists, or that it is verifiably gone.

        These names are local evidence and not a live catalogue, so the row is
        marked exactly as an `ls` that could not reach the node marks it.
        Returns the names now recorded for *login*.
        """
        added = [name for name in dict.fromkeys(add) if name]
        dropped = {name for name in remove if name}
        with self._evidence_lock():
            by_login = self.read_completion_sessions()
            evidence = self._saved_items()
            item = next((i for i in evidence if i["login"] == login), None)
            known = list(item.get("sessions", ()) if item is not None
                         else by_login.get(login, ()))
            names = [name for name in known if name not in dropped]
            names += [name for name in added if name not in names]
            if names == known:
                return names
            by_login[login] = names
            if item is None:
                # `ls` has never listed this login, so there is no row to
                # correct — but completion reads the names file directly and
                # can be right immediately.
                self._write_completion_sessions(by_login)
                return names
            item["sessions"] = names
            item["sessions_loaded"] = False
            if not names and len(item.get("row", ())) >= 7:
                # The writer only rewrites that cell when there are names to put
                # in it, so an emptied list has to say so here; otherwise the row
                # keeps advertising the session we just watched die.
                item["row"][6] = "-"
            self._save_evidence(evidence, previous=by_login, prune=False)
            return names

    def note_renamed_login(self, old, new):
        """Carry saved `ls` and completion evidence across a login rename.

        Without this the rename hides a login's sessions twice over: the row is
        filed under a name no longer in the registry, so `ls` drops it from the
        saved table, and completion offers nothing for the new name.
        """
        with self._evidence_lock():
            by_login = self.read_completion_sessions()
            evidence = self._saved_items()
            moved = False
            for item in evidence:
                if item["login"] == old:
                    item["login"] = new
                    item["row"] = list(item.get("row", []))
                    if len(item["row"]) >= 2:
                        item["row"][1] = new
                    moved = True
            if old in by_login:
                by_login[new] = by_login.pop(old)
                moved = True
            if moved:
                self._save_evidence(evidence, previous=by_login, prune=False)
            return moved

    # --- pins ---------------------------------------------------------------
    def pin_read(self, name):
        path = self.pin_path(name)
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8").strip()

    def pin_write(self, name, node):
        if not node:
            return
        plat.atomic_write_text(self.pin_path(name), node.strip() + "\n")
        self.ledger_add(node)

    def pin_clear(self, name):
        self.pin_path(name).unlink(missing_ok=True)

    def login_pinned_to(self, node, exclude=None, short=None):
        """The first other login pinned to *node*, or ''.

        *short* normalizes spellings (a pin may be recorded as an FQDN or a
        short name, depending on who wrote it) so either form matches.
        """
        if not node:
            return ""
        key = short(node) if short else node
        for name in self.known_logins():
            if name == exclude:
                continue
            pinned = self.pin_read(name)
            if pinned and (short(pinned) if short else pinned) == key:
                return name
        return ""

    def forget_login_files(self, name):
        """Remove a retired login's scratch files.

        Deliberately never touches the pin or the meta: those are dropped only
        by the caller that has confirmed the login is really finished.
        """
        for path in (
            self.master_log_path(name),
            self.mount_master_log_path(name),
            self.sshfs_log_path(name),
            self.watch_log_path(name),
            self.watch_pid_path(name),
            self.login_lock_path(name),
            self.mountnode_path(name),
        ):
            path.unlink(missing_ok=True)
        # Probe results are per-probe; the glob keeps the `-` so it cannot
        # sweep probe-{name}2-… belonging to another login.
        for path in self.dir.glob(f"probe-{name}-*.result"):
            path.unlink(missing_ok=True)

    # --- mount node (a failed-over mount lives elsewhere) -------------------
    def mountnode_read(self, name):
        path = self.mountnode_path(name)
        return path.read_text(encoding="utf-8").strip() if path.is_file() else ""

    def mountnode_write(self, name, node):
        plat.atomic_write_text(self.mountnode_path(name), node.strip() + "\n")

    def mountnode_clear(self, name):
        self.mountnode_path(name).unlink(missing_ok=True)

    # --- node ledger --------------------------------------------------------
    def ledger_nodes(self):
        path = self.ledger_path
        if not path.is_file():
            return []
        seen, ordered = set(), []
        for line in path.read_text(encoding="utf-8").splitlines():
            node = line.strip()
            if node and node not in seen:
                seen.add(node)
                ordered.append(node)
        return ordered

    def ledger_add(self, node):
        if not node:
            return
        if node in self.ledger_nodes():
            return
        with self.ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(node.strip() + "\n")

    def ledger_remove(self, node):
        nodes = [n for n in self.ledger_nodes() if n != node]
        plat.atomic_write_text(self.ledger_path, "".join(f"{n}\n" for n in nodes))

    # --- abandoned sessions -------------------------------------------------
    # A session left on a node its login no longer occupies keeps that login's
    # ownership tag, so a sweep would protect it forever: the owner still exists.
    # Recording it here is what keeps it *tracked* — the record is local, so it
    # survives the node being unreachable, which is the usual case: a login is
    # repinned while a session still runs on its old node, precisely because
    # that node died.
    def abandoned(self):
        """[(node_short, session, former_login), ...]"""
        path = self.abandoned_path
        if not path.is_file():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and all(parts[:3]):
                out.append(tuple(parts[:3]))
        return out

    def abandon_record(self, node_short, session, former):
        self._rewrite_abandoned(node_short, session, (node_short, session, former))

    def abandon_forget(self, node_short, session):
        self._rewrite_abandoned(node_short, session, None)

    def _rewrite_abandoned(self, node_short, session, replacement):
        """Drop the (node, session) row and add *replacement*, under the lock.

        Read, change and write are one step: two commands abandoning sessions
        at once must not each write back a copy missing the other's row.
        """
        # The record is what keeps an abandoned session findable, so a writer
        # waits for the one ahead of it, unless that one is stuck.
        with _queued(self.abandoned_lock_path, self._patience(),
                     "the abandoned-session record"):
            rows = [r for r in self.abandoned()
                    if (r[0], r[1]) != (node_short, session)]
            if replacement is not None:
                rows.append(replacement)
            if not rows:
                self.abandoned_path.unlink(missing_ok=True)
                return
            plat.atomic_write_text(
                self.abandoned_path,
                "".join("\t".join(r) + "\n" for r in sorted(rows)))

    def abandoned_on(self, node_short):
        return [r[1] for r in self.abandoned() if r[0] == node_short]

    # --- TOTP pacing --------------------------------------------------------
    def totp_pace(self, wait=True, log=None):
        """Ensure this process consumes a TOTP window nobody else just used.

        The cluster rejects a reused code, so two concurrent authentications
        must not share a 30-second window. The consumed window index is recorded
        under a lock. A caller that finds the current window already claimed,
        or with fewer than TOTP_MIN_LEFT seconds to run, sleeps into the next
        one, so the window claimed is the window the code is typed in.
        """
        if not self.backend.paces_totp:
            return True
        self.totp_blocked = ""
        lock = plat.FileLock(self.totp_lock_path, record_holder=True)
        if wait:
            def queued(_holder):
                if log:
                    log("waiting for another authentication to claim its TOTP window")
            # Movement is the lock changing hands or a window being claimed:
            # either one says the authentications ahead are getting through. A
            # queue of processes each taking their turn keeps moving, however
            # long it is, and is waited for (TOTP_LOCK_PATIENCE).
            patience = self._patience("TOTP_LOCK_PATIENCE")
            held = lock.acquire_queued(
                patience=patience, announce=queued, stopped=self._patience(),
                progress=lambda: (lock.holder(), self._claimed_window()))
            if not held:
                self.totp_blocked = plat.gave_up_text(lock, "the TOTP lock")
        else:
            held = lock.acquire()
        if not held:
            return False
        try:
            while True:
                now = time.time()
                current = totp_window(now)
                left = seconds_left_in_window(now)
                if left >= TOTP_MIN_LEFT and self._claimed_window() != current:
                    plat.atomic_write_text(self.totp_window_path, f"{current}\n")
                    return True
                if not wait:
                    return False
                delay = left + 0.5
                if log:
                    log(f"waiting {delay:.0f}s for a fresh TOTP window")
                time.sleep(delay)
        finally:
            lock.release()

    def claim_totp_window(self, log=None):
        """totp_pace, or die saying what kept the window from being had."""
        if not self.totp_pace(log=log):
            ui.die("could not reserve a TOTP window for authentication",
                   *([self.totp_blocked] if self.totp_blocked else []),
                   "something has been authenticating for minutes; look "
                   "with: cluster status")

    def _claimed_window(self):
        try:
            return int(self.totp_window_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    # --- login lock ---------------------------------------------------------
    def login_lock(self, name, wait=None, announce=None):
        """Serialize teardown+rebuild of one login.

        Two processes each tearing a login down and building it back up would
        kill each other's in-flight authentication; everything that touches a
        login's master holds this first.

        *wait* seconds, or by default for as long as another process holds it
        and is not stopped (see plat.FileLock.acquire_queued): its holder is
        authenticating or repairing, which can take minutes on a slow node,
        and the lock is gone the moment that process is. *announce* is told
        the holder's pid once, if there is a wait. None if the lock was not
        had, with login_lock_blocked saying why.
        """
        lock = plat.FileLock(self.login_lock_path(name), record_holder=True)
        self.login_lock_blocked = ""
        if wait is None:
            held = lock.acquire_queued(announce=announce, stopped=self._patience())
            if not held:
                self.login_lock_blocked = plat.gave_up_text(
                    lock, f"login '{name}'s lock")
        else:
            held = lock.acquire(wait=wait)
        return lock if held else None

    def login_lock_busy(self, name):
        return plat.is_locked(self.login_lock_path(name))


@contextlib.contextmanager
def _queued(path, patience, what):
    """Hold the lock at *path* for the block, waiting for whoever is ahead.

    Yields whether it is held. The wait ends only when the holder has kept
    it *patience* seconds with nothing moving, or has been stopped that long
    (plat.FileLock.acquire_queued); the writer then goes ahead anyway, saying
    so, since the holder of a lock over one small write that has not let go
    in that time is stuck, and waiting on it would hang this command too.
    """
    lock = plat.FileLock(path, record_holder=True)
    try:
        held = lock.acquire_queued(patience=patience, stopped=patience)
    except OSError:
        held = False
    if not held and lock.gave_up:
        ui.warn(f"{plat.gave_up_text(lock, what)}; writing it anyway")
    try:
        yield held
    finally:
        if held:
            lock.release()
