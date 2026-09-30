"""`cluster status` and `cluster doctor`.

``status`` answers "what is running right now"; ``doctor`` answers "is this
machine able to do the job at all". They are separate because the second is the
thing you run when the first looks wrong.
"""

from __future__ import annotations

import getpass
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import (config, linger, mounts as mountstate, platform as plat,
               strays as straylib, ui)
from .auth import seconds_left_in_window


def _show_tmux_details(details, include_ssh=False):
    ui.say("sessions:")
    if details.sessions:
        ui.table([
            [row.name, row.owner + (f" (via {row.foreign})" if row.foreign else ""),
             row.windows, "attached" if row.attached else "detached",
             row.created or "-"]
            for row in details.sessions
        ], ["SESSION", "OWNER", "WINDOWS", "CLIENTS", "CREATED"])
    else:
        ui.say("  (none)")

    ui.say("\nwindows:")
    if details.windows:
        ui.table([
            [f"{row.session}:{row.index}", row.name, row.panes,
             "yes" if row.active else "no"]
            for row in details.windows
        ], ["TARGET", "NAME", "PANES", "ACTIVE"])
    else:
        ui.say("  (none)")

    ui.say("\ntmux clients:")
    if details.clients:
        ui.table([
            [row.name, row.session, row.tty] for row in details.clients
        ], ["CLIENT", "SESSION", "TTY"])
    else:
        ui.say("  (none)")

    if include_ssh:
        ui.say("\nssh clients for this user on the node:")
        if details.ssh_clients:
            for line in details.ssh_clients:
                ui.say(f"  {line}")
        else:
            ui.say("  (none)")


def sessions(ctx, name):
    """The session, window and client report for one login."""
    ctx.logins.ensure(name)
    complete, details = ctx.tmux.details_checked(name, include_who=False)
    if not complete:
        ui.warn(f"could not read tmux diagnostics through '{name}'")
        return 1
    _show_tmux_details(details)
    return 0


def where(ctx, name):
    """Node identity plus tmux and SSH clients for one managed login."""
    if not ctx.logins.is_active(name):
        pinned = ctx.state.pin_read(name)
        ui.say(f"{name}: not connected" +
               (f" (pinned to {ctx.backend.short(pinned)})" if pinned else ""))
        return 1
    fqdn, complete, details = ctx.tmux.node_and_details_checked(name)
    ui.say(f"node: {ctx.backend.short(fqdn) or '-'}")
    ui.say(f"fqdn: {fqdn or '-'}\n")
    if not complete:
        ui.warn(f"could not read tmux/SSH diagnostics through '{name}'")
        return 1
    _show_tmux_details(details, include_ssh=True)
    return 0


