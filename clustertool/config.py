"""Persistent, environment-overridable configuration and state paths.

There is one settings file, ``~/.config/cluster/settings.ini``. Values in its
``[global]`` section apply everywhere and a ``[fasrc]`` or ``[nersc]`` section
overrides them for one backend. Environment variables are the final override,
in a scoped form::

    built-in < [global] < [backend] < CLUSTER_NAME < CLUSTER_BACKEND_NAME

Every setting is declared once, as a :class:`Setting` that carries its built-in
value, its meaning and what it accepts. The ones every backend reads are
declared here; the ones only one backend reads are declared on that backend's
class (``Backend.SETTINGS``), which is also what makes them backend-only:
`cluster config set` writes them to their backend's section.

Keeping this machinery here gives the CLI, cron, tests and every subsystem one
answer. It also means an invalid value can be reported with the exact source
that supplied it instead of quietly turning into a default.
"""

from __future__ import annotations

import collections
import configparser
import os
import re
import sys
from pathlib import Path


def xdg_dir(variable, fallback):
    """An XDG base directory, or *fallback* when the variable is unset, empty
    or relative (the XDG specification says a relative value is ignored)."""
    value = os.environ.get(variable, "")
    return Path(value) if os.path.isabs(value) else Path(fallback)


HOME = Path.home()
CONFIG_ROOT = xdg_dir("XDG_CONFIG_HOME", HOME / ".config") / "cluster"
SETTINGS_FILE = CONFIG_ROOT / "settings.ini"

#: Mode for anything holding a secret, and for the directories above them.
SECRET_MODE = 0o600
SECRET_DIR_MODE = 0o700

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


class Setting(collections.namedtuple(
        "Setting", "default help flag minimum also choices required check",
        defaults=(False, 1, (), (), False, None))):
    """One setting: its built-in value, what it means and what it accepts.

    An int default makes it a whole number and anything else text; the other
    fields narrow that down:

    - *flag*: on/off, kept as 1/0
    - *minimum*: the smallest whole number accepted
    - *also*: whole numbers below the minimum that have a meaning of their own
    - *choices*: the only text values accepted
    - *required*: text that may not be empty (a path, a name)
    - *check*: validates text and returns its canonical form, or raises
      ValueError

    A namedtuple rather than a dataclass, like every record the package
    defines: dataclasses imports inspect, which every command would then pay
    for before it starts.
    """

    __slots__ = ()

    def parse(self, raw):
        """The in-memory value for *raw*; ValueError says why it is refused."""
        text = str(raw).strip()
        if "\n" in text:
            raise ValueError("a value may not span lines")
        if self.flag:
            if text.lower() in _TRUE:
                return 1
            if text.lower() in _FALSE:
                return 0
            raise ValueError("expected on/off, true/false, yes/no, or 1/0")
        if isinstance(self.default, int):
            try:
                value = int(text)
            except ValueError as exc:
                raise ValueError("expected a whole number") from exc
            if value < self.minimum and value not in self.also:
                if not self.minimum:
                    raise ValueError("expected zero or a positive value")
                raise ValueError(f"expected a value of at least {self.minimum}"
                                 + (" (or -1 to disable)" if -1 in self.also else ""))
            return value
        if self.choices:
            if text.lower() not in self.choices:
                raise ValueError("expected one of: " + ", ".join(self.choices))
            return text.lower()
        if self.required and not text:
            raise ValueError("a value is required")
        return self.check(text) if self.check else text


def _backend_value(text):
    """The canonical backend *text* names, asked of the registry."""
    # Imported here because the registry imports this module.
    from .backends import BACKENDS, as_backend

    canonical = as_backend(text)
    if not canonical:
        raise ValueError("expected one of: " + ", ".join(sorted(BACKENDS)))
    return canonical


#: Machine-wide settings, read before any backend exists. Only [global] and
#: CLUSTER_<KEY> set them: several choose the paths the rest of this module
#: uses, so their defaults exist at import.
GLOBAL = {
    "BACKEND": Setting("fasrc", "default backend for new, otherwise-unclaimed "
                                "login names", check=_backend_value),
    "STATE_ROOT": Setting(
        str(xdg_dir("XDG_STATE_HOME", HOME / ".local/state") / "cluster"),
        "local runtime/state directory", required=True),
    "CTL_DIR": Setting(str(HOME / ".ssh/controlmasters"),
                       "OpenSSH control-socket directory", required=True),
    "MOUNT_ROOT": Setting(str(HOME / "cluster_mounts"),
                          "root directory for managed SSHFS mounts", required=True),
    # Empty means the standard places for this platform: see
    # clustertool.setup.vscode_targets.
    "VSCODE_SETTINGS": Setting("", "VS Code settings file that setup merges into "
                                   "(empty: the standard ones for this platform)"),
    # Off: setup leaves VS Code's terminal tab title alone. It is one setting
    # for every terminal, so showing cluster's launch command there is opt-in.
    "VSCODE_TAB_TITLE": Setting(0, "let setup make VS Code terminal tabs show "
                                   "the launch command", flag=True),
    "CRED_ROOT": Setting(str(CONFIG_ROOT / "credentials"),
                         "private credential directory root", required=True),
    "GLOBUS": Setting("", "Globus CLI executable override"),
    "FOREIGN_OWNER_OPTIONS": Setting("", "tmux owner options set by other tools; "
                                         "their sessions are never swept"),
    "FORCE_PORTABLE": Setting(0, "exercise portable/macOS implementation paths",
                              flag=True),
}

