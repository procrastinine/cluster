"""Interactive login, tmux-session, and pin-management commands."""

from __future__ import annotations

import argparse

from .. import backends, platform as plat, registry, tmuxlayer, ui, workstation
from ..command import command
from ..lifecycle import (close_logins as _close_logins,
                         refresh_login as _refresh_login,
                         release_sessions as _release_sessions)
from ..processname import process_label
from ..remote_sh import tmux_target
from ..sshmux import quote_remote
from .mounts import auto_mount


def _split_double_dash(args):
    """Split options at the double-dash introducing a remote command."""
    if "--" in args:
        index = args.index("--")
        return args[:index], args[index + 1:]
    return list(args), []


def session_hint(ctx, session):
    """Which node a session name is registered on, from any live login.

    It turns "no login named X" into a useful message when X is really a
    session name. Looked up locally where possible so it costs nothing.
    """
    for candidate in ctx.logins.active_names():
        for (node, name), _owner in ctx.tmux.crumbs(candidate).items():
            if name == session:
                return node
    return ""


def recorded_elsewhere(ctx, session, crumbs, away_from=""):
    """``(node, owner)`` of a record of *session* this machine did not make,
    on a node other than *away_from*; None if there is none.

    Such a record is another workstation's, or names a login unknown here: in
    either case the session is someone's work on that node, not a name this
    machine may reuse there or here.
    """
    known = set(ctx.state.known_logins())
    for (node, name), owner in sorted((crumbs or {}).items()):
        if name != session or node == away_from:
            continue
        if workstation.is_other(getattr(owner, "workstation", "")) or (
                owner and owner not in known):
            return node, owner
    return None


def login_on_node(ctx, short):
    """This machine's login on node *short*, or ""."""
    for name in ctx.state.known_logins():
        if node_short(ctx, name) == short:
            return name
    return ""


def attach_elsewhere(ctx, session, node, owner):
    """Attach to *session* on *node*, which this machine did not start.

    Through this machine's login on that node, or, when it has none, a new
    login pinned there and named after the node (asked first: it costs an
    authentication). The session is only attached to — never created,
    tagged or recorded — so it stays its maker's.
    """
    made_on = getattr(owner, "workstation", "")
    whose = f"'{owner}' on workstation {made_on}" if made_on else f"'{owner}'"
    via = login_on_node(ctx, node)
    if not via:
        via = node
        tmuxlayer.require_name(via, "login name")
        if via in ctx.state.known_logins() or registry.is_taken_elsewhere(
                via, ctx.backend.name):
            ui.die(f"session '{session}' is on {node}, and the login name "
                   f"'{via}' is taken",
                   f"move a login of yours there, then attach through it: "
                   f"cluster repin LOGIN {node}")
        backends.refuse_backend_name(via)
        if not ui.ask_yes(f"'{session}' runs on {node} (started by {whose}). "
                          f"Open login '{via}' there to attach?", default=True):
            ui.die("not attached; nothing was opened")
        ctx.state.pin_write(via, ctx.backend.fqdn(node))
        ui.info(f"login '{via}' pinned to {node}")
    else:
        ui.info(f"'{session}' runs on {node} (started by {whose}); "
                f"attaching through login '{via}'")
    ctx.by_hand()
    ctx.logins.ensure(via)
    plat.set_process_name(process_label("attach", [via, session]))
    return ctx.logins.interactive(via, ctx.tmux.attach_argv(session),
                                  again=ctx.tmux.reattach_argv(session))


def node_short(ctx, login):
    """Which node a login is on, short form, from local state only.

    The pin is the durable answer, but an *unpinned* login has none, and
    reading that as "" would make every breadcrumb look like it was on a
    different node, so the guard below would refuse a name against the very
    node holding it. The meta record, written whenever a login is seen live,
    is the fallback.
    """
    node = (ctx.logins.node_of(login)
            or ctx.state.read_meta(login).get("node", ""))
    return ctx.backend.short(node) if node else ""


