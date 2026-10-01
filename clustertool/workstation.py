"""Which machine this is, so work started from another one is left alone.

A login name is this machine's: `main` on one workstation and `main` on
another are two connections that know nothing of each other. The cluster's
shared home is the same for both, though, so each one sees the other's
breadcrumbs and, on a node they share, the other's tmux sessions. Told apart
by login name alone, every session the other machine started looks like the
leftover of a login this machine forgot, which is exactly what `clean` and
`strays` exist to remove.

So every session this machine creates is also tagged with this machine's ID
(the tmux option WORKSTATION_OPTION), and its breadcrumb records it after a
tab. Older versions read only the first field of a breadcrumb, so they see
the owner they always did. A session or record carrying another machine's ID
is that machine's: it is listed, and can be attached to, but nothing here
sweeps, kills, adopts, retags or forgets it. One with no ID at all was made
before this existed, and is judged as before by its owner; a machine claims
its own such sessions on its next sweep (Tmux.crumb_sync).

The ID is made once, from the short hostname and a random suffix, so two
machines with one hostname still differ, and kept in the state directory. The
WORKSTATION setting overrides it.
"""

from __future__ import annotations

import os
import re
import socket

from . import config

WORKSTATION_OPTION = "@cluster_workstation"

#: How a breadcrumb records the machine, in its second tab-separated field.
CRUMB_FIELD = "ws="

_VALID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_cached = None


def _made():
    host = socket.gethostname().split(".")[0].lower()
    host = re.sub(r"[^a-z0-9-]", "-", host).strip("-")[:40] or "host"
    return f"{host}-{os.urandom(2).hex()}"


def ident():
    """This machine's workstation ID, made and saved on first use."""
    global _cached
    if _cached:
        return _cached
    chosen = config.global_value("WORKSTATION", "")
    if chosen and _VALID.match(chosen):
        _cached = chosen
        return _cached
    path = config.STATE_ROOT / "workstation"
    try:
        saved = path.read_text(encoding="utf-8").strip()
    except OSError:
        saved = ""
    if not _VALID.match(saved):
        saved = _made()
        try:
            config.private_dir(config.STATE_ROOT)
            tmp = path.with_name(f".workstation.{os.getpid()}")
            tmp.write_text(saved + "\n", encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass  # unsaved, it is still this process's answer
    _cached = saved
    return _cached


def is_other(ws):
    """Does *ws*, a recorded ID, name a different machine? "" never does."""
    return bool(ws) and ws != ident()


class Owner(str):
    """A breadcrumb's owning login, carrying the workstation that wrote it.

    A str, so everything that compares, prints or sorts owners is unchanged;
    ``workstation`` is "" for a record written before IDs existed.
    """

    workstation = ""

    def __new__(cls, login, workstation=""):
        made = super().__new__(cls, login)
        made.workstation = workstation
        return made


def crumb_text(owner):
    """What a breadcrumb file holds: the owner, then this machine's ID."""
    return f"{owner}\t{CRUMB_FIELD}{ident()}"


def parse_crumb_fields(fields):
    """``Owner`` from a breadcrumb's tab-separated fields (owner first)."""
    owner = fields[0] if fields else ""
    ws = ""
    for field in fields[1:]:
        if field.startswith(CRUMB_FIELD):
            ws = field[len(CRUMB_FIELD):].strip()
    return Owner(owner.strip(), ws)
