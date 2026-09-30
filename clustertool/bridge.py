"""Keep the NERSC credential and the companion fresh on a hub.

A hub is a login of another cluster that runs the companion, `nersc`
(remote/nersc in this repository), so that anything on that cluster's login
nodes can treat NERSC as one more Slurm backend. The companion authenticates
with the 24-hour sshproxy certificate, which only this machine can fetch: the
sshproxy exchange needs the password and TOTP seed, and those never leave this
machine. So the bridge is a push loop that runs here:

    fetch a fresh certificate when the current one has under
    BRIDGE_MIN_CERT_LEFT seconds left  ->  install key + cert + companion on
    the hub over an existing login master  ->  verify end to end.

Run every 8 hours from cron (`cluster bridge push --cron`), the hub always
holds a certificate with >=16h of validity, so nothing there waits on this
machine: an outage here shorter than the remaining validity is invisible.

What lands on the hub (all under its home):
    .ssh/nersc-bridge             24h private key       (0600)
    .ssh/nersc-bridge-cert.pub    certificate           (0600)
    .ssh/known_hosts              + NERSC's @cert-authority line
    .local/bin/nersc              the companion         (0755, see below)
    .config/nersc/config          its settings          (written only if missing)
    .config/nersc/mirror.exclude  code-mirror excludes  (written only if missing)

The hub's config is the one place the companion's settings live: the first
push writes a commented template of every setting, and nothing here changes
the file afterwards.

The companion is a two-way file. Agents on the hub may edit ~/.local/bin/nersc
in place, so a push reconciles before it ships (companion.reconcile_hub): an
edit made on the hub is never overwritten, only reported as a conflict or,
with COMPANION_ADOPT_HUB_EDITS on, adopted into remote/nersc here; only a
source this machine changed is shipped. The credential lands either way: the
companion being in conflict must not strand the hub without a certificate.

Deliberate: the key written to the hub's disk is only ever the 24-hour
sshproxy key, never the password or TOTP seed. Root on the hub can read it for
the remainder of its validity; that exposure is the documented trade-off (see
the security model in docs/nersc-bridge.md).
"""

from __future__ import annotations

import json
import shlex
import shutil
import time
from datetime import datetime
from pathlib import Path

from . import backends, companion, config, platform as plat, registry, sshmux, ui
from .auth import failure_text, is_rejection

REMOTE_KEY = ".ssh/nersc-bridge"
REMOTE_CERT = ".ssh/nersc-bridge-cert.pub"
REMOTE_TOOL = ".local/bin/nersc"
REMOTE_CONFIG = ".config/nersc/config"
REMOTE_EXCLUDE = ".config/nersc/mirror.exclude"


def _source_backend():
    backend = backends.load("nersc")
    if not backend.lends_credential:
        ui.die("the nersc backend does not lend its credential")
    return backend


def _require_rsync():
    """Stop before any certificate fetch or connection when rsync is missing."""
    if shutil.which("rsync"):
        return
    if plat.IS_MAC:
        hint = "macOS ships /usr/bin/rsync; if it is gone, `brew install rsync`"
    else:
        hint = ("install it with the system package manager, for example "
                "`sudo apt install rsync` or `sudo dnf install rsync`")
    ui.die("rsync is not installed on this machine; the bridge ships files with it",
           hint)


def _ensure_fresh_cert(source, force=False):
    """Seconds left on a certificate fit to push, fetching one if need be."""
    min_left = source.settings.int("BRIDGE_MIN_CERT_LEFT")
    seen = source.cert_mark()
    left = source.cert_seconds_left()
    if force or left is None or left < min_left:
        current = "none" if left is None else f"{int(left)}s left"
        ui.info(f"fetching a fresh NERSC certificate (current: {current})")
        source.fetch_certificate(quiet=True, min_left=min_left, seen=seen)
        left = source.cert_seconds_left()
        if left is None or left <= 0:
            ui.die("certificate fetch reported success but the certificate is unusable")
    return left


def _rsync_argv(remote_shell, local, host, tmp):
    # Progress, so that a copy that is moving says so (see _rsync_file).
    return ["rsync", plat.rsync_progress_flag(), "-e", remote_shell, str(local),
            f"{host}:{tmp}"]


