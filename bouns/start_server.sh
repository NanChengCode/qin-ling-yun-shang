#!/usr/bin/env bash
# 秦岭云商抢单工具 - Linux 远程服务启动脚本
# 用法: ./start_server.sh [额外参数, 如 --port 8788 --no-https]
# 启动后在控制台输出中查看: 访问令牌 + 本机/局域网访问地址
cd "$(dirname "$0")" || exit 1
exec python3 grab_server.py --host 0.0.0.0 --port 8787 "$@"
