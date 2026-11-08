#!/bin/sh
# server-autoops installer. POSIX sh; works on Debian/Ubuntu (systemd) and OpenWrt (procd).
#   sudo ./install.sh                     install, enable and start the service
#   sudo ./install.sh --no-start          install only
#   ./install.sh --help
set -eu

PREFIX=/opt/server-autoops
ETC=/etc/server-autoops
STATE=/var/lib/server-autoops
BIN_DIR=/usr/local/bin
SERVICE=auto
START=1
MEM_GUARD=0

usage() {
	cat <<USAGE
用法: ./install.sh [选项]
  --prefix DIR        程序目录（默认 $PREFIX）
  --etc DIR           配置目录（默认 $ETC）
  --state DIR         数据目录：队列、运行记录、告警流（默认 $STATE）
  --bin-dir DIR       命令链接放在哪里（默认 $BIN_DIR）
  --service KIND      auto | systemd | procd | none（默认 auto 自动识别）
  --no-start          只安装，不启用、不启动服务
  --with-mem-guard    同时安装可选的内存观察 mem-guard 服务
已有的 config.json、policy.json 不会被覆盖；升级时重新运行即可。
USAGE
}

while [ $# -gt 0 ]; do
	case "$1" in
		--prefix) PREFIX=$2; shift 2 ;;
		--etc) ETC=$2; shift 2 ;;
		--state) STATE=$2; shift 2 ;;
		--bin-dir) BIN_DIR=$2; shift 2 ;;
		--service) SERVICE=$2; shift 2 ;;
		--no-start) START=0; shift ;;
		--with-mem-guard) MEM_GUARD=1; shift ;;
		-h|--help) usage; exit 0 ;;
		*) echo "未知选项: $1" >&2; usage >&2; exit 2 ;;
	esac
done

SRC=$(cd "$(dirname "$0")" && pwd -P)
say() { printf '==> %s\n' "$*"; }
die() { printf '错误: %s\n' "$*" >&2; exit 1; }

PY=$(command -v python3 || true)
[ -n "$PY" ] || die "找不到 python3（需要 3.10 或更新）。Debian/Ubuntu: apt install python3；OpenWrt: opkg install python3"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || die "Python 版本太旧：$("$PY" -V 2>&1)，需要 3.10+"
"$PY" -c 'import sqlite3, zoneinfo' 2>/dev/null || die "Python 缺少 sqlite3 模块（OpenWrt: opkg install python3-sqlite3）"
[ "$(id -u)" = 0 ] || say "提示：不是 root 运行，只适合装到自己有写权限的目录做试验"

if [ "$SERVICE" = auto ]; then
	if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then SERVICE=systemd
	elif [ -x /sbin/procd ] && [ -f /etc/rc.common ]; then SERVICE=procd
	else SERVICE=none; fi
fi
case "$SERVICE" in systemd|procd|none) ;; *) die "--service 只能是 auto、systemd、procd、none" ;; esac

say "安装程序到 $PREFIX"
mkdir -p "$PREFIX"
rm -rf "$PREFIX/server_autoops"
for item in server_autoops bin plugin agent-image schema examples contrib docs README.md LICENSE THIRD_PARTY_NOTICES.md CHANGELOG.md SECURITY.md install.sh uninstall.sh; do
	[ -e "$SRC/$item" ] || continue
	[ "$SRC" = "$PREFIX" ] && continue
	rm -rf "${PREFIX:?}/$item"
	cp -R "$SRC/$item" "$PREFIX/$item"
done
find "$PREFIX" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
chmod 755 "$PREFIX/bin/server-autoops" "$PREFIX/contrib/mem-guard/mem-guard.sh"
chmod -R go-w "$PREFIX"

say "命令链接 $BIN_DIR/server-autoops"
mkdir -p "$BIN_DIR"
ln -sf "$PREFIX/bin/server-autoops" "$BIN_DIR/server-autoops"
BIN="$BIN_DIR/server-autoops"

say "配置目录 $ETC，数据目录 $STATE"
mkdir -p "$ETC" "$STATE/state" "$STATE/runs"
chmod 755 "$ETC"
chmod 700 "$STATE" "$STATE/state" "$STATE/runs"
[ -e "$STATE/alert-stream.jsonl" ] || { : > "$STATE/alert-stream.jsonl"; chmod 600 "$STATE/alert-stream.jsonl"; }

