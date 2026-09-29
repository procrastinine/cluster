"""Commands for locally mirrored and remotely installed companion tools."""

from __future__ import annotations

import argparse

from .. import bridge, companion, ui
from ..command import command


@command("nersc-tool", "nt", needs_context=False,
         help="Install or inspect the standalone NERSC companion")
def cmd_nersc_tool(invocation, args):
    """Usage: cluster nersc-tool ACTION [LOGIN]

``install-local`` puts the repository's companion on PATH as
``~/.local/bin/nersc``. ``install LOGIN`` makes the same companion work on a
hub (a login of any non-NERSC cluster) by installing its short-lived
credential too; ``--tool-only`` stages just the companion and its default
config. Neither overwrites an edit made to the hub's copy: it is reported as
a conflict, or adopted here when COMPANION_ADOPT_HUB_EDITS is on. ``sync``
takes the hub's copy into this repository unconditionally.
"""
    parser = argparse.ArgumentParser(prog="cluster nersc-tool", add_help=False)
    parser.add_argument("action", choices=["path", "install-local", "install", "sync"])
    parser.add_argument("login", nargs="?")
    parser.add_argument("--force", action="store_true",
                        help="replace a different local command or renew the certificate")
    parser.add_argument("--tool-only", action="store_true",
                        help="hub install only: do not copy a NERSC credential")
    parser.add_argument("--no-verify", action="store_true",
                        help="skip the end-to-end NERSC connectivity check")
    opts = parser.parse_args(args)

    if opts.action == "path":
        ui.say(str(companion.SOURCE))
        return 0
    if opts.action == "install-local":
        if opts.login or opts.tool_only or opts.no_verify:
            ui.die("install-local does not take a login, --tool-only, or --no-verify")
        return companion.install_local(force=opts.force)
    # A local/path operation must not initialize an unrelated default backend.
    # Installing on a hub needs the normal managed-login context, constructed
    # only after the action and target are known.
    from ..context import Context

    if opts.action == "install":
        from .transfers import need_rsync

        need_rsync("cluster nersc-tool install")
    ctx = Context(invocation.backend_name, explicit=invocation.explicit_backend)
    if opts.action == "sync":
        if opts.force or opts.tool_only or opts.no_verify:
            ui.die("sync does not take --force, --tool-only, or --no-verify")
        return companion.sync_from_hub(ctx, opts.login)
    return bridge.install(ctx, opts.login, force=opts.force,
                          verify=not opts.no_verify,
                          tool_only=opts.tool_only)