def _rsync_file(ctx, name, local, remote_rel):
    """Ship one file to a temp name next to its destination; caller moves it.

    Modes are set by :func:`_move_into_place` on the hub rather than by rsync,
    so any rsync works: `--chmod` needs 3.1, and macOS ships 2.6.9.

    Not timed: rsync reports its progress as the bytes move, and only
    TRANSFER_IO_TIMEOUT seconds with nothing reported stop it, since that is
    a stalled connection rather than a slow one.
    """
    sock = ctx.state.socket(name)
    node = ctx.logins.node_of(name)
    remote_shell = " ".join(shlex.quote(p) for p in sshmux.rider_argv(
        sock, options=["-o", "BatchMode=yes"]))
    host = ctx.backend.target(node)
    parent, _, base = remote_rel.rpartition("/")
    tmp = f"{parent}/.{base}.bridge-tmp" if parent else f".{base}.bridge-tmp"
    idle = ctx.settings.int("TRANSFER_IO_TIMEOUT")
    proc = plat.run(_rsync_argv(remote_shell, local, host, tmp), idle=idle)
    if proc.returncode == 124:
        ui.die(f"could not ship {Path(local).name} to hub '{name}': rsync reported "
               f"no progress for {idle}s",
               f"the connection may have stalled; reconnect it with: cluster refresh {name}")
    if proc.returncode != 0:
        tail = ((proc.stderr or "") + (proc.stdout or "")).strip().splitlines()
        ui.die(f"could not ship {Path(local).name} to hub '{name}'",
               *(tail[-2:] if tail else [f"rsync rc={proc.returncode}"]))
    return tmp


def _move_into_place(tmp, remote_rel, mode):
    """The hub-side command that gives a shipped file its mode and its name.

    The temp file sits next to its destination, in a directory the prepare
    step made (~/.ssh is 700), so nobody else can reach it in between.
    """
    return f"chmod {mode} ~/{tmp} && mv -f ~/{tmp} ~/{remote_rel}"


def _remote(ctx, name, command, timeout=sshmux.COMMAND_TIMEOUT):
    """*command* on hub login *name*, within *timeout*: REMOTE_COMMAND_TIMEOUT
    unless one is given, and none at all for None (Logins.run_remote)."""
    return ctx.logins.run_remote(name, command, timeout=timeout)


def _reconcile_tool(ctx, name, overwrite=False):
    """Adopt (or refuse to overwrite) the hub's companion; report either way."""
    plan = companion.reconcile_hub(ctx, name, overwrite=overwrite)
    action = plan["action"]
    if action == "adopt":
        ui.info(f"adopted the companion edited on hub '{name}': "
                f"v{plan['local_version'] or 'none'} -> v{plan['hub_version']} "
                f"({plan['hash'][:12]})")
        ui.note(f"adopted into {companion.SOURCE}")
        if plan["backup"]:
            ui.note(f"the source it replaced is kept at {plan['backup']}")
    elif action in ("conflict", "blocked"):
        ui.warn(f"left the companion on hub '{name}' exactly as it is: "
                f"{plan['detail']}")
        if plan["saved"]:
            ui.note(f"a copy of the hub's file is kept at {plan['saved']}")
            ui.note(f"compare:                  diff -u {companion.SOURCE} "
                    f"{plan['saved']}")
        ui.note(f"take the hub's version:   cluster nersc-tool sync {name}")
        ui.note(f"take this machine's:      cluster bridge push {name} "
                f"--overwrite-tool")
    elif action == "push" and plan["saved"]:
        ui.note(f"the hub's copy was kept at {plan['saved']} before overwriting")
    return plan


TOOL_NOTES = {
    "install": "companion installed",
    "push": "companion refreshed",
    "keep": "companion already current",
    "adopt": "companion adopted from the hub",
    "conflict": "companion left as edited on the hub (unresolved conflict)",
    "blocked": "companion left untouched",
}


