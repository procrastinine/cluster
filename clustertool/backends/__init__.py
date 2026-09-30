"""Backend registry: the types, and the profiles made of them.

A *type* is a class of hooks (base.Backend) for one kind of site: ``ssh``
reaches any host ssh does, and ``fasrc`` and ``nersc`` know their sites'
authentication and nodes. A *profile* is what every command names: a type,
under a name that its settings section, state and credentials are kept by.

``fasrc`` and ``nersc`` are built-in profiles of their own types, with the
short forms ``--fasrc`` and ``fasrc:PATH``. Any other profile is a section of
the settings file with a TYPE::

    [lab]
    TYPE = ssh
    HOST = lab-login

and is named with ``--backend lab`` (or CLUSTER_BACKEND, or BACKEND). TYPE may
also be the path of a Python file whose BACKEND is a subclass of
base.Backend: see docs/backends.md.

Nothing else in the tool names a backend by hand.
"""

from __future__ import annotations

import collections.abc
import os
import re
from pathlib import Path

from .. import config
from ..config import Settings
from .base import Backend, BackendUnavailable
from .fasrc import FasrcBackend
from .nersc import NerscBackend
from .ssh import SshBackend

#: Every type a profile's TYPE can name, besides a file of one's own.
TYPES = {cls.type_name: cls for cls in (SshBackend, FasrcBackend, NerscBackend)}

#: The profiles there always are. A type whose class carries a name is one
#: site's, and this is its profile.
BUILTIN = {cls.name: cls for cls in TYPES.values() if cls.name}

#: Other spellings of a built-in profile, accepted wherever one is named:
#: `--fas`, `--backend perlmutter`, `fas:~/path`. Deliberately few, because
#: each one is a word the command line gives up: `--X` is taken out of *any*
#: command, so an alias that is a real option elsewhere (rclone's `--rc`)
#: would be stolen from it, and `X:path` names the backend before a login
#: called X ever gets a look. That is also why only built-in profiles have
#: these short forms: a profile's name is the user's to choose.
ALIASES = {alias: cls.name for cls in BUILTIN.values() for alias in cls.aliases}

#: A profile's name: a settings section, a state directory, and a word of
#: CLUSTER_<NAME>_<KEY>.
PROFILE_NAME = re.compile(r"[a-z][a-z0-9_-]{0,23}\Z")

#: Sections of the settings file that are not profiles.
RESERVED = ("global", config.RELAY_SECTION)


def profile_name_problem(name, taken=()):
    """Why *name* cannot name a new profile, or None.

    *taken* holds the profiles there are besides it. Two names whose
    environment words are the same (``my-lab``, ``my_lab``) would share every
    CLUSTER_<NAME>_<KEY>, and a name that with another setting's name makes a
    third one would make that variable mean two things (``mount``:
    CLUSTER_MOUNT_NODES).
    """
    if not PROFILE_NAME.match(name or ""):
        return ("a backend name is a lower-case letter, then up to 23 letters, "
                "digits, '-' or '_'")
    if name in RESERVED:
        return f"[{name}] is the settings file's own section"
    if name in BUILTIN or name in ALIASES or name in TYPES:
        return f"'{name}' names a built-in backend or type"
    word = config.env_word(name)
    for other in taken:
        if other != name and config.env_word(other) == word:
            return (f"it and '{other}' would share every "
                    f"CLUSTER_{word}_<KEY>")
    prefix = word + "_"
    # The types' own tables, not config.known_keys(): that asks this registry,
    # which is what is being built.
    keys = set(config.SHARED) | set(config.GLOBAL) | set(config.RELAY)
    for cls in TYPES.values():
        keys.update(cls.SETTINGS)
    for key in sorted(keys):
        if key.startswith(prefix) and key[len(prefix):] in keys:
            return (f"CLUSTER_{key} would mean both the {key} setting and "
                    f"{key[len(prefix):]} for '{name}'")
    return None


# --- types from a file -----------------------------------------------------

#: Types loaded from files, by (path, mtime_ns, size).
_FILE_TYPES = {}


def _file_type(text):
    """The class a TYPE naming a file holds; ValueError saying why not.

    The file is code this tool runs as you, so it must be yours and nobody
    else's to change, like the settings file that names it.
    """
    path = Path(os.path.expanduser(text))
    if not path.is_absolute():
        path = config.SETTINGS_FILE.parent / path
    try:
        status = path.stat()
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc.strerror or exc}") from None
    if hasattr(os, "getuid") and status.st_uid != os.getuid():
        raise ValueError(f"{path} is not yours")
    if status.st_mode & 0o022:
        raise ValueError(f"{path} can be changed by others (chmod go-w)")
    key = (str(path), status.st_mtime_ns, status.st_size)
    if key in _FILE_TYPES:
        return _FILE_TYPES[key]
    import hashlib
    import importlib.util

    digest = hashlib.sha256(str(path).encode()).hexdigest()[:12]
    spec = importlib.util.spec_from_file_location(
        f"clustertool.backends._type_{digest}", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"{path} is not a Python file")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - anything the file raises
        raise ValueError(f"{path} failed to load: "
                         f"{type(exc).__name__}: {exc}") from None
    cls = getattr(module, "BACKEND", None)
    if not (isinstance(cls, type) and issubclass(cls, Backend)):
        raise ValueError(f"{path} defines no BACKEND, a subclass of "
                         "clustertool.backends.base.Backend")
    if "type_name" not in vars(cls):
        cls.type_name = path.stem
    _FILE_TYPES[key] = cls
    return cls


