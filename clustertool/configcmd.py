"""Implementation of the unified ``cluster config`` command.

This module deliberately does not construct a backend or a login Context.  A
configuration command must be able to repair a missing username or credential,
and it must never authenticate, open a master, inspect a mount, or otherwise
disturb work that is already running.
"""

from __future__ import annotations

import argparse
import getpass
import textwrap
import warnings
from pathlib import Path

from . import backends, config, ui
from .backends.base import USERNAME, resolve_cred_dir


# --- credentials ---------------------------------------------------------------

def _class(name):
    return backends.BACKENDS[name]


def _credential(name, key):
    """The credential backend *name* knows by *key*, or None."""
    for field in _class(name).CREDENTIALS:
        if key in field.keys:
            return field
    return None


def credential_keys():
    """Every name `cluster config` accepts for a credential, sorted."""
    return sorted({key for cls in backends.BACKENDS.values()
                   for field in cls.CREDENTIALS for key in field.keys})


def _cred_dir(name):
    return resolve_cred_dir(name, config.Settings(name))


def _listed(items):
    """"a", "a and b", "a, b and c"."""
    items = list(items)
    return ", ".join(items[:-1]) + " and " + items[-1] if len(items) > 1 else "".join(items)


def tilde(path):
    """*path* with the home directory shown as ~."""
    home = str(Path.home())
    text = str(path)
    return "~" + text[len(home):] if text == home or text.startswith(home + "/") else text


def _username(name):
    return _class(name).local_username(config.Settings(name))


def _status(name, field):
    """A plain credential's value, or whether a secret is stored."""
    if field is USERNAME:
        return _username(name) or "missing"
    try:
        stored = (_cred_dir(name) / field.filename).stat().st_size > 0
    except OSError:
        stored = False
    if not field.secret and stored:
        return (_cred_dir(name) / field.filename).read_text(encoding="utf-8").strip()
    return "set" if stored else "missing"


def credential_table(names):
    """Print each backend's credentials: values of plain ones, secrets as set or missing."""
    fields = []
    for name in names:
        fields += [field for field in _class(name).CREDENTIALS if field not in fields]
    rows = [[name] + [_status(name, field) if field in _class(name).CREDENTIALS
                      else "-" for field in fields] + [tilde(_cred_dir(name))]
            for name in names]
    ui.table(rows, ["BACKEND"] + [field.label.upper() for field in fields]
             + ["KEPT IN"])


def _save(name, answers):
    """Write {field: value} into backend *name*'s credential directory at once.

    The directories on the way are the tool's own and are made private (0700)
    along with it; every file is 0600.
    """
    from . import platform as plat

    directory = _cred_dir(name)
    for parent in (config.CONFIG_ROOT, config.CRED_ROOT):
        if parent in directory.parents:
            config.private_dir(parent)
    config.private_dir(directory)
    for field, value in answers.items():
        plat.atomic_write_text(directory / field.filename, value + "\n",
                               mode=config.SECRET_MODE)
    return directory


def _code_matches(seed):
    """Ask whether the code *seed* makes now is the one the app shows.

    The code goes to stderr with the question and nowhere else: it is valid
    for 30 seconds and must not end up in a log or a captured stdout.
    """
    from .auth import seconds_left_in_window, totp

    return ui.ask_yes(f"code right now: {totp(seed)} "
                      f"({seconds_left_in_window():.0f}s left); does it match "
                      "your authenticator app?", default=True)


def _verified(field, value):
    """Why a typed *value* is not confirmed, or None when it is."""
    if field.verify == "repeat" and ui.stdin_is_terminal():
        try:
            again = ui.ask(f"repeat the {field.label}", secret=True)
        except EOFError:
            again = None
        if again != value:
            return f"the two {field.label}s did not match"
    if field.verify == "code" and not _code_matches(value):
        return f"then that is not the {field.label} your app has"
    return None


def _ask_credential(name, field):
    """A new value for *field*, or None to keep the stored one.

    Required when nothing is stored. A person gets three tries at a bad
    answer; a script's bad answer stops everything, as does the end of its
    input, and nothing is saved.
    """
    current = _username(name) if field is USERNAME else ""
    stored = _status(name, field) != "missing"
    question = field.label
    if field.hint:
        question += f" ({field.hint})"
    if stored and field.secret:
        question += "; Enter keeps the saved one"
    tries = 3 if ui.stdin_is_terminal() else 1
    problem = ""
    for attempt in range(tries):
        try:
            typed = ui.ask(question, default=current, secret=field.secret)
        except EOFError:
            ui.die(f"no answer for the {field.label}; nothing was saved")
        if not typed and stored:
            return None
        try:
            value = field.clean(typed) if typed else ""
        except ValueError as exc:
            problem = str(exc)
        else:
            problem = (f"a {field.label} is required" if not value
                       else _verified(field, value) if value != current else None)
            if problem is None:
                return value if value != current else None
        if attempt + 1 < tries:
            ui.note(problem)
    ui.die(f"{problem}; nothing was saved")


