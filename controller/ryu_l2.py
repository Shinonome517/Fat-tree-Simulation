#!/usr/bin/env python3
# OS-Ken L2-like controller (no ECMP): proactive static IP flows to avoid loops.
# OpenFlow 1.3. Matches IPv4 and directs along a deterministic single path.

from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from os_ken.ofproto import ofproto_v1_3
from os_ken.lib.packet import packet, ethernet, arp, ipv4


def parse_edge_from_dpid(dpid: int):
    # Accept both 0x000000E000ppii and 0xE000ppii
    if ((dpid >> 40) & 0xFF) == 0xE0 or ((dpid >> 24) & 0xFF) == 0xE0:
        p = (dpid >> 8) & 0xFF
        i = dpid & 0xFF
        return p, i
    return None


def parse_agg_from_dpid(dpid: int):
    # Accept both 0x000000A000ppii and 0xA000ppii
    if ((dpid >> 40) & 0xFF) == 0xA0 or ((dpid >> 24) & 0xFF) == 0xA0:
        p = (dpid >> 8) & 0xFF
        i = dpid & 0xFF
        return p, i
    return None


def parse_core_from_dpid(dpid: int):
    # Accept both 0x000000C00000ij and 0xC00000ij
    if ((dpid >> 40) & 0xFF) == 0xC0 or ((dpid >> 24) & 0xFF) == 0xC0:
        i = (dpid >> 4) & 0xF
        j = dpid & 0xF
        return i, j
    return None


class L2StaticController(app_manager.OSKenApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser

        # table miss to controller
        self.add_flow(dp, 0, parser.OFPMatch(), [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)])

        if parse_edge_from_dpid(dp.id):
            self._setup_edge(dp)
        elif parse_agg_from_dpid(dp.id):
            self._setup_agg(dp)
        elif parse_core_from_dpid(dp.id):
            self._setup_core(dp)

    def add_flow(self, dp, priority, match, actions):
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=dp, priority=priority, match=match, instructions=inst)
        dp.send_msg(mod)

    def _setup_edge(self, dp):
        parser = dp.ofproto_parser
        ofp = dp.ofproto
        p, i = parse_edge_from_dpid(dp.id)

        # ARP flood
        self.add_flow(dp, 50, parser.OFPMatch(eth_type=0x0806), [parser.OFPActionOutput(ofp.OFPP_FLOOD)])

        # Local /24 hosts
        for h, port in ((1, 1), (2, 2)):
            ipdst = (10 << 24) | (p << 16) | (i << 8) | (h + 1)
            self.add_flow(dp, 200, parser.OFPMatch(eth_type=0x0800, ipv4_dst=ipdst), [parser.OFPActionOutput(port)])

        # Default IPv4: deterministic uplink (port 3)
        self.add_flow(dp, 100, parser.OFPMatch(eth_type=0x0800), [parser.OFPActionOutput(3)])

    def _setup_agg(self, dp):
        parser = dp.ofproto_parser
        ofp = dp.ofproto
        p, j = parse_agg_from_dpid(dp.id)

        # ARP flood
        self.add_flow(dp, 50, parser.OFPMatch(eth_type=0x0806), [parser.OFPActionOutput(ofp.OFPP_FLOOD)])

        # Down to edges by /24
        for i_idx, port in ((0, 1), (1, 2)):
            ipdst = (10 << 24) | (p << 16) | (i_idx << 8)
            mask = 0xFFFFFF00
            self.add_flow(dp, 200, parser.OFPMatch(eth_type=0x0800, ipv4_dst=(ipdst, mask)), [parser.OFPActionOutput(port)])

        # Default IPv4: deterministic uplink (port 3)
        self.add_flow(dp, 100, parser.OFPMatch(eth_type=0x0800), [parser.OFPActionOutput(3)])

    def _setup_core(self, dp):
        parser = dp.ofproto_parser
        ofp = dp.ofproto

        # ARP flood
        self.add_flow(dp, 50, parser.OFPMatch(eth_type=0x0806), [parser.OFPActionOutput(ofp.OFPP_FLOOD)])

        # IPv4 per pod 10.p.0.0/16 -> port (p+1)
        for p in range(4):
            ipdst = (10 << 24) | (p << 16)
            mask = 0xFFFF0000
            self.add_flow(dp, 200, parser.OFPMatch(eth_type=0x0800, ipv4_dst=(ipdst, mask)), [parser.OFPActionOutput(p + 1)])

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        # Intentionally quiet; proactive flows should carry dataplane.
        pass
