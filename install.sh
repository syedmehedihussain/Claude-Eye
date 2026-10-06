#!/usr/bin/env bash
# Install cctop: link it into ~/.local/bin.
#   --skill   also link the project-notes skill into ~/.claude/skills
# Nothing else on the system is changed.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
src="$here/cctop.py"
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

if [ "${1:-}" = "--skill" ]; then
    skills="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills"
    mkdir -p "$skills"
    ln -sfn "$here/skills/project-notes" "$skills/project-notes"
    echo "installed: $skills/project-notes (start a new Claude Code session to load it)"
fi

echo
echo "optional: record sessions into ~/Projects/<name>/_project/ (Projects tab)"
echo "  see docs/project-notes.md for the hook, the skill and the CLAUDE.md snippet"
