#!/usr/bin/env bash
# Install cctop: link it into ~/.local/bin. Nothing else on the system is changed.
set -euo pipefail

src="$(cd "$(dirname "$0")" && pwd)/cctop.py"
bin="${HOME}/.local/bin"

python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || {
    echo "cctop needs Python 3.9 or newer" >&2; exit 1; }

mkdir -p "$bin"
chmod +x "$src"
ln -sf "$src" "$bin/cctop"
echo "installed: $bin/cctop -> $src"

case ":$PATH:" in
    *":$bin:"*) ;;
    *) echo "note: $bin is not on your PATH yet" ;;
esac

echo
echo "optional: record sessions into ~/Projects/<name>/_project/ (Projects tab)"
echo "  see docs/project-notes.md for the hook and CLAUDE.md snippet"
