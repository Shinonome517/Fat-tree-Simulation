#!/usr/bin/env python3
from mininet.topo import Topo
from mininet.node import OVSSwitch
from mininet.link import TCLink


class FatTreeTopo(Topo):
    """
    Fat-Tree topology for k=4 by default.

    Naming/indices:
      - Core:  br_c_i_j      i,j in [0,1]
      - Agg:   br_a_p_i      p in [0..3], i in [0,1]
      - Edge:  br_e_p_i      p in [0..3], i in [0,1]
      - Host:  h_p_i_h       h in [1,2]

    Addressing:
      - Subnet S(p,i) = 10.p.i.0/24
      - Host IP: 10.p.i.(h+1) -> h=1 => .2, h=2 => .3

    Ports (explicit, deterministic):
      - Edge br_e_p_i: 1->h=1, 2->h=2, 3->a_p_0, 4->a_p_1
      - Agg  br_a_p_i: 1->e_p_0, 2->e_p_1, 3->c_0_i, 4->c_1_i
      - Core br_c_i_j: 1->a_0_j, 2->a_1_j, 3->a_2_j, 4->a_3_j

    DPID scheme (hex, 16 digits):
      - Edge:  0xE00000ppii  -> "000000E000ppii"
      - Agg:   0xA00000ppii  -> "000000A000ppii"
      - Core:  0xC0000000ij  -> "000000C00000ij"
    """

    def __init__(
        self,
        k: int = 4,
        bw_edge: float = 10.0,  # Gbps
        bw_agg: float = 10.0,
        bw_core: float = 10.0,
        delay: str = "100us",
        loss: int = 0,
        max_queue: int = 100,
        **_kwargs,
    ):
        assert k == 4, "This implementation currently targets k=4 (generalization later)."
        super().__init__()

        linkopt_edge = dict(bw=bw_edge, delay=delay, loss=loss, max_queue_size=max_queue, use_htb=True)
        linkopt_agg = dict(bw=bw_agg, delay=delay, loss=loss, max_queue_size=max_queue, use_htb=True)
        linkopt_core = dict(bw=bw_core, delay=delay, loss=loss, max_queue_size=max_queue, use_htb=True)

        # Create core switches (4)
        cores = {}
        for i in range(2):
            for j in range(2):
                name = f"br_c_{i}_{j}"
                dpid = self._dpid_core(i, j)
                cores[(i, j)] = self.addSwitch(
                    name,
                    cls=OVSSwitch,
                    protocols="OpenFlow13",
                    dpid=dpid,
                )

        # Create agg/edge switches (per pod) and hosts
        aggs = {}
        edges = {}
        hosts = {}

        for p in range(4):
            # Agg switches a_p_0, a_p_1
            for i in range(2):
                name = f"br_a_{p}_{i}"
                dpid = self._dpid_agg(p, i)
                aggs[(p, i)] = self.addSwitch(
                    name,
                    cls=OVSSwitch,
                    protocols="OpenFlow13",
                    dpid=dpid,
                )

            # Edge switches e_p_0, e_p_1
            for i in range(2):
                name = f"br_e_{p}_{i}"
                dpid = self._dpid_edge(p, i)
                edges[(p, i)] = self.addSwitch(
                    name,
                    cls=OVSSwitch,
                    protocols="OpenFlow13",
                    dpid=dpid,
                )

            # Hosts under each edge: h=1,2 with deterministic IPs
            for i in range(2):
                for h in (1, 2):
                    host_name = f"h_{p}_{i}_{h}"
                    ip = f"10.{p}.{i}.{h+1}/24"
                    hosts[(p, i, h)] = self.addHost(host_name, ip=ip)

        # Wire edge <-> hosts and edge <-> aggs
        for p in range(4):
            e0 = edges[(p, 0)]
            e1 = edges[(p, 1)]
            a0 = aggs[(p, 0)]
            a1 = aggs[(p, 1)]

            # Edge p,0: ports 1,2 -> hosts; 3->a0, 4->a1
            self.addLink(e0, hosts[(p, 0, 1)], port1=1, port2=1, cls=TCLink, **linkopt_edge)
            self.addLink(e0, hosts[(p, 0, 2)], port1=2, port2=1, cls=TCLink, **linkopt_edge)
            self.addLink(e0, a0, port1=3, port2=1, cls=TCLink, **linkopt_agg)
            self.addLink(e0, a1, port1=4, port2=1, cls=TCLink, **linkopt_agg)

            # Edge p,1: ports 1,2 -> hosts; 3->a0, 4->a1
            self.addLink(e1, hosts[(p, 1, 1)], port1=1, port2=1, cls=TCLink, **linkopt_edge)
            self.addLink(e1, hosts[(p, 1, 2)], port1=2, port2=1, cls=TCLink, **linkopt_edge)
            self.addLink(e1, a0, port1=3, port2=2, cls=TCLink, **linkopt_agg)
            self.addLink(e1, a1, port1=4, port2=2, cls=TCLink, **linkopt_agg)

        # Wire agg <-> cores
        for p in range(4):
            a0 = aggs[(p, 0)]
            a1 = aggs[(p, 1)]
            # a_p_0 uplinks: port3->c_0_0, port4->c_1_0
            self.addLink(a0, cores[(0, 0)], port1=3, port2=p + 1, cls=TCLink, **linkopt_core)
            self.addLink(a0, cores[(1, 0)], port1=4, port2=p + 1, cls=TCLink, **linkopt_core)
            # a_p_1 uplinks: port3->c_0_1, port4->c_1_1
            self.addLink(a1, cores[(0, 1)], port1=3, port2=p + 1, cls=TCLink, **linkopt_core)
            self.addLink(a1, cores[(1, 1)], port1=4, port2=p + 1, cls=TCLink, **linkopt_core)

    @staticmethod
    def _dpid_edge(p: int, i: int) -> str:
        # 000000E000ppii (pp and ii are two-digit hex or decimal packed)
        val = (0x000000E0000000) | (p << 8) | i
        return f"{val:016x}"

    @staticmethod
    def _dpid_agg(p: int, i: int) -> str:
        # 000000A000ppii
        val = (0x000000A0000000) | (p << 8) | i
        return f"{val:016x}"

    @staticmethod
    def _dpid_core(i: int, j: int) -> str:
        # 000000C00000ij
        val = (0x000000C0000000) | (i << 4) | j
        return f"{val:016x}"


topos = {
    # Enable: mn --custom topo/fattree.py --topo fattree
    "fattree": FatTreeTopo,
}
