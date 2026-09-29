"""Building blocks for the shell commands run on a cluster.

Every value spliced into a remote command goes through one of these, so a path
with a space, a session name that is a prefix of another, or a leading ``~``
means the same thing everywhere. The output is POSIX sh: FASRC login nodes run
bash 4.2 and tmux 2.7, NERSC's run tmux 3.3a.
"""

from __future__ import annotations

import re
import shlex

#: What tmux accepts as an option name: a user option (``@name``) or one of
#: its own. Anything else is refused rather than quoted into a command.
_OPTION_RE = re.compile(r"^@?[A-Za-z0-9][A-Za-z0-9_-]*$")

#: The loop variable :func:`for_each_session` binds, and the exact target for
#: it, for use inside the loop body.
SESSION_VAR = "s"
EACH_TARGET = '"=$s:"'


def tmux_target(session, window=None, pane=None):
    """A ``-t`` operand naming exactly *session*, shell-quoted.

    tmux resolves a bare ``-t NAME`` as the exact name, else a unique prefix,
    else a glob, so ``kill-session -t ap`` kills ``api`` and exits 0. A leading
    ``=`` accepts only the exact name, on tmux 2.7 as on 3.x.

    The colon after the name is part of the rule. Without it, a command whose
    target is a window or a pane (``send-keys``, ``new-window``,
    ``display-message``, and ``show-options`` on tmux 3.x) reads ``=api`` as a
    pane name and finds nothing; ``=api:`` is session ``api`` and its current
    window for every kind of command.
    """
    target = f"={session}:"
    if window is not None:
        target += str(window)
    if pane is not None:
        target += f".{pane}"
    return shlex.quote(target)


def split_target(target):
    """``SESSION[:WINDOW[.PANE]]`` as :func:`tmux_target` wants it."""
    session, colon, rest = target.partition(":")
    if not colon:
        return tmux_target(session)
    window, dot, pane = rest.partition(".")
    return tmux_target(session, window, pane if dot else None)


def quote_opt(option):
    """A tmux option name, as one word of a remote command.

    Raises ValueError for a name tmux would not accept, so a setting holding
    one cannot turn into anything else on the far side.
    """
    if not _OPTION_RE.match(option or ""):
        raise ValueError(f"not a tmux option name: {option!r}")
    return option


def for_each_session(body):
    """Remote shell running *body* once per tmux session, with ``$s`` set.

    Read line by line, so a session name is one word however it is spelled;
    use :data:`EACH_TARGET` for its exact ``-t`` operand. A server that is not
    running lists nothing, and the loop then runs no times.
    """
    return ("tmux list-sessions -F '#{session_name}' 2>/dev/null | "
            f"while IFS= read -r {SESSION_VAR}; do {body}; done")


def remote_path(path):
    """*path* as one word for the cluster's shell, ``~`` meaning the home directory.

    Quoting keeps spaces and quotes literal, and it would keep ``~`` literal too:
    ``~/results`` would name a directory called ``~``. So a leading ``~/``, a
    bare ``~`` or an empty path is taken relative to the home directory, where
    the cluster's shell starts, as it is for rclone's sftp paths. A relative
    path gets a leading ``./``, so one that starts with ``-`` is never read as
    an option and ``cd`` never consults CDPATH.
    """
    if path in ("", "~"):
        path = "."
    elif path.startswith("~/"):
        path = path[2:].lstrip("/") or "."
    if not path.startswith("/") and path.split("/", 1)[0] not in (".", ".."):
        path = "./" + path
    return shlex.quote(path)


def home_path(path):
    """*path* as one word for a program that resolves it away from the home.

    tmux resolves ``new-session -c`` in its server, not in the shell that runs
    the command, so a home path is spelled out from ``$HOME`` rather than made
    relative as :func:`remote_path` does. Anything else is quoted as it is.
    """
    if path in ("", "~"):
        return '"$HOME"'
    if path.startswith("~/"):
        rest = path[2:].lstrip("/")
        return '"$HOME"' + (f"/{shlex.quote(rest)}" if rest else "")
    return shlex.quote(path)


def sftp_path(path):
    """*path* as an sftp server sees it: relative paths start in the home.

    ``~/x`` is ``x`` and ``~`` is the home itself. Anything else is passed
    through: an sftp path is one argument, never parsed by a shell.
    """
    if path == "~":
        return ""
    if path.startswith("~/"):
        return path[2:].lstrip("/")
    return path