def guard_session_elsewhere(ctx, login, session, crumbs=None):
    """Refuse to create a session whose name is live under another login.

    Two *different* things sharing one name is how work gets lost: the
    second one looks like the first. One thing recorded twice is not that. A
    login is exactly one connection to exactly one node, so a breadcrumb
    naming this login on a node it is not on cannot be live: it is a
    leftover, and refusing over it would print a remedy ("attach to it
    there") that cannot be followed.

    So the record is classified, not merely counted:

    * owned by this login, elsewhere  -> stale record; say so, proceed
    * owned by another existing login -> the real collision; refuse
    * retired tag or a vanished login -> a known orphan; say so, proceed

    ``--here`` overrides everything. *crumbs* lets a caller pass
    breadcrumbs it has already read, so this costs no round trip of its own.
    """
    mine = node_short(ctx, login)
    known = crumbs if crumbs is not None else ctx.tmux.crumbs(login)
    hits = [(node, owner) for (node, name), owner in sorted(known.items())
            if name == session and node != mine]
    if not hits:
        return
    if not mine:
        # Nothing local says where this login is, so "another node" cannot
        # be established. Report rather than refuse: the caller is about to
        # find out for real.
        ui.note(f"session '{session}' is also recorded on "
                f"{', '.join(node for node, _ in hits)}; "
                f"cannot tell whether that is this login's node")
        return
    logins = set(ctx.state.known_logins())
    for node, owner in hits:
        if owner and owner != login and owner in logins:
            ui.die(
                f"session '{session}' is already registered on {node} "
                f"(owned by login '{owner}')",
                f"attach to it there: cluster attach {owner} {session}",
                f"or pass --here to make a second one on {mine}",
            )
    for node, owner in hits:
        if owner == login:
            ui.warn(f"'{session}' is also recorded on {node} for this "
                    f"login, which is on {mine} — a stale record, since a "
                    f"login is only ever on one node")
        else:
            ui.warn(f"'{session}' is also recorded on {node} under "
                    f"'{owner or 'no owner'}', which is not a live login")
    ui.note(f"see what is really there: cluster strays check {hits[0][0]}")


@command("run", "r", help="Run a command through a login (cluster run [LOGIN] -- CMD)")
def cmd_run(ctx, args):
    """Run one command on a login's node and return its exit status.

    Everything after `--` is the command, and LOGIN before it may be any
    name, including a new one. Without `--` the first word is taken as LOGIN
    only when a login of that name exists, the same rule `attach` follows: a
    command word is never made into a login, so `cluster run hostname` is
    refused rather than guessed at.
    """
    if "--" in args:
        split = args.index("--")
        head, remote = args[:split], args[split + 1 :]
    elif args and registry.find(args[0]):
        head, remote = args[:1], args[1:]
    elif args:
        ui.die(f"no login named '{args[0]}'",
               "usage: cluster run [LOGIN] -- COMMAND ...",
               f"to run it through the default login: cluster run -- "
               f"{quote_remote(args)}",
               f"to make '{args[0]}' a login first: cluster login {args[0]}")
    else:
        head, remote = [], []
    if not remote:
        ui.die("nothing to run", "usage: cluster run [LOGIN] -- COMMAND ...")
    name, _ = ctx.resolve_login(head)
    ctx.logins.ensure(name)
    proc = ctx.logins.run_remote(name, quote_remote(remote), timeout=None, capture=False)
    return proc.returncode


@command("shell", "sh", "ssh", help="Interactive shell through a login")
def cmd_shell(ctx, args):
    """With LOGIN, use and retain its managed connection; bare is disposable.

`cluster sh` opens one direct SSH shell with no ControlMaster, pin, mount,
watcher, or retained local login state. `cluster sh LOGIN` deliberately uses
the named managed connection and leaves that connection available afterwards.
"""
    parser = argparse.ArgumentParser(prog="cluster shell", add_help=False)
    parser.add_argument("name", nargs="?")
    parser.add_argument("--no-mount", action="store_true",
                        help="do not mount before a managed shell")
    opts = parser.parse_args(args)
    if opts.name is None:
        if opts.no_mount:
            ui.note("bare `cluster sh` is disposable and never mounts")
        ctx.by_hand()
        return ctx.logins.disposable_interactive()
    name = ctx.login(opts.name)
    ctx.by_hand()
    ctx.logins.ensure(name)
    auto_mount(ctx, name, skip=opts.no_mount)
    return ctx.logins.interactive(name)


@command("sessions", "ss", "tmux-list", help="List remote tmux sessions on a login's node")
def cmd_sessions(ctx, args):
    name, _ = ctx.resolve_login(args)
    from ..diagnostics import sessions

    return sessions(ctx, name)


