"""Command-line interface: parsing, dispatch and help.

Global shape:  cluster COMMAND [ARGS]

**The first argument is always a command.** There is deliberately no bare
``cluster NAME`` form: with this many verbs, a name that happens to match one
would silently do something else, and a name that matches none would have to be
guessed at — as a login *and a session*, so a typo would create a session
named after the typo. Say the verb: ``cluster attach main``. Short aliases
(``a``, ``w``, ``n``, ...) keep that cheap to type.

A backend is named per invocation with ``--backend NAME`` or CLUSTER_BACKEND,
and a built-in one also as a ``--nersc``-style flag anywhere on the line or a
prefix on the verb (``cluster nersc:ls``) — so day-to-day use never has to
repeat it.

The commands themselves live in :mod:`clustertool.commands`; importing a module
there registers its commands.
"""

from __future__ import annotations

import os
import sys
import textwrap

from . import backends, config, platform as plat, registry, ui
from .command import COMMANDS
from .commands import (configure, connections, init, maintenance, mounts,
                       sessions, setup as setup_command, strays, tools, transfers)
from .context import Context, Invocation
from .processname import process_label

#: The command modules in the order `cluster --help` lists them; within one
#: module, commands are listed in the order they are defined.
COMMAND_MODULES = (init, configure, connections, maintenance, mounts, sessions,
                   transfers, tools, setup_command, strays)


def split_backend_prefix(token):
    """``nersc:main`` -> ('nersc', 'main'): a built-in backend's short form."""
    if token and ":" in token:
        head, _, tail = token.partition(":")
        name = backends.as_shorthand(head)
        if name:
            return name, tail
    return None, token


USAGE = """\
Usage: cluster COMMAND [ARGS]        (the first word is always a command)

First time on this machine? Start with:  cluster init

Login names are global: one name means one connection, on one backend. So
`cluster where work` finds work wherever it lives — no --backend needed.

  cluster ls                    every connection and session, all backends
  cluster new work              one name  -> tmux "work" (or configured shell)
  cluster new work api          two names -> always tmux session "api"
  cluster sh                    disposable shell; no retained connection/state
  cluster attach work api       come back to it later  (detach: Ctrl-b d)

Naming a backend decides where a *new* login is created, and scopes fleet-wide
commands like ls/status to one cluster. Shortest first; all four are equivalent:

  cluster login gpu --nersc     a --BACKEND flag, anywhere on the line
  cluster nersc:login gpu       a prefix on the verb
  cluster --backend nersc login gpu
  CLUSTER_BACKEND=nersc cluster login gpu

The default is fasrc, or the only backend set up on this machine (BACKEND
changes it). Aliases: --fas = fasrc, --perlmutter = nersc. These names and
aliases cannot also be login names.

Any other host ssh reaches is a backend of your own, named with --backend:

  cluster backends add lab lab-login     a Host of your ssh config, a hostname,
  cluster --backend lab login work       or user@host

Commands (short alias in parentheses):
"""


def unknown_verb(verb):
    """Refuse a bare name, and say which command was probably meant.

    Guessing is left to the error: taking the name as an implicit `attach`
    would make a login whose name collides with a verb unreachable, and would
    take a mistyped name as *both* login and session, so `cluster mian` would
    create a session called `mian`.
    """
    import difflib

    hints = []
    owner = registry.find(verb)
    if owner:
        hints += [
            f"'{verb}' is a login on {owner}, not a command. Did you mean:",
            f"  cluster attach {verb}          attach to its session '{verb}'",
            f"  cluster where {verb}           its node and sessions",
        ]
    else:
        close = difflib.get_close_matches(verb, sorted(COMMANDS), n=3, cutoff=0.6)
        if close:
            hints.append("did you mean: " + ", ".join(close))
        hints += [
            f"to create a connection by that name:  cluster login {verb}",
            f"to create a session by that name:     cluster new-session LOGIN {verb}",
        ]
    hints.append("'cluster --help' lists every command")
    ui.die(f"unknown command '{verb}'", *hints)


def wants_help(rest):
    """Is `-h`/`--help` being asked of this command?

    Only before a `--`: `cluster run main -- rclone --help` is asking *rclone*.
    """
    limit = rest.index("--") if "--" in rest else len(rest)
    return any(token in ("-h", "--help") for token in rest[:limit])


def declared_options(func):
    """The options *func* declares, read from its source instead of run.

    Some commands build an argparse parser and some parse nothing at all, so
    there is no parser to interrogate without calling the command — which is
    exactly what --help must never do. Reading the source stays correct on its
    own, where a hand-written list beside each command would drift.
    """
    import ast
    import inspect

    sources = [func]
    # A shared parser factory (`_move_parser`) holds the options for several
    # commands, so follow the call rather than reporting no options at all.
    module = inspect.getmodule(func)
    for name in getattr(func, "__code__", None) and func.__code__.co_names or ():
        factory = getattr(module, name, None)
        if callable(factory) and name.endswith("_parser"):
            sources.append(factory)

    found = list(getattr(func, "cmd_options", ()))
    for source in sources:
        try:
            # dedent, not cleandoc: cleandoc treats line 1 as a docstring's
            # first line and strips the body's indentation out from under it.
            tree = ast.parse(textwrap.dedent(inspect.getsource(source)))
        except (OSError, SyntaxError, TypeError):
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add_argument"):
                continue
            flags = [arg.value for arg in node.args
                     if isinstance(arg, ast.Constant)
                     and isinstance(arg.value, str) and arg.value.startswith("-")]
            if not flags:
                continue
            text = next((kw.value.value for kw in node.keywords
                         if kw.arg == "help" and isinstance(kw.value, ast.Constant)), "")
            found.append((", ".join(flags), text))
    return found


