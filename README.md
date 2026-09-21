# 秦岭云商抢单工具

面向秦岭数字物流平台的多账号自动抢单工具。本地 HTTPS 控制台 + 多任务并行抢单引擎，部署在一台机器（推荐 Linux 服务器长期挂机），其他设备（手机/电脑）通过浏览器远程访问，下发抢单任务并实时查看执行结果。

> 纯 Python 标准库实现（仅依赖 `cryptography`），直接调用网页接口，无需浏览器、无需 Selenium。

## 功能特性

### 抢单引擎

- **多账号、多任务并行抢单**：单账号可同时跑多个订单任务，多账号互不干扰
- **同账号共享登录**：账号级锁串行化登录，只真正登录一次，其余任务复用缓存 Cookie（短信二次验证码只消耗一次）
- **车辆份额自动拆分**：同账号、同订单、同车队、同日期的 N 个任务，自动把车队车辆按 N 份互不重叠拆分（按下标取模交错选取，大车/小车分布均匀）
- **快速提交**：容量校验通过后不再重复校验，每 150ms 直接连续提交；根据服务器返回的「最多只可能继续派车【N】」就地缩减车辆数，省一次校验往返
- **智能轮询重试**：
  - 订单尚未发布 → 按间隔轮询等待订单出现（默认 1s/60s 超时）
  - 今日可派车数为 0 / 剩余量不足 → 持续轮询等待配额刷新（默认 150ms/15 分钟超时）
  - 提交失败原因无法识别 → 原参数快速重试 3 次，仍失败则回退容量校验，按实时剩余量重新评估
- **容量自动缩减**：剩余量不足时自动过滤超载车辆 + 贪心组合，凑出满足载重约束的最大子集
- **演练模式**：只走完整流程不真实提交。控制台全局开关默认关闭（直接真实抢单，任务启动时以开关为准）；命令行默认 `--dry-run`，须显式 `--no-dry-run` 才真实下单
- **会话失效自愈**：检测到登录失效自动清除 Cookie 缓存，后续任务重新登录

### 控制台

- 暗色单页 Web 界面：账号管理、订单/车队查询、任务增删、一键启动/停止、日志实时滚动
- **信息查询**：候选订单列表（含每日预约配额）、车队列表、车队车辆状态统计（空闲/已派/排队/已发运）
- 状态持久化：账号/任务/演练开关落盘（`state.json`），服务重启自动恢复（运行中任务标记为已停止），可一键「重跑」
- 多设备协同：以服务端状态为准，多设备实时同步；密码不回传浏览器
- 日志管理：实时控制台按轮次显示（新一轮抢单自动清空，只推本次轮次）；每个任务独立日志落盘 `logs/tasks/`，任务行「日志」按钮切换查看（运行中实时尾随）；按天滚动文件自动清理 10 天前日志

### 安全机制

| 机制 | 说明 |
|---|---|
| 访问令牌 | 所有 API 要求令牌（`X-Auth-Token` 头 / `?token=` 参数 / Cookie 三选一），首次启动自动生成并保存在 `data/token.txt` |
| 每日令牌轮换 | 每天 12:00 自动重新生成令牌（`--rotate-at` 可改，`off` 关闭），新令牌经飞书推送（`grab-token-notify.timer` 定时任务或 `--notify-cmd`） |
| HTTPS | 默认开启（自签证书有效期 10 年，SAN 自动含 localhost/局域网 IP，可追加公网域名），令牌和账号密码不明文过公网 |
| 密码加密落盘 | `state.json` 中密码用 Fernet 加密（密钥 `data/secret.key`，权限 600），密码不回传给浏览器 |
| 防泄漏细节 | 日志不记录含 `?token=` 的完整 URL；令牌比较用 `hmac.compare_digest`；状态原子写（tmp + replace） |

## 架构与模块

```
┌─────────────────────────────────────────────┐
│                grab_ui.html                 │  控制台前端 (单页)
└──────────────────┬──────────────────────────┘
                   │ REST API (HTTPS + 令牌鉴权)
┌──────────────────▼──────────────────────────┐
│                grab_server.py               │  ThreadingHTTPServer 控制台服务
│  多账号管理 / 任务调度 / 共享登录 / 状态持久化 │
└──────┬───────────────────────┬──────────────┘
       │ 子进程调度              │ 子进程调度
┌──────▼──────────┐   ┌────────▼─────────┐
│  grab_order.py  │   │  grab_query.py   │  抢单引擎 / 信息查询
└──────┬──────────┘   └────────┬─────────┘
       └───────────┬───────────┘
        ┌──────────▼──────────┐
        │   grab_common.py    │  HTTP 会话(Cookie维护) / RSA 登录 / 日志
        └─────────────────────┘
```

| 文件 | 职责 |
|---|---|
| `grab_server.py` | 控制台服务：账号/任务管理、并行调度、令牌鉴权与轮换、状态加密持久化、日志 |
| `grab_order.py` | 抢单引擎 CLI：登录 → 订单轮询 → 解析派车表单 → 车队车辆 → 容量校验 → 快速提交 |
| `grab_query.py` | 信息查询 CLI：候选订单（含配额）/ 车队 / 车队车辆统计 |
| `grab_common.py` | 公共模块：Cookie 自动维护的 HTTP 会话、RSA PKCS1 v1.5 加密（与网站 JSEncrypt 一致）、登录（含短信 2FA）、日志 |
| `grab_ui.html` | 控制台前端（单页、暗色主题） |
| `start_server.sh` / `grab_server.service` / `启动抢单界面.bat` | Linux / systemd / Windows 启动入口 |
| `*.spec` | PyInstaller 打包配置（Windows exe） |

