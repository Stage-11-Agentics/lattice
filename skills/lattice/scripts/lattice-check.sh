#!/usr/bin/env bash
# lattice-check.sh — Verify Lattice is installed and initialized.
# Exit 0 if ready, non-zero otherwise. Prints diagnostic info.

set -euo pipefail

# Check if lattice binary is available
if ! command -v lattice &>/dev/null; then
    echo "ERROR: 'lattice' command not found."
    echo ""
    echo "Install with one of:"
    echo "  pip install lattice-tracker"
    echo "  pipx install lattice-tracker"
    echo "  uv tool install lattice-tracker"
    exit 1
fi

echo "lattice binary: $(command -v lattice)"
echo "version: $(lattice --version 2>/dev/null || echo 'unknown')"

# Check if .lattice/ exists in current directory or any parent
DIR="$PWD"
while [ "$DIR" != "/" ]; do
    if [ -d "$DIR/.lattice" ]; then
        echo "project root: $DIR"
        echo "project code: $(python3 -c "import json; print(json.load(open('$DIR/.lattice/config.json')).get('project_code', 'none'))" 2>/dev/null || echo 'unknown')"

        # Count active tasks
        TASK_COUNT=$(find "$DIR/.lattice/tasks" -name '*.json' 2>/dev/null | wc -l | tr -d ' ')
        echo "active tasks: $TASK_COUNT"

        echo ""
        echo "Lattice is ready."
        exit 0
    fi
    DIR="$(dirname "$DIR")"
done

echo ""
echo "No .lattice/ directory found in this project."
echo "Initialize with: lattice init --project-code PROJ"
exit 2