def default_session(ctx, login, sessions=None):
    """Which session `cluster attach LOGIN` means when none is named.

    A login is an SSH connection; sessions are tmux on its node, and there may be
    none, one, or many — nothing guarantees one exists, and naming it after the
    login is only a convention. Following that convention blindly would be a
    trap: a login whose work lives under another name (say `work` holding a
    session `api`) would get a *second, empty* session created next to it.

    So: the convention if it is there, the only session if there is exactly one,
    and otherwise ask — never guess between several.

    *sessions* lets a caller that has already listed them pass the rows in rather
    than pay for a second round trip. This decision stays here, in Python, on
    purpose: pushing it into the remote shell would make `attach` a single
    round trip instead of two, at the price of reimplementing these three cases
    and their two distinct refusals in a place that is far harder to test.
    """
    rows = sessions if sessions is not None else ctx.tmux.list_sessions(login)
    live = [row.name for row in rows if not row.foreign]
    if not live:
        # Create one named after the login. Say so: the alternative a user often
        # wants here is a plain shell, and that is a different verb on purpose —
        # a raw ssh shell carries no ownership tag and no breadcrumb, so nothing
        # can see it, and it dies with the connection instead of surviving it.
        ui.info(f"login '{login}' has no sessions; starting one called '{login}'")
        ui.note(f"for a throwaway shell with no tmux: cluster shell {login}")
        return login
    if login in live:
        return login
    if len(live) == 1:
        ui.info(f"login '{login}' has one session, '{live[0]}'")
        return live[0]
    ui.die(f"login '{login}' has {len(live)} sessions: {', '.join(sorted(live))}",
           f"none is named '{login}', so there is nothing to default to",
           f"say which one: cluster attach {login} SESSION")


@command("attach", "a", "tmux", help="Attach to a remote tmux session")
def cmd_attach(ctx, args):
    parser = argparse.ArgumentParser(prog="cluster attach", add_help=False)
    parser.add_argument("--here", action="store_true",
                        help="allow a session name registered on another node")
    parser.add_argument("--no-mount", action="store_true",
                        help="do not mount this login's home first")
    parser.add_argument("names", nargs="*")
    opts = parser.parse_args(args)

    names = opts.names
    login = ctx.login(names[0] if names else None)
    named_session = len(names) > 1
    session = names[1] if named_session else login
    tmuxlayer.require_name(login, "login name")
    tmuxlayer.require_name(session, "session name")

    # attach never creates a login: a typo must not silently spawn one. A
    # session another workstation started is the exception, offered (asked)
    # once its record is found.
    if login not in ctx.state.known_logins():
        if not named_session:
            for candidate in ctx.logins.active_names():
                away = recorded_elsewhere(ctx, login, ctx.tmux.crumbs(candidate))
                if away:
                    return attach_elsewhere(ctx, login, *away)
                break
        hint = session_hint(ctx, login)
        ui.die(
            f"no login named '{login}'",
            *( [f"'{login}' looks like a session on {hint}; use: "
                f"cluster attach LOGIN {login}"] if hint else
               [f"create it first: cluster login {login}"] ),
        )

    ctx.by_hand()
    ctx.logins.ensure(login)
    auto_mount(ctx, login, skip=opts.no_mount)

    # Two round trips, not five. A channel costs the same whatever it carries —
    # measured, a bare `true` and a full session listing both take about the same
    # as sshd's per-session setup dominates — so what `attach` costs is how many
    # channels it opens and tears down. Everything it must *know* travels in one
    # reply, and everything it must *do* in one command.
    #
    # It stops at two rather than one because the session name is not known until
    # the listing has been read: default_session() picks between the
    # login-named session, the only session, and refusing to guess. That decision
    # stays local.
    need_reads = (not named_session) or (not opts.here)
    if need_reads:
        complete, sessions, crumbs = ctx.tmux.sessions_and_crumbs_checked(login)
        if not complete:
            ui.die(f"could not catalogue tmux sessions through '{login}'",
                   "nothing was created or attached; retry when the connection is healthy")
    else:
        sessions, crumbs = [], {}
    if not named_session:
        session = default_session(ctx, login, sessions=sessions)
    # Relabel now that the defaults are resolved: `cluster attach` with no
    # arguments could not be named before we knew what it would attach to.
    plat.set_process_name(process_label("attach", [login, session]))

    if not opts.here and session not in {row.name for row in sessions}:
        # Not here, and someone else's elsewhere: that is the session meant,
        # not a new empty one under its name on this node.
        away = recorded_elsewhere(ctx, session, crumbs,
                                  away_from=node_short(ctx, login))
        if away:
            return attach_elsewhere(ctx, session, *away)
    if not opts.here:
        guard_session_elsewhere(ctx, login, session, crumbs=crumbs)
    steps = ctx.tmux.register_session(login, session, owner=login)
    # One exit status for three writes would make a failure unattributable, so
    # each is reported. None is fatal: the session may well still be attachable,
    # and an untagged or uncrumbed session is degraded rather than broken — but a
    # missing breadcrumb is how a session becomes invisible to `clean`, so it
    # must never fail silently.
    ctx.tmux.warn_unrecorded(login, session, steps)
    return ctx.logins.interactive(login, ctx.tmux.attach_argv(session, create=True),
                                  again=ctx.tmux.reattach_argv(session))


