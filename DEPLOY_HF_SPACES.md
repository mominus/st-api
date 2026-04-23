# Hugging Face Spaces 部署说明（请先看限制）

截至 2026-04-23，根据 Hugging Face 官方文档，当前有两个必须先确认的限制：

1. 免费 `CPU Basic` 默认只有 `50GB` 非持久化磁盘；Space 重启、休眠恢复、重建后，本地 SQLite 数据可能丢失。
2. `Private Space` 的运行中应用只对 owner / collaborators 可访问；外部用户访问其 URL 会得到 `404`，不适合作为公共 API 地址。

所以这份文档的结论很直接：

- 仅免费用户：可以临时试跑，但不适合承载“需要长期保存 SQLite 数据”的正式服务。
- 需要外部程序直接调用 API：Space 不能设为 `Private`，至少要 `Public`。
- 如果你想“代码不公开，但应用可访问”，需要 `Protected` visibility；这是 Hugging Face `PRO` 或 `Team & Enterprise` 能力。
- 如果你既要稳定持久化，又要外部可调用，通常更推荐 VPS。

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
   - **Visibility**:
     - 如果需要外部客户端直接访问：选 **Public**
     - 如果你有付费计划且要“源码私有、应用可访问”：选 **Protected**
     - 不要选 **Private**，除非这个 Space 只给你自己或协作者在网页里使用
3. 点击 Create Space

## 三、先确认免费层和持久化限制（重要）

### 免费层默认情况

- `CPU Basic` 免费层默认提供 `2 vCPU / 16GB RAM / 50GB` 磁盘。
- 这 `50GB` 磁盘是 **非持久化** 的，不是可长期保存的 SQLite 数据盘。
- 也就是说，如果你没有额外购买/附加持久化存储，不应把 HF 免费 Space 当作稳定的 SQLite 持久化部署环境。

### 什么时候可以把 SQLite 放到 `/data`

只有在你已经为 Space 配置了 **付费持久化存储 / attached volume** 时，才建议把 SQLite 文件和日志放到 `/data`。

可执行原则：

- 有持久化存储：`DATABASE_URL` 用 `/data/api_service.db`
- 没有持久化存储：可以临时运行，但 SQLite 数据不保证保留

### 免费用户建议

如果你当前就是免费用户，建议把 HF Spaces 仅作为：

- 临时演示
- 功能验证
- 前端/接口联调环境

不建议用于：

- 正式生产
- 需要保留账号池、API Key、日志、统计数据的长期服务

## 四、配置环境变量（Secrets）

在 Space Settings → **Repository secrets** 中添加：

| 变量名 | 值 | 说明 |
|--------|-----|------|
| `DATABASE_URL` | `sqlite+aiosqlite:////data/api_service.db` | 仅在你已挂载持久化存储到 `/data` 时推荐这样设置 |
| `JWT_SECRET_KEY` | 随机字符串 | 生成：`openssl rand -hex 32` |
| `ENCRYPTION_KEY` | Fernet 密钥 | 生成：见下方命令 |
| `ADMIN_USERNAME` | `admin` | 管理员用户名 |
| `ADMIN_PASSWORD` | 你的密码 | 管理员密码（请修改！） |
| `ADMIN_PATH` | 随机字符串 | 后台路径，如 `openssl rand -hex 16` |
| `LOG_FILE` | `/data/api_service.log` | 仅在你已挂载持久化存储到 `/data` 时推荐这样设置 |

说明：

- 如果你没有持久化存储，`/data` 里的数据库和日志仍然可能在 Space 重启/重建后丢失。
- 如果 Space 设为 `Public`，请务必设置强密码、随机 `ADMIN_PATH`，并只暴露必要接口。

### 生成 Fernet 密钥
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 生成随机字符串
```bash
openssl rand -hex 32
```

## 五、准备部署文件

### 1. Dockerfile

如果你直接使用当前仓库，根目录已经包含可用的 `Dockerfile` 和 `.dockerignore`，通常不需要再手动创建。

当前 `Dockerfile` 内容如下：

```dockerfile
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY run.py .

RUN mkdir -p /data && chmod 777 /data

RUN chmod -R 755 /app

VOLUME ["/data"]

ENV PORT=7860
ENV HOST=0.0.0.0
ENV DATABASE_URL=sqlite+aiosqlite:////data/api_service.db
ENV LOG_FILE=/data/api_service.log

EXPOSE 7860

CMD ["python", "run.py"]
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

注意：

- 以上 URL 只有在 Space 为 `Public` 或 `Protected` 时，才适合作为外部访问地址。
- 如果 Space 为 `Private`，外部用户访问通常会得到 `404`。

### 测试健康检查
```bash
curl https://你的用户名-st-api.hf.space/health
# 应返回: {"status": "ok"}
```

## 八、SQLite 配置说明

### HF Spaces 上的 SQLite 适用场景

- 项目当前仅保留 SQLite 存储，部署时无需额外数据库服务。
- 如果你有持久化存储，SQLite + `/data` 路径是可行方案。
- 如果你只有免费 `CPU Basic`，要重点关注“数据是否会丢失”，而不只是连接数问题。
- 对外提供稳定 API 时，`Visibility` 也必须一起考虑，不能只看数据库。
- 默认 SQLite 运行参数为 `journal_mode=WAL` 和 `busy_timeout=30s`。

### 数据库路径配置

如果你已经给 Space 配置了持久化卷，建议把挂载路径设为 `/data`。

**环境变量配置**：
```
DATABASE_URL=sqlite+aiosqlite:////data/api_service.db
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

1. **Visibility**:
   - 需要外部调用：`Public`
   - 需要源码私有但应用可访问：`Protected`（付费计划）
   - `Private` 不适合作为公共 API 地址
2. **持久化**:
   - 免费层默认 `50GB` 磁盘不是持久化存储
   - 没有付费持久化卷时，SQLite 数据可能在重启/重建后丢失
3. **休眠**: 免费 Space 长时间无访问会休眠，首次访问需等待冷启动
4. **资源限制**: 免费版默认 `2 vCPU + 16GB RAM`
5. **域名格式**: `用户名-space名.hf.space`
6. **端口**: HF Spaces 使用 `7860`，不是 `8000`
7. **部署建议**: 如果你要长期保存 SQLite 数据并对外提供稳定 API，更推荐 VPS

## 十二、故障排查

### 502 Bad Gateway
- 检查 Dockerfile 端口是否为 7860
- 查看 Logs 确认应用是否启动成功

### 数据丢失
- 确认你是否真的配置了付费持久化存储 / attached volume
- 确认挂载路径和 `DATABASE_URL` 都指向 `/data/`
- 如果你是免费层且没有持久化卷，数据丢失属于预期现象

### 登录失败
- 检查 JWT_SECRET_KEY 和 ENCRYPTION_KEY 是否正确配置
- 查看日志确认具体错误

### 无法访问管理后台
- 确认 ADMIN_PATH 环境变量已设置
- 访问路径格式：`https://域名/你的ADMIN_PATH`

### Space URL 外部无法调用
- 检查 Space 是否被设成了 `Private`
- `Private Space` 只对 owner / collaborators 可访问，外部访问会返回 `404`
- 如果你需要让第三方程序调用，请改为 `Public`；如果要源码私有但应用可访问，请使用 `Protected`
