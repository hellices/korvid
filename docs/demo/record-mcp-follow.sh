#!/usr/bin/env bash
#
# Record the MCP follow clip, and publish it only if the recording is whole.
#
# VHS has no failure channel. It renders the timeline its tape describes and
# exits 0 whatever the shell it typed into did, so an `exit 1` inside
# docs/demo/mcp-follow.tape rejected nothing: by the time that check ran, VHS
# had already written the file named by the tape's `Output`. While `Output`
# was the published clip, every failed take — a client that died on its second
# call, a scene whose MCP server never bound — overwrote a reviewed asset with
# a truncated story and then announced a rejection it could not carry out.
#
# The verdict therefore lives here, outside VHS. The tape renders to a
# candidate beside the published clip and never to the clip itself; this
# wrapper reads the verdict the client pane left in two repository-local
# markers and promotes the candidate in a single rename only when the failure
# marker is absent and the success marker is present. Every other path — VHS
# itself failing, a failure marker, a missing success marker, a missing
# candidate — prints one reason on stderr, removes the candidate and the run's
# scratch, tears down the recording's own tmux server — a private socket in the
# checkout, never the user's shared one — and exits non-zero, leaving a
# previously approved clip byte-identical.
#
# Which tape may run is settled the same way, and just as bluntly: by its
# bytes. `reviewed_tape_sha256` below is the SHA-256 of the reviewed
# docs/demo/mcp-follow.tape, and a tape that does not hash to it is refused
# before VHS starts. So this wrapper never asks what a directive would do —
# an edit is an unreviewed tape whatever it spells, and recording one means
# reviewing its bytes and moving the pin, in that order.
#
# Usage, from anywhere in the checkout:
#
#   docs/demo/record-mcp-follow.sh
#
# and with nothing in the environment. The overrides below let the Bash
# contracts drive this boundary against a fake VHS in a temporary directory,
# and they are refused everywhere else: each one names something this script
# deletes or a tmux server it kills a session on, so a stale export or a
# mistyped copy would otherwise be carried out rather than questioned. An
# override therefore costs two declarations — KORVID_MCP_TEST_MODE=1 and a
# KORVID_MCP_TEST_ROOT outside this checkout — and every path a run may
# destroy has to sit inside that root before any of it can run.
set -euo pipefail

root=$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
cd -- "$root"

# Defined here rather than beside the checks below because the first refusals
# this script can reach now happen before anything is cleaned up, and a
# refusal that cannot print is a refusal nobody acts on.
fail() {
  printf 'record-mcp-follow.sh: rejecting this recording: %s\n' "$1" >&2
  printf 'record-mcp-follow.sh: %s is unchanged\n' "$final" >&2
  exit 1
}

