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

# Read the tape before handing it to VHS. VHS honours every `Output` it is
# given, so a second one — or an edit of the first back to the published
# path — would put the clip back under VHS's pen, where a failed take
# overwrites it before anything here can object.
#
# Two checks stand here, and the first one parses nothing. VHS's grammar is
# whitespace-separated tokens, not lines: `Hide` takes no argument, so
# `Hide Output <clip>` is two directives VHS obeys on one line, and so are
# `Sleep 1s Output <clip>` and `Enter Output <clip>`. Any line-shaped reader
# looks at that line's first field, sees `Hide`, and waves it through. Rather
# than grow a second VHS parser here to chase that — the losing half of the
# race, since the tape's real reader is VHS — the wrapper refuses the
# published clip's own basename anywhere in the tape's bytes. Whatever the
# grammar, VHS cannot write that file without naming it, and the name is
# derived from the very path this script promotes to, so it can never drift
# from it. Every spelling is covered at once: absolute, repository-relative,
# `./` and through `../`.
#
# This is deliberately stricter than VHS. The canonical name inside a comment
# is a tape VHS would render and this wrapper rejects; that false positive is
# the price of a guard no lexer change can outflank, and it costs a tape
# author one word. The shipped tape passes because the candidate is
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

# The second check is the tape's normal shape, read the way VHS reads it.
# VHS has no line in its grammar: its lexer emits whitespace-separated tokens
# and its parser gives each directive the arguments that directive takes, so
# `Hide`, which takes none, ends where the next token begins. `Hide Output
# <clip>`, `Sleep 1s Output <clip>` and `Enter Output <clip>` are each two
# directives VHS obeys, and a check that judged a line by its first field
# would see `Hide`, `Sleep` or `Enter` and wave them through. The byte guard
# above catches only the ones aimed at this clip; a second `Output` aimed at
# `agent-demo.mp4` carries none of the names it looks for.
#
# So every whitespace-separated field of every non-comment line is visited
# and every field that is exactly `Output` is counted. There must be one, and
# it must be a directive of its own: the first field of its line, carrying
# exactly one argument, and that argument must be the candidate. Requiring it
# to stand alone is the point — the wrapper cannot know what else a shared
# line does without becoming VHS's parser, so it declines to reason about it.
# One argument is part of the same rule: the candidate is a repository-relative
# path with no whitespace in it, so a trailing second field is not a longer
# path, it is a directive nobody reviewed.
#
# awk splits on runs of blanks and ignores leading ones, which is exactly VHS's
# own normalisation: `  Output <clip>`, a tab-indented `Output` and
# `Output<TAB><clip>` are all directives it obeys, and all of them are read
# here. Two edges of this scan are deliberate, and neither is a bug. A
# full-line comment is skipped, because VHS ignores it and the shipped tape
# says the word `Output` in its own prose. An `Output` inside a `Type` string
# or after a trailing `#` is *not* skipped: VHS would type or ignore it, this
# wrapper refuses it, and a tape author spends one word rewording.
verdict=$(
  awk -v want="$candidate" '
    substr($1, 1, 1) == "#" { next }
    {
      for (i = 1; i <= NF; i++) {
        if ($i != "Output") continue
        seen += 1
        if (i != 1 || NF != 2) shape = 1
        else if ($2 != want) elsewhere = 1
      }
    }
    END {
      if (seen != 1) print "count"
      else if (shape) print "shape"
      else if (elsewhere) print "elsewhere"
      else print "ok"
    }
  ' <"$tape"
)
case "$verdict" in
ok) ;;
count) fail "the tape must declare exactly one Output" ;;
shape) fail "the tape's Output must be a directive of its own naming exactly one path" ;;
*) fail "the tape must render to the candidate, never to another asset" ;;
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
