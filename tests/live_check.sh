#!/usr/bin/env bash
# live_check.sh — end-to-end checks against the real clusters you have set up.
#
# Connection discipline: almost everything here rides the control masters that
# are already open, or uses `--via LOGIN` so a transfer borrows the login's
# master instead of opening its own. Only the checks marked NEW-CONN
# authenticate (one per backend), because that is the only way to exercise the
# open path. On FASRC every authentication costs a 30-second TOTP window, so
# this script must never be turned into a loop.
#
# This is the live half of the tests; the unit suite (python3 -m unittest) is
# the offline half, with made-up credentials and no network. Which clusters
# are exercised, and how, is decided per backend from local state alone
# (`cluster backends` and the logins on record; nothing is asked of a cluster
# to find out):
#   a login already up        ridden, as nearly everything here is;
#   credentials set up, but   a login is opened for the checks (one
#     no login up             authentication; on FASRC a TOTP window) and
#                             closed again at the end; NO_OPEN=1 skips instead;
#   credentials missing       skipped, saying so;
#   credentials refused       skipped, saying so: a refused credential is never
#                             spent on a check (`cluster login` by hand
#                             retries it, and a success clears the refusal).
# Cross-cluster checks need both fasrc and nersc. The archive-sync checks run
# only if that optional extra is configured.
#
# Usage: tests/live_check.sh [-q]
#   -q         print only failures and the summary
#   NO_OPEN=1  never open a login; exercise only backends with one up
#   CROSS=1    also run the real fasrc -> nersc transfer (costs TOTP windows)
set -uo pipefail

QUIET=0
[ "${1:-}" = "-q" ] && QUIET=1

SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT  # and, once there are any, the logins opened here

PASS=0; FAIL=0; SKIP=0
declare -a FAILED=()

ok()   { PASS=$((PASS+1)); [ "$QUIET" = 1 ] || printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); FAILED+=("$1"); printf '  \033[31mFAIL\033[0m %s\n' "$1"
         [ -n "${2:-}" ] && printf '       %s\n' "$2"; }
skip() { SKIP=$((SKIP+1)); [ "$QUIET" = 1 ] || printf '  \033[33mSKIP\033[0m %s (%s)\n' "$1" "${2:-}"; }
head_() { [ "$QUIET" = 1 ] || printf '\n\033[1m== %s\033[0m\n' "$1"; }
# timeout(1) is GNU coreutils; macOS has it only as Homebrew's gtimeout. With
# neither, the command runs unbounded.
bounded() {
    local secs=$1; shift
    if command -v timeout >/dev/null 2>&1; then timeout "$secs" "$@"
    elif command -v gtimeout >/dev/null 2>&1; then gtimeout "$secs" "$@"
    else "$@"
    fi
}

# check NAME EXPECTED_SUBSTRING COMMAND...
check() {
    local name="$1" want="$2"; shift 2
    local out rc
    out="$("$@" 2>&1)"; rc=$?
    if [ "$rc" -eq 0 ] && { [ -z "$want" ] || printf '%s' "$out" | grep -qF -- "$want"; }; then
        ok "$name"
    else
        bad "$name" "rc=$rc; wanted '$want'; got: $(printf '%s' "$out" | tail -3 | tr '\n' ' ')"
    fi
}

# check_fails NAME EXPECTED_SUBSTRING COMMAND...  (must exit non-zero)
check_fails() {
    local name="$1" want="$2"; shift 2
    local out rc
    out="$("$@" 2>&1)"; rc=$?
    if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -qiF -- "$want"; then
        ok "$name"
    else
        bad "$name" "rc=$rc; wanted failure containing '$want'; got: $(printf '%s' "$out" | tail -3 | tr '\n' ' ')"
    fi
}

# Where the cluster tool keeps things. `config get` reads local settings only.
setting() {
    local v
    v="$(cluster config get "$1" 2>/dev/null)" && [ -n "$v" ] && { printf '%s\n' "$v"; return; }
    printf '%s\n' "$2"
}
STATE_ROOT="$(setting STATE_ROOT "${XDG_STATE_HOME:-$HOME/.local/state}/cluster")"
CTL_DIR="$(setting CTL_DIR "$HOME/.ssh/controlmasters")"

