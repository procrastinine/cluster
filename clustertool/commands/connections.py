"""Opening a login, and looking at the connections that exist."""

from __future__ import annotations

import argparse

from .. import ui
from ..command import command


@command("login", "l", "open", help="Open or reuse a named login (pinned to its node)")
def cmd_login(ctx, args):
    parser = argparse.ArgumentParser(prog="cluster login", add_help=False)
    parser.add_argument("name", nargs="?")
    parser.add_argument("--no-mount", action="store_true",
                        help="do not mount this backend's home after connecting")
    opts = parser.parse_args(args)
    from .mounts import auto_mount

    name = ctx.login(opts.name)
    ctx.by_hand()
    ctx.logins.ensure(name)
    auto_mount(ctx, name, skip=opts.no_mount)
    node = ctx.logins.node_of(name)
    ui.info(f"login '{name}' active on {ctx.backend.short(node) or 'unknown node'}")
    return 0


@command("list", "ls", help="List every connection and its sessions, all backends")
def cmd_list(ctx, args):
    """The unified view: all backends, every connection, with its sessions.

    Deliberately never authenticates. Sessions are read only for logins whose
    control master is already up, because `ls` is what you type to find out
    where things are — it must not cost a TOTP window to look.

    It does still talk to every live node — a pin says where a login *should*
    be, only the node itself says where it *is* — so the cost is one round trip
    per login, taken concurrently.
    """
    parser = argparse.ArgumentParser(prog="cluster ls", add_help=False)
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="connections only, no sessions")
    opts = parser.parse_args(args)
    from ..listing import run, say_nothing_set_up

    if ctx.nothing_set_up():
        return say_nothing_set_up()
    return run(ctx, quiet=opts.quiet)


@command("where", "w", "node", help="Show a login's node and remote sessions")
def cmd_where(ctx, args):
    name, _ = ctx.resolve_login(args)
    from ..diagnostics import where

    return where(ctx, name)


@command("channels", "ch", help="Show how much of a login's SSH channel budget is free")
def cmd_channels(ctx, args):
    """Account for the one resource nothing else reports.

    A login is a single SSH connection, and sshd caps it at MaxSessions channels
    (10 on FASRC). Everything riding it competes for those: each open `attach`,
    the sshfs mount, every rclone sftp connection. Overrun is reported by sshd as
    "channel N: open failed: connect failed: open failed", which names neither
    the limit nor what filled it, so a tool that wants to size its concurrency
    has nowhere to ask. `--free` is that answer, as a bare number for scripts.

    Reads local process state only: no channel is spent to count the channels.
    """
    parser = argparse.ArgumentParser(prog="cluster channels", add_help=False)
    parser.add_argument("login", nargs="?")
    parser.add_argument("--free", action="store_true",
                        help="print only the number still openable")
    opts = parser.parse_args(args)

    name = ctx.login(opts.login)
    if name not in ctx.state.known_logins():
        ui.die(f"no login named '{name}'")
    limit = ctx.settings.int("SSH_MAX_SESSIONS")
    held = ctx.logins.channel_clients(name)
    free = ctx.logins.channels_free(name)

    if opts.free:
        ui.say(str(free))
        return 0

    if not ctx.logins.is_active(name):
        ui.say(f"{name}: not connected")
        return 1
    ui.say(f"{name}: {len(held)} of ~{limit} channels in use, {free} free")
    for kind in sorted({k for _, k in held}):
        pids = [str(p) for p, k in held if k == kind]
        ui.say(f"  {len(pids):>2} x {kind:<24} pid {' '.join(pids)}")
    if not held:
        ui.say("  (nothing riding it)")
    if free == 0:
        ui.warn("no channels left; the next one will be refused by the server")
        ui.note("close an attach you are not using, or wait for a transfer")
    return 0
