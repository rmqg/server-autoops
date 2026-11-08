#!/bin/sh
# Same entry point for local builds and CI: tests, offline demo, then the reproducible tarball.
#   scripts/build-release.sh            -> dist/server-autoops-<version>.tar.gz + dist/SHA256SUMS
set -eu
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || { echo "需要 Python 3.10+（当前 $("$PY" -V 2>&1)）；可以用 PYTHON=/path/to/python3.12 scripts/build-release.sh" >&2; exit 1; }
"$PY" -I -m unittest discover -s tests -t . >/dev/null 2>&1 || "$PY" -m unittest discover -s tests -t .
"$PY" -I bin/server-autoops.py policy check examples/policy.example.json >/dev/null
"$PY" -I bin/server-autoops.py demo >/dev/null
"$PY" scripts/build_release.py "$@"
