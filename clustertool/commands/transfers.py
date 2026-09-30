"""External SSH, rsync, rclone, and cross-cluster transfer commands."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import tempfile
import unicodedata
from pathlib import Path

from .. import platform as plat, registry, ui
from ..command import command
from ..remote_sh import remote_path
from ..riding import Link, Ride
from ..sshmux import rclone_ssh_value
from ..transfer import channel_ssh

#: rclone flags that take a *separate* value. They have to be known, because
#: splitting "flags" from "paths" by a leading dash otherwise tears the value off
#: and feeds it to rclone as a path: `--exclude '*.log'` would become a third
#: positional argument, silently corrupting both the filter and the path list.
#: (The `--flag=value` form needs no help — it is a single token.)
RCLONE_VALUE_FLAGS = frozenset("""
    --backup-dir --bwlimit --checkers --compare-dest --config --contimeout
    --copy-dest --exclude --exclude-from --files-from --filter --filter-from
    --include --include-from --log-file --log-level --low-level-retries
    --max-age --max-backlog --max-depth --max-size --max-transfer --min-age
    --min-size --modify-window --multi-thread-streams --order-by --retries
    --sftp-connections --stats --suffix --timeout --transfers
""".split())


#: rsync options that treat a file cut short their own way, which rsync will
#: not combine with --partial-dir.
RSYNC_OWN_PARTIALS = frozenset(("--inplace", "--append", "--append-verify",
                                "--write-devices", "--partial-dir"))

#: rsync options that name a file to read, which "-" makes standard input.
RSYNC_FILE_OPTIONS = frozenset(("--files-from", "--exclude-from", "--include-from",
                                "--read-batch"))

#: The names under which rsync reads a file that is standard input.
STDIN_NAMES = frozenset(("-", "/dev/stdin", "/dev/fd/0", "/proc/self/fd/0"))

#: A filter rule that merges rules from a file: ". FILE", "merge FILE".
_MERGE_RULE = re.compile(r"^(dir-merge|merge|[.:])[^ _]*[ _](\S+)$")


def _merges_stdin(rule):
    merge = _MERGE_RULE.match(rule.strip())
    return bool(merge) and merge.group(2) in STDIN_NAMES


def rsync_reads_stdin(extra):
    """The rsync option among *extra* that has rsync read standard input (a
    file that is it, as in --files-from=- or --files-from=/dev/stdin, or a
    filter rule merging from it), or None. Values here are attached
    (``--opt=VALUE``, ``-fRULE``): push and pull take a separate one for a
    path."""
    for token in extra:
        name, eq, value = token.partition("=")
        if eq and value in STDIN_NAMES and name in RSYNC_FILE_OPTIONS:
            return name
        if eq and name == "--filter" and _merges_stdin(value):
            return name
        if token.startswith("-f") and not token.startswith("--") \
                and _merges_stdin(token[2:]):
            return "-f"
    return None


def need_rsync(what):
    """Stop before any connection is opened when rsync is not installed here.

    Found missing only after authenticating, it would cost a TOTP window on
    FASRC to learn something this machine knew all along.
    """
    if shutil.which("rsync"):
        return
    ui.die(f"{what} needs rsync, which is not installed on this machine",
           "install it: apt install rsync, dnf install rsync or brew install rsync")


def split_rclone_args(extra):
    """Split leftover argv into (paths, rclone flags), keeping values attached.

    Anything unrecognised that looks like a flag still goes to rclone untouched,
    which is what makes boolean flags like --checksum work without an escape
    hatch; the set above only says which ones swallow the token after them.
    """
    positional, passthrough = [], []
    index = 0
    while index < len(extra):
        token = extra[index]
        if token.startswith("-"):
            passthrough.append(token)
            if token in RCLONE_VALUE_FLAGS and index + 1 < len(extra):
                passthrough.append(extra[index + 1])
                index += 2
                continue
            if token in RCLONE_VALUE_FLAGS:
                ui.die(f"{token} needs a value")
        else:
            positional.append(token)
        index += 1
    return positional, passthrough


@command("ssh-command", help="Print an ssh command line for an external tool "
                            "(e.g. rclone --sftp-ssh)")
def cmd_ssh_command(ctx, args):
    """Hand another program a ready-to-use transport.

    This is the seam that keeps tools like archive-sync out of the connection
    business: they ask for a command line and get one, without knowing about
    control masters, jump hosts, certificates or which node class is right for
    bulk I/O.

    It prints shell words, for a shell to run. rclone's --sftp-ssh splits its
    value its own way, so --rclone prints the same words written for that.
    """
    parser = argparse.ArgumentParser(prog="cluster ssh-command", add_help=False)
    parser.add_argument("login", nargs="?")
    parser.add_argument("--transfer", action="store_true",
                        help="use a dedicated transfer connection on an I/O node "
                             "rather than a login's master")
    parser.add_argument("--node",
                        help="use this node instead of the usual choice")
    parser.add_argument("--quiet", action="store_true",
                        help="no progress or status output")
    parser.add_argument("--rclone", action="store_true",
                        help="print it the way rclone's --sftp-ssh reads it "
                             "rather than as shell words")
    opts = parser.parse_args(args)

    if opts.transfer:
        from ..transfer import Transfers

        xfer = Transfers(ctx.logins)
        # The same connection a transfer opens: the pool's, on whichever
        # transfer node answers, unless a node was asked for.
        node = ctx.backend.fqdn(opts.node) if opts.node else None
        already_open = set(xfer.active_tags())
        tag = xfer.open_connection(node=node, quiet=True)
        # The caller keeps using the connection after this process exits, so
        # the lease is held for the caller: a transfer that ends meanwhile
        # leaves it open. `cluster transfer --close TAG` from the same caller
        # releases the lease and closes it, and the tag is named here so the
        # caller closes this one and no other.
        xfer.lease_take_for_caller(tag)
        xfer.lease_drop(tag)
        sock = ctx.state.xfer_socket(tag)
        node = node or xfer.node_of(tag)
        if not opts.quiet:
            ui.info(f"{'reusing' if tag in already_open else 'opened'} "
                    f"transfer connection {tag}")
            ui.note(f"close it when done: cluster {ctx.backend.cli_flag()} "
                    f"transfer --close {tag}")
    else:
        name = ctx.login(opts.login)
        ctx.logins.ensure(name, quiet=opts.quiet)
        sock = ctx.state.socket(name)
        node = ctx.logins.node_of(name)

    parts = channel_ssh(ctx.backend, ctx.settings, sock, node)
    print(rclone_ssh_value(parts) if opts.rclone
          else " ".join(shlex.quote(p) for p in parts))
    return 0


@command("push", help="rsync a local path to the cluster over a login's master")
def cmd_push(ctx, args):
    return _rsync(ctx, args, up=True)


@command("pull", help="rsync a remote path from the cluster over a login's master")
def cmd_pull(ctx, args):
    return _rsync(ctx, args, up=False)


def _rsync(ctx, args, up):
    parser = argparse.ArgumentParser(prog="cluster push/pull", add_help=False)
    parser.add_argument("first")
    parser.add_argument("second", nargs="?")
    parser.add_argument("third", nargs="?")
    opts, extra = parser.parse_known_args(args)
    verb = "push" if up else "pull"
    if opts.second is None:
        ui.die("need a source and a destination",
               f"usage: cluster {verb} [LOGIN] SRC DEST")
    need_rsync(f"cluster {verb}")
    if opts.third is not None:
        name, source, dest = ctx.login(opts.first), opts.second, opts.third
    else:
        name, source, dest = ctx.login(), opts.first, opts.second
    ctx.logins.ensure(name)
    if not up and plat.IS_MAC:
        _refuse_case_clashes(ctx, name, source, dest)

    def restore(_lost_pid):
        ctx.logins.restore(name)
        return ctx.logins.node_of(name)

    link = Link(f"login '{name}'", ctx.state.socket(name), restore, ctx.logins,
                ctx.logins.node_of(name))

    own_partials = any(token.split("=", 1)[0] in RSYNC_OWN_PARTIALS for token in extra)
    partial = [] if own_partials else plat.rsync_partial_flags()

    def argv():
        *ssh, host = channel_ssh(ctx.backend, ctx.settings, link.sock, link.node)
        remote_shell = " ".join(shlex.quote(p) for p in ssh)
        ends = [source, f"{host}:{dest}"] if up else [f"{host}:{source}", dest]
        return ["rsync", "-a", plat.rsync_progress_flag(), *partial,
                "-e", remote_shell, *extra, *ends]

    # A lost connection is restored and rsync run again, which skips what
    # arrived whole and finishes a file it had begun (riding.Ride), unless
    # rsync read standard input, which the next run would find empty.
    reader = rsync_reads_stdin(extra)
    once = (f"its {reader} was standard input, which a second run would find "
            "empty" if reader else None)
    return Ride([link], ctx.settings, once=once).run(argv)


#: Marks where the listing starts, past anything a login shell prints first.
_NAMES_MARKER = "__cluster_names__"


def _refuse_case_clashes(ctx, name, source, dest):
    """Stop a pull that a case-folding destination would quietly merge.

    rsync writes `README` and then `readme` onto one file there and says
    nothing: the second replaces the first. So the source is listed before
    anything moves, and a pull that would lose a file that way is refused,
    naming the names. A listing that cannot be had skips the check, not the
    pull, and a listing cut short checks what it got.

    The listing takes as long as the tree does: a big one is slow to walk,
    not stuck, and a connection that dies ends it through the master's
    keepalives.
    """
    if not _folds_case(dest):
        return
    proc = ctx.logins.run_remote(
        name, f"printf '%s\\0' {_NAMES_MARKER} && "
              f"find {remote_path(source)} -print0", timeout=None)
    text = proc.stdout or ""
    start = _NAMES_MARKER + "\0"
    said = (proc.stderr or "").strip().splitlines()
    why = said[-1] if said else f"exit status {proc.returncode}"
    if start not in text:
        ui.warn(f"could not list {source} to look for names that differ only "
                f"by case ({why}); pulling without that check")
        return
    clashes = case_clashes(text.split(start, 1)[1].split("\0"))
    if clashes:
        shown = [" and ".join(group) for group in clashes[:10]]
        if len(clashes) > len(shown):
            shown.append(f"and {len(clashes) - len(shown)} more")
        ui.die(f"{dest} is on a file system that ignores case, and {source} "
               "holds names that differ only by case", *shown,
               "rename them on the cluster, or pull onto a case-sensitive volume")
    if proc.returncode != 0:
        # find lists what it can and exits non-zero for what it cannot read,
        # which rsync will not be able to read either.
        ui.warn(f"listed only part of {source} to look for names that differ "
                f"only by case ({why}); the rest is pulled unchecked")


def case_clashes(paths):
    """Groups of *paths* that one case-folding file system stores as one name.

    macOS ignores Unicode normalization as well as case, so both are folded.
    A listing names every directory as well as every file, so two spellings
    of one directory are a clash too: their contents would be merged.
    """
    groups = {}
    for path in paths:
        if path:
            key = unicodedata.normalize("NFC", path).casefold()
            groups.setdefault(key, set()).add(path)
    return sorted(sorted(group) for group in groups.values() if len(group) > 1)


def _folds_case(path):
    """Whether the file system under *path* stores `a` and `A` as one name.

    Asked of the file system itself, with a probe file, at the nearest
    directory that exists: a Mac can have case-sensitive volumes too.
    """
    directory = Path(path).expanduser()
    while not directory.is_dir():
        if directory.parent == directory:
            return False
        directory = directory.parent
    try:
        handle, probe = tempfile.mkstemp(prefix=".cluster-Case-", dir=str(directory))
    except OSError:
        return False        # unwritable: rsync will say so itself
    os.close(handle)
    try:
        return os.path.exists(os.path.join(str(directory),
                                           os.path.basename(probe).swapcase()))
    finally:
        os.unlink(probe)


@command("transfer", "xfer", "copy", "cp", help="Move bulk data with rclone over sftp")
def cmd_transfer(ctx, args):
    from .. import crossxfer
    from ..transfer import Transfers

    xfer = Transfers(ctx.logins)

    if "--close" in args:
        return _close_connections(xfer, _close_parser().parse_args(args))

    parser = argparse.ArgumentParser(prog="cluster transfer", add_help=False)
    parser.add_argument("--via",
                        help="ride LOGIN's existing connection instead of opening one")
    parser.add_argument("--node",
                        help="use this node instead of the usual choice")
    parser.add_argument("--engine", choices=crossxfer.ENGINES, default="auto",
                        help="cluster-to-cluster only: who moves the bytes "
                             "(direct = on a cluster, relay = through this "
                             "machine, globus = the Globus service)")
    parser.add_argument("--executor",
                        help="cluster-to-cluster only: which side runs the "
                             "transfer")
    parser.add_argument("--peer-node",
                        help="cluster-to-cluster only: which host on the far "
                             "cluster the executor should dial")
    parser.add_argument("--no-agent-forward", action="store_true",
                        help="never lend a credential to a cluster; forces relay")
    parser.add_argument("--keep", action="store_true",
                        help="leave the transfer connection open afterwards")
    parser.add_argument("--contents", action="store_true",
                        help="copy what is inside the directory, not the directory")
    parser.add_argument("--up", action="store_true",
                        help="force the direction: local -> cluster")
    parser.add_argument("--down", action="store_true",
                        help="force the direction: cluster -> local")
    parser.add_argument("--move", action="store_true",
                        help="delete each source file once it has arrived")
    parser.add_argument("--sync", action="store_true",
                        help="make the destination match the source, deleting extras")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="say what would happen, change nothing")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="no progress output")
    parser.add_argument("-P", "--progress", action="store_true",
                        help="show per-file progress")
    parser.add_argument("-L", dest="symlinks", action="store_const", const="follow",
                        help="follow symlinks and copy what they point at")
    parser.add_argument("-l", dest="symlinks", action="store_const", const="keep",
                        help="copy symlinks as symlinks")
    parser.add_argument("--skip-symlinks", dest="symlinks",
                        action="store_const", const="skip",
                        help="ignore symlinks entirely")
    parser.add_argument("--transfers", type=int, default=0,
                        help="how many files to copy at once")
    parser.add_argument("--checkers", type=int, default=0,
                        help="how many files to compare at once")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="do not ask before --sync deletes files")
    opts, extra = parser.parse_known_args(args)

    positional, passthrough = split_rclone_args(extra)
    if len(positional) < 2:
        ui.die("need a source and a destination",
               "usage: cluster transfer [OPTIONS] SRC DEST")
    # Several sources into one destination, as cp and rsync take them — a shell
    # glob makes that the ordinary way to type this. Keeping only the last two
    # names would make a glob a quiet loss: `cluster transfer out_*.json work:d/`
    # would copy the last file, drop the rest without a word, and exit 0.
    sources, dest = positional[:-1], positional[-1]

    operation = "sync" if opts.sync else ("move" if opts.move else "copy")
    if opts.sync and not opts.dry_run:
        ui.warn("--sync DELETES files at the destination that are not in the source")
        if not ui.confirm("continue?", opts.yes,
                          "pass -y to let --sync delete files unattended"):
            ui.die("aborted")

    # Sources that name different clusters are a grouping problem, not an
    # error: one connection serves one cluster, so the sources are gathered by
    # the cluster they name and run one group after another. The user typed one
    # command and gets one result; the order they typed is the order things
    # move in.
    order, grouped = [], {}
    for source in sources:
        named = crossxfer.split_endpoint(source)[0]
        if named not in grouped:
            order.append(named)
            grouped[named] = []
        grouped[named].append(source)

    done = 0
    for named in order:
        rc = _transfer_from(ctx, grouped[named], dest, opts, passthrough,
                            operation, many=len(sources) > 1)
        if rc != 0:
            if len(order) > 1:
                ui.warn(f"stopped after {done} of {len(order)} clusters; "
                        f"{named or 'the local'} source(s) did not finish")
            return rc
        done += 1
    return 0


def _transfer_from(ctx, sources, dest, opts, passthrough, operation, many):
    """One cluster's worth of sources, into the one destination."""
    from .. import crossxfer
    from ..transfer import TransferSpec, Transfers

    xfer = Transfers(ctx.logins)

    # Both sides on one cluster: rclone would read every byte down to this
    # machine and write it straight back to where it came from. The cluster can
    # do the whole thing without moving anything off itself, so ask it to.
    dst_name, dst_path = crossxfer.split_endpoint(dest)
    if dst_name and all(crossxfer.split_endpoint(s)[0] == dst_name
                        for s in sources):
        return _copy_on_cluster(ctx, sources, dest, dst_name, dst_path, opts,
                                passthrough, operation)

    # Both sides naming a different cluster is a different kind of transfer:
    # neither side is this machine, so something else has to move the bytes.
    # rclone drives both remotes itself, so several sources are several runs of
    # that, one after another, rather than a refusal.
    if any(crossxfer.is_cross(s, dest) for s in sources):
        for index, source in enumerate(sources):
            rc = crossxfer.CrossTransfer(
                source, dest,
                engine="relay" if opts.no_agent_forward else opts.engine,
                executor=opts.executor, operation=operation,
                contents=opts.contents,
                symlinks=opts.symlinks or "follow", dry_run=opts.dry_run,
                progress=opts.progress, quiet=opts.quiet,
                keep=opts.keep or index + 1 < len(sources),
                transfers=opts.transfers, checkers=opts.checkers,
                extra=passthrough,
                allow_agent=not opts.no_agent_forward, peer_node=opts.peer_node,
                executor_node=opts.node, via=opts.via,
            ).run()
            if rc != 0:
                if len(sources) > 1:
                    ui.warn(f"stopped after {index} of {len(sources)} sources")
                return rc
        return 0

    # A cluster-qualified path names its cluster whichever side it is on, so
    # `nersc:~/x ./here` is a download from NERSC without needing --nersc.
    qualified, owners = [], set()
    for source in sources:
        source, dest, named = _qualified_sides(source, dest)
        qualified.append(source)
        if named:
            owners.add(named)
    named = next(iter(owners), None)

    # A login name is global, so --via must resolve to the backend that actually
    # owns it. Otherwise `--via work` under the default backend would
    # authenticate to the default cluster and create a second login called
    # 'work' there.
    via_owner = registry.find(opts.via) if opts.via else None
    if named and via_owner and named != via_owner:
        ui.die(f"this transfer names {named} but --via {opts.via} is a "
               f"{via_owner} login",
               "a transfer rides a connection to the cluster it names")
    owner = named or via_owner
    if owner and owner != ctx.backend.name:
        if ctx.explicit_backend:
            ui.die(f"this transfer names {owner}, not {ctx.backend.name}",
                   "drop the backend flag: a qualified path names its own cluster")
        ctx = type(ctx)(owner, explicit=False)
        xfer = Transfers(ctx.logins)

    up = True if opts.up else (False if opts.down else None)
    specs = [TransferSpec(
        ctx.backend, source, dest, up=up, contents=opts.contents,
        operation=operation, symlinks=opts.symlinks or "follow",
        dry_run=opts.dry_run, progress=opts.progress or plat.terminal_attached(),
        quiet=opts.quiet, keep=opts.keep, via=opts.via,
        node=ctx.backend.fqdn(opts.node) if opts.node else None,
        transfers=opts.transfers, checkers=opts.checkers, extra=passthrough,
        dest_is_dir=many,
    ) for source in qualified]
    return xfer.run_all(specs, quiet=opts.quiet)