@command("new", "n", help="Open tmux/shell with one name; two names always mean tmux")
def cmd_new(ctx, args):
    """The convenient front door for interactive work.

    One name creates/reuses that managed connection, then follows
    NEW_LOGIN_MODE: by default `cluster n main` creates or attaches tmux session
    `main`; setting it to `shell` opens a non-durable shell over the still-managed
    connection. Two names are never ambiguous and always mean a durable tmux
    session: `cluster n main api`.
    """
    head, remote = _split_double_dash(args)
    opts = _task_parser("cluster new").parse_args(head)
    names = opts.names

    if not names:
        ui.die("need a name",
               "one name opens its configured tmux/shell: cluster new work",
               "two always create a tmux session:          cluster new work api",
               "a disposable connection with no retained state: cluster sh")
    if len(names) > 1:
        return cmd_task(ctx, args)

    login = ctx.login(names[0])
    mode = ctx.settings.str("NEW_LOGIN_MODE").lower()
    if mode == "shell":
        if opts.detach or opts.here or opts.cwd or remote:
            ui.die("tmux options and remote commands need an explicit session name",
                   f"use: cluster new {login} SESSION"
                   + (" -- COMMAND" if remote else ""))
        return cmd_shell(ctx, [login] + (["--no-mount"] if opts.no_mount else []))

    # A one-name tmux form means the conventional same-named session. Explicitly
    # passing both names keeps cmd_task's invariant intact and, importantly,
    # makes `cluster n work work` take this exact same always-tmux path.
    expanded = []
    if opts.detach:
        expanded.append("--detach")
    if opts.here:
        expanded.append("--here")
    if opts.no_mount:
        expanded.append("--no-mount")
    if opts.cwd:
        expanded += ["--cwd", opts.cwd]
    expanded += [login, login]
    if remote:
        expanded += ["--"] + remote
    return cmd_task(ctx, expanded)


def _task_parser(prog):
    parser = argparse.ArgumentParser(prog=prog, add_help=False)
    parser.add_argument("-d", "--detach", action="store_true",
                        help="start it running and do not attach")
    parser.add_argument("--here", action="store_true",
                        help="allow a session name already registered on another node")
    parser.add_argument("--no-mount", action="store_true",
                        help="do not mount this login's home first")
    parser.add_argument("-c", "--cwd",
                        help="start the session in this remote directory")
    parser.add_argument("names", nargs="*")
    return parser


@command("new-session", "task", "session",
         help="Create a tmux session on a login (needs LOGIN and SESSION)")
def cmd_task(ctx, args):
    head, remote = _split_double_dash(args)
    opts = _task_parser("cluster new-session").parse_args(head)

    # Both names are required: a session belongs to a login, and defaulting the
    # login is how a session ends up running on the wrong node.
    if len(opts.names) < 2:
        ui.die("a session needs both a login and a session name",
               "usage: cluster new-session LOGIN SESSION [-- COMMAND]",
               *([f"did you mean a new connection? cluster new {opts.names[0]}"]
                 if len(opts.names) == 1 else []))
    login, session = ctx.login(opts.names[0]), opts.names[1]
    tmuxlayer.require_name(session, "session name")
    plat.set_process_name(process_label("attach", [login, session]))

    if not opts.detach:
        ctx.by_hand()
    ctx.logins.ensure(login)
    auto_mount(ctx, login, skip=opts.no_mount)

    # Whether the session exists and, unless --here, where else its name is
    # recorded: one round trip for both, as attach reads them.
    sessions, crumbs = ((None, None) if opts.here
                        else ctx.tmux.sessions_and_crumbs(login))
    exists = ctx.tmux.session_exists(login, session, sessions=sessions)
    if exists and remote:
        ui.die(f"session '{session}' already exists on {login}",
               "attach to it instead: cluster attach " f"{login} {session}")
    if not exists:
        if not opts.here:
            guard_session_elsewhere(ctx, login, session, crumbs=crumbs)
        command_line = quote_remote(remote) if remote else None
        ctx.tmux.create(login, session, command=command_line, cwd=opts.cwd)
        ui.info(f"created session '{session}' on {login}")

    # Attach when there is a terminal to attach to, unless told to detach.
    if opts.detach or not plat.terminal_attached():
        if not opts.detach:
            ui.info("no terminal attached; leaving the session detached")
        return 0
    return ctx.logins.interactive(login, ctx.tmux.attach_argv(session),
                                  again=ctx.tmux.reattach_argv(session))