def status(ctx):
    backend = ctx.backend
    ui.say(ui.bold(f"{backend.label} — {backend.user or backend.target()}"))
    state, detail = backend.credential_state()
    mark = {"ok": ui.green, "ready": ui.green, "expiring": ui.yellow,
            "missing": ui.red}[state]
    ui.say(f"credential: {mark(state)} — {detail}")
    refused = ctx.state.refusals.status()
    if refused:
        ui.say(f"credential: {ui.red(refused)}")
        ui.note(f"check it with: {backend.credentials_command()}")

    names = ctx.state.known_logins()
    live_logins = []
    crumbs, layouts = None, []
    if not names:
        ui.say("\nno logins")
        ui.note("open one with: cluster new NAME")
    else:
        rows = []
        sessions_by_login = {}
        for name in names:
            active = ctx.logins.is_active(name)
            pinned = ctx.state.pin_read(name)
            live, sessions = "", []
            if active and not live_logins:
                # The first live login's read brings the shared home's
                # breadcrumbs and layouts too: any login would answer the same.
                live, sessions, crumbs, layouts = ctx.tmux.status_read(name)
            elif active:
                live, sessions = ctx.tmux.node_and_sessions(name)
            if active:
                live_logins.append(name)
                sessions_by_login[name] = sessions
            node = backend.short(live)
            if node and pinned and node != backend.short(pinned):
                node += "!"
            mounted = "-"
            if ctx.mounts.is_mounted(name):
                probe = ctx.mounts.probe(name)[0]
                mounted = mountstate.STATUS_LABEL[probe]
                via = ctx.state.mountnode_read(name)
                if via:
                    mounted += f" via {backend.short(via)}"
            rows.append([
                name,
                "active" if active else ("down" if pinned else "stale"),
                node or "-",
                backend.short(pinned) or "-",
                mounted,
                "on" if ctx.mounts.watcher_running(name) else "off",
            ])
        ui.say("")
        ui.table(rows, ["LOGIN", "STATE", "NODE", "PINNED", "MOUNT", "WATCH"])

    # tmux, from whichever login can answer
    if live_logins:
        ui.say("")
        rows = []
        for name in live_logins:
            for row in sessions_by_login[name]:
                rows.append([backend.short(ctx.logins.node_of(name)), row.name,
                             row.windows,
                             "attached" if row.attached else "detached",
                             row.owner + (f" (via {row.foreign})" if row.foreign else "")])
        if rows:
            ui.table(rows, ["NODE", "SESSION", "WINDOWS", "CLIENTS", "OWNER"])
        else:
            ui.say("no remote tmux sessions")

        # Breadcrumbs live in the backend's shared home. Every live login returns
        # the same catalogue, so asking each one would duplicate rows and round
        # trips. A read that failed is said as such: "none" would claim the
        # shared home holds no record of any session.
        if crumbs is None:
            ui.say("\nsession breadcrumbs on shared home: unreadable")
            crumbs = {}
        elif crumbs:
            ui.say("\nsession breadcrumbs on shared home:")
            ui.table(
                [[node, session, owner or "-"]
                 for (node, session), owner in sorted(crumbs.items())],
                ["NODE", "SESSION", "OWNER"],
            )
        else:
            ui.say("\nsession breadcrumbs on shared home: none")

        # Sessions recorded on nodes no login occupies: these are the ones that
        # quietly become unreachable, so they are called out with the verb that
        # reconciles them. Same definition as `ls` and `cluster strays` use.
        found = straylib.collect(ctx, crumbs)
        if found:
            ui.say("")
            straylib.report(found)

        ui.say("\nlayout snapshots on shared home: " +
               (", ".join(layouts) if layouts else "none"))

    from .transfer import Transfers

    tags = Transfers(ctx.logins).active_tags()
    if tags:
        ui.say(f"\nopen transfer connections: {', '.join(tags)}")

    ledger = ctx.state.ledger_nodes()
    if ledger:
        ui.say(f"ledger: {', '.join(backend.short(n) for n in ledger)}")
    used = ctx.logins.connection_count(active=live_logins, transfers=tags)
    ui.say(f"connections in use: {used}/{ctx.settings.int('MAX_LOGINS')}")
    return 0


#: A crontab line that restores logins at boot. Anything may sit between
#: `cluster` and `boot`, because the usual form names the backend
#: (`cluster --backend fasrc boot main`). A false alarm in a health check is
#: worse than no check: it teaches you to skim past the warnings.
BOOT_ENTRY = re.compile(r"^@reboot\b.*\bcluster\b.*\bboot\b")

#: The unit that asserts linger on the way down; see extras/ for both forms of
#: it and clustertool.linger for why its timing is the whole point.
HOOK_UNIT = "cluster-linger.service"


#: `systemctl show -p ExecStop` renders one line per command:
#: ``{ path=/home/you/.local/bin/cluster ; argv[]=... ; ignore_errors=yes ; }``
HOOK_PATH = re.compile(r"\bpath=(\S+)")

#: Seconds a local helper (systemctl, crontab, the lock prober) may take before
#: doctor reports it as not answering rather than waiting on it.
LOCAL_TIMEOUT = 10

#: Where a shutdown hook cannot exist, and what stands in for it.
NO_SYSTEMD = "there is no shutdown hook on this machine (no systemd)"
NO_SYSTEMD_HINT = ("on macOS a running `cluster watch` re-asserts linger every "
                   "LINGER_INTERVAL seconds while it is connected; its last "
                   "assertion, as it stops at shutdown, may come after the "
                   "connection is gone; the node-side keeper (LINGER_KEEPER) "
                   "covers that gap")


def has_systemd():
    """Whether this machine can run the shutdown hook at all."""
    return not plat.IS_MAC and bool(shutil.which("systemctl"))


def _systemctl(argv):
    """systemctl's answer, stripped. Raises TimeoutExpired past LOCAL_TIMEOUT."""
    return subprocess.run(argv, capture_output=True, text=True,
                          timeout=LOCAL_TIMEOUT).stdout.strip()


def hook_installed(run=None):
    """``"system"``, ``"user"``, or ``""`` — where the shutdown hook is enabled.

    The distinction is not cosmetic. Only the system unit can be ordered
    against the user slice that holds the control masters, so only it is
    guaranteed to still have a connection to assert over.
    """
    run = run or _systemctl
    for kind, argv in (("system", ["systemctl", "is-enabled", HOOK_UNIT]),
                       ("user", ["systemctl", "--user", "is-enabled",
                                 HOOK_UNIT])):
        if run(argv) == "enabled":
            return kind
    return ""