def _copy_on_cluster(ctx, sources, dest, cluster, dest_path, opts, passthrough,
                     operation):
    """Copy within one cluster by running the copy there.

    Only a plain copy is done this way. ``cp`` cannot express a filter, and
    --sync and --move delete things: doing either from a translated command
    line would put a guess in charge of removing the user's data.
    """
    from .. import crossxfer

    paths = []
    for source in sources:
        _named, path = crossxfer.split_endpoint(source)
        inside = opts.contents or path.rstrip().endswith("/")
        paths.append(f"{path.rstrip('/')}/." if inside else path.rstrip("/"))

    if operation != "copy" or passthrough:
        blocked = ("--sync and --move delete files, so they are not translated"
                   if operation != "copy" else
                   "filters and rclone flags do not translate to cp")
        tool = {"move": "mv", "sync": "rsync -a --delete"}.get(operation, "cp -a")
        # Words for the user's own shell: remote_path leaves no ~ for it to
        # expand to a home on this machine.
        ui.die(f"both sides of this transfer are on {cluster}",
               f"copying within one cluster runs there, not through here, and "
               f"{blocked}",
               "run it there: cluster run LOGIN -- "
               f"{tool} {' '.join(remote_path(one) for one in paths)} "
               f"{remote_path(dest_path)}")

    # Several sources, or a named directory, means the destination is one --
    # and cp will not create it.
    into_dir = len(paths) > 1 or dest.rstrip().endswith("/") or opts.contents
    target = remote_path(dest_path)
    steps = []
    if into_dir:
        steps.append(f"mkdir -p {target}")
    steps.append("cp -a " + " ".join(remote_path(one) for one in paths)
                 + " " + target)
    script = " && ".join(steps)

    if opts.dry_run:
        ui.info(f"would run on {cluster}: {script}")
        return 0
    if not opts.quiet:
        ui.info(f"copy on {cluster}: {script}")
    name, _rest = ctx.resolve_login([opts.via] if opts.via else [])
    ctx.logins.ensure(name)
    # cp on one filesystem is its own delivery check: it reports a non-zero
    # status for anything it could not write, and nothing crosses a network
    # that could truncate it.
    return ctx.logins.run_remote(name, script, timeout=None,
                                 capture=False).returncode


