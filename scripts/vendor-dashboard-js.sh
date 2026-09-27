#!/usr/bin/env bash
# Vendor the dashboard's third-party scripts under
# src/lattice/dashboard/static/vendor/ (SPEC §10: the hosted CSP allows only
# same-origin scripts, and a local dashboard then works offline too).
#
# Each package is resolved to one exact version with `npm pack`, and the file
# unpkg serves for it (the package's "unpkg" field, else "browser", else
# "main", unless a path is given) is copied with the package's LICENSE files.
# vendor/VERSIONS.json records the version, the tarball's npm integrity, and
# each file's sha256, so a reviewer can check them against the registry.
#
# Needs npm and network access to registry.npmjs.org. Run from the repo root:
#   scripts/vendor-dashboard-js.sh
set -euo pipefail

DEST="src/lattice/dashboard/static/vendor"
# spec | file (empty: what unpkg serves for the bare spec)
PACKAGES=(
  "d3@7|"
  "d3-binarytree@1|"
  "d3-octree@1|"
  "d3-force-3d@3|"
  "force-graph@1|dist/force-graph.min.js"
  "three@0.160.0|build/three.min.js"
)

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$DEST"
entries=()
for item in "${PACKAGES[@]}"; do
  spec="${item%%|*}"
  file="${item#*|}"
  name="${spec%@*}"
  (cd "$work" && npm pack --silent --json "$spec" > "pack-$name.json")
  tarball="$(node -e 'console.log(JSON.parse(require("fs").readFileSync(process.argv[1]))[0].filename)' "$work/pack-$name.json")"
  version="$(node -e 'console.log(JSON.parse(require("fs").readFileSync(process.argv[1]))[0].version)' "$work/pack-$name.json")"
  integrity="$(node -e 'console.log(JSON.parse(require("fs").readFileSync(process.argv[1]))[0].integrity)' "$work/pack-$name.json")"
  mkdir -p "$work/$name"
  tar -xzf "$work/$tarball" -C "$work/$name"
  pkg="$work/$name/package"
  if [ -z "$file" ]; then
    file="$(node -e '
      const p = JSON.parse(require("fs").readFileSync(process.argv[1]));
      const f = p.unpkg || (typeof p.browser === "string" ? p.browser : null) || p.main;
      console.log(f.replace(/^\.\//, ""));' "$pkg/package.json")"
  fi
  rm -rf "${DEST:?}/$name"
  mkdir -p "$DEST/$name"
  cp "$pkg/$file" "$DEST/$name/$(basename "$file")"
  licenses=()
  for lic in "$pkg"/LICEN[CS]E* "$pkg"/license*; do
    [ -f "$lic" ] || continue
    cp "$lic" "$DEST/$name/"
    licenses+=("$(basename "$lic")")
  done
  if [ ${#licenses[@]} -eq 0 ]; then
    echo "error: $name@$version ships no LICENSE file" >&2
    exit 1
  fi
  sha="$(sha256sum "$DEST/$name/$(basename "$file")" | cut -d' ' -f1)"
  entries+=("$(node -e '
    const [name, version, integrity, file, sha, spec, lic] = process.argv.slice(1);
    console.log(JSON.stringify({name, spec, version, integrity, source: file,
      file: `${name}/${require("path").basename(file)}`, sha256: sha, licenses: lic.split(",")}));' \
    "$name" "$version" "$integrity" "$file" "$sha" "$spec" "$(IFS=,; echo "${licenses[*]}")")")
  echo "vendored $name@$version: $file"
done
printf '%s\n' "${entries[@]}" | node -e '
  const lines = require("fs").readFileSync(0, "utf8").trim().split("\n");
  const out = lines.map(l => JSON.parse(l));
  process.stdout.write(JSON.stringify({packages: out}, null, 2) + "\n");' > "$DEST/VERSIONS.json"
echo "wrote $DEST/VERSIONS.json"