def _install(ctx, login_name, credential, force=False, verify=True, quiet=False,
             overwrite=False):
    """Install the companion on a hub, with the credential when *credential*.

    Returns rc. Everything that can refuse locally (a NERSC login, no rsync,
    no NERSC credentials) does so before a certificate is fetched or the hub
    is contacted.
    """
    name = ctx.login(login_name)
    if ctx.backend.companion_drives:
        ui.die(f"login '{name}' is on NERSC itself",
               "the companion drives NERSC from a hub on another cluster",
               "usage: cluster bridge push [HUB_LOGIN]")
    _require_rsync()
    source = _source_backend()
    left = _ensure_fresh_cert(source, force=force) if credential else None

    ctx.logins.ensure(name)
    node = ctx.backend.short(ctx.logins.node_of(name) or "")
    prep = _remote(ctx, name,
                   "install -d -m 700 ~/.ssh ~/.config/nersc && "
                   "install -d ~/.local/bin ~/.local/state")
    if prep.returncode != 0:
        ui.die(f"cannot run commands on hub '{name}' ({node}): rc={prep.returncode}",
               "the node may be refusing new sessions; try another login "
               "(cluster bridge push OTHER_LOGIN)")

    plan = _reconcile_tool(ctx, name, overwrite=overwrite)
    ship_tool = plan["action"] in companion.SHIP_ACTIONS
    shipments = []
    if credential:
        shipments += [(source.key_path, REMOTE_KEY, "600"),
                      (source.cert_path, REMOTE_CERT, "600")]
    if ship_tool:
        tool_text = companion.SOURCE.read_text(encoding="utf-8")
        shipments.append((companion.SOURCE, REMOTE_TOOL, "755"))
    moves = []
    for local, remote_rel, mode in shipments:
        tmp = _rsync_file(ctx, name, local, remote_rel)
        moves.append(_move_into_place(tmp, remote_rel, mode))
    if moves:
        move = _remote(ctx, name, " && ".join(moves))
        if move.returncode != 0:
            ui.die(f"shipped files but could not move them into place on hub '{name}'")
    if credential:
        # From here the hub holds this certificate, whatever the check below
        # finds; the check fills in whether it works end to end.
        _record(name, node, left, verified=False)
    if ship_tool:
        companion.record_installed(ctx.backend.name, tool_text, name)

    _remote(ctx, name,
            "grep -qsF 'cert-authority *.nersc.gov' ~/.ssh/known_hosts || "
            f"printf '%s\\n' {shlex.quote(source.CERT_AUTHORITY)} >> ~/.ssh/known_hosts")
    _install_default(ctx, name, REMOTE_CONFIG, hub_config_text(source))
    if companion.EXCLUDE_SOURCE.is_file():
        _install_default(ctx, name, REMOTE_EXCLUDE,
                         companion.EXCLUDE_SOURCE.read_text(encoding="utf-8"))

    if credential and verify:
        # The companion bounds its own connection attempts; this waits for its
        # answer, and only a companion that has not given one in
        # BRIDGE_VERIFY_TIMEOUT seconds counts as hung.
        patience = source.settings.int("BRIDGE_VERIFY_TIMEOUT")
        check = _remote(ctx, name, f"~/{REMOTE_TOOL} run true", timeout=patience)
        if check.returncode == 124:
            ui.die(f"pushed, but `nersc run true` on hub '{name}' had not finished "
                   f"after {patience}s",
                   "the files are in place; a busy NERSC login node can be this slow",
                   f"check again with: cluster run {name} -- ~/{REMOTE_TOOL} run true")
        if check.returncode != 0:
            tail = ((check.stdout or "") + (check.stderr or "")).strip().splitlines()
            ui.die(f"pushed, but hub '{name}' could not reach NERSC end to end",
                   *(tail[-3:] if tail else ["no output"]))
        _record(name, node, left, verified=True)
    elif not credential:
        check = _remote(ctx, name, f"~/{REMOTE_TOOL} --version")
        if check.returncode != 0:
            ui.die(f"the companion on hub '{name}' did not execute")

    if quiet:
        return 0
    if credential:
        ui.info(f"bridge pushed via '{name}' ({node}): certificate valid "
                f"{int(left // 3600)}h, {TOOL_NOTES[plan['action']]} at ~/{REMOTE_TOOL}"
                + (", verified against NERSC" if verify else ""))
    else:
        ui.info(f"NERSC companion on hub '{name}' ({node}): "
                f"{TOOL_NOTES[plan['action']]}; "
                "any existing bridge credential was left unchanged")
        ui.note(f"if this hub has no credential yet: cluster nersc-tool install {name}")
    return 0


def push(ctx, login_name=None, force=False, verify=True, quiet=False,
         overwrite_tool=False):
    """Install/refresh the credential and the companion on a hub. Returns rc."""
    return _install(ctx, login_name, credential=True, force=force, verify=verify,
                    quiet=quiet, overwrite=overwrite_tool)


def install(ctx, login_name=None, force=False, verify=True, tool_only=False,
            quiet=False, overwrite=False):
    """`cluster nersc-tool install`: a full push, or with *tool_only* just the
    companion and its default config, which stages a hub before deciding
    whether it should receive the NERSC credential (until then the companion
    reports the missing credential)."""
    return _install(ctx, login_name, credential=not tool_only, force=force,
                    verify=verify, quiet=quiet, overwrite=overwrite)


