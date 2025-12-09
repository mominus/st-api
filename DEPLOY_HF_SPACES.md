# Hugging Face Spaces 部署教程

## 一、准备工作

### 1. 注册 Hugging Face 账号
访问 [huggingface.co](https://huggingface.co) 注册账号

### 2. 创建 Access Token
1. 点击头像 → Settings → Access Tokens
2. 创建新 Token，选择 `write` 权限
3. 保存 Token

## 二、创建 Space

### 方式一：网页创建（推荐）

1. 访问 [huggingface.co/new-space](https://huggingface.co/new-space)
2. 填写信息：
   - Space name: `st-api`（或你喜欢的名字）
   - License: 选择一个
   - SDK: 选择 **Docker**
   - Hardware: **CPU basic (Free)**
   - Visibility: **Private**（推荐，保护你的 API）
3. 点击 Create Space

### 方式二：命令行创建

```bash
# 安装 huggingface_hub
pip install huggingface_hub

# 登录
huggingface-cli login

# 创建 Space
huggingface-cli repo create st-api --type space --space-sdk docker
```

## 三、配置环境变量（Secrets）

在 Space 页面：Settings → Repository secrets

添加以下 Secrets：

| Name | Value |
|------|-------|
| `JWT_SECRET_KEY` | 随机字符串（用 `openssl rand -hex 32` 生成） |
| `ENCRYPTION_KEY` | Fernet 密钥 |
| `ADMIN_USERNAME` | admin |
| `ADMIN_PASSWORD` | 你的强密码 |
| `ADMIN_PATH` | 你的隐藏后台路径 |

> 生成 Fernet 密钥：
> ```python
> python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
> ```

## 四、推送代码

### 1. 克隆 Space 仓库
```bash
git clone https://huggingface.co/spaces/你的用户名/st-api
cd st-api
```

### 2. 复制项目文件
```bash
# 复制所有文件到 Space 目录
cp -r /home/ww/Project/st2/* .
```

### 3. 推送
```bash
git add .
git commit -m "Initial deployment"
git push
```

### 或者：直接推送到 HF（从现有项目）
```bash
cd /home/ww/Project/st2

# 添加 HF 远程仓库
git remote add hf https://huggingface.co/spaces/你的用户名/st-api

# 推送
git push hf main
```

## 五、访问服务

部署完成后（约 2-5 分钟），访问：

- Space 页面：`https://huggingface.co/spaces/你的用户名/st-api`
- API 直接访问：`https://你的用户名-st-api.hf.space`
- 管理后台：`https://你的用户名-st-api.hf.space/你的ADMIN_PATH`

## 六、使用云数据库（推荐）

⚠️ HF Spaces 免费版不支持持久化，建议使用免费云数据库。

### 方案：Supabase（免费 PostgreSQL）

#### 1. 创建数据库
1. 访问 [supabase.com](https://supabase.com) 注册
2. 创建新项目，选择区域（建议新加坡 `ap-southeast-1`）
3. 设置并记录数据库密码

#### 2. 获取连接字符串
项目 Settings → Database → Connection string → URI（选择 `Mode: Session`）

格式：
```
postgresql://postgres.xxxx:[密码]@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres
```

#### 3. 转换为异步格式
将 `postgresql://` 改为 `postgresql+asyncpg://`：
```
postgresql+asyncpg://postgres.xxxx:[密码]@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres
```

#### 4. 添加到 Secrets
在 HF Space Settings → Repository secrets 添加：
```
DATABASE_URL=postgresql+asyncpg://postgres.xxxx:[密码]@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres
```

### Supabase 免费额度
- 500MB 数据库存储
- 无限 API 请求
- 2 个项目
- 7 天无活动暂停（访问即恢复）

## 七、常用操作

### 查看日志
Space 页面 → Logs 标签

### 重启 Space
Settings → Factory reboot

### 更新代码
```bash
git add .
git commit -m "update"
git push hf main
```

## 八、注意事项

1. **Private Space**：建议设为私有，避免 API 被滥用
2. **休眠**：免费 Space 48 小时无访问会休眠，首次访问需等待启动
3. **资源限制**：免费版 2 vCPU + 16GB RAM，足够使用
4. **域名**：格式为 `用户名-space名.hf.space`