**技术栈**：Python 3（`http.server` / `urllib` / `threading` 标准库）+ `cryptography`（RSA 登录加密、Fernet 状态加密、x509 自签证书），前端原生 HTML/JS，无框架。

## 快速开始

### 方式一：控制台（推荐）

```bash
# Linux
cd bouns
pip3 install cryptography    # 依赖 (一般已安装)
./start_server.sh            # 监听 0.0.0.0:8787, 前台运行, Ctrl+C 停止

# Windows
双击 启动抢单界面.bat          # 默认仅本机访问 https://127.0.0.1:8787/
```

启动输出会显示**访问令牌**（已保存到 `data/token.txt`）和访问地址。浏览器打开 `https://127.0.0.1:8787/`，输入令牌进入控制台：添加账号 → 查询订单/车队 → 添加抢单任务 → 「开始全部」。

> 首次访问有自签证书安全警告属预期；手机可下载 `https://<IP>:8787/cert` 安装证书消除警告。

### 方式二：命令行直用

```bash
# 抢单（默认演练模式, 只走流程不提交）
python3 grab_order.py --username 138xxxx --password 密码 \
    --order-code SG260826133791-01 --fleet-name 发发发 --date 今日

# 真实抢单需显式关闭演练模式
python3 grab_order.py --username 138xxxx --password 密码 \
    --order-code SG260826133791-01 --fleet-name 发发发 --date 今日 --no-dry-run

# 信息查询
python3 grab_query.py --username 138xxxx --password 密码 --action Orders --with-quota
python3 grab_query.py --username 138xxxx --password 密码 --action Fleets
python3 grab_query.py --username 138xxxx --password 密码 --action FleetVehicles --fleet-id 5531
```

常用参数（`grab_order.py`）：

| 参数 | 说明 |
|---|---|
| `--cookie "k=v; ..."` | 注入共享 Cookie 跳过登录（调试用） |
| `--date 今日/明日/后日` | 计划预约日期，默认今日 |
| `--sms-code xxx` | 短信二次验证码（账号开启 2FA 时） |
| `--max-vehicle N` | 最多选取 N 辆车（默认不限，抢整个车队） |
| `--share-index/--share-total` | 车辆份额拆分（由服务端多任务调度时自动传入） |
| `--poll-interval/--poll-timeout` | 订单轮询间隔/超时，默认 1s / 60s |
| `--retry-interval/--retry-timeout` | 无可派时轮询间隔/总超时，默认 150ms / 900s（15 分钟） |
| `--dry-run` / `--no-dry-run` | 演练模式（默认开启）/ 真实抢单 |

服务端常用参数（`grab_server.py`）：

| 参数 | 说明 |
|---|---|
| `--host` / `--port` | 监听地址/端口，默认 `0.0.0.0` / `8787` |
| `--token xxx` | 指定访问令牌（指定后停用每日轮换） |
| `--rotate-at HH:MM` | 每日令牌轮换时刻，默认 `12:00`，`off` 关闭 |
| `--notify-cmd "cmd"` | 轮换后推送新令牌的自定义命令（stdin 读消息） |
| `--cert-sans "域名,IP"` | 追加证书 SAN（公网域名/IP） |
| `--no-https` | 关闭 HTTPS（仅用于 frp/nginx 已终结 TLS 的场景） |
| `--no-browser` | 启动后不自动打开浏览器 |

## 目录结构

```
.
├── README.md                  # 本文档
├── .gitignore                 # 排除构建产物 / data 密钥 / 日志 / 大文件
└── bouns/
    ├── grab_server.py         # 控制台服务
    ├── grab_order.py          # 抢单引擎
    ├── grab_query.py          # 信息查询
    ├── grab_common.py         # 公共模块
    ├── grab_ui.html           # 控制台前端
    ├── start_server.sh        # Linux 启动脚本
    ├── grab_server.service    # systemd 单元模板
    ├── 启动抢单界面.bat        # Windows 启动入口
    ├── *.spec                 # PyInstaller 打包配置
    ├── data/                  # 运行时数据: token.txt / secret.key / server.key / state.json (已 gitignore, 严禁提交)
    ├── logs/                  # 每日日志, 保留 10 天 (已 gitignore)
    └── README-远程访问.md      # 远程访问部署文档
```

## 远程访问与部署

局域网访问、公网穿透（frp）、systemd 开机自启、每日令牌飞书推送等详见 [bouns/README-远程访问.md](bouns/README-远程访问.md)。

## 注意事项

- **控制台演练开关默认关闭**：右上角可切换「演练/真实」全局开关，任务启动时以全局开关为准；命令行默认 `--dry-run`，必须显式 `--no-dry-run` 才会真实下单
- `data/` 目录含访问令牌、加密密钥与 TLS 私钥，等同完整控制权，请勿外泄或整目录分享
- 派车单创建后为初始化状态，请到平台【派车单管理】中确认/审核
- 本工具请仅用于本人有权限的账号与订单