vhs_bin=${KORVID_MCP_VHS_BIN:-vhs}
tape=${KORVID_MCP_TAPE:-docs/demo/mcp-follow.tape}
# The raw SHA-256 of docs/demo/mcp-follow.tape as it was reviewed. This is the
# whole preflight: the wrapper runs that file and no other, so what any
# directive of VHS's grammar means is VHS's business and never this script's.
#
# Moving this value is therefore an act with a stated meaning — the new bytes
# were read and approved — and it is the only way to record an edited tape.
# Recompute it *after* that review, never to make a refusal go away:
#
#   sha256sum docs/demo/mcp-follow.tape     # or: shasum -a 256 <same>
#
# docs/demo/visual-storytelling.md and the 2026-08-26 plan publish the same
# digest, and a contract compares all three against the shipped tape.
reviewed_tape_sha256=60334eb07ab42901a4885584174b9f1bfe4089f1ebdb685f64c8e136cbe2a743
expected_digest=$reviewed_tape_sha256
test_mode=${KORVID_MCP_TEST_MODE:-0}
# The directory an isolated run declares and is held to. Empty is the normal
# case: a contributor's run has no overrides to confine, so it needs no root.
test_root=${KORVID_MCP_TEST_ROOT:-}
resolved_test_root=
candidate=${KORVID_MCP_CANDIDATE:-docs/assets/scenes/.mcp-follow-demo.candidate.mp4}
final=${KORVID_MCP_FINAL:-docs/assets/scenes/mcp-follow-demo.mp4}
ok_marker=${KORVID_MCP_CLIENT_OK:-.korvid-mcp-demo-client-ok}
failed_marker=${KORVID_MCP_CLIENT_FAILED:-.korvid-mcp-demo-client-failed}
ready_marker=${KORVID_MCP_READY:-.korvid-mcp-demo-ready}
go_marker=${KORVID_MCP_GO:-.korvid-mcp-demo-go}
session=korvid-mcp-demo
# The tmux server this recording composes on, and the reason the fixed
# session name above is safe. tmux's default socket belongs to the invoking
# user and carries every session they are already running, so `korvid-mcp-demo`
# there is a name this script merely hopes nobody else took — and the teardown
# below, which the EXIT trap runs on *every* path including the refusals that
# happen before VHS creates anything, would then kill a developer's own work.
# A socket inside the checkout is a server this recording creates, owns and
# removes: the name cannot collide, because nothing else speaks to this socket.
# The tape composes on the same literal path (it cannot read this environment —
# VHS types shell into a pane), and a contract compares the two.
socket=${KORVID_MCP_TMUX_SOCKET:-.korvid-mcp-demo.tmux.sock}

# Nothing above this line has been checked, and every one of those values names
# something a run destroys: five paths `cleanup` unlinks, the clip it promotes
# to, and the socket it kills a session on. The `EXIT` trap that does all of
# that fires on refusals too, so it may not exist yet. What follows settles
# every target first — the gate, the root, then each path — and only then arms
# it.
#
# Outside the isolated contract mode there is nothing to settle, because there
# is nothing to configure: the wrapper records with the repository-relative
# defaults above and refuses every other value. Listing them one by one keeps
# this readable and needs no indirection, which Bash 3.2 — the shell macOS
# ships — does not offer for this.
overridden=
note_override() {
  if [ "${2:-}" = x ]; then
    overridden="$overridden $1"
  fi
}

note_override KORVID_MCP_VHS_BIN "${KORVID_MCP_VHS_BIN+x}"
note_override KORVID_MCP_TAPE "${KORVID_MCP_TAPE+x}"
note_override KORVID_MCP_TAPE_SHA256 "${KORVID_MCP_TAPE_SHA256+x}"
note_override KORVID_MCP_TEST_ROOT "${KORVID_MCP_TEST_ROOT+x}"
note_override KORVID_MCP_CANDIDATE "${KORVID_MCP_CANDIDATE+x}"
note_override KORVID_MCP_FINAL "${KORVID_MCP_FINAL+x}"
note_override KORVID_MCP_CLIENT_OK "${KORVID_MCP_CLIENT_OK+x}"
note_override KORVID_MCP_CLIENT_FAILED "${KORVID_MCP_CLIENT_FAILED+x}"
note_override KORVID_MCP_READY "${KORVID_MCP_READY+x}"
note_override KORVID_MCP_GO "${KORVID_MCP_GO+x}"
note_override KORVID_MCP_TMUX_SOCKET "${KORVID_MCP_TMUX_SOCKET+x}"

case "$test_mode" in
0 | 1) ;;
*) fail "KORVID_MCP_TEST_MODE must be 0 or 1" ;;
esac

if [ -n "$overridden" ] && [ "$test_mode" != 1 ]; then
  fail "these are available only to isolated contract tests, which declare\
 KORVID_MCP_TEST_MODE=1 and a KORVID_MCP_TEST_ROOT of their own outside this\
 checkout:$overridden"
fi

