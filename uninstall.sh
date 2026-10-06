#!/usr/bin/env bash
# Remove the cctop link. Project notes in ~/Projects/*/_project/ are left alone.
set -euo pipefail
rm -f "${HOME}/.local/bin/cctop"
rm -rf "${HOME}/.cache/cctop"
echo "removed ~/.local/bin/cctop and ~/.cache/cctop"
echo "if you added the hook, remove the 'cctop.py hook' entries from ~/.claude/settings.json"