#: The [relay] section, which bin/cluster-relay reads (as HOST, BIN and so on,
#: overridden by CLUSTER_RELAY_<KEY>). Declared here so `cluster config` can
#: show and change them like everything else.
RELAY = {
    "RELAY_HOST": Setting("", "user@host of the relay host that bin/cluster-relay "
                              "runs cluster on"),
    "RELAY_BIN": Setting("", "the cluster command on the relay host (empty: "
                             "~/.local/bin/cluster there if it exists, else "
                             "cluster on its PATH)"),
    # A relayed transfer tries again after a lost connection by
    # clustertool.backoff's rule, as a reconnect does; the client holds these
    # defaults too (RETRY_DEFAULTS in bin/cluster-relay). Only a staging
    # copy, which rsync resumes, earns the half-life's credit: a stream starts
    # again from its first byte.
    "RELAY_RETRIES": Setting(3, "lost connections in quick succession a relayed "
                                "transfer tries again after", minimum=0),
    "RELAY_RETRY_DELAY": Setting(2, "seconds before a relayed transfer first tries "
                                    "again; doubles with each loss after it",
                                 minimum=0),
    "RELAY_RETRY_DELAY_MAX": Setting(60, "longest wait before a relayed transfer "
                                         "tries again"),
    "RELAY_RETRY_HALF_LIFE": Setting(300, "seconds of a relayed staging copy's "
                                          "progress that halve its count of losses",
                                     minimum=0),
}
RELAY_SECTION = "relay"

#: The timeout of a command run on a cluster when none is given: the
#: REMOTE_COMMAND_TIMEOUT setting (Logins.command_timeout). None is no timeout
#: at all, in Logins.run_remote and in everything that passes one on to it.
COMMAND_TIMEOUT = object()

#: The longest one ssh master open may take (Logins.open_master): ssh's own
#: ConnectTimeout and the prompts fit well inside it, so one that has not
#: returned by then is stuck.
MASTER_OPEN_TIMEOUT = 180