def hook_program(kind, run=None):
    """The program *kind*'s hook would run at shutdown, or ``""`` if none.

    Enabled is not the same as working. `ExecStop` carries a leading ``-`` by
    design — a node may be unreachable and a shutdown must not be made to look
    broken by that — but the same leading ``-`` swallows "no such file", so a
    hook pointing at a program that has gone away fails silently and every
    shutdown afterwards looks perfectly healthy while nothing whatever is
    asserted. Moving the working tree, or dropping the ~/.local/bin symlink
    that this repo is deployed through, is all it takes. The unit records an
    absolute path taken at install time and never revisits it, so the path is
    the only thing that can be checked.
    """
    if not kind:
        return ""
    run = run or _systemctl
    argv = ["systemctl", "show", "-p", "ExecStop", "--value", HOOK_UNIT]
    if kind == "user":
        argv.insert(1, "--user")
    found = HOOK_PATH.search(run(argv) or "")
    return found.group(1) if found else ""


#: The two unit templates shipped with the tool, strongest first.
HOOK_TEMPLATES = (("system", "cluster-linger-system.service"),
                  ("user", "cluster-linger.service"))


def extras_dir():
    """Where the unit templates live, found from the installed package."""
    return Path(__file__).resolve().parent.parent / "extras"


def repo_root():
    """The checkout this package was loaded from."""
    return Path(__file__).resolve().parent.parent


def hook_entry_point():
    """The `cluster` a shutdown hook should run: the one running now.

    ``abspath`` rather than ``realpath``: a ~/.local/bin symlink is the name
    the tool was deployed under, and it keeps working when the checkout
    behind it moves. Anything that is not `cluster` itself (a test runner, a
    ``python -m``) falls back to the one on PATH, then to this checkout's.
    """
    argv0 = sys.argv[0] if sys.argv else ""
    if os.path.basename(argv0) == "cluster":
        return os.path.abspath(argv0)
    return shutil.which("cluster") or str(repo_root() / "bin" / "cluster")


def render_hook(text, kind, program=None, repo=None):
    """Fill a unit template's placeholders for *kind* ("system" or "user").

    ``%`` starts a specifier in a unit file, so paths have it doubled. In the
    user unit a path under home is written ``%h/…``: the unit stays correct if
    the home directory is renamed, and a standard install renders a unit with
    no absolute home path in it.
    """
    home = str(Path.home())

    def unit_path(path):
        path = str(path)
        if kind == "user" and (path == home or path.startswith(home + "/")):
            return "%h" + path[len(home):].replace("%", "%%")
        return path.replace("%", "%%")

    return (text.replace("@CLUSTER@", unit_path(program or hook_entry_point()))
            .replace("@REPO@", unit_path(repo or repo_root()))
            .replace("@USER@", getpass.getuser())
            .replace("@HOME@", home.replace("%", "%%")))


def _hook_runner():
    def run(argv, stdin=None):
        try:
            return subprocess.run(argv, input=stdin, capture_output=True,
                                  text=True)
        except FileNotFoundError as exc:
            # As a shell reports a missing command, so the step reads as the
            # failure it is rather than a traceback.
            return subprocess.CompletedProcess(argv, 127, "", str(exc))
    return run


def install_hook(interactive=True, run=None, extras=None):
    """Install and enable the shutdown hook, strongest form available.

    Returns ``(kind, detail)`` with *kind* ``"system"``, ``"user"`` or ``""``.
    The unit runs the `cluster` doing the installing (see
    :func:`hook_entry_point`).

    The system unit is tried first because it is the only form ordered against
    the slice holding the control masters; the ``--user`` one needs no root and
    is the fallback. Idempotent, and re-running is also the repair for a unit
    left pointing at a `cluster` that has since moved — the path is baked in at
    install time and never revisited afterwards.

    *interactive* false adds ``sudo -n``, so an unattended caller (a boot hook,
    a cron line) fails straight through to the user unit instead of blocking
    forever on a password prompt nobody is there to answer.

    A machine without systemd gets no unit at all: nothing is asked for and
    nothing is written.
    """
    if run is None and not has_systemd():
        return "", NO_SYSTEMD
    run = run or _hook_runner()
    extras = Path(extras) if extras else extras_dir()
    sudo = ["sudo"] + ([] if interactive else ["-n"])
    problems = []

    for kind, filename in HOOK_TEMPLATES:
        template = extras / filename
        if not template.is_file():
            problems.append(f"{filename} is missing from {extras}")
            continue
        body = render_hook(template.read_text(), kind)
        if kind == "system":
            steps = [(sudo + ["tee", f"/etc/systemd/system/{HOOK_UNIT}"], body),
                     (sudo + ["systemctl", "daemon-reload"], None),
                     (sudo + ["systemctl", "enable", "--now", HOOK_UNIT], None)]
        else:
            target = (config.xdg_dir("XDG_CONFIG_HOME", Path.home() / ".config")
                      / "systemd" / "user" / HOOK_UNIT)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body)
            steps = [(["systemctl", "--user", "daemon-reload"], None),
                     (["systemctl", "--user", "enable", "--now", HOOK_UNIT],
                      None)]
        failed = next((step for step, stdin in steps
                       if run(step, stdin).returncode != 0), None)
        if failed:
            problems.append(f"{kind}: `{' '.join(failed)}` failed")
            continue
        detail = {"system": "installed and enabled; its ExecStop is ordered "
                            "to run while the masters and the network are up",
                  "user": "installed and enabled, but a --user unit is not "
                          "ordered against the control masters — install the "
                          "system unit where you have root"}[kind]
        return kind, detail
    return "", "; ".join(problems) or "no unit templates to install"


