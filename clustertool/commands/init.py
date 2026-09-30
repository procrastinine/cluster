"""`cluster init`: set this machine up, one question at a time."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

from .. import backends, config, configcmd, diagnostics, platform as plat, setup, ui
from ..command import command

_CHECKOUT = Path(__file__).resolve().parents[2]
#: This checkout's entry point, which ~/.local/bin/cluster links to.
ENTRY = _CHECKOUT / "bin" / "cluster"
COMPLETION = _CHECKOUT / "completions" / "cluster.bash"

#: What the test login prints before the node's name, to tell it from the
#: authentication chatter around it.
MARKER = "__cluster_init__"

OPENSSH = ("install OpenSSH's client (Debian/Ubuntu: apt install openssh-client; "
           "Fedora/RHEL: dnf install openssh-clients)")


@command("init", needs_context=False,
         help="Set this machine up: checks, credentials and the optional parts")
def cmd_init(invocation, args):
    """Usage: cluster [--BACKEND] init

A guided setup that can be run again at any time. It checks this machine,
asks for each cluster's credentials (Enter keeps what is saved) and saves
them together, says which optional parts work here, and offers the steps
that go further. Nothing contacts a cluster or any other service unless you
answer yes to a question that says it will; those questions default to no.

With --fasrc, --nersc or --backend NAME, only that cluster is set up.
Answers may also be piped in, one per line, in the order the questions are
asked. A host of your own is added with `cluster backends add NAME HOST`.
"""
    argparse.ArgumentParser(prog="cluster init", add_help=False).parse_args(args)
    missing = _this_machine()
    names = _which_clusters(invocation)
    for name in names:
        _credentials(name)
    _default_backend()
    _optional_parts()
    worked = _further(names, missing)
    set_up = [name for name in sorted(backends.BACKENDS) if backends.configured(name)]
    _next(set_up, missing)
    return 0 if worked and not missing and all(name in set_up for name in names) else 1


# --- this machine -------------------------------------------------------------

def _shell():
    return Path(os.environ.get("SHELL", "")).name


def _startup_file(interactive):
    """The file the user's shell reads: every interactive shell's, or the
    login shell's (where PATH belongs), as each platform starts them."""
    home = Path.home()
    if _shell() == "zsh":
        return home / (".zprofile" if plat.IS_MAC and not interactive else ".zshrc")
    return home / (".bash_profile" if plat.IS_MAC else ".bashrc")


def _this_machine():
    """Python, ssh and `cluster` on the PATH, asking nothing. Returns the
    OpenSSH tools that are missing."""
    ui.say(ui.bold("this machine"))
    report = diagnostics.Report()
    report.check("python", True, f"{'.'.join(map(str, sys.version_info[:3]))} "
                                 f"({sys.executable})")
    missing = []
    for tool in ("ssh", "ssh-keygen"):
        found = shutil.which(tool)
        report.check(tool, bool(found), found or f"not found; {OPENSSH}")
        if not found:
            missing.append(tool)
    found = shutil.which("cluster")
    if found and Path(found).resolve() == ENTRY.resolve():
        report.check("cluster on PATH", True, found)
    elif found:
        report.line("note", "cluster on PATH",
                    f"{found} is another copy; this one is {ENTRY}")
    else:
        report.line("off", "cluster on PATH", "not yet; offered at the end")
    return missing


def _on_path():
    """Offer to link ~/.local/bin/cluster here, if `cluster` is not on PATH."""
    if shutil.which("cluster"):
        return
    link = Path.home() / ".local" / "bin" / "cluster"
    if os.path.lexists(link) and link.resolve() != ENTRY.resolve():
        ui.say(f"cluster on PATH: {configcmd.tilde(link)} is something else, so "
               f"it is left alone; run this copy as {ENTRY}")
        return
    if not os.path.lexists(link):
        if not ui.ask_yes(f"Link {configcmd.tilde(link)} to {ENTRY}, so that "
                          "`cluster` works in any directory?", default=True):
            ui.say(f"cluster on PATH: not linked; run it as {ENTRY}")
            return
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(ENTRY)
        ui.info(f"linked {configcmd.tilde(link)} -> {ENTRY}")
    if str(link.parent) not in os.environ.get("PATH", "").split(os.pathsep):
        ui.say("~/.local/bin is not on PATH yet; for new shells, add it with:")
        ui.say(f"  echo 'export PATH=\"$HOME/.local/bin:$PATH\"' >> "
               f"{configcmd.tilde(_startup_file(interactive=False))}")


