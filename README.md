# TG 签到控制台

一个用 Docker 部署的 Telegram 群组自动签到服务：用你自己的 Telegram 账号，按设定的 cron 时间向指定群组发送签到指令，并通过 Web 面板管理群组、查看日志和运行结果。

## 功能

- 支持多个群组，每个群组可单独设置签到指令和多个执行时间
- Telegram 账号登录（验证码 + 两步验证），会话持久化，容器重启不用重新登录
- 中文 Web 面板：概览、群组管理、账号登录、系统设置、运行日志
- 失败自动重试、随机延迟、失败关键词识别
- 发送到指定会话的运行结果通知
- 模拟运行模式，先验证连接不真发消息
- 面板密码保护，SQLite 存储所有配置与日志

## 一、准备 Telegram API 凭据

1. 打开 <https://my.telegram.org/apps> 并用你的 Telegram 账号登录。
2. 创建一个应用（API development tools），记下 **App api_id** 和 **App api_hash**。
3. 面板里填写手机号时要带国家区号，例如 `+8613800138000`。

> 这一步拿的是你自己账号的 API 凭据，不是 BotFather 的 bot token。因为签到通常要求「群成员本人」发指令，所以用用户账号发送，机器人 token 通常无法触发群里的签到机器人。

## 二、Docker 部署

```bash
cd tg-checkin
cp .env.example .env
# 修改 .env 里的 PANEL_PASSWORD（强烈建议）
docker compose up -d --build
```

打开 <http://服务器IP:8000> ，输入面板密码进入控制台。

`.env` 中的 `PANEL_PASSWORD` 只在数据库首次初始化时生效，后续请在面板「系统设置」里修改。

常用命令：

```bash
docker compose logs -f          # 查看运行日志
docker compose restart          # 重启
docker compose down             # 停止（数据保留在 ./data）
docker compose up -d --build    # 修改代码后重新构建
```

## 三、部署到 Koyeb

Koyeb 免费组织通常没有持久卷权限，容器文件系统是临时的，因此登录会话和群组配置在重建实例后会丢失。本项目支持把完整配置导出到环境变量 `TGCK_CONFIG`，在容器首次初始化时自动恢复。

### 从零部署的命令

```bash
koyeb deploy . <org>/<app> \
  --archive-builder docker \
  --ports 8000 --checks 8000:http:/health \
  --regions was --instance-type free \
  --env PANEL_PASSWORD='换成你自己的强密码' \
  --env TZ=Asia/Shanghai --env DATA_DIR=/app/data \
  --wait

# 改为 GitHub 驱动部署（推送 main 自动更新）
koyeb services update <org>/<app> \
  --git github.com/<你的用户名>/tg-checkin --git-branch main --git-builder docker
```

### 两个重要限制

1. **没有持久卷**：当前组织没有卷权限，容器文件系统是临时的，重建实例后登录会话和群组配置会丢失。本项目支持把配置导出到环境变量 `TGCK_CONFIG`，在容器首次初始化时自动恢复：
   - 面板「系统设置」→「导出配置（含登录会话）」，下载 `tg-checkin-config.json`。
   - 保存为 Koyeb Secret 并绑定：
     ```bash
     cat tg-checkin-config.json | koyeb secrets create tgck_config --value-from-stdin
     koyeb services update <org>/<app> --env 'TGCK_CONFIG={{secret.tgck_config}}'
     ```
   - JSON 内含 Telegram 会话，等同于账号凭证，请妥善保管。
2. **Free 实例会休眠**：平台规定 free 实例最多空闲约 65 分钟后深度休眠，且不允许 `min-scale=1`。休眠期间调度器不会运行。要保证 7×24 自动签到，需要在 Koyeb 绑定支付方式后把实例规格改为 `nano` 或更高（`koyeb services update <org>/<app> --instance-type nano`），或用外部定时请求持续唤醒面板地址。

## 四、面板使用流程

1. **账号与登录**：填写 API ID、API Hash、手机号（境外网络环境可填 socks5/http 代理），点「保存凭据」，再点「获取验证码并登录」。验证码会发到你的 Telegram 账号（一般在 Saved Messages 或官方通知里），填入后完成登录；开启两步验证的账号继续填验证密码。
2. **账号与登录**：点「检查登录状态」确认已登录。
3. **系统设置**：按需调整时区、重试次数、随机延迟；首次使用建议开启「模拟运行」测试一遍。
4. **群组管理**：添加群组，填写：
   - 群组名称：方便自己识别
   - 群组链接 / 用户名 / ID：`@群用户名`、邀请链接 `https://t.me/+xxxx`、或 `-1001234567890`
   - 签到指令：群里的签到命令，如 `/checkin`、`/sign`、`签到`；必须和该群要求一致
   - 签到时间：5 段 cron，每行一个
5. 用「测试」按钮立即发一次，确认日志里能看到机器人回复；确认无误后关闭模拟运行。

### cron 格式

```
分 时 日 月 周
```

| 表达式 | 含义 |
| --- | --- |
| `0 8 * * *` | 每天 08:00 |
| `0 8,20 * * *` | 每天 08:00 和 20:00 |
| `*/30 * * * *` | 每 30 分钟 |
| `0 9 * * 1-5` | 工作日 09:00 |

时区在「系统设置」中修改，例如 `Asia/Shanghai`、`Asia/Tokyo`、`UTC`。修改后调度任务会自动重载。

## 五、数据与备份

所有数据都在宿主机的 `./data/checkin.db`：包括配置、Telegram 会话字符串、群组和日志。备份时停止容器后复制该文件即可：

```bash
docker compose stop
cp data/checkin.db data/checkin.db.bak
docker compose start
```

注意：这个文件包含 Telegram 会话，等同于账号登录凭证，请不要泄露或上传到公开仓库。

## 六、常见问题

- **收不到验证码**：确认手机号带区号；确认 API ID/Hash 正确；网络受限时配置代理。验证码有时会被 Telegram 归到「Saved Messages」。
- **提示触发限流（FloodWait）**：Telegram 要求等待若干秒，按提示时间过后再试，不要连续重试。
- **提示无法访问群组**：你的 Telegram 账号必须先加入该群；私有群建议用邀请链接或 `https://t.me/c/群ID/任意消息ID`（程序会自动转成 `-100群ID`）。
- **消息发出但机器人不回复**：检查签到指令是否正确，有些群要求不带 `/`，有些要求纯文本（例如 `签到`）。可在「系统设置」里填失败关键词辅助判断。
- **面板打不开**：确认 `docker compose ps` 中容器 healthy，确认服务器安全组放行了 8000 端口。
- **忘记面板密码**：密码保存在数据库中，`.env` 里的 `PANEL_PASSWORD` 只在首次初始化时生效。可在宿主机执行下面的命令重置为 `admin123`（不会丢失其他配置）：
  ```bash
  docker exec -it tg-checkin python -c "from app import db; db.init_db(); db.set_setting('panel_password','admin123')"
  docker compose restart
  ```

## 七、本地冒烟测试（可选）

不需要连接 Telegram 即可验证面板接口：

```bash
pip install -r requirements.txt
# 若 TestClient 提示缺少依赖，再安装 httpx 或 httpx2
python tests/smoke_test.py
```

## 八、使用提醒

该工具会代表你的账号在群里定时发送消息。请只在你有权参与的群组中使用，遵守 Telegram 服务条款和群规，不要用于批量骚扰、刷屏或规避平台限制；频繁自动化操作可能导致账号被限制。建议开启随机延迟，并避免把间隔设得过小。