# The login to use for a backend, read from local state rather than hardcoded:
# login names are global, so they differ per backend. Only a login whose
# master is already up counts. `cluster channels NAME` answers that from local
# process state (`ssh -O check` on the control socket), so looking opens no
# connection and costs no credential. The configured default is tried first.
login_for() {
    local b="$1" f name
    {
        cluster --backend "$b" config get DEFAULT_LOGIN 2>/dev/null
        for f in "$STATE_ROOT/$b"/*.node; do
            [ -e "$f" ] || continue
            f="${f##*/}"; printf '%s\n' "${f%.node}"
        done
    } | while IFS= read -r name; do
        [ -n "$name" ] || continue
        if cluster --backend "$b" channels "$name" >/dev/null 2>&1; then
            printf '%s\n' "$name"
            break
        fi
    done
}

# Every backend the tool knows, with its credential state (ok, ready,
# expiring, missing or refused), and which of them the checks can use
# (AVAILABLE): one with a login up, or one a login is opened on here.
BACKEND_ROWS="$(cluster backends 2>/dev/null | awk -F '  +' 'NR > 1 && $1 ~ /^[a-z][a-z0-9]*$/ { print $1 " " $4 " " $5 }')"
ALL_BACKENDS="$(printf '%s\n' "$BACKEND_ROWS" | awk 'NF { print $1 }')"
AVAILABLE=""
OPENED=""
close_opened() {
    local pair
    for pair in $OPENED; do
        cluster --backend "${pair%%:*}" close "${pair#*:}" >/dev/null 2>&1 || true
    done
}
trap 'close_opened; rm -rf "$SCRATCH"' EXIT
head_ "Backends"
while read -r B CRED DETAIL; do
    [ -n "$B" ] || continue
    L="$(login_for "$B")"
    if [ -z "$L" ]; then
        case "$CRED" in
            missing) skip "$B" "no credentials on this machine: cluster --$B config credentials" ;;
            refused) skip "$B" "its credential was $DETAIL" ;;
            *)  if [ "${NO_OPEN:-0}" = 1 ]; then
                    skip "$B" "no login up (NO_OPEN=1)"
                elif out="$(bounded 300 cluster --backend "$B" login "livechk-$B" --no-mount 2>&1)"; then
                    L="livechk-$B"
                    OPENED="${OPENED:+$OPENED }$B:$L"
                    ok "$B: opened login $L for the checks (credential $CRED)"
                else
                    bad "$B: could not open a login for the checks" \
                        "$(printf '%s' "$out" | tail -2 | tr '\n' ' ')"
                fi ;;
        esac
    else
        [ "$QUIET" = 1 ] || printf '  riding %s login %s\n' "$B" "$L"
    fi
    printf -v "LOGIN_$B" '%s' "$L"
    [ -z "$L" ] || AVAILABLE="${AVAILABLE:+$AVAILABLE }$B"
done <<EOF_ROWS
$BACKEND_ROWS
EOF_ROWS
login_of() { local v="LOGIN_$1"; printf '%s' "${!v-}"; }
has() { case " $AVAILABLE " in *" $1 "*) return 0 ;; esac; return 1; }
FIRST_B="${AVAILABLE%% *}"
FL="$(login_of "$FIRST_B")"

# The managed mount of a backend, as the kernel sees it: `cluster mounts`
# names the mount points it manages (under MOUNT_ROOT), and the mount table
# says which of them is really mounted. Never a path guessed from a login name.
kernel_has_mount() {
    if [ -r /proc/self/mounts ]; then
        # /proc escapes a space in a path as \040 (and awk -v would unescape
        # it again, hence ENVIRON)
        MP_WANT="$(printf '%s' "$1" | sed 's/ /\\040/g')" \
            awk '$2 == ENVIRON["MP_WANT"] { found = 1 } END { exit !found }' /proc/self/mounts
    else
        mount | grep -qF " on $1 ("
    fi
}
mount_of() {  # mount_of "$(cluster --backend B mounts)"
    printf '%s\n' "$1" | awk -F '  +' 'NR > 1 && NF >= 2 { print $2 }' |
    while IFS= read -r mp; do
        case "$mp" in "~"*) mp="$HOME${mp#\~}" ;; esac
        if kernel_has_mount "$mp"; then printf '%s\n' "$mp"; break; fi
    done
}

