#!/bin/bash
# Hook: fires when a task transitions to "review" status.
# Backgrounds a headless Claude agent to run a team three code review.
#
# Environment variables (set by Lattice hook system):
#   LATTICE_TASK_ID  — the task being reviewed
#   LATTICE_ROOT     — path to .lattice/ directory
#
# Exits immediately (<1s) — the review agent runs independently.

set -euo pipefail

TASK_ID="${LATTICE_TASK_ID:?LATTICE_TASK_ID not set}"
ROOT="${LATTICE_ROOT:?LATTICE_ROOT not set}"
PROJECT_DIR="$(dirname "$ROOT")"
LOG_DIR="$ROOT/logs"

# Ensure log directory exists
mkdir -p "$LOG_DIR"

nohup claude -p "Read $PROJECT_DIR/prompts/review-hook-prompt.md and follow the instructions. LATTICE_TASK_ID=$TASK_ID LATTICE_ROOT=$ROOT" \
  --dangerously-skip-permissions \
  > "$LOG_DIR/review-$TASK_ID.log" 2>&1 &

exit 0