def hub_config_text(source):
    """The hub's config as the first push writes it."""
    return companion.config_template({
        "user": source.user,
        "key": f"~/{REMOTE_KEY}",
        "scratch": companion.nersc_scratch(source.user),
    }, installer="cluster bridge push")


def _install_default(ctx, name, remote_rel, content):
    """Write a file on the hub only if it does not exist there.

    One remote command, and noclobber (set -C) makes the write itself refuse
    an existing file, so a file that appears meanwhile is not replaced.
    """
    quoted = " ".join(shlex.quote(line) for line in content.splitlines())
    proc = _remote(ctx, name, f"test -e ~/{remote_rel} || "
                              f"(set -C; printf '%s\\n' {quoted} > ~/{remote_rel})")
    if proc.returncode != 0:
        ui.warn(f"could not write ~/{remote_rel} on hub '{name}' (rc={proc.returncode})")


def _state_path():
    """Where the last push is recorded.

    Not `*.json` or `*.node`: those suffixes are how a login is discovered in
    a backend's state directory.
    """
    return Path(config.state_dir("nersc")) / "bridge.record"


def _record(name, node, cert_left, verified):
    """Record a push whose files are in place; *verified*: the end-to-end check passed."""
    until = datetime.now().timestamp() + cert_left
    plat.atomic_write_text(_state_path(), json.dumps({
        "pushed_at": int(time.time()),
        "login": name,
        "node": node,
        "cert_valid_until": int(until),
        "verified": bool(verified),
    }) + "\n")


def _ago(seconds):
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _tool_status():
    """Report where each hub's companion stands relative to this repository."""
    local_text = companion.SOURCE.read_text(encoding="utf-8")
    local_hash = companion.digest(local_text)
    version, problem = companion.inspect(local_text)
    ui.say(f"local companion source: v{version or '?'} ({local_hash[:12]})"
           + (f" — {problem}" if problem else ""))
    for backend_name, entry in sorted(companion.records().items()):
        installed = str(entry.get("hash") or "")
        conflict = entry.get("conflict") or {}
        age = int(time.time()) - int(entry.get("at") or 0)
        hub_hash = str(conflict.get("hub_hash") or "")
        if conflict:
            state = (f"CONFLICT: v{conflict.get('hub_version') or '?'} "
                     f"({hub_hash[:12]}) was edited there and is kept in "
                     f"place; the next push refuses to overwrite it")
        elif installed == local_hash:
            state = "matches this source"
        else:
            state = "this source is ahead; the next push ships it"
        ui.say(f"{backend_name}: last installed v{entry.get('version') or '?'} "
               f"({installed[:12]}) {_ago(age)} ago via '{entry.get('login')}' — {state}")
        if conflict:
            when = int(time.time()) - int(conflict.get("at") or 0)
            ui.warn(f"{backend_name}: the companion changed on both sides "
                    f"{_ago(when)} ago; nothing has been overwritten")
            if conflict.get("saved"):
                ui.note(f"the hub's copy is kept at {conflict['saved']}")
            ui.note(f"resolve with: cluster nersc-tool sync {entry.get('login')}"
                    f"  |  cluster bridge push {entry.get('login')} --overwrite-tool")


def _hub_backends():
    """Backends a hub can live on: every one the companion does not drive."""
    return [name for name in sorted(backends.BACKENDS)
            if not backends.BACKENDS[name].companion_drives]


def _hub_candidates(ctx, preferred):
    """[(login, backend)] a push may go through, in the order to try them."""
    candidates, chosen = [], set()
    known = registry.logins_by_backend()
    for backend_name in _hub_backends():
        if backend_name not in known:
            continue
        default = ctx.sibling(backend_name).settings.str("DEFAULT_LOGIN")
        for login in [preferred, default] + sorted(known[backend_name]):
            if (login and login not in chosen
                    and registry.find(login) == backend_name):
                chosen.add(login)
                candidates.append((login, backend_name))
    return candidates


