import os
from typing import Dict

import pytest
from mininet.link import TCLink
from mininet.net import Mininet

import mininet_fattree_k4 as fattree

if os.geteuid() != 0:
    pytest.skip("Mininet requires root privileges", allow_module_level=True)


@pytest.fixture(scope="session")
def fattree_net() -> Dict[str, object]:
    """
    Provision the k=4 fat-tree topology once per test session.

    The fixture mirrors the setup logic used by the runtime script so that all
    test modules work off the same Mininet instance.
    """
    net = Mininet(link=TCLink, build=False)
    cores, aggs, edges, hosts = fattree.create_nodes_and_links(net)
    net.build()

    routers = cores + fattree.flatten(aggs) + fattree.flatten(edges)
    fattree.tune_sysctls(routers)
    fattree.setup_edge_tor(edges)
    fattree.assign_addresses(cores, aggs, edges, hosts)
    fattree.install_routes_ecmp(cores, aggs, edges)

    try:
        yield {
            "net": net,
            "cores": cores,
            "aggs": aggs,
            "edges": edges,
            "hosts": hosts,
        }
    finally:
        net.stop()
