"""Command registration shared by the small CLI command modules."""

from __future__ import annotations


COMMANDS = {}


def command(name, *aliases, needs_login=False, needs_context=True, help="",
            options=()):
    """Register one command handler and all of its aliases."""
    def register(func):
        func.cmd_help = help
        func.needs_login = needs_login
        func.needs_context = needs_context
        func.cmd_options = tuple(options)
        for key in (name,) + aliases:
            COMMANDS[key] = func
        func.cmd_name = name
        return func

    return register
