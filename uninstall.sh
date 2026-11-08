#!/bin/sh
# Remove the program and the service. Keeps /etc/server-autoops and /var/lib/server-autoops
# unless --purge is given (they contain your policy, queue history and run records).
set -eu
PREFIX=/opt/server-autoops; ETC=/etc/server-autoops; STATE=/var/lib/server-autoops; BIN_DIR=/usr/local/bin; PURGE=0
while [ $# -gt 0 ]; do
	case "$1" in
		--prefix) PREFIX=$2; shift 2 ;; --etc) ETC=$2; shift 2 ;; --state) STATE=$2; shift 2 ;;
		--bin-dir) BIN_DIR=$2; shift 2 ;; --purge) PURGE=1; shift ;;
		-h|--help) echo "用法: ./uninstall.sh [--prefix DIR] [--etc DIR] [--state DIR] [--bin-dir DIR] [--purge]"; exit 0 ;;
		*) echo "未知选项: $1" >&2; exit 2 ;;
	esac
done
if command -v systemctl >/dev/null 2>&1 && [ -f /etc/systemd/system/server-autoops.service ]; then
	systemctl disable --now server-autoops mem-guard 2>/dev/null || true
	rm -f /etc/systemd/system/server-autoops.service /etc/systemd/system/mem-guard.service
	systemctl daemon-reload
fi
for svc in server-autoops mem-guard; do
	if [ -f "/etc/init.d/$svc" ] && grep -q "$PREFIX\|server-autoops" "/etc/init.d/$svc"; then
		"/etc/init.d/$svc" stop 2>/dev/null || true
		"/etc/init.d/$svc" disable 2>/dev/null || true
		rm -f "/etc/init.d/$svc"
	fi
done
rm -f "$BIN_DIR/server-autoops"
rm -rf "$PREFIX"
if [ "$PURGE" = 1 ]; then
	rm -rf "$ETC" "$STATE"
	echo "已删除程序、配置和数据。"
else
	echo "已删除程序和服务；保留了 $ETC 和 $STATE（加 --purge 一起删除）。"
fi
echo "智能体镜像不会自动删除：docker image rm local/server-autoops-agent:0.1.0"
