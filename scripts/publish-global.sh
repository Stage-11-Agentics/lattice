#!/usr/bin/env bash
# Publish current source to the global lattice tool install.
#
# uv caches built wheels aggressively. Without cleaning the cache first,
# `uv tool install . --force` silently serves a stale build. This script
# ensures a clean rebuild every time.

set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

echo "Cleaning uv build cache for lattice-tracker..."
uv cache clean lattice-tracker --force 2>/dev/null || true

echo "Installing from source..."
output=$(uv tool install . --force 2>&1)
echo "$output"

if echo "$output" | grep -q "Building lattice-tracker"; then
    echo ""
    echo "Fresh build confirmed."
else
    echo ""
    echo "WARNING: Did not see 'Building lattice-tracker' in output."
    echo "The cache may not have been fully cleared. Try: uv cache clean --force"
    exit 1
fi
