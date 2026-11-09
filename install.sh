#!/bin/sh
# server-autoops installer. POSIX sh; works on Debian/Ubuntu (systemd) and OpenWrt (procd).
#   sudo ./install.sh                     check the machine, install, ask a few questions, start the service
#   sudo ./install.sh --non-interactive   no questions (keeps existing config; new installs get placeholders)
#   sudo ./install.sh --config answers.env  answer the questions from a file (examples/install.env.example)
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
WAIT=""
ASK=auto
ANSWERS=""

usage() {
	cat <<USAGE
用法: sudo ./install.sh [选项]
  （不带选项：先检查环境，再一步步问你要不要接模型、Bark 通知地址，最后自检）
  --non-interactive   不提问。已有配置不变；新装时模型是占位符、通知关闭
  --config FILE       从答案文件读取向导的回答，不提问（格式见 examples/install.env.example）
  --prefix DIR        程序目录（默认 $PREFIX）
  --etc DIR           配置目录（默认 $ETC）
  --state DIR         数据目录：队列、运行记录、告警流（默认 $STATE）
  --bin-dir DIR       命令链接放在哪里（默认 $BIN_DIR）
  --service KIND      auto | systemd | procd | none（默认 auto 自动识别）
  --no-start          只安装，不启用、不启动服务
  --with-mem-guard    同时安装可选的内存观察 mem-guard 服务
  --wait-path PATH    procd：开机时等这个路径出现再启动（可多次，例如第二套 dockerd 的 socket、数据盘上的配置）
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
		--wait-path) WAIT="$WAIT || [ ! -e $2 ]"; shift 2 ;;
		--non-interactive) ASK=no; shift ;;
		--config|--answers) ANSWERS=$2; shift 2 ;;
		-h|--help) usage; exit 0 ;;
		*) echo "未知选项: $1" >&2; usage >&2; exit 2 ;;
	esac
done

SRC=$(cd "$(dirname "$0")" && pwd -P)
say() { printf '==> %s\n' "$*"; }
die() { printf '错误: %s\n' "$*" >&2; exit 1; }

PY=$(command -v python3 || true)
[ -n "$PY" ] || die "找不到 python3（需要 3.10 或更新）。Debian/Ubuntu: apt-get update && apt-get install -y python3；OpenWrt: opkg update && opkg install python3 python3-sqlite3"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || die "Python 版本太旧：$("$PY" -V 2>&1)，需要 3.10+（Debian 12、Ubuntu 22.04 及更新的系统自带的就够）"
if [ -n "$ANSWERS" ]; then
	[ -f "$ANSWERS" ] || die "找不到答案文件 $ANSWERS"
	ANSWERS=$(cd "$(dirname "$ANSWERS")" && pwd -P)/$(basename "$ANSWERS")
	ASK=no
fi
if [ "$ASK" = auto ]; then
	if [ -t 0 ] && [ -t 1 ]; then ASK=yes; else ASK=no; fi
fi
"$PY" -I "$SRC/bin/server-autoops.py" setup --check-only --state "$STATE" || exit 1
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
# Services call the program by its install path, so a lost /usr/bin link (e.g. after a firmware upgrade) cannot break them.
BIN="$PREFIX/bin/server-autoops"

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
try:                                     # minimal systems (OpenWrt) often have no tzdata
    import zoneinfo; zoneinfo.ZoneInfo(c["timezone"])
except Exception:
    import time
    off = time.strftime("%z")            # e.g. +0800
    c["timezone"] = f"UTC{off[0]}{off[1:3]}:{off[3:5]}" if len(off) == 5 else "UTC"
    print(f"==> 本机没有 tzdata，timezone 改用 {c['timezone']}")
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

if [ -n "$ANSWERS" ]; then
	"$BIN" setup --config "$ETC/config.json" --answers "$ANSWERS" || die "答案文件有问题，配置没有改动；改好后重新运行，或者 sudo $BIN setup 手动回答"
elif [ "$ASK" = yes ]; then
	"$BIN" setup --config "$ETC/config.json" || say "向导没有完成，配置保持原样；之后可以运行 sudo $BIN setup 重新来"
elif [ ! -t 0 ] && [ "$ASK" = no ]; then
	say "没有终端，跳过提问（配置保持原样，新装时模型是占位符）。要自动回答：./install.sh --config 答案文件，格式见 $PREFIX/examples/install.env.example"
fi

render() { sed -e "s#@ETC@#$ETC#g" -e "s#@BIN@#$BIN#g" -e "s#@PREFIX@#$PREFIX#g" -e "s#@WAIT@#$WAIT#g" "$1" > "$2"; }
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
		# Only config under /etc needs keeping across firmware upgrades; never put a data-disk directory
		# (it holds bark.url) into sysupgrade.conf, which also feeds LuCI's downloadable config backup.
		case "$ETC" in /etc/*) grep -qx "$ETC/" /etc/sysupgrade.conf 2>/dev/null || echo "$ETC/" >> /etc/sysupgrade.conf ;; esac
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

[ "$START" = 1 ] && [ "$SERVICE" != none ] && sleep 3   # let the service create its state database first
say "运行自检（下面的「提醒」大多是正常的，每个错误后面都写着修复命令）"
"$BIN" doctor --config "$ETC/config.json" || true
CMD=server-autoops
[ "$BIN_DIR/server-autoops" -ef "$(command -v server-autoops 2>/dev/null || echo /nonexistent)" ] || CMD=$BIN
[ "$ETC" = /etc/server-autoops ] || CMD="AUTOOPS_CONFIG=$ETC/config.json $CMD"
KIND=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("agent", {}).get("kind", "dsh"))' "$ETC/config.json" 2>/dev/null || echo dsh)
echo
say "安装完成。下一步做什么（详细的图文步骤见 $PREFIX/docs/QUICKSTART.md）："
case "$KIND" in
	mock) cat <<NEXT
  你选的是「入门演练」。接着做 docs/QUICKSTART.md 的第 7 步「第一次自动修复」（用 root 运行）：
  1. 启动演练网站：     docker run -d --name quickstart-web -p 127.0.0.1:18080:80 nginx:alpine
  2. 把它停掉（制造故障）并报警：
                        docker stop quickstart-web
                        $CMD alert --service quickstart-web --fault-code WEB_DOWN --level failure --title 网站挂了 --summary 演练
  3. 等半分钟看结果：   $CMD status    和    docker ps
  4. 接真实模型：       sudo $CMD setup   （选「接真实模型」）
NEXT
	;;
	dsh) cat <<NEXT
  1. 构建智能体镜像（几百 MB）：  docker build -t local/server-autoops-agent:0.1.0 $PREFIX/agent-image
  2. 写白名单：                   编辑 $ETC/policy.json，然后 $CMD policy check $ETC/policy.json（写法见 docs/POLICY.md）
  3. 接看门狗：                   见 docs/INSTALL.md 第 8 步
  4. 再自检一次：                 sudo $CMD doctor
NEXT
	;;
	*) cat <<NEXT
  1. 先试离线演示：               $CMD demo
  2. 想让它自动修：               sudo $CMD setup   （选「入门演练」或「接真实模型」）
  3. 再自检一次：                 sudo $CMD doctor
NEXT
	;;
esac
