#!/bin/bash
# 抢单控制台 → 飞书私聊推送包装脚本 (由 notify-token.sh 经 runuser 以 wupengyu 身份调用)
# stdin 读入消息正文, 以应用机器人身份发给管理员私聊 (吴鹏宇)。
# 收件人 open_id 存于 /opt/grab_tool/lark/p2p-user-id (root 600, 不进 git)。
# 群会话推送不走本脚本 (外部群需用自定义机器人 webhook, 见 notify-token.sh)。
set -u
export HOME=/home/wupengyu
export PATH=/home/wupengyu/.nvm/versions/node/v22.23.2/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export LANG=C.UTF-8
MESSAGE="$(cat)"
if [ -z "$MESSAGE" ]; then
  echo "empty message" >&2
  exit 2
fi
USER_ID=$(tr -d '\r\n ' < /opt/grab_tool/lark/p2p-user-id 2>/dev/null || true)
if [ -z "$USER_ID" ]; then
  echo "missing /opt/grab_tool/lark/p2p-user-id" >&2
  exit 2
fi
exec lark-cli im +messages-send --as bot \
  --user-id "$USER_ID" \
  --text "$MESSAGE" --format json >/dev/null