#: What every backend reads. Each may be set in [global] or for one backend.
SHARED = {
    # naming
    "DEFAULT_LOGIN": Setting("main", "default managed login name"),
    # What the one-name creation shortcut does. Two names always mean tmux.
    "NEW_LOGIN_MODE": Setting("tmux", "one-name `cluster n LOGIN`: tmux or shell",
                              choices=("tmux", "shell")),
    # capacity
    "MAX_LOGINS": Setting(5, "maximum managed connections per backend"),
    # connecting: /dev/null keeps a stray ~/.ssh/config stanza from changing
    # how the tool reaches a site whose type knows how (the ssh type reads
    # ssh's own configuration instead: its HOST is usually a name there).
    "SSH_CONFIG": Setting("/dev/null", "OpenSSH config file; /dev/null isolates "
                                       "cluster settings", required=True),
    # mounting
    "AUTO_MOUNT": Setting(1, "automatically mount before interactive work", flag=True),
    # Every login node of a backend serves the same home filesystem, so a second
    # login's auto-mount would be a duplicate: identical bytes under a second
    # path, paid for with one of that connection's ten channels. The first mount
    # of a backend stands for all of its logins; an explicit `cluster mount` is
    # honoured, and 0 gives each login its own mount.
    "ONE_MOUNT_PER_BACKEND": Setting(1, "share one home mount across backend logins",
                                     flag=True),
    # Sessions are node-local: two logins pinned to one node see (and list) the
    # same tmux server, so their session lists shadow each other. One login per
    # node keeps "whose work is where" answerable at a glance. A deliberate
    # repin onto an occupied node is refused; a pool balancer that lands an
    # unpinned login there only warns, because the authentication is already
    # spent. 0 allows sharing a node.
    "ONE_LOGIN_PER_NODE": Setting(1, "refuse two managed logins on one tmux server",
                                  flag=True),
    "MOUNT_CHECK_TIMEOUT": Setting(8, "seconds before a mount probe is considered slow"),
    # After a missed probe deadline, how long to watch the FUSE queue to tell a
    # saturated mount (depth moving) from a wedged one (depth frozen). Doubles
    # as extra patience: the probe's answer is accepted during it, and a mount
    # behind a deep queue was measured answering at 7.8s, right on the
    # MOUNT_CHECK_TIMEOUT edge. Long enough to collect ~24 depth samples,
    # because the verdict rests on seeing the depth move.
    "MOUNT_BUSY_GRACE": Setting(6, "extra seconds to distinguish busy from wedged FUSE"),
    "MOUNT_FAILOVER": Setting(1, "move only the mount after repeated repair failures",
                              flag=True),
    "MOUNT_FAILOVER_AFTER": Setting(2, "failed repairs before mount failover", minimum=0),
    "MOUNT_FAILOVER_TRIES": Setting(3, "candidate mount nodes tried during failover"),
    "MOUNT_FAILBACK_TICKS": Setting(20, "watcher ticks before trying the preferred "
                                        "mount node"),
    "MOUNT_NODE_MAX_DSTATE": Setting(200, "D-state process limit for a candidate "
                                          "mount node"),
    "MOUNT_NODES": Setting("", "space-separated mount-node override"),
    # watcher
    "WATCH_INTERVAL": Setting(30, "seconds between watcher health checks"),
    # Failed ticks are counted as reconnects are (clustertool.backoff): healthy
    # ticks fade them, and the watcher backs off but never gives up.
    "WATCH_RETRIES": Setting(5, "failed ticks in quick succession after which "
                                "the watcher waits longer between ticks"),
    "WATCH_FAILURE_HALF_LIFE": Setting(600, "seconds of healthy ticks that halve "
                                            "the count of recent failures",
                                       minimum=0),
    "WATCH_BACKOFF_MAX": Setting(600, "longest extra wait between watcher ticks"),
    "WATCH_START_TIMEOUT": Setting(3, "seconds to wait for a watcher to start"),
    "LAYOUT_INTERVAL": Setting(300, "seconds between tmux layout snapshots"),
    # Linger is what lets a node's tmux server outlive the last connection to
    # it, and the node clears it from under us — so it is re-asserted on this
    # cadence rather than set once at setup. The interval is also the exposure:
    # a client reboot landing between a sweep and the next assertion ends the
    # sessions. See clustertool.linger.
    "LINGER": Setting(1, "keep tmux alive on a node after the last connection drops",
                      flag=True),
    "LINGER_INTERVAL": Setting(60, "seconds between linger re-assertions"),
    # The node-side half: one crontab line per login node, re-asserting linger
    # once a minute. It is the one part that keeps working when this machine
    # does not — a closed laptop, a dropped wifi, a power cut — so it is what
    # makes the guarantee portable. Off by default all the same: it writes to
    # your crontab on a shared system, which is not a thing to start doing
    # without being asked. See clustertool.linger.keeper_line.
    "LINGER_KEEPER": Setting(0, "install a node-side crontab line that re-asserts "
                                "linger", flag=True),
    # connections
    # Reconnecting a dropped connection (an attach, a shell, a transfer). Each
    # drop adds one to a count that a working connection fades with
    # RECONNECT_HALF_LIFE, and the wait before a reconnect doubles with that
    # count: drops scattered over days never add up, a burst does. See
    # clustertool.backoff.
    "INTERACTIVE_RETRIES": Setting(8, "drops in quick succession an attach or "
                                      "shell reconnects after", minimum=0),
    "RECONNECT_DELAY": Setting(2, "seconds before reconnecting after a drop; "
                                  "doubles with each drop in quick succession"),
    "RECONNECT_DELAY_MAX": Setting(60, "longest wait before a reconnect"),
    "RECONNECT_HALF_LIFE": Setting(300, "seconds of working connection that halve "
                                        "the count of recent drops", minimum=0),
    "NODE_PROBE_TRIES": Setting(3, "TCP reachability attempts per node"),
    "NODE_PROBE_TIMEOUT": Setting(6, "seconds per TCP reachability attempt"),
    # `ssh -f` returns once it has forked, so a brand-new master is polled for
    # rather than asked about once; losing that race is not a real failure.
    "MASTER_READY_WAIT": Setting(6, "seconds to wait for a new ControlMaster socket"),
    # How many times to open a master when the failure was a pre-authentication
    # drop. A login pool behind one address can hand a connection to a node that
    # accepts the TCP connection and then closes it; ssh will not fall through to
    # the pool's other address, because connecting *worked*. Measured on FASRC's
    # login VIP: roughly a quarter of attempts, so one retry turns a 1-in-4
    # failure into 1-in-16 and two into 1-in-64.
    "POOL_OPEN_TRIES": Setting(3, "new pool connections tried after pre-auth drops"),
    # sshd's MaxSessions: how many channels one connection may carry. Everything
    # riding a login's master shares it — each open `attach`, the sshfs mount,
    # every rclone sftp connection — and sshd refuses the overflow with
    # "channel N: open failed: connect failed: open failed" rather than anything
    # that names the real limit. FASRC's boslogin nodes leave it at the upstream
    # default of 10.
    "SSH_MAX_SESSIONS": Setting(10, "estimated SSH channel limit per master"),
    "STOP_TIMEOUT": Setting(5, "seconds before a stuck local transport is force-killed"),
    # A wait for a lock another cluster command holds goes on for as long as
    # the holder lives and works. What ends it is evidence: the holder stopped
    # (Ctrl-Z) for LOCK_PATIENCE seconds, or, for a lock held only for a quick
    # write, one holder keeping it that long with nothing moving. The TOTP
    # lock's holder sleeps into at most one fresh window (30 s), so it is
    # given TOTP_LOCK_PATIENCE of stillness.
    "LOCK_PATIENCE": Setting(30, "seconds a lock's holder may stay stopped, or a "
                                 "quick lock sit still, before a wait for it ends"),
    "TOTP_LOCK_PATIENCE": Setting(90, "seconds the TOTP lock may sit still, no "
                                      "window claimed, before a wait for it ends"),
    # A refused credential is tried once more, by one process, this long
    # after the refusal: two TOTP windows clear a code another authentication
    # had just used, and a clock still off after a laptop wakes. A second
    # refusal stops every unattended try until the credential files change or
    # someone connects by hand (state.Refusals).
    "REFUSAL_CONFIRM_DELAY": Setting(90, "seconds after a refused credential before "
                                         "one process tries it once more",
                                     minimum=0),
    "REFRESH_TRIES": Setting(5, "attempts to land a refreshed login on a new node"),
    "CONNECT_TIMEOUT": Setting(25, "OpenSSH connection timeout in seconds"),
    # How long one small command on a cluster may go unanswered before it
    # counts as hung: a tmux listing, a breadcrumb, a home directory, a
    # version, whose work does not grow with what is there, so a clock is the
    # evidence there is. One whose work does grow (retagging a login's
    # sessions for `rename`, killing a list of them) prints as it goes, and
    # is stopped only after this long of silence. One on a connection of its
    # own gets CONNECT_TIMEOUT more, for being let in first, and a session
    # create the node-side bounds of what it asks systemd (linger.bound).
    # REMOTE_CHECK_TIMEOUT bounds the one question that decides whether a
    # connection is rebuilt, whether it answers at all, which is asked when a
    # session has just dropped and someone is waiting. A master that is up
    # and misses it is tried again; one gone, or two misses in a row, is
    # rebuilt.
    "REMOTE_COMMAND_TIMEOUT": Setting(60, "seconds a small command on a cluster may "
                                          "take, or a longer one stay silent, "
                                          "before it counts as hung"),
    "REMOTE_CHECK_TIMEOUT": Setting(10, "seconds a connection may take to run a "
                                        "trivial command before it counts as not "
                                        "answering"),
    "SSH_SERVER_ALIVE_INTERVAL": Setting(30, "managed SSH keepalive interval in seconds"),
    "SSH_SERVER_ALIVE_COUNT_MAX": Setting(10, "missed managed SSH keepalives before "
                                              "disconnect"),
    # Short-lived clients printed for rclone/rsync use tighter liveness checks.
    "EXTERNAL_SSH_SERVER_ALIVE_INTERVAL": Setting(15, "rclone/rsync SSH keepalive "
                                                      "interval"),
    "EXTERNAL_SSH_SERVER_ALIVE_COUNT_MAX": Setting(4, "missed external keepalives "
                                                      "before disconnect"),
    # `ls` probes distinct masters concurrently; each worker uses one channel.
    "LIST_WORKERS": Setting(8, "maximum concurrent live probes for `cluster ls`"),
    # unattended boot restoration
    # Either one running out leaves the login to its watcher, which keeps trying.
    "BOOT_WAIT": Setting(180, "seconds `cluster boot` waits for the network before "
                              "leaving the login to its watcher", minimum=0),
    "BOOT_TRIES": Setting(5, "login attempts `cluster boot` makes before leaving the "
                             "login to its watcher"),
    "BOOT_RETRY_DELAY": Setting(5, "initial seconds between boot login attempts"),
    "BOOT_RETRY_DELAY_MAX": Setting(30, "maximum seconds between boot login attempts"),
    "BOOT_NETWORK_POLL_INTERVAL": Setting(1, "seconds between boot DNS checks"),
    # transfers — rclone asks for one sftp connection per transfer, per checker
    # and one for its shell commands, and sshd allows only about ten per
    # connection, so the pool defaults to transfers+checkers+1.
    "TRANSFER_TRANSFERS": Setting(4, "parallel file transfers on a dedicated connection"),
    "TRANSFER_CHECKERS": Setting(3, "parallel file checks on a dedicated connection"),
    "TRANSFER_CONNECTIONS": Setting(0, "SFTP connection cap; 0 derives it automatically",
                                    minimum=0),
    "TRANSFER_RETRIES": Setting(2, "whole-transfer retries after the first attempt"),
    "TRANSFER_RECONNECTS": Setting(8, "losses of its connection in quick succession "
                                      "a transfer reconnects and resumes after",
                                   minimum=0),
    "TRANSFER_OPEN_TRIES": Setting(3, "attempts to open a dedicated transfer connection"),
    "TRANSFER_IO_TIMEOUT": Setting(120, "seconds of silence that stop a listing, a "
                                        "walk, or a bridge push's rsync"),
    # A stat is one round trip and is timed; a listing or a walk of a tree is
    # not, and ends only after TRANSFER_IO_TIMEOUT seconds of printing nothing.
    # It is also rclone's own --timeout, which bounds only a network
    # connection rclone makes itself, and over --sftp-ssh it makes none. A
    # direct transfer's rclone, on the executing cluster, stops itself after
    # hearing nothing from this machine for as long as the connection itself
    # rides out (SSH_SERVER_ALIVE_INTERVAL x SSH_SERVER_ALIVE_COUNT_MAX), and
    # one look more.
    "TRANSFER_PROBE_TIMEOUT": Setting(45, "seconds one path question (a stat) to a "
                                          "cluster may take before it is asked again"),
    "TRANSFER_PROBE_TRIES": Setting(3, "times an unanswered path question is asked "
                                       "before it counts as unknown"),
    "TRANSFER_MULTI_THREAD_STREAMS": Setting(1, "streams used for each large file"),
    "SHARED_TRANSFER_TRANSFERS": Setting(2, "parallel transfers on a shared login master"),
    "SHARED_TRANSFER_CHECKERS": Setting(2, "parallel checks on a shared login master"),
    "RCLONE": Setting("", "local rclone executable override"),
    # cross-cluster transfers: rclone on the executing cluster (empty = look for
    # one on PATH there), and the Globus collection each cluster is registered as.
    "REMOTE_RCLONE": Setting("", "rclone executable on a cluster"),
    "GLOBUS_COLLECTION": Setting("", "backend Globus collection UUID override"),
    # Reading a hub's companion back into this repository. The size cap
    # prevents an accidental log/binary path from replacing the script.
    "COMPANION_SYNC_TIMEOUT": Setting(60, "seconds allowed to read the companion "
                                          "from a hub"),
    "COMPANION_MAX_BYTES": Setting(1048576, "maximum accepted size of a hub's "
                                            "companion script"),
    # Agents on the hub may edit its companion in place, and a push never
    # overwrites such an edit. Off (the default), the edit is reported as a
    # conflict and the companion refresh stops until it is resolved; on, the
    # push adopts it into remote/nersc in this checkout, which then runs code
    # written on the hub.
    "COMPANION_ADOPT_HUB_EDITS": Setting(0, "adopt a companion edited on the hub into "
                                            "this checkout (else: conflict)", flag=True),
    # Idempotent workstation/cluster bootstrap. The one timeout covers each
    # small remote inspection, isolated tmux validation, and atomic install.
    "SETUP_REMOTE_TIMEOUT": Setting(60, "seconds allowed for each remote setup "
                                        "operation"),
    "SETUP_DRIFT_CHECK_INTERVAL": Setting(86400, "seconds between automatic local "
                                                 "setup drift checks; -1 disables",
                                          also=(-1,)),
    # Off: setup installs nothing into a cluster home beyond the tmux block
    # unless asked. On, setup of a hub login also installs the companion there.
    "SETUP_SYNC_NERSC_TOOL": Setting(0, "install the NERSC companion during setup of "
                                        "a hub login", flag=True),
    "PEER_CONNECT_TIMEOUT": Setting(20, "seconds to connect during direct "
                                        "cross-cluster transfer"),
    # doctor
    "NTP_SERVER": Setting("pool.ntp.org", "time server used by doctor"),
}

