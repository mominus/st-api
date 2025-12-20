# StackAI2API

将 StackAI 工作流 API 转换为标准的 OpenAI / Anthropic / Gemini API 格式，支持多账号池化、负载均衡、Token 监控和可视化后台管理。

## ✨ 功能特性

### 🔌 多 API 格式支持
- **OpenAI 兼容**: `/v1/chat/completions`、`/v1/models`、`/v1/responses`
- **Anthropic 兼容**: `/v1/messages`（支持思维链 Thinking）
- **Gemini 兼容**: `/v1beta/models/{model}:generateContent`、`:streamGenerateContent`

### 🚀 核心功能
- **流式响应**: 支持 SSE (Server-Sent Events) 实时流式输出
- **多账号池化**: 多个 StackAI 账号轮询负载均衡，自动故障转移
- **Token 监控**: 实时监控每个账号的 Token 使用情况，支持从 StackAI Analytics 同步真实使用量
- **API Key 管理**: 生成和管理客户端 API Key，支持模型隔离、权限控制、配额限制
- **费用追踪**: 自动计算 API 调用费用，支持按 Key 统计
- **调用日志**: 完整的 API 调用记录，支持查询和导出

### 🛡️ 安全特性
- **敏感数据加密**: API Key、Bearer Token 使用 Fernet 加密存储
- **JWT 认证**: 管理后台使用 JWT Token 认证
- **隐藏后台路径**: 管理后台使用自定义隐藏路径，防止被扫描发现
- **登录保护**: 支持登录失败锁定机制
- **CORS 配置**: 灵活的跨域访问控制

### 📊 可视化后台
- **仪表板**: 实时显示活跃账号、Token 使用量、请求统计
- **性能监控**: 实时并发数、QPS、响应时间、限流配置
- **账号管理**: 添加/编辑/删除 StackAI 账号，支持批量导入
- **模型组管理**: 创建模型组，配置输入字段映射
- **API Key 管理**: 生成 Key、设置权限、配额限制、过期时间
- **调用日志**: 查看详细的 API 调用记录

## 📋 系统要求

- Python 3.11+
- SQLite（内置）或 PostgreSQL（可选）

## 🚀 快速开始

### 方式一：本地部署

#### 1. 克隆项目

```bash
git clone <repository-url>
cd st2
```

#### 2. 创建虚拟环境

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# Linux/macOS
source venv/bin/activate
```

#### 3. 安装依赖

```bash
pip install -r requirements.txt
```

#### 4. 配置环境变量

```bash
# 复制环境变量模板
cp .env.example .env

# 编辑 .env 文件
```

**必须配置的项目**:

| 配置项 | 说明 | 生成方式 |
|--------|------|----------|
| `JWT_SECRET_KEY` | JWT 密钥 | `openssl rand -hex 32` |
| `ENCRYPTION_KEY` | Fernet 加密密钥 | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `ADMIN_PATH` | 隐藏的后台路径 | `openssl rand -hex 16` |
| `ADMIN_PASSWORD` | 管理员密码 | 自定义强密码 |

#### 5. 启动服务

```bash
python run.py
```

服务启动后：
- API 地址: `http://localhost:8000`
- 管理后台: `http://localhost:8000/<你的ADMIN_PATH>`
- 健康检查: `http://localhost:8000/health`

---

### 方式二：Docker 部署

#### 1. 构建镜像

```bash
docker build -t stackai2api .
```

#### 2. 运行容器

```bash
docker run -d \
  --name stackai2api \
  -p 8000:8000 \
  -v $(pwd)/data:/app/data \
  -e JWT_SECRET_KEY=your-jwt-secret-key \
  -e ENCRYPTION_KEY=your-fernet-key \
  -e ADMIN_PATH=your-secret-admin-path \
  -e ADMIN_USERNAME=admin \
  -e ADMIN_PASSWORD=your-strong-password \
  stackai2api
```

#### 3. 使用 Docker Compose

创建 `docker-compose.yml`:

```yaml
version: '3.8'

services:
  stackai2api:
    build: .
    ports:
      - "8000:8000"
    volumes:
      - ./data:/app/data
    environment:
      - HOST=0.0.0.0
      - PORT=8000
      - DATABASE_URL=sqlite+aiosqlite:///./data/api_service.db
      - JWT_SECRET_KEY=${JWT_SECRET_KEY}
      - ENCRYPTION_KEY=${ENCRYPTION_KEY}
      - ADMIN_PATH=${ADMIN_PATH}
      - ADMIN_USERNAME=admin
      - ADMIN_PASSWORD=${ADMIN_PASSWORD}
      - CORS_ORIGINS=*
    restart: unless-stopped
```

