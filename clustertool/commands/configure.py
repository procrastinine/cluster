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
         help="List known cluster backends and credential state")
def cmd_backends(invocation, args):
    """Every backend the tool knows, including ones not set up yet.

    Needs no backend of its own, so it answers on a machine where none is
    configured — which is exactly when someone asks it what there is. A
    credential the cluster refused shows as refused (state.Refusals), read
    where it is kept, so listing makes no state.
    """
    from ..state import Refusals

    rows = []
    for name, cls in sorted(backends.BACKENDS.items()):
        try:
            backend = cls(config.Settings(name))
        except backends.BackendUnavailable:
            rows.append([name, cls.label, "-", "missing",
                         f"not set up: cluster --{name} config credentials"])
            continue
        state, detail = backend.credential_state()
        refused = Refusals(backend, config.STATE_ROOT / name).status()
        if refused:
            state, detail = "refused", refused
        rows.append([backend.name, backend.label, backend.user, state, detail])
    ui.table(rows, ["BACKEND", "CLUSTER", "USER", "CRED", "DETAIL"])
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
    ui.table(rows, ["CLASS", "REACH", "PURPOSES", "MEMBERS"])
    for node_class in node_classes:
        if node_class.note:
            ui.say(f"\n{node_class.name}: {node_class.note}")
    ui.say(f"\npool address: {backend.pool_host}")
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
