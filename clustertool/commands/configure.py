"""Settings, credentials, and the backends they apply to."""

from __future__ import annotations

import argparse

from .. import backends, config, ui
from ..command import command


@command(
    "config", "cfg", needs_context=False,
    help="Show or change settings and credentials without touching live connections",
    options=(("--all", "include built-in values"),
             ("--global", "write globally even when a backend was named")),
)
def cmd_config(invocation, args):
    """Usage: cluster [--BACKEND] config [show|list|get|set|unset|path|credentials]

`show` lists the settings that are set, and each cluster's credentials;
`list` lists every setting. `set KEY VALUE` writes a persistent setting to
~/.config/cluster/settings.ini: to [global], or with a backend flag to that
backend's section (`cluster --nersc config set MAX_LOGINS 3`). A setting only
one backend reads goes to its section by itself. CLUSTER_KEY and
CLUSTER_BACKEND_KEY in the environment override them for one command.

`credentials` asks for a cluster's username, password and TOTP seed (or its
otpauth:// link), checks the code it makes, and saves them together in
~/.config/cluster/credentials/BACKEND/. Answers may be piped in, one per
line. `set username NAME`, `set password` and `set totp` change one of them;
a password or seed is typed at the prompt, never given on the command line,
and never displayed.
"""
    from ..configcmd import run

    return run(invocation, args)


@command("backends", needs_context=False,
         help="List the backends and their credential state; add or remove one")
def cmd_backends(invocation, args):
    """Usage: cluster backends [add NAME [HOST] [--type TYPE] [--label TEXT] | remove NAME]

Every backend the tool knows, including ones not set up yet: the built-in
fasrc and nersc, and each profile of the settings file. `add` makes a profile
of any host ssh reaches, a section of settings.ini like

    [lab]
    TYPE = ssh
    HOST = lab-login

where HOST is a Host of your ssh config, a hostname or user@host. It is then
named with --backend NAME. TYPE is ssh unless --type names another: the path
of a Python file defining one (docs/backends.md). `remove` deletes the section
of a backend that has no logins.
"""
    if args and args[0] == "add":
        return _add_backend(_add_parser().parse_args(args[1:]))
    if args and args[0] in ("remove", "rm"):
        return _remove_backend(args[1:])
    if args:
        ui.die(f"unknown backends subcommand '{args[0]}'",
               "usage: cluster backends [add NAME [HOST] | remove NAME]")
    return _list_backends()


def _list_backends():
    """The table. Needs no backend of its own, so it answers on a machine
    where none is configured — which is exactly when someone asks it what
    there is. A credential the cluster refused shows as refused
    (state.Refusals), read where it is kept, so listing makes no state."""
    from ..state import Refusals

    rows = []
    for name, cls in sorted(backends.BACKENDS.items()):
        try:
            backend = cls(config.Settings(name))
        except backends.BackendUnavailable:
            rows.append([name, cls.type_name, cls.label, "-", "missing",
                         f"not set up: {cls.setup_command()}"])
            continue
        state, detail = backend.credential_state()
        refused = Refusals(backend, config.STATE_ROOT / name).status()
        if refused:
            state, detail = "refused", refused
        rows.append([backend.name, cls.type_name, backend.label,
                     backend.user or "-", state, detail])
    ui.table(rows, ["BACKEND", "TYPE", "CLUSTER", "USER", "CRED", "DETAIL"])
    if all(cls.shorthand for cls in backends.BACKENDS.values()):
        ui.note("any other host ssh reaches: cluster backends add NAME HOST")
    return 0


def _add_parser():
    parser = argparse.ArgumentParser(prog="cluster backends add")
    parser.add_argument("name")
    parser.add_argument("host", nargs="?", default="",
                        help="a Host of your ssh config, a hostname, or user@host")
    parser.add_argument("--type", default="ssh", dest="kind", metavar="TYPE",
                        help="add: ssh, or the path of a Python file defining a type")
    parser.add_argument("--label", default="", help="add: the name shown for it")
    return parser


def _add_backend(opts):
    name = opts.name
    if name in backends.BACKENDS:
        ui.die(f"backend '{name}' already exists",
               f"change it with: cluster {backends.flag(name)} config set KEY VALUE")
    problem = backends.profile_name_problem(name, list(backends.BACKENDS))
    if problem:
        ui.die(f"'{name}' cannot name a backend: {problem}")
    try:
        kind = backends._type(opts.kind)
    except ValueError as exc:
        ui.die(str(exc))
    values = [("HOST", opts.host), ("LABEL", opts.label)]
    for key, value in values:
        if not value:
            continue
        if key not in kind.SETTINGS:
            ui.die(f"a {kind.type_name} backend has no {key}")
        try:
            kind.SETTINGS[key].parse(value)
        except ValueError as exc:
            ui.die(f"{key} {value!r}: {exc}")
    where = config.SETTINGS_FILE
    try:
        config.write_entry(name, "TYPE", opts.kind.strip())
        for key, value in values:
            if value:
                config.write_value(key, value, backend=name)
    except ValueError as exc:
        ui.die(str(exc), "the malformed file was left untouched")
    ui.info(f"added backend '{name}' ({kind.type_name}) as [{name}] of {where}")
    cls = backends.BACKENDS[name]
    if not cls.is_configured(config.Settings(name)):
        ui.note(f"it is not set up yet: {cls.setup_command()}")
        return 0
    from ..configcmd import say_steps

    say_steps(list(cls.setup_steps()) + [
        (f"cluster {cls.cli_flag()} new work",
         "open a connection and a tmux session called work")])
    return 0


