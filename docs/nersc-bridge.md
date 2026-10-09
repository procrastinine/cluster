# The NERSC bridge

> **Experimental.** The bridge is built for one direction: code running on a
> FASRC login node (the *hub*) driving NERSC Perlmutter. It has not been tried
> with other sites, and its interface may change.

The bridge makes NERSC look like one more Slurm backend to anything running on
the hub: a batch pipeline, a watcher job, or a coding agent in a tmux session.
It has two halves:

- `cluster bridge push`, on your workstation, keeps a short-lived NERSC
  credential and the companion installed on the hub.
- `nersc`, the *companion* (`remote/nersc` in this repository), runs on the hub
  and talks to NERSC: Slurm commands, file movement through NERSC's data
  transfer nodes, a one-way code mirror, and a loop that brings finished jobs'
  run directories back to the hub.

## Why it is shaped this way

- **Only your workstation can authenticate to NERSC.** The sshproxy exchange
  needs your password and TOTP seed, and those never leave the workstation.
  What the hub receives is the 24-hour key and certificate that sshproxy
  returns, so a push has to happen at least once a day.
- **NERSC cannot dial FASRC.** NERSC login nodes cannot open connections to
  FASRC, so the hub always initiates: it pulls results rather than waiting for
  NERSC to push them.
- **NERSC's topology.** Login nodes are firewalled and are reached by tunnelling
  through `perlmutter.nersc.gov`. The data transfer nodes (DTNs) are directly
  routable, so bulk data goes through them and never spends login-connection
  channels.

## Security model

`cluster bridge push` writes to the hub, under your home there:

```
~/.ssh/nersc-bridge            the 24-hour sshproxy private key   (mode 600)
~/.ssh/nersc-bridge-cert.pub   its certificate                    (mode 600)
~/.ssh/known_hosts             + NERSC's @cert-authority line
~/.local/bin/nersc             the companion                      (mode 755)
~/.config/nersc/config         its configuration (only if missing)
~/.config/nersc/mirror.exclude code-mirror excludes (only if missing)
```

It never copies your password or TOTP seed. Anyone who can read your files on
the hub, including its administrators, can use the key until the certificate
expires. That exposure is the price of letting jobs and agents on the hub work
without you. If it is not acceptable for your account, do not use the bridge.

## Prerequisites

- **On the workstation:** working NERSC credentials (`cluster --nersc config
  credentials`), and `rsync`, which ships the files. A push checks for rsync
  before it fetches a certificate or contacts the hub, and says how to install
  it. The hub sets each file's mode after the copy, so the `rsync` macOS ships
  is enough.
- **On the hub:** `python3` 3.6 or newer as the system Python, plus `rsync` and
  the OpenSSH client, which login nodes have. The companion and any hooks file
  use only the standard library.
- **A working FASRC login** in `cluster`, here called `work`. The bridge
  installs everything over that login's connection, and opens it first if it
  is down.

## Setting it up

1. Push:

   ```bash
   cluster bridge push work
   ```

   This fetches a fresh certificate if the current one has less than
   `BRIDGE_MIN_CERT_LEFT` seconds (default 72000, that is 20 hours) left,
   installs the files above over `work`'s connection, and verifies the result
   by running `nersc run true` on the hub. `--no-verify` skips the check, and
   `--force` fetches a new certificate regardless.

   A copy has no time limit: rsync reports its progress, and only one that
   reports nothing for `TRANSFER_IO_TIMEOUT` seconds (default 120) is a
   stalled connection, where the push stops and suggests
   `cluster refresh work`. The push is recorded as soon as the files are in
   place, and the check marks the record verified when it passes. The check
   opens a connection of its own from the hub to NERSC, so it waits
   `BRIDGE_VERIFY_TIMEOUT` seconds (default 300) for an answer; when that
   runs out, the files are already in place and the message says how to
   check again.

2. Configure the companion on the hub. The first push writes
   `~/.config/nersc/config` as a commented template of every setting: `user`,
   `key` and `scratch` are filled in, and every other setting is commented out
   with an example value and a one-line explanation. Edit it there; no push
   changes it once it exists. On the hub, `nersc config` prints every
   effective value, marks the ones that are defaults, and names any key in the
   file that the companion does not read.