def hook_report(run=None, exists=None):
    """``(armed, detail)`` for the shutdown hook, as doctor words it.

    Separate from the checks that print it so the decision can be tested
    without a machine that happens to have the unit installed — and because
    "enabled" and "armed" are not the same question, which is the whole point
    of this function existing. See :func:`hook_program`.
    """
    exists = exists or (lambda path: Path(path).exists())
    try:
        kind = hook_installed(run)
        program = hook_program(kind, run)
    except subprocess.TimeoutExpired as exc:
        return False, f"systemctl did not answer within {exc.timeout:.0f}s"
    if program and not exists(program):
        return False, (f"{kind} unit is enabled but runs {program}, which does "
                       "not exist — ExecStop ignores the failure, so shutdowns "
                       "will look healthy while nothing is asserted; reinstall "
                       "it from extras/")
    return bool(kind), {
        "system": "system unit; ordered after user.slice, so it asserts linger "
                  "while the masters and the network are both still up",
        # Worth saying out loud rather than calling it ok: a --user unit is a
        # sibling of the session scope holding the control masters, and
        # nothing orders the two against each other at shutdown.
        "user": "user unit only; it may run after the control masters are gone "
                "— prefer extras/cluster-linger-system.service where you have "
                "root",
    }.get(kind, "not installed; a reboot falls back to the watcher's last "
                "assertion — see extras/cluster-linger-system.service")


def scope_finding(scope):
    """``(ok, detail)`` about where the node's tmux server lives, or ``None``.

    Linger keeps ``user@$UID.service`` alive once the account has no sessions
    left; it says nothing about the per-connection scope that logind kills
    when a connection ends. A server in the second one dies on a clean close
    whatever linger says, so this is reported separately from it — they are
    two different deaths and only one of them is linger's.
    """
    if scope in ("", "none"):
        return None
    if scope == "user":
        return True, ("tmux server runs under user@$UID.service; it outlives "
                      "the connection that started it")
    if scope == "session":
        return False, ("tmux server runs in this connection's session scope, "
                       "so it dies when the connection does, linger or not — "
                       "it was started outside a user scope and cannot be "
                       "moved; recreate the sessions to protect them")
    return False, "cannot tell which cgroup the tmux server is in"


def keeper_finding(found, wanted):
    """``(label_ok, detail)`` about a node's keeper, or ``None`` if it is fine.

    Both directions are findings. A missing keeper that was asked for does not
    protect anything; a keeper left behind after the setting went off is
    residue that nothing on this machine records and that goes on asserting
    once a minute, on a node shared with everyone else who logs in there.
    """
    if wanted and found == "no":
        return False, ("LINGER_KEEPER is on but the node has no keeper; "
                       "install it with: cluster linger")
    if found == "yes" and not wanted:
        return False, ("a keeper is installed on this node but LINGER_KEEPER "
                       "is off; it re-asserts linger every minute — take it "
                       "out with: cluster linger --remove-keeper")
    return None


def boot_entries(crontab_text):
    """The @reboot lines of *crontab_text* that restore logins."""
    return [line.strip() for line in (crontab_text or "").splitlines()
            if not line.lstrip().startswith("#")
            and BOOT_ENTRY.search(line.strip())]


# --- doctor --------------------------------------------------------------------

