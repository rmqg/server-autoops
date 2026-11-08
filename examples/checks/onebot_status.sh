#!/bin/sh
# Optional example check for chat bots that expose a OneBot v11 HTTP API
# (for example NapCat-compatible gateways).
# When the account is no longer online it writes "<service>:LOGIN_LOST", which an example
# policy maps with "needs_scan": true - a human has to log in again; nothing is restarted.
#   ONEBOT_URL=http://127.0.0.1:3000 ONEBOT_TOKEN_FILE=/etc/bot/onebot.token ./onebot_status.sh
ONEBOT_URL=${ONEBOT_URL:-http://127.0.0.1:3000}
SERVICE=${SERVICE:-chat-bot}
ALERT_CMD=${ALERT_CMD:-server-autoops alert}
auth=""
[ -n "$ONEBOT_TOKEN_FILE" ] && [ -r "$ONEBOT_TOKEN_FILE" ] && auth="Authorization: Bearer $(cat "$ONEBOT_TOKEN_FILE")"
body=$(curl -fsS --max-time 8 ${auth:+-H "$auth"} "$ONEBOT_URL/get_status" 2>/dev/null)
case "$body" in
	*'"online":true'*|*'"online": true'*) exit 0 ;;
esac
# shellcheck disable=SC2086
$ALERT_CMD --service "$SERVICE" --fault-code LOGIN_LOST --level failure \
	--title "$SERVICE 登录掉线" --summary "OneBot get_status 显示不在线，需要人工重新登录"
