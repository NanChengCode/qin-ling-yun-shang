# 秦岭云商抢单工具 · 远程访问部署说明

服务部署在一台机器上（推荐 Linux 服务器长期挂机），其他设备（手机/电脑）通过浏览器远程访问，下发抢单任务并实时查看执行结果。

## 安全机制

| 机制 | 说明 |
|---|---|
| 访问令牌 | 所有 API 要求令牌（`X-Auth-Token` 头 / `?token=` 参数 / Cookie 三选一）。首次启动自动生成并保存在 `data/token.txt`。**每天 12:00 自动重新生成**（`--rotate-at HH:MM` 可改，`off` 关闭），新令牌经飞书推送；`--token xxx` 指定时停用轮换 |
| HTTPS | 默认开启（自签证书，有效期 10 年），令牌和账号密码不明文过公网。证书 SAN 自动包含 localhost/局域网 IP，公网域名用 `--cert-sans` 追加 |
| 密码加密落盘 | `data/state.json` 中密码用 Fernet 加密（密钥 `data/secret.key`，权限 600）。密码**不**回传给浏览器 |
| 状态持久化 | 账号/任务/演练开关存 `data/state.json`，服务重启自动恢复（运行中的任务标记为已停止） |

## 一、Linux 部署（当前机器）

```bash
cd /home/wupengyu/qinlingyunshang/bouns
pip3 install cryptography        # 依赖 (一般已安装)
./start_server.sh                # 前台运行, Ctrl+C 停止
```

启动输出示例：

```
========== 秦岭云商抢单控制台已启动 ==========
访问令牌: XXXX... (请妥善保存, 已存于 /.../bouns/data/token.txt)
本机访问: https://127.0.0.1:8787/
局域网访问: https://172.18.56.24:8787/
```

常用参数：

```
--host 0.0.0.0      监听地址 (默认 0.0.0.0 所有网卡)
--port 8787         端口
--token xxx         指定访问令牌 (默认读/生成 data/token.txt; 指定后停用每日轮换)
--rotate-at 12:00   每日令牌轮换时刻 HH:MM (默认 12:00; off 关闭)
--notify-cmd "cmd"  轮换后推送新令牌的自定义命令 (stdin 读消息; 默认不推送)
--cert-sans "grab.example.com"   公网域名/IP 加入证书 (逗号分隔)
--no-https          关闭 HTTPS (令牌明文传输! 仅用于 frp/nginx 终结 TLS 场景)
```

### systemd 开机自启

```bash
sudo cp grab_server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now grab-server
sudo journalctl -u grab-server -n 30     # 查看启动输出(含令牌)
```

> 注意：按实际路径修改单元文件中的 `WorkingDirectory`/`ExecStart`/`User`；令牌不写进 ExecStart，避免 `ps` 泄露。

### 每日令牌飞书推送（可选）

服务每天 12:00 轮换令牌后，由独立定时任务把新令牌经 lark-cli 发到管理员的飞书：

```bash
sudo tee /etc/systemd/system/grab-token-notify.service <<'EOF'
[Unit]
Description=抢单控制台每日令牌飞书推送
[Service]
Type=oneshot
ExecStart=/opt/grab_tool/lark/notify-token.sh
EOF
sudo tee /etc/systemd/system/grab-token-notify.timer <<'EOF'
[Unit]
Description=抢单控制台每日令牌飞书推送定时器 (每天 12:00:30)
[Timer]
OnCalendar=*-*-* 12:00:30
Persistent=true
[Install]
WantedBy=timers.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now grab-token-notify.timer
```

