"""Backend registry.

Adding a backend means writing its class and listing it in BACKENDS; nothing
else in the tool names a backend by hand.
"""

from __future__ import annotations

import os

from .. import config
from ..config import Settings
from .base import Backend, BackendUnavailable
from .fasrc import FasrcBackend
from .nersc import NerscBackend

BACKENDS = {
    FasrcBackend.name: FasrcBackend,
    NerscBackend.name: NerscBackend,
}

#: Other spellings accepted wherever a backend is named: `--fas`,
#: `--backend perlmutter`, `fas:~/path`. Deliberately few, because each one is
#: a word the command line gives up: `--X` is taken out of *any* command, so an
#: alias that is a real option elsewhere (rclone's `--rc`) would be stolen from
#: it, and `X:path` names the backend before a login called X ever gets a look.
ALIASES = {
    "fas": "fasrc",
    "perlmutter": "nersc",
}


def as_backend(name):
    """Resolve *name* to a backend key, or None if it names no backend.

    Never raises, so it is safe for probing: the CLI uses it to tell a `--nersc`
    shorthand apart from an ordinary option like `--dry-run`.
    """
    if not name:
        return None
    key = name.strip().lower()
    key = ALIASES.get(key, key)
    return key if key in BACKENDS else None


def resolve_name(name):
    if not name:
        return None
    key = as_backend(name)
    if key is None:
        known = ", ".join(sorted(BACKENDS))
        raise SystemExit(f"cluster: unknown backend '{name}' (known: {known})")
    return key


def configured(name):
    """Whether this machine has a username for backend *name*. Local files only."""
    return bool(BACKENDS[name].local_username(Settings(name)))


def none_configured():
    """Whether no backend has a username on this machine yet."""
    return not any(configured(name) for name in BACKENDS)


def setup_commands():
    """How to set a backend up: `cluster init`, or one backend's credentials."""
    return ("`cluster init` (or " + " or ".join(
        f"`cluster --{name} config credentials`" for name in sorted(BACKENDS)) + ")")


def nothing_set_up():
    """The refusal for a command that needs a backend where none is set up.

    Site-neutral on purpose: with nothing set up, the built-in default is only
    a guess, and naming its site would send someone who uses another cluster
    to set up the wrong one.
    """
    return BackendUnavailable(
        None, "no cluster is set up on this machine yet",
        f"run {setup_commands()}; `cluster backends` lists them")


def _chosen():
    """The backend CLUSTER_BACKEND or a persisted BACKEND names, or ""."""
    return os.environ.get("CLUSTER_BACKEND") or config.global_value("BACKEND", "")


def default_name():
    """The backend a new, otherwise-unclaimed login name goes to.

    CLUSTER_BACKEND or a persisted BACKEND is an instruction and is followed.
    The built-in default is only a guess, so it gives way on a machine where
    it is not set up and another backend is: with NERSC as the only enrolled
    cluster, `cluster ls` must not die asking for a FASRC username.
    """
    chosen = _chosen()
    if chosen:
        return resolve_name(chosen)
    builtin = config.GLOBAL_DEFAULTS["BACKEND"]
    if configured(builtin):
        return builtin
    ready = [name for name in sorted(BACKENDS) if configured(name)]
    return ready[0] if ready else builtin


def load(name=None):
    """The backend *name*, or the default one when no name is given.

    Raises :class:`BackendUnavailable` when the backend has no username here;
    with no name given, nothing chosen and nothing set up at all, the refusal
    is :func:`nothing_set_up` rather than one site's.
    """
    key = resolve_name(name)
    if key is None:
        if not _chosen() and none_configured():
            raise nothing_set_up()
        key = default_name()
    return BACKENDS[key](Settings(key))


def load_all():
    """Every backend that can be constructed (missing credentials are skipped)."""
    result = []
    for key in sorted(BACKENDS):
        try:
            result.append(BACKENDS[key](Settings(key)))
        except SystemExit:
            continue
    return result


def refuse_backend_name(name, what="login"):
    """Die if *name* is also a backend name or alias.

    A login called ``nersc`` or ``fas`` could never be addressed: ``--fas``
    and ``fas:path`` would always mean the backend.
    """
    key = as_backend(name)
    if key:
        from .. import ui

        spelled = "" if key == name.lower() else f" (an alias of {key})"
        ui.die(f"'{name}' cannot be a {what} name: it names a backend{spelled}",
               f"--{name} and {name}:PATH would always mean the backend, "
               "never the login", "pick another name")


__all__ = ["Backend", "BackendUnavailable", "BACKENDS", "as_backend",
           "configured", "load", "load_all", "none_configured",
           "nothing_set_up", "refuse_backend_name", "resolve_name",
           "default_name", "setup_commands"]