@command("window", "new-window", help="Create a tmux window in a session")
def cmd_window(ctx, args):
    head, remote = _split_double_dash(args)
    if len(head) < 2:
        ui.die("need a session and a window name",
               "usage: cluster window [LOGIN] SESSION WINDOW [-- COMMAND]")
    if len(head) >= 3:
        login, session, window = ctx.login(head[0]), head[1], head[2]
    else:
        login, session, window = ctx.login(), head[0], head[1]
    ctx.logins.ensure(login)
    command_line = quote_remote(remote) if remote else None
    if not ctx.tmux.new_window(login, session, window, command=command_line):
        ui.die(f"could not create window '{window}' in session '{session}'")
    ui.info(f"created window '{window}' in '{session}' on {login}")
    return 0


@command("send", help="Send a command line to a tmux target")
def cmd_send(ctx, args):
    """Type a command line into a tmux pane, as if at its prompt, and press Enter.

    One argument after `--` is typed exactly as given, so it can carry the
    pane shell's own syntax: `cluster send api -- 'make && make test'`.
    Several are one command and its arguments, each quoted for that shell,
    so `cluster send api -- grep -r "two words" .` searches for both words.
    """
    head, remote = _split_double_dash(args)
    if not head or not remote:
        ui.die("need a target and a command",
               "usage: cluster send [LOGIN] TARGET -- COMMAND ...")
    if len(head) >= 2:
        login, target = ctx.login(head[0]), head[1]
    else:
        login, target = ctx.login(), head[0]
    ctx.logins.ensure(login)
    line = remote[0] if len(remote) == 1 else quote_remote(remote)
    if not ctx.tmux.send(login, target, line):
        ui.die(f"could not send to '{target}'")
    return 0


@command("kill-session", "k", help="Kill a remote tmux session")
def cmd_kill_session(ctx, args):
    if not args:
        ui.die("need a session name", "usage: cluster kill-session [LOGIN] SESSION")
    if len(args) >= 2:
        login, session = ctx.login(args[0]), args[1]
    else:
        login, session = ctx.login(), args[0]
    ctx.logins.ensure(login)
    gone, killed, remaining = ctx.tmux.kill_session_checked(login, session)
    if not gone:
        ui.die(f"could not confirm session '{session}' was killed",
               "its records were left in place so the work stays findable")
    if not killed:
        import difflib

        # The name is matched exactly. tmux alone would take a unique prefix,
        # so `cluster k ap` would kill `api` and report a kill of `ap`: say
        # what was probably meant rather than claim a kill.
        near = ([name for name in remaining if name.startswith(session)]
                or difflib.get_close_matches(session, remaining, n=3))
        ui.die(f"no session '{session}' on {login}; nothing was killed",
               *([f"did you mean: cluster kill-session {login} {name}"
                  for name in near]
                 or [f"sessions there: {', '.join(sorted(remaining))}"
                     if remaining else f"{login} has no sessions"]))
    ui.info(f"killed session '{session}' on {login}")
    return 0


@command("rescue", help="One-off attach to a session on a specific node (no state)")
def cmd_rescue(ctx, args):
    if not args:
        ui.die("need a node", "usage: cluster rescue NODE [SESSION]")
    node = ctx.backend.fqdn(args[0])
    session = (tmuxlayer.require_name(args[1], "session name")
               if len(args) > 1 else None)
    ctx.by_hand()
    ctx.backend.ensure_credential()
    remote = (f"tmux attach-session -t {tmux_target(session)}" if session
              else "tmux list-sessions")
    argv = ctx.backend.ssh_argv(node=node, extra=["-t"], remote=remote)
    ui.info(f"rescue: {ctx.backend.short(node)}"
            + (f" session '{session}'" if session else " (listing sessions)"))
    return ctx.logins.exec_directly(argv, ctx.backend.short(node))