def _remove_backend(args):
    from .. import registry

    if len(args) != 1:
        ui.die("usage: cluster backends remove NAME")
    name = args[0]
    cls = backends.BACKENDS.get(name)
    if cls is None:
        ui.die(f"no backend '{name}'")
    if cls.shorthand:
        ui.die(f"'{name}' is built in; there is nothing to remove",
               f"its settings go with: cluster {cls.cli_flag()} config unset KEY")
    logins = registry.logins_of(name)
    if logins:
        ui.die(f"backend '{name}' has logins: {', '.join(logins)}",
               "their sessions would be left where nothing looks at them",
               f"close them first: cluster {cls.cli_flag()} close --all")
    try:
        for section, entry in config.file_entries():
            if section == name:
                config.remove_entry(section, entry)
    except ValueError as exc:
        ui.die(str(exc), "the malformed file was left untouched")
    ui.info(f"removed backend '{name}' from {config.SETTINGS_FILE}")
    state = config.STATE_ROOT / name
    if state.exists():
        ui.note(f"its logs and records stay in {state}")
    return 0


@command("nodes", needs_context=False,
         help="Show this backend's node classes and what they are for")
def cmd_nodes(invocation, args):
    name = backends.resolve_name(invocation.backend_name) or backends.default_name()
    backend = backends.BACKENDS[name]
    node_classes = backend.configured_node_classes(config.Settings(name))
    rows = []
    for node_class in node_classes:
        members = node_class.members()
        shown = ", ".join(backend.short(m) for m in members[:3])
        if len(members) > 3:
            shown += f", … ({len(members)} total)"
        rows.append([
            node_class.name,
            "direct" if node_class.routable else "via pool",
            ",".join(sorted(node_class.purposes)),
            shown,
        ])
    if rows:
        ui.table(rows, ["CLASS", "REACH", "PURPOSES", "MEMBERS"])
    else:
        ui.say("no node list: a login is pinned to the node its connection "
               "lands on")
    for node_class in node_classes:
        if node_class.note:
            ui.say(f"\n{node_class.name}: {node_class.note}")
    try:
        pool = backend(config.Settings(name)).pool_host
    except backends.BackendUnavailable:
        pool = backend.pool_host
    ui.say(f"\npool address: {pool or '-'}")
    return 0


@command("auth", "cert", help="Obtain or renew the cluster credential")
def cmd_auth(ctx, args):
    parser = argparse.ArgumentParser(prog="cluster auth", add_help=False)
    parser.add_argument("--force", action="store_true",
                        help="renew even if the current credential is still good")
    parser.add_argument("--drop", action="store_true", help="delete the cached credential")
    parser.add_argument("--status", action="store_true", help="report and exit")
    opts = parser.parse_args(args)

    if opts.drop:
        if ctx.backend.drop_credential():
            ui.info(f"dropped the cached {ctx.backend.label} credential")
        else:
            ui.info("nothing cached to drop")
        return 0
    state, detail = ctx.backend.credential_state()
    if opts.status:
        ui.say(f"{ctx.backend.name}: {state} — {detail}")
        return 0 if state != "missing" else 1
    ctx.by_hand()
    ctx.backend.ensure_credential(force=opts.force)
    state, detail = ctx.backend.credential_state()
    ui.say(f"{ctx.backend.name}: {state} — {detail}")
    return 0


@command("bridge", help="Keep the NERSC credential and the companion fresh on a hub")
def cmd_bridge(ctx, args):
    """Usage: cluster bridge push|status [LOGIN]

``push`` fetches a NERSC certificate here if the current one is too old,
copies it and the ``nersc`` companion to the hub through LOGIN (a login of
any non-NERSC cluster, opened if need be; default: the default login), and
checks that ``nersc run true`` works there. ``status`` reports the
certificate here, the last push, and what the hub has when that login is
connected; it changes nothing.
"""
    from .. import bridge

    parser = argparse.ArgumentParser(prog="cluster bridge", add_help=False)
    parser.add_argument("action", choices=["push", "status"],
                        help="push: install/refresh on the hub; status: report")
    parser.add_argument("login", nargs="?",
                        help="hub login to push through (default: the default login)")
    parser.add_argument("--force", action="store_true",
                        help="fetch a fresh certificate even if the current one is young")
    parser.add_argument("--cron", action="store_true",
                        help="unattended: skip if a push is running; fetch the "
                             "certificate once; try other logins, but stop at "
                             "a refused credential")
    parser.add_argument("--no-verify", action="store_true",
                        help="skip the end-to-end `nersc run true` check after pushing")
    parser.add_argument("--overwrite-tool", action="store_true",
                        help="replace an edited companion on the hub with this "
                             "machine's source (a copy of the hub's is kept)")
    opts = parser.parse_args(args)
    if opts.action == "status":
        return bridge.status(ctx, opts.login)
    from .transfers import need_rsync

    need_rsync("cluster bridge push")
    if opts.cron:
        if opts.overwrite_tool:
            ui.die("--overwrite-tool is not allowed with --cron",
                   "the unattended push must never discard an agent's edit")
        return bridge.push_cron(ctx, force=opts.force)
    return bridge.push(ctx, opts.login, force=opts.force, verify=not opts.no_verify,
                       overwrite_tool=opts.overwrite_tool)
