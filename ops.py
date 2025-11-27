"""
Runtime helpers for the Fat-Tree topology:
- RTT measurement
- CLI entry
- Headless keep-alive loop
"""

import re
import time
from typing import Dict, Optional

from mininet.cli import CLI
from mininet.net import Mininet

__all__ = ['measure_path_rtt', 'run_cli', 'headless_loop']

PING_SUMMARY_RE = re.compile(r'(?P<tx>\d+)\s+packets transmitted,\s+(?P<rx>\d+)\s+received')
PING_RTT_RE = re.compile(
    r'round-trip\s+min/avg/max/(?:stddev|mdev)\s*=\s*'
    r'(?P<min>[\d\.]+)/(?P<avg>[\d\.]+)/(?P<max>[\d\.]+)/(?P<mdev>[\d\.]+)\s*ms'
)


def measure_path_rtt(
    net: Mininet,
    src: str = 'h000',
    dst: str = 'h311',
    count: int = 10,
    interval: float = 0.2,
) -> Dict[str, Optional[float]]:
    """Execute ping from `src` to `dst` and parse RTT statistics."""
    host = net.get(src)
    dst_ip = net.get(dst).IP()
    # Apply a conservative deadline so the probe cannot block indefinitely.
    deadline = max(3, int(count * max(1.0, interval + 0.8)))
    cmd = f'ping -n -c {count} -i {interval} -w {deadline} {dst_ip}'
    output = host.cmd(cmd)
    result: Dict[str, Optional[float]] = {
        'command': cmd,
        'raw_output': output,
        'sent': None,
        'received': None,
        'packet_loss_pct': None,
        'min_rtt_ms': None,
        'avg_rtt_ms': None,
        'max_rtt_ms': None,
        'mdev_rtt_ms': None,
        'success': False,
    }

    summary_match = PING_SUMMARY_RE.search(output)
    if summary_match:
        sent = int(summary_match.group('tx'))
        received = int(summary_match.group('rx'))
        result['sent'] = sent
        result['received'] = received
        if sent:
            result['packet_loss_pct'] = round(((sent - received) / sent) * 100.0, 3)
        result['success'] = received > 0

    rtt_match = PING_RTT_RE.search(output)
    if rtt_match:
        result['min_rtt_ms'] = float(rtt_match.group('min'))
        result['avg_rtt_ms'] = float(rtt_match.group('avg'))
        result['max_rtt_ms'] = float(rtt_match.group('max'))
        result['mdev_rtt_ms'] = float(rtt_match.group('mdev'))

    return result


def run_cli(net: Mininet) -> None:
    """Drop into Mininet CLI with a few handy hints."""
    print('\nTopology is up. Quick sanity checks you can try:')
    print('- pingall')
    print('- On an edge (e00/e01/e10/..): ip route show; mtr -n 10.3.1.1 to watch ECMP')
    print('- iperf or iperf3 tests across different pods to exercise ECMP paths')
    CLI(net)


def headless_loop(net: Mininet) -> None:
    """Keep the topology alive until SIGINT."""
    print('Fat-Tree topology is running headless. Press Ctrl+C to stop.')
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print('\nCaught interrupt; stopping topology.')