# ---------------------------------------------------------------- offline ----
head_ "Offline (no cluster contact)"

HERE="$(cd "$(dirname "$0")" && pwd)"
out="$(python3 -m unittest discover -s "$HERE" -p 'test_*.py' 2>&1 | tail -3)"
printf '%s' "$out" | grep -q '^OK' \
    && ok "unit tests ($(printf '%s' "$out" | grep -oE 'Ran [0-9]+' | cut -d' ' -f2))" \
    || bad "unit tests" "$out"
out="$(CLUSTER_FORCE_PORTABLE=1 python3 -m unittest discover -s "$HERE" -p 'test_*.py' 2>&1 | tail -2)"
printf '%s' "$out" | grep -q '^OK' && ok "unit tests under CLUSTER_FORCE_PORTABLE" || bad "portable tests" "$out"

check "backends lists both"        "nersc"   cluster backends
check "fasrc node classes"         "direct"  cluster --backend fasrc nodes
check "nersc login nodes jump"     "via pool" cluster nersc:nodes
check "nersc has dtn transfer class" "dtn"   cluster nersc:nodes
check_fails "unknown backend refused" "backend" cluster --backend nope list
check_fails "leading-dash session name refused" "session name" \
    cluster new-session -- '--resume'
check_fails "ambiguous transfer direction refused" "cannot tell which side is local" \
    cluster transfer "$SCRATCH/nope-a" "$SCRATCH/nope-b"

# -- dispatch rules. Nearly all local; `ls` asks each live login, and `run`
# rides an existing master.
head_ "Dispatch (the verb comes first; no bare names)"
if [ -n "$FL" ]; then
    check_fails "a bare login name is not a command" "unknown command" cluster "$FL"
    check_fails "  ...and it names the command meant" "cluster attach" cluster "$FL"
else
    skip "a bare login name is not a command" "no backend is available (see Backends)"
fi
check_fails "a bare unknown name is not a command" "cluster login zzznope" cluster zzznope
check_fails "a stray flag is still a flag" "unknown option" cluster --resume
check "aliases reach their commands" "BACKEND" cluster ls
if [ -n "$FIRST_B" ]; then
    check "--BACKEND flag works at the end" "$FIRST_B" cluster ls --"$FIRST_B"
else
    skip "--BACKEND flag works at the end" "no backend is available (see Backends)"
fi
if has fasrc; then
    check "--BACKEND flag works at the front" "fasrc" cluster --fasrc ls
else
    skip "--BACKEND flag works at the front" "fasrc is not available (see Backends)"
fi
# Any backend other than the login's own will do: the refusal is local.
OTHER_B=""
for B in $ALL_BACKENDS; do [ "$B" = "$FIRST_B" ] || { OTHER_B="$B"; break; }; done
if [ -n "$FL" ] && [ -n "$OTHER_B" ]; then
    check_fails "an explicit backend that disagrees is refused" "is on" \
        cluster where "$FL" --"$OTHER_B"
else
    skip "an explicit backend that disagrees is refused" "needs a login that is up"
fi
check_fails "new with no name explains both forms" "one name opens its configured tmux/shell" \
    cluster new
check_fails "a session needs both names" "both a login and a session name" \
    cluster new-session zzznope
# A backend name inside a remote command must reach the cluster untouched.
if [ -n "$FL" ]; then
    check "--BACKEND is not read past --" "--nersc" cluster run "$FL" -- echo --nersc
else
    skip "--BACKEND is not read past --" "no backend is available (see Backends)"
fi

