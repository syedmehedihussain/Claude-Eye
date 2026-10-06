#!/usr/bin/env bash
# Create ~/Projects/<name>/_project/ with a STATUS.md and keep it out of git.
# Usage: ensure_project.sh <name> [extra code path ...]
# Safe to re-run: existing files are never overwritten.
set -euo pipefail

name="${1:?usage: ensure_project.sh <name> [extra code path ...]}"
shift
case "$name" in
    */* | .* | _*) echo "invalid project name: $name" >&2; exit 1 ;;
esac

root="${CCTOP_PROJECTS:-$HOME/Projects}"
project="$root/$name"
meta="$project/_project"
here="$(cd "$(dirname "$0")/.." && pwd)"

mkdir -p "$meta"

tilde() { case "$1" in "$HOME"*) printf '~%s' "${1#"$HOME"}" ;; *) printf '%s' "$1" ;; esac; }

if [ ! -e "$meta/STATUS.md" ]; then
    paths="$(tilde "$project")"
    for p in "$@"; do
        paths="$paths, $(tilde "$(realpath -m "$p")")"
    done
    sed -e "s|{{name}}|$name|g" \
        -e "s|{{summary}}|One line: what this project is|" \
        -e "s|{{paths}}|$paths|" \
        -e "s|{{date}}|$(date +%F)|" \
        "$here/templates/STATUS.md" > "$meta/STATUS.md"
    echo "created $meta/STATUS.md"
else
    echo "exists  $meta/STATUS.md"
fi

# keep notes out of git without touching a tracked .gitignore
if top="$(git -C "$project" rev-parse --show-toplevel 2>/dev/null)"; then
    gitdir="$(git -C "$project" rev-parse --absolute-git-dir)"
    entry="/$(realpath --relative-to="$top" "$meta")/"
    exclude="$gitdir/info/exclude"
    mkdir -p "$(dirname "$exclude")"
    if ! grep -qxF "$entry" "$exclude" 2>/dev/null; then
        printf '\n# cctop project notes\n%s\n' "$entry" >> "$exclude"
        echo "excluded $entry from git"
    fi
fi