def _type(text):
    """The type TYPE *text* names; ValueError saying why it names none."""
    text = text.strip()
    if text in TYPES:
        cls = TYPES[text]
        if cls.name:
            raise ValueError(f"'{text}' is one site's type, and its profile is "
                             f"the built-in [{cls.name}]")
        return cls
    if "/" in text or text.endswith(".py"):
        return _file_type(text)
    raise ValueError(f"unknown TYPE '{text}' (known: "
                     + ", ".join(n for n, c in sorted(TYPES.items()) if not c.name)
                     + ", or the path of a Python file)")


# --- profiles --------------------------------------------------------------

_PROFILE_CLASSES = {}


def _profile(name, base):
    """The class of profile *name*, of type *base*: *base* under a name."""
    key = (name, base)
    if key not in _PROFILE_CLASSES:
        _PROFILE_CLASSES[key] = type(
            f"{base.__name__}[{name}]", (base,),
            {"name": name, "shorthand": False, "aliases": (),
             "__module__": base.__module__})
    return _PROFILE_CLASSES[key]


def _profiles(parser):
    """``{name: class}``: the built-in profiles and the settings file's.

    A section that is not a usable profile is warned about once and left out,
    so one typo cannot make every other backend unusable.
    """
    table = dict(BUILTIN)
    sections = [s for s in parser.sections()
                if config._section_value(parser, s, "TYPE") is not None]
    for section in sections:
        kind = config._section_value(parser, section, "TYPE").strip()
        if section in BUILTIN and kind == BUILTIN[section].type_name:
            continue
        problem = profile_name_problem(section, [s for s in sections if s != section])
        if problem is None:
            try:
                table[section] = _profile(section, _type(kind))
                continue
            except ValueError as exc:
                problem = str(exc)
        config._warn_once(f"[{section}] in {config.SETTINGS_FILE} is not a "
                          f"backend: {problem}")
    return table


class _Registry(collections.abc.Mapping):
    """Every profile by name, read afresh whenever the settings file changes
    (config._read_file keeps one parse per version of the file)."""

    def __init__(self):
        self._parsed, self._table = None, dict(BUILTIN)

    def _current(self):
        parser = config._read_file()
        if parser is not self._parsed:
            self._parsed, self._table = parser, _profiles(parser)
        return self._table

    def __getitem__(self, name):
        return self._current()[name]

    def __iter__(self):
        return iter(self._current())

    def __len__(self):
        return len(self._current())

    def __repr__(self):
        return f"BACKENDS({sorted(self._current())})"


#: Every profile, by name.
BACKENDS = _Registry()


def as_shorthand(name):
    """The built-in profile *name* or an alias spells, or None: what `--X`,
    `X:path` and `X:login` may say. Never raises."""
    if not name:
        return None
    key = name.strip().lower()
    key = ALIASES.get(key, key)
    return key if key in BUILTIN else None


def as_backend(name):
    """Resolve *name* to a profile, or None if it names none.

    Never raises, so it is safe for probing. Any profile, as --backend,
    CLUSTER_BACKEND and BACKEND may name it; the short forms are
    :func:`as_shorthand`'s.
    """
    if not name:
        return None
    key = name.strip().lower()
    key = ALIASES.get(key, key)
    return key if key in BACKENDS else None


def flag(name):
    """How a command line names profile *name*: `--nersc`, `--backend lab`."""
    cls = BACKENDS.get(name)
    return cls.cli_flag() if cls else f"--backend {name}"


def resolve_name(name):
    if not name:
        return None
    key = as_backend(name)
    if key is None:
        known = ", ".join(sorted(BACKENDS))
        raise SystemExit(f"cluster: unknown backend '{name}' (known: {known})\n"
                         f"  add one with: cluster backends add NAME HOST")
    return key


def configured(name):
    """Whether this machine knows enough to use backend *name*. Local files only."""
    return bool(BACKENDS[name].is_configured(Settings(name)))


def none_configured():
    """Whether no backend is set up on this machine yet."""
    return not any(configured(name) for name in BACKENDS)


def setup_commands():
    """How to set a backend up: `cluster init`, one site's credentials, or a
    profile for any other host."""
    return ("`cluster init` (or " + " or ".join(
        f"`{BUILTIN[name].setup_command()}`" for name in sorted(BUILTIN))
        + ", or `cluster backends add NAME HOST` for any host ssh reaches)")


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

    Raises :class:`BackendUnavailable` when the backend is not set up here;
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
    """Every backend that can be constructed (one not set up is skipped)."""
    result = []
    for key in sorted(BACKENDS):
        try:
            result.append(BACKENDS[key](Settings(key)))
        except SystemExit:
            continue
    return result


def refuse_backend_name(name, what="login"):
    """Die if *name* is also a built-in backend's name or alias.

    A login called ``nersc`` or ``fas`` could never be addressed: ``--fas``
    and ``fas:path`` would always mean the backend. Any other profile is only
    ever named with --backend, so it takes no name from logins.
    """
    key = as_shorthand(name)
    if key:
        from .. import ui

        spelled = "" if key == name.lower() else f" (an alias of {key})"
        ui.die(f"'{name}' cannot be a {what} name: it names a backend{spelled}",
               f"--{name} and {name}:PATH would always mean the backend, "
               "never the login", "pick another name")


__all__ = ["Backend", "BackendUnavailable", "BACKENDS", "BUILTIN", "TYPES",
           "as_backend", "as_shorthand", "configured", "flag", "load",
           "load_all", "none_configured", "nothing_set_up",
           "profile_name_problem", "refuse_backend_name", "resolve_name",
           "default_name", "setup_commands"]