class Report:
    """Doctor's lines and its tally: one mark per check, one summary at the end.

    ``ok``, ``FAIL`` and ``warn`` judge something; ``off`` and ``n/a`` only
    say that an optional part is absent or does not apply here, and ``note``
    points at something worth a look, so they count as neither a problem nor
    a warning.
    """

    MARKS = {"ok": ui.green, "FAIL": ui.red, "warn": ui.yellow,
             "off": ui.dim, "n/a": ui.dim, "note": ui.dim}

    def __init__(self):
        self.problems = 0
        self.warnings = 0

    def line(self, mark, label, detail=""):
        painted = self.MARKS[mark](mark) + " " * (6 - len(mark))
        ui.say(f"  {painted}{label}" + (f" — {detail}" if detail else ""))

    def section(self, title):
        ui.say(ui.bold(title))

    def check(self, label, ok, detail="", fatal=True):
        if ok:
            self.line("ok", label, detail)
        elif fatal:
            self.problems += 1
            self.line("FAIL", label, detail)
        else:
            self.warnings += 1
            self.line("warn", label, detail)

    def finish(self):
        """Print the summary; the exit status is 1 when anything failed."""
        warned = f", {self.warnings} warning(s)" if self.warnings else ""
        ui.say("")
        if self.problems:
            ui.say(ui.red(f"{self.problems} problem(s)") + warned)
            return 1
        ui.say(ui.green("no problems") + warned)
        return 0


def _rclone_found():
    """The rclone a transfer would pick, as :func:`transfer.find_rclone` says.

    Doctor only reports it: a missing or old rclone is a feature that is off,
    not a reason to stop the checkup.
    """
    from .transfer import find_rclone

    return find_rclone(config.Settings(None))


def _version(parts):
    return ".".join(str(p) for p in parts)


def feature_rows():
    """``[(feature, available, detail)]`` for every optional part of the tool.

    Each missing one says what to install, so a row reading "off" is never a
    dead end. Nothing here is required: a login, a session and `cluster run`
    need only ssh.
    """
    from . import globuslayer
    from .transfer import MIN_RCLONE

    rows = []
    missing = plat.mount_tools_missing()
    # The mount options this tool passes on macOS are macFUSE's, so its bundle
    # is named when absent. Only doctor looks: a mount still goes ahead, and
    # sshfs then reports what it lacks.
    if plat.IS_MAC and not Path("/Library/Filesystems/macfuse.fs").exists():
        missing.append("macFUSE")
    rows.append(("mounts", not missing,
                 shutil.which("sshfs") if not missing else
                 f"{' and '.join(missing)} not installed; "
                 f"{plat.sshfs_install_hint()}, or turn mounts off: "
                 "cluster config set AUTO_MOUNT 0"))

    rsync = shutil.which("rsync")
    rows.append(("push, pull and bridge", bool(rsync),
                 rsync or "rsync not installed; install it (apt install rsync, "
                          "dnf install rsync or brew install rsync)"))

    found = _rclone_found()
    need = _version(MIN_RCLONE)
    ok = found.problem is None
    if ok:
        detail = f"rclone {_version(found.version)} at {found.path}"
    elif found.version:
        detail = (f"rclone {_version(found.version)} at {found.path} is too old "
                  f"(need {need})")
    else:
        detail = f"{found.path or 'rclone'} {found.problem} (need {need})"
    if not ok:
        detail += ("; install the official binary from "
                   "https://rclone.org/downloads/, or point at one with: "
                   "cluster config set RCLONE /path/to/rclone")
    rows.append(("transfer and archive-sync", ok, detail))

    globus = globuslayer.find_cli()
    rows.append(("Globus transfers", bool(globus),
                 globus or "globus-cli not installed; install it with: "
                           "pipx install globus-cli"))

    if has_systemd():
        rows.append(("linger shutdown hook", True, "systemd"))
    else:
        rows.append(("linger shutdown hook", False,
                     "no systemd on this machine"
                     + (f"; {NO_SYSTEMD_HINT}" if plat.IS_MAC else "")))

    crontab = shutil.which("crontab")
    if plat.IS_MAC:
        rows.append(("restore after a reboot", False,
                     "not automatic on macOS; run `cluster boot` after a "
                     "reboot, or have a LaunchAgent run it at login: see "
                     "docs/setup.md#macos-launchagents"))
    else:
        rows.append(("restore after a reboot", bool(crontab),
                     "cron" if crontab else
                     "crontab not installed; install cron, or run `cluster "
                     "boot` after a reboot"))
    return rows


