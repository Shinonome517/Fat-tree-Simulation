import os
from typing import Dict

import pytest
from mininet.link import TCLink
from mininet.net import Mininet

import mininet_fattree_k4 as fattree

if os.geteuid() != 0:
    pytest.skip("Mininet requires root privileges", allow_module_level=True)

K = int(os.environ.get("FATTREE_K", "4"))


@pytest.fixture(scope="session")
def fattree_net() -> Dict[str, object]:
    """
    Provision the fat-tree topology once per test session.

    The fixture mirrors the setup logic used by the runtime script so that all
    test modules work off the same Mininet instance.
    """
    assert K % 2 == 0 and 2 <= K <= 16, "FATTREE_K must be an even integer between 2 and 16"
    net = Mininet(link=TCLink, build=False)
    cores, aggs, edges, hosts = fattree.create_nodes_and_links(net, k=K)
    net.build()

    routers = cores + fattree.flatten(aggs) + fattree.flatten(edges)
    fattree.tune_sysctls(routers)
    fattree.setup_edge_tor(edges, k=K)
    fattree.assign_addresses(cores, aggs, edges, hosts, k=K)
    fattree.install_routes_ecmp(cores, aggs, edges, k=K)

    try:
        yield {
            "net": net,
            "cores": cores,
            "aggs": aggs,
            "edges": edges,
            "hosts": hosts,
            "k": K,
        }
    finally:
        net.stop()
