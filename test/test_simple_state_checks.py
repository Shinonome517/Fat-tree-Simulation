"""
Helpers for running simple state checks against the k=4 fat-tree topology.

The `run_sanity_checks` helper is kept in the test suite so that pytest runs
can exercise the same quick validations that were previously invoked directly
from the runtime script.
"""


def run_sanity_checks(edges, hosts):
    """Execute quick automated checks for bridge IP, L2, and L3 connectivity."""
    edge = edges[0][0]
    print('=== br_e00 address ===')
    print(edge.cmd('ip addr show br_e00'))
    print('=== Edge downlinks (expect no inet addr) ===')
    for h in range(2):
        print(edge.cmd(f'ip addr show dev e00-h{h}'))
    print('=== br_e00 member ports ===')
    print(edge.cmd('bridge link show br_e00'))

    h_local = hosts[0][0][0]
    print('=== Same-ToR L2 ping (h000 -> h001) ===')
    print(h_local.cmd('ping -c1 -W1 10.0.0.2'))
    print('=== h000 ARP table after ping ===')
    print(h_local.cmd('ip neigh show dev h000-eth0'))

    print('=== Cross-pod L3 ping (h000 -> h100) ===')
    print(h_local.cmd('ping -c1 -W1 10.1.0.1'))
    print('=== h000 route table snippet ===')
    print(h_local.cmd('ip route show default'))
