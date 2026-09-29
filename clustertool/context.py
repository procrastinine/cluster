"""What a command runs against: the backend, its state and its connections.

A login name is global — one name, one connection, on exactly one backend — so
a :class:`Context` *follows the name*. ``--backend`` (and CLUSTER_BACKEND) only
decide where a brand-new login is created; every other command finds the login
wherever it already lives.
"""

from __future__ import annotations

import collections

from . import backends, config, platform as plat, registry, tmuxlayer, ui
from .sshmux import Logins
from .state import State
from .tmuxlayer import Tmux


class Invocation(collections.namedtuple("Invocation",
                                        "backend_name explicit_backend")):
    """The command line's backend choice, for commands that need no Context."""

    __slots__ = ()


class Context:
    """Everything a command needs: backend, state, and the login manager.

    The backend is bound the first time a command asks for any of it, not when
    the Context is made. A command that rejects its arguments therefore does so
    before a backend is loaded, so it prints its own usage rather than a
    missing username, and a command that fails for want of a backend leaves no
    directory behind.
    """

    def __init__(self, backend_name=None, explicit=False):
        #: True when the user actually named a backend, rather than defaulting.
        #: An explicit backend that disagrees with where a login lives is a
        #: mistake worth reporting, not something to silently override.
        self.explicit_backend = explicit
        self._requested = backend_name
        #: sibling contexts by backend, built once per invocation
        self._siblings = {}
        #: backends already reported as skipped, so each is said once
        self._skipped = set()
        self._backend = None
        self._mounts = None

    def _bind(self, backend_name):
        backend = backends.load(backend_name)
        # Only now that the backend exists: `cluster ls` on a machine with no
        # username must not leave state directories behind.
        config.ensure_dirs()
        self._backend = backend
        self._state = State(backend)
        self._logins = Logins(backend, self._state)
        self._tmux = Tmux(self._logins)
        self._mounts = None

    def _bound(self, attribute):
        if self._backend is None:
            self._bind(self._requested)
        return getattr(self, attribute)

    backend = property(lambda self: self._bound("_backend"))
    state = property(lambda self: self._bound("_state"))
    logins = property(lambda self: self._bound("_logins"))
    tmux = property(lambda self: self._bound("_tmux"))
    settings = property(lambda self: self.backend.settings)

    @property
    def mounts(self):
        from .mounts import Mounts

        if self._mounts is None:
            self._mounts = Mounts(self.logins, self.tmux)
        return self._mounts

    def login(self, name=None):
        """Resolve a login name, rebinding this context to the backend it is on.

        Passing None means "the default login". A name that exists nowhere is a
        new login and stays on the current backend — the one case where the
        default backend applies.
        """
        if name is None:
            name = self.default_login()
        tmuxlayer.require_name(name, "login name")
        # If the backend we are already bound to claims this name, it is that
        # one. This also disambiguates a collision: `--backend nersc rename main`
        # must reach nersc's 'main', which is the case rename exists to fix.
        if self.backend.name in registry.backends_claiming(name):
            return name
        owner = registry.find(name)
        if owner is None:
            # A new name. One that is also a backend could never be reached:
            # `--fas` and `fas:path` would always mean the backend.
            backends.refuse_backend_name(name)
            return name
        if self.explicit_backend:
            ui.die(
                f"login '{name}' is on {owner}, not {self.backend.name}",
                "login names are global: one name is one connection",
                f"drop the flag (names resolve on their own), or say --{owner}",
            )
        self._bind(owner)
        return name

    def by_hand(self):
        """Take this command as a person connecting by hand, when a terminal
        is attached: a credential that was refused is then tried again, saying
        so, where an unattended process waits (state.Refusals). Only its first
        connection: a reconnect later is made with nobody asked
        (Logins.interactive)."""
        self.backend.by_hand = plat.terminal_attached()

    def sibling(self, backend_name):
        """A second context bound to another backend, for whole-fleet commands.

        Bound at once, unlike a Context made by hand, so it raises
        :class:`backends.BackendUnavailable` here for a backend this machine
        has no username for; :meth:`scope` is what filters those out.
        """
        if backend_name == self.backend.name:
            return self
        if backend_name not in self._siblings:
            sibling = Context(backend_name, explicit=self.explicit_backend)
            sibling._bound("_backend")
            self._siblings[backend_name] = sibling
        return self._siblings[backend_name]

    def _in_use(self):
        """Backends with a login here or a username configured, plus this one.

        Local state only, like the registry. A backend nobody has set up is
        not part of the fleet, and building its context would only fail.
        """
        names = {name for name, logins in registry.logins_by_backend().items()
                 if logins}
        names.update(name for name in backends.BACKENDS
                     if backends.configured(name))
        names.add(self.backend.name)
        return sorted(names)

    def _usable(self, names):
        kept = []
        for name in names:
            try:
                self.sibling(name)
            except backends.BackendUnavailable as exc:
                # Its logins are still on disk; say why they are not shown
                # rather than failing the whole command over one backend.
                if name not in self._skipped:
                    self._skipped.add(name)
                    ui.warn(f"skipping {name}: {exc.reason}")
                    ui.note(exc.fix)
                continue
            kept.append(name)
        return kept

    def scope(self):
        """Backend names this invocation covers.

        One when a backend was named, otherwise every backend in use on this
        machine. This single rule is what makes `ls`, `status` and `close --all`
        agree with each other.
        """
        if self.explicit_backend:
            return [self.backend.name]
        return self._usable(self._in_use())

    def every_backend(self):
        """Every backend in use, whether or not one was named (--all-backends)."""
        return self._usable(self._in_use())

    def scope_all(self):
        """[(context, login), ...] for every login in scope."""
        out = []
        for backend_name in self.scope():
            sub = self.sibling(backend_name)
            for name in registry.logins_of(backend_name):
                out.append((sub, name))
        return out

    def nothing_set_up(self):
        """Whether no backend was named and none has a username on this machine.

        Whole-fleet views answer that with the setup hint instead of a
        refusal: an empty fleet is a state, not an error.
        """
        return not self.explicit_backend and backends.none_configured()

    def default_login(self):
        """The login to act on when none was named.

        When a backend was named explicitly, the answer must be a login *on that
        backend*, or `cluster nersc:where` would resolve to the configured
        default (which lives on fasrc) and then refuse itself.
        """
        configured = self.settings.str("DEFAULT_LOGIN")
        if not self.explicit_backend:
            return configured
        mine = registry.logins_of(self.backend.name)
        if configured in mine:
            return configured
        if len(mine) == 1:
            return mine[0]
        return configured

    def resolve_login(self, args, index=0):
        """Take a login name from *args* if present, else the default."""
        if len(args) > index and args[index] and not args[index].startswith("-"):
            return self.login(args[index]), args[index + 1 :]
        return self.login(), args[index:]