def _lock_excludes_processes():
    """``(ok, detail)``: does an flock taken here keep another process out?

    Proved with a second process rather than trusted, in the state directory
    when it exists (the filesystem that matters) and otherwise in the temp
    directory. The probe file is removed either way.
    """
    where = config.STATE_ROOT if config.STATE_ROOT.is_dir() else None
    handle, name = tempfile.mkstemp(prefix=".locktest-", dir=where)
    os.close(handle)
    probe = Path(name)
    lock = plat.FileLock(probe)
    try:
        if not lock.acquire():
            return False, f"could not lock {probe}"
        code = (
            "import fcntl,os,sys\n"
            f"fd=os.open({str(probe)!r}, os.O_RDWR)\n"
            "try:\n"
            "    fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB); sys.exit(0)\n"
            "except OSError: sys.exit(3)\n"
        )
        try:
            blocked = subprocess.run([sys.executable, "-c", code],
                                     capture_output=True,
                                     timeout=LOCAL_TIMEOUT).returncode == 3
        except subprocess.TimeoutExpired:
            return False, f"the probe process did not finish within {LOCAL_TIMEOUT}s"
        return blocked, ("logins are serialized with this" if blocked else
                         "a second process took the same lock")
    finally:
        lock.release()
        probe.unlink(missing_ok=True)


def _private_dir_check(report, label, path):
    """A directory of secrets or settings is the owner's alone (mode 700)."""
    try:
        status = path.stat()
    except FileNotFoundError:
        return
    except OSError as exc:
        report.check(label, False, f"cannot inspect {path}: {exc}", fatal=False)
        return
    if not path.is_dir():
        report.check(label, False, f"{path} is not a directory", fatal=False)
        return
    mode = oct(status.st_mode & 0o777)[2:]
    problems = []
    if mode != "700":
        problems.append(f"run: chmod 700 {path}")
    if hasattr(os, "getuid") and status.st_uid != os.getuid():
        problems.append(f"it belongs to uid {status.st_uid}, not to you (uid {os.getuid()})")
    report.check(label, not problems, "; ".join([f"{path} is mode {mode}"] + problems),
                 fatal=False)


def configuration_checks(report):
    """The settings file and the directories that hold it and the secrets."""
    problem = config.file_problem()
    if problem:
        report.check("settings file", False,
                     f"{problem}; every setting has its built-in value until "
                     "it is fixed")
    elif config.SETTINGS_FILE.is_file():
        report.check("settings file", True, str(config.SETTINGS_FILE))
        unread = config.unread_entries()
        if unread:
            report.line("note", "settings nothing reads",
                        ", ".join(f"[{section}] {name}"
                                  for section, name, _value in unread)
                        + "; `cluster config list` shows every setting")
    else:
        report.check("settings file", True,
                     f"none yet ({config.SETTINGS_FILE}); every setting has its "
                     "built-in value")
    _private_dir_check(report, "configuration directory", config.CONFIG_ROOT)
    _private_dir_check(report, "credentials directory", config.CRED_ROOT)


def machine_checks(report):
    """Checks about this machine, whatever backends are set up or in scope."""
    report.section("platform")
    report.check("python", True, _version(sys.version_info[:3]))
    report.check("os", True, ("macOS" if plat.IS_MAC else "linux")
                 + (" (portable shims forced)" if plat.FORCE_PORTABLE else ""))
    report.check("mount table source", True,
                 "/proc/self/mountinfo" if Path("/proc/self/mountinfo").exists()
                 and not plat.FORCE_PORTABLE else "mount(8)")
    report.check("process listing", True,
                 "/proc" if Path("/proc").is_dir() and not plat.FORCE_PORTABLE
                 else "ps")
    if plat.IS_MAC:
        report.line("n/a", "fuse abort",
                    "not on macOS (a wedged mount is unwedged with "
                    "diskutil unmount force)")
    else:
        report.check("fuse abort available",
                     Path("/sys/fs/fuse/connections").is_dir(),
                     "wedge recovery needs /sys/fs/fuse/connections",
                     fatal=False)

    report.section("\nrequired tools")
    for tool in ("ssh", "ssh-keygen"):
        found = shutil.which(tool)
        report.check(tool, bool(found), found or "not found")

    report.section("\nconfiguration")
    configuration_checks(report)

    report.section("\nfeatures")
    for feature, available, detail in feature_rows():
        if available:
            report.check(feature, True, f"available ({detail})")
        else:
            report.line("off", feature, detail)

    if not plat.IS_MAC:
        report.section("\nreboot restoration")
        crontab = shutil.which("crontab")
        if crontab:
            try:
                scheduled = subprocess.run([crontab, "-l"], capture_output=True,
                                           text=True, timeout=LOCAL_TIMEOUT)
            except subprocess.TimeoutExpired:
                report.check("@reboot `cluster boot` entry", False,
                             f"`crontab -l` did not answer within "
                             f"{LOCAL_TIMEOUT}s", fatal=False)
            else:
                found = boot_entries(scheduled.stdout)
                report.check("@reboot `cluster boot` entry", bool(found),
                             f"{len(found)} entry(s) present" if found else
                             "missing; logins will not auto-restore after this "
                             "machine reboots", fatal=False)
        # The other half of a reboot: restoring the logins afterwards is no
        # use if the sessions they reconnect to were killed on the way down.
        if has_systemd():
            armed, detail = hook_report()
            report.check(f"{HOOK_UNIT} shutdown hook", armed, detail,
                         fatal=False)

    report.section("\nfile locking")
    ok, detail = _lock_excludes_processes()
    report.check("flock excludes other processes", ok, detail)