- 脚本源文件在 `bouns/lark/`（`notify-token.sh` / `notify.sh`），部署到 `/opt/grab_tool/lark/`。
- `notify-token.sh`：仅当令牌在最近 3 分钟内被轮换时才推送（防止服务未运行时误发旧令牌）。**每条通道发两条消息**：①仅令牌本身（飞书里长按该消息 → 复制，即得纯令牌，粘贴到登录框即可）；②说明文字（更新时间与使用提示）。双通道：①私聊——经 `runuser` 以飞书登录用户身份调 `notify.sh` 发应用机器人 P2P 消息，收件人 open_id 存 `/opt/grab_tool/lark/p2p-user-id`（root 600，不进 git）；②群会话——读 `/opt/grab_tool/lark/group-webhook.url`（600 root）POST 到群自定义机器人 webhook（外部群唯一可用方式，无需应用发布；未配置时静默跳过）。
- 收不到消息时的兜底：登录服务器 `cat data/token.txt`。

## 二、公网访问（frp 内网穿透）

### 方式 A：TCP 透传（推荐，简单，端到端 TLS）

frpc 配置：

```ini
[grab]
type = tcp
localIP = 127.0.0.1
localPort = 8787
remotePort = 8787
```

公网访问：`https://服务器公网IP:8787/`（TLS 由本服务自签证书端到端承载，只需一次性信任证书）。域名需加 `--cert-sans "你的域名"` 并重启（证书重新生成）。

### 方式 B：frp 终结 TLS + 域名证书（无浏览器告警）

frpc 配置（frps 上需配置域名证书）：

```ini
[grab-https]
type = https
customDomains = ["grab.example.com"]
localIP = 127.0.0.1
localPort = 8787
```

此时本服务用 `--no-https` 运行（TLS 由 frps 终结）。

### 防火墙

```bash
sudo ufw allow 8787/tcp        # 直接暴露端口时
# 若走 frp 且 frpc 在本机: 无需开放 8787, frpc 走回环即可
```

## 三、手机/浏览器使用

1. 打开 `https://<服务器IP或域名>:8787/`，浏览器提示证书不安全 → **高级 → 继续访问**（自签证书属预期）
2. 输入访问令牌（`cat data/token.txt` 可查看）→ 进入控制台；令牌保存在本机浏览器，下次自动登录
3. 添加账号 → 查询订单/车队 → 添加抢单任务 → 「开始全部」，日志实时刷新
4. 手机安装证书可消除警告（可选）：
   - 打开 `https://<IP>:8787/cert` 下载证书（需带令牌，如追加 `?token=xxx`）
   - **Android**：设置 → 安全 → 加密与凭据 → 安装 CA 证书（安装后 Chrome 直接信任）
   - **iOS**：下载后 设置 → 已下载描述文件 → 安装，再到 通用 → 关于本机 → 证书信任设置 → 完全信任
   - 证书重新生成（如更换域名）后需重新安装

## 四、多设备协同

- 账号/任务/演练开关以**服务端**为准，多设备实时同步（400ms 轮询）
- 服务端不回传密码：其他设备首次打开时密码框为空（提示「密码未在本机保存」），任务执行不受影响（服务端用自己存的密码登录）；该设备上做查询或改凭据前需补录一次密码
- 任务执行完可点「重跑」再次执行（状态重置为待执行）
- 同一账号被多设备同时启动时会拒绝重复启动

## 五、安全注意事项

1. 令牌等同于完整控制权：不要泄露，不要通过不安全的渠道传输
2. 优先使用 header/Cookie 通道；`?token=` 会进入浏览器历史和反向代理日志
3. 公网访问**务必保持 HTTPS 开启**（默认即开），`--no-https` 仅用于 frp/nginx 已终结 TLS 的场景
4. 自签证书首次信任时确认「指纹/颁发者 = qinling-grab-server」，防止中间人
5. `data/` 目录含密钥与加密后的凭据，注意文件权限；不要整目录分享给他人

## 六、Windows 部署（兼容）

双击 `启动抢单界面.bat` 默认仅本机访问（https://127.0.0.1:8787/）。需要远程时：

```
grab_server.py --host 0.0.0.0
```

PyInstaller 打包配置无需修改（`grab_server.spec` 已包含前端和 cryptography；证书/状态运行时生成）。
