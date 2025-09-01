#!/usr/bin/env python3
import argparse
import os
import signal
import subprocess
import sys
import time
import shutil
import shlex
from pathlib import Path

# Ensure repository root is on sys.path so we can import 'topo' and 'controller'
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mininet.net import Mininet
from mininet.node import RemoteController
from mininet.log import setLogLevel

from topo.fattree import FatTreeTopo


def run_cmd(cmd: list[str], cwd: str | None = None, timeout: int = 30) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError as e:
        return 127, "", str(e)
    except subprocess.TimeoutExpired as e:
        return 124, e.stdout or "", e.stderr or "timeout"


def ensure_ovs_running() -> None:
    """Ensure Open vSwitch daemons are running. Try multiple strategies.
    Raises RuntimeError if OVS cannot be contacted after attempts.
    """
    # Quick check
    code, _, _ = run_cmd(["ovs-vsctl", "--no-wait", "show"], timeout=5)
    if code == 0:
        return

    # Try to load kernel module (best effort)
    run_cmd(["modprobe", "openvswitch"], timeout=5)

    # Try systemd
    if shutil.which("systemctl"):
        run_cmd(["systemctl", "start", "openvswitch-switch"], timeout=10)
        time.sleep(0.5)
        code, out, _ = run_cmd(["systemctl", "is-active", "openvswitch-switch"], timeout=5)
        if code == 0 and out.strip() == "active":
            # Re-check ovs-vsctl
            code, _, _ = run_cmd(["ovs-vsctl", "--no-wait", "show"], timeout=5)
            if code == 0:
                return

    # Try service/init.d
    if shutil.which("service"):
        run_cmd(["service", "openvswitch-switch", "start"], timeout=10)
        time.sleep(0.5)
    else:
        if Path("/etc/init.d/openvswitch-switch").exists():
            run_cmd(["/etc/init.d/openvswitch-switch", "start"], timeout=10)

    # Try ovs-ctl if available
    ovs_ctl = shutil.which("ovs-ctl") or \
              ("/usr/share/openvswitch/scripts/ovs-ctl" if Path("/usr/share/openvswitch/scripts/ovs-ctl").exists() else None)
    if ovs_ctl:
        run_cmd([ovs_ctl, "start"], timeout=10)

    # Final check
    time.sleep(1.0)
    code, _, err = run_cmd(["ovs-vsctl", "--no-wait", "show"], timeout=5)
    if code != 0:
        raise RuntimeError("[T1] Open vSwitch is not running (ovs-vsctl show failed). Please start 'openvswitch-switch'.")


def _get_of_port() -> str:
    return os.environ.get("OSKEN_OF_PORT") or os.environ.get("RYU_OF_PORT") or "6653"


def start_controller(ecmp: bool, logdir: Path) -> subprocess.Popen:
    app_rel = "controller/ryu_ecmp.py" if ecmp else "controller/ryu_l2.py"
    app = str(ROOT / app_rel)
    of_port = _get_of_port()
    logfile = open(logdir / ("osken_ecmp.log" if ecmp else "osken_l2.log"), "w")
    env = os.environ.copy()
    env.setdefault("OSKEN_LOGLEVEL", env.get("RYU_LOGLEVEL", "INFO"))

    # User override: prefer OSKEN_CMD, fallback to RYU_CMD for compatibility
    override = os.environ.get("OSKEN_CMD") or os.environ.get("RYU_CMD")
    if override:
        base = shlex.split(override)
        cmd = base + ["--ofp-tcp-listen-port", of_port, app]
    else:
        # Prefer osken-manager if on PATH, fallback to python -m os_ken.cmd.manager
        osken_bin = shutil.which("osken-manager")
        if osken_bin:
            cmd = [osken_bin, "--ofp-tcp-listen-port", of_port, app]
        else:
            # Check if the current Python can import os_ken; if not, fail early with guidance
            code, _, _ = run_cmd([sys.executable, "-c", "import os_ken"], timeout=5)
            if code != 0:
                raise RuntimeError(
                    "[T1] OS-Ken is not installed. Install via 'pip install os-ken' or 'apt-get install python3-os-ken', "
                    "or set OSKEN_CMD to a runnable osken-manager path."
                )
            cmd = [sys.executable, "-m", "os_ken.cmd.manager", "--ofp-tcp-listen-port", of_port, app]

    proc = subprocess.Popen(cmd, stdout=logfile, stderr=subprocess.STDOUT, env=env, cwd=str(ROOT))
    time.sleep(1.0)
    return proc


def stop_controller(proc: subprocess.Popen):
    if not proc:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception:
        pass


def build_net(args) -> Mininet:
    topo = FatTreeTopo(k=4, bw_edge=args.bw, bw_agg=args.bw, bw_core=args.bw, delay=args.delay, loss=args.loss, max_queue=args.max_queue)
    cport = int(_get_of_port())
    net = Mininet(
        topo=topo,
        controller=lambda name: RemoteController(name, ip="127.0.0.1", port=cport),
        autoSetMacs=True,
        autoStaticArp=True,
    )
    return net


def wait_for_controller_listen(host: str = "127.0.0.1", port: int = 6653, timeout: float = 10.0) -> None:
    import socket
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError as e:
            last_err = e
            time.sleep(0.3)
    raise RuntimeError(f"[T1] OS-Ken not listening on {host}:{port} ({last_err})")


