#!/usr/bin/env python3
import os
import subprocess
import time
from pathlib import Path


def run(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError as e:
        return 127, "", str(e)
    except subprocess.TimeoutExpired as e:
        return 124, e.stdout or "", e.stderr or "timeout"


def write_file(dirpath: Path, name: str, content: str):
    (dirpath).mkdir(parents=True, exist_ok=True)
    with open(dirpath / name, "w") as f:
        f.write(content)


def collect_diags(root: Path):
    ddir = root / "diags"
    ddir.mkdir(parents=True, exist_ok=True)

    # System
    for name, cmd in [
        ("uname.txt", ["uname", "-a"]),
        ("ip_link.txt", ["ip", "-details", "link", "show"]),
        ("ip_addr.txt", ["ip", "addr", "show"]),
        ("route.txt", ["ip", "route", "show"]),
        ("ss_tcp.txt", ["ss", "-tanpo"]),
        ("ps.txt", ["ps", "axf"]),
        ("osken_check.txt", ["bash", "-lc", "command -v osken-manager && osken-manager --version || echo 'osken-manager: not found'; python3 -c 'import importlib,sys;\n\nprint(getattr(importlib.import_module(\"os_ken\"),\"__version__\",\"os_ken-import-ok\"))' 2>&1 || true"]),
    ]:
        code, out, err = run(cmd)
        write_file(ddir, name, out + ("\n[error] " + err if code != 0 else ""))

    # OVS
    code, out, err = run(["ovs-vsctl", "show"])
    write_file(ddir, "ovs_vsctl_show.txt", out + ("\n[error] " + err if code != 0 else ""))

    # Per-bridge details (if any)
    code, out, _ = run(["ovs-vsctl", "list-br"])
    brs = [l.strip() for l in out.splitlines() if l.strip()] if code == 0 else []
    for br in brs:
        for name, cmd in [
            (f"{br}_show.txt", ["ovs-ofctl", "-O", "OpenFlow13", "show", br]),
            (f"{br}_flows.txt", ["ovs-ofctl", "-O", "OpenFlow13", "dump-flows", br]),
            (f"{br}_groups.txt", ["ovs-ofctl", "-O", "OpenFlow13", "dump-groups", br]),
            (f"{br}_group_stats.txt", ["ovs-ofctl", "-O", "OpenFlow13", "dump-group-stats", br]),
            (f"{br}_ports.txt", ["ovs-ofctl", "-O", "OpenFlow13", "dump-ports", br]),
        ]:
            code, out, err = run(cmd)
            write_file(ddir, name, out + ("\n[error] " + err if code != 0 else ""))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", action="store_true", help="collect immediately into logs/<ts>/diags")
    ap.add_argument("--logdir", default=None, help="If provided, collect into this existing logs dir")
    args = ap.parse_args()

    if args.logdir:
        root = Path(args.logdir)
    else:
        ts = time.strftime("%Y%m%d-%H%M%S")
        root = Path("logs") / ts
    root.mkdir(parents=True, exist_ok=True)
    collect_diags(root)
    print(f"[diags] Collected diagnostics in: {root / 'diags'}")
