"""The NERSC companion, ``remote/nersc``: identify it, install it on this
workstation, and keep the copy on a hub reconciled with this repository.

There is deliberately one implementation: ``remote/nersc``.  The local
``bin/nersc`` entry point executes it in place, and installing it on a hub
ships that same file atomically (``clustertool.bridge``).  A second copied
implementation would drift.

The hub's copy is not read-only in practice.  Coding agents on the hub improve
``~/.local/bin/nersc`` where they hit its rough edges, which is the point of
installing a real tool there instead of a thin wrapper.  So installing on a hub
is a reconciliation rather than a copy (``reconcile_hub``): an edit made on the
hub is never overwritten.  It is reported as a conflict, or, when
COMPANION_ADOPT_HUB_EDITS is on, adopted into this repository before anything
is shipped.

What makes that decidable is ``record_installed``: the hash of what was last
installed on each hub.  Without it, "the hub differs from my source" cannot be
told apart from "my source is newer than the hub", and every push has to
guess.  With it, a difference the record does not explain is an edit that
happened on the hub, and nothing else.
"""

from __future__ import annotations

import json
import os
import re
import textwrap
import time
from pathlib import Path

from . import backends, config, platform as plat, ui


REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE = REPO_ROOT / "remote" / "nersc"
EXCLUDE_SOURCE = REPO_ROOT / "remote" / "mirror.exclude"
LOCAL_ENTRY = REPO_ROOT / "bin" / "nersc"
LOCAL_BIN = Path.home() / ".local" / "bin" / "nersc"
#: The workstation companion's own files, in cluster's trees. Not
#: STATE_ROOT/nersc: that is the nersc backend's login state, where a
#: ``*.json`` file is a login.
LOCAL_CONFIG = config.CONFIG_ROOT / "companion" / "config"
LOCAL_STATE = config.STATE_ROOT / "companion"
BACKUP_DIR = config.STATE_ROOT / "nersc-tool-backups"
CONFLICT_DIR = config.STATE_ROOT / "nersc-tool-conflicts"
BASELINE_PATH = config.STATE_ROOT / "nersc-tool-installed.json"
SYNC_LOCK = config.STATE_ROOT / "nersc-tool-sync.lock"

#: ``reconcile_hub`` actions after which the caller ships the local source.
SHIP_ACTIONS = ("install", "push")

_HUB_TOOL = '"$HOME/.local/bin/nersc"'
# Distinct exit codes so an absent companion (install it) never looks like an
# unreadable one (leave it alone) or like a broken connection.
_READ_HUB = ("if [ -e {tool} ]; then cat {tool} || exit 9; else exit 8; fi"
             .format(tool=_HUB_TOOL))


# --- the companion's configuration file ---------------------------------

