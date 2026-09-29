"""Idempotent workstation and cluster-side integration setup.

This module owns configuration *merges*, not command-line parsing.  Local VS
Code Server settings and remote tmux settings are deliberately separate so
either can be checked and tested without constructing an SSH context.
"""

from __future__ import annotations

import json
import re
import shlex
import time
from pathlib import Path

from . import bridge, companion, config, platform as plat, ui


TMUX_BEGIN = "# >>> cluster setup: remote coding compatibility >>>"
TMUX_END = "# <<< cluster setup: remote coding compatibility <<<"
_TMUX_CONTENT = "__CLUSTER_SETUP_TMUX_CONFIG_v1__"

#: What the remote install prints on its way: just before the new file is
#: moved into place, just after, and whether the live server reloaded it. The
#: first two tell a failure that left the old file from one that may not have.
_TMUX_MOVING = "cluster-setup-moving"
_TMUX_INSTALLED = "cluster-setup-installed"
_TMUX_RELOADED = "cluster-setup-reloaded="

# Each unit has an intentionally broad detector.  A user's equivalent setting
# wins over our spelling and stays outside the managed block.
TMUX_UNITS = (
    ("terminal title publishing",
     re.compile(r"(?mi)^[^#\n]*\bset-titles\s+on(?=\s|['\"]|$)"),
     "set -g set-titles on"),
    ("session/window terminal title",
     re.compile(r"(?mi)^[^#\n]*\bset-titles-string\b[^\n]*#S[^\n]*#W"),
     "set -g set-titles-string 'cluster:#S:#W'"),
    # A window is sized to the smallest client attached to its *session*, so a
    # second client anywhere — the same login driven from the laptop, or an
    # attach left behind by a closed terminal — pins the pane to that client's
    # width. Resizing the VS Code terminal then changes nothing the remote
    # shell knows about, and it keeps wrapping at the old column. Aggressive
    # resizing sizes each window to the clients actually looking at it. Spelled
    # `setw` because on tmux 2.7, which FASRC runs, `set` does not reach
    # the window option table. A user who wrote `off` has an opinion; it wins.
    ("window sizing for resized clients",
     re.compile(r"(?mi)^[^#\n]*\baggressive-resize\s+(?:on|off)(?=\s|['\"]|$)"),
     "setw -g aggressive-resize on"),
    ("focus events",
     re.compile(r"(?mi)^[^#\n]*\bset(?:-option)?\s+-s\s+focus-events\s+on(?=\s|['\"]|$)"),
     "set -s focus-events on"),
    ("OSC 52 clipboard",
     re.compile(r"(?mi)^[^#\n]*\bset(?:-option)?\s+-s\s+set-clipboard\s+on(?=\s|['\"]|$)"),
     "set -s set-clipboard on"),
    ("extended keys", re.compile(r"(?mi)^[^#\n]*\bextended-keys\s+on(?=\s|['\"]|$)"),
     "if-shell \"tmux -V | grep -Eq '^tmux (3\\.([2-9]|[1-9][0-9]+)|([4-9]|[1-9][0-9]+)\\.)'\" \"set -s extended-keys on\""),
    ("terminal extended-key capability",
     re.compile(r"(?mi)^[^#\n]*\bterminal-features\b[^\n]*\bextkeys\b"),
     "if-shell \"tmux -V | grep -Eq '^tmux (3\\.([2-9]|[1-9][0-9]+)|([4-9]|[1-9][0-9]+)\\.)'\" \"set -as terminal-features ',xterm*:extkeys'\""),
    ("escape-sequence passthrough",
     re.compile(r"(?mi)^[^#\n]*\ballow-passthrough\s+on(?=\s|['\"]|$)"),
     "if-shell \"tmux -V | grep -Eq '^tmux (3\\.([3-9]|[1-9][0-9]+)|([4-9]|[1-9][0-9]+)\\.)'\" \"set -g allow-passthrough on\""),
)


