# Hugging Face Spaces 部署教程（SQLite + Persistent Storage）

本教程使用 **SQLite + Persistent Storage** 方案，无需外部数据库，无连接数限制。

## 一、准备工作

### 1. 注册 Hugging Face 账号
访问 [huggingface.co](https://huggingface.co) 注册账号

### 2. 创建 Access Token
1. 点击头像 → Settings → Access Tokens
2. 创建新 Token，选择 `write` 权限
3. 保存 Token

## 二、创建 Space

1. 访问 [huggingface.co/new-space](https://huggingface.co/new-space)
2. 填写信息：
   - **Space name**: `st-api`（或你喜欢的名字）
   - **License**: 选择一个
   - **SDK**: 选择 **Docker**
   - **Hardware**: **CPU basic (Free)**
   - **Visibility**: **Private**（推荐，保护你的 API）
3. 点击 Create Space

## 三、开启 Persistent Storage（重要！）

⚠️ **必须开启，否则重启后数据丢失！**

1. 进入你的 Space 页面
2. 点击 **Settings** 标签
3. 找到 **Persistent Storage** 部分
4. 选择 **Small (20GB Free)** 并点击 **Subscribe**
5. 等待存储挂载完成

开启后，`/data` 目录的数据会持久化保存。

## 四、配置环境变量（Secrets）

在 Space Settings → **Repository secrets** 中添加：

| 变量名 | 值 | 说明 |
|--------|-----|------|
| `DATABASE_URL` | `sqlite+aiosqlite:///data/api_service.db` | SQLite 数据库路径（注意：使用 /data 绝对路径） |
| `JWT_SECRET_KEY` | 随机字符串 | 生成：`openssl rand -hex 32` |
| `ENCRYPTION_KEY` | Fernet 密钥 | 生成：见下方命令 |
| `ADMIN_USERNAME` | `admin` | 管理员用户名 |
| `ADMIN_PASSWORD` | 你的密码 | 管理员密码（请修改！） |
| `ADMIN_PATH` | 随机字符串 | 后台路径，如 `openssl rand -hex 16` |
| `LOG_FILE` | `/data/api_service.log` | 日志文件路径 |

### 生成 Fernet 密钥
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 生成随机字符串
```bash
openssl rand -hex 32
```

## 五、准备部署文件

### 1. 创建 Dockerfile

在项目根目录创建 `Dockerfile`：

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码
COPY . .

# 创建数据目录（会被 Persistent Storage 覆盖）
RUN mkdir -p /data

# 设置权限
RUN chmod -R 755 /app

# 暴露端口（HF Spaces 使用 7860）
EXPOSE 7860

# 启动命令
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860"]
```

### 2. 创建 .dockerignore

```
venv/
__pycache__/
*.pyc
.env
.git/
*.db
*.log
```

## 六、部署到 HF Spaces

### 方式一：Git 推送（推荐）

```bash
# 克隆你的 Space（替换用户名和 space 名）
git clone https://huggingface.co/spaces/你的用户名/st-api
cd st-api

# 复制项目文件到 Space 目录
# 或者添加 HF 为远程仓库
git remote add hf https://huggingface.co/spaces/你的用户名/st-api

# 推送
git push hf main
```

### 方式二：网页上传

直接在 Space 的 Files 标签页上传所有文件。

## 七、验证部署

部署完成后（约 2-5 分钟）：

1. **API 地址**: `https://你的用户名-st-api.hf.space`
2. **健康检查**: `https://你的用户名-st-api.hf.space/health`
3. **管理后台**: `https://你的用户名-st-api.hf.space/你的ADMIN_PATH`

### 测试健康检查
```bash
curl https://你的用户名-st-api.hf.space/health
# 应返回: {"status": "ok"}
```

## 八、SQLite 配置说明

### 为什么选择 SQLite + Persistent Storage？

| 对比项 | SQLite | PostgreSQL (Render/Supabase) |
|--------|--------|------------------------------|
| 连接数限制 | ❌ 无限制 | ⚠️ 有限制（5-10个） |
| 配置复杂度 | ✅ 简单 | ⚠️ 需要外部服务 |
| 成本 | ✅ 免费 | ⚠️ 可能收费 |
| 性能 | ✅ 本地读写快 | ⚠️ 网络延迟 |
| 并发写入 | ⚠️ 有锁竞争 | ✅ 更好 |

对于中小规模 API 服务，SQLite 完全够用。

### 数据库路径配置

在 HF Spaces 中，Persistent Storage 挂载在 `/data` 目录。

**环境变量配置**：
```
DATABASE_URL=sqlite+aiosqlite:///data/api_service.db
LOG_FILE=/data/api_service.log
```

注意：使用绝对路径 `/data/` 而不是相对路径 `./data/`。

## 九、常用操作

### 查看日志
Space 页面 → **Logs** 标签

### 重启 Space
Settings → **Factory reboot**

### 更新代码
```bash
git switch main #先切到 main，避免你在其他分支或 detached HEAD 上提交。
git add -A       #暂存所在仓库所有变更
git commit -m "feat/fix: ..."
git push origin main
```

### 发布新版本（自动写更新日志 + 打 Tag + 推送）

项目内置了版本发布脚本：
- `VERSION`：版本号
- `CHANGELOG.md`：更新日志
- `scripts/release.py`：发布工具

```bash
python scripts/release.py --version 1.0.1 \
  --note "本次更新说明 1" \
  --note "本次更新说明 2" \
  --commit --tag --push --remote hf --branch main
```

### 查看可下载版本

```bash
git ls-remote --tags https://huggingface.co/spaces/你的用户名/st-api
```

### 下载指定版本

```bash
git clone --branch v1.0.1 https://huggingface.co/spaces/你的用户名/st-api
```

### 查看数据库文件
可以在 Space 的 Files 标签查看 `/data` 目录下的文件。

## 十、防止休眠（可选）

免费 Space 48 小时无访问会休眠。使用 UptimeRobot 保活：

1. 访问 [uptimerobot.com](https://uptimerobot.com) 注册
2. 添加监控：
   - **Monitor Type**: HTTP(s)
   - **URL**: `https://你的用户名-st-api.hf.space/health`
   - **Monitoring Interval**: 30 minutes

## 十一、注意事项

1. **Private Space**: 建议设为私有，避免 API 被滥用
2. **休眠**: 免费 Space 48 小时无访问会休眠，首次访问需等待 10-30 秒启动
3. **资源限制**: 免费版 2 vCPU + 16GB RAM，足够使用
4. **域名格式**: `用户名-space名.hf.space`
5. **端口**: HF Spaces 使用 7860 端口，不是 8000

## 十二、故障排查

### 502 Bad Gateway
- 检查 Dockerfile 端口是否为 7860
- 查看 Logs 确认应用是否启动成功

### 数据丢失
- 确认已开启 Persistent Storage
- 确认 DATABASE_URL 使用 `/data/` 绝对路径

### 登录失败
- 检查 JWT_SECRET_KEY 和 ENCRYPTION_KEY 是否正确配置
- 查看日志确认具体错误

### 无法访问管理后台
- 确认 ADMIN_PATH 环境变量已设置
- 访问路径格式：`https://域名/你的ADMIN_PATH`