#: Every setting the companion reads, in the order its config file lists them:
#: (key, example, meaning). An empty key starts a new group. The example is
#: shown commented out unless the installer knows the real value.
CONFIG_KEYS = (
    ("user", "", "Your NERSC username."),
    ("key", "~/.ssh/nersc-bridge",
     "The sshproxy key; its certificate is the KEY-cert.pub next to it."),
    ("scratch", "/pscratch/sd/u/user",
     "Your NERSC scratch directory. $PSCRATCH and $SCRATCH in paths mean this."),
    ("pool", "perlmutter.nersc.gov", "The NERSC login pool."),
    ("dtns", "dtn01.nersc.gov dtn02.nersc.gov dtn03.nersc.gov dtn04.nersc.gov",
     "Data transfer nodes for bulk data, tried in this order."),
    ("", "", "Project settings, all optional."),
    ("cfs", "/global/cfs/cdirs/m0000", "A CFS project directory. $CFS in paths means this."),
    ("mirror_src", "~/code/myproject",
     "The code mirror for `nersc sync` and `nersc submit`: a directory on this machine."),
    ("mirror_dest", "myproject", "Where the code mirror goes, relative to your NERSC home."),
    ("return_root", "~/runs",
     "Where returned run directories land on this machine: $PSCRATCH/X returns to "
     "RETURN_ROOT/X. Unset, every return needs an explicit destination."),
    ("scratch_link", "runs",
     "A symlink in your NERSC home that points at scratch, so SCRATCH_LINK/X maps "
     "like $PSCRATCH/X."),
    ("hooks", "~/code/myproject/tools/nersc_hooks.py",
     "A project hooks file (Python 3.6, standard library only); see "
     "remote/hooks.example.py in the cluster repository."),
    ("return_queue", "$PSCRATCH/.nersc-return", "The job-return registry on NERSC."),
    ("env_parity", "auto",
     "Whether `nersc doctor` compares the mirror's .venv packages on both sides: "
     "auto (only when the mirror has one), on or off."),
    ("refresh_hint",
     "renew it on the workstation with `cluster bridge push` (normally run from cron)",
     "What error messages suggest when the certificate is missing or expired."),
    ("", "", "Connections and waits, all optional; the defaults suit NERSC."),
    ("connect_timeout", "25", "Seconds ssh waits for NERSC to answer a connection."),
    ("connect_retries", "2",
     "Failed connections in quick succession that are tried again, opening one "
     "or resuming a transfer that lost its own. A refused credential is not."),
    ("connect_retry_delay", "2",
     "Seconds before trying a connection again; doubles with each failure in "
     "quick succession."),
    ("connect_retry_delay_max", "60", "The longest wait before trying again."),
    ("connect_half_life", "300",
     "Seconds of a working connection that halve the count of recent failures, "
     "so that failures spread over a long run never add up."),
    ("alive_interval", "30",
     "Seconds between keepalives on the connection to the login node."),
    ("alive_count_max", "10",
     "Keepalives unanswered in a row that close that connection."),
    ("master_ready_wait", "8",
     "Seconds a connection that has authenticated is given to be ready."),
    ("dtn_probe_timeout", "5",
     "Seconds a data transfer node is given to answer before the next is tried."),
    ("no_progress_seconds", "180",
     "Seconds without output after which a remote command is taken to be stuck "
     "on NERSC storage and abandoned; 0 never abandons one. "
     "NERSC_NO_PROGRESS_SECONDS overrides it for one run."),
    ("lock_patience", "30",
     "Seconds a mirror sync waits on another that holds the sync lock but is "
     "stopped (Ctrl-Z) before giving up."),
    ("reap_lock_stale_seconds", "900",
     "Seconds a reap lock may go untouched before it is taken to belong to a "
     "pass that died."),
)


def config_template(values, installer):
    """The companion's config file as *installer* first writes it.

    Every setting appears with a one-line explanation. Those in *values* are
    written live; the rest are commented out, with an example value.
    """
    lines = textwrap.wrap(
        f"Configuration of the NERSC companion, `nersc`: `key = value` lines. "
        f"Written once by `{installer}`, which never changes it afterwards: "
        f"edit it here. `nersc config` prints the effective values and "
        f"`nersc doctor` checks them.",
        78, initial_indent="# ", subsequent_indent="# ")
    for key, example, meaning in CONFIG_KEYS:
        lines.append("")
        if not key:
            lines.append(f"# --- {meaning}")
            continue
        lines.extend(textwrap.wrap(meaning, 78, initial_indent="# ",
                                   subsequent_indent="# "))
        if key in values:
            lines.append(f"{key} = {values[key]}")
        else:
            lines.append(f"# {key} = {example}".rstrip())
    return "\n".join(lines) + "\n"


def nersc_scratch(user):
    """NERSC's scratch directory for *user*."""
    return f"/pscratch/sd/{user[:1]}/{user}"


# --- the companion on this workstation ----------------------------------

def local_environment(environ):
    """*environ* plus where the companion keeps its files on this workstation.

    bin/nersc runs remote/nersc with this environment, so its config (with
    mirror.exclude beside it) and its state live in cluster's own trees. A
    NERSC_CONFIG or NERSC_STATE_DIR that is already set wins.
    """
    env = dict(environ)
    env.setdefault("NERSC_CONFIG", str(LOCAL_CONFIG))
    env.setdefault("NERSC_STATE_DIR", str(LOCAL_STATE))
    return env


def _local_config_text(source):
    """Configuration for running the companion on this workstation."""
    return config_template({
        "user": source.user,
        "key": str(source.key_path),
        "scratch": nersc_scratch(source.user),
        "refresh_hint": "renew locally with: cluster --nersc auth --force",
    }, installer="cluster nersc-tool install-local")