def _backup(path, content, stem):
    """Keep one content-addressed recovery copy and return its path."""
    import hashlib

    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    root = config.STATE_ROOT / "setup-backups"
    backup = root / f"{stem}-{digest}"
    if not backup.exists():
        root.mkdir(parents=True, exist_ok=True)
        plat.atomic_write_text(backup, content, mode=0o600)
    return backup


def vscode_pattern(mount_root=None):
    """Watcher exclusion matching the configurable managed-mount directory."""
    root = Path(mount_root or config.MOUNT_ROOT).expanduser()
    return f"**/{root.name}/**"


def _health_path():
    return config.STATE_ROOT / "setup-local-health.json"


def _record_local_health(ready, detail):
    payload = json.dumps({
        "checked_at": int(time.time()),
        "ready": bool(ready),
        "detail": str(detail),
    }, indent=2) + "\n"
    plat.atomic_write_text(_health_path(), payload, mode=0o600)


def vscode_targets(settings_path=None):
    """The VS Code settings files setup merges into, installed or not.

    VSCODE_SETTINGS names one file. Otherwise: on macOS, the desktop editor's
    user settings; elsewhere, the VS Code Server's machine settings (what a
    Remote-SSH window onto this machine reads) and the desktop editor's user
    settings.
    """
    explicit = settings_path or config.global_value("VSCODE_SETTINGS")
    if explicit:
        return [Path(explicit).expanduser()]
    home = Path.home()
    if plat.IS_MAC:
        return [home / "Library" / "Application Support" / "Code" / "User"
                / "settings.json"]
    return [home / ".vscode-server" / "data" / "Machine" / "settings.json",
            config.xdg_dir("XDG_CONFIG_HOME", home / ".config") / "Code" / "User"
            / "settings.json"]


def _vscode_root(path):
    """The folder that shows the VS Code a settings file belongs to has run here.

    ``~/.vscode-server/data/Machine/settings.json`` belongs to
    ``~/.vscode-server``. ``.../Code/User/settings.json`` belongs to
    ``.../Code/User``, which the editor makes when it first starts: ``.../Code``
    alone is not enough, since other programs (the server's remote tooling,
    for one) keep files of their own there. A settings path of any other shape
    belongs to its own folder.
    """
    if path.parent.name == "Machine" and path.parent.parent.name == "data":
        return path.parents[2]
    return path.parent


def _vscode_name(path):
    return ("VS Code Server" if path.parent.name == "Machine" else "VS Code")


def _installed_targets(settings_path=None):
    return [path for path in vscode_targets(settings_path)
            if _vscode_root(path).is_dir()]


def vscode_installed(settings_path=None):
    """Whether a VS Code has ever run here, i.e. whether its directory exists.

    Setup configures an editor that is already present. It never creates one's
    directory: on a machine without VS Code that would be clutter, and it would
    make every later drift check nag about an editor nobody uses.
    """
    return bool(_installed_targets(settings_path))


def _nothing_installed(settings_path=None):
    return ", ".join(f"no {_vscode_name(path)} at {_vscode_root(path)}"
                     for path in vscode_targets(settings_path))


def _tab_title_wanted(tab_title=None):
    if tab_title is not None:
        return bool(tab_title)
    return config.global_value("VSCODE_TAB_TITLE",
                               config.GLOBAL_DEFAULTS["VSCODE_TAB_TITLE"]) == "1"


def _vscode_requirements(document, pattern, tab_title=False):
    missing = []
    excludes = document.get("files.watcherExclude", {})
    if not isinstance(excludes, dict):
        return False, [], "files.watcherExclude is not an object"
    if excludes.get(pattern) is not True:
        missing.append("watcher exclusion")
    # The tab title is a setting for every terminal VS Code opens, not only
    # cluster's, so it is part of "ready" only for somebody who asked for it.
    title = document.get("terminal.integrated.tabs.title")
    if tab_title and "${shellCommand}" not in str(title or ""):
        missing.append("cluster launch command in VS Code tabs")
    return not missing, missing, ""