# The process must be identifiable in `ps` while it runs, so a long-lived
# attach does not show as a bare "python3".
if [ -n "${FL:-}" ] && [ "$(uname -s)" = Darwin ]; then
    skip "process name in ps" "macOS gives no way to rename a running process"
elif [ -n "${FL:-}" ]; then
    cluster run "$FL" -- sleep 4 >/dev/null 2>&1 &
    live_pid=$!
    sleep 1
    live_comm="$(ps -o comm= -p "$live_pid" 2>/dev/null | tr -d ' ')"
    case "$live_comm" in
        "cluster:$FL") ok "process names itself in ps ($live_comm)" ;;
        cluster*)      ok "process names itself in ps ($live_comm)" ;;
        "")            skip "process name in ps" "process already gone" ;;
        *)             bad "process name in ps" "comm was '$live_comm', wanted cluster:*" ;;
    esac
    wait "$live_pid" 2>/dev/null || true
fi

# --help is a question, and must never be answered by doing the job. Checked on
# `where` rather than `close`: if this guard ever regresses, the check itself
# should print a node, not close the connection the rest of the suite is using.
check "a command explains itself instead of running" "cluster where" cluster where --help
check "  ...and lists what its options mean" "connections only" cluster ls --help

# Completion must never contact the cluster; just prove it loads and produces
# candidates from local state only.
if [ -r "$HOME/.local/share/bash-completion/completions/cluster" ]; then
    out="$(bash -c 'set -u
        . "$HOME/.local/share/bash-completion/completions/cluster"
        COMP_WORDS=(cluster mou); COMP_CWORD=1; _cluster; printf "%s\n" "${COMPREPLY[@]}"' 2>&1)"
    printf '%s' "$out" | grep -q 'mount' && ok "bash completion offers commands" \
        || bad "bash completion" "$out"
else
    skip "bash completion" "not installed"
fi