def configure_hosts_for_l2(net: Mininet):
    """Add an on-link route so hosts ARP for all 10.0.0.0/8 peers (L2 dataplane, no GW)."""
    for h in net.hosts:
        intf = h.defaultIntf()
        if not intf:
            continue
        # Make entire 10/8 on-link to trigger ARP instead of requiring a gateway
        h.cmd(f"ip route replace 10.0.0.0/8 dev {intf} scope link || true")


def wait_for_switch_connections(expected: int, timeout: float = 10.0) -> None:
    """Wait until OVS shows expected controller connections with is_connected=true."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, out, _ = run_cmd([
            "ovs-vsctl", "--format=csv", "--no-heading", "--columns=is_connected",
            "find", "controller", "target=\"tcp:127.0.0.1:6653\""
        ], timeout=5)
        if code == 0:
            trues = sum(1 for line in out.splitlines() if line.strip().lower() == "true")
            if trues >= expected:
                return
        time.sleep(0.5)
    # Not fatal, but warn
    print(f"[T1] Warning: only some switches connected to controller (expected {expected})")


def representative_pairs():
    # (src_host_name, dst_host_name)
    return [
        ("h_0_0_1", "h_1_1_1"),  # pod0 -> pod1
        ("h_0_0_2", "h_2_1_2"),  # pod0 -> pod2
    ]


def run_ping_tests(net: Mininet, logdir: Path, count: int) -> bool:
    ok = True
    for s, d in representative_pairs():
        hs, hd = net.get(s), net.get(d)
        out = hs.cmd(f"ping -c {count} -i 0.2 -W 1 {hd.IP()}")
        with open(logdir / f"ping_{s}_to_{d}.log", "w") as f:
            f.write(out)
        if " 0% packet loss" not in out and ", 0% packet loss" not in out:
            ok = False
    return ok


def run_iperf_tests(net: Mininet, logdir: Path, duration: int) -> bool:
    ok = True
    for idx, (s, d) in enumerate(representative_pairs(), start=1):
        hs, hd = net.get(s), net.get(d)
        # Start server in background for a single test
        hd.cmd("pkill -f iperf3; true")
        hs.cmd("pkill -f iperf3; true")
        hd.cmd("iperf3 -s -1 >/dev/null 2>&1 &")
        time.sleep(0.5)
        out = hs.cmd(f"iperf3 -c {hd.IP()} -t {duration} -J")
        with open(logdir / f"iperf3_{idx}_{s}_to_{d}.json", "w") as f:
            f.write(out)
        if '"end"' not in out:
            ok = False
        time.sleep(0.5)
    return ok


def main():
    parser = argparse.ArgumentParser(description="Run k=4 Fat-Tree with ECMP and basic tests")
    parser.add_argument("--ecmp", choices=["on", "off"], default="on")
    parser.add_argument("--ping-count", type=int, default=3)
    parser.add_argument("--iperf-time", type=int, default=30)
    parser.add_argument("--log-root", default="logs")
    parser.add_argument("--bw", type=float, default=10.0)
    parser.add_argument("--delay", default="100us")
    parser.add_argument("--loss", type=int, default=0)
    parser.add_argument("--max-queue", type=int, default=100)
    parser.add_argument("--no-clean", action="store_true", help="do not run mn -c before/after")
    args = parser.parse_args()

    setLogLevel("warning")

    ts = time.strftime("%Y%m%d-%H%M%S")
    logdir = Path(args.log_root) / ts
    (logdir / "evidence").mkdir(parents=True, exist_ok=True)

    if not args.no_clean:
        run_cmd(["mn", "-c"])  # best-effort

    proc = None
    net = None
    try:
        # Ensure OVS is up before creating Mininet switches
        ensure_ovs_running()

        ecmp = args.ecmp == "on"
        proc = start_controller(ecmp, logdir)
        # Ensure OS-Ken listens before switches attempt to connect
        of_port = int(_get_of_port())
        wait_for_controller_listen("127.0.0.1", of_port, timeout=10.0)
        net = build_net(args)
        net.start()
        time.sleep(2.0)  # allow switches to connect

        # Ensure all hosts treat 10/8 as on-link (L2 dataplane, no GW)
        configure_hosts_for_l2(net)

        # Give controller time to program flows
        wait_for_switch_connections(expected=len(net.switches), timeout=12.0)
        time.sleep(1.0)

        # Confirm OVS protocol v1.3 forced (best-effort check)
        code, out, err = run_cmd(["ovs-vsctl", "list-br"])
        if code == 0:
            for br in [l.strip() for l in out.splitlines() if l.strip()]:
                run_cmd(["ovs-vsctl", "set", "bridge", br, "protocols=OpenFlow13"])  # idempotent

        # Basic tests
        ping_ok = run_ping_tests(net, logdir, args.ping_count)
        iperf_ok = run_iperf_tests(net, logdir, args.iperf_time)

        # Evidence collection
        from scripts.evidence import collect_all
        collect_all(logdir)

        if not (ping_ok and iperf_ok):
            raise RuntimeError("[T1] Ping or iperf3 failed; check logs and diags")

        print(f"[T1] Success. Logs: {logdir}")
        return 0

    except Exception as e:
        # Diags on failure
        print(f"[T1] ERROR: {e}")
        try:
            from scripts.diags import collect_diags
            collect_diags(logdir)
        except Exception as de:
            print(f"[T1] diags failed: {de}")
        print("Try: python3 scripts/diags.py --now")
        return 1
    finally:
        if net is not None:
            try:
                net.stop()
            except Exception:
                pass
        stop_controller(proc)
        if not args.no_clean:
            run_cmd(["mn", "-c"])  # best-effort


if __name__ == "__main__":
    sys.exit(main())