运行：

```bash
docker-compose up -d
```

---

### 方式三：Hugging Face Spaces 部署

#### 1. 创建 Space

1. 访问 [huggingface.co/new-space](https://huggingface.co/new-space)
2. 填写信息：
   - **Space name**: `stackai2api`
   - **SDK**: 选择 **Docker**
   - **Hardware**: **CPU basic (Free)**
   - **Visibility**: **Private**（推荐）
3. 点击 Create Space

#### 2. 开启 Persistent Storage（重要！）

⚠️ **必须开启，否则重启后数据丢失！**

1. 进入 Space 页面 → **Settings**
2. 找到 **Persistent Storage** 部分
3. 选择 **Small (20GB Free)** 并点击 **Create**
4. 等待存储创建完成

#### 3. 配置 Secrets

在 Space 的 **Settings** → **Repository secrets** 中添加：

| Secret Name | 说明 |
|-------------|------|
| `JWT_SECRET_KEY` | JWT 密钥 |
| `ENCRYPTION_KEY` | Fernet 加密密钥 |
| `ADMIN_PATH` | 隐藏的后台路径 |
| `ADMIN_USERNAME` | 管理员用户名 |
| `ADMIN_PASSWORD` | 管理员密码 |

#### 4. 修改 Dockerfile

确保 `Dockerfile` 使用 HF Spaces 的端口和数据目录：

```dockerfile
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY run.py .

# 使用 Persistent Storage 目录
ENV DATABASE_URL=sqlite+aiosqlite:////data/api_service.db
ENV LOG_FILE=/data/api_service.log

# HF Spaces 使用 7860 端口
ENV PORT=7860
ENV HOST=0.0.0.0

EXPOSE 7860

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860"]
```

#### 5. 推送代码

```bash
git remote add hf https://huggingface.co/spaces/你的用户名/stackai2api
git push hf main
```

#### 6. 访问服务

- API 地址: `https://你的用户名-stackai2api.hf.space`
- 管理后台: `https://你的用户名-stackai2api.hf.space/<你的ADMIN_PATH>`

---


## 📖 使用指南

### 从 StackAI 获取参数

在 StackAI 工作流的 **API** 页面，可以看到类似以下的 Python 示例代码：

```python
import requests

API_URL = "https://api.stack-ai.com/inference/v0/run/3b0c67e9-89d4-42ba-bc69-544e3cf8bd41/691af0876d6b6da025de1ab2"

headers = {
    'Authorization': 'Bearer XXXXXXXXXXXXX',
    'Content-Type': 'application/json'
}
```

从中提取以下参数：

| 参数 | 说明 | 示例 |
|------|------|------|
| **Organization ID** | API URL 中的第一个 ID | `3b0c67e9-89d4-42ba-bc69-544e3cf8bd41` |
| **Flow ID** | API URL 中的第二个 ID | `691af0876d6b6da025de1ab2` |
| **Bearer Token** | Authorization 头中 Bearer 后面的值 | `XXXXXXXXXXXXX` |

### 管理后台功能

#### 1. 仪表板
- 显示活跃账号数、今日 Token 使用量、API Key 数量、今日请求数
- 按账号合并显示使用情况
- 支持手动同步和自动同步（每 30 秒）

#### 2. 账号管理
添加 StackAI 账号时需要填写：
- **账号名称**: 便于识别的名称
- **Organization ID**: 从 API URL 获取
- **Flow ID**: 从 API URL 获取
- **Bearer Token**: API 页面的 Authorization Bearer Token
- **模型组**: 将此账号关联到哪个模型组
- **每日配额**: StackAI 免费账号每日限额 1,000,000 Token
- **Private API Key**（可选）: 用于同步真实使用量统计

#### 3. 模型组管理
- 创建模型组（如 `gpt-4o`、`claude-3.5-sonnet`、`gemini-2.5-pro`）
- 配置输入字段映射（默认 `{"user_input": "in-0"}`）
- 一个模型组可以关联多个账号，实现负载均衡

#### 4. API Key 管理
- 生成 API Key 并授权访问指定模型组
- 支持设置：过期时间、请求配额、Token 配额、费用限制
- 支持启用/禁用/删除 API Key
- 完整 Key 仅在创建时显示一次，请妥善保存

#### 5. 调用日志
- 查看所有 API 调用记录
- 支持按 API Key、模型组、时间筛选
- 显示输入/输出预览、Token 使用、响应时间、费用

#### 6. 性能监控
- **实时指标**: 当前并发数、实时 QPS、平均响应时间、成功率
- **限流配置**: 每分钟请求数 (RPM)、最大并发数、数据库并发数、HTTP 连接池
- **性能统计**: 总请求数、拒绝请求数、P95/P99 响应时间
- **系统配置**: 数据库类型、连接池大小、HTTP 超时、服务运行时间

---

## 🔗 API 端点

### OpenAI 兼容接口

```bash
# Chat Completions
POST /v1/chat/completions

# 模型列表
GET /v1/models

# Responses API (Codex 兼容)
POST /v1/responses
```

**请求示例**:

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-your-api-key" \
  -d '{
    "model": "gpt-4o",
    "messages": [
      {"role": "user", "content": "Hello!"}
    ],
    "stream": false
  }'