@command("restore-layout", "restore", help="Rebuild sessions/windows/cwds from a snapshot")
def cmd_restore_layout(ctx, args):
    if not args:
        ui.die("need the node whose snapshot should be restored",
               "usage: cluster restore-layout [LOGIN] NODE")
    # The login first: resolving it binds the backend that owns it, and only
    # that backend knows what the node's short name stands for.
    if len(args) >= 2:
        login = ctx.login(args[0])
        node = ctx.backend.fqdn(args[1])
    else:
        login = ctx.login()
        node = ctx.backend.fqdn(args[0])
    ctx.logins.ensure(login)
    entries = ctx.tmux.layout_read(login, node)

    # Breadcrumbs are the floor under the snapshot. A snapshot is only as
    # fresh as the watcher's last save, so a session created and killed
    # between two ticks appears in no snapshot at all — while its crumb was
    # written in the very round trip that created it. Names are the most that
    # survives either way (processes are never restored), so a crumb with no
    # snapshot entry is still worth a bare session under the right name.
    short = ctx.backend.short(node)
    known = {entry["session"] for entry in entries}
    from_crumbs = sorted(
        session for (crumb_node, session), owner in
        ctx.tmux.crumbs(login).items()
        if crumb_node == short and owner == login and session not in known)
    entries = list(entries) + [
        {"session": session, "window": 0, "path": "", "name": ""}
        for session in from_crumbs]
    if not entries:
        ui.die(f"nothing recorded for {short}",
               "no layout snapshot and no breadcrumbs naming a session there")
    if from_crumbs:
        ui.note(f"{len(from_crumbs)} session(s) known only from breadcrumbs, "
                f"so without windows or cwds: {', '.join(from_crumbs)}")

    existing = {row.name for row in ctx.tmux.list_sessions(login)}
    made_sessions, made_windows = 0, 0
    seen_windows = set()
    for entry in entries:
        session, window, path = entry["session"], entry["window"], entry["path"]
        if session in existing:
            continue
        if session not in seen_windows:
            ctx.tmux.create(login, session, cwd=path or None)
            made_sessions += 1
            seen_windows.add(session)
            continue
        key = (session, window)
        if key in seen_windows:
            continue
        seen_windows.add(key)
        if ctx.tmux.new_window(login, session, entry["name"] or f"w{window}"):
            made_windows += 1
    ui.info(f"restored {made_sessions} session(s) and {made_windows} window(s); "
            "processes are not restarted")
    return 0


@command("close", "logout", help="Close a login (kills its owned tmux sessions)")
def cmd_close(ctx, args):
    parser = argparse.ArgumentParser(prog="cluster close", add_help=False)
    parser.add_argument("name", nargs="?")
    parser.add_argument("--keep-tmux", action="store_true",
                        help="leave this login's tmux sessions running")
    parser.add_argument("--abandon-tmux", action="store_true",
                        help="leave them running and record them for later cleaning")
    parser.add_argument("--all", action="store_true",
                        help="every login, on every backend unless one is named")
    opts = parser.parse_args(args)
    if opts.keep_tmux and opts.abandon_tmux:
        ui.die("--keep-tmux and --abandon-tmux are different dispositions",
               "--keep-tmux retains the pin; --abandon-tmux records sessions and releases it")

    if opts.all:
        # Same scoping rule as everywhere else: an explicit backend means "just
        # this one", no backend means every one. Leaving --all backend-local
        # under global names would be the surprising reading.
        targets = ctx.scope_all()
    else:
        targets = [(ctx, ctx.login(opts.name))]

    return _close_logins(targets, keep_tmux=opts.keep_tmux,
                         abandon_tmux=opts.abandon_tmux)


@command("refresh", "reconnect", help="Move a login to a fresh node")
def cmd_refresh(ctx, args):
    parser = argparse.ArgumentParser(prog="cluster refresh", add_help=False)
    parser.add_argument("name", nargs="?")
    parser.add_argument("--keep-tmux", action="store_true",
                        help="leave this login's tmux sessions running")
    opts = parser.parse_args(args)
    name = ctx.login(opts.name)
    ctx.by_hand()
    return _refresh_login(ctx, name, keep_tmux=opts.keep_tmux)


