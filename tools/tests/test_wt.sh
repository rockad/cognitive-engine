#!/usr/bin/env bash
set -euo pipefail

# Self-contained test suite for tools/wt and .githooks/pre-commit
# Runs in an isolated temporary directory

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd -P)"

TMP_DIR="$(mktemp -d -t ce-wt-test-XXXXXX)"
trap 'rm -rf "$TMP_DIR"' EXIT

echo "=== Running tools/wt test suite in: $TMP_DIR ==="

# 1. Initialize a fake primary hub repo
HUB_DIR="$TMP_DIR/cognitive-engine"
mkdir -p "$HUB_DIR"
cd "$HUB_DIR"
git init -b main --quiet
git config user.name "Test User"
git config user.email "test@example.com"

# Copy tools/wt and .githooks
mkdir -p "$HUB_DIR/tools/tests" "$HUB_DIR/.githooks"
cp "$REPO_ROOT/tools/wt" "$HUB_DIR/tools/wt"
cp "$REPO_ROOT/.githooks/pre-commit" "$HUB_DIR/.githooks/pre-commit"
chmod +x "$HUB_DIR/tools/wt" "$HUB_DIR/.githooks/pre-commit"

# Initial commit
echo "# Test Repo" > README.md
mkdir -p docs/handoffs
echo "initial bridge text" > docs/handoffs/antigravity-claude-bridge.md
git add .
git commit -m "initial commit" --quiet

# 2. Test: tools/wt init sets core.hooksPath
"$HUB_DIR/tools/wt" init
HOOKS_PATH="$(git config --local core.hooksPath || echo "")"
if [ "$HOOKS_PATH" != ".githooks" ]; then
  echo "FAIL: tools/wt init did not set core.hooksPath=.githooks (got: '$HOOKS_PATH')" >&2
  exit 1
fi
echo "PASS: tools/wt init sets core.hooksPath=.githooks"

# 3. Test: pre-commit blocks in primary worktree
echo "uncommitted edit" >> README.md
git add README.md
if git commit -m "should fail" 2>/dev/null; then
  echo "FAIL: pre-commit hook permitted commit in primary worktree without CE_ALLOW_HUB=1" >&2
  exit 1
fi
echo "PASS: pre-commit blocks in primary worktree"

# 4. Test: CE_ALLOW_HUB=1 allows commit in primary worktree
CE_ALLOW_HUB=1 git commit -m "allowed commit" --quiet
echo "PASS: CE_ALLOW_HUB=1 overrides pre-commit in primary worktree"

# 5. Test: tools/wt new refuses a dirty hub (with uncommitted changes to other files)
echo "dirty file" > dirty.txt
git add dirty.txt
if "$HUB_DIR/tools/wt" new test-branch 2>/dev/null; then
  echo "FAIL: tools/wt new did not refuse dirty hub" >&2
  exit 1
fi
echo "PASS: tools/wt new refuses dirty hub"
git reset --hard HEAD --quiet
rm -f dirty.txt

# 6. Test: tools/wt new strictly refuses dirty hub with zero exceptions
echo "channel update" >> docs/handoffs/antigravity-claude-bridge.md
if "$HUB_DIR/tools/wt" new branch-one 2>/dev/null; then
  echo "FAIL: tools/wt new did not refuse dirty hub (bridge file uncommitted)" >&2
  exit 1
fi
echo "PASS: tools/wt new strictly refuses dirty hub with zero exceptions"
git checkout -- docs/handoffs/antigravity-claude-bridge.md 2>/dev/null || git restore docs/handoffs/antigravity-claude-bridge.md
"$HUB_DIR/tools/wt" new branch-one
if [ ! -d "$TMP_DIR/cognitive-engine-branch-one" ]; then
  echo "FAIL: tools/wt new failed to create worktree on clean hub" >&2
  exit 1
fi

# 7. Test: pre-commit passes in a linked worktree
cd "$TMP_DIR/cognitive-engine-branch-one"
echo "feature work" > feature.txt
git add feature.txt
git commit -m "feature commit" --quiet
echo "PASS: pre-commit passes in linked worktree"

# 8. Test: tools/wt new refuses a branch checked out elsewhere
cd "$HUB_DIR"
if "$HUB_DIR/tools/wt" new branch-one 2>/dev/null; then
  echo "FAIL: tools/wt new did not refuse already-checked-out branch" >&2
  exit 1
fi
echo "PASS: tools/wt new refuses a branch checked out elsewhere"

# 9. Test: tools/wt list shows active worktrees
LIST_OUT="$("$HUB_DIR/tools/wt" list)"
if ! echo "$LIST_OUT" | grep -q "cognitive-engine-branch-one"; then
  echo "FAIL: tools/wt list did not show active worktree" >&2
  exit 1
fi
echo "PASS: tools/wt list displays active worktrees"

# 10. Test: tools/wt remove cleans up worktree
"$HUB_DIR/tools/wt" remove branch-one
if [ -d "$TMP_DIR/cognitive-engine-branch-one" ]; then
  echo "FAIL: tools/wt remove did not delete worktree directory" >&2
  exit 1
fi
echo "PASS: tools/wt remove cleans up worktree"

# 11. Portable stat idiom check
stat -c %Y README.md >/dev/null 2>&1 || stat -f %m README.md >/dev/null 2>&1
echo "PASS: portable stat idiom functions correctly"

echo "=== All tools/wt test suite checks passed! ==="