#: Built-in values, by name, for code that only needs the default.
GLOBAL_DEFAULTS = {key: str(setting.default) for key, setting in GLOBAL.items()}
DEFAULTS = {key: setting.default for key, setting in SHARED.items()}

_MACHINE = dict(GLOBAL, **RELAY)


def normalize_key(key):
    return str(key).strip().replace("-", "_").upper()


def env_word(backend):
    """*backend* as a word of CLUSTER_<BACKEND>_<KEY>: ``my-lab`` -> ``MY_LAB``."""
    return backend.upper().replace("-", "_")


def _backend_classes():
    from .backends import BACKENDS

    return BACKENDS


def owners(key):
    """The backends that declare *key* for themselves, sorted; [] if shared."""
    key = normalize_key(key)
    if key in _MACHINE or key in SHARED:
        return []
    return sorted(name for name, cls in _backend_classes().items()
                  if key in cls.SETTINGS)


def lookup(key, backend=None):
    """The :class:`Setting` for *key*, as *backend* reads it. KeyError if none.

    With no backend, a backend-only key is described by the first backend
    that declares it. A backend's own declaration of a shared key is its
    type's default for it (the ssh type mounts nothing unasked).
    """
    key = normalize_key(key)
    if key in _MACHINE:
        return _MACHINE[key]
    classes = _backend_classes()
    if backend:
        # A backend the registry does not know reads what every backend reads.
        from .backends.base import Backend

        declared = getattr(classes.get(backend), "SETTINGS", Backend.SETTINGS)
        if key in declared:
            return declared[key]
    if key in SHARED:
        return SHARED[key]
    if backend:
        raise KeyError(key)
    for name in owners(key):
        return classes[name].SETTINGS[key]
    raise KeyError(key)