def command_help(verb, stream=None):
    """Explain one command, without running a byte of it.

    Every command has to answer --help, including the ones that parse no
    options: an unrecognised flag falling through to "no name given" would
    make `cluster close --help` close the default connection. A question must
    never be answered with an action.
    """
    import inspect

    stream = stream or sys.stdout
    func = COMMANDS[verb]
    aliases = sorted(key for key, other in COMMANDS.items()
                     if other is func and key != func.cmd_name)
    title = f"cluster {func.cmd_name}"
    if aliases:
        title += "   (also: " + ", ".join(aliases) + ")"
    stream.write(title + "\n")
    if func.cmd_help:
        stream.write(f"  {func.cmd_help}\n")
    options = declared_options(func)
    if options:
        stream.write("\noptions:\n")
        width = max(len(flags) for flags, _text in options)
        for flags, text in options:
            stream.write(f"  {flags:<{width}}  {text}".rstrip() + "\n")
    doc = inspect.getdoc(func)
    if doc:
        stream.write("\n" + doc + "\n")
    return 0


def listed_commands():
    """Every command once, in help order, whichever module was imported first."""
    rank = {module.__name__: index for index, module in enumerate(COMMAND_MODULES)}
    unique = {func.cmd_name: func for func in COMMANDS.values()}
    return sorted(unique.values(), key=lambda func: (
        rank.get(func.__module__, len(rank)), func.__code__.co_firstlineno))


def print_usage(stream=None):
    stream = stream or sys.stderr
    stream.write(USAGE)
    write = stream.write
    short = {}
    for key, func in COMMANDS.items():
        if key != func.cmd_name and len(key) <= 2:
            short[func.cmd_name] = key
    for func in listed_commands():
        name = func.cmd_name
        label = f"{name} ({short[name]})" if name in short else name
        write(f"  {label:<20} {func.cmd_help}\n")
    write("\nRun 'cluster COMMAND --help' for one command on its own.\n")


def strip_backend_flag(argv):
    """Pull a `--nersc` / `--fasrc` shorthand out of anywhere in *argv*.

    Only a built-in backend has one: any other is named by its user, and a
    `--NAME` taken from every command line would take options from them.

    `--backend nersc` is the long form, and it has to come first; naming the
    cluster as a flag is what people actually reach for, and it reads best where
    the thought ends: `cluster login gpu --nersc`. So it is accepted anywhere —
    but only before the `--` that introduces a remote command, or
    `cluster run main -- echo --nersc` would quietly change backend.

    Returns (backend_name_or_None, argv_without_it).
    """
    limit = argv.index("--") if "--" in argv else len(argv)
    found, kept = None, []
    for index, token in enumerate(argv):
        if index < limit and token.startswith("--") and len(token) > 2:
            name = backends.as_shorthand(token[2:])
            if name:
                found = name
                continue
        kept.append(token)
    return found, kept


#: The lines around the generated part of completions/cluster.bash.
COMPLETION_BEGIN = ("# >>> generated by `cluster _complete-data --update`; "
                    "do not edit by hand >>>")
COMPLETION_END = "# <<< generated <<<"


def _wrapped(words, first, indent, end="", joint=""):
    """*words* after *first*, as lines of at most 88 columns."""
    lines = textwrap.wrap(" ".join(words), width=88, initial_indent=first,
                          subsequent_indent=indent, break_long_words=False,
                          break_on_hyphens=False) or [first]
    return [line + joint for line in lines[:-1]] + [lines[-1] + end]


def _shell_case(function, comment, arms, fallback=None):
    """A shell function that prints each word's values, one `case` arm each."""
    lines = [f"{function}() {{"] + ([f"    # {comment}"] if comment else [])
    lines.append('    case "$1" in')
    for word, form, values in arms:
        lines += _wrapped(values, f"        {word}) printf {form} ", " " * 12,
                          end=" ;;", joint=" \\")
    if fallback:
        lines.append(f"        *) {fallback} ;;")
    return lines + ["    esac", "}"]


