#!/bin/sh
# Optional example check: is outbound traffic leaving through the expected country?
# Useful when some service must use a proxy/VPN exit. The lookup URL must return a bare
# two-letter country code (for example https://ipinfo.io/country). It only alerts;
# map "network:EGRESS_COUNTRY" as diagnose_only in your policy (never auto-change networks).
#   EXPECTED=DE COUNTRY_URL=https://ipinfo.io/country ./egress_country.sh
EXPECTED=${EXPECTED:?set EXPECTED to a two-letter country code}
COUNTRY_URL=${COUNTRY_URL:-https://ipinfo.io/country}
ALERT_CMD=${ALERT_CMD:-server-autoops alert}
got=$(curl -fsS --max-time 10 "$COUNTRY_URL" 2>/dev/null | tr -d ' \r\n' | tr 'a-z' 'A-Z')
[ "$got" = "$EXPECTED" ] && exit 0
# shellcheck disable=SC2086
$ALERT_CMD --service network --fault-code EGRESS_COUNTRY --level failure \
	--title "出口地区不符" --summary "期望 $EXPECTED，实际 ${got:-查询失败}"