def known_keys(include_global=True, classes=None):
    """Every setting name, sorted: shared, backend-only, and machine-wide.
    Backend-only ones are those of *classes*, by default every backend's."""
    keys = set(SHARED)
    for cls in _backend_classes().values() if classes is None else classes:
        keys.update(cls.SETTINGS)
    if include_global:
        keys.update(_MACHINE)
    return sorted(keys)


def is_machine_wide(key):
    """Whether *key* is only ever global (or a [relay] key)."""
    return normalize_key(key) in _MACHINE


def describe(key):
    try:
        return lookup(key).help
    except KeyError:
        return ""


def parse_value(key, raw, backend=None):
    """Validate and normalize a user-supplied value.

    Returns the in-memory value type used by the rest of the application and
    raises ValueError with a user-facing reason on invalid input (KeyError for
    a name nothing declares).
    """
    return lookup(key, backend).parse(raw)


# --- the settings file -----------------------------------------------------

#: Parsed files by path, with the (mtime_ns, size, inode) they were parsed at.
_PARSED = {}
_WARNED = set()


def _warn_once(message):
    if message not in _WARNED:
        _WARNED.add(message)
        print(f"cluster: warning: {message}", file=sys.stderr)


def _empty(strict=True):
    parser = configparser.ConfigParser(interpolation=None, strict=strict)
    parser.optionxform = str
    return parser


