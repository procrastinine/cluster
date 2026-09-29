"""The editor and tmux integration: local VS Code, and one cluster home."""

from __future__ import annotations

import argparse

from .. import setup
from ..command import command


@command("setup", needs_context=False,
         help="Editor and tmux integration (setting up this machine is `cluster init`)")
def cmd_setup(invocation, args):
    """Usage: cluster setup [LOGIN] [--fasrc|--nersc] [OPTIONS]

With no scope switch, setup keeps VS Code from watching the mounts, in each
VS Code installed here (the VS Code Server's machine settings, and the
desktop editor's user settings; VSCODE_SETTINGS names another file), with the
terminal tab title too when VSCODE_TAB_TITLE is on. It also merges the
selected cluster's remote tmux compatibility block.  With
SETUP_SYNC_NERSC_TOOL on, setup of a hub login also installs the NERSC
companion there; an edit made to it on the hub is never overwritten.
Existing configuration is preserved and backed up; live tmux servers are
sourced, never restarted.

Setting this machine up in the first place (credentials, and which optional
parts work here) is `cluster init`.
"""
    parser = argparse.ArgumentParser(prog="cluster setup", add_help=False)
    parser.add_argument("login", nargs="?")
    parser.add_argument("--check", action="store_true",
                        help="report drift without writing local or remote files")
    parser.add_argument("--local-only", action="store_true",
                        help="configure/check only VS Code on this machine")
    parser.add_argument("--remote-only", action="store_true",
                        help="configure/check only the selected cluster home")
    parser.add_argument("--no-tool", action="store_true",
                        help="do not install the NERSC companion on a hub, "
                             "even with SETUP_SYNC_NERSC_TOOL on")
    opts = parser.parse_args(args)
    if opts.local_only and opts.remote_only:
        parser.error("--local-only and --remote-only cannot be combined")
    if opts.local_only and opts.login:
        parser.error("--local-only does not take a login")
    if opts.local_only and opts.no_tool:
        parser.error("--no-tool has no effect with --local-only")

    ready = True
    if not opts.remote_only:
        ready = setup.configure_vscode(check=opts.check) and ready

    if not opts.local_only:
        # Constructing Context may load credentials, so local-only stays useful
        # even on a workstation where no backend has been enrolled yet.
        from ..context import Context

        ctx = Context(invocation.backend_name, explicit=invocation.explicit_backend)
        name = ctx.login(opts.login)
        ready = setup.configure_remote_tmux(ctx, name, check=opts.check) and ready
        if not opts.no_tool and ctx.settings.flag("SETUP_SYNC_NERSC_TOOL"):
            ready = setup.configure_companion(ctx, name, check=opts.check) and ready

    return 0 if ready else 1