# --- which clusters, and their credentials ------------------------------------

def _which_clusters(invocation):
    if invocation.explicit_backend:
        return [configcmd.selected_backend(invocation)]
    names = sorted(backends.BACKENDS)
    default = " ".join(name for name in names if backends.configured(name))
    question = ("Which clusters will you use here: "
                + ", ".join(names[:-1]) + f" or {names[-1]}"
                + (", or all" if len(names) > 2 else ", or both"))
    for _ in range(3 if ui.stdin_is_terminal() else 1):
        ui.say("")
        try:
            typed = ui.ask(question, default=default)
        except EOFError:
            ui.die("no cluster was named; nothing was changed")
        words = [word for word in re.split(r"[\s,]+", typed.lower()) if word]
        if words and all(word in ("both", "all") for word in words):
            return names
        chosen = [backends.as_backend(word) for word in words]
        if words and all(chosen):
            return sorted(set(chosen))
        ui.note(f"name one or more of {', '.join(names)}")
    ui.die("no cluster was named; nothing was changed")


def _credentials(name):
    cls = backends.BACKENDS[name]
    settings = config.Settings(name)
    ui.say("")
    if cls.is_configured(settings) and not cls.missing_credentials(settings):
        question = (f"{name} is set up as {cls.local_username(settings)}; go "
                    "through its credentials again?" if cls.CREDENTIALS else
                    f"{name} is set up; go through its settings again?")
        if not ui.ask_yes(question, default=False):
            return
    configcmd.enroll(name)


def _default_backend():
    """With one cluster set up, new logins go there: say so in BACKEND."""
    set_up = [name for name in sorted(backends.BACKENDS) if backends.configured(name)]
    if len(set_up) != 1:
        return
    value, source = config.resolve("BACKEND")
    if value == set_up[0] and source != config.BUILTIN:
        return
    if source != config.BUILTIN and not ui.ask_yes(
            f"BACKEND is {value}, which is not set up here; make it {set_up[0]}?",
            default=True):
        return
    config.write_value("BACKEND", set_up[0])
    ui.info(f"new logins go to {set_up[0]} (BACKEND={set_up[0]})")


# --- the optional parts -------------------------------------------------------

def _optional_parts():
    ui.say("")
    ui.say(ui.bold("optional parts"))
    report = diagnostics.Report()
    rows = diagnostics.feature_rows()
    for feature, available, detail in rows:
        if available:
            report.check(feature, True, f"available ({detail})")
        else:
            report.line("off", feature, detail)
    mounts = next((available for feature, available, _ in rows
                   if feature == "mounts"), True)
    if not mounts and config.resolve("AUTO_MOUNT")[0]:
        if ui.ask_yes("Mounts are off here. Stop every new login from trying "
                      "to mount (AUTO_MOUNT 0)?", default=True):
            config.write_value("AUTO_MOUNT", "0")
            ui.info("set AUTO_MOUNT=0; to mount again once sshfs is installed: "
                    "cluster config unset AUTO_MOUNT")


# --- further steps, each only on a yes ----------------------------------------

def _completion():
    rc = _startup_file(interactive=True)
    try:
        present = str(COMPLETION) in rc.read_text(encoding="utf-8", errors="replace")
    except OSError:
        present = False
    if present:
        ui.say(f"tab completion: already loaded by {configcmd.tilde(rc)}")
        return
    line = f"source '{COMPLETION}'"
    if _shell() == "zsh":
        line = ("autoload -U +X compinit && compinit; autoload -U +X bashcompinit "
                f"&& bashcompinit; {line}")
    ui.say(f"tab completion: add this line to {configcmd.tilde(rc)}")
    ui.say(f"  {line}")