@command("pin", "pins", help="Show which node each login is pinned to")
def cmd_pin(ctx, args):
    names = [args[0]] if args else ctx.state.known_logins()
    rows = []
    for name in names:
        pinned = ctx.state.pin_read(name)
        live = ctx.logins.live_node(name) if ctx.logins.is_active(name) else ""
        flag = ""
        if pinned and live and ctx.backend.short(pinned) != ctx.backend.short(live):
            flag = "  <- live node differs from the pin"
        rows.append([name, ctx.backend.short(pinned) or "-",
                     ctx.backend.short(live) or "-", flag])
    if not rows:
        ui.say("no logins")
        return 0
    ui.table(rows, ["LOGIN", "PINNED", "LIVE", ""])
    return 0


def _move_parser(prog):
    parser = argparse.ArgumentParser(prog=prog, add_help=False)
    parser.add_argument("names", nargs="*")
    parser.add_argument("--migrate", action="store_true",
                        help="snapshot the session layout so it can be rebuilt after the move")
    parser.add_argument("--abandon", action="store_true",
                        help="leave the old sessions running, and record them for cleaning")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="do not ask for confirmation")
    return parser


@command("repin", "move", help="Pin a login to a specific node, moving its sessions")
def cmd_repin(ctx, args):
    opts = _move_parser("cluster repin").parse_args(args)
    if not opts.names:
        ui.die("need a node", "usage: cluster repin [LOGIN] NODE")
    if len(opts.names) >= 2:
        name, node = ctx.login(opts.names[0]), ctx.backend.fqdn(opts.names[1])
    else:
        name, node = ctx.login(), ctx.backend.fqdn(opts.names[0])
    ctx.by_hand()

    holder = ctx.state.login_pinned_to(node, exclude=name, short=ctx.backend.short)
    if holder and ctx.settings.flag("ONE_LOGIN_PER_NODE"):
        ui.die(
            f"{ctx.backend.short(node)} is already login '{holder}'s node",
            "sessions are node-local: two logins on one node list each other's "
            "tmux sessions",
            "pick another node, or set CLUSTER_ONE_LOGIN_PER_NODE=0 to share",
        )

    old = ctx.state.pin_read(name)
    moving = bool(old) and old != node
    rc = _release_sessions(ctx, name, old, opts, "repinning") if moving else 0

    if ctx.logins.is_active(name):
        ctx.mounts.unmount(name, quiet=True)
        # keep_tmux: whatever is still running there was decided above, and this
        # close must not kill sessions a second time.
        ctx.logins.close(name, keep_tmux=True, keep_pin=True, quiet=True)
    # Nothing exists on the new node yet, so no local record may claim otherwise.
    ctx.state.mountnode_clear(name)
    ctx.state.pin_write(name, node)
    ui.info(f"login '{name}' pinned to {ctx.backend.short(node)}"
            + (f" (was {ctx.backend.short(old)})" if moving else ""))

    if moving and opts.migrate:
        # Reopening costs an authentication on FASRC, which is the price of
        # carrying the layout across; the snapshot is keyed by the old node.
        ctx.logins.ensure(name)
        return cmd_restore_layout(ctx, [name, old]) or rc
    return rc


@command("unpin", help="Release a login's pin")
def cmd_unpin(ctx, args):
    opts = _move_parser("cluster unpin").parse_args(args)
    name = ctx.login(opts.names[0] if opts.names else None)
    old = ctx.state.pin_read(name)
    if not old:
        ui.say(f"login '{name}' has no pin")
        return 0
    # An unpinned login takes a fresh node next time, so its sessions are just as
    # stranded as by a repin — except there is no new node to migrate them to.
    if opts.migrate:
        ui.die("--migrate needs a destination", f"use: cluster repin {name} NODE --migrate")
    rc = _release_sessions(ctx, name, old, opts, "unpinning")
    if ctx.logins.is_active(name):
        ctx.logins.close(name, keep_tmux=True, keep_pin=True, quiet=True)
    ctx.state.mountnode_clear(name)
    ctx.state.pin_clear(name)
    ui.info(f"released the pin on {ctx.backend.short(old)}")
    return rc
