#!/usr/bin/env bash
# push_runner.sh — safely push today's Maya code to GitHub.
#
# Usage:
#   scripts/push_runner.sh [--dry-run]   # default: dry-run
#   scripts/push_runner.sh --yes         # actually push
#
# Behaviour:
#   1. cd to repo root
#   2. git init if not already a repo
#   3. honour .gitignore (built/cached files excluded)
#   4. add remote (koraussie7/maya) only if no remote exists
#   5. commit everything (no further filtering — user asked "all code")
#   6. push to main (override the empty remote if it has no branches yet)
#
# Run --dry-run first to see what would be sent.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE_URL="https://github.com/koraussie7/maya.git"
# Local branch is whatever HEAD currently points to. We do NOT hardcode "main"
# because a fresh init on older git defaulted to "master". Pushing to whatever
# the remote actually has on its default branch is the safe move; we detect it
# below before pushing.
BRANCH=""

# ── arg parsing ───────────────────────────────────────────
DO_PUSH="no"
for arg in "$@"; do
    case "$arg" in
        --dry-run) DO_PUSH="no" ;;
        --yes)     DO_PUSH="yes" ;;
        -h|--help)
            sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "unknown flag: $arg" >&2; exit 2 ;;
    esac
done

cd "$ROOT"
echo "==> root: $ROOT"

# ── git identity (per-repo, not global) ───────────────────
git config user.email  "brianyeon@users.noreply.github.com"
git config user.name   "brianyeon"

# ── git init if needed ───────────────────────────────────
if ! git rev-parse --git-dir >/dev/null 2>&1; then
    echo "==> git init"
    git init -q
fi

# Detect the local branch. If there's only one branch and HEAD is detached,
# use the configured init.defaultBranch, otherwise fall back to the existing
# branch.
BRANCH="$(git symbolic-ref --short HEAD 2>/dev/null || true)"
if [[ -z "$BRANCH" ]]; then
    BRANCH="$(git rev-parse --abbrev-ref HEAD)"
fi
if [[ "$BRANCH" == "HEAD" || -z "$BRANCH" ]]; then
    echo "ERROR: cannot determine local branch. Run 'git checkout -b main' first." >&2
    exit 3
fi
echo "==> local branch: $BRANCH"

# ── remote handling ──────────────────────────────────────
if ! git remote get-url origin >/dev/null 2>&1; then
    echo "==> adding origin: $REMOTE_URL"
    git remote add origin "$REMOTE_URL"
else
    echo "==> origin already: $(git remote get-url origin)"
fi

# ── safety: refuse to push if remote owner doesn't match expected
# (this protects against typos / hijacked repos)
EXPECTED_OWNER="koraussie7"
ACTUAL_OWNER="$(git remote get-url origin | sed -E 's#.*github\.com[:/]([^/]+)/.*#\1#')"
if [[ "$ACTUAL_OWNER" != "$EXPECTED_OWNER" ]]; then
    echo "ERROR: origin owner is '$ACTUAL_OWNER', expected '$EXPECTED_OWNER'." >&2
    echo "       Refusing to push. Edit REMOTE_URL in this script if you really mean it." >&2
    exit 3
fi

# ── stage everything .gitignore filters ───────────────────
echo "==> git add .  (respecting .gitignore)"
git add .

# ── report ────────────────────────────────────────────────
STAGED_COUNT="$(git diff --cached --name-only | wc -l | tr -d ' ')"
TOTAL_BYTES=0
while IFS= read -r f; do
    if [[ -f "$f" ]]; then
        sz="$(stat -f%z "$f" 2>/dev/null || stat -c%s "$f" 2>/dev/null || echo 0)"
        TOTAL_BYTES=$((TOTAL_BYTES + sz))
    fi
done < <(git diff --cached --name-only)

if [[ "$TOTAL_BYTES" -gt 1048576 ]]; then
    SIZE_H="$((TOTAL_BYTES / 1048576)) MiB"
else
    SIZE_H="$((TOTAL_BYTES / 1024)) KiB"
fi

echo
echo "==> staged: $STAGED_COUNT files, $SIZE_H"
echo
echo "==> top-level layout that will be pushed:"
git diff --cached --name-only | awk -F/ '{print $1}' | sort | uniq -c | sort -rn | sed 's/^/    /'
echo
echo "==> files (first 40):"
git diff --cached --name-only | head -40 | sed 's/^/    /'
if [[ "$STAGED_COUNT" -gt 40 ]]; then
    echo "    ... ($(($STAGED_COUNT - 40)) more)"
fi
echo
echo "==> biggest 10 files:"
git diff --cached --name-only | while IFS= read -r f; do
    if [[ -f "$f" ]]; then
        sz="$(stat -f%z "$f" 2>/dev/null || stat -c%s "$f" 2>/dev/null || echo 0)"
        printf '%s\t%s\n' "$sz" "$f"
    fi
done | sort -rn | head -10 | awk '{printf "    %8d  %s\n", $1, $2}'
echo

# ── decide ────────────────────────────────────────────────
if [[ "$DO_PUSH" != "yes" ]]; then
    echo "==> DRY-RUN. Nothing was committed or pushed."
    echo "    Re-run with --yes to actually push to $REMOTE_URL ($BRANCH)."
    exit 0
fi

# ── commit ────────────────────────────────────────────────
if git diff --cached --quiet; then
    echo "==> nothing staged; nothing to commit."
    exit 0
fi

echo "==> git commit"
COMMIT_MSG="push: full Maya workspace snapshot (today)

- Co-Authored-By: cline <noreply@cline.ai>"
git commit -q -m "$COMMIT_MSG"

# ── push ──────────────────────────────────────────────────
# Push the current branch (whatever its name is — local is "master" here).
# -u sets upstream on first push. Force-push is intentionally NOT used;
# if the remote already has a different history, the push will fail safely.
echo "==> git push -u origin $BRANCH"
if git push -u origin "$BRANCH" 2>&1; then
    echo
    echo "==> ✅ pushed successfully."
    git log --oneline -1
else
    echo
    echo "==> ❌ push failed. Common causes:"
    echo "      • 403: not authenticated. Need PAT or 'gh auth login' for push."
    echo "      • remote rejected non-fast-forward: rebase or merge first."
    echo "      • remote is empty AND has protected branch rules."
    exit 4
fi