def push_cron(ctx, force=False):
    """Unattended push: lock out overlapping runs, try every usable login.

    The home filesystem is shared across a cluster's login nodes, so any login
    of the hub's cluster lands the same files; a node that authenticates but
    refuses new sessions must not strand the bridge. The certificate is
    fetched once, before any login is tried (with *force*, fetched afresh
    once). A login whose cluster refused the credentials stops the other
    logins of that cluster from being tried: each would repeat the refusal,
    and repeated refusals are what locks an account.
    """
    lock = plat.FileLock(Path(config.state_dir("nersc")) / "bridge.lock",
                         record_holder=True)
    if not lock.acquire():
        ui.info(f"another bridge push ({plat.describe_pid(lock.holder())}) is "
                "already running; skipping")
        return 0
    try:
        source = _source_backend()
        candidates = _hub_candidates(ctx, source.settings.str("BRIDGE_LOGIN"))
        if not candidates:
            ui.warn("bridge: no hub login to push through")
            return 1
        try:
            _ensure_fresh_cert(source, force=force)
        except SystemExit as exc:
            ui.warn(f"bridge: no certificate to push: {failure_text(exc)}")
            return 1
        refused, tried = set(), []
        for login, backend_name in candidates:
            if backend_name in refused:
                continue
            hub = ctx.sibling(backend_name)
            hub.logins.last_failure = ""
            tried.append(login)
            try:
                # The certificate is fresh by now, so this fetches nothing.
                return push(hub, login)
            except SystemExit as exc:
                reason = failure_text(exc, hub.logins.last_failure)
            if is_rejection(reason):
                refused.add(backend_name)
                ui.warn(f"bridge push via '{login}' was refused: {reason}")
                ui.note(f"that is the {backend_name} credential failing, which "
                        f"every {backend_name} login would repeat, so no other "
                        f"is tried; check it with: "
                        f"{backends.BACKENDS[backend_name].setup_command()}")
            else:
                ui.warn(f"bridge push via '{login}' failed; trying the next login")
        ui.warn(f"bridge push failed on every login tried ({', '.join(tried)})")
        return 1
    finally:
        lock.release()


def _say_hub(name, proc, what):
    lines = [line.rstrip() for line in (proc.stdout or "").splitlines() if line.strip()]
    if proc.returncode == 0 and lines:
        for line in lines:
            ui.say(f"hub [{name}]: {line}")
        return
    tail = ((proc.stderr or "") + (proc.stdout or "")).strip().splitlines()
    why = "no answer in time" if proc.returncode == 124 else f"rc={proc.returncode}"
    ui.say(f"hub [{name}]: could not read {what} ({why})"
           + (f": {tail[-1].strip()}" if tail else ""))


def status(ctx, login_name=None):
    source = _source_backend()
    state, detail = source.credential_state()
    ui.say(f"local credential: {state} — {detail}")

    recorded, unreadable = {}, ""
    try:
        recorded = json.loads(_state_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        unreadable = str(exc)
    if not isinstance(recorded, dict):
        recorded, unreadable = {}, "not a record"
    if unreadable:
        ui.say(f"last push: unknown ({_state_path()} is unreadable: {unreadable})")
    elif recorded:
        age = int(time.time()) - recorded.get("pushed_at", 0)
        until = recorded.get("cert_valid_until", 0) - int(time.time())
        verified = recorded.get("verified") is True
        ui.say(f"last push: {_ago(age)} ago "
               f"via '{recorded.get('login')}' ({recorded.get('node')}), "
               + ("verified against NERSC" if verified else "NOT verified end to end")
               + "; that certificate "
               + (f"expires in {_ago(until)}" if until > 0 else "has EXPIRED"))
        if not verified:
            ui.say(f"  the check was skipped, cut short or failed; run it with: "
                   f"cluster run {recorded.get('login')} -- ~/{REMOTE_TOOL} run true")
    else:
        ui.say("last push: never")

    _tool_status()

    target_login = login_name or recorded.get("login")
    backend_name = registry.find(target_login) if target_login else None
    if backend_name not in _hub_backends():
        return 0
    hub = ctx.sibling(backend_name)
    name = hub.login(target_login)
    if not hub.logins.is_active(name):
        ui.say(f"hub check: login '{name}' has no live connection (skipped)")
        return 0
    _say_hub(name, _remote(hub, name,
                           f"ssh-keygen -L -f ~/{REMOTE_CERT} 2>/dev/null | grep Valid; "
                           f"~/{REMOTE_TOOL} --version 2>/dev/null"),
             "the installed certificate and companion")
    _say_hub(name, _remote(hub, name, f"~/{REMOTE_TOOL} config"),
             "the companion's effective config")
    return 0
