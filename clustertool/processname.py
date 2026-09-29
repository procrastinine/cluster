"""Short, readable process names for long-running cluster commands."""

from __future__ import annotations

from . import platform as plat


#: Verbs whose first two names are LOGIN then SESSION, and whose processes are
#: worth telling apart — these are the ones that sit around in a process list.
SESSION_VERBS = {"attach", "a", "tmux", "new", "n", "new-session", "task",
                 "session", "window", "new-window", "send", "kill-session", "k"}
LOGIN_VERBS = {"shell", "sh", "ssh", "run", "r", "mount", "m", "umount",
               "unmount", "repair", "where", "w", "node", "sessions", "ss",
               "login", "l", "open", "close", "logout", "push", "pull", "boot"}

#: How much room is left for the detail after the ``cluster:`` prefix.
PROC_DETAIL_MAX = plat.PROC_NAME_MAX - len("cluster:")


def name_pair(login, session):
    """Return ``login/session``, shortened from the left to fit the kernel."""
    room = PROC_DETAIL_MAX - len(session) - 1
    if room >= 1:
        return f"{login[:room]}/{session}"
    return session[:PROC_DETAIL_MAX]


def process_label(verb, rest):
    """Build a process name no longer than the kernel's 15-character limit."""
    names = [arg for arg in rest if not arg.startswith("-")]
    if verb in ("monitor", "watch"):
        detail = "w:" + names[0] if names else "w"
    elif verb in SESSION_VERBS and names:
        login = names[0]
        session = names[1] if len(names) > 1 else login
        detail = login if session == login else name_pair(login, session)
    elif verb in LOGIN_VERBS and names:
        detail = names[0]
    else:
        detail = ""
    if not detail:
        return "cluster"
    return f"cluster:{detail}"[:plat.PROC_NAME_MAX]
