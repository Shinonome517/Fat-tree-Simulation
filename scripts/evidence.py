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


def list_bridges() -> list[str]:
    code, out, _ = run(["ovs-vsctl", "list-br"])
    if code != 0:
        return []
    return [l.strip() for l in out.splitlines() if l.strip()]


def dump_groups(logdir: Path):
    (logdir / "evidence").mkdir(parents=True, exist_ok=True)
    brs = list_bridges()
    with open(logdir / "evidence" / "dump_groups.txt", "w") as f:
        for br in brs:
            code, out, err = run(["ovs-ofctl", "-O", "OpenFlow13", "dump-groups", br])
            f.write(f"# {br}\n")
            f.write(out)
            if code != 0:
                f.write(f"\n[error] {err}\n")
                
    with open(logdir / "evidence" / "dump_group_stats.txt", "w") as f:
        for br in brs:
            code, out, err = run(["ovs-ofctl", "-O", "OpenFlow13", "dump-group-stats", br])
            f.write(f"# {br}\n")
            f.write(out)
            if code != 0:
                f.write(f"\n[error] {err}\n")


def dump_ip_route(logdir: Path):
    # Dump global root NS routes (hosts are in namespaces; optional)
    (logdir / "evidence").mkdir(parents=True, exist_ok=True)
    code, out, err = run(["ip", "route", "show"])
    with open(logdir / "evidence" / "ip_route.txt", "w") as f:
        f.write(out)
        if code != 0:
            f.write(f"\n[error] {err}\n")


def trace_summary(logdir: Path):
    (logdir / "evidence").mkdir(parents=True, exist_ok=True)
    brs = [br for br in list_bridges() if br.startswith("br_e_") or br.startswith("br_a_")]
    rows = []
    # Sample a spread of 5-tuple values to test SELECT bucket diversity
    samples = [
        (1, "10.0.0.2", "10.1.1.2", 5001, 5201),
        (1, "10.0.0.2", "10.2.1.2", 5002, 5202),
        (2, "10.1.1.3", "10.3.0.2", 5003, 5203),
        (2, "10.2.1.2", "10.0.0.3", 5004, 5204),
        (1, "10.3.1.3", "10.1.0.2", 5005, 5205),
        (2, "10.0.1.2", "10.2.0.2", 5006, 5201),
        (1, "10.1.0.3", "10.3.1.2", 5007, 5207),
        (2, "10.2.0.3", "10.0.1.2", 5008, 5208),
    ]
    for br in brs:
        for in_port, src, dst, tps, tpd in samples:
            # Use TCP so that tp_src/tp_dst prerequisites are satisfied
            cmd = [
                "ovs-appctl", "ofproto/trace", br,
                f"in_port={in_port},tcp,nw_src={src},nw_dst={dst},tp_src={tps},tp_dst={tpd}"
            ]
            code, out, err = run(cmd, timeout=10)
            sel = ""
            for line in out.splitlines():
                if "select" in line and "bucket" in line:
                    sel = line.strip()
                    break
                if "selected bucket" in line:
                    sel = line.strip()
                    break
            rows.append((br, in_port, src, dst, tps, tpd, sel or err.strip()))

    with open(logdir / "evidence" / "trace_summary.tsv", "w") as f:
        f.write("bridge\tin_port\tsrc\tdst\ttp_src\ttp_dst\tselection\n")
        for r in rows:
            f.write("\t".join(str(x) for x in r) + "\n")


def collect_all(logdir: str | Path):
    logdir = Path(logdir)
    dump_ip_route(logdir)
    dump_groups(logdir)
    trace_summary(logdir)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--logdir", required=True, help="logs/<run>/ directory")
    args = ap.parse_args()
    collect_all(args.logdir)