# ------------------------------------------------------------ each backend ---
head_ "Each backend"
for B in $AVAILABLE; do
    L="$(login_of "$B")"
    head_ "$B — state over the existing master (login '$L')"

    check "$B list shows active login"  "active"  cluster --backend "$B" list
    check "$B where names a node"       "$L:"     cluster --backend "$B" where "$L"
    check "$B pins agree with live"     "$L"      cluster --backend "$B" pins
    check "$B status"                   "$L"      cluster --backend "$B" status
    check "$B doctor is clean"          "no problems" cluster --backend "$B" doctor
    # The mount checks need a mount. Find it from the tool and the kernel's
    # mount table (see mount_of), never from the login name.
    mounts_out="$(cluster --backend "$B" mounts 2>&1)"; mounts_rc=$?
    MP="$(mount_of "$mounts_out")"
    if [ -z "$MP" ]; then
        skip "$B mount is healthy" "no $B filesystem is mounted"
    elif [ "$mounts_rc" -eq 0 ] && printf '%s' "$mounts_out" | grep -qw ok; then
        ok "$B mount is healthy"
    else
        bad "$B mount is healthy" "rc=$mounts_rc; got: $(printf '%s' "$mounts_out" | tail -3 | tr '\n' ' ')"
    fi

    # -- ssh-command must emit a machine-parseable transport on stdout alone.
    out="$(cluster --backend "$B" ssh-command "$L" 2>/dev/null)"
    if [ "$(printf '%s\n' "$out" | wc -l)" = 1 ] && [ "${out#ssh }" != "$out" ]; then
        ok "$B ssh-command is one clean line on stdout"
    else
        bad "$B ssh-command stdout" "got: $out"
    fi
    # and it must actually work as a transport
    if [ -n "$out" ]; then
        got="$(eval "$out" 'printf ok' 2>/dev/null)"
        [ "$got" = "ok" ] && ok "$B ssh-command transport works" \
            || bad "$B ssh-command transport" "got: $got"
    fi

    head_ "$B — remote execution"
    check "$B run returns output"    "hello" cluster --backend "$B" run "$L" -- echo hello
    # `run` quotes its arguments, so ~ must NOT expand (documented behaviour)
    out="$(cluster --backend "$B" run "$L" -- sh -c 'cd ~ && pwd -P' 2>/dev/null)"
    [ -n "$out" ] && ok "$B remote home resolves ($out)" || bad "$B remote home" "empty"

    head_ "$B — tmux session lifecycle"
    S="clichk$$"
    check "$B new-session"    "$S" cluster --backend "$B" new-session "$L" "$S"
    check "$B session listed" "$S" cluster --backend "$B" sessions "$L"
    # ownership tag must be set, so a sweep can tell it is ours
    owner="$(cluster --backend "$B" run "$L" -- \
        sh -c "tmux show-options -qv -t $S @cluster_login" 2>/dev/null)"
    [ "$owner" = "$L" ] && ok "$B session tagged @cluster_login=$L" \
        || bad "$B session ownership tag" "got '$owner', want '$L'"
    # breadcrumb on the shared home
    crumb="$(cluster --backend "$B" run "$L" -- \
        sh -c "cat ~/.cluster/sessions/*/$S 2>/dev/null" 2>/dev/null)"
    [ "$crumb" = "$L" ] && ok "$B breadcrumb written" \
        || bad "$B breadcrumb" "got '$crumb', want '$L'"
    check "$B send-keys into session" "" cluster --backend "$B" send "$L" "$S" -- true
    check "$B new-window"  "" cluster --backend "$B" window "$L" "$S" w2
    check "$B kill-session" "$S" cluster --backend "$B" kill-session "$L" "$S"
    out="$(cluster --backend "$B" sessions "$L" 2>&1)"
    printf '%s' "$out" | grep -qw "$S" && bad "$B session removed" "still listed" \
        || ok "$B session gone after kill"
    crumb="$(cluster --backend "$B" run "$L" -- \
        sh -c "cat ~/.cluster/sessions/*/$S 2>/dev/null" 2>/dev/null)"
    [ -z "$crumb" ] && ok "$B breadcrumb removed on kill" || bad "$B breadcrumb cleanup" "$crumb"

    head_ "$B — mount round trip"
    # $MP (found above) comes from the kernel's mount table, never from the
    # login name. Under ONE_MOUNT_PER_BACKEND a second login has no mount of
    # its own, so a path guessed from its name was an empty *local* directory:
    # the stamp landed on this machine, the remote read found nothing, and the
    # dropping stayed behind in that login's mount directory. Writing into an
    # unmounted mount point is the silent-data-loss shape this suite exists to
    # catch, so it must not be the suite doing it.
    STAMP="livecheck-$$-$B"
    if [ -z "$MP" ]; then
        skip "$B mount round trip" "no $B filesystem is mounted"
    elif printf '%s\n' "$STAMP" > "$MP/.$STAMP" 2>/dev/null; then
        got="$(cluster --backend "$B" run "$L" -- sh -c "cat ~/.$STAMP" 2>/dev/null)"
        [ "$got" = "$STAMP" ] && ok "$B local write visible remotely" \
            || bad "$B local->remote" "got '$got'"
        cluster --backend "$B" run "$L" -- sh -c "rm -f ~/.$STAMP" >/dev/null 2>&1
        [ ! -e "$MP/.$STAMP" ] && ok "$B remote delete visible locally" \
            || bad "$B remote->local" "still present"
    else
        bad "$B mount write" "could not write to $MP"
    fi

    head_ "$B — transfers over the existing login master (--via)"
    mkdir -p "$SCRATCH/up/inner"; echo payload > "$SCRATCH/up/inner/f.txt"
    echo single > "$SCRATCH/one.txt"
    # cp-like: a directory lands INSIDE the destination
    check "$B upload dir (cp semantics)" "" \
        cluster --backend "$B" transfer --via "$L" -q "$SCRATCH/up" "remote:.livechk-$$/"
    got="$(cluster --backend "$B" run "$L" -- \
        sh -c "cat ~/.livechk-$$/up/inner/f.txt 2>/dev/null" 2>/dev/null)"
    [ "$got" = "payload" ] && ok "$B uploaded dir landed inside dest" \
        || bad "$B upload dir placement" "got '$got'"
    # single file download to a new name must produce a FILE, not a directory
    check "$B download file to new name" "" \
        cluster --backend "$B" transfer --via "$L" -q \
        "remote:.livechk-$$/up/inner/f.txt" "$SCRATCH/renamed.txt"
    if [ -f "$SCRATCH/renamed.txt" ] && [ "$(cat "$SCRATCH/renamed.txt")" = payload ]; then
        ok "$B downloaded single file is a file"
    else
        bad "$B download file" "$(ls -ld "$SCRATCH/renamed.txt" 2>&1)"
    fi
    # dry run must not change anything
    before="$(cluster --backend "$B" run "$L" -- \
        sh -c "ls ~/.livechk-$$ 2>/dev/null | wc -l" 2>/dev/null)"
    cluster --backend "$B" transfer --via "$L" -q -n "$SCRATCH/one.txt" \
        "remote:.livechk-$$/" >/dev/null 2>&1
    after="$(cluster --backend "$B" run "$L" -- \
        sh -c "ls ~/.livechk-$$ 2>/dev/null | wc -l" 2>/dev/null)"
    [ "$before" = "$after" ] && ok "$B --dry-run changed nothing" \
        || bad "$B dry-run" "$before -> $after"

    head_ "$B — rsync push/pull"
    check "$B pull a file" "" cluster --backend "$B" pull "$L" ".livechk-$$/up/inner/f.txt" \
        "$SCRATCH/pulled.txt"
    [ "$(cat "$SCRATCH/pulled.txt" 2>/dev/null)" = payload ] && ok "$B pull content correct" \
        || bad "$B pull" "got '$(cat "$SCRATCH/pulled.txt" 2>/dev/null)'"
    check "$B push a file" "" cluster --backend "$B" push "$L" "$SCRATCH/one.txt" \
        ".livechk-$$/"
    got="$(cluster --backend "$B" run "$L" -- \
        sh -c "cat ~/.livechk-$$/one.txt 2>/dev/null" 2>/dev/null)"
    [ "$got" = single ] && ok "$B push content correct" || bad "$B push" "got '$got'"

    # clean up the remote scratch
    cluster --backend "$B" run "$L" -- sh -c "rm -rf ~/.livechk-$$" >/dev/null 2>&1

    head_ "$B — layout snapshot"
    out="$(cluster --backend "$B" run "$L" -- \
        sh -c 'cat ~/.cluster/layout/$(hostname -s) 2>/dev/null | head -1' 2>/dev/null)"
    if printf '%s' "$out" | grep -qP '\t'; then
        ok "$B layout snapshot is tab-separated"
    elif [ -z "$out" ]; then
        skip "$B layout snapshot" "not written yet"
    else
        bad "$B layout format" "got: $out"
    fi
