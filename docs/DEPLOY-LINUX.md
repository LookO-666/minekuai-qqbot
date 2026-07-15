# Linux 服务器 Docker 部署

完整流程见 [README.md 的快速开始](../README.md#快速开始linux--docker) ——这里只补充 Linux 服务器特有的注意点和运维技巧。

---

## 装 Docker

如果服务器还没装：

```bash
# Ubuntu / Debian
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker

# CentOS / Rocky / RHEL
sudo dnf install -y dnf-plugins-core
sudo dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
newgrp docker
```

### 国内服务器加速（建议）

```bash
sudo mkdir -p /etc/docker
sudo tee /etc/docker/daemon.json <<EOF
{
  "registry-mirrors": [
    "https://docker.m.daocloud.io",
    "https://dockerproxy.com"
  ]
}
EOF
sudo systemctl restart docker
```

---

## 防火墙

WebUI 端口（6099）只在第一次扫码登录 QQ 时需要外露，之后建议关掉：

```bash
# Ubuntu
sudo ufw allow 6099/tcp                   # 临时开放扫码
# ...登录完成后...
sudo ufw deny 6099/tcp                    # 关回去

# CentOS
sudo firewall-cmd --add-port=6099/tcp --permanent
sudo firewall-cmd --reload
```

阿里云 / 腾讯云用户还要在控制台**安全组**也加一条对应规则。

> SSH 用户更推荐：不开放 6099，只用 SSH 端口转发：
> ```bash
> ssh -L 6099:127.0.0.1:6099 user@server
> ```
> 本地浏览器访问 `http://127.0.0.1:6099/webui/?token=xxx`

---

## 自启 / 开机恢复

`docker-compose.yml` 里两个容器都设了 `restart: unless-stopped`，配合 Docker 自启就能开机自动恢复。确认 Docker 自启：

```bash
sudo systemctl enable docker
```

---

## systemd（不想用 docker-compose）

如果你不想用 docker-compose，直接 `python bot.py` + systemd 也行。模板：[deploy/minekuai-bot.service](../deploy/minekuai-bot.service)

---

## 备份

服务器配置 + 操作日志都在一个 SQLite 文件里：

```bash
cp bot-data/operation_log.db ~/backup/operation_log.db.$(date +%F)
```

QQ 登录态：

```bash
tar czf ~/backup/napcat-data.$(date +%F).tar.gz napcat-data/
```

---

## 升级

```bash
cd minekuai-qqbot
git pull
docker compose up -d --build bot              # 只重建 bot
```

`servers` 表的 schema 迁移是幂等的（启动时自动 ALTER TABLE 加新列），不用手动迁移。

---

## 常见 Linux 特有问题

**Q: napcat 容器 Up 但 6099 端口不通**

通常是 X11/Xvfb 启动失败。看 `docker compose logs napcat` 找 `Missing X server` / `server already running`。`docker-compose.yml` 已经有针对这个问题的 `entrypoint:` 覆盖（创建 `/tmp/.X11-unix` + 把 Xvfb 显示号改成 `:99` 避开宿主机冲突）。

**Q: 服务器有 SELinux**

挂载 volume 可能要加 `:Z` 后缀：

```yaml
volumes:
  - ./napcat-data/QQ:/app/.config/QQ:Z
```

**Q: 内网穿透 / 反向代理**

bot 监听 `127.0.0.1:8080`，napcat 也跑在 `127.0.0.1`，所有流量都走本地。**没有任何端口需要映射出公网**——bot 是从 QQ 服务器主动收消息，不需要公网入口。

---

## 完整流程速记

```bash
# SSH 上服务器，装好 Docker（见上文）
git clone <repo> minekuai-qqbot
cd minekuai-qqbot
cp bot/.env.example bot/.env
chmod 600 bot/.env
nano bot/.env                 # 填 ALLOWED_GROUPS 等

docker compose up -d napcat
docker compose logs napcat | grep -iE "webui|token"
# 用 SSH 转发或开 6099 端口，浏览器登录 + 配置反向 WS（见 README）

docker compose up -d --build bot
docker compose logs -f bot
# 等到 "Bot xxx connected"

# QQ 群里发『帮助』测试，再发『添加服务器』加配置
```
