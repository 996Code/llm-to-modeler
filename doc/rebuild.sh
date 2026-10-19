#!/bin/bash
# rebuild.sh — 本项目专用：validate + deliver 全部 diagram-src JSON，按前缀路由到分类子目录
# 目录约定：engine/ sdk/ platform/ chatbi/ pack/ frontend/，system-architecture 留在 doc/ 顶层
# 用法: bash doc/rebuild.sh   （在仓库根执行）
set -u
SRC="doc/diagram-src"
CLI="$HOME/.qoder-cn/tools/archify/bin/archify.mjs"

if [ ! -f "$CLI" ]; then
  echo "ERROR: archify CLI 不存在: $CLI"
  exit 1
fi

route() {  # $1 = 图名称（不含 .html），输出目标目录
  case "$1" in
    engine-*)   echo "doc/engine" ;;
    sdk-*)      echo "doc/sdk" ;;
    platform-*) echo "doc/platform" ;;
    chatbi-*)   echo "doc/chatbi" ;;
    pack-*|form-*|kg-*) echo "doc/pack" ;;
    frontend-*) echo "doc/frontend" ;;
    ragflow-*)  echo "doc/RAGFlow项目学习/图" ;;
    *)          echo "doc" ;;
  esac
}

pass=0; fail=0; failed=""
for json in "$SRC"/*.json; do
  name=$(basename "$json" .json)                 # e.g. foo.workflow
  type="${name##*.}"
  dir=$(route "${name%.*}")
  html="$dir/${name%.*}.html"
  mkdir -p "$dir"
  if node "$CLI" validate "$type" "$json" --quality showcase --json >/dev/null 2>&1; then
    if node "$CLI" deliver "$type" "$json" "$html" --quality showcase --json >/dev/null 2>&1; then
      echo "OK      $name -> $html"
      pass=$((pass+1))
    else
      echo "DELIVER-FAIL  $name"
      fail=$((fail+1)); failed="$failed $name"
    fi
  else
    echo "VALIDATE-FAIL  $name  (node $CLI validate $type $json --quality showcase --json 查看诊断)"
    fail=$((fail+1)); failed="$failed $name"
  fi
done

# 渲染 RAGFlow 笔记 MD -> HTML（MD 是唯一真源）
python3 doc/RAGFlow项目学习/render-notes.py

echo "----"
echo "passed: $pass  failed: $fail"
[ $fail -gt 0 ] && { echo "failed:$failed"; exit 1; }
exit 0