done

# ------------------------------------------------------- new connections -----
head_ "NEW-CONN — the transfer open path (one authentication per backend)"
[ -n "$AVAILABLE" ] || skip "transfer open" "no backend is available (see Backends)"
for B in $AVAILABLE; do
    # $L must be re-derived here: it is per-backend, and reusing the value the
    # previous loop left behind asked fasrc about a login that lives on nersc.
    L="$(login_of "$B")"
    cluster --backend "$B" transfer --close --force >/dev/null 2>&1
    echo probe > "$SCRATCH/probe.txt"
    out="$(cluster --backend "$B" transfer -q --keep "$SCRATCH/probe.txt" \
        "remote:.livechk-open-$$/" 2>&1)"; rc=$?
    if [ "$rc" -eq 0 ]; then
        got="$(cluster --backend "$B" run "$L" -- \
            sh -c "cat ~/.livechk-open-$$/probe.txt 2>/dev/null" 2>/dev/null)"
        [ "$got" = probe ] && ok "$B dedicated transfer connection works" \
            || bad "$B dedicated transfer" "uploaded but content '$got'"
    else
        bad "$B dedicated transfer connection" "$(printf '%s' "$out" | tail -2 | tr '\n' ' ')"
    fi
    check "$B closes its transfer connection" "" cluster --backend "$B" transfer --close --force
    cluster --backend "$B" run "$L" -- sh -c "rm -rf ~/.livechk-open-$$" >/dev/null 2>&1