def _vscode_document(path):
    if not path.exists():
        return {}, ""
    try:
        original = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    try:
        document = json.loads(original)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"{path} is JSON-with-comments or malformed JSON ({exc}); "
            "cluster will not rewrite it or discard comments"
        ) from exc
    if not isinstance(document, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return document, original


def vscode_status(settings_path=None, mount_root=None, tab_title=None):
    """Return ``(ready, detail)`` for the watcher safeguard in every installed
    VS Code."""
    targets = _installed_targets(settings_path)
    if not targets:
        return True, f"{_nothing_installed(settings_path)}; nothing to do"
    results = [_vscode_file_status(path, mount_root, tab_title) for path in targets]
    return (all(ready for ready, _detail in results),
            "; ".join(detail for _ready, detail in results))


def _vscode_file_status(path, mount_root=None, tab_title=None):
    tab_title = _tab_title_wanted(tab_title)
    try:
        document, _original = _vscode_document(path)
    except ValueError as exc:
        return False, str(exc)
    pattern = vscode_pattern(mount_root)
    ready, missing, problem = _vscode_requirements(document, pattern, tab_title)
    if problem:
        return False, f"{path}: {problem}"
    if ready:
        return True, (f"{path}: watcher excluded"
                      + ("; terminal title uses shell command" if tab_title else ""))
    return False, f"{path}: missing " + ", ".join(missing)


def configure_vscode(check=False, settings_path=None, mount_root=None,
                     tab_title=None):
    """Merge the managed mount exclusion into each installed VS Code's settings.

    Only where a VS Code already is, and the terminal tab title only when
    ``VSCODE_TAB_TITLE`` asks for it (see :func:`_vscode_requirements`).
    Returns whether every one of them is ready.
    """
    targets = _installed_targets(settings_path)
    if not targets:
        if check:
            ui.say(f"local VS Code integration: {_nothing_installed(settings_path)}")
        else:
            ui.info(f"{_nothing_installed(settings_path)}; nothing to configure")
            ui.note("if it lives elsewhere: cluster config set VSCODE_SETTINGS PATH")
        return True
    ready = True
    for path in targets:
        ready = _configure_vscode_file(path, check, mount_root, tab_title) and ready
    return ready


def _configure_vscode_file(path, check, mount_root, tab_title):
    tab_title = _tab_title_wanted(tab_title)
    pattern = vscode_pattern(mount_root)
    try:
        document, original = _vscode_document(path)
    except ValueError as exc:
        ui.warn(str(exc))
        ui.note(f"add {json.dumps(pattern)}: true under files.watcherExclude by hand")
        return False

    excludes = document.setdefault("files.watcherExclude", {})
    if not isinstance(excludes, dict):
        ui.warn(f"{path}: files.watcherExclude is not an object; cluster will "
                "not replace an existing setting of another type")
        return False
    ready, missing, _problem = _vscode_requirements(document, pattern, tab_title)
    if check:
        ui.say(f"local VS Code integration ({path}): "
               f"{'ready' if ready else 'needs setup'}")
        ui.say(f"  watcher: {pattern}")
        if tab_title:
            ui.say("  terminal tabs: shell launch command"
                   + ("" if "cluster launch command in VS Code tabs" not in missing
                      else " (missing)"))
        else:
            ui.say("  terminal tabs: left alone (VSCODE_TAB_TITLE is off)")
        return ready
    if ready:
        _record_local_health(True, f"{path}: ready")
        ui.info(f"{_vscode_name(path)} watcher exclusion"
                + (" and tmux tab titles are" if tab_title else " is")
                + f" already configured ({path})")
        return True

    excludes[pattern] = True
    title_conflict = False
    if tab_title:
        title = document.get("terminal.integrated.tabs.title")
        title_conflict = (title not in (None, "${process}")
                          and "${shellCommand}" not in str(title))
        if not title_conflict:
            document["terminal.integrated.tabs.title"] = "${shellCommand}"
            if document.get("terminal.integrated.tabs.description") is None:
                document["terminal.integrated.tabs.description"] = ""
    rendered = json.dumps(document, indent=4, ensure_ascii=False) + "\n"
    backup = _backup(path, original, "vscode-settings") if original else None
    # Under an install directory that exists: at most data/Machine or User
    # is created.
    path.parent.mkdir(parents=True, exist_ok=True)
    plat.atomic_write_text(path, rendered)
    ready_after, missing_after, _problem = _vscode_requirements(
        document, pattern, tab_title)
    _record_local_health(ready_after, f"{path}: "
                         + ("ready" if ready_after else ", ".join(missing_after)))
    ui.info(f"{_vscode_name(path)} now excludes {pattern} from file watching "
            f"({path})")
    if title_conflict:
        ui.warn("preserved custom terminal.integrated.tabs.title; it does not use ${shellCommand}")
        ui.note("include ${shellCommand} in that setting to display the cluster launch command")
    elif tab_title:
        ui.info("VS Code terminal tabs now use the shell launch command")
    if backup:
        ui.note(f"previous settings retained at {backup}")
    ui.note("reload the VS Code window only if an already-open watcher remains slow")
    return ready_after


def _without_managed_tmux_block(text):
    begins = [match.start() for match in re.finditer(re.escape(TMUX_BEGIN), text)]
    ends = [match.start() for match in re.finditer(re.escape(TMUX_END), text)]
    if not begins and not ends:
        return text
    if len(begins) != 1 or len(ends) != 1 or ends[0] < begins[0]:
        raise ValueError("existing cluster-managed tmux block is incomplete or duplicated")
    end = ends[0] + len(TMUX_END)
    if end < len(text) and text[end] == "\n":
        end += 1
    return text[:begins[0]] + text[end:]


def render_tmux_config(current):
    """Return ``(new_text, missing_labels)`` while preserving user content."""
    base = _without_managed_tmux_block(current)
    missing = [(label, line) for label, detector, line in TMUX_UNITS
               if not detector.search(base)]
    rendered = base.rstrip("\n")
    if missing:
        block = "\n".join([TMUX_BEGIN] + [line for _label, line in missing] + [TMUX_END])
        rendered = (rendered + "\n\n" if rendered else "") + block
    if rendered:
        rendered += "\n"
    return rendered, [label for label, _line in missing]


def _read_remote_tmux(ctx, name):
    timeout = ctx.settings.int("SETUP_REMOTE_TIMEOUT")
    command = (
        "command -v tmux >/dev/null 2>&1 || exit 127; "
        "tmux -V; "
        f"printf '%s\\n' {_TMUX_CONTENT!r}; "
        "if test -f \"$HOME/.tmux.conf\"; then printf 'present\\n'; "
        "cat \"$HOME/.tmux.conf\"; else printf 'missing\\n'; fi"
    )
    proc = ctx.logins.run_remote(name, command, timeout=timeout)
    if proc.returncode != 0:
        detail = ((proc.stderr or "") + (proc.stdout or "")).strip().splitlines()
        if proc.returncode == 127:
            ui.die(f"tmux is not installed on '{name}'",
                   "install the site's tmux module/package, then rerun cluster setup")
        ui.die(f"could not inspect remote tmux configuration through '{name}'",
               *(detail[-2:] if detail else [f"remote command rc={proc.returncode}"]))
    head, marker, body = (proc.stdout or "").partition(_TMUX_CONTENT + "\n")
    if not marker:
        ui.die(f"remote tmux inspection through '{name}' returned an incomplete reply")
    version = head.strip().splitlines()[-1] if head.strip() else "tmux (unknown)"
    state, newline, current = body.partition("\n")
    if not newline or state not in ("present", "missing"):
        ui.die(f"remote tmux inspection through '{name}' returned invalid state")
    return version, current if state == "present" else "", state == "present"


def _install_remote_tmux(ctx, name, rendered, current, existed):
    """Validate and install *rendered* as ~/.tmux.conf on login *name*.

    Returns (how the live server took it: "yes", "none" (no server), "failed",
    or "" when that was not reported; the backup's path, or "" with no old
    file; the remote output worth showing). Dies saying whether the old file
    was left in place, or that nobody can tell.
    """
    import base64
    import hashlib

    encoded = base64.b64encode(rendered.encode("utf-8")).decode("ascii")
    old_hash = hashlib.sha256(current.encode("utf-8")).hexdigest()[:12]
    backup = f"$HOME/.tmux.conf.cluster-backup-{old_hash}"
    timeout = ctx.settings.int("SETUP_REMOTE_TIMEOUT")
    # Validation gets its own tmux socket and one disposable detached session.
    # The default socket is only touched *after* the new file has validated, and
    # only when it already exists; this avoids tmux 2.7's server-less query bug.
    # The file is sourced by a client rather than loaded at server start: a
    # detached server keeps its start-up config errors to itself (measured
    # with tmux 3.6), while a client's source-file prints them, and in 3.6
    # also exits 1. Either one refuses the file.
    command = f"""
set -eu
dest="$HOME/.tmux.conf"
tmp="$HOME/.tmux.conf.cluster-new"
sock="cluster-setup-$$"
errors="$HOME/.tmux.conf.cluster-errors"
cleanup() {{ tmux -L "$sock" kill-server >/dev/null 2>&1 || :; rm -f "$tmp" "$errors"; }}
trap cleanup 0 1 2 3 15
printf %s {shlex.quote(encoded)} | base64 -d > "$tmp"
if test -e "$dest"; then chmod --reference="$dest" "$tmp"; else chmod 600 "$tmp"; fi
tmux -L "$sock" -f /dev/null new-session -d -s cluster_setup_validate
if ! tmux -L "$sock" source-file "$tmp" >"$errors" 2>&1 || test -s "$errors"; then
    cat "$errors" >&2; exit 65
fi
tmux -L "$sock" kill-server >/dev/null 2>&1 || :
rm -f "$errors"
if test -f "$dest" && ! test -e "{backup}"; then cp -p "$dest" "{backup}"; fi
printf '{_TMUX_MOVING}\\n'
mv -f "$tmp" "$dest"
printf '{_TMUX_INSTALLED}\\n'
trap - 0 1 2 3 15
default_socket="${{TMUX_TMPDIR:-/tmp}}/tmux-$(id -u)/default"
if ! test -S "$default_socket"; then
    printf '{_TMUX_RELOADED}none\\n'
elif tmux source-file "$dest"; then
    printf '{_TMUX_RELOADED}yes\\n'
else
    printf '{_TMUX_RELOADED}failed\\n'
fi
""".strip()
    proc = ctx.logins.run_remote(name, command, timeout=timeout)
    said = (proc.stdout or "").splitlines()
    detail = [line for line in
              ((proc.stderr or "") + (proc.stdout or "")).strip().splitlines()
              if not line.startswith("cluster-setup-")]
    if _TMUX_INSTALLED in said:
        reloaded = next((line[len(_TMUX_RELOADED):] for line in said
                         if line.startswith(_TMUX_RELOADED)), "")
        return reloaded, backup if existed else "", detail
    # With the script's own exit status its whole output arrived, so no
    # marker means it stopped before the move. A timeout (124), a lost
    # connection (255) or a move begun and not reported leaves it unknown.
    if _TMUX_MOVING in said or proc.returncode in (0, 124, 255):
        ui.die(f"could not tell whether ~/.tmux.conf on '{name}' was replaced: "
               f"the install did not report back (rc={proc.returncode})",
               *detail[-2:],
               f"look with: cluster setup --remote-only --check {name}")
    ui.die(f"remote tmux validation/install failed on '{name}'",
           *(detail[-3:] if detail else [f"remote command rc={proc.returncode}"]),
           "the existing ~/.tmux.conf and live tmux server were left in place")


def configure_remote_tmux(ctx, login_name=None, check=False):
    """Safely check or merge compatibility settings on one cluster home."""
    name = ctx.login(login_name)
    ctx.logins.ensure(name)
    version, current, existed = _read_remote_tmux(ctx, name)
    try:
        rendered, missing = render_tmux_config(current)
    except ValueError as exc:
        ui.die(f"cannot manage ~/.tmux.conf on '{name}': {exc}",
               "repair or remove only the cluster-managed marker block, then retry")
    node = ctx.backend.short(ctx.logins.node_of(name) or "") or "unknown node"
    changed = rendered != current
    if check:
        ui.say(f"remote tmux [{ctx.backend.name}:{name}@{node}]: {version}; "
               + ("ready" if not changed else "needs setup"))
        if changed and missing:
            ui.say("  missing: " + ", ".join(missing))
        return not changed
    if not changed:
        ui.info(f"remote tmux already compatible on '{name}' ({node}, {version})")
        return True
    reloaded, backup, detail = _install_remote_tmux(ctx, name, rendered, current,
                                                    existed)
    ui.info(f"remote tmux configured on '{name}' ({node}, {version}); " + {
        "yes": "live server reloaded",
        "none": "no live server to reload",
        "failed": "the live server did not reload it",
    }.get(reloaded, "the connection ended before the live server's reload "
                    "was reported"))
    if reloaded not in ("yes", "none"):
        for line in detail[-2:]:
            ui.note(line)
        ui.note("a new tmux server reads it; to reload a running one, run "
                f"`tmux source-file ~/.tmux.conf` on '{name}'")
    if backup:
        ui.note(f"previous remote config retained at ~/{backup.split('/')[-1]}")
    return True


def _remote_companion_matches(ctx, name):
    import hashlib

    digest = hashlib.sha256(companion.SOURCE.read_bytes()).hexdigest()
    timeout = ctx.settings.int("SETUP_REMOTE_TIMEOUT")
    command = ("test -r \"$HOME/.local/bin/nersc\" && "
               "sha256sum \"$HOME/.local/bin/nersc\" | awk '{print $1}'")
    proc = ctx.logins.run_remote(name, command, timeout=timeout)
    return proc.returncode == 0 and (proc.stdout or "").strip() == digest


def configure_companion(ctx, name, check=False):
    """Install the companion on a hub; an edit made there is never overwritten."""
    if ctx.backend.companion_drives:
        if check:
            ui.say(f"NERSC companion: not applicable on {ctx.backend.label} itself")
        return True
    matches = _remote_companion_matches(ctx, name)
    if check:
        ui.say(f"NERSC companion [{ctx.backend.name}:{name}]: "
               + ("matches this checkout" if matches else "needs install"))
        return matches
    # Coding agents may edit the hub's copy in place. bridge.install reconciles
    # before it ships (companion.reconcile_hub) and never overwrites such an
    # edit, so setup does not need its own pull step.
    bridge.install(ctx, name, tool_only=True, quiet=True)
    ui.info(f"NERSC companion reconciled with '{name}'")
    return True


def warn_if_local_drift():
    """Emit a cheap, concrete warning after non-interactive status commands."""
    # Nothing to say on a machine without a VS Code, or before anything is
    # mounted: no mount point means no expensive tree to watch yet. The mount
    # root itself is made by the first mount.
    if not vscode_installed():
        return
    try:
        if not any(config.MOUNT_ROOT.iterdir()):
            return
    except OSError:
        return
    settings = config.Settings(config.global_value("BACKEND"))
    interval = settings.int("SETUP_DRIFT_CHECK_INTERVAL")
    if interval == -1:
        return
    cache = {}
    try:
        cache = json.loads(_health_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        cache = {}
    if int(time.time()) - int(cache.get("checked_at", 0) or 0) < interval:
        ready = cache.get("ready") is True
        detail = str(cache.get("detail") or "cached setup check reports drift")
    else:
        ready, detail = vscode_status()
        _record_local_health(ready, detail)
    if ready:
        return
    ui.warn("local VS Code/cluster integration needs attention")
    ui.note(detail)
    ui.note("run: cluster setup --local-only")
