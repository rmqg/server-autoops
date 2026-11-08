#!/bin/sh
# Follow docs/INSTALL.md step by step inside a fresh Debian/Ubuntu container.
# Usage on a Docker host (dist/ must contain the tarball and SHA256SUMS):
#   docker run --rm --platform linux/arm64 -v "$PWD/dist:/dist:ro" -v "$PWD/scripts:/s:ro" debian:12 sh /s/container-validate.sh
set -eu
step() { printf '\n### %s\n' "$*"; }
step "系统信息"
. /etc/os-release && echo "$PRETTY_NAME"; uname -m; uname -r
step "0. 准备：安装 python3（容器里没有）"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null && apt-get install -y -qq python3 ca-certificates >/dev/null
python3 -c 'import sys, sqlite3; print(sys.version.split()[0], "sqlite", sqlite3.sqlite_version)'
step "1. 校验并解包"
mkdir -p /work && cp /dist/* /work/ && cd /work
sha256sum -c SHA256SUMS
tar xzf server-autoops-*.tar.gz && cd server-autoops-*/
step "2. 离线演示"
./bin/server-autoops demo | tail -3
step "3. 安装"
./install.sh 2>&1 | grep -E '^==>|错误' || true
command -v server-autoops
step "doctor（还没配 Docker、镜像和模型，预期有错误）"
server-autoops doctor || echo "doctor exit=$?"
step "6. 白名单校验"
cp /opt/server-autoops/examples/policy.minimal.json /etc/server-autoops/policy.json
server-autoops policy check /etc/server-autoops/policy.json
step "8. 告警命令与状态"
server-autoops alert --service app --fault-code WEB_DOWN --level failure --title t --summary s --no-push | head -c 200; echo
server-autoops status
step "单元测试（安装目录里没有 tests，用解包目录）"
python3 -I -m unittest discover -s tests -t . 2>&1 | tail -3
step "卸载"
/opt/server-autoops/uninstall.sh --purge
[ ! -e /opt/server-autoops ] && [ ! -e /usr/local/bin/server-autoops ] && echo "已卸载干净"