done

# ------------------------------------------------------------- sweeping ------
head_ "Sweeping (nersc only — a FASRC sweep costs a TOTP window per node)"
if has nersc; then
    out="$(cluster nersc:clean --dry-run 2>&1)"
    if printf '%s' "$out" | grep -qE 'no reapable|kept'; then
        ok "nersc clean --dry-run reaps nothing live"
    else
        bad "nersc clean" "$out"
    fi
else
    skip "nersc clean --dry-run" "nersc is not available (see Backends)"
fi

# A real cross-cluster transfer opens a dedicated FASRC connection (agent
# forwarding is fixed when a master is created), so it costs a TOTP window that
# the rest of this suite is careful not to spend. Opt in with CROSS=1.
# Everything in this section, the planning checks included, involves both
# clusters, so it runs only when both are available.
head_ "Cross-cluster transfers"
FL="$(login_of fasrc)"; NL="$(login_of nersc)"
if ! has fasrc || ! has nersc; then
    skip "cross-cluster transfers" "needs both fasrc and nersc (see Backends)"
else
    check "a qualified path names its own cluster" "" \
        cluster transfer --via "$NL" --dry-run "nersc:~" "$SCRATCH/"
    check_fails "both sides on one cluster is refused" "both sides" \
        cluster transfer "fasrc:~/a" "fasrc:~/b"
    check_fails "an executor that cannot authenticate is refused" "cannot authenticate" \
        cluster transfer --executor nersc "fasrc:~/a" "nersc:~/b"
    if [ "${CROSS:-0}" = "1" ]; then
        probe="xfer-livecheck-$$"
        if cluster run "$FL" -- bash -lc \
                "mkdir -p ~/$probe && head -c 1048576 /dev/urandom > ~/$probe/f.bin" \
                >/dev/null 2>&1; then
            check "direct fasrc -> nersc" "" \
                cluster transfer "fasrc:~/$probe" "nersc:~/"
            want="$(cluster run "$FL" -- bash -lc \
                    "md5sum ~/$probe/f.bin | cut -d' ' -f1" 2>/dev/null | tr -d '[:space:]')"
            got="$(cluster run "$NL" -- bash -lc \
                    "md5sum ~/$probe/f.bin | cut -d' ' -f1" 2>/dev/null | tr -d '[:space:]')"
            if [ -n "$want" ] && [ "$want" = "$got" ]; then
                ok "the bytes that arrived are the bytes that left"
            else
                bad "cross-cluster checksum" "sent $want, got $got"
            fi
            # Fallbacks. A peer node that does not resolve is a setup failure,
            # so it must reach the destination anyway — through this machine.
            cluster run "$NL" -- bash -lc "rm -rf ~/$probe" >/dev/null 2>&1 || true
            out="$(cluster transfer --peer-node dtn99-does-not-exist \
                    "fasrc:~/$probe" "nersc:~/" 2>&1)"; rc=$?
            got="$(cluster run "$NL" -- bash -lc \
                    "md5sum ~/$probe/f.bin | cut -d' ' -f1" 2>/dev/null | tr -d '[:space:]')"
            if [ "$rc" -eq 0 ] && [ -n "$want" ] && [ "$want" = "$got" ] &&
                    printf '%s' "$out" | grep -q "falling back to relay"; then
                ok "an unreachable peer node falls back to relay and still arrives"
            else
                bad "relay fallback" "rc=$rc, sent $want got $got: $(printf '%s' "$out" | tail -2 | tr '\n' ' ')"
            fi
            # ...but only when the engine was left to the tool.
            check_fails "an explicitly chosen engine never falls back" "engine relay" \
                cluster transfer --engine direct --peer-node dtn99-does-not-exist \
                "fasrc:~/$probe" "nersc:~/"
            # That failed attempt owns a forwarding master with nothing behind
            # it to close it; the handover must not run when no fallback will.
            stray=""
            for sock in "$CTL_DIR"/*-fwd*; do
                [ -e "$sock" ] && stray="$stray ${sock##*/}"
            done
            [ -z "$stray" ] && ok "a failed direct transfer leaves no connection behind" \
                || bad "connection handover" "left $stray"
            cluster run "$FL" -- bash -lc "rm -rf ~/$probe" >/dev/null 2>&1 || true
            cluster run "$NL" -- bash -lc "rm -rf ~/$probe" >/dev/null 2>&1 || true
        else
            bad "cross-cluster staging" "could not write a probe file on fasrc"
        fi
    else
        skip "direct fasrc -> nersc round trip" "set CROSS=1; costs TOTP windows"
        skip "relay fallback and connection handover" "set CROSS=1"
    fi