def _ask_setting(name, key):
    """A new value for backend *name*'s setting *key*: None keeps it, "" removes it."""
    raw, _source = config.file_value(key, name)
    current = (raw or "").strip()
    question = f"{key}: {config.lookup(key, name).help}"
    if current:
        question += "; - for none"
    tries = 3 if ui.stdin_is_terminal() else 1
    problem = ""
    for attempt in range(tries):
        try:
            typed = ui.ask(question, default=current)
        except EOFError:
            return None
        if typed == "-":
            return "" if current else None
        if typed == current:
            return None
        try:
            return str(config.parse_value(key, typed, name))
        except ValueError as exc:
            problem = f"{key}: {exc}"
        if attempt + 1 < tries:
            ui.note(problem)
    ui.die(f"{problem}; nothing was saved")


def enroll(name):
    """Ask for what backend *name* needs from you, then save it all at once.

    Nothing is written until every answer is in, so an interrupted or refused
    enrolment leaves things as they were. Never touches the network.
    """
    cls = _class(name)
    width = min(ui.terminal_width() or 80, 80) - 2
    ui.say(ui.bold(f"{cls.label} ({name})"))
    for paragraph in cls.enroll_hint:
        ui.say(textwrap.fill(paragraph, width=width, initial_indent="  ",
                             subsequent_indent="  "))
    answers = {}
    for field in cls.CREDENTIALS:
        value = _ask_credential(name, field)
        if value is not None:
            answers[field] = value
    changes = {}
    for key in cls.enroll_settings:
        value = _ask_setting(name, key)
        if value is not None:
            changes[key] = value

    if answers:
        directory = _save(name, answers)
        ui.info(f"saved the {_listed(field.label for field in answers)} for "
                f"{name} in {tilde(directory)}")
    for key, value in changes.items():
        if value:
            config.write_value(key, value, backend=name)
            ui.info(f"set {key}={value} in [{name}] of {tilde(config.SETTINGS_FILE)}")
        else:
            config.unset_value(key, backend=name)
            ui.info(f"removed {key} from [{name}] of {tilde(config.SETTINGS_FILE)}")
    if not answers and not changes:
        ui.info(f"nothing changed for {name}")


def next_steps(name):
    """``[(command, what it does)]`` to try once backend *name* is set up."""
    steps = []
    if not _class(name).interactive_auth:
        steps.append((f"cluster --{name} auth",
                      "fetch a certificate now (uses one TOTP code)"))
    steps.append((f"cluster --{name} new work",
                  "open a connection and a tmux session called work"))
    return steps


def say_steps(steps):
    width = max(len(command) for command, _what in steps)
    for index, (command, what) in enumerate(steps):
        ui.say(f"{'next:' if index == 0 else '':<6}{command:<{width}}  {what}")


def choose_backend(question="Which cluster"):
    """A backend named by whoever is at stdin: its name or an alias.

    A persisted BACKEND is the default. The end of input stops, since a
    guess would set up someone else's cluster.
    """
    names = sorted(backends.BACKENDS)
    persisted, source = config.resolve("BACKEND")
    default = persisted if source != config.BUILTIN else ""
    listed = ", ".join(f"{name} ({backends.BACKENDS[name].label})" for name in names)
    for _ in range(3 if ui.stdin_is_terminal() else 1):
        try:
            typed = ui.ask(f"{question}: {listed}", default=default)
        except EOFError:
            ui.die("no cluster was named; nothing was changed",
                   "name one with a flag: " + " or ".join(
                       f"cluster --{name} config credentials" for name in names))
        chosen = backends.as_backend(typed)
        if chosen:
            return chosen
        ui.note(f"'{typed}' is not one of: {', '.join(names)}")
    ui.die("no cluster was named; nothing was changed")


def _credentials(invocation):
    name = (selected_backend(invocation) if invocation.explicit_backend
            else choose_backend())
    enroll(name)
    ui.say("")
    credential_table([name])
    missing = _class(name).missing_credentials(config.Settings(name))
    if missing:
        ui.say("")
        ui.say(f"still missing: {_listed(missing)}")
        return 1
    ui.say("")
    say_steps(next_steps(name))
    return 0


# --- settings ------------------------------------------------------------------

def selected_backend(invocation):
    """Selected/default backend without constructing its credential object."""
    return backends.resolve_name(invocation.backend_name) or backends.default_name()