def mountpoint_findings(ctx):
    """``[(label, detail, ok)]`` about this backend's mount point directories.

    Two things worth saying out loud, both of them silent otherwise:

    * A *non-empty unmounted* mount point means something wrote to the path
      while the filesystem was not there, so those bytes are on this machine
      and invisible from the cluster. It happens, for example, when a login
      declines to mount under ONE_MOUNT_PER_BACKEND and something writes to
      its mount point anyway.
    * A directory named after a login that no longer exists is debris. Empty
      ones are simply removed — ``rmdir`` refuses if anything is mounted or
      stored there, which is the same safety check ``forget`` relies on.

    The mount table is the only thing asked about a *mounted* path; a wedged
    FUSE mount must never be stat'ed. See :mod:`clustertool.platform`.
    """
    root = ctx.state.mount_root
    if not root.is_dir():
        return []
    known = set(ctx.state.known_logins())
    findings = []
    for path in sorted(root.iterdir()):
        # The mount table is consulted *before* anything stats this path:
        # is_dir() on a wedged FUSE mount blocks forever, which is exactly the
        # state a health check has to be able to run in.
        if plat.mount_table_has(path) or not path.is_dir():
            continue
        try:
            contents = sorted(p.name for p in path.iterdir())
        except OSError as exc:
            findings.append((f"  mount point {path.name}", str(exc), False))
            continue
        if contents:
            findings.append((
                f"  mount point {path.name} is not mounted but not empty",
                "these were written to this machine, not the cluster: "
                + ", ".join(contents[:6]) + f"; look in {path}", False))
        elif path.name not in known:
            try:
                path.rmdir()
                findings.append((f"  stale mount point {path.name}",
                                 "no such login any more; removed", True))
            except OSError as exc:
                findings.append((f"  stale mount point {path.name}",
                                 f"no such login any more: {exc}", False))
    return findings


def clock_finding(offset, source):
    """``(ok, detail, fatal)`` about this machine's clock, as TOTP needs it."""
    if offset is None:
        if source == "timedatectl":
            return False, ("system clock not NTP-synchronised (timedatectl); "
                           "TOTP codes will be rejected once it drifts"), False
        return False, (f"could not check ({source}); if the system clock is "
                       "not NTP-synchronised, TOTP codes will be rejected"), False
    if abs(offset) < 5:
        return True, f"offset {offset:+.2f}s via {source}", True
    return False, (f"offset {offset:+.2f}s via {source}: system clock not "
                   "NTP-synchronised; TOTP codes will be rejected"), True


def credential_checks(backend, report):
    """The credential itself, and the files and directory it is made from."""
    state, detail = backend.credential_state()
    report.check("credential", state != "missing", detail,
                 fatal=(state == "missing"))
    if backend.cred_dir is None:
        return      # it keeps none: ssh's own configuration says how
    cred_dir = Path(backend.cred_dir)
    # The directory matters as much as the files: a group-writable one lets
    # someone else swap your password file out from under you, and 600 files
    # inside it do nothing to stop that.
    _private_dir_check(report, "credential directory permissions", cred_dir)
    for field in backend.CREDENTIALS:
        path = cred_dir / field.filename
        if field.secret and path.exists():
            mode = plat.file_mode(path)
            report.check(f"{field.filename} permissions", mode in ("600", "400"),
                         f"mode {mode}"
                         + ("" if mode in ("600", "400") else
                            f"; run: chmod 600 {path}"),
                         fatal=False)
    # Anything else in there is not read, and is worth a look: a stray copy
    # of a secret is how a credential directory ends up in a backup.
    if cred_dir.is_dir():
        known = [field.filename for field in backend.CREDENTIALS]
        others = sorted(p.name for p in cred_dir.iterdir() if p.name not in known)
        if others:
            report.line("note", "other files in the credential directory",
                        ", ".join(others) + f"; this tool reads only {', '.join(known)}")


def _resolves(backend, node):
    """Whether the host ssh dials first for *node* resolves; true when no
    probe from here can tell (Backend.node_probe_host)."""
    probe = backend.node_probe_host(node)
    return probe is None or plat.dns_ok(probe[0])


