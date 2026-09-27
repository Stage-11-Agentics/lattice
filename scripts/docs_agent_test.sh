#!/usr/bin/env bash
# AC-44, the docs-agent test (docs/hosted/EVALUATION.md §3 and §5).
#
# A fresh agent, given only docs/hosted/guide.md and docs/hosted/api.md, in an
# empty temporary directory with Lattice installed from this checkout:
#   1. sets up a server, a project, a token, binds a checkout, and completes a write;
#   2. from the guide's first section only, untracks the board of a fixture
#      repository (git-tracked board, two linked worktrees), after which a write
#      in one worktree is visible in the other and git status shows no board file;
#   3. follows the guide's move-back steps on the hosted project from part 1,
#      and doctor passes on the resulting local board.
# The script then checks each outcome itself and keeps the transcript.
#
# Usage: scripts/docs_agent_test.sh [OUT_DIR]
#   AGENT_CMD   the agent to run (default: claude); it receives the prompt with -p.
#   AGENT_ARGS  extra arguments (default: stream-json output, no permission prompts).
#   AGENT_TIMEOUT seconds (default 2700).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$(mktemp -d -t lattice-docs-agent-XXXXXX)}"
mkdir -p "$OUT"
T="$(mktemp -d -t lattice-docs-agent-run-XXXXXX)"
AGENT_CMD="${AGENT_CMD:-claude}"
AGENT_TIMEOUT="${AGENT_TIMEOUT:-2700}"

# Lattice from this checkout, in its own environment, first on PATH.
uv venv -q "$T/venv" --python 3.12
uv pip install -q --python "$T/venv/bin/python" "$REPO[server]"

export HOME="$T/home"
mkdir -p "$HOME"
unset XDG_CONFIG_HOME XDG_DATA_HOME LATTICE_ROOT LATTICE_SERVER_ROOT
for v in $(env | sed -n 's/^\(LATTICE_[A-Z0-9_]*\)=.*/\1/p'); do unset "$v"; done
export PATH="$T/venv/bin:$PATH"
export GIT_AUTHOR_NAME="Docs Agent" GIT_AUTHOR_EMAIL="docs-agent@example.invalid"
export GIT_COMMITTER_NAME="Docs Agent" GIT_COMMITTER_EMAIL="docs-agent@example.invalid"

# The only instructions the agent gets.
mkdir -p "$T/work/docs"
cp "$REPO/docs/hosted/guide.md" "$REPO/docs/hosted/api.md" "$T/work/docs/"

# Part 2's fixture: a repository whose board is tracked in git, with two linked worktrees.
FIX="$T/work/fixture"
mkdir -p "$FIX/main"
(
  cd "$FIX/main"
  git init -q -b main
  lattice init --project-code FIX --actor human:fixture > /dev/null
  lattice create "Fixture task" --actor human:fixture > /dev/null
  echo fixture > README.md
  git add -A && git commit -q -m "fixture: a repository with a tracked board"
  git worktree add -q -b feature-a "$FIX/wt-a"
  git worktree add -q -b feature-b "$FIX/wt-b"
)

cat > "$T/work/PROMPT.md" <<EOF
You are testing documentation. Your only instructions are the two files in
$T/work/docs/: guide.md (the Lattice Hosted guide) and api.md. Do not read any
other Lattice documentation or source code. \`lattice\` is installed and on PATH.
Work only under $T. Do not ask questions; decide from the docs.

Do three things, in order:

1. Following the guide, set up a Lattice server on this machine, create a
   project, issue a token, bind a git checkout to the project, and complete a
   write (create a task and move it to another status) from that checkout.
2. Following only the guide's first section ("Before any server"), untrack the
   Lattice board of the repository at $FIX/main, which has two linked worktrees
   ($FIX/wt-a and $FIX/wt-b). Then show that a write made in wt-a is visible
   from wt-b, and that \`git status\` shows no board file in either worktree.
3. Following the guide's steps for moving a board back to local, move the
   hosted project you set up in part 1 back to a local board in its checkout,
   and run \`lattice doctor\` there.

Leave the server running at the end. When done, write $T/work/REPORT.md with:
the checkout path from part 1, the project slug, the server root, and, for
every place the guide was unclear, wrong, or missing a step, the section, what
happened, and what you did instead.
EOF

cd "$T/work"
set +e
timeout "$AGENT_TIMEOUT" "$AGENT_CMD" -p "$(cat PROMPT.md)" \
  ${AGENT_ARGS:---output-format stream-json --verbose --dangerously-skip-permissions} \
  > "$OUT/transcript.jsonl" 2> "$OUT/agent.stderr"
echo "agent exit: $?" > "$OUT/agent.exit"
set -e
cp "$T/work/REPORT.md" "$OUT/REPORT.md" 2> /dev/null || echo "no REPORT.md" > "$OUT/REPORT.md"

# Independent checks.
check() { if eval "$2" > /dev/null 2>&1; then echo "PASS  $1"; else echo "FAIL  $1"; fi; }
{
  echo "== part 2: fixture untracked"
  check "a write in wt-a is visible in wt-b" "(cd $FIX/wt-a && lattice create 'Checker write' --actor human:checker) && (cd $FIX/wt-b && lattice list | grep -q 'Checker write')"
  for wt in main wt-a wt-b; do
    check "after that write, git status shows no board file in $wt" "! git -C $FIX/$wt status --porcelain | grep -q '\.lattice'"
  done
  check "no tracked board files on main" "[ -z \"\$(git -C $FIX/main ls-files .lattice)\" ]"
  check "board still present in the primary" "[ -f $FIX/main/.lattice/config.json ]"
  echo "== parts 1 and 3: from the agent's report"
  sed -n '1,40p' "$OUT/REPORT.md"
} > "$OUT/checks.txt" 2>&1
cat "$OUT/checks.txt"
echo "transcript: $OUT/transcript.jsonl"
echo "scratch:    $T (server may still be running; its pid is under the server root the report names)"
