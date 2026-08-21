# =============================================================================
# llm-to-modeler 一体镜像（单容器：nginx 伺服静态/反代 + uvicorn 跑 API）
# 单容器单端口（容器内 80 → 宿主映射），宿主网关按 /ai-modeler 前缀反代。
#
# 零联网构建（对齐 njmind 镜像习惯）：
#   前端 dist / 后端 .deps / nginx+supervisor 的 .deb 都在宿主侧经
#   docker run 预备（deploy.sh ②a/②b/②c）——docker run 的容器网络正常，
#   不在 docker build 里做联网 RUN（规避构建容器 DNS 依赖 daemon resolv.conf
#   快照的坑）。
#
# 进程模型：启动脚本后台拉起 uvicorn（127.0.0.1:8000），前台 exec nginx。
# nginx 伺服静态（/ai-modeler/）+ 反代 API（/ai-modeler/api → 127.0.0.1:8000，
# SSE 关缓冲）；任一进程退出容器退出，由 docker restart 策略拉起。
# =============================================================================
FROM python:3.12-slim

# nginx 及依赖 deb（宿主侧 docker run apt-get --download-only 预下载）
COPY debs/ /tmp/debs/
RUN dpkg -i /tmp/debs/*.deb && rm -rf /tmp/debs \
    && rm -f /etc/nginx/sites-enabled/default

WORKDIR /app
# 预装好的依赖（pip install -t 产物）
COPY .deps/ /app/deps/
# 后端源码
COPY src/ /app/src/
# 前端构建产物（平铺 dist；nginx root 直指）
COPY dist/ /usr/share/nginx/html/ai-modeler/

# nginx 配置：静态 + 反代本机 8000（SSE 防缓冲三件套）
COPY nginx-single.conf /etc/nginx/conf.d/default.conf
# 启动脚本：后台 uvicorn + 前台 nginx
COPY start-single.sh /app/start.sh
RUN chmod +x /app/start.sh

RUN mkdir -p /app/data /app/logs
VOLUME ["/app/data", "/app/logs"]

# 源码内混用两种导入风格（main.py 用 from src.xxx，api/engine 用裸 from
# engine.xxx）——/app 与 /app/src 都要可达
ENV PYTHONPATH=/app/deps:/app:/app/src \
    PYTHONUNBUFFERED=1

EXPOSE 80

ENTRYPOINT ["/app/start.sh"]