def install_local(force=False, destination=None, config_path=None):
    """Put the companion on PATH here by linking, never copying, bin/nersc.

    Everything that can refuse runs before anything is created: without NERSC
    credentials there is nothing to configure, and a failed install leaves no
    link or directory behind.
    """
    destination = Path(destination or LOCAL_BIN)
    config_path = Path(config_path or LOCAL_CONFIG)
    source = backends.load("nersc")

    replace = False
    if destination.exists() or destination.is_symlink():
        same = destination.is_symlink() and destination.resolve() == LOCAL_ENTRY.resolve()
        if not same:
            if not force:
                ui.die(f"{destination} already exists and is not cluster's companion link",
                       "pass --force only if it is safe to replace")
            if destination.is_dir():
                ui.die(f"refusing to replace directory {destination}")
            replace = True
    else:
        replace = True

    if replace:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".cluster-new")
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(LOCAL_ENTRY)
        os.replace(temporary, destination)

    config.private_dir(config_path.parent)
    if not config_path.exists():
        plat.atomic_write_text(config_path, _local_config_text(source), mode=0o600)
    excludes = config_path.parent / "mirror.exclude"
    if not excludes.exists() and EXCLUDE_SOURCE.is_file():
        plat.atomic_write_text(excludes, EXCLUDE_SOURCE.read_text(encoding="utf-8"),
                               mode=0o600)

    ui.info(f"companion installed at {destination} -> {LOCAL_ENTRY}")
    ui.note(f"its config: {config_path} (written only when missing)")
    ui.note(f"canonical source: {SOURCE}")
    return 0


# --- identifying a companion --------------------------------------------

def digest(text):
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


#: Standard-library modules that Python 3.6 does not have.
_NEWER_MODULES = {"dataclasses": "3.7", "contextvars": "3.7",
                  "importlib.resources": "3.7", "importlib.metadata": "3.8",
                  "zoneinfo": "3.9", "graphlib": "3.9", "tomllib": "3.11"}


def _fstring_sources(text):
    """``(line, source)`` of every f-string literal in *text*.

    Python 3.12 tokenizes an f-string into pieces, older versions into one
    STRING token; either way the literal's own source text is what comes back.
    """
    import io
    import tokenize

    starts = [0]
    for line in text.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))

    def offset(position):
        return starts[position[0] - 1] + position[1]

    fstring_start = getattr(tokenize, "FSTRING_START", None)
    fstring_end = getattr(tokenize, "FSTRING_END", None)
    depth, opened = 0, None
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.STRING:
            prefix = re.match(r"[A-Za-z]*", token.string).group(0)
            if "f" in prefix.lower():
                yield token.start[0], token.string
        elif fstring_start is not None and token.type == fstring_start:
            if not depth:
                opened = token.start
            depth += 1
        elif fstring_end is not None and token.type == fstring_end:
            depth -= 1
            if not depth:
                yield opened[0], text[offset(opened):offset(token.end)]


def _self_documenting(literal):
    """Whether an f-string literal uses the ``{expr=}`` specifier (3.8+)."""
    body = literal.lstrip("rRbBuUfF")
    quote = body[:3] if body[:3] in ('"""', "'''") else body[:1]
    body = body[len(quote):len(body) - len(quote)]
    index = 0
    while index < len(body):
        if body.startswith("{{", index):
            index += 2
            continue
        if body[index] != "{":
            index += 1
            continue
        # One replacement field: stop at its end, its conversion or its
        # format spec, looking for an `=` that is not part of an operator.
        depth, inner, cursor = 0, None, index + 1
        while cursor < len(body):
            char = body[cursor]
            if inner:
                inner = None if char == inner else inner
            elif char in "'\"":
                inner = char
            elif char in "([{":
                depth += 1
            elif char in ")]}":
                if not depth:
                    break
                depth -= 1
            elif not depth and char == "!" and body[cursor + 1:cursor + 2] != "=":
                break
            elif not depth and char == ":":
                break
            elif not depth and char == "=":
                operator = (body[cursor - 1] in "=!<>"
                            or body[cursor + 1:cursor + 2] == "=")
                if not operator and body[cursor + 1:].lstrip()[:1] in ("}", "!", ":"):
                    return True
            cursor += 1
        index = cursor + 1
    return False