def _parse(candidate):
    """The file's contents, read as bin/cluster-relay reads them.

    A section or a name given twice is accepted, the later value winning, as
    appending a section to a file that has one makes it; the first such
    repetition is warned about once.
    """
    text = candidate.read_text(encoding="utf-8")
    parser = _empty()
    try:
        parser.read_string(text, source=str(candidate))
    except (configparser.DuplicateSectionError,
            configparser.DuplicateOptionError) as exc:
        _warn_once(f"{exc}; where a name is given twice, the later value is used")
        parser = _empty(strict=False)
        parser.read_string(text, source=str(candidate))
    return parser


def _read_file(path=None, strict=False):
    """The settings file, parsed without interpolation or case folding.

    Display/runtime reads warn once and fall back, so a typo cannot make
    unrelated commands unusable, and are cached until the file changes.
    Mutating callers use ``strict=True``: overwriting a malformed file would
    destroy the user's remaining settings.
    """
    candidate = Path(path or SETTINGS_FILE)
    if strict:
        try:
            return _parse(candidate)
        except FileNotFoundError:
            return _empty()
        except (OSError, UnicodeError, configparser.Error) as exc:
            raise ValueError(f"cannot read configuration {candidate}: {exc}") from exc
    try:
        status = candidate.stat()
    except FileNotFoundError:
        _PARSED.pop(str(candidate), None)
        return _empty()
    except OSError as exc:
        _warn_once(f"cannot read configuration {candidate}: {exc}")
        return _empty()
    signature = (status.st_mtime_ns, status.st_size, status.st_ino)
    cached = _PARSED.get(str(candidate))
    if cached and cached[0] == signature:
        return cached[1]
    try:
        parser = _parse(candidate)
    except FileNotFoundError:
        parser = _empty()
    except (OSError, UnicodeError, configparser.Error) as exc:
        _warn_once(f"cannot read configuration {candidate}: {exc}")
        parser = _empty()
    _PARSED[str(candidate)] = (signature, parser)
    return parser


def _section_value(parser, section, key):
    """Case-insensitive lookup while writing canonical uppercase names."""
    if not parser.has_section(section):
        return None
    wanted = key.upper()
    for stored, value in parser.items(section):
        if stored.upper() == wanted:
            return value
    return None


def _stored_as(key):
    """``(section or None, name)`` a key is kept under in the file."""
    if key in RELAY:
        return RELAY_SECTION, key[len(RELAY_SECTION) + 1:]
    return None, key


def file_value(key, backend=None, path=None):
    """A raw persistent value and where it came from, backend section first."""
    key = normalize_key(key)
    parser = _read_file(path)
    where = Path(path or SETTINGS_FILE)
    section, name = _stored_as(key)
    if section:
        scopes = [section]
    else:
        scopes = ([backend.lower()] if backend else []) + ["global"]
    for scope in scopes:
        value = _section_value(parser, scope, name)
        if value is not None:
            return value, f"{where} [{scope}]"
    return None, ""