def backend_checks(ctx, report):
    """Health of one backend that is set up on this machine."""
    backend = ctx.backend
    report.section(f"\ncredentials ({backend.name})")
    credential_checks(backend, report)
    if backend.paces_totp:
        left = seconds_left_in_window()
        report.check("TOTP window", True,
                     f"{left:.0f}s left in the current 30s window")
    ok, detail, fatal = clock_finding(
        *plat.clock_offset(ctx.settings.str("NTP_SERVER")))
    report.check("clock sync", ok, detail, fatal=fatal)

    report.section("\nnetwork")
    reach = backend.reach_host()
    if reach is None:
        report.check(f"reach {backend.pool_host}", True,
                     "through a proxy command; only a connection can tell")
    else:
        host, port = reach
        report.check(f"resolve {host}", plat.dns_ok(host))
        report.check(f"reach {host}:{port}", plat.tcp_open(host, port, 8),
                     fatal=False)
    for node_class in backend.node_classes:
        if node_class.routable:
            members = node_class.members()
            reachable = sum(1 for m in members[:4] if _resolves(backend, m))
            report.check(f"{node_class.name} nodes resolve",
                         reachable > 0, f"{reachable}/{min(4, len(members))} sampled",
                         fatal=False)
        else:
            report.check(f"{node_class.name} nodes", True,
                         "not directly routable; reached through the pool address")

    report.section("\nlogins")
    names = ctx.state.known_logins()
    if not names:
        ui.say("  (none)")
    for name in names:
        _login_checks(ctx, report, name)

    for label, detail, ok in mountpoint_findings(ctx):
        report.check(label, ok, detail, fatal=False)


def _login_checks(ctx, report, name):
    backend = ctx.backend
    pinned = ctx.state.pin_read(name)
    active = ctx.logins.is_active(name)
    report.check(f"login {name}", True,
                 ("active" if active else "not connected")
                 + (f", pinned to {backend.short(pinned)}" if pinned
                    else ", unpinned"))
    if active:
        # The failure mode this catches is invisible from anywhere else: the
        # master answers `-O check` normally while the server refuses every
        # new channel, so a login can be "active" and unusable at once. A
        # warning rather than a problem — a full table is a busy day, not a
        # fault, until something needs a channel.
        limit = ctx.settings.int("SSH_MAX_SESSIONS")
        held = ctx.logins.channel_clients(name)
        free = ctx.logins.channels_free(name)
        kinds = ", ".join(
            f"{sum(1 for _, k in held if k == kind)} x {kind}"
            for kind in sorted({k for _, k in held})) or "nothing riding it"
        report.check(f"  channels {name}", free > 0,
                     f"{len(held)}/~{limit} in use, {free} free ({kinds})",
                     fatal=False)
        if backend.reaps_on_logout:
            _linger_checks(ctx, report, name)
    if ctx.mounts.is_mounted(name):
        probe_state = ctx.mounts.probe(name)
        report.check(f"  mount {name}", probe_state[0] in mountstate.STATUS_OK,
                     probe_state[1], fatal=False)


def _linger_checks(ctx, report, name):
    # The one check here that is about work rather than about this machine:
    # without linger, everything this login is running on its node dies the
    # moment the last connection to it does, and a reboot of this machine is
    # exactly that moment.
    #
    # Read even where LINGER is off, because that is the case with something
    # to find: turning the setting off does not reach the node, so a keeper
    # installed while it was on stays there asserting once a minute, and
    # nothing on this machine records that it exists. One round trip in a
    # command whose whole job is to look.
    found = linger.read(ctx.logins, name)
    keeper = {"yes": ", node-side keeper installed", "no": ""}.get(
        found["keeper"], "")
    if not linger.required(ctx.logins):
        report.check(f"  linger {name}", True,
                     "not asserted here (LINGER is off); the node is "
                     f"{'lingering anyway' if found['linger'] == 'yes' else 'not lingering'}")
    else:
        report.check(f"  linger {name}", found["linger"] == "yes",
                     {"yes": "on; user@$UID.service stays up here with "
                             "no sessions left" + keeper,
                      "no": "off; a reboot here would take this node's "
                            "user manager with it — cluster login "
                            f"{name} re-asserts it",
                      }.get(found["linger"], "the node would not say"),
                     fatal=False)
    finding = keeper_finding(found["keeper"], linger.keeper_wanted(ctx.logins))
    if finding:
        report.check(f"  linger keeper {name}", finding[0], finding[1],
                     fatal=False)
    finding = scope_finding(found["scope"])
    if finding:
        report.check(f"  tmux scope {name}", finding[0], finding[1],
                     fatal=False)