if [ ! -e "$ETC/config.json" ]; then
	"$PY" - "$PREFIX/examples/config.example.json" "$ETC/config.json" "$ETC" "$STATE" "$PREFIX" <<'PYEOF'
import json, sys
src, dst, etc, state, prefix = sys.argv[1:]
c = json.load(open(src))
c.update(root=state, stream=f"{state}/alert-stream.jsonl", state_db=f"{state}/state/queue.sqlite3",
         runs_dir=f"{state}/runs", policy=f"{etc}/policy.json")
c["notify"] = {"kind": "none"}          # turned on after the Bark URL file exists (docs/CONFIGURATION.md)
c["agent"]["plugin_dir"] = f"{prefix}/plugin"
c["agent"]["model"]["env_file"] = f"{etc}/model.env"
open(dst, "w").write(json.dumps(c, ensure_ascii=False, indent=2) + "\n")
PYEOF
	chmod 644 "$ETC/config.json"
	say "已生成 $ETC/config.json（通知先关闭，模型地址是占位符）"
else
	say "保留已有的 $ETC/config.json"
fi
if [ ! -e "$ETC/policy.json" ]; then
	printf '{\n  "version": 1,\n  "description": "空白名单：什么都不会被自动处理。参照 %s/examples/policy.example.json 填写",\n  "services": {},\n  "faults": {}\n}\n' "$PREFIX" > "$ETC/policy.json"
	chmod 644 "$ETC/policy.json"
	say "已生成空白 $ETC/policy.json（没有任何授权）"
else
	say "保留已有的 $ETC/policy.json"
fi
[ -e "$ETC/model.env" ] || { cp "$PREFIX/examples/model.env.example" "$ETC/model.env"; chmod 600 "$ETC/model.env"; }

render() { sed -e "s#@ETC@#$ETC#g" -e "s#@BIN@#$BIN#g" -e "s#@PREFIX@#$PREFIX#g" "$1" > "$2"; }
case "$SERVICE" in
	systemd)
		render "$PREFIX/contrib/systemd/server-autoops.service" /etc/systemd/system/server-autoops.service
		[ "$MEM_GUARD" = 1 ] && render "$PREFIX/contrib/systemd/mem-guard.service" /etc/systemd/system/mem-guard.service
		systemctl daemon-reload
		if [ "$START" = 1 ]; then
			systemctl enable --now server-autoops
			[ "$MEM_GUARD" = 1 ] && systemctl enable --now mem-guard
		fi
		;;
	procd)
		render "$PREFIX/contrib/openwrt/server-autoops.init" /etc/init.d/server-autoops
		chmod 755 /etc/init.d/server-autoops
		if [ "$MEM_GUARD" = 1 ]; then
			render "$PREFIX/contrib/openwrt/mem-guard.init" /etc/init.d/mem-guard
			chmod 755 /etc/init.d/mem-guard
		fi
		grep -qx "$ETC/" /etc/sysupgrade.conf 2>/dev/null || echo "$ETC/" >> /etc/sysupgrade.conf
		grep -qx "/etc/init.d/server-autoops" /etc/sysupgrade.conf 2>/dev/null || echo "/etc/init.d/server-autoops" >> /etc/sysupgrade.conf
		if [ "$START" = 1 ]; then
			/etc/init.d/server-autoops enable && /etc/init.d/server-autoops start
			[ "$MEM_GUARD" = 1 ] && /etc/init.d/mem-guard enable && /etc/init.d/mem-guard start
		fi
		;;
	none)
		say "没有安装系统服务（--service none 或没识别到 systemd/procd）；前台运行：$BIN run"
		;;
esac

say "运行自检（有错误是正常的：还没配模型、镜像和白名单）"
"$BIN" doctor --config "$ETC/config.json" || true
cat <<NEXT

安装完成。接下来：
  1. 先试离线演示：            $BIN demo
  2. 写白名单：                 编辑 $ETC/policy.json，然后 $BIN policy check $ETC/policy.json
  3. 填模型地址和密钥：         编辑 $ETC/config.json 的 agent.model，以及 $ETC/model.env（权限 600）
  4. 构建智能体镜像：           docker build -t local/server-autoops-agent:0.1.0 $PREFIX/agent-image
  5. 配通知（可选）：           见 $PREFIX/docs/CONFIGURATION.md 的 notify 一节，然后 $BIN notify-test
  6. 再自检一次：               $BIN doctor
NEXT