def _fetch_credential(name):
    """Offer to fetch a cached credential now. Returns False if one failed."""
    backend = backends.load(name)
    state, _detail = backend.credential_state()
    if state == "ok":
        return True
    if not ui.ask_yes(f"Fetch a {backend.label} credential now? This contacts "
                      f"{name} and uses one TOTP code.", default=False):
        return True
    # Asked for just now, by whoever answered: a refusal on record is tried.
    backend.by_hand = True
    try:
        backend.ensure_credential()
    except SystemExit as exc:
        said = exc.code if isinstance(exc.code, str) else ""
        ui.warn("could not fetch it" + (f": {said.split(': ', 1)[-1]}" if said else ""))
        return False
    return True


def _test_login(name):
    """Offer one throwaway login. Returns False if it was tried and failed."""
    from ..auth import explain_failure
    from ..context import Context

    # The backend alone for the question: its context is made only on a yes.
    shown = backends.load(name)
    if not ui.ask_yes(f"Try logging in to {shown.label} now? This connects, "
                      f"{shown.login_cost}.", default=False):
        return True
    ctx = Context(name, explicit=True)
    backend = ctx.backend
    # Asked for just now, by whoever answered: a refusal on record is tried,
    # and what this one finds goes on the record (Logins.authenticate_directly).
    backend.by_hand = True
    argv = backend.ssh_argv(
        extra=["-o", "ControlMaster=no", "-o", "ControlPath=none",
               "-o", "ControlPersist=no"],
        remote=f"echo {MARKER}; hostname")
    # No deadline of its own: ssh's ConnectTimeout and ServerAlive options tell
    # a dead node from a slow one, and whoever is watching can press Ctrl-C.
    try:
        proc = ctx.logins.authenticate_directly(backend.pool_host,
                                                lambda: backend.run_ssh(argv))
    except KeyboardInterrupt:
        ui.say("")
        ui.warn("the test login was interrupted; nothing else was changed")
        return False
    if proc is None:
        ui.warn(f"could not try it: {ctx.logins.last_failure}")
        return False
    lines = (proc.stdout or "").splitlines()
    for index, line in enumerate(lines):
        if line.strip() == MARKER:
            node = lines[index + 1].strip() if index + 1 < len(lines) else "?"
            ui.info(f"logged in to {node}"
                    + (f" as {backend.user}" if backend.user else ""))
            return True
    # Under a pty (a password backend) both streams hold the one transcript.
    said = lines + ([] if proc.stderr == proc.stdout
                    else (proc.stderr or "").splitlines())
    ui.warn(f"the login did not work (ssh exit {proc.returncode}): "
            f"{explain_failure(said)}")
    return False


def _further(names, missing):
    """The steps past the setup itself. Returns False if one was tried and
    failed."""
    ui.say("")
    _on_path()
    _completion()
    worked = True
    for name in names:
        if not backends.configured(name):
            continue
        cls = backends.BACKENDS[name]
        # A test login runs ssh; a new certificate is read with ssh-keygen.
        login = cls.first_check == "login"
        tool, step = (("ssh", "a test login") if login
                      else ("ssh-keygen", "a certificate"))
        if tool in missing:
            ui.say(f"{cls.label}: {step} needs {tool}, which is not installed")
        elif login:
            worked = _test_login(name) and worked
        else:
            worked = _fetch_credential(name) and worked
    return worked


# --- what next ----------------------------------------------------------------

def _next(set_up, missing):
    steps = []
    for name in set_up:
        steps += [step for step in configcmd.next_steps(name) if step not in steps]
    if setup.vscode_installed() and not setup.vscode_status()[0]:
        steps.append(("cluster setup --local-only",
                      "keep VS Code from watching the mounts"))
    steps.append(("cluster doctor", "check everything, the network included"))
    ui.say("")
    if missing:
        ui.say(f"first: {' and '.join(missing)} "
               f"{'is' if len(missing) == 1 else 'are'} not installed, and every "
               f"connection needs {'it' if len(missing) == 1 else 'them'}; {OPENSSH}")
    if not set_up:
        ui.say("no cluster is set up yet: run cluster init again when you have "
               "the credentials")
    configcmd.say_steps(steps)
    if len(set_up) > 1:
        default = backends.default_name()
        other = next(name for name in set_up if name != default)
        ui.say(f"new logins go to {default}; to change that: "
               f"cluster config set BACKEND {other}")