def _qualified_sides(source, dest):
    """Turn a cluster-qualified path into the remote side of a transfer.

    ``nersc:~/x`` and ``fasrc:~/x`` name a cluster wherever they appear, so the
    same word means the same thing in a one-cluster transfer as in a
    cluster-to-cluster one. At most one side names a cluster by the time this
    runs: both on one cluster is a copy there, and two clusters is a
    cluster-to-cluster transfer. Returns (source, dest, cluster or None).
    """
    from .. import crossxfer

    src_name, src_path = crossxfer.split_endpoint(source)
    dst_name, dst_path = crossxfer.split_endpoint(dest)
    if src_name:
        return f"remote:{src_path}", dest, src_name
    if dst_name:
        return source, f"remote:{dst_path}", dst_name
    return source, dest, None


def _close_parser():
    parser = argparse.ArgumentParser(prog="cluster transfer --close",
                                     add_help=False)
    parser.add_argument("--close", nargs="*", metavar="TAG",
                        help="close each TAG named, or every open transfer "
                             "connection, and exit")
    parser.add_argument("--force", action="store_true",
                        help="with --close: close even while other runs use it")
    return parser


def _close_connections(xfer, opts):
    """Close the transfer connections *opts* names, or every open one."""
    open_now = xfer.active_tags()
    wanted = opts.close or open_now
    closed = 0
    for tag in wanted:
        if tag not in open_now:
            ui.say(f"no open transfer connection {tag}")
            continue
        # What `ssh-command --transfer` claimed for this caller is the
        # caller's own, not another run's: closing it is what the caller asked.
        xfer.lease_drop_for_caller(tag)
        if xfer.close_connection(tag, force=opts.force):
            ui.info(f"closed transfer connection {tag}")
            closed += 1
    if not opts.close and not closed:
        ui.say("no open transfer connections")
    return 0
