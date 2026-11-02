#!/bin/sh
# 一体容器入口：后台 uvicorn + nginx，shell 监视两个子进程。
# 任一进程退出则容器退出，由 docker restart 策略整体拉起。
set -e

# 后台拉起后端（单进程;并发由线程池承担,存储为 PG(DATABASE_URL 必填)）
# 以 main:app 启动（PYTHONPATH 含 /app/src）：若以 src.main:app 启动，
# src.X 与 X 双模块加载会让 thread-local（services 表/透传头）互不可见
python -m uvicorn main:app --host 127.0.0.1 --port 8000 &
UVC_PID=$!

# 等 uvicorn 就绪再起 nginx（避免 nginx 启动时反代目标不存在报错退出）
i=0
until curl -sf http://127.0.0.1:8000/api/health >/dev/null 2>&1; do
  i=$((i+1))
  [ "$i" -ge 30 ] && { echo "uvicorn 30s 未就绪，容器退出" >&2; exit 1; }
  sleep 1
done

# nginx 不 daemonize，shell 同时监视它与 uvicorn。
nginx -g 'daemon off;' &
NGX_PID=$!

# 任一进程退出即整体退出（交给 docker restart）。
# 注意 /bin/sh 是 dash：不支持 bash 的 wait -n，用 kill -0 轮询监视
while kill -0 "$UVC_PID" 2>/dev/null && kill -0 "$NGX_PID" 2>/dev/null; do
  sleep 5
done
exit 1