fi

head_ "Globus engine"
if ! has fasrc || ! has nersc; then
    skip "globus engine" "needs both fasrc and nersc (see Backends)"
elif command -v globus >/dev/null 2>&1 || [ -x "$HOME/.local/bin/globus" ]; then
    check "collections are built in, no env var needed" "globus transfer" \
        env -u CLUSTER_FASRC_GLOBUS_COLLECTION -u CLUSTER_NERSC_GLOBUS_COLLECTION \
        cluster transfer --engine globus --dry-run \
        "fasrc:/n/netscratch/x" "nersc:/global/homes/u/user"
    check_fails "a FASRC home path is refused without a round trip" "does not export home" \
        cluster transfer --engine globus --dry-run "fasrc:/n/home01/user/x" "nersc:/global/homes/u/user"
    check_fails "a relative path is refused" "absolute" \
        cluster transfer --engine globus --dry-run "fasrc:~/x" "nersc:~/y"
    out="$(bounded 60 globus whoami 2>&1)"
    if printf '%s' "$out" | grep -q "@"; then
        ok "globus session is live ($out)"
    else
        skip "globus session" "not logged in: globus login --no-local-server"
    fi
else
    skip "globus engine" "globus CLI not installed"
fi

# archive-sync is an optional extra: without a config it is not a failure, just
# not part of this setup. `--list-backends` reads the config only (exit 3 means
# "not configured"); a configured-but-broken one is a real failure.
head_ "archive-sync integration (dry run)"
AS="$(command -v archive-sync 2>/dev/null || true)"
[ -n "$AS" ] || { [ -x "$HERE/../extras/archive-sync" ] && AS="$HERE/../extras/archive-sync"; }
AS_BACKENDS=""
if [ -z "$AS" ]; then
    skip "archive-sync" "not installed (optional extra)"
else
    AS_BACKENDS="$("$AS" --list-backends 2>&1)"; rc=$?
    if [ "$rc" -eq 3 ]; then
        skip "archive-sync" "not configured; optional extra, see extras/README.md"
        AS_BACKENDS=""
    elif [ "$rc" -ne 0 ]; then
        bad "archive-sync configuration" "$(printf '%s' "$AS_BACKENDS" | tail -3 | tr '\n' ' ')"
        AS_BACKENDS=""
    fi
fi
for B in $AS_BACKENDS; do
    if ! has "$B"; then
        skip "archive-sync --backend $B --dry-run" "$B is not available (see Backends)"
        continue
    fi
    out="$("$AS" --backend "$B" --dry-run 2>&1)"; rc=$?
    if [ "$rc" -eq 0 ]; then
        ok "archive-sync --backend $B --dry-run"
    else
        bad "archive-sync $B" "$(printf '%s' "$out" | tail -3 | tr '\n' ' ')"
    fi
done

# --------------------------------------------------------------- summary -----
printf '\n\033[1m== summary ==\033[0m\n'
printf '  passed: %d   failed: %d   skipped: %d\n' "$PASS" "$FAIL" "$SKIP"
if [ "$FAIL" -gt 0 ]; then
    printf '\n  failures:\n'
    for f in "${FAILED[@]}"; do printf '    - %s\n' "$f"; done
    exit 1
fi
exit 0