def _in_file(key):
    """The sections of the settings file that hold *key*, in file order."""
    return [section for section, name in config.file_entries()
            if config.normalize_key(name) == key]


def _known_or_die(key, in_file_is_enough=False):
    if key in credential_keys() or key in config.known_keys():
        return
    if in_file_is_enough and _in_file(key):
        return
    import difflib

    close = difflib.get_close_matches(key, config.known_keys() + credential_keys(),
                                      n=3, cutoff=0.5)
    ui.die(f"unknown setting '{key}'",
           *(["did you mean: " + ", ".join(close)] if close else []),
           "run `cluster config list` to see every setting")


def _owner(invocation, key, writing, force_global=False):
    """The backend whose section *key* is read from or written to, or None
    for the machine-wide answer.

    A setting only some backends read belongs to them: named, or the only
    one, it is theirs; another backend's flag, or --global, is refused rather
    than written where nothing reads it. A setting every backend reads is
    written to [global] unless a backend is named, and read as the named or
    default backend sees it.
    """
    named = selected_backend(invocation) if invocation.explicit_backend else None
    if config.is_machine_wide(key):
        return None
    owners = config.owners(key)
    if owners:
        flags = " or ".join(f"cluster --{owner} config set {key} VALUE"
                            for owner in owners)
        if force_global:
            ui.die(f"{key} is read only by {', '.join(owners)}, so it cannot be "
                   "global", f"run: {flags}")
        if named and named not in owners:
            ui.die(f"{key} is a {', '.join(owners)} setting; {named} does not read it",
                   f"run: {flags}")
        if named:
            return named
        if len(owners) == 1:
            return owners[0]
        if not writing and selected_backend(invocation) in owners:
            return selected_backend(invocation)
        ui.die(f"{key} is set for one backend at a time: name it", f"run: {flags}")
    if force_global or (writing and not named):
        return None
    return named or (None if writing else selected_backend(invocation))


def _show(invocation, show_all=False):
    explicit = invocation.explicit_backend
    backend = selected_backend(invocation) if explicit else None
    if not explicit and not backends.none_configured():
        backend = backends.default_name()
    rows = []
    for key in config.known_keys():
        owners = config.owners(key)
        scope = None if config.is_machine_wide(key) else backend
        if owners and backend not in owners:
            scope = owners[0] if len(owners) == 1 else None
        value, source = config.resolve(key, scope)
        if not show_all and source == config.BUILTIN:
            continue
        meaning = config.describe(key)
        if owners and len(owners) < len(backends.BACKENDS):
            meaning += f" ({', '.join(owners)} only)"
        rows.append([key, value, source, meaning])
    for section, name, value in config.unread_entries():
        rows.append([name, value, f"{config.SETTINGS_FILE} [{section}]",
                     "not a setting this tool reads"])

    ui.say(f"settings file: {config.SETTINGS_FILE}")
    if backend:
        ui.say(f"backend: {backend}" + ("" if backends.configured(backend) else
                                        f" (not set up: cluster --{backend} "
                                        "config credentials)"))
    else:
        ui.say("no cluster is set up on this machine yet: run `cluster init`")
    if rows:
        ui.table(rows, ["SETTING", "VALUE", "SOURCE", "MEANING"])
    else:
        ui.say("no overrides (all settings use built-in defaults)")
    ui.say("")
    credential_table([backend] if explicit else sorted(backends.BACKENDS))
    return 0


def _get(invocation, key):
    _known_or_die(key)
    field = _credential(selected_backend(invocation), key)
    if field is not None:
        value = _status(selected_backend(invocation), field)
        if not field.secret:
            ui.say(value if value != "missing" else "")
            return 0 if value != "missing" else 1
        ui.say(value)
        return 0
    if key == "BACKEND":
        # What commands actually use: the setting, or the backend set up here.
        ui.say(backends.default_name())
        return 0
    value, _source = config.resolve(key, _owner(invocation, key, writing=False))
    ui.say(str(value))
    return 0


def _typed_secret(prompt):
    """A secret typed at the terminal, never echoed and never on the command line."""
    with warnings.catch_warnings():
        # With no terminal, getpass reads stdin instead and says so.
        warnings.simplefilter("ignore", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt).strip()
        except EOFError:
            ui.die("no answer; nothing was saved")


