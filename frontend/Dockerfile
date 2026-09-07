# llm-to-modler 前端镜像
# 阶段1: Node 构建(vite, base=/ai-modeler/ 多页入口 main/admin/embed)
# 阶段2: nginx 托管静态文件 + API/SSE 反代到 backend

FROM node:20-alpine AS build

WORKDIR /app

# 国内源加速
RUN npm config set registry https://registry.npmmirror.com

# 先拷依赖文件,利用 Docker 层缓存
COPY package.json package-lock.json* ./
RUN npm ci || npm install

# 构建产物:dist/(index.html + admin.html + embed.js + assets)
COPY . .
RUN npm run build

# ── 运行阶段 ──
FROM nginx:1.27-alpine

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

ENV TZ=Asia/Shanghai

EXPOSE 80
