# Design notes

Why `cluster` behaves the way it does, and what the sites it was built against
actually do. Measurements carry the date and node they were taken on. Sites
change, so treat them as evidence, not as a specification. The user-facing
behaviour is in [USAGE.md](../USAGE.md).

## Core ideas

**One name is one connection.** Login names live in a single namespace across
every backend, so `cluster where gpu` finds a NERSC login without being told.
The default backend applies only to creating a name that does not exist yet.
Everything else resolves from local state, with no credentials and no network.
Naming a backend that disagrees with where a login lives is refused rather than
redirected, and `cluster doctor` reports a duplicated name. `cluster rename OLD
NEW` costs no reauthentication: a unix socket's listener is bound to its inode,
not its path, so the running control master keeps serving under the new name.

**A login is pinned to its node, and the pin is durable state.** tmux sessions
are node-local, so a login always reconnects to the node it first landed on. If
that node is unreachable, the command fails with recovery instructions rather
than quietly taking another node. A silent move strands the real work and
creates empty look-alike sessions somewhere else.

**Nothing is left running where nothing looks for it.** A login cannot take its
sessions to a new node, so `repin` and `unpin` close them as part of the move,
after warning and asking. `--abandon` opts out, and even then the abandonment is
recorded locally. A left-behind session keeps its login's ownership tag, and
`clean` spares anything whose owner still exists, so without that record it
would be invisible to `where` (which reports the login's current node) and
unreapable. The record is local because the usual reason to repin is that the
node died, and a dead node cannot be asked anything. When the old node is
unreachable, the move still happens, the sessions are recovered from the
breadcrumbs in the shared home, and the command exits non-zero: it proceeded,
but it did not succeed.

**A record nothing looks at is still evidence.** Not every move goes through
`repin`. A login can be reset off a broken node, redrawn by the pool, or have
its pin dropped by `forget`, and each leaves breadcrumbs describing sessions on
a node no login occupies. Left alone, such strays would be both invisible and
immovable: `clean` protects any session whose owner login still exists, and
breadcrumb reconciliation only covers the node its own login is on. They would
surface only as a refusal, when a new session name is declined because of a
record on a node the login left long before. So `ls`, `status`, `clean` and the
watcher name them, and `cluster strays` reconciles them. Nothing is deleted on a
guess: `check` drops a record only for a session it proved gone, and an
unreachable node keeps all of them, because that is exactly when the record is
the only evidence left.

**Nothing destructive happens without proof.** A tmux kill must be confirmed
before any pin or breadcrumb is dropped. `clean` reaps only sessions it can
prove are orphans: sessions carrying this tool's ownership tag and naming a
login that does not exist. From the outside, a stranger's live work is
indistinguishable from litter, so untagged and foreign-tagged sessions need an
explicit flag each.

**A sick mount must not tear down the login.** Repair is a ladder: unwedge and
remount over the existing connection (free), then move the mount to another
node, and only then rebuild the login.

**Storage has no node affinity, but sessions do.** That is why a mount can fail
over to another node while the login stays pinned with its tmux sessions.

## Why a backend layer

The two sites differ in ways that reach into the design, so the differences are
declared by each backend rather than special-cased at call sites. As observed
in August and September 2026:

| | FASRC | NERSC |
|---|---|---|
| Authentication | password + TOTP on every connection | `sshproxy` exchanges password + OTP for a 24-hour certificate |
| TOTP pacing | required: a reused code is rejected, so logins serialize and wait for a fresh 30 s window | only for the daily certificate |
| Login nodes | `holylogin05-08`, `boslogin06-08`, all directly routable | `login01-40`, firewalled; reached by tunnelling through `perlmutter.nersc.gov` |
| Choosing a node | unaffordable (each attempt costs a TOTP window), so a login takes what the balancer gives and pins itself there | free, so a login derives its node from its own name |
| Load balancer | sticky | not sticky: four connections landed on four nodes |
| Mounts and transfers | any login node (storage is shared pool-wide) | `dtn01-04`, routable and built for I/O; login nodes are cgroup-capped at 30 GB and 12.5% CPU per user |
| tmux | 2.7 | 3.3a |
| Lending the credential | impossible: a password typed into a pty cannot travel | a certificate file, which can be forwarded in an agent |
| Reaching the other site | can dial NERSC's DTNs | login nodes cannot dial FASRC at all |
| Globus collection | "Harvard FAS RC Holyoke"; does not export `/n/home*`; session policy that expires | "NERSC Perlmutter"; home and scratch; consent only |

The last rows are why a cluster-to-cluster transfer runs on FASRC in both
directions. A backend also declares its node classes (`cluster nodes`), with
what each is for and whether it is directly routable, so "where should a mount
live" and "does this node need a jump" have one answer.

NERSC derives a login's node from a hash of its name rather than choosing at
random, so if local state is ever lost, the same name lands back on the node
that holds its sessions.

## Looking must not cost an authentication

`ls`, `status`, `where`, `channels` and completion never authenticate. On FASRC
every authentication spends a 30-second TOTP window, and `ls` is what you type
to find out where things are.

Free of authentication is not free of the network. A pin records where a login
should be; only the node can say where it is, and only the node knows its
sessions. In a terminal, `ls` draws the last saved evidence immediately, asks
every live login concurrently, and replaces the table when the slowest reply
arrives. Each live login costs one round trip, with node and sessions on the
same channel. A failed reply does not erase the saved session list: the final
table shows those names as `cached: ...`, with attached markers removed because
they are stale. A literal `none` is reserved for a successful live query of an
empty tmux server.

That saved evidence is also updated by every session this tool creates or kills,
the moment the remote end confirms it, so the first table `ls` paints already
knows about the session `cluster n` just started. Only confirmed changes are
recorded: an unacknowledged create would invent work, and an unconfirmed kill
would hide it. `ls -q` asks tmux nothing, so it cannot block on a sick tmux
server, but it does not save a round trip.

Tables are sized to the terminal. At natural width they run past a hundred
columns, and a terminal narrowed to make room for an editor would hard-wrap
each row with no indent, running rows together into an unreadable block. Space
is taken from the widest free-text columns first, while the first column
(the row's identity) keeps up to a third of the line. Below the width where a
squeezed cell stops being recognisable, the table becomes one stacked record per
row. Piped output is never trimmed and carries no control sequences.

## attach: two round trips, on purpose

A channel costs about the same whatever it carries: sshd's per-session setup
dominates, so a bare `true` and a full session listing take about as long. The
number of round trips is therefore the cost. `attach` needs to know two things
(which sessions exist, and whether this name is registered on another node) and
to do three (create or attach, tag the owner, drop the breadcrumb). The knowing
travels in one reply and the doing in one command. Measured on boslogin08 at
load average 25 on 2026-08-11, the batched sequence took 3.2 s against 9.5 s
for separate calls.

It does not collapse to one round trip, because the session name is unknown
until the listing has been read: the default is the login-named session, else
the only session, else a refusal to guess. Pushing that decision into the remote
shell would save one round trip and cost a tested decision, so it stays local,
and the split falls where the data dependency is.

The batching had to respect two things:

- `new-session -A -d` must remain the first tmux command to touch a server-less
  node. On tmux 2.7, a query as the first command loses the server it just
  started, and the follow-up attaches to a corpse. A single blob invites someone
  to reorder that away, so a test pins it.
- Each of the three writes reports its own exit status. One status for three
  actions would make a failure unattributable, and a missing breadcrumb is how a
  session becomes invisible to `clean`. On tmux 2.7 the `-A` path for an
  existing session tries to attach even under `-d` and exits 1 with `open
  terminal failed: not a terminal`, so the start-up step falls back to asking
  whether the session exists, and its status means "the session exists", not
  "the session was new".

## Exact session names

tmux resolves a bare `-t NAME` as the exact name, else a unique prefix, else a
glob. So `kill-session -t ap` kills `api` and exits 0, and the tool would record
a kill of `ap` while `api` stayed in the saved session names. Every tmux target
the tool sends is `=NAME:`, which matches only the exact name on tmux 2.7 as on
3.x; the trailing colon makes it mean the session for commands that take a
window or pane target too. A kill of a name that matches nothing kills nothing
and suggests the session that was probably meant.

## The channel budget

One login is one SSH connection, and sshd caps a connection at `MaxSessions`
channels. FASRC leaves it at the upstream default of 10. Everything riding the
login competes for those: each open attach, the sshfs mount, and every rclone
sftp connection a transfer opens.

Overrun is invisible in two directions at once. sshd refuses the channel with

```
channel 22: open failed: connect failed: open failed
```

which names neither `MaxSessions` nor what filled the table, while `ssh -O
check` goes on answering `Master running` in a tenth of a second. The master
looks healthy while every new channel fails. That is why a failed channel must
not trigger a master rebuild: rebuilding kills every other attach on the control
path, breaks any transfer in flight, and spends a TOTP window to replace a
connection that was fine. Recovery recognises exhaustion and says so instead.

`cluster channels` accounts for the budget from local process state, so no
channel is spent counting channels. Sizing concurrency against it is not
optional: a job with a fixed 3 transfers + 3 checkers + 1, sized against "mount
attached and nothing else", goes over the limit once five attaches are open,
and every channel it asks for after that is refused.

Sizing is not enough on its own, because a master can be up and unusable: its
channels can be spent, or its node can accept the connection while refusing
per-user exec (and still pass the balancer's health check). An unattended job
should escalate rather than give up: primary login, then a login of its own,
then that login on a fresh node, closing only what it opened, and printing the
remote's own words when a step fails. `cluster bridge push --cron` falls through
to other logins for the same reason.

## One backend, one mount

Every login node of a backend serves the same home, so a second login's mount is
not extra access; it is the same bytes under a second path, paid for out of the
channel budget. Auto-mount therefore mounts a backend once, and `ls` shows
`shares LOGIN` for the others. That is deliberately different from `-`: a login
sharing the backend's mount must not look like one whose mount failed. The
watcher leaves such a login alone, or it would recreate the duplicate on the
next tick. The policy only ever declines to create a duplicate: an existing
mount is still probed and healed, and an explicit `cluster mount` is honoured.

## Pools that drop connections before authentication

Measured on 2026-08-17, FASRC's login pool name published two addresses, and
one of them accepted the TCP connection and then closed it on roughly a quarter
of attempts:

```
cluster: could not open login 'x' on any Harvard FASRC node
  Connection closed by <pool address> port 22
```

ssh does not fall through to the other address here, because connecting is
exactly what succeeded. Without a retry, failing means typing the command again
and waiting out another TOTP window. `POOL_OPEN_TRIES` (default 3) redraws instead, only for
failures that provably never reached the password prompt: one retry turns 1 in
4 into 1 in 16, and two into 1 in 64. A rejected password is still fatal on the
first attempt, because retrying credentials spends TOTP windows and repeated
failures lock accounts. Each redraw re-paces TOTP even though a connection
dropped at the banner never sent a code: guessing wrong would send a reused
code, which fails exactly like a wrong password. A pinned login dials its node's
own address and never meets the pool.

## Retrying across a long run

Everything that reconnects or retries over a run that can last days follows
one rule, in `clustertool/backoff.py`: each failure adds one to a score, the
score halves with every half-life of healthy time after it, and the wait before
the next try doubles with the score, up to a ceiling. Failures spread over days
never add up, while a burst ends the run once the score passes its setting's
limit, and the run says so. The score itself stops where one more failure
changes nothing, at the longest wait or one past the limit, so a loop that
never gives up (the watcher) forgets a day's outage as fast as a short burst.
A fixed count over the whole run would end a transfer that drops once a day
on its ninth day; a count reset after some healthy seconds forgets a burst
with a pause in it. A refused credential is
not retried by these loops, because on FASRC each try counts toward locking
the account. It goes on a record shared by the backend's processes
(`state.Refusals`, keyed by a stat of the credential files), which one process
confirms once after `REFUSAL_CONFIRM_DELAY`, since a reused code or a clock
off after a wake passes; a second refusal holds every unattended process until
the files change or a person connects by hand. The confirming try is claimed
for as long as the claimant's authentication can take (a master's open, 180 s,
and the waits around it), so a claimant that died is passed over even when its
pid is someone else's by then. A process looks at the record before it waits
for its TOTP window, so that a refusal costs nobody a window, and again once it
has one, since another process may have been refused in the meantime. The
record is of the password, so on NERSC it belongs to the sshproxy fetch: a
connection there presents a certificate, which risks nothing on the password,
and a certificate ssh refuses is replaced once instead (a file names the
replacement, so one refused in turn is not replaced again). A connection that
opens no master (a direct sweep, a disposable shell, rescue, init's test login)
goes through the same record as a master's open (Logins.authenticate_directly):
only a status the far side gave counts as the credential taken.

A transfer knows its connection was lost by the master it started on being gone
or replaced, not by rclone's exit status. rclone spends every retry on a
connection that cannot come back, eight and a half minutes with the settings
here (measured), and then exits as it would for a failure of its own. So the
master's process (its pid, which `ssh -O check` names when the run takes the
link) is looked at every ten seconds, a look that asks nothing of the master;
rclone is stopped when it is gone and run again once the connection is back,
and it skips what already arrived whole. A run that fails, whatever its
status, has its masters asked at their sockets as well, since one on its way
out closes its channels a moment before its process ends; one there but slow
to answer has not gone, and is left to the runs riding it. On a connection
that is fine, rclone's own failure stands. A direct transfer's rclone runs on
the executor, where losing this machine's connection does not stop it. It is
tied to a heartbeat this machine sends down the channel and stops itself when
that has been quiet for as long as the connection itself would ride out, and
says so with a status of its own; the transfer then resumes at once. After a
lost connection the resumed run starts only once the old one has stopped, so
two never copy the same files at once: each run's processes there go by a
name of their own, and once a master is back on the node it ran on, that
node is asked whether any is left, at once and at each look, for as long as
the old one could still be running.

rclone's `--timeout` does not reach a transfer here: it bounds rclone's own
network connections, and with `--sftp-ssh` there are none, only pipes to ssh.
A connection that stops answering is ended by ssh's keepalives instead. A far
side that still answers but has stopped moving, such as a hung filesystem
there, is waited for, as `cp` would wait.

The pieces that cannot import the package follow the same rule in their own
terms. The relay client imports only `clustertool.backoff`, which reads no
state, and its knobs are the `[relay]` `RETRY*` keys. It tries again only
after a lost connection, which it reads from ssh's 255 on either hop; the
relay host's `--serve` says 255 too for a connection to the cluster it could
not open for a reason that may pass, and 96 for a refused credential. A
download whose own end stopped first, as at a full disk, is not a lost
connection, though the far end then fails as one would. A relayed stream
cannot resume, so it starts again from the first byte, and its time earns no
credit: a stream that keeps losing its connection stops after `RETRIES`,
however long each try ran. A staging copy, which rsync resumes, earns it. The NERSC companion carries a copy of the rule, which a test
holds to `backoff.py`, with its `connect_*` config keys. `archive-sync-cron`
does the arithmetic in awk, and credits a failed run's time only when its log
shows that it copied, moved or deleted something and that it failed on some
file or directory other than those the try before it failed on: a live home
always has something to copy, so a copy alone is not progress. It gives every backend its
first try before any retry, and it starts no try past
`ARCHIVE_SYNC_RETRY_WINDOW` seconds into its run, so that one run ends before
the next is due. A run stopped by a signal is not tried again.

A lock is waited on for as long as its holder works. What ends the wait is
evidence that the holder will not let go: a holder stopped with Ctrl-Z for
`LOCK_PATIENCE` seconds, or, for a lock held only for a small write or for the
TOTP queue, nothing moving for the lock's patience. The message names the
holder, so the one who stopped it knows what to resume.

## Answering prompts

On FASRC every ssh runs under a pty that types the password and TOTP code when
asked. Passwords never reach a command line or a log. The answering stops once
the session has started: a prompt that appears later belongs to something the
user ran, such as `sudo` or a nested `ssh`, and must never receive the cluster
password.

A failed connection has to say what failed. FASRC prints a post-quantum
advisory banner as the last thing on a login, so taking the final line of output
would report "the server may need to be upgraded" for every failure, including a
rejected password. Failure reporting skips banner text, ssh's debug chatter and
the prompts that were answered, and reports the last line that says something.
A pty has no separate stderr, so the child's output is collected and returned in
its place.

## SSH options that cannot be added later

- **ssh takes the first value it is given for an option, not the last.** Agent
  forwarding cannot be bolted onto an existing command line by appending `-o
  ForwardAgent=yes` after `-o ForwardAgent=no`; it is silently ignored. It is a
  parameter, and a test asserts exactly one `ForwardAgent=` reaches argv.
- **Agent forwarding cannot be added to a running master.** The master proxies
  the agent, so forwarding is fixed when it is created. `-o ForwardAgent=yes`
  over a live master gives the far side no `SSH_AUTH_SOCK`. That is why a direct
  cross-cluster transfer opens its own connection and ignores `--via`.
- **A forwarded agent dies with the process that opened it.** A master left by
  `--keep` outlives its agent: the socket on the far side is present and answers
  nothing. A reused forwarding master is probed and replaced.
- **`-F /dev/null` by default.** The tool is the single source of truth for how
  it connects to a site it knows, so a stray `~/.ssh/config` stanza cannot
  change its behaviour. `SSH_CONFIG` opts back in. The ssh type is the
  exception: it knows nothing about its host, so its `HOST` is usually a name in
  that file, and it reads ssh's own configuration unless told otherwise. What
  rides a master still gets `-F` with the machine-wide value, so a `Host *`
  stanza's `RemoteCommand` or forwards never reach a shared master.
- **A destination is not a node.** A login is pinned to what its node calls
  itself, and every connection to it goes through the backend's `host_for`,
  which for the ssh type is `HOST` or a `NODE_HOSTS` route, never the node's own
  name: an alias, a jump or a port lives in ssh's configuration under the
  destination. A reconnect that lands elsewhere is dropped, as on any backend,
  and a command sent to a node over a connection of its own checks where it is
  before it runs.
- **A dead channel is not a dead master.** One channel can fail while the master
  is fine, so reconnect pings first and retries the channel. Tearing the master
  down unconditionally would cost a full reauthentication for nothing. Teardown and
  rebuild share one lock so the watcher cannot cut down an authentication in
  flight.
- **Opening a master occasionally loses a race.** ssh authenticates, forks, and
  the background process is gone by the time it is asked. Masters are polled
  for (`MASTER_READY_WAIT`) rather than asked about once, and a transfer retries
  the open.

## Cross-cluster transfers

Neither rclone nor rsync has a server-to-server mode over ssh, so the only
question is who runs the copy. The executor authenticates to the peer as you,
which means the peer's credential must be lendable. NERSC's certificate is a
file, loaded into a private agent that holds only that key, with a lifetime
matching the certificate, killed when the transfer ends. Root on the executor
node can sign with it while the transfer runs, which is strictly less exposure
than writing the key there. FASRC's password cannot travel, so FASRC always
executes. Nothing hardcodes that; it falls out of which backend can produce an
identity.

Every failure before the first byte falls back to `relay`: a peer node that
does not resolve, a firewall, an sftp refusal, a missing remote rclone. The
failed attempt's connection is handed to relay rather than dropped, so the
fallback costs no second TOTP window. Two limits are deliberate: an explicitly
chosen engine never falls back, and nothing falls back once rclone is running,
because a half-written destination must not be silently restarted by another
engine. Failures relay would hit too (no credential, no connection at all) stay
fatal.

Globus was verified end to end in both directions on 2026-08-08, with matching
checksums. It is not the default because FASRC's collection enforces a session
policy that expires, which rules out unattended use, and because
`/n/home*` returns `EndpointPermissionDenied` however the session looks, while
`/n/netscratch/...` and `/n/holystore01/LABS/...` work. Both are checked before
submission, so they surface as a synchronous error with the fix rather than an
opaque task failure.

**Several sources at once.** `cluster transfer a.py b.slurm work:dir/` shares one
connection, and sources from the same directory share one rclone run through
`--files-from`, because on sftp every extra invocation is another channel and
handshake, not a cheap fork. Twelve files took 19 s batched against 92 s one at
a time. Every name a shell glob expands to is a source: reading only the last
two as source and destination would copy one file and exit 0.

**Value-taking rclone flags.** Separating flags from paths by a leading dash
tears `--exclude '*.log'` in half: the flag reaches rclone bare and `*.log`
becomes a path. A table lists the flags that take a separate value; unknown
flags still pass through, so boolean ones need no escape hatch. The relay
client duplicates the table, and a test keeps the two in step.

## The relay client

The relay host is a conduit, not a staging area. A `tar` on one end and a `tar`
on the other are joined by an ssh on each hop, so the relay host holds a 4 MB
block at a time, and no user data rests on a machine that exists to hold a
credential and a socket. The far end of the pipe is `cluster-relay --serve`
rather than a bare ssh, so it opens the connection through the tool's own
transfer layer and holds a lease for as long as the stream runs; no other run
can tear the master down mid-transfer. The lease is returned in a `finally`,
and Ctrl-C, SIGTERM or SIGHUP take both ends of the pipe down.

Exit 0 means the bytes arrived: the sending side lists every file and size
first, the receiving side is asked what it holds afterwards, and the two are
compared. A stream whose connection is lost is run again (see "Retrying
across a long run"), because every mode overwrites rather than appends, so
running it again is the same operation. `tar` carries files, not intentions, so filters and
`--sync`/`--move`-style requests are staged through the relay host's disk under
`~/.cache/cluster-relay`, deliberately not `/tmp`, which is often a tmpfs. A
staging directory left by a run that was killed is swept by a later one once it
has gone a day untouched, and a live run touches its own every ten minutes, so
a transfer that takes longer than a day is never swept from under itself.

## The two ways a login node kills your work

On a FASRC login node, your processes are in one of these:

```
/user.slice/user-<uid>.slice/session-<n>.scope     <- one per connection
/user.slice/user-<uid>.slice/user@<uid>.service    <- one per node
```

1. **A connection ends.** logind runs with `KillUserProcesses=yes`, so it
   terminates that connection's session scope and everything in it. Linger has
   no bearing on this.
2. **The last connection ends.** `user@<uid>.service` is stopped, taking what it
   holds. This is what linger (`loginctl enable-linger`) prevents.

A `tmux new-session` sent over SSH runs in that connection's shell, so it lands
in the session scope and dies at (1) whatever linger says. Starting the tmux
server under `systemd-run --scope --user` puts it in `user@<uid>.service`
instead: out of reach of (1), and behind linger for (2). FASRC ships both
halves in `/etc/profile.d/linger.sh` (`loginctl enable-linger`, and `alias
tmux="systemd-run --scope --user tmux"`), but an alias reaches interactive
shells only, and nothing this tool sends is interactive.

Measured on holylogin07 on 2026-09-17, with linger enabled throughout and one
clean `close` and reconnect between each:

| how the server was started | where it landed | after a clean close |
|---|---|---|
| `tmux new-session -d -s a` | `session-<n>.scope` | gone |
| `systemd-run --scope --user tmux ...` | `user@<uid>.service/run-...` | survived |

Dropping every client connection without closing them tests neither mechanism:
FASRC's sshd sets no `ClientAliveInterval`, so the node does not end those
sessions at all until TCP gives up, and a session found alive after four minutes
of that says nothing. A clean close is what a reboot of the workstation does,
and that is what the table measures. On holylogin06 on 2026-09-17, a server
started in a session scope ended at the clean close of the last connection
while the node itself stayed up.

A server started in the session scope cannot be moved out of it: cgroup v2
needs write access to the source cgroup as well as the destination, and the
session scope belongs to root. On holylogin06, creating the destination was
permitted and the write to `cgroup.procs` was denied. Reporting it (`doctor`'s
`tmux scope`, and a warning on the create that landed badly) is the whole job.
The probe asks tmux for its own server pid. Matching `/tmux/` in a process list
would not work, because an attached client always sits in the session scope of
the connection it came from, which would make a protected server look doomed
whenever someone had it open.

### Linger

Three facts shape the linger half, from holylogin06 on 2026-09-17:

- **Only one instant matters:** the one when the last session ends. A client
  reboot ends every session at once, so linger has to be in place already.
- **Linger is node-local and not durable.** `/var/lib/systemd/linger` does not
  survive a node reboot, and something on the node clears it even while a
  connection is up: a file created at 14:32:49 was gone by 14:42:32 with the
  master alive throughout. It was absent for about 3 minutes out of 11.
- **Nothing removes it when a session closes.** An extra session opened and
  closed against a watched node left the file in place, so re-asserting is not
  a race the node simply undoes.

So linger is asserted continuously rather than configured once. The state is
read from the linger file with a `stat`, because on holylogin05 at load 15 every
`loginctl` call timed out while the file sat there, enabled. Assertion
short-circuits on the file and bounds `loginctl` with a 5-second timeout on the
node, so a wedged logind cannot turn a session create into a two-minute wait.

Releasing matters as much as asserting. An unreleased node keeps a user manager
alive indefinitely, on a machine shared with everyone else who logs in there.
Every ending therefore settles the node in one command: assert if any tmux
server is still running there, else remove the keeper line and disable linger
if this tool enabled it.
"Any tmux server" means anyone's, not just ours, because `close` and `clean`
spare foreign and untagged sessions, and disabling linger with those running
would kill them. It is one command rather than a check followed by an action,
so a session cannot appear in between and be killed by the release.

Only linger this tool enabled is ever released. An enable that logind accepted
leaves a note on the node, `~/.cluster/linger/<node>`, holding the linger
file's inode and mtime; the note lives node-side because the keeper enables
linger with no workstation present. Settle disables linger only while the file
is still the one recorded. A file that was already there, or that changed
since, belongs to someone else and is left alone. FASRC's own
`/etc/profile.d/linger.sh` re-enables linger on every interactive login, which
changes the file, so on a node where anyone logs in interactively a release
usually leaves linger on. That errs in the direction this design wants: an
unreleased node costs one idle user manager, while a wrong release kills
somebody's work. `LINGER` is on by default for the same reason, since without
it FASRC can end a detached session at the next disconnect.

Verified end to end on 2026-09-17: a session on holylogin05, every connection
from the workstation dropped for four minutes, and the session was still there
on reconnect with its original creation time.

### The keeper

The keeper is a node-side crontab line that re-asserts linger once a minute.
It exists because FASRC's sshd sets no `ClientAliveInterval`: a vanished
client's session is held open until TCP keepalive gives up, hours later, and
that is when logind looks at the linger file. Something has to be asserting
through that window, and it cannot be the machine that went away.

The setting is reconciled, not merely applied: every path that asserts linger
also makes the node's crontab agree with `LINGER_KEEPER`, in the same round
trip. Nothing on the workstation records that a keeper was installed, so a
forgotten one would otherwise assert forever. Only lines carrying the
`cluster-linger-keeper` marker are touched.

Verified on holylogin06 in September 2026: cron runs user jobs there, the line is appended without
disturbing an existing crontab, installing twice adds nothing, removal leaves
the rest alone, and a removal attempted while a session was still running left
the keeper in place. Cost on the same node: a lingering account keeps a
`systemd --user` (13.6 MB RSS) and a `dbus-daemon` (4.5 MB) alive, plus 4 KB of
`/run/user`, against 51 accounts already lingering there and 74 user managers
totalling 968 MB. The keeper itself is one fork and one `stat` a minute and
writes nothing, so cron sends no mail.

### The shutdown hook

The system unit (`extras/cluster-linger-system.service`) is the one that
matters. The control masters live in a user session scope:

```
/user.slice/user-<uid>.slice/session-<n>.scope     <- masters, watcher
/user.slice/user-<uid>.slice/user@<uid>.service    <- a --user unit
```

Those two are siblings, and nothing orders them against each other at
shutdown, so a `--user` hook may run after the masters it needs are gone. A
system unit takes `After=network.target user.slice systemd-logind.service`, and
stop order is the reverse of start order, so its `ExecStop` runs while both the
masters and the network are up. The watcher's own SIGTERM cannot promise that:
it is a detached process that systemd kills in its final sweep, after the
network has gone. The unit records an absolute path at install time and
`ExecStop` ignores failures, so a hook pointing at a moved `cluster` would fail
silently; `doctor` checks for that.

### Recovering what was lost

Both deaths leave the same evidence: a breadcrumb in the shared home naming a
session the node is not running. A crumb on a node its own login still
occupies is checked too, because that is exactly the shape of a reboot: the
login comes back, the pin is intact, the sessions are gone, and nothing else
would say so. Such a record is `lost`. Only a node whose session list was actually read is
judged, because "did not ask" must never read as "not there"; a node too loaded
to answer is left alone.

Recovery reads both records, because neither is a superset of the other. A
layout snapshot has sessions, windows and cwds, but is only as fresh as the
watcher's last save. A breadcrumb holds only the name, but is written in the
round trip that creates the session. In one measured case a login's snapshot
held `api, build, train, work` while its crumbs held `api, eval, train`.
`restore-layout` restores the union and says which sessions came back name-only.
Records are dropped only when someone says so, because the record is the last
thing naming the work.

## Mounts

- **A wedged FUSE mount cannot be probed with `stat`.** The attribute cache
  reports a dead mount healthy for about 20 seconds, and its waiters cannot be
  killed. The health probe is a `mkdir`, which no cache can answer, and it is
  abandoned on its deadline rather than waited on.
- **Never leave a mount wedged.** Anything that touches the path, including
  unrelated tooling, blocks in uninterruptible sleep.
- **Busy is not wedged.** Both miss the probe deadline. The discriminator is
  whether the FUSE queue is turning over, read from movement in its depth, not
  from whether the depth falls. A saturated queue never dips below where it
  started: on 2026-08-11 one held 14, 14, ..., 15, 14 for fifteen seconds on a
  mount that was answering the whole time. A busy mount counts as usable and is
  not remounted; remounting a loaded mount aborts in-flight work and does
  nothing about the load. `MOUNT_BUSY_GRACE` is long enough for about 24 depth
  samples, and a mount behind a deep queue was measured answering at 7.8 s,
  right at the `MOUNT_CHECK_TIMEOUT` edge.
- **The existence of the probe's result file is not an answer.** `mkdir ...
  2>result` creates the file as soon as the shell sets up the redirect, so only
  the trailing `rc=` line means the request came back. A bare existence check
  would report every mount with a non-empty queue as healthy, so every caller
  uses one function for "has it answered", and two copies cannot drift apart.
- **Each probe writes its own result file**, and sweeps only files nobody is
  waiting on. With a shared name, a prober orphaned on an earlier wedge can
  unblock minutes later, write `rc=0`, and be read as the current answer. With an
  indiscriminate sweep, a hand-run probe and the watcher would delete each
  other's files, and the loser would call a live mount wedged.
- **Editors walk mounts.** VS Code's file search walks `~/cluster_mounts` by
  default, which is the likeliest reason a mount looks permanently busy. Worse is
  `search.followSymlinks`: a cluster home holds symlinks into lab and scratch
  storage, so a repo search becomes a walk of multi-terabyte filesystems over a
  WAN. Measured on 2026-08-11, a mount answering in 0.15 s when idle took about
  8 s with a dozen requests permanently queued.
- **NERSC homes are symlinks.** `/global/homes/<i>/<user>` points into
  `/global/u2/...`, and sshfs refuses a symlink as a mount root, so mounts use
  the login shell's home (`.`). Tools that scan the home must resolve it first,
  or they see the home itself as an escaping symlink.
- **Changing node restores what was there.** `refresh` remembers whether the
  login was mounted and watched and puts both back, or a refresh would silently
  leave a working login unmounted.

## Terminals

- **A dropped session must not wreck the local terminal.** ssh restores tty
  flags, but not terminal emulator modes that a remote full-screen program
  enabled. A tmux session that dies with mouse reporting on leaves the next
  scroll arriving at your shell as literal text like `0;48;27M`. Interactive
  sessions snapshot the terminal, restore it after every disconnect, and clear
  mouse, focus, alternate-scroll, bracketed-paste, alternate-screen and keypad
  modes, plus xterm and kitty extended-keyboard modes, including once before
  connecting to clean up after an earlier crash.
- **Leaked layout state is the quiet half.** Auto-wrap left off makes every
  character past the margin land on the same cell, so typing looks like it
  overwrites a ghost. A scroll region left set confines output to a band. Both
  hide until the window is narrow enough to reach the margin, which is why they
  appear on a resize. The reset also restores auto-wrap, the scroll region and
  left and right margins, insert mode, cursor visibility, attributes and the G0
  charset. The two that home the cursor are fenced in DECSC/DECRC so the next
  prompt lands where the session left off, and the rest come after DECRC,
  which would otherwise restore the broken state DECSC captured.
- **A pty can come back at the wrong size.** A resize that arrives while a
  full-screen program owns the alternate screen may never reach the pty, so the
  kernel's window size and the emulator's disagree from then on. Nothing on the
  pty can detect this, because the pty is what is wrong. Every session ends by
  asking the emulator (`CSI 18 t`, falling back to parking the cursor at the
  bottom right and reading where it clamped, both on a deadline) and writing the
  answer back with `TIOCSWINSZ`. It prints a line only when it corrected
  something.
- **`cluster fixterm`** performs all of that on demand, plus `stty sane`, for a
  terminal broken by a program that `cluster` did not start. Unlike `reset` it
  does not clear the screen, which would discard the output you were reading.

## The command line

- **The verb comes first, and a bare name is an error.** Read as an implicit
  attach, `cluster work` would make a login unreachable if its name collided
  with a verb, and `cluster gpu where` would create a session literally named
  `where`; a mistyped name would be taken as both a login and a session. Both
  fail with the command you probably meant. Short aliases keep the explicit form
  cheap, and `l` is `login` rather than `list` because `ls` already covers
  listing.
- **`--help` is a question, never an action.** A command that took an unknown
  flag as "no name given" would fall back to the default login, so `cluster
  close --help` would close a connection. Help is answered before dispatch for
  every command, and the option list is read from each command's source with
  `ast`, because asking the parser would mean running the command. A test walks
  every command and fails if any answers `--help` with anything but an
  explanation.
- **There is no fixed default session.** Following the name-after-the-login
  convention blindly would create an empty second session beside the real work.
- **`run` quotes its arguments,** so `~` and globs reach the cluster literally.
- **Every process says what it is.** A long-lived attach showing as `python3` is
  indistinguishable from any other Python, so each process sets its kernel name
  (`prctl(PR_SET_NAME)`). The field holds 15 bytes and `cluster:` spends 8, so a
  pair is filled from the right: the session is written whole and the login
  takes the remaining room. Cutting the login off entirely the moment
  `login/session` overflows would be worse, because one character of name
  length would flip the shape of the name. This is `comm` only: `ps aux` still shows the
  real command line, and watcher detection matches on the command line, not the
  name.

## Credentials

- A secret file readable by group or other is refused, with the `chmod` to run.
  `doctor` also checks the directory mode, because 600 files in a
  group-writable directory protect nothing: someone else can replace them.
- `doctor` reports any file in a credential directory that is not a credential.
  Clutter is how such a directory drifts out of 700 and accumulates stray
  copies of a secret.
- The NERSC certificate exchange is a single HTTPS POST to sshproxy whose
  response is the private key with the certificate appended, so no vendor binary
  is needed. A code consumed in the last seconds of its window can arrive after
  the window closes, so the exchange waits for a fresh window when fewer than 3
  seconds remain.
