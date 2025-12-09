# Railway 部署教程

## 一、准备工作

### 1. 注册 Railway 账号
访问 [railway.app](https://railway.app) 使用 GitHub 账号登录

### 2. 确保代码已推送到 GitHub
```bash
cd st2
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/你的用户名/你的仓库名.git
git push -u origin main
```

## 二、部署步骤

### 1. 创建新项目
1. 登录 Railway Dashboard
2. 点击 **New Project**
3. 选择 **Deploy from GitHub repo**
4. 授权并选择你的仓库

### 2. 配置环境变量
在项目设置中添加以下环境变量（Variables 标签页）：

```
# 必须配置
JWT_SECRET_KEY=你的随机密钥（使用 openssl rand -hex 32 生成）
ENCRYPTION_KEY=你的Fernet密钥
ADMIN_USERNAME=admin
ADMIN_PASSWORD=你的强密码
ADMIN_PATH=你的隐藏后台路径

# 可选配置
DEBUG=false
LOG_LEVEL=INFO
CORS_ORIGINS=*
```

> ⚠️ 生成密钥命令：
> - JWT: `openssl rand -hex 32`
> - Fernet: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`

### 3. 添加持久化存储（重要！）
1. 在项目中点击 **+ New** → **Volume**
2. 挂载路径设置为：`/app/data`
3. 这样 SQLite 数据库文件会被持久化保存

### 4. 修改数据库路径
在环境变量中添加：
```
DATABASE_URL=sqlite+aiosqlite:///./data/api_service.db
```

### 5. 部署
Railway 会自动检测并部署，等待构建完成即可

## 三、访问服务

### 1. 获取域名
- 在项目设置中点击 **Settings** → **Networking**
- 点击 **Generate Domain** 生成免费域名
- 或绑定自定义域名

### 2. 访问后台
```
https://你的域名/你设置的ADMIN_PATH
```

## 四、常用命令

### 查看日志
在 Railway Dashboard 的 **Deployments** 标签页查看

### 重新部署
```bash
git add .
git commit -m "update"
git push
```
Railway 会自动触发重新部署

## 五、注意事项

1. **免费额度**：每月 $5（约 500 小时），个人项目足够
2. **休眠策略**：免费版不会休眠
3. **数据持久化**：必须添加 Volume，否则重启数据丢失
4. **环境变量**：敏感信息不要提交到代码仓库

## 六、故障排查

### 部署失败
- 检查 `requirements.txt` 是否完整
- 查看构建日志定位错误

### 数据库错误
- 确认 Volume 已正确挂载
- 检查 `DATABASE_URL` 路径是否正确

### 无法访问
- 确认已生成域名
- 检查 `PORT` 环境变量（Railway 自动设置）