3. Keep it fresh from cron on the workstation. Every 8 hours keeps at least 16
   hours of validity on the hub. Cron's `PATH` does not include `~/.local/bin`,
   so give the full path (under `/Users` on macOS):

   ```
   20 1,9,17 * * * /home/user/.local/bin/cluster bridge push --cron
   ```

   `--cron` skips a run while another is still going, naming it, and it does
   not stop at one login: it tries `BRIDGE_LOGIN`, then the hub cluster's
   default login, then every other login `cluster` knows there, until a push
   succeeds. The home directory is shared across the hub's login nodes, so any
   of them lands the same files. The certificate is fetched once per run,
   before any login is tried (with `--force`, fetched afresh once). A login
   that fails because FASRC refused your credentials ends the run instead:
   every other login would repeat the refusal, and repeated refusals are what
   lock an account.
   On a Mac, cron skips a run whose time passes while the machine sleeps; a
   LaunchAgent with `StartCalendarInterval` runs it on wake instead (see the
   example in [setup.md](setup.md#macos-launchagents)).

4. Check both sides:

   ```bash
   cluster bridge status            # on the workstation
   nersc doctor                     # on the hub
   ```

   `cluster bridge status` never authenticates. It reports the local
   credential, the last push (and whether its check passed; a push that was
   not verified says so and gives the command that checks it) and where each
   hub's companion stands. When the
   hub login already has a live connection, it also shows, through that
   connection, the certificate installed on the hub, the companion's version
   and its effective configuration.

## The companion

`nersc` is a single Python file that runs under the hub's system `python3` (3.6
or newer) with the standard library only. On each hub node it keeps one
multiplexed SSH connection to NERSC and opens it on demand.

| Verb | What it does |
|---|---|
| `sbatch`, `squeue`, `sacct`, `scancel`, `sinfo`, `scontrol`, `sstat`, `sprio`, `sshare`, `sqs` | passed through to NERSC verbatim; `squeue` alone means `squeue --me` |
| `q`, `jobs` | a compact `squeue --me` |
| `run [--cd DIR] CMD...` | run an argv on a NERSC login node; nothing is expanded there |
| `sh 'LINE'` | run a line under `bash -lc`, for `$PSCRATCH`, `~`, pipes and globs |
| `shell` | an interactive login shell |
| `push SRC DEST`, `pull SRC DEST` | rsync between the hub and NERSC through a DTN, or over the login connection when no DTN answers; NERSC paths are relative to your NERSC home, and rsync options pass through (`--exclude PAT` and `--exclude=PAT` alike). A lost connection is resumed (see `connect_retries`) |
| `sync [--dry-run]` | mirror `mirror_src` to `mirror_dest`, one way, with `--delete`; other rsync options pass through |
| `submit ...` | sync, then run a command in the mirror; see below |
| `sbatch --return ...`, `track`, `untrack`, `reap`, `fetch`, `peek` | the job return loop; see below |
| `status [--quick]` | the certificate, the connection, your jobs and the mirror's age; `--quick` asks nothing of NERSC |
| `doctor` | probes connectivity end to end, checks the configuration, and names a scratch storage target that has stopped answering (see below) |
| `config` | the effective configuration, key by key |
| `cert` | certificate validity |
| `connect`, `disconnect` | explicit control of the connection (normally automatic) |
| `--version`, `--help` | the companion's version; the verb list |

```bash
nersc sbatch job.slurm
nersc q
nersc sh 'ls $PSCRATCH/runs | tail'
nersc push ./inputs/ '$PSCRATCH/inputs/'
```

Quote `$PSCRATCH`, `$SCRATCH` and `$CFS` so the hub's shell leaves them alone.
At the start of a NERSC path, the companion substitutes them from its
configuration, and a leading `~/` means your NERSC home.

Nothing the companion runs remotely has a deadline, but none may go silent: a
non-interactive remote command that prints nothing for `no_progress_seconds`
(180) is taken to be stuck on NERSC storage, reported, and abandoned. 0 turns
abandonment off, and `NERSC_NO_PROGRESS_SECONDS` sets the window for one run.
Abandoning stops only the hub's end, so for a command whose output streams
straight to you (`run`, `sh`, `submit`, the Slurm verbs), the report adds that
it may still be running on NERSC, and how to look before running it again:

```
  remote  : the command may still be running on NERSC; before
            running it again, look: nersc q, or
            nersc sh 'ps -fu $USER'
```

SIGTERM, SIGHUP and Ctrl-C stop the companion cleanly: an rsync it is running
is asked to stop (SIGTERM, then SIGKILL after 10 seconds), so that it keeps a
file cut short for the next run to finish, and it exits with `nersc: stopped
by SIGTERM` (status 143), `SIGHUP` (129) or `Ctrl-C` (130). Under `nohup`,
SIGHUP changes nothing.

A `push`, `pull` or `sync` whose connection is lost is run again, on another
DTN first if one answers, and what arrived whole is not sent again. A file cut
short waits in `.rsync-partial` beside it in the destination for the next run
to finish, unless the options already say how to treat one (`--inplace`,
`--append`, a `--partial-dir` of their own). rsync leaves that directory out of
the transfer and out of `--delete`. An rsync without `--partial-dir` keeps the
file in place (`--partial`), and one whose `rsync --help` fails or complains,
as openrsync may, is asked for neither. Two kinds of transfer are not run again. One
that NERSC refused (ssh's `Permission denied` and the like, which the companion
passes on as ssh said it) would only be refused again. One that read standard
input (`--files-from=-`, `--exclude-from=/dev/stdin`, a filter merging `-`)
would find it empty the second time: no list of files, or, under `--delete`, no excludes
to protect what they named.

Mirror syncs on one hub node take turns, since two `--delete` rsyncs into the
same mirror trip over each other. A sync that finds another running waits for
as long as that one runs, and every 60 seconds says so:

```
nersc: waiting for another mirror sync on this node (pid 4242) to finish (60s so far)
```

It never gives up on, or breaks, the lock of a sync that is still running. The
lock is `sync.lock` in the connection directory (see below).

### Its files

| Where | What |
|---|---|
| `~/.config/nersc/config` | the configuration; `NERSC_CONFIG` names another file |
| `mirror.exclude`, next to the config | rsync exclude patterns for `sync` |
| `~/.local/state/nersc/` | the last sync time, the DTN it last used, and the return loop's bookkeeping and lock; `NERSC_STATE_DIR` moves it |
| `$XDG_RUNTIME_DIR/nersc/`, else `/tmp/nersc-<uid>/` | the connection's socket, lock and log, and `sync.lock`, on the node's local disk |
| on NERSC: `$PSCRATCH/.nersc-return/` | the return registry, `queue/`, `done/` and `untracked/` (see `return_queue`) |
| on NERSC: `~/<mirror_dest>` | the code mirror |

The connection directory must be a real directory you own with mode 700. The
companion tightens a looser mode on its own directory, and refuses a symlink, a
file, or a directory owned by someone else, saying how to fix it.

`mirror.exclude` starts as a copy of `remote/mirror.exclude`, which leaves out
virtual environments, caches, agent and editor state, and stray Slurm logs.
Excluded paths are also protected from `--delete` on the NERSC side, so an
environment built on NERSC inside the mirror survives each sync.

### Configuration on the hub

`~/.config/nersc/config` holds `key = value` lines. Lines starting with `#` are
comments.

| Key | Default | Meaning |
|---|---|---|
| `user` | none | your NERSC username |
| `key` | `~/.ssh/nersc-bridge` | the key the bridge installs; its certificate is `KEY-cert.pub` |
| `scratch` | none | your NERSC scratch directory, such as `/pscratch/sd/u/user`; substituted for `$PSCRATCH` and `$SCRATCH` |
| `pool` | `perlmutter.nersc.gov` | the login pool address |
| `dtns` | `dtn01.nersc.gov` to `dtn04.nersc.gov` | space-separated DTNs for bulk data, tried in order |
| `cfs` | none | a CFS project directory, such as `/global/cfs/cdirs/m0000`; substituted for `$CFS` |
| `mirror_src`, `mirror_dest` | none | the code mirror: a tree on the hub, and its destination relative to your NERSC home. `sync` and the syncing form of `submit` refuse until both are set. |
| `return_root` | none | where returned run directories land on the hub: `$PSCRATCH/X` returns to `return_root/X`. Unset, every return needs an explicit destination. |
| `scratch_link` | none | the name of a symlink in your NERSC home that points at scratch (for example `runs`), so `runs/X` maps like `$PSCRATCH/X` |
| `hooks` | none | the path of a project hooks file |
| `return_queue` | `$PSCRATCH/.nersc-return` | the return registry on NERSC |
| `env_parity` | `auto` | `auto`, `on` or `off`: whether `doctor` compares the Python packages installed in a virtual environment at `.venv` in the mirror, on the hub and on NERSC. `auto` checks only when the hub's mirror has one. |
| `refresh_hint` | renew with `cluster bridge push` | what error messages suggest when the certificate is missing or expired |
| `connect_timeout` | 25 | seconds ssh waits for NERSC to answer a connection; an attempt that has not authenticated in twice this, plus ten seconds, is stuck |
| `connect_retries` | 2 | failed connections in quick succession that are tried again: opening the login connection, or resuming a `push`, `pull` or `sync` whose connection was lost. A connection NERSC refuses is not tried again, nor a transfer that read standard input (see above) |
| `connect_retry_delay`, `connect_retry_delay_max` | 2, 60 | the wait before trying again, which doubles with each failure in quick succession up to the second |
| `connect_half_life` | 300 | seconds of a working connection that halve the count of recent failures, so that failures spread over a long run never add up |
| `alive_interval`, `alive_count_max` | 30, 10 | keepalives on the login connection: after this many unanswered, this many seconds apart, it is closed |
| `master_ready_wait` | 8 | seconds an authenticated login connection is given to be ready |
| `dtn_probe_timeout` | 5 | seconds a DTN is given to answer before the next is tried |
| `no_progress_seconds` | 180 | seconds without output after which a remote command is abandoned (see above) |
| `lock_patience` | 30 | seconds a mirror sync waits on another that holds the sync lock but is stopped (Ctrl-Z) before it gives up, naming it |
| `reap_lock_stale_seconds` | 900 | seconds a reap lock may go untouched before it is taken to belong to a pass that died; a live pass touches it every 15 seconds, waiting to connect included. Never less than 300, below which a live pass would lose it |

### submit

`submit` syncs the mirror and then runs a command on a NERSC login node, from the
mirror's directory. The command is usually a script that calls `sbatch`.

```bash
nersc submit -- ./scripts/launch.sh --nodes 4   # explicit form: runs exactly this
nersc submit --no-sync -- ./scripts/launch.sh   # against the existing mirror
nersc submit --cd other/dir -- make run         # from another directory
nersc submit jobs/train.py --epochs 10          # default form, defined by a hooks file
```

Without `--`, the arguments go to the hooks file's `submit_argv`, which can turn
them into the real command. Without a hooks file, they run as given. Tool
options (`--no-sync`, `--cd`) are read only before the command, so the command
keeps its own flags. `--cd` belongs to the explicit form; the default form
always runs from the mirror's root. If the sync fails, nothing is submitted.

## The job return loop

NERSC scratch is treated as temporary. A finished job's run directory is pulled
back to the hub, and the analysis reads the copy there.

A run directory is registered for return in one of three ways. The first two
run on the hub: `sbatch --return` when submitting, and `track` for a job that
is already queued.

```bash
nersc sbatch --return --chdir=/pscratch/sd/u/user/runs/r1 job.slurm
nersc sbatch --return --run-dir /pscratch/sd/u/user/runs/r1 --dest ~/elsewhere/r1 job.slurm
nersc track 12345678 /pscratch/sd/u/user/runs/r1 [DEST]
```

`--run-dir` defaults to sbatch's `--chdir`. Without `--dest`, the destination
follows path parity: `$PSCRATCH/X` returns to `return_root/X`. `--cleanup`,
described below, works with both.

The third way is for code running on NERSC: write a JSON file to
`$PSCRATCH/.nersc-return/queue/<jobid>.json` with at least `job`, `src` and
`dest`, and optionally `cleanup`. To return only part of the run directory, a
record may also carry `include_paths`, a list of files or directories relative
to it, or `include_prefixes`, whose entries also match names that continue
after a dot (`model` returns `model.pt` and `model.json` too). A record that
cannot be read, or that lacks `job`, `src` or `dest`, is reported and skipped.

Each record is read with a time limit of 30 seconds, or half of
`no_progress_seconds` when that is shorter. Reading a file whose
storage on NERSC has stopped answering does not fail. It waits, so a
record that is still unread when the limit runs out is reported by name
and skipped until the next pass, and the records after it are still
read. When three records in a row run out of time, the read stops,
because the fault is then likely to affect most of scratch rather than
a few files. With `no_progress_seconds = 0`, reads have no limit.

The limit is a wait, not a kill. A process waiting on a storage target
that has stopped answering cannot be killed, not even with SIGKILL: on
login33 on 9 October 2026, a `find` killed after 16 hours stayed where
it was. So the reader of a skipped record is left on the login node, and
it ends by itself once the target answers.

When one of scratch's Lustre object storage targets (OSTs) stops
answering, a stat or read of any file stored on it hangs. That happened
to pscratch OST 61 on 8 October 2026. Before this limit existed, one
such record ended every pass's read of the queue, and no records after
it returned. `nersc doctor` checks for such a target: it gives `lfs df`
15 seconds to report every target of scratch, and when it has to be
killed, names the one after the last it printed:

```
scratch storage: OST 61 is NOT ANSWERING (lfs df stopped after OST 60, and was killed after 15s)
```

Files stored there cannot be read by any route, since the data transfer
nodes see the same targets, so they wait on NERSC until it answers. To
list a tree without touching them, read metadata only and leave that
target out: `lfs find DIR -t f ! --ost 61`.

`nersc reap` does one pass over the registry. Run it periodically on the hub,
from cron or from a long-running watcher job:

```bash
nersc reap --list     # what is tracked and each job's state
nersc reap            # return every job that has finished
nersc reap --max-seconds=540   # start no pull after 9 minutes
nersc reap --parallel=8        # up to 8 pulls at once
```

A pass has no time budget: it returns every finished job, however long the
pulls take. A SIGTERM, such as a scheduler's time limit, ends it cleanly. Each
pull in progress is asked to stop and resumes on the next pass, the pass
saves its record of which jobs it has seen finish and lets go of its lock,
and it prints its verdict
with the line `stopped by SIGTERM part way through; a pull it cut short
resumes next pass`.

`--max-seconds=N` (or `--max-seconds N`) makes a pass start no new pull after
N seconds; it never cuts a pull short, and 0 means no budget. It is for a
scheduler that kills with SIGKILL and no warning, or for passes that should
end between pulls: set it below the scheduler's limit, as in the example
above. The jobs it leaves wait for the next pass. `reap` refuses any other
argument.

`--parallel=N` (or `--parallel N`, 1 to 16) runs up to N pulls at once; the
default, 1, runs one at a time. Each pull is one rsync over its own ssh
connection to a DTN, and a long route can cap what one connection carries
well below what the route can. On 9 October 2026 a DTN sent FASRC 0.7 MB/s
over one connection, 2.7 MB/s over four and 5.3 MB/s over eight, so a
backlog of finished jobs drained at the speed of one. With N above 1, each
rsync line starts with its job (`59452518: measurement/tee48.json`), and two
entries with one destination are never pulled at the same time. A pull that
stalls on a dead storage target then holds one of the N slots instead of the
whole pass. Everything else still happens one entry at a time: retiring an
entry, cleaning its run dir off scratch, and the budget, which is checked
whenever a slot frees. A SIGTERM stops every pull in progress, and none of
them is reported as failed.

Only one pass runs at a time; a second one says `another reap is running;
skipping`. A live pass touches its lock every 15 seconds, even inside one long
pull, so a lock left untouched for 15 minutes belongs to a pass that died, and
the next pass breaks it and says so.

A job must look terminal on two consecutive passes before it is returned, so a
job that requeues itself on timeout is not pulled mid-resurrection. A fresh
destination is written as `DEST.nersc-part` and renamed into place when
complete; an existing one is updated incrementally. A failed pull stays in the
queue and resumes on the next pass, after every other job: a pull that
failed is likely to fail again, as one does on a file whose storage on NERSC
has stopped answering, and each failure costs `no_progress_seconds` of
silence. `reap --list` marks such a job `[last pull failed]`.

A job that Slurm's accounting has no record of stays `UNKNOWN`, which is not
terminal, so reap can never act on its entry. (A never-started array element
is not one of these: reap finds it under its array's own record.) A pass names those registered more than seven days ago, and
`untrack` sets entries aside:

```bash
nersc untrack --forgotten --dry-run   # what it would set aside
nersc untrack --forgotten             # every job unknown to accounting, tracked over 7 days ago
nersc untrack 12345678 12345679_3     # these jobs, whatever their state
```

Nothing is deleted. Each entry moves to the registry's `untracked/`, where an
earlier entry of the same name is kept as a numbered backup (`X.json.~1~`),
and moving it back into `queue/` tracks it again. `--forgotten` leaves an
entry that does not record when it was registered (`tracked_at`); name its
job instead. While a reap pass is running, `untrack` refuses rather than
move an entry the pass may be working on.

`--cleanup` removes the NERSC copy after a successful return:

- only for a run directory strictly inside `scratch`: an absolute path with no
  `..` component. Scratch itself is refused. The rule is checked when the job
  is registered and again right before the delete, since a record written on
  NERSC was never registered. The check is lexical: it reads the path as
  written and resolves no symlinks. A run directory that is itself a symlink
  is removed as just the link, but a symlinked directory above it (say
  `$PSCRATCH/runs` pointing into CFS) is followed, so do not register cleanup
  through one;
- only after rechecking that the job is still finished. If the recheck gets no
  answer, the record stays queued for the next pass. If the job is running
  again, the record stays queued too, and the job is returned, and cleaned,
  once it has finished again;
- only after the record has moved to the registry's `done/`, so a failed
  delete never repeats the return. A failed delete is reported with the
  command that finishes it.

`reap` exits with status 1 when a return or a cleanup failed.

To look at a job without waiting for it:

```bash
nersc peek 12345678           # state, run-directory listing, tail of its output
nersc peek 12345678 log.txt -n 100
nersc fetch 12345678 [DEST]   # copy the run directory now, even mid-run; rerun to refresh
```

## Project hooks

Project-specific behaviour belongs in a hooks file, not in the companion. Name
it in the hub's config (`hooks = PATH`). The file is imported by the hub's
system Python, so it must be compatible with Python 3.6 and use only the
standard library. Every hook is optional, and each receives `tool`, a live view
of the running companion: `tool.CFG`, `tool.die(message, *hints)`,
`tool.expand_remote(path)`, `tool.mirror_src()`, `tool.return_root()` and so on.

| Hook | Purpose |
|---|---|
| `submit_argv(args, tool)` | Returns a list, or `None`. Rewrites the default (no `--`) `nersc submit ARGS` form into the remote command, for example by prepending a launcher or injecting flags. `None` runs ARGS as given. |
| `submit_inputs(argv, tool)` | Returns local paths the submission needs on NERSC. Each is staged to its parity path (`return_root` to scratch, or `mirror_src` to `mirror_dest`) before submitting. Called only when `submit_argv` rewrote the command. |
| `SUBMIT_INPUT_EXCLUDES` | A tuple of rsync patterns applied when a staged input is a directory. |
| `return_excludes(tool)` | Returns rsync exclude patterns applied to every return (`reap` and `fetch`), for example `*.tmp` files or core dumps. |

If a hooks file fails to load, every command warns. The default `submit` form
refuses, so a half-configured project never submits. `reap` and `fetch` carry
on without the extra excludes: returning too much costs storage, while failing a
return strands a finished job. `nersc doctor` reports the hooks file and which
hooks it provides, along with the mirror and the return root.

`remote/hooks.example.py` in this repository is a complete example: its
`submit_argv` runs a `.py` submitter with the mirror's `.venv/bin/python`,
`submit_inputs` stages `--input=PATH` arguments, and `return_excludes` skips
temporary files and core dumps. A shorter version:

```python
# nersc_hooks.py: Python 3.6, standard library only
SUBMIT_INPUT_EXCLUDES = ("*.log", "__pycache__/")


def submit_argv(args, tool):
    if not args or not args[0].endswith(".py"):
        return None                       # not ours: run as given
    return [".venv/bin/python", args[0]] + list(args[1:])


def submit_inputs(argv, tool):
    return [a.split("=", 1)[1] for a in argv if a.startswith("--input=")]


def return_excludes(tool):
    return ["*.tmp", "core.*"]
```

## When the certificate expires

Nothing on the hub can renew the certificate. That is the point of the design.
Once it expires, every `nersc` command says so and names the remedy, `cluster
bridge push` on the workstation. If the workstation was offline for less than
the remaining validity, the hub never notices.

## Installing and syncing the companion

The companion on the hub is a two-way file: agents on the hub may edit
`~/.local/bin/nersc` in place. Before shipping anything, a push compares the
hub's copy with a record, kept on the workstation, of what it last installed
there:

| Hub's copy | Result |
|---|---|
| identical to `remote/nersc` | nothing shipped |
| absent | installed |
| edited on the hub, source unchanged here | conflict: the hub's copy is left untouched and a copy of it is kept. With `COMPANION_ADOPT_HUB_EDITS=1`, adopted into `remote/nersc` instead (the replaced source is backed up), and nothing shipped. |
| as installed, source changed here | shipped |
| changed on both sides | conflict: the hub's copy is left untouched and a copy of it is kept |
| different, with no install record here | shipped if the local `VERSION` is higher; otherwise treated as an edit on the hub |
| unreadable, not the companion, or larger than `COMPANION_MAX_BYTES` (1 MiB) | left untouched |

`--overwrite-tool` replaces any readable copy, and keeps a copy of what it
replaced. A conflict report prints a `diff -u` command comparing this machine's source
with the kept copy. To resolve it, take the hub's version with `cluster
nersc-tool sync work` or this machine's with `cluster bridge push work
--overwrite-tool`. The credential is pushed either way, so a conflict never
leaves the hub without a certificate, and `--cron` refuses `--overwrite-tool`.
On the workstation, backups of replaced sources are kept in
`~/.local/state/cluster/nersc-tool-backups/` and conflict copies in
`~/.local/state/cluster/nersc-tool-conflicts/`.

Adoption is off by default because it makes this checkout run code that was
written on the hub. Turn it on with `cluster --fasrc config set
COMPANION_ADOPT_HUB_EDITS 1` only if you trust what runs there.

Prefer a hooks file to editing the companion: a project's behaviour then
survives companion updates without merges.

Other `cluster nersc-tool` actions:

```bash
cluster nersc-tool path                     # where the canonical source is
cluster nersc-tool install-local            # ~/.local/bin/nersc on this workstation
cluster nersc-tool install work             # install the companion and credential over login work
cluster nersc-tool install work --tool-only # the companion and default config only
cluster nersc-tool sync work                # take the hub's copy
```

The local install links `~/.local/bin/nersc` to the repository's `bin/nersc`,
which runs `remote/nersc` in place with the workstation's Python (3.8 or
newer, as for `cluster`). On the workstation the companion keeps its files in
cluster's own trees: its config is `~/.config/cluster/companion/config`
(written once, from the same template, with `key` set to the key that
`cluster --nersc auth` keeps certified, `~/.ssh/nersc` unless `KEY` names
another), with
`mirror.exclude` beside it, and its state is in
`~/.local/state/cluster/companion`. Both follow `XDG_CONFIG_HOME` and cluster's
`STATE_ROOT` when those are set. The install needs your NERSC credentials
configured on the workstation and creates nothing without them. `cluster setup`
installs the companion on the hub only when `SETUP_SYNC_NERSC_TOOL=1`.
