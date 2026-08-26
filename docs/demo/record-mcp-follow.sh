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
# scratch, tears down the recording's own tmux session and exits non-zero,
# leaving a previously approved clip byte-identical.
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
# The environment overrides below exist for the contracts in
# tests/test_docs_visual_assets.py, which drive this boundary against a fake
# VHS inside a temporary directory. Their defaults are the repository-relative
# paths published in docs/demo/visual-storytelling.md ("MCP follow"), and any
# override has to keep the candidate in the published clip's own directory:
# promotion is a rename, which is atomic only there. The wrapper checks that
# below rather than trusting it.
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd -- "$root"

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
reviewed_tape_sha256=771a88d89e0e8fdb242d5e264b556ca868d67ac26ef0c42e1776d85d2f2c2596
expected_digest=${KORVID_MCP_TAPE_SHA256:-$reviewed_tape_sha256}
candidate=${KORVID_MCP_CANDIDATE:-docs/assets/scenes/.mcp-follow-demo.candidate.mp4}
final=${KORVID_MCP_FINAL:-docs/assets/scenes/mcp-follow-demo.mp4}
ok_marker=${KORVID_MCP_CLIENT_OK:-.korvid-mcp-demo-client-ok}
failed_marker=${KORVID_MCP_CLIENT_FAILED:-.korvid-mcp-demo-client-failed}
ready_marker=${KORVID_MCP_READY:-.korvid-mcp-demo-ready}
go_marker=${KORVID_MCP_GO:-.korvid-mcp-demo-go}
session=korvid-mcp-demo

# Scratch, never artefacts: the candidate is an unreviewed render, and the
# four markers are one run's signals, which must never decide the next one.
# Each is named literally — a glob here could reach a file no recording made.
clean_scratch() {
  rm -f -- "$candidate" "$ok_marker" "$failed_marker" "$ready_marker" "$go_marker"
}

# Only the session this recording composes, and only by name.
end_session() {
  if command -v tmux >/dev/null 2>&1; then
    tmux kill-session -t "$session" >/dev/null 2>&1 || true
  fi
}

cleanup() {
  clean_scratch
  end_session
}

trap cleanup EXIT
trap 'cleanup; exit 130' INT TERM

fail() {
  printf 'record-mcp-follow.sh: rejecting this recording: %s\n' "$1" >&2
  printf 'record-mcp-follow.sh: %s is unchanged\n' "$final" >&2
  exit 1
}

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
# a failure outranks a success, because a client that raised inside its own
# closing hold — after publishing success — is still a failed run.
[ ! -e "$failed_marker" ] || fail "the client pane reported a failed run"
[ -e "$ok_marker" ] || fail "the client pane did not report a completed run"
[ -s "$candidate" ] || fail "vhs produced no candidate recording"

# One rename, in the directory the clip already lives in — checked above, not
# assumed: a reader either sees the previous asset or the whole new one, never
# a half-written file.
mv -f -- "$candidate" "$final"
printf 'record-mcp-follow.sh: published %s\n' "$final"