def file_entries(path=None):
    """``[(section, name)]`` of every value in the settings file, as written."""
    parser = _read_file(path)
    return [(section, name) for section in parser.sections()
            for name, _value in parser.items(section)]


def unread_entries(path=None):
    """``[(section, name, value)]`` of what the settings file holds that
    nothing reads.

    A section is read only under its exact name: [global], [relay], or a
    backend's; a name is read in any case. TYPE is read in the section of a
    profile that is not built in (clustertool.backends).
    """
    classes = _backend_classes()
    parser = _read_file(path)
    unread = []
    for section, name in file_entries(path):
        key = normalize_key(name)
        if section == RELAY_SECTION:
            read = f"{RELAY_SECTION.upper()}_{key}" in RELAY
        elif section == "global":
            read = key in GLOBAL or key in SHARED or bool(owners(key))
        elif section in classes:
            read = (key in SHARED or key in classes[section].SETTINGS
                    or (key == "TYPE" and not classes[section].shorthand))
        else:
            read = False
        if not read:
            unread.append((section, name, parser.get(section, name)))
    return unread


def file_problem(path=None):
    """Why the settings file cannot be read, or None. No file is no problem."""
    try:
        _read_file(path, strict=True)
    except ValueError as exc:
        return str(exc)
    return None


# --- resolving a value -----------------------------------------------------

BUILTIN = "built-in"


def _environment(key, backend):
    """``(raw, variable)`` from the environment, most specific first."""
    section, name = _stored_as(key)
    names = [f"CLUSTER_{section.upper()}_{name}"] if section else []
    if backend and not section and key not in GLOBAL:
        names.append(f"CLUSTER_{env_word(backend)}_{key}")
    if not section:
        names.append(f"CLUSTER_{key}")
    for variable in names:
        value = os.environ.get(variable)
        if value is not None:
            return value, variable
    return None, ""


def raw_value(key, backend=None):
    """``(raw text or None, source)`` for *key*, before any validation."""
    key = normalize_key(key)
    raw, source = _environment(key, backend)
    if raw is not None:
        return raw, source
    return file_value(key, None if key in GLOBAL else backend)


def _warn_invalid(key, raw, source, reason, default):
    _warn_once(f"invalid {key}={raw!r} from {source}: {reason}; "
               f"using default {default!r}")


def resolve(key, backend=None):
    """``(value, source)`` for *key*: the one place a setting is resolved.

    *backend* is whose value it is; None asks the machine-wide question
    (the environment and [global] only). An invalid override is reported once
    and the built-in value used, with a source that says so.
    """
    key = normalize_key(key)
    setting = lookup(key, backend)
    raw, source = raw_value(key, backend)
    if raw is None:
        return setting.default, BUILTIN
    try:
        return setting.parse(raw), source
    except ValueError as exc:
        _warn_invalid(key, raw, source, exc, setting.default)
        return setting.default, f"{BUILTIN} (invalid override in {source})"


def global_value(key, default=None):
    """A machine-wide answer, as text, for use before any backend exists.

    *default*, when given, stands in for the built-in value.
    """
    value, source = resolve(key)
    if source == BUILTIN and default is not None:
        return str(default)
    return str(value)


# These paths are fixed for one process. `cluster config set` changes what the
# *next* invocation uses, which is both unsurprising and safe for a running one.
STATE_ROOT = Path(global_value("STATE_ROOT")).expanduser()
CTL_DIR = Path(global_value("CTL_DIR")).expanduser()
MOUNT_ROOT = Path(global_value("MOUNT_ROOT")).expanduser()

#: Secrets live here and nowhere else: one private directory per backend,
#: well away from anything shareable and from the tool's own code.
CRED_ROOT = Path(global_value("CRED_ROOT")).expanduser()


# --- changing the settings file --------------------------------------------

_SECTION = re.compile(r"^\s*\[([^\]]+)\]")
_OPTION = re.compile(r"^([^\s#;=:\[][^=:]*?)\s*[=:]")


def _edited(text, section, key, value):
    """*text* with *key* in *section* set to *value*, or removed for None.

    Every other line, comments and blank lines included, stays as it was. A
    section left with nothing in it goes too.
    """
    target = section
    out, current, placed = [], None, False
    last = None        # where the target section's last header/option line is
    dropping = False   # inside the continuation lines of a replaced option
    for line in text.splitlines():
        header = _SECTION.match(line)
        if header:
            current, dropping = header.group(1).strip(), False
            out.append(line)
            if current == target:
                last = len(out)
            continue
        if dropping and line[:1].isspace() and line.strip():
            continue
        dropping = False
        option = _OPTION.match(line)
        if current == target and option:
            if option.group(1).strip().upper() == key.upper():
                dropping = True
                if value is None or placed:
                    continue
                line, placed = f"{key} = {value}", True
            out.append(line)
            last = len(out)
            continue
        out.append(line)
    if value is not None and not placed:
        if last is None:
            while out and not out[-1].strip():
                out.pop()
            out += ([""] if out else []) + [f"[{section}]", f"{key} = {value}"]
        else:
            out.insert(last, f"{key} = {value}")
    if value is None:
        out = _without_empty_section(out, target)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out) + "\n" if out else ""


