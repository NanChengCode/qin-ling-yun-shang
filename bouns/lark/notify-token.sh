#!/bin/bash
# 抢单控制台每日令牌飞书推送 (由 grab-token-notify.timer 每天 12:00:30 以 root 触发)
# 仅当令牌在最近 3 分钟内被轮换时才推送, 避免服务未运行/未轮换时误发旧令牌。
# 每条通道发两条消息: ①仅令牌本身 (在飞书里长按该消息→复制, 即得纯令牌, 无需手动划选);
#                     ②说明文字 (更新时间与使用提示)。
# 通道: ①私聊 — runuser 以 wupengyu 的 lark-cli 登录态发应用机器人消息 (收件人 id 存 p2p-user-id);
#       ②群会话 — 群自定义机器人 webhook 直发 (外部群唯一可用方式, 无需应用发布)。
set -u
TOKEN_FILE=/opt/grab_tool/data/token.txt
WEBHOOK_FILE=/opt/grab_tool/lark/group-webhook.url
[ -f "$TOKEN_FILE" ] || { echo "token file missing" >&2; exit 0; }
NOW=$(date +%s)
MTIME=$(stat -c %Y "$TOKEN_FILE")
AGE=$((NOW - MTIME))
if [ "$AGE" -lt 0 ] || [ "$AGE" -gt 180 ]; then
  echo "skip: token not rotated recently (age=${AGE}s)" >&2
  exit 0
fi
TOKEN=$(tr -d '\r\n' < "$TOKEN_FILE")
[ -n "$TOKEN" ] || { echo "empty token" >&2; exit 0; }

# 消息①: 只含令牌本身, 方便整条消息一键复制
MSG_TOKEN="$TOKEN"
# 消息②: 说明文字
MSG_INFO=$(printf '【抢单控制台】每日访问令牌已更新\n更新时间: %s\n\n上方那条只有一串字符的消息就是新令牌: 长按它 → 复制 → 粘贴到浏览器登录框即可。\n(如未收到, 可登录服务器查看 data/token.txt)' "$(date '+%Y-%m-%d %H:%M:%S')")

# ① 私聊推送 (lark-cli, wupengyu 登录态)
RC=0
printf '%s' "$MSG_TOKEN" | runuser -u wupengyu -- /opt/grab_tool/lark/notify.sh || {
  echo "p2p token notify failed" >&2; RC=1; }
printf '%s' "$MSG_INFO" | runuser -u wupengyu -- /opt/grab_tool/lark/notify.sh || {
  echo "p2p info notify failed" >&2; RC=1; }

# ② 群会话推送 (自定义机器人 webhook; 未配置 group-webhook.url 则跳过)
WEBHOOK=$(tr -d '\r\n ' < "$WEBHOOK_FILE" 2>/dev/null || true)
if [ -n "${WEBHOOK:-}" ]; then
  send_webhook() {
    PAYLOAD=$(printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps({"msg_type":"text","content":{"text":sys.stdin.read()}}, ensure_ascii=False))')
    curl -sS --max-time 30 -X POST -H 'Content-Type: application/json' \
          --data-binary "$PAYLOAD" "$WEBHOOK" 2>&1
  }
  for m in "$MSG_TOKEN" "$MSG_INFO"; do
    RESP=$(send_webhook "$m") && \
      printf '%s' "$RESP" | grep -qE '"(code|StatusCode)": ?0' || {
        echo "group webhook send failed: $(printf '%s' "$RESP" | head -c 200)" >&2; }
  done
fi
exit $RC