```

### Anthropic 兼容接口

```bash
POST /v1/messages
```

**请求示例**:

```bash
curl -X POST http://localhost:8000/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: sk-your-api-key" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "claude-3.5-sonnet",
    "max_tokens": 1024,
    "messages": [
      {"role": "user", "content": "Hello!"}
    ]
  }'
```

**思维链 (Thinking) 支持**:

```bash
curl -X POST http://localhost:8000/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: sk-your-api-key" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "claude-3.5-sonnet",
    "max_tokens": 8192,
    "thinking": {
      "type": "enabled",
      "budget_tokens": 10000
    },
    "messages": [
      {"role": "user", "content": "解释量子计算的原理"}
    ]
  }'
```

### Gemini 兼容接口

```bash
# 同步生成
POST /v1beta/models/{model}:generateContent

# 流式生成
POST /v1beta/models/{model}:streamGenerateContent

# 模型列表
GET /v1beta/models
```

**请求示例**:

```bash
curl -X POST "http://localhost:8000/v1beta/models/gemini-2.5-pro:generateContent" \
  -H "Content-Type: application/json" \
  -H "x-goog-api-key: sk-your-api-key" \
  -d '{
    "contents": [
      {"parts": [{"text": "Hello!"}]}
    ]
  }'
```

---

## 🛠️ 在 AI 工具中配置

### Cherry Studio / ChatBox

```
API 地址: http://localhost:8000/v1
API Key: sk-your-generated-key
模型: gpt-4o (或其他配置的模型组名称)
```

### Claude Code

```bash
export ANTHROPIC_BASE_URL=http://localhost:8000
export ANTHROPIC_API_KEY=sk-your-generated-key
```

### Cursor / Continue

在设置中配置：
- API Base URL: `http://localhost:8000/v1`
- API Key: `sk-your-generated-key`

### OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="sk-your-generated-key"
)

response = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "Hello!"}]
)
print(response.choices[0].message.content)
```

### Anthropic Python SDK

```python
import anthropic

client = anthropic.Anthropic(
    base_url="http://localhost:8000",
    api_key="sk-your-generated-key"
)

message = client.messages.create(
    model="claude-3.5-sonnet",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello!"}]
)
print(message.content[0].text)
```

---

## 📁 项目结构

```
st2/
├── app/
│   ├── main.py                 # 应用入口
│   ├── models/
│   │   └── database.py         # 数据库模型
│   ├── routers/
│   │   ├── openai.py           # OpenAI 兼容路由
│   │   ├── anthropic.py        # Anthropic 兼容路由
│   │   ├── gemini.py           # Gemini 兼容路由
│   │   └── admin.py            # 管理 API 路由
│   ├── services/
│   │   ├── account_pool.py     # 账号池管理
│   │   ├── api_key.py          # API Key 管理
│   │   ├── analytics.py        # StackAI Analytics 服务
│   │   ├── auth.py             # 认证服务
│   │   ├── backend_client.py   # 后端 API 客户端
│   │   ├── call_logger.py      # 调用日志服务
│   │   ├── crypto.py           # 加密服务
│   │   ├── error_handler.py    # 错误处理
│   │   ├── pricing.py          # 费用计算
│   │   ├── response_transformer.py  # 响应转换
│   │   ├── token_counter.py    # Token 计数
│   │   └── ...
│   └── static/
│       ├── admin.html          # 管理后台页面
│       ├── admin.js            # 前端脚本
│       └── style.css           # 样式
├── data/                       # 数据目录（SQLite 数据库）
├── .env.example                # 环境变量模板
├── Dockerfile                  # Docker 构建文件
├── requirements.txt            # Python 依赖
├── run.py                      # 启动脚本
└── README.md
```

---

## ⚙️ 环境变量说明

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `HOST` | 服务器主机地址 | `0.0.0.0` |
| `PORT` | 服务器端口 | `8000` |
| `DEBUG` | 调试模式 | `false` |
| `DATABASE_URL` | 数据库连接字符串 | `sqlite+aiosqlite:///./data/api_service.db` |
| `JWT_SECRET_KEY` | JWT 密钥 | **必须设置** |
| `JWT_EXPIRE_HOURS` | JWT 过期时间（小时） | `24` |
| `ENCRYPTION_KEY` | Fernet 加密密钥 | **必须设置** |
| `ADMIN_USERNAME` | 管理员用户名 | `admin` |
| `ADMIN_PASSWORD` | 管理员密码 | `admin123` |
| `ADMIN_PATH` | 隐藏的后台路径 | 随机生成 |
| `MAX_LOGIN_ATTEMPTS` | 最大登录失败次数 | `5` |
| `LOGIN_LOCKOUT_MINUTES` | 登录锁定时间（分钟） | `15` |
| `LOG_LEVEL` | 日志级别 | `INFO` |
| `LOG_FILE` | 日志文件路径 | `./data/api_service.log` |
| `CORS_ORIGINS` | 允许的跨域来源 | `*` |

