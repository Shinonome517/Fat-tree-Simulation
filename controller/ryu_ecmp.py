#!/usr/bin/env python3
# OS-Ken ECMP controller for k=4 Fat-Tree (OpenFlow 1.3)
# - Builds SELECT groups on edge/agg switches (upstream ports)
# - Proactively installs IPv4 flows per topology semantics
# - Attempts OVS selection_method=hash (5-tuple) via ovs-ofctl; falls back if unsupported

from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from os_ken.ofproto import ofproto_v1_3
from os_ken.lib.packet import packet, ethernet, arp, ipv4
from os_ken.lib import hub
import subprocess
import logging


def parse_edge_from_dpid(dpid: int):
    """Parse edge switch id from DPID.
    Accept both 64-bit style 0x000000E000ppii and compact 0xE000ppii.
    """
    if ((dpid >> 40) & 0xFF) == 0xE0 or ((dpid >> 24) & 0xFF) == 0xE0:
        p = (dpid >> 8) & 0xFF
        i = dpid & 0xFF
        return p, i
    return None


def parse_agg_from_dpid(dpid: int):
    """Parse aggregation switch id from DPID.
    Accept both 64-bit style 0x000000A000ppii and compact 0xA000ppii.
    """
    if ((dpid >> 40) & 0xFF) == 0xA0 or ((dpid >> 24) & 0xFF) == 0xA0:
        p = (dpid >> 8) & 0xFF
        i = dpid & 0xFF
        return p, i
    return None


def parse_core_from_dpid(dpid: int):
    """Parse core switch id from DPID.
    Accept both 64-bit style 0x000000C00000ij and compact 0xC00000ij.
    """
    if ((dpid >> 40) & 0xFF) == 0xC0 or ((dpid >> 24) & 0xFF) == 0xC0:
        i = (dpid >> 4) & 0xF
        j = dpid & 0xF
        return i, j
    return None


def bridge_name_from_dpid(dpid: int) -> str:
    e = parse_edge_from_dpid(dpid)
    if e is not None:
        p, i = e
        return f"br_e_{p}_{i}"
    a = parse_agg_from_dpid(dpid)
    if a is not None:
        p, i = a
        return f"br_a_{p}_{i}"
    c = parse_core_from_dpid(dpid)
    if c is not None:
        i, j = c
        return f"br_c_{i}_{j}"
    return f"br_{dpid:x}"