if [ "$test_mode" = 1 ]; then
  [ -n "$test_root" ] ||
    fail "KORVID_MCP_TEST_MODE=1 must come with a KORVID_MCP_TEST_ROOT:\
 the directory this run is confined to"
  test_root=${test_root%/}
  case "$test_root" in
  /?*) ;;
  *) fail "KORVID_MCP_TEST_ROOT must be an absolute directory, not $test_root" ;;
  esac
  [ -d "$test_root" ] || fail "KORVID_MCP_TEST_ROOT is not a directory: $test_root"
  resolved_test_root=$(cd -P -- "$test_root" >/dev/null 2>&1 && pwd -P) ||
    resolved_test_root=""
  [ -n "$resolved_test_root" ] ||
    fail "KORVID_MCP_TEST_ROOT cannot be resolved: $test_root"
  # A checkout is what isolation is measured against, so it cannot also be the
  # thing measured: a root inside this repository puts scratch and publication
  # in a working tree, and a root containing it confines nothing about it.
  case "$root/" in
  "$resolved_test_root"/*)
    fail "KORVID_MCP_TEST_ROOT must sit outside this checkout,\
 and $resolved_test_root contains it"
    ;;
  esac
  case "$resolved_test_root/" in
  "$root"/*)
    fail "KORVID_MCP_TEST_ROOT must sit outside this checkout,\
 and $resolved_test_root is inside it"
    ;;
  esac
fi

# Two checks, because a path can leave a root two ways. The lexical half reads
# the spelling: an absolute path, no `..` component, and a prefix that is the
# declared root. The physical half resolves the deepest ancestor that exists
# and compares that, which is what catches a spelling that never leaves the
# root but walks through a link to somewhere that does. The declared root is
# the prefix for the first and its resolved form for the second, so a root
# reached through a link — every temporary directory on macOS — still matches
# itself.
confine_to_test_root() {
  local name=$1
  local target=$2
  local probe
  local settled
  case "$target" in
  /?*) ;;
  *) fail "$name must be an absolute path inside KORVID_MCP_TEST_ROOT; it is $target" ;;
  esac
  case "$target" in
  */../* | */.. | ../* | ..)
    fail "$name may not walk out of KORVID_MCP_TEST_ROOT with '..'; it is $target"
    ;;
  esac
  case "$target/" in
  "$test_root"/*) ;;
  *)
    fail "$name must name a path inside KORVID_MCP_TEST_ROOT ($test_root); it is $target"
    ;;
  esac
  [ ! -L "$target" ] ||
    fail "$name may not be a symbolic link; it is $target"
  probe=$target
  while [ ! -d "$probe" ]; do
    settled=$(dirname -- "$probe")
    [ "$settled" != "$probe" ] ||
      fail "$name has no directory inside KORVID_MCP_TEST_ROOT to resolve: $target"
    probe=$settled
  done
  probe=$(cd -P -- "$probe" >/dev/null 2>&1 && pwd -P) || probe=""
  [ -n "$probe" ] || fail "$name cannot be resolved inside KORVID_MCP_TEST_ROOT: $target"
  case "$probe/" in
  "$resolved_test_root"/*) ;;
  *)
    fail "$name resolves to $probe, outside KORVID_MCP_TEST_ROOT ($resolved_test_root)"
    ;;
  esac
}

if [ "$test_mode" = 1 ]; then
  confine_to_test_root KORVID_MCP_CANDIDATE "$candidate"
  confine_to_test_root KORVID_MCP_FINAL "$final"
  confine_to_test_root KORVID_MCP_CLIENT_OK "$ok_marker"
  confine_to_test_root KORVID_MCP_CLIENT_FAILED "$failed_marker"
  confine_to_test_root KORVID_MCP_READY "$ready_marker"
  confine_to_test_root KORVID_MCP_GO "$go_marker"
  confine_to_test_root KORVID_MCP_TMUX_SOCKET "$socket"
  expected_digest=${KORVID_MCP_TAPE_SHA256:-$reviewed_tape_sha256}
fi

# Cleanup must know whether the candidate is another spelling of the approved
# final before the EXIT trap can run. Direct equality covers missing parents;
# physical parent comparison also catches a directory reached through a symlink.
candidate_aliases_final=0
if [ "$candidate" = "$final" ]; then
  candidate_aliases_final=1
else
  alias_candidate_parent=$(cd -P -- "$(dirname -- "$candidate")" >/dev/null 2>&1 && pwd -P) ||
    alias_candidate_parent=""
  alias_final_parent=$(cd -P -- "$(dirname -- "$final")" >/dev/null 2>&1 && pwd -P) ||
    alias_final_parent=""
  if [ -n "$alias_candidate_parent" ] &&
    [ "$alias_candidate_parent" = "$alias_final_parent" ] &&
    [ "$(basename -- "$candidate")" = "$(basename -- "$final")" ]; then
    candidate_aliases_final=1
  fi
fi

# Scratch, never artefacts: the candidate is an unreviewed render, and the
# four markers are one run's signals, which must never decide the next one.
# Each is named literally — a glob here could reach a file no recording made.
clean_scratch() {
  if [ "$candidate_aliases_final" -eq 0 ]; then
    rm -f -- "$candidate"
  fi
  rm -f -- "$ok_marker" "$failed_marker" "$ready_marker" "$go_marker"
}

# Only this recording's own server, and only through its own socket. Every
# tmux command here carries `-S`, so a refusal that runs before any server
# exists can address nothing but an empty path, and the shared default server
# is never even asked a question. The socket file itself is a recording side
# effect like the markers above, so it goes too.
end_session() {
  if command -v tmux >/dev/null 2>&1; then
    tmux -S "$socket" kill-session -t "$session" >/dev/null 2>&1 || true
  fi
  rm -f -- "$socket"
}

cleanup() {
  clean_scratch
  end_session
}

trap cleanup EXIT
trap 'cleanup; exit 130' INT TERM

[ "$candidate_aliases_final" -eq 0 ] ||
  fail "the candidate and published clip must be different files"

[ -f "$tape" ] || fail "no tape to record"

# Hash the tape before handing it to VHS. Linux ships coreutils' sha256sum;
# macOS ships shasum, a perl script, instead. Both print the digest as their
# first field, so either answers the only question asked here. A host with
# neither cannot check anything, and "unable to check" must never read as
# "checked": that case is a refusal like any other, and so is a tape whose
# bytes cannot be read at all.
tape_sha256() {
  local target=$1
  local line
  if command -v sha256sum >/dev/null 2>&1; then
    line=$(sha256sum -- "$target") || return 1
  elif command -v shasum >/dev/null 2>&1; then
    line=$(shasum -a 256 -- "$target") || return 1
  else
    return 2
  fi
  printf '%s' "${line%% *}"
}

digest_status=0
actual_digest=$(tape_sha256 "$tape") || digest_status=$?
case "$digest_status" in
0) ;;
2) fail "neither sha256sum nor shasum is available to check the tape's bytes" ;;
*) fail "the tape's bytes could not be hashed" ;;
esac

# The comparison, and with it the reason VHS is trusted with this tape at all.
# Nothing here reads a directive: the reviewed bytes were approved as a whole,
# so an edit is refused whatever it spells and wherever it sits — a space, a
# tab, a quote, a comment, a repointed path, a second directive on a line
# already carrying one. What that edit would have done to VHS is exactly the
# question this wrapper no longer has to answer.
[ "$actual_digest" = "$expected_digest" ] ||
  fail "the tape is not the reviewed recording script; review its bytes, then move the pin"

# Defence in depth behind the digest, for the one mistake a digest cannot
# catch: a pin moved onto bytes nobody read carefully. VHS honours every
# `Output` it is given, so a tape naming the published clip would put that
# clip back under VHS's pen, where a failed take overwrites it before anything
# here can object. The needle is derived from the very path this script
# promotes to, so it can never drift from it, and it is looked for literally,
# in the tape's bytes: every spelling falls at once — absolute,
# repository-relative, `./` and through `../`.
#
# This is deliberately stricter than VHS. The canonical name inside a comment
# is a tape VHS would render and this wrapper rejects; that false positive
# costs a tape author one word and buys a rule with nothing to reason about.
# The shipped tape passes because the candidate is
# `.mcp-follow-demo.candidate.mp4`, which does not contain the published
# basename. Any failure to scan is a refusal too — an unreadable tape is a
# tape nobody reviewed.
final_name=$(basename -- "$final")
[ -n "$final_name" ] || fail "the published clip has no name to guard"
scan=0
grep -qF -- "$final_name" "$tape" || scan=$?
case "$scan" in
0) fail "the tape names the published clip; it may only name the candidate" ;;
1) ;;
*) fail "the tape could not be read for the published clip's name" ;;
esac

# The mirror image of that guard, and the one thing the digest genuinely does
# not settle: which file this run then grades. `KORVID_MCP_CANDIDATE` is set
# independently of the tape, so a pinned tape and a mismatched override would
# leave the wrapper promoting a file this recording never wrote. The
# candidate's own name has to appear in the tape's bytes. Like the guard
# above, this looks for a literal string and parses nothing.
candidate_name=$(basename -- "$candidate")
[ -n "$candidate_name" ] || fail "the candidate has no name to look for"
scan=0
grep -qF -- "$candidate_name" "$tape" || scan=$?
case "$scan" in
0) ;;
1) fail "the tape does not name the candidate this run would promote" ;;
*) fail "the tape could not be read for the candidate's name" ;;
esac

# A stale marker from an interrupted run would certify this one.
clean_scratch
mkdir -p -- "$(dirname -- "$candidate")"

# Promotion is a single `mv`, and `mv` is `rename(2)` only while both paths
# share a directory — and therefore a filesystem. Across two of them it
# degrades to copy-then-unlink, which is exactly the half-written asset this
# boundary exists to prevent. The defaults above put the candidate beside the
# published clip, but `KORVID_MCP_CANDIDATE` and `KORVID_MCP_FINAL` are set
# independently: any override has to preserve that, and this is where it is
# checked rather than assumed.
#
# Both parents are resolved physically — `cd -P` then `pwd -P` — so one
# directory reached through a symlink is still one directory, and no string
# comparison of two spellings decides it. The candidate's parent is created
# first, exactly as a checkout expects; the published clip's parent is only
# resolved, never created, or the wrapper could invent the destination it is
# about to compare against. And the check stands in front of VHS, not in
# front of the `mv`: a recording that cannot be promoted atomically is a
# recording nobody should pay to make.
candidate_parent=$(cd -P -- "$(dirname -- "$candidate")" >/dev/null 2>&1 && pwd -P) ||
  candidate_parent=""
[ -n "$candidate_parent" ] || fail "the candidate's directory could not be resolved"
final_parent=$(cd -P -- "$(dirname -- "$final")" >/dev/null 2>&1 && pwd -P) || final_parent=""
[ -n "$final_parent" ] || fail "the published clip's directory does not exist"
[ "$candidate_parent" = "$final_parent" ] ||
  fail "the candidate must be rendered in the published clip's own directory"

status=0
"$vhs_bin" "$tape" || status=$?
[ "$status" -eq 0 ] || fail "vhs exited ${status}"

# The verdict the client pane published, in the order that keeps it honest:
# a failure outranks a success. The client publishes its success only once
# the story, its session and its transport have all closed cleanly, so the
# two markers should never appear together — and a run that somehow produced
# both is a failed one.
[ ! -e "$failed_marker" ] || fail "the client pane reported a failed run"
[ -e "$ok_marker" ] || fail "the client pane did not report a completed run"
[ -s "$candidate" ] || fail "vhs produced no candidate recording"

# One rename, in the directory the clip already lives in — checked above, not
# assumed: a reader either sees the previous asset or the whole new one, never
# a half-written file.
mv -f -- "$candidate" "$final"
printf 'record-mcp-follow.sh: published %s\n' "$final"
