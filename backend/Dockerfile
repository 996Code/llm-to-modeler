# llm-to-modler 后端镜像
# FastAPI + Uvicorn;数据(SQLite/上传文件)全部落在挂载卷,容器无状态

FROM python:3.12-slim

WORKDIR /app

# 国内镜像源加速(pip)
RUN pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/ 2>/dev/null; true

# 先拷依赖文件,利用 Docker 层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    --trusted-host mirrors.aliyun.com \
    -r requirements.txt

# 应用代码(.dockerignore 挡住 venv/__pycache__/data)
COPY . .

# 数据目录(SQLite/上传文件/日志,挂载点)
RUN mkdir -p /app/data

ENV PYTHONPATH=/app/src \
    TZ=Asia/Shanghai \
    DATABASE_PATH=/app/data/conversations.db \
    KG_FILES_DIR=/app/data/kg/files

EXPOSE 18080

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "18080"]