class ECMPController(app_manager.OSKenApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = "osken_ecmp"
        # Tracks which switches finished setup
        self.ready = {}
        # background thread to annotate groups with selection method (best-effort)
        self._annotator = hub.spawn(self._annotate_groups_loop)

    def _annotate_groups_loop(self):
        # Try repeatedly to set selection_method on existing groups
        while True:
            try:
                for dpid, ok in list(self.ready.items()):
                    if not ok:
                        continue
                    br = bridge_name_from_dpid(dpid)
                    # detect if already set
                    cmd = [
                        "ovs-ofctl",
                        "-O", "OpenFlow13",
                        "dump-groups",
                        br,
                    ]
                    try:
                        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True)
                    except Exception:
                        continue
                    if "selection_method=hash" in out:
                        continue

                    # Attempt to mod group with selection_method=hash fields=5-tuple
                    # We know group ids used: 0x1000+ and 0x2000+ (edge/agg)
                    for gid in range(0x1000, 0x2000):
                        self._try_set_select_props(br, gid)
                    for gid in range(0x2000, 0x3000):
                        self._try_set_select_props(br, gid)
            except Exception as e:
                self.logger.debug(f"annotate loop error: {e}")
            finally:
                hub.sleep(5)

    def _try_set_select_props(self, br: str, gid: int):
        # Update existing groups with selection_method and fields (best-effort, ignore errors)
        # We don't know buckets here; mod-group without buckets can fail, so we re-spec buckets by querying dump-groups.
        try:
            out = subprocess.check_output(["ovs-ofctl", "-O", "OpenFlow13", "dump-groups", br], text=True)
            lines = [l.strip() for l in out.splitlines() if f"group_id={gid}" in l]
            if not lines:
                return
            # Extract buckets as "bucket=..." joined by ","
            buckets = []
            for l in lines:
                parts = [p.strip() for p in l.split(",")]
                for p in parts:
                    if p.startswith("bucket="):
                        # keep the original bucket spec after '='
                        buckets.append(p)
            if not buckets:
                return
            bucket_str = ",".join(buckets)
            spec = f"group_id={gid},type=select,selection_method=hash,fields=ip_src,ip_dst,ip_proto,tp_src,tp_dst,{bucket_str}"
            subprocess.check_call(["ovs-ofctl", "-O", "OpenFlow13", "mod-group", br, spec])
            self.logger.info(f"Set selection_method=hash on {br} gid={gid}")
        except subprocess.CalledProcessError as e:
            self.logger.debug(f"mod-group failed on {br} gid={gid}: {getattr(e, 'output', '')}")
        except FileNotFoundError:
            self.logger.warning("ovs-ofctl not found; cannot set selection_method=hash (fallback)")
        except Exception as e:
            self.logger.debug(f"set_select_props unexpected error: {e}")

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        dpid = dp.id

        self.logger.info(f"Switch connected: dpid=0x{dpid:x} name={bridge_name_from_dpid(dpid)}")

        # Table-miss: send to controller (minimal)
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        self.add_flow(dp, priority=0, match=match, actions=actions)

        # Set up per-role
        if parse_edge_from_dpid(dpid):
            self._setup_edge(dp)
        elif parse_agg_from_dpid(dpid):
            self._setup_agg(dp)
        elif parse_core_from_dpid(dpid):
            self._setup_core(dp)
        else:
            self.logger.warning(f"Unknown DPID format: 0x{dpid:x}")

        self.ready[dpid] = True

    def add_flow(self, datapath, priority, match, actions=None, inst=None, table_id=0):
        ofp = datapath.ofproto
        parser = datapath.ofproto_parser
        if inst is None:
            inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions or [])]
        mod = parser.OFPFlowMod(
            datapath=datapath,
            table_id=table_id,
            priority=priority,
            match=match,
            instructions=inst,
        )
        datapath.send_msg(mod)

    def _add_group_select(self, dp, gid: int, out_ports: list[int]):
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        buckets = []
        for p in out_ports:
            actions = [parser.OFPActionOutput(p)]
            buckets.append(parser.OFPBucket(actions=actions))
        req = parser.OFPGroupMod(
            dp,
            ofp.OFPGC_ADD,
            ofp.OFPGT_SELECT,
            gid,
            buckets,
        )
        dp.send_msg(req)

    def _setup_edge(self, dp):
        dpid = dp.id
        parser = dp.ofproto_parser
        ofp = dp.ofproto
        p, i = parse_edge_from_dpid(dpid)
        br = bridge_name_from_dpid(dpid)
        self.logger.info(f"Setup EDGE {br} (p={p}, i={i})")

        # Upstream ECMP group over ports [3,4]
        gid = 0x1000 + (p * 16) + i
        self._add_group_select(dp, gid, [3, 4])

        # ARP: flood
        match = parser.OFPMatch(eth_type=0x0806)
        actions = [parser.OFPActionOutput(ofp.OFPP_FLOOD)]
        self.add_flow(dp, 50, match, actions)

        # IPv4 local hosts (h=1 -> .2 on port1, h=2 -> .3 on port2)
        for h, port in ((1, 1), (2, 2)):
            ipdst = (10 << 24) | (p << 16) | (i << 8) | (h + 1)
            match = parser.OFPMatch(eth_type=0x0800, ipv4_dst=ipdst)
            actions = [parser.OFPActionOutput(port)]
            self.add_flow(dp, 200, match, actions)

        # IPv4 default: upstream via group
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, [parser.OFPActionGroup(gid)])]
        match = parser.OFPMatch(eth_type=0x0800)
        self.add_flow(dp, 100, match, inst=inst)

    def _setup_agg(self, dp):
        dpid = dp.id
        parser = dp.ofproto_parser
        ofp = dp.ofproto
        p, j = parse_agg_from_dpid(dpid)
        br = bridge_name_from_dpid(dpid)
        self.logger.info(f"Setup AGG {br} (p={p}, j={j})")

        # Upstream ECMP group over ports [3,4]
        gid = 0x2000 + (p * 16) + j
        self._add_group_select(dp, gid, [3, 4])

        # ARP: flood
        match = parser.OFPMatch(eth_type=0x0806)
        actions = [parser.OFPActionOutput(ofp.OFPP_FLOOD)]
        self.add_flow(dp, 50, match, actions)

        # IPv4 down to edge: two /24s 10.p.0.0/24 -> port1, 10.p.1.0/24 -> port2
        for i_idx, port in ((0, 1), (1, 2)):
            ipdst = (10 << 24) | (p << 16) | (i_idx << 8)
            mask = 0xFFFFFF00
            match = parser.OFPMatch(eth_type=0x0800, ipv4_dst=(ipdst, mask))
            actions = [parser.OFPActionOutput(port)]
            self.add_flow(dp, 200, match, actions)

        # IPv4 default: upstream via group
        match = parser.OFPMatch(eth_type=0x0800)
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, [parser.OFPActionGroup(gid)])]
        self.add_flow(dp, 100, match, inst=inst)

    def _setup_core(self, dp):
        dpid = dp.id
        parser = dp.ofproto_parser
        ofp = dp.ofproto
        i, j = parse_core_from_dpid(dpid)
        br = bridge_name_from_dpid(dpid)
        self.logger.info(f"Setup CORE {br} (i={i}, j={j})")

        # ARP: flood
        match = parser.OFPMatch(eth_type=0x0806)
        actions = [parser.OFPActionOutput(ofp.OFPP_FLOOD)]
        self.add_flow(dp, 50, match, actions)

        # IPv4 for each pod p: 10.p.0.0/16 -> port (p+1)
        for p in range(4):
            ipdst = (10 << 24) | (p << 16)
            mask = 0xFFFF0000
            match = parser.OFPMatch(eth_type=0x0800, ipv4_dst=(ipdst, mask))
            actions = [parser.OFPActionOutput(p + 1)]
            self.add_flow(dp, 200, match, actions)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        # Minimal handling to log ARP/IPv4; proactive flows should handle dataplane
        msg = ev.msg
        dp = msg.datapath
        in_port = msg.match["in_port"]
        data = None if msg.buffer_id == dp.ofproto.OFP_NO_BUFFER else None
        pkt = packet.Packet(msg.data if data is None else b"")
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None:
            return
        ipv4p = pkt.get_protocol(ipv4.ipv4)
        arpp = pkt.get_protocol(arp.arp)
        if arpp is not None:
            self.logger.debug(f"ARP packet-in on dpid=0x{dp.id:x} port={in_port}")
        elif ipv4p is not None:
            self.logger.debug(
                f"IPv4 packet-in on dpid=0x{dp.id:x} port={in_port} src={ipv4p.src} dst={ipv4p.dst}"
            )
