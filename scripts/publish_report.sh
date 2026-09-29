#!/usr/bin/env bash
# 把最新报告发布为在线报告(gh-pages 分支)。
# 用法: scripts/publish_report.sh [报告路径]   默认 artifacts/report.html
set -euo pipefail

cd "$(dirname "$0")/.."
SRC=${1:-artifacts/report.html}
BRANCH=gh-pages
WORKTREE=.gh-pages-tmp
PAGE_URL=https://colbert33789.github.io/travel/

if [ ! -f "$SRC" ]; then
  echo "找不到报告: $SRC —— 请先运行 scripts/run_pipeline.py" >&2
  exit 1
fi

# 用 worktree 操作 gh-pages, 不影响当前工作分支
if ! git show-ref --verify --quiet "refs/heads/$BRANCH"; then
  git worktree add -q -b "$BRANCH" "$WORKTREE"
else
  git worktree add -q "$WORKTREE" "$BRANCH"
fi

cp "$SRC" "$WORKTREE/index.html"
(
  cd "$WORKTREE"
  git add index.html
  if git diff --cached --quiet; then
    echo "报告无变化, 无需更新"
  else
    git -c user.name="colbert33789" \
        -c user.email="colbert33789@users.noreply.github.com" \
        commit -q -m "更新在线报告 $(date '+%F %H:%M')"
    git push origin "$BRANCH"
    echo "已发布 → $PAGE_URL"
  fi
)

git worktree remove --force "$WORKTREE"