def py36_problems(text):
    """Why *text* would not run on Python 3.6, or ``[]`` if nothing is found.

    FASRC's login nodes run 3.6.8. ``ast.parse(feature_version=(3, 6))`` is
    best effort: depending on the interpreter doing the parsing it accepts
    assignment expressions, positional-only parameters and the f-string
    ``=`` specifier. So those are looked for explicitly, together with a few
    other cheap tells: ``from __future__ import annotations``, a standard
    module 3.6 lacks, and the ``capture_output=``/``text=`` keywords.
    """
    import ast
    import tokenize

    try:
        tree = ast.parse(text, filename="nersc", feature_version=(3, 6))
    except SyntaxError as exc:
        return [str(exc)]
    problems = []

    def found(node, what):
        problems.append(f"line {getattr(node, 'lineno', '?')}: {what}")

    for node in ast.walk(tree):
        if type(node).__name__ == "NamedExpr":
            found(node, "an assignment expression (:=) needs Python 3.8")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            if getattr(node.args, "posonlyargs", None):
                found(node, "positional-only parameters need Python 3.8")
        elif isinstance(node, ast.ImportFrom):
            names = {alias.name for alias in node.names}
            if node.module == "__future__" and "annotations" in names:
                found(node, "`from __future__ import annotations` needs Python 3.7")
            elif node.module in _NEWER_MODULES:
                found(node, f"{node.module} needs Python {_NEWER_MODULES[node.module]}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _NEWER_MODULES:
                    found(node, f"{alias.name} needs Python {_NEWER_MODULES[alias.name]}")
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg in ("capture_output", "text"):
                    found(node, f"the {keyword.arg}= keyword needs Python 3.7")
    try:
        for line, literal in _fstring_sources(text):
            if _self_documenting(literal):
                problems.append(f"line {line}: the f-string = specifier needs Python 3.8")
    except (tokenize.TokenError, SyntaxError) as exc:
        problems.append(str(exc))
    return problems


def inspect(text):
    """Return (VERSION, problem) for candidate companion source; never raises.

    A problem means the text is not this tool, so it must neither replace the
    canonical source nor be trusted as an agent's improvement of it.
    """
    import ast

    if not text.startswith("#!/usr/bin/env python3"):
        return "", "the hub's file is not the expected Python NERSC companion"
    problems = py36_problems(text)
    if problems:
        more = f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""
        return "", ("the hub's NERSC companion is not valid Python 3.6: "
                    f"{problems[0]}{more}")
    tree = ast.parse(text, filename="nersc")

    version = ""
    functions = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.add(node.name)
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == "VERSION":
                value = node.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    version = value.value
    if not version or not {"load_config", "main"}.issubset(functions):
        return "", "the hub's file lacks the NERSC companion's version or entry points"
    return version, ""


def _validated_version(text):
    """Return VERSION after proving this is the Python-3.6 companion source."""
    version, problem = inspect(text)
    if problem:
        ui.die(problem, "the local canonical source was not changed")
    return version


def _version_key(version):
    """Sortable key for a VERSION string; "3.4+local2" ranks as (3, 4).

    The suffix is deliberately ignored: it marks an edit made on the hub, and
    a hub edit already wins ties in ``reconcile_hub``.
    """
    base = str(version).partition("+")[0]
    return tuple(int(part) for part in re.findall(r"\d+", base))


# --- what we last installed on each hub ---------------------------------

def _read_records(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_records(data, path):
    plat.atomic_write_text(Path(path), json.dumps(data, sort_keys=True, indent=2) + "\n",
                           mode=0o600)


def records(path=None):
    """Every hub backend's install record, newest state as written."""
    return _read_records(path or BASELINE_PATH)


def installed_record(backend, path=None):
    return records(path).get(backend) or {}


def _record_target(destination, baseline_path):
    """Where to write the install record; None means "do not record".

    A caller that redirects the canonical source without redirecting the
    record (tests, inspection of a scratch copy) must not leave a hash in it:
    the record describes this repository's source, and a stray value would
    make the next push read an ordinary hub as freshly edited.
    """
    if baseline_path is not None:
        return Path(baseline_path)
    if Path(destination) == SOURCE:
        return BASELINE_PATH
    return None


def record_installed(backend, text, login, path=BASELINE_PATH):
    """Remember exactly what both sides hold after an install, adopt or match.

    This is the only durable fact that lets a later push tell an agent's edit
    on the hub from a newer source here.  Recording also clears any conflict:
    both sides now hold the same bytes, so there is nothing left to resolve.
    ``path=None`` means "do not record"; see ``_record_target``.
    """
    if path is None:
        return
    data = _read_records(path)
    data[backend] = {"hash": digest(text), "version": inspect(text)[0],
                     "login": login, "at": int(time.time())}
    _write_records(data, path)


def record_conflict(backend, hub_version, hub_hash, saved, path=BASELINE_PATH):
    """Note an unresolved two-sided divergence next to the install record."""
    if path is None:
        return
    data = _read_records(path)
    entry = dict(data.get(backend) or {})
    entry["conflict"] = {"hub_version": hub_version, "hub_hash": hub_hash,
                         "saved": str(saved) if saved else "",
                         "at": int(time.time())}
    data[backend] = entry
    _write_records(data, path)


# --- moving content around, never losing any ----------------------------

def _keep_copy(text, directory, mode=0o700):
    """Content-addressed local copy, so no version is ever only remote."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"nersc-{digest(text)[:12]}"
    if not path.exists():
        plat.atomic_write_text(path, text, mode=mode)
    return path


def _adopt(incoming, destination, backup_dir, current):
    """Replace the canonical source, keeping what it held."""
    backup = _keep_copy(current, backup_dir) if current else None
    plat.atomic_write_text(Path(destination), incoming, mode=0o755)
    return backup


def read_hub(ctx, name, timeout=None):
    """Return (text, problem) for the companion installed on a hub login."""
    timeout = timeout or ctx.settings.int("COMPANION_SYNC_TIMEOUT")
    proc = ctx.logins.run_remote(name, _READ_HUB, timeout=timeout)
    if proc.returncode == 8:
        return None, "absent"
    if proc.returncode == 9:
        return None, "the hub has a ~/.local/bin/nersc that cannot be read"
    if proc.returncode != 0:
        tail = ((proc.stderr or "") + (proc.stdout or "")).strip().splitlines()
        detail = f": {tail[-1].strip()}" if tail else ""
        return None, f"could not read ~/.local/bin/nersc through '{name}'{detail}"
    return proc.stdout or "", ""


def reconcile_hub(ctx, name=None, overwrite=False, destination=None,
                  backup_dir=None, conflict_dir=None, baseline_path=None,
                  lock_path=None):
    """Decide what the hub's companion should become, keeping agent edits.

    Called before the bridge ships the companion.  The hub's copy is compared
    with the hash ``record_installed`` left behind, not with the local source,
    so an edit made on the hub is never mistaken for a newer source here:

        hub == local source              -> "keep"      ship nothing
        no companion on the hub          -> "install"   ship the source
        hub edited, source untouched     -> "adopt"     pull it in, ship nothing
                                            (a "conflict" unless
                                            COMPANION_ADOPT_HUB_EDITS is on)
        source edited, hub as installed  -> "push"      ship the source
        both edited since the install    -> "conflict"  ship nothing
        hub unreadable, or not this tool -> "blocked"   ship nothing

    Every branch that would drop content keeps a content-addressed copy of it
    on this machine first, so no version of the companion exists only on the
    hub.  The returned dict always carries ``action`` and a human ``detail``.
    """
    name = ctx.login(name)
    backend = ctx.backend.name
    source = Path(destination or SOURCE)
    record_path = _record_target(source, baseline_path)
    conflict_dir = Path(conflict_dir or CONFLICT_DIR)
    backup_dir = Path(backup_dir or BACKUP_DIR)

    def result(action, detail, **extra):
        plan = {"action": action, "detail": detail, "login": name,
                "local_version": "", "hub_version": "", "hash": "",
                "saved": None, "backup": None}
        plan.update(extra)
        return plan

    lock = plat.FileLock(lock_path or SYNC_LOCK)
    if not lock.acquire(wait=5.0):
        return result("blocked", "another NERSC companion sync is already running")
    try:
        local_text = source.read_text(encoding="utf-8") if source.is_file() else ""
        local_hash = digest(local_text)
        local_version = inspect(local_text)[0] if local_text else ""

        hub_text, problem = read_hub(ctx, name)
        if problem == "absent":
            return result("install", "the hub has no companion yet",
                          local_version=local_version, hash=local_hash)
        if hub_text is None:
            return result("blocked", problem, local_version=local_version)

        hub_hash = digest(hub_text)
        if hub_hash == local_hash:
            record_installed(backend, local_text, name, record_path)
            return result("keep", "the hub already runs the canonical source",
                          local_version=local_version, hub_version=local_version,
                          hash=local_hash)

        hub_version, bad = inspect(hub_text)
        size = len(hub_text.encode("utf-8"))
        maximum = ctx.settings.int("COMPANION_MAX_BYTES")
        if not bad and size > maximum:
            bad = f"the hub's companion is {size} bytes; the limit is {maximum}"

        def conflict(detail):
            saved = _keep_copy(hub_text, conflict_dir)
            record_conflict(backend, hub_version or "unknown", hub_hash, saved,
                            record_path)
            return result("conflict", detail, local_version=local_version,
                          hub_version=hub_version, hash=hub_hash, saved=saved)

        def adopt(detail):
            if not ctx.settings.flag("COMPANION_ADOPT_HUB_EDITS"):
                return conflict(f"{detail}; COMPANION_ADOPT_HUB_EDITS is off, so "
                                f"the edit is kept there and not adopted")
            backup = _adopt(hub_text, source, backup_dir, local_text)
            record_installed(backend, hub_text, name, record_path)
            return result("adopt", detail, local_version=local_version,
                          hub_version=hub_version, hash=hub_hash, backup=backup)

        if bad:
            # Not our tool: adopting it would replace the source with something
            # unknown, and overwriting it blind could destroy someone's file.
            if overwrite:
                return result("push", f"--overwrite-tool: replacing an unrecognised "
                                      f"file on the hub ({bad})",
                              local_version=local_version, hash=local_hash,
                              saved=_keep_copy(hub_text, conflict_dir))
            saved = _keep_copy(hub_text, conflict_dir)
            record_conflict(backend, hub_version or "unknown", hub_hash, saved,
                            record_path)
            return result("blocked", bad, local_version=local_version,
                          hub_version=hub_version, hash=hub_hash, saved=saved)

        if overwrite:
            return result("push", "--overwrite-tool: replacing the hub's copy",
                          local_version=local_version, hub_version=hub_version,
                          hash=local_hash, saved=_keep_copy(hub_text, conflict_dir))

        installed = (str(installed_record(backend, record_path).get("hash") or "")
                     if record_path else "")
        if not installed:
            # No record of what we last installed: the higher VERSION wins, and
            # a tie goes to the hub, because the hub is where edits happen.
            if _version_key(local_version) > _version_key(hub_version):
                return result("push", f"no install record; the local source "
                                      f"v{local_version} outranks the hub's "
                                      f"v{hub_version}",
                              local_version=local_version,
                              hub_version=hub_version, hash=local_hash)
            return adopt(f"no install record, and the hub's v{hub_version} "
                         f"does not trail the local source")
        if hub_hash != installed and local_hash == installed:
            return adopt(f"the companion was edited on '{name}'")
        if hub_hash == installed:
            return result("push", "the canonical source is ahead of the hub",
                          local_version=local_version, hub_version=hub_version,
                          hash=local_hash)
        return conflict(f"'{name}' and this machine both changed the companion "
                        f"since it was installed there")
    finally:
        lock.release()


def sync_from_hub(ctx, login_name=None, destination=None, backup_dir=None,
                  lock_path=None, baseline_path=None):
    """Take the hub's companion into the canonical source, unconditionally."""
    name = ctx.login(login_name)
    if ctx.backend.companion_drives:
        ui.die(f"login '{name}' is on NERSC itself",
               "sync from the hub, where agents edit the companion")

    destination = Path(destination or SOURCE)
    backup_dir = Path(backup_dir or BACKUP_DIR)
    record_path = _record_target(destination, baseline_path)
    lock = plat.FileLock(lock_path or SYNC_LOCK)
    if not lock.acquire():
        ui.die("another NERSC companion sync is already running")
    try:
        ctx.logins.ensure(name)
        incoming, problem = read_hub(ctx, name)
        if problem == "absent":
            ui.die(f"there is no companion on hub '{name}'",
                   f"install it first: cluster nersc-tool install {name}")
        if problem:
            ui.die(problem)

        size = len(incoming.encode("utf-8"))
        maximum = ctx.settings.int("COMPANION_MAX_BYTES")
        if size > maximum:
            ui.die(f"the hub's NERSC companion is {size} bytes; the limit is {maximum}",
                   "check that ~/.local/bin/nersc on the hub is the intended script")
        version = _validated_version(incoming)

        current = destination.read_text(encoding="utf-8") if destination.is_file() else ""
        new_hash = digest(incoming)
        if current == incoming:
            # Still worth recording: this proves what the hub holds, which is
            # what keeps the next bridge push from reading it as an edit.
            record_installed(ctx.backend.name, incoming, name, record_path)
            ui.info(f"the NERSC companion already matches '{name}' "
                    f"(v{version}, {new_hash[:12]})")
            return 0

        # This checkout's own source may be mid-edit: that must neither stop
        # the sync nor be blamed on the hub.
        old_version = (inspect(current)[0] or "?") if current else "none"
        backup = _adopt(incoming, destination, backup_dir, current)
        record_installed(ctx.backend.name, incoming, name, record_path)
        ui.info(f"took the NERSC companion from '{name}': "
                f"v{old_version} -> v{version} ({new_hash[:12]})")
        if backup:
            ui.note(f"the previous source is kept at {backup}")
        return 0
    finally:
        lock.release()
