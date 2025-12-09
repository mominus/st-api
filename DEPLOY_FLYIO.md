# Fly.io 部署教程

## 一、安装 Fly CLI

### Windows (PowerShell)
```powershell
pwsh -Command "iwr https://fly.io/install.ps1 -useb | iex"
```

### Linux/WSL
```bash
curl -L https://fly.io/install.sh | sh
```

### macOS
```bash
brew install flyctl
```

## 二、登录 Fly.io

```bash
fly auth login
```
浏览器会打开登录页面，使用 GitHub 账号登录即可。

## 三、部署步骤

### 1. 进入项目目录
```bash
cd /home/ww/Project/st2
```

### 2. 创建应用（首次部署）
```bash
fly launch --no-deploy
```
- 提示 app name 时输入你想要的名称（如 `st-api-ww`）
- 选择区域：建议选 `nrt`（东京）或 `hkg`（香港）
- 不需要 PostgreSQL 和 Redis

### 3. 创建持久化卷（重要！）
```bash
fly volumes create st_data --size 1 --region nrt
```
> 免费额度包含 3GB 存储，这里用 1GB 足够

### 4. 设置环境变量（敏感信息）
```bash
# JWT 密钥
fly secrets set JWT_SECRET_KEY=$(openssl rand -hex 32)

# Fernet 加密密钥
fly secrets set ENCRYPTION_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

# 管理员配置
fly secrets set ADMIN_USERNAME=admin
fly secrets set ADMIN_PASSWORD=你的强密码
fly secrets set ADMIN_PATH=你的隐藏后台路径
```

### 5. 部署
```bash
fly deploy
```

## 四、访问服务

### 获取域名
```bash
fly status
```
域名格式：`https://你的应用名.fly.dev`

### 访问
- API: `https://你的应用名.fly.dev/`
- 健康检查: `https://你的应用名.fly.dev/health`
- 管理后台: `https://你的应用名.fly.dev/你的ADMIN_PATH`

## 五、常用命令

```bash
# 查看日志
fly logs

# 查看状态
fly status

# SSH 进入容器
fly ssh console

# 重新部署
fly deploy

# 查看环境变量
fly secrets list

# 扩容/缩容
fly scale count 1
fly scale memory 512
```

## 六、更新代码

```bash
git add .
git commit -m "update"
git push origin main
fly deploy
```

## 七、费用说明

Fly.io 免费额度（每月）：
- 3 个共享 CPU VM
- 256MB 内存
- 3GB 持久存储
- 160GB 出站流量

个人项目完全够用，超出才收费。

## 八、故障排查

### 部署失败
```bash
fly logs --app 你的应用名
```

### 数据库问题
```bash
# 进入容器检查
fly ssh console
ls -la /data/
```

### 重启应用
```bash
fly apps restart
```
