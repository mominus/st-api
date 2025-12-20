# Hugging Face Spaces Dockerfile
# 使用 SQLite + Persistent Storage 方案

FROM python:3.11-slim

WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY requirements.txt .

# 安装 Python 依赖
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY app/ ./app/
COPY run.py .

# 创建数据目录（会被 Persistent Storage 覆盖）
RUN mkdir -p /data

# 设置权限
RUN chmod -R 755 /app

# HF Spaces 使用 7860 端口
ENV PORT=7860
ENV HOST=0.0.0.0

# 暴露端口
EXPOSE 7860

# 启动命令
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860"]
