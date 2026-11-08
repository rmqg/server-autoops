#!/usr/bin/env python3
"""Build a reproducible release tarball and SHA256SUMS (used locally and by CI).

  python3 scripts/build_release.py [--out dist] [--tag v0.1.0]

Files come from `git ls-files` (so only committed files ship); the archive has
sorted entries, owner root:root, normalised modes and a fixed mtime
(SOURCE_DATE_EPOCH, else the HEAD commit time), so the same commit gives the
same bytes on any machine.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "server-autoops"


def version() -> str:
    text = (ROOT / "server_autoops" / "__init__.py").read_text()
    return re.search(r'__version__ = "([^"]+)"', text).group(1)


def files() -> dict[str, int]:
    """Tracked files and their git modes (git, not the file system, decides 755 vs 644:
    network mounts such as SMB often report every file as executable)."""
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-s", "-z"], capture_output=True, check=True).stdout
    entries = {}
    for record in out.decode().split("\0"):
        if not record:
            continue
        meta, name = record.split("\t", 1)
        entries[name] = 0o755 if meta.split()[0] == "100755" else 0o644
    if not entries:
        sys.exit("no tracked files; git add first")
    return dict(sorted(entries.items()))


def epoch() -> int:
    if os.environ.get("SOURCE_DATE_EPOCH"):
        return int(os.environ["SOURCE_DATE_EPOCH"])
    out = subprocess.run(["git", "-C", str(ROOT), "log", "-1", "--format=%ct"], capture_output=True, text=True, check=True)
    return int(out.stdout.strip())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "dist"))
    parser.add_argument("--tag", default=os.environ.get("GITHUB_REF_NAME", ""))
    args = parser.parse_args()
    ver = version()
    if args.tag and args.tag.startswith("v") and args.tag != f"v{ver}":
        sys.exit(f"tag {args.tag} does not match server_autoops.__version__ {ver}")
    mtime = epoch()
    prefix = f"{NAME}-{ver}"
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        dirs: set[str] = set()
        for name, mode in files().items():
            path = ROOT / name
            if not path.is_file():
                continue
            parts = Path(name).parts
            for i in range(1, len(parts)):
                d = "/".join(parts[:i])
                if d not in dirs:
                    dirs.add(d)
                    info = tarfile.TarInfo(f"{prefix}/{d}")
                    info.type, info.mode, info.mtime = tarfile.DIRTYPE, 0o755, mtime
                    info.uid = info.gid = 0
                    info.uname = info.gname = "root"
                    tar.addfile(info)
            data = path.read_bytes()
            info = tarfile.TarInfo(f"{prefix}/{name}")
            info.size, info.mtime = len(data), mtime
            info.mode = mode
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            tar.addfile(info, io.BytesIO(data))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    archive = out / f"{prefix}.tar.gz"
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0, compresslevel=9) as gz:
        gz.write(raw.getvalue())
    archive.write_bytes(buf.getvalue())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (out / "SHA256SUMS").write_text(f"{digest}  {archive.name}\n")
    print(f"{archive}\n{digest}  {archive.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
