"""The Globus engine for cross-cluster transfers.

Globus is the only one of the three engines where neither this machine nor either
cluster's shell moves the bytes: both sides are *collections* registered with the
Globus service, and the service brokers a transfer between them. That buys the
things that matter at scale — restart after a network failure, checksum
verification, parallel streams tuned by the endpoint operators, and a task that
survives this process exiting.

What it costs is setup that cannot be automated away, and two limits, measured
against the real endpoints on 2026-08-08:

* ``globus login`` is a browser flow, once. On a headless machine that is
  ``--no-local-server``: it prints a URL to open elsewhere and reads back a code.
* Each mapped collection needs a ``data_access`` **consent** — once, per
  collection.
* FASRC's collection additionally enforces a **session policy**: the consent is
  not enough, the session must have authenticated recently through
  ``globus.rc.fas.harvard.edu``, and that *expires*. NERSC's does not.
* FASRC's collection **does not export home directories** at all. ``/n/home*``
  answers ``EndpointPermissionDenied`` regardless of the session;
  ``/n/netscratch/...`` and ``/n/holystore01/LABS/...`` work.

Those last two are why the direct engine, not this one, is the default and the
right answer for anything scheduled: a session policy resolves through a browser,
which no cron job can do, and half the filesystem is invisible here anyway. This
engine is for what it is genuinely best at — very large, restartable, attended
moves on the exported filesystems.

Both of those failures are detected *before* a task is submitted (see preflight
and check_path), because a Globus task fails asynchronously: submit succeeds, the
task dies minutes later, and ``task wait`` reports a bare non-zero exit. And the
two authorization failures are told apart deliberately — they read almost alike
and need different commands. The same listing that proves a side readable also
says whether the source is a file or a directory, which decides the path
semantics.

The globus CLI calls are not given a deadline of their own: its SDK already
times out a request the service stops answering, and retries with backoff. A
large listing or a slow submission is left to finish.

Verified end to end in both directions with matching checksums; the collection
ids each backend declares are the ones that were actually used.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

from . import config, platform as plat, ui

#: Each failure names only the steps still outstanding. Telling someone who has
#: the CLI installed to install it is the kind of hint that makes the real step
#: harder to see.
INSTALL = ("install: uv tool install globus-cli   (or: pipx install globus-cli)",)
#: --no-local-server is what makes this work without a browser on this machine:
#: the CLI prints a URL to open anywhere and takes a pasted code back. It is
#: implied when the CLI detects a remote session, but saying it is deterministic.
LOGIN = ("log in:  globus login --no-local-server   (prints a URL; paste the "
         "code back)",)
#: Each cluster's collection is declared by its backend, so this is only needed
#: when a site changes its collection or another is preferred.
COLLECTIONS = (
    "each cluster's collection is built in; to use another for one:",
    "  cluster --BACKEND config set GLOBUS_COLLECTION <uuid>",
    "  (find candidates with: globus endpoint search <site>)",
)
SETUP = INSTALL + LOGIN + COLLECTIONS


def _fallbacks():
    """Where the CLI is installed when PATH does not say.

    A tool installed by `uv tool install` or pipx lives in ~/.local/bin, and
    Homebrew's in /opt/homebrew/bin or /usr/local/bin. cron's minimal PATH
    includes none of them, and an unattended transfer is exactly what this
    engine is for.
    """
    return (str(Path.home() / ".local" / "bin" / "globus"),
            "/opt/homebrew/bin/globus", "/usr/local/bin/globus")


def find_cli():
    """The globus CLI to run, or None; the GLOBUS setting, when set, is the
    only candidate."""
    explicit = config.global_value("GLOBUS", "")
    candidates = ([explicit] if explicit else
                  [shutil.which("globus") or ""] + list(_fallbacks()))
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def cli():
    """The globus CLI binary, or die with the step that is missing."""
    found = find_cli()
    if found:
        return found
    explicit = config.global_value("GLOBUS", "")
    if explicit:
        ui.die(f"GLOBUS is set to {explicit}, which is not an executable file",
               "point it at the CLI: cluster config set GLOBUS /path/to/globus",
               *INSTALL)
    ui.die("the globus CLI is not installed", *(INSTALL + LOGIN + COLLECTIONS),
           "to use one that is not on PATH: cluster config set GLOBUS /path/to/globus")


def collection_for(endpoint):
    """The collection UUID for a backend: its setting, else what it declares."""
    uuid = endpoint.settings.str("GLOBUS_COLLECTION").strip()
    if uuid:
        return uuid
    declared = getattr(endpoint.backend, "globus_collection", "")
    if declared:
        return declared
    ui.die(
        f"no Globus collection known for {endpoint.backend.label}",
        f"set its UUID: cluster --{endpoint.name} config set GLOBUS_COLLECTION <uuid>",
        "  (find candidates with: globus endpoint search <site>)",
    )


#: What Globus says when authorization is missing, and what actually fixes it.
#: Both are *browser* remedies, which is the whole reason this engine is a poor
#: fit for unattended work — so each explanation says so.
CONSENT_MARKER = "requires you to grant consent"
SESSION_MARKER = "requires you to re-authenticate"


def auth_remedy(text, endpoint, uuid):
    """(problem, *hints) for an authorization failure, or None if not one.

    Globus reports two different things in similar language, and they need
    different commands: a *consent* is granted once per collection, while a
    *session policy* demands a recent authentication through a particular
    identity provider and expires again afterwards.
    """
    if CONSENT_MARKER in text:
        return (
            f"{endpoint.backend.label} needs a one-time data_access consent",
            "grant it (opens a URL you can paste a code back from):",
            f"  globus session consent --no-local-server "
            f"'urn:globus:auth:scope:transfer.api.globus.org:all"
            f"[*https://auth.globus.org/scopes/{uuid}/data_access]'",
        )
    if SESSION_MARKER in text:
        domain = _session_domain(text) or getattr(
            endpoint.backend, "globus_session_domain", "") or endpoint.name
        return (
            f"{endpoint.backend.label} enforces a session policy: a consent is "
            "not enough, the session must have authenticated recently through "
            f"{domain}",
            "re-authenticate (a browser step, and it expires again):",
            f"  globus session update --no-local-server {domain}",
            "this recurring browser step is why --engine direct, not globus, is "
            "the right choice for scheduled transfers",
        )
    return None


def _session_domain(text):
    """The domain Globus itself named in a session-reauthentication message."""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("globus session update"):
            parts = line.split()
            if len(parts) >= 4:
                return parts[3]
    return None


def _parent(path):
    """The directory whose listing holds *path*, or *path* for a directory
    named with a trailing slash."""
    return path if path.endswith("/") else (path.rsplit("/", 1)[0] or "") + "/"


def preflight(binary, endpoint, uuid, path):
    """Prove a collection is usable before a task is submitted, and return the
    listing that proved it: the entries of the directory holding *path*.

    Without this, an authorization problem is discovered *asynchronously*: the
    submission succeeds, the task fails minutes later, and `task wait` reports a
    non-zero exit with no explanation. One `ls` converts that into a
    synchronous message naming the exact fix.
    """
    probe = _parent(path)
    proc = plat.run([binary, "ls", "--format", "json", f"{uuid}:{probe}"])
    if proc.returncode == 0:
        try:
            listing = json.loads(proc.stdout or "{}").get("DATA")
        except (ValueError, AttributeError):
            listing = None
        return listing if isinstance(listing, list) else None
    text = (proc.stderr or "") + (proc.stdout or "")
    remedy = auth_remedy(text, endpoint, uuid)
    if remedy:
        ui.die(*remedy)
    # Not an authorization problem: report what Globus said, unembellished.
    ui.die(f"cannot read {endpoint.backend.label} at {probe}",
           text.strip()[-500:] or f"exit status {proc.returncode}",
           f"collection: {uuid}")


def source_is_dir(endpoint, path, listing):
    """Whether *path* is a directory, from the listing of the one it is in.

    A path ending in "/" says so itself. Otherwise the listing decides, and a
    name it does not hold is a source that is not there, which is better said
    now than by a task that fails minutes after it was accepted.
    """
    if path.endswith("/"):
        return True
    if listing is None:
        ui.die(f"could not read the Globus listing of {_parent(path)} on "
               f"{endpoint.backend.label}, so whether {path} is a file or a "
               "directory is unknown",
               "end the path with / if it is a directory")
    name = path.rsplit("/", 1)[-1]
    entry = next((row for row in listing
                  if isinstance(row, dict) and row.get("name") == name), None)
    if entry is None:
        ui.die(f"{path} does not exist on {endpoint.backend.label}",
               f"it is not in the Globus listing of {_parent(path)}")
    kind = entry.get("type")
    if kind == "invalid_symlink":
        ui.die(f"{path} on {endpoint.backend.label} is a symlink to nothing")
    return kind == "dir"


def _is_listed_dir(path, listing):
    """Whether *path* is a directory already: named with a trailing slash, or
    listed as one in the directory holding it."""
    if path.endswith("/"):
        return True
    name = path.rsplit("/", 1)[-1]
    return any(isinstance(row, dict) and row.get("name") == name
               and row.get("type") == "dir" for row in listing or ())


def logged_in(binary):
    proc = plat.run([binary, "whoami"])
    return proc.returncode == 0, (proc.stdout or proc.stderr or "").strip()


def check_path(endpoint):
    """The path a collection can actually serve, or die explaining why not."""
    example = getattr(endpoint.backend, "globus_path_example", "")
    if not endpoint.path.startswith("/"):
        ui.die(
            f"Globus needs an absolute path, got {endpoint!r}",
            "a collection's paths are relative to its own root, so '~' and "
            "relative paths cannot be resolved from here",
            f"give the full path — for {endpoint.backend.label}: {example}"
            if example else "give the full path",
        )
    # A collection is not the whole filesystem. Refusing here, by declaration,
    # beats waiting for the endpoint to answer 403 for a path that can never work.
    problem = endpoint.backend.globus_path_problem(endpoint.path)
    if problem:
        ui.die(
            f"Globus cannot reach {endpoint!r}", problem,
            f"use {example}" if example else "use an exported filesystem",
            "or move it with --engine direct, which goes over ssh and can see "
            "everything your shell can",
        )
    return endpoint.path


def run_cross(cross):
    """Submit *cross* to Globus. Returns a process exit code."""
    binary = cli()
    ok, detail = logged_in(binary)
    if not ok:
        ui.die("not logged in to Globus", detail, *(LOGIN + COLLECTIONS))

    source, dest = cross.source, cross.dest
    src_path = check_path(source)
    dst_path = check_path(dest)
    src_uuid = collection_for(source)
    dst_uuid = collection_for(dest)

    if cross.operation == "move":
        ui.die(
            "--move is not supported over Globus",
            "Globus has no atomic move; doing it by hand means deleting the "
            "source after a transfer this tool did not verify itself",
            "transfer first, check the task, then delete deliberately",
        )
    if cross.symlinks != "follow":
        flag = "-l" if cross.symlinks == "keep" else "--skip-symlinks"
        ui.die(
            f"{flag} is not available over Globus",
            "Globus copies what a symlink points to, as -L does, and has no "
            "setting that keeps links or leaves them out",
            "use --engine direct or --engine relay, which can",
        )

    # Prove both sides are readable *now*. A submitted task fails asynchronously,
    # so without this an expired session policy looks like a task that simply died.
    listing = preflight(binary, source, src_uuid, src_path)
    into = _is_listed_dir(dst_path, preflight(binary, dest, dst_uuid, dst_path))
    is_dir = cross.contents or source_is_dir(source, src_path, listing)
    src_path, dst_path, recursive = plan_paths(src_path, dst_path, cross.contents,
                                               is_dir, into)

    argv = [binary, "transfer", f"{src_uuid}:{src_path}", f"{dst_uuid}:{dst_path}",
            "--label", _label(cross), "--format", "json"]
    if recursive:
        argv.append("--recursive")
    if cross.operation == "sync":
        # checksum is the only sync level that cannot silently keep a corrupted
        # or truncated destination file.
        argv += ["--sync-level", "checksum", "--delete-destination-extra"]
    argv += cross.extra

    if cross.dry_run:
        ui.info("would submit: " + shlex.join(argv))
        return 0

    if cross.operation == "sync" and not cross.quiet:
        ui.warn("--sync over Globus DELETES files at the destination that are "
                "not in the source")

    proc = plat.run(argv)
    if proc.returncode != 0:
        text = (proc.stderr or "") + (proc.stdout or "")
        for endpoint, uuid in ((source, src_uuid), (dest, dst_uuid)):
            remedy = auth_remedy(text, endpoint, uuid)
            if remedy:
                ui.die(*remedy)
        ui.die("globus transfer was rejected", text.strip()[-600:])

    task_id = _task_id(proc.stdout)
    if not task_id:
        ui.warn("could not read a task id from the globus response")
        print((proc.stdout or "").strip())
        return 0

    ui.info(f"globus task {task_id} submitted")
    ui.note(f"watch it:  globus task show {task_id}")
    ui.note(f"cancel it: globus task cancel {task_id}")
    if cross.keep:
        # Fire and forget: the task id is the handle, and the transfer proceeds at
        # Globus whatever happens here. This is the right shape for a cron job —
        # it cannot hang, and the id goes in the log to be checked later.
        return 0

    # No --timeout: it means "wait N seconds", *not* "no limit", so `--timeout 0`
    # returns at once, with "Task has yet to complete after 0 seconds" and a
    # failure status, for a transfer that goes on to succeed. Omitted, it waits
    # for a terminal state and exits 0 only if the task actually succeeded.
    ui.info("waiting for the task to finish (safe to interrupt; the transfer "
            "continues at Globus)")
    rc = subprocess.run([binary, "task", "wait", task_id]).returncode
    if rc != 0:
        # The task, not this command, holds the reason — and it outlives us, so
        # point at it rather than returning a bare status.
        ui.warn(f"globus task {task_id} did not succeed")
        ui.note(f"why:     globus task show {task_id}")
        ui.note(f"details: globus task event-list {task_id}")
        ui.note("a partial transfer can be resumed by submitting it again: "
                "Globus skips what already matches")
    return rc


def plan_paths(src_path, dst_path, contents, is_dir, dest_is_dir=False):
    """(source, destination, recursive) under this tool's cp-like rule.

    Globus has no "contents of" subtlety: a recursive transfer always puts the
    *contents* of the source directory into the destination directory. So landing
    SRC *inside* DEST — which is what every other engine here does, and what `cp`
    does — has to be spelled out by extending the destination path. A file is
    written to the path it is given, so a file sent into a directory
    (*dest_is_dir*) is given its own name there. *is_dir* is what the
    collection's listing said the source is.
    """
    recursive = contents or src_path.endswith("/") or is_dir
    name = src_path.rstrip("/").rsplit("/", 1)[-1]
    if (recursive and not contents) or (not recursive and dest_is_dir):
        dst_path = dst_path.rstrip("/") + "/" + name
    return src_path, dst_path, recursive


def _label(cross):
    label = f"cluster {cross.source.name}-{cross.dest.name}"
    return label[:128]


def _task_id(stdout):
    try:
        return json.loads(stdout or "{}").get("task_id")
    except ValueError:
        return None
