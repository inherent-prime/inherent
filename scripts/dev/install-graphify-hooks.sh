#!/usr/bin/env bash
#
# install-graphify-hooks.sh — install the tracked git hooks into .git/hooks.
# Non-destructive: backs up any existing hook to <hook>.pre-graphify.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC_DIR="$REPO_ROOT/scripts/git-hooks"
GIT_DIR="$(git -C "$REPO_ROOT" rev-parse --git-path hooks 2>/dev/null || echo "$REPO_ROOT/.git/hooks")"

mkdir -p "$GIT_DIR"

for src in "$SRC_DIR"/*; do
  [ -f "$src" ] || continue
  name="$(basename "$src")"
  dest="$GIT_DIR/$name"
  if [ -e "$dest" ] && ! cmp -s "$src" "$dest"; then
    cp "$dest" "$dest.pre-graphify"
    echo "Backed up existing $name -> $name.pre-graphify"
  fi
  cp "$src" "$dest"
  chmod +x "$dest"
  echo "Installed hook: $dest"
done

echo "Done. Graph will refresh after each pull/merge. Logs: graphify-out/.refresh.log"
echo "If pre-commit was installed: staged service Python is black+ruff checked before commit."