def _without_empty_section(lines, target):
    for index, line in enumerate(lines):
        header = _SECTION.match(line)
        if not header or header.group(1).strip() != target:
            continue
        end = next((j for j in range(index + 1, len(lines))
                    if _SECTION.match(lines[j])), len(lines))
        if any(lines[j].strip() for j in range(index + 1, end)):
            return lines
        return lines[:index] + lines[end:]
    return lines


def _change_file(path, section, key, value):
    """Apply one change under a lock. Returns whether the file changed."""
    from . import platform as plat

    candidate = Path(path or SETTINGS_FILE)
    private_dir(candidate.parent)
    # Each change is one small write, so only a holder that keeps the lock
    # LOCK_PATIENCE seconds, or is stopped that long, ends the wait.
    lock = plat.FileLock(candidate.with_name(f".{candidate.name}.lock"),
                         record_holder=True)
    patience = int(global_value("LOCK_PATIENCE"))
    if not lock.acquire_queued(patience=patience, stopped=patience):
        raise ValueError(f"{candidate} is being changed by another cluster "
                         f"command: {plat.gave_up_text(lock, 'its lock')}")
    try:
        _read_file(candidate, strict=True)  # a malformed file is never rewritten
        try:
            text = candidate.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        changed = _edited(text, section, key, value)
        if changed == text:
            return False
        plat.atomic_write_text(candidate, changed, mode=SECRET_MODE)
        return True
    finally:
        lock.release()


def section_for(key, backend=None):
    """The section of the settings file *key* is written to."""
    key = normalize_key(key)
    section, _name = _stored_as(key)
    if section:
        return section
    if key in GLOBAL or not backend:
        return "global"
    return backend.lower()


def write_value(key, value, backend=None, path=None):
    """Persist one validated value. Returns its canonical value."""
    key = normalize_key(key)
    value = parse_value(key, value, None if key in _MACHINE else backend)
    section = section_for(key, backend)
    _change_file(path, section, _stored_as(key)[1], value)
    return value


def unset_value(key, backend=None, path=None):
    """Remove one persisted override. Returns whether anything changed.

    Any name may be removed, including one no setting declares.
    """
    key = normalize_key(key)
    return _change_file(path, section_for(key, backend), _stored_as(key)[1], None)


def write_entry(section, name, value, path=None):
    """Write *name* in *section* as it is, for an entry no setting declares
    (a profile's TYPE, which says which settings its section has)."""
    return _change_file(path, section, name, value)


def remove_entry(section, name, path=None):
    """Remove *name* from *section*, spelled exactly as the file spells it."""
    return _change_file(path, section, name, None)


class Settings:
    """Effective settings for one backend."""

    def __init__(self, backend_name):
        self.backend = backend_name

    def raw_with_source(self, key):
        return raw_value(key, self.backend)

    def _raw(self, key):
        return self.raw_with_source(key)[0]

    def get(self, key):
        return self.effective(key)[0]

    def effective(self, key):
        """Return ``(validated value, source)`` for configuration display."""
        return resolve(key, self.backend)

    def int(self, key):
        return int(self.get(key))

    def flag(self, key):
        return str(self.get(key)).strip().lower() in _TRUE

    def str(self, key):
        return str(self.get(key))


def state_dir(backend_name):
    path = STATE_ROOT / backend_name
    path.mkdir(parents=True, exist_ok=True)
    return path


def private_dir(path):
    """Make *path* exist as a directory only its owner can enter.

    Anything this creates on the way is private too, which matters for
    CTL_DIR's parent: a missing ``~/.ssh`` must not come into being as 0775.
    Directories that already existed above *path* are left exactly as they
    were; *path* itself belongs to this tool and is tightened if it drifted —
    the control sockets in CTL_DIR are live, authenticated connections to the
    cluster, and anyone who can reach one can use it.
    """
    path = Path(path)
    missing = []
    probe = path
    while not probe.exists() and probe != probe.parent:
        missing.append(probe)
        probe = probe.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=SECRET_DIR_MODE)
        except FileExistsError:
            pass
    try:
        if path.stat().st_mode & 0o777 != SECRET_DIR_MODE:
            os.chmod(path, SECRET_DIR_MODE)
    except OSError:
        # Not ours to change (someone pointed a setting at a shared directory);
        # the tool works regardless, and `doctor` is where permissions are judged.
        pass
    return path


def ensure_dirs():
    """The directories every bound command uses. The mount root is made by
    the first mount, so a machine that never mounts never has one."""
    for path in (STATE_ROOT, CTL_DIR):
        private_dir(path)
