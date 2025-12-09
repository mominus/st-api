# Render 部署教程

## 一、准备云数据库（Supabase）

由于 Render 免费版不支持持久化存储，需要使用外部数据库。

### 1. 创建 Supabase 项目
1. 访问 [supabase.com](https://supabase.com) 注册
2. 创建新项目，选择区域（建议 `Singapore`）
3. 设置并记录数据库密码

### 2. 获取连接字符串
1. 项目 Settings → Database
2. 找到 Connection string → URI（选择 `Mode: Session`）
3. 复制连接字符串

### 3. 转换为异步格式
将 `postgresql://` 改为 `postgresql+asyncpg://`：
```
postgresql+asyncpg://postgres.xxxx:[密码]@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres
```

## 二、部署到 Render

### 1. 注册 Render
访问 [render.com](https://render.com) 使用 GitHub 登录

### 2. 创建 Web Service
1. Dashboard → **New** → **Web Service**
2. 连接 GitHub 仓库 `ww51wake/st`
3. 配置：
   - **Name**: `st-api`（或你喜欢的名字）
   - **Region**: `Singapore`（或离你近的区域）
   - **Branch**: `main`
   - **Runtime**: `Python 3`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
   - **Instance Type**: `Free`

### 3. 配置环境变量
在 **Environment** 中添加：

| Key | Value |
|-----|-------|
| `DATABASE_URL` | `postgresql+asyncpg://...`（Supabase 连接字符串） |
| `JWT_SECRET_KEY` | 随机字符串（`openssl rand -hex 32`） |
| `ENCRYPTION_KEY` | Fernet 密钥（见下方生成方法） |
| `ADMIN_USERNAME` | `admin` |
| `ADMIN_PASSWORD` | 你的强密码 |
| `ADMIN_PATH` | 你的隐藏后台路径 |
| `PYTHON_VERSION` | `3.12.0` |

> 生成 Fernet 密钥：
> ```bash
> python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
> ```

### 4. 部署
点击 **Create Web Service**，等待部署完成（约 2-5 分钟）

## 三、配置自定义域名

### 1. 添加域名
1. 进入你的 Web Service
2. **Settings** → **Custom Domains**
3. 点击 **Add Custom Domain**
4. 输入你的域名（如 `api.example.com`）

### 2. 配置 DNS
在你的域名服务商处添加 CNAME 记录：
```
类型: CNAME
名称: api（或你想要的子域名）
值: st-api.onrender.com（Render 提供的域名）
```

### 3. 等待生效
- DNS 生效需要几分钟到几小时
- Render 会自动配置 SSL 证书

## 四、访问服务

### Render 默认域名
```
https://st-api.onrender.com
```

### 自定义域名
```
https://api.你的域名.com
```

### 管理后台
```
https://你的域名/你的ADMIN_PATH
```

## 五、常用操作

### 查看日志
Dashboard → 你的服务 → **Logs**

### 手动部署
Dashboard → 你的服务 → **Manual Deploy** → **Deploy latest commit**

### 重启服务
Dashboard → 你的服务 → **Manual Deploy** → **Clear build cache & deploy**

## 六、注意事项

### 休眠机制
- 免费版 15 分钟无请求会休眠
- 首次访问需等待 10-30 秒启动
- 可使用 [UptimeRobot](https://uptimerobot.com) 免费监控保活

### 免费额度
- 750 小时/月（约 31 天连续运行）
- 自动 SSL 证书
- 自定义域名

### 数据库
- 必须使用外部数据库（Supabase 免费）
- 不要使用 SQLite（重启数据丢失）

## 七、保活方案（可选）

使用 UptimeRobot 每 5 分钟 ping 一次防止休眠：

1. 访问 [uptimerobot.com](https://uptimerobot.com) 注册
2. 添加监控：
   - Monitor Type: HTTP(s)
   - URL: `https://你的域名/health`
   - Monitoring Interval: 5 minutes
