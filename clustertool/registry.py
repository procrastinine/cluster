"""Where a login lives.

A login name is **global**: one name means one connection, on exactly one
backend. That is the whole point — `cluster attach main` has to be unambiguous without
the user remembering which cluster `main` is on, and `--backend` should only
matter when creating something new.

This module answers "which backend owns this name" from **local state alone**:
no credentials, no network, no backend construction. That matters because it sits
in front of almost every command, and because a backend whose credentials are
missing still has state worth listing.
"""

from __future__ import annotations

from . import config
from .backends import BACKENDS
from .state import recorded_logins


def logins_of(backend_name):
    """Sorted login names known for one backend, from local state only.

    The same answer as that backend's ``State.known_logins()``, without
    constructing the backend or creating its state directory.
    """
    return recorded_logins(backend_name, config.STATE_ROOT / backend_name,
                           config.CTL_DIR)


def logins_by_backend():
    """{backend: [login, ...]} for every known backend, in backend name order."""
    return {name: logins_of(name) for name in sorted(BACKENDS)}


def all_logins():
    """[(backend, login), ...] across every backend."""
    return [(backend, login)
            for backend, logins in logins_by_backend().items()
            for login in logins]


def find(name):
    """The backend owning *name*, or None.

    On a collision the first backend in name order wins, so behaviour stays
    deterministic; :func:`collisions` is what reports the problem.
    """
    if not name:
        return None
    for backend, logins in logins_by_backend().items():
        if name in logins:
            return backend
    return None


def backends_claiming(name):
    """Every backend with state for *name* — more than one is a collision."""
    return [backend for backend, logins in logins_by_backend().items()
            if name in logins]


def collisions():
    """{login: [backend, ...]} for names claimed by more than one backend."""
    seen = {}
    for backend, login in all_logins():
        seen.setdefault(login, []).append(backend)
    return {login: names for login, names in seen.items() if len(names) > 1}


def is_taken_elsewhere(name, backend_name):
    """True when *name* already belongs to a backend other than *backend_name*."""
    return any(other != backend_name for other in backends_claiming(name))
