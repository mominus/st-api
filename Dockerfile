FROM python:3.12-slim

WORKDIR /app

# 创建非 root 用户（HF Spaces 要求）
RUN useradd -m -u 1000 user

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码
COPY --chown=user:user . .

# 创建数据目录并设置权限
RUN mkdir -p /app/data && chown -R user:user /app/data

# 切换到非 root 用户
USER user

# 暴露端口（HF Spaces 使用 7860）
EXPOSE 7860

# 启动命令
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860"]