def completion_data():
    """The part of completions/cluster.bash that comes from the code: backend
    names and aliases, the built-in nodes, and the setting names."""
    from .configcmd import credential_keys
    from .nodes import LOGIN

    # The built-in backends only: a profile is this machine's, and the
    # completion reads those from settings.ini as it runs.
    names = sorted(backends.BUILTIN)
    logins, fixed = [], []
    for name in names:
        cls = backends.BUILTIN[name]
        login_class = next((node_class for node_class in cls.node_classes
                            if node_class.serves(LOGIN)), None)
        if login_class:
            logins.append((name, r"'%s\n'",
                           [cls.short(host) for host in login_class.members()]))
        others = [cls.short(host) for node_class in cls.node_classes
                  if node_class is not login_class for host in node_class.members()]
        if others:
            fixed.append((name, r"'%s\n'", others))
    lines = [
        COMPLETION_BEGIN,
        f"_CLUSTER_BACKENDS='{' '.join(names)}'",
        f"_CLUSTER_BACKEND_WORDS='{' '.join(names + sorted(backends.ALIASES))}'",
        f"_CLUSTER_DEFAULT_BACKEND='{config.GLOBAL_DEFAULTS['BACKEND']}'",
    ]
    lines += _wrapped(config.known_keys(classes=backends.TYPES.values()),
                      "_CLUSTER_SETTINGS='", "    ", end="'")
    lines += _wrapped(credential_keys(), "_CLUSTER_CREDENTIALS='", "    ", end="'")
    lines.append("")
    lines += _shell_case("_cluster_alias", "", [
        (alias, "'%s'", [name]) for alias, name in sorted(backends.ALIASES.items())],
        fallback="""printf '%s' "$1\"""")
    lines += [""] + _shell_case("_cluster_builtin_nodes",
                                "The login nodes, which NODES replaces.", logins)
    lines += [""] + _shell_case("_cluster_fixed_nodes",
                                "The other node classes, which NODES leaves alone.",
                                fixed)
    return "\n".join(lines + [COMPLETION_END]) + "\n"


def complete_data(args):
    """`cluster _complete-data [--update]`: print the generated part of the
    completion, or rewrite it in place. For whoever changes a backend or a
    setting, so it is left out of --help."""
    from .commands.init import COMPLETION

    if args not in ([], ["--update"]):
        ui.die("usage: cluster _complete-data [--update]")
    block = completion_data()
    if not args:
        sys.stdout.write(block)
        return 0
    text = COMPLETION.read_text(encoding="utf-8")
    start = text.find(COMPLETION_BEGIN)
    end = text.find(COMPLETION_END + "\n", max(start, 0))
    if start < 0 or end < 0:
        ui.die(f"{COMPLETION} has no generated part to replace",
               f"it starts with the line: {COMPLETION_BEGIN}")
    updated = text[:start] + block + text[end + len(COMPLETION_END) + 1:]
    if updated == text:
        ui.info(f"{COMPLETION} is up to date")
        return 0
    COMPLETION.write_text(updated, encoding="utf-8")
    ui.info(f"updated {COMPLETION}")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["_complete-data"]:
        return complete_data(argv[1:])
    backend_name, argv = strip_backend_flag(argv)

    while argv and argv[0].startswith("--"):
        if argv[0] in ("--help", "-h"):
            print_usage()
            return 0
        if argv[0] == "--backend":
            if len(argv) < 2:
                ui.die("--backend needs a name")
            backend_name = argv[1]
            argv = argv[2:]
            continue
        if argv[0].startswith("--backend="):
            backend_name = argv[0].split("=", 1)[1]
            argv = argv[1:]
            continue
        break

    if not argv:
        print_usage()
        return 2

    verb = argv[0]
    rest = argv[1:]

    prefix, stripped = split_backend_prefix(verb)
    if prefix:
        backend_name, verb = prefix, stripped

    if verb in ("-h", "--help", "help"):
        print_usage(sys.stdout)
        return 0

    # Before dispatch, and before the process is even named: a command asked to
    # explain itself must not do its job instead.
    if verb in COMMANDS and wants_help(rest):
        return command_help(verb)

    # Do this before dispatch, so even a command that blocks for a long time is
    # identifiable in `ps` from the moment it starts.
    plat.set_process_name(process_label(verb, rest))

    func = COMMANDS.get(verb)
    if func is None:
        if verb.startswith("-"):
            ui.die(f"unknown option '{verb}'", "run 'cluster --help'")
        unknown_verb(verb)

    # CLUSTER_BACKEND counts as explicit: the user set it deliberately for this
    # shell, so a name that lives elsewhere is worth reporting rather than
    # silently overriding. A persisted default does not: it is only where new
    # names go, not a request to fence existing global names into one backend.
    explicit = backend_name is not None or bool(os.environ.get("CLUSTER_BACKEND"))
    if not func.needs_context:
        result = func(Invocation(backend_name, explicit), rest)
        return _finish_command(func, result)

    # The Context binds its backend on first use, so a command that rejects
    # its arguments does so before anything is loaded or created.
    ctx = Context(backend_name, explicit=explicit)
    return _finish_command(func, func(ctx, rest))


# These commands return to the local terminal with status information rather
# than replacing it with a remote interactive session.  A single cheap local
# drift check here keeps the warning consistent without coupling every command
# module to VS Code.
_DRIFT_WARNING_COMMANDS = frozenset({
    "backends", "doctor", "list", "mounts", "nodes", "pin", "status",
})


def _finish_command(func, result):
    if func.cmd_name in _DRIFT_WARNING_COMMANDS:
        from .setup import warn_if_local_drift

        warn_if_local_drift()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