def _set_credential(name, field, value):
    if field.secret:
        if value is not None:
            ui.die(f"refusing a {field.label} on the command line",
                   "it would remain in shell history and the process list",
                   f"run: cluster --{name} config set {field.keys[0].lower()}")
        prompt = field.label[:1].upper() + field.label[1:]
        value = _typed_secret(f"{prompt}{f' ({field.hint})' if field.hint else ''}: ")
    elif value is None:
        ui.die(f"the {field.label} needs a value",
               f"run: cluster --{name} config set {field.keys[0].lower()} VALUE")
    try:
        value = field.clean(value)
    except ValueError as exc:
        ui.die(f"{exc}; nothing was saved")
    if field.verify == "repeat" and ui.stdin_is_terminal():
        if _typed_secret(f"Repeat the {field.label}: ") != value:
            ui.die(f"the two {field.label}s did not match; nothing was saved")
    if field.verify == "code":
        if ui.stdin_is_terminal():
            if not _code_matches(value):
                ui.die(f"then that is not the {field.label} your app has; "
                       "nothing was saved")
        else:
            from .auth import seconds_left_in_window, totp

            ui.info(f"code right now: {totp(value)} "
                    f"({seconds_left_in_window():.0f}s left); it should match "
                    "your authenticator app")
    directory = _save(name, {field: value})
    ui.info(f"saved the {field.label} for {name} in "
            f"{tilde(directory / field.filename)} (mode 600)")
    return 0


def _set(invocation, key, value, force_global=False):
    _known_or_die(key)
    name = selected_backend(invocation)
    field = _credential(name, key)
    if field is not None:
        return _set_credential(name, field, value)
    if value is None:
        ui.die(f"{key} needs a value")
    section = _owner(invocation, key, writing=True, force_global=force_global)
    try:
        normalized = config.write_value(key, value, backend=section)
    except (KeyError, ValueError) as exc:
        ui.die(f"invalid value for {key}: {exc}")
    ui.info(f"set {key}={normalized} in {config.SETTINGS_FILE} "
            f"[{config.section_for(key, section)}]")
    ui.note("the new value applies to subsequent cluster commands; running "
            "sessions were untouched")
    return 0


def _unset(invocation, key, force_global=False):
    _known_or_die(key, in_file_is_enough=True)
    name = selected_backend(invocation)
    field = _credential(name, key)
    if field is not None:
        path = _cred_dir(name) / field.filename
        if path.exists():
            ui.die(f"refusing to delete {path} through config unset",
                   "credentials are deleted by hand, deliberately",
                   f"to change it: cluster --{name} config credentials")
        ui.say(f"the {field.label} is already missing")
        return 0
    if key in config.known_keys():
        owner = _owner(invocation, key, writing=True, force_global=force_global)
        entries = [(config.section_for(key, owner), config.normalize_key(key))]
        remove = lambda section, _name: config.unset_value(key, owner)  # noqa: E731
    else:
        # Nothing reads it, so it goes from wherever it is, or from the
        # named backend's section.
        entries = [(section, name) for section, name in config.file_entries()
                   if config.normalize_key(name) == key
                   and (not invocation.explicit_backend
                        or section == selected_backend(invocation))]
        remove = config.remove_entry
    for section, name in entries or [("global", key)]:
        try:
            changed = remove(section, name) if entries else False
        except ValueError as exc:
            ui.die(str(exc), "the malformed file was left untouched")
        if changed:
            ui.info(f"removed {key} from [{section}]")
            continue
        ui.say(f"{key}: no persisted value in [{section}]")
        elsewhere = [other for other in _in_file(key) if other != section]
        if elsewhere:
            ui.note("it is set in " + ", ".join(f"[{other}]" for other in elsewhere)
                    + (f"; to remove it: cluster --{elsewhere[0]} config unset {key}"
                       if elsewhere[0] in backends.BACKENDS else ""))
    return 0


def run(invocation, args):
    parser = argparse.ArgumentParser(prog="cluster config", add_help=False)
    parser.add_argument("action", nargs="?", default="show",
                        choices=("show", "list", "get", "set", "unset", "path", "credentials"))
    parser.add_argument("key", nargs="?")
    parser.add_argument("value", nargs="?")
    parser.add_argument("--all", action="store_true",
                        help="include built-in values, not only overrides")
    parser.add_argument("--global", dest="force_global", action="store_true",
                        help="write the global section even with a backend flag")
    opts = parser.parse_args(args)

    if opts.action == "path":
        ui.say(str(config.SETTINGS_FILE))
        return 0
    if opts.action in ("show", "list"):
        return _show(invocation, show_all=opts.all or opts.action == "list")
    if opts.action == "credentials":
        return _credentials(invocation)
    if not opts.key:
        ui.die(f"config {opts.action} needs a setting name")
    key = config.normalize_key(opts.key)
    if opts.action == "get":
        return _get(invocation, key)
    if opts.action == "set":
        return _set(invocation, key, opts.value, force_global=opts.force_global)
    return _unset(invocation, key, force_global=opts.force_global)