---

## ❓ 常见问题

### Q: 如何重置管理员密码？

```bash
python reset_admin.py
```

或删除 `data/api_service.db` 数据库文件后重启服务。

### Q: Token 配额是如何计算的？

- 如果配置了 Private API Key，系统会从 StackAI Analytics 同步真实使用量
- 否则，系统会使用 tiktoken 根据 API 调用时的内容进行估算
- StackAI 免费账号每日限额 1,000,000 Token，每日 UTC 0:00 重置

### Q: 账号显示「耗尽」状态怎么办？

1. 等待次日自动重置
2. 在管理后台手动重置使用量
3. 添加更多账号到同一模型组实现负载均衡

### Q: 如何实现多账号负载均衡？

将多个 StackAI 账号关联到同一个模型组，系统会自动轮询选择可用账号。

### Q: 流式响应不工作？

确保:
1. 请求中设置 `stream: true`
2. 客户端支持 SSE
3. 没有代理或中间件缓冲响应

### Q: 出现 "database is locked" 错误？

这是 SQLite 并发写入限制导致的。系统已启用 WAL 模式优化，如果问题持续：
1. 减少并发请求
2. 考虑迁移到 PostgreSQL（修改 `DATABASE_URL` 即可）

### Q: 如何使用 PostgreSQL？

修改 `DATABASE_URL` 环境变量：

```bash
DATABASE_URL=postgresql+asyncpg://user:password@host:5432/dbname
```

---

## 🚀 高并发测试

### 快速测试

运行内置的并发功能测试：

```bash
python tests/quick_test.py
```

输出示例：
```
并发限制器测试
  最大并发请求数: 100
  测试: 并发 200 个请求...
  结果: 200 成功, QPS: 1500+

账号池轮询测试
  模拟 5 个账号，获取 100 次
  分布均匀: 是

突发流量测试
  5 波突发流量，每波 100 个请求
  总计: 500/500 成功 (100%)
```

### 负载测试

使用负载测试脚本测试真实 API：

```bash
# 基本负载测试
python tests/load_test.py --url http://localhost:8000 --api-key sk-xxx --concurrency 50 --requests 200

# 流式请求测试
python tests/load_test.py --url http://localhost:8000 --api-key sk-xxx --concurrency 20 --requests 100 --stream

# 多 API 格式测试
python tests/load_test.py --url http://localhost:8000 --api-key sk-xxx --test multi-api

# 压力测试 (持续 60 秒，目标 QPS 10)
python tests/load_test.py --url http://localhost:8000 --api-key sk-xxx --test stress --duration 60 --qps 10
```

### 模拟后端测试

启动模拟后端服务器进行测试（无需真实 StackAI）：

```bash
# 终端 1: 启动模拟后端
python tests/mock_server.py --port 9000

# 终端 2: 运行负载测试
python tests/load_test.py --url http://localhost:8000 --api-key sk-xxx --concurrency 100 --requests 500
```

### 高并发配置

在 `.env` 中调整并发参数：

```bash
# 数据库连接池
DB_POOL_SIZE=20
DB_MAX_OVERFLOW=30

# HTTP 客户端连接池
HTTP_MAX_CONNECTIONS=100
HTTP_MAX_KEEPALIVE=20

# 并发限制
MAX_CONCURRENT_REQUESTS=100
MAX_CONCURRENT_DB_OPS=50
```

---

## 📄 许可证

MIT License
