# Copyright (C) 2016 Li Cheng at Beijing University of Posts
# and Telecommunications. www.muzixing.com
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# conding=utf-8
import logging
import struct
import copy
import networkx as nx
from operator import attrgetter
from ryu import cfg
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, DEAD_DISPATCHER
from ryu.controller.handler import CONFIG_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet
from ryu.lib.packet import ethernet
from ryu.lib.packet import ipv4
from ryu.lib.packet import ipv6
from ryu.lib.packet import arp
from ryu.lib import hub

from ryu.topology import event, switches
from ryu.topology.api import get_switch, get_link
import setting


#CONF = cfg.CONF


class NetworkAwareness(app_manager.RyuApp):
    """
        NetworkAwareness is a Ryu app for discover topology information.
        This App can provide many data services for other App, such as
        link_to_port, access_table, switch_port_table,access_ports,
        interior_ports,topology graph and shorteest paths.

    """
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(NetworkAwareness, self).__init__(*args, **kwargs)
        self.topology_api_app = self
        self.name = "awareness"
        self.link_to_port = {}       # (src_dpid,dst_dpid)->(src_port,dst_port)
        self.access_table = {}       # {(sw,port) :[host1_ip]}
        self.access_table_IPv6 = {}  # {(sw,port) :[host1_ipv6]}
        self.switch_port_table = {}  # dpip->port_num
        self.access_ports = {}       # dpid->port_num
        self.interior_ports = {}     # dpid->port_num 

        self.graph = nx.DiGraph()       
        self.pre_graph = nx.DiGraph()
        self.pre_access_table = {}
        self.pre_access_table_IPv6 = {}
        self.pre_link_to_port = {}
        self.shortest_paths = None      
        self.event_brick = None
        # Start a green thread to discover network resource.
        self.discover_thread = hub.spawn(self._discover)

    def _discover(self):
        i = 0
        while True:
            self.show_topology()
            if i == 5:
#                msg = "I get a Event!"
#                event_WIAPA = ofp_event.EventWIAPAPathCalculation(msg)
#                self.event_brick = app_manager.lookup_service_brick('ofp_event')
#                self.event_brick.send_event_to_observers(event_WIAPA, MAIN_DISPATCHER)
#                self.logger.info("I send a Event!")
                self.get_topology(None)
                i = 0
            hub.sleep(setting.DISCOVERY_PERIOD)
            i = i + 1

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """
            Initial operation, send miss-table flow entry to datapaths.
        """
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        msg = ev.msg
        self.logger.info("switch:%s connected", datapath.id)

        # install table-miss flow entry
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)

    def add_flow(self, dp, p, match, actions, idle_timeout=0, hard_timeout=0):
        ofproto = dp.ofproto
        parser = dp.ofproto_parser

        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS,
                                             actions)]

        mod = parser.OFPFlowMod(datapath=dp, priority=p,
                                idle_timeout=idle_timeout,
                                hard_timeout=hard_timeout,
                                match=match, instructions=inst)
        dp.send_msg(mod)

    def get_host_location(self, host_ip):
        """
            Get host location info:(datapath, port) according to host ip.
        """
        for key in self.access_table.keys():
            if self.access_table[key] == host_ip:
                return key
        for key in self.access_table_IPv6.keys():
            if self.access_table_IPv6[key] == host_ip:
                return key
        if host_ip == "0.0.0.0" or host_ip == "255.255.255.255":
            return None
        self.logger.info("%s location is not found." % host_ip)
        return None

    def get_switches(self):
        return self.switches

    def get_links(self):
        return self.link_to_port


    def get_graph(self, link_list):
        """
            Get Adjacency matrix from link_to_port
        """
        for src in self.switches:
            for dst in self.switches:
                if src == dst:
                    self.graph.add_edge(src, dst, weight=0)
                elif (src, dst) in link_list:
                    self.graph.add_edge(src, dst, weight=1)
        return self.graph

    def create_port_map(self, switch_list):
        """
            Create interior_port table and access_port table. 
        """
        for sw in switch_list:
            dpid = sw.dp.id
            self.switch_port_table.setdefault(dpid, set())
            self.interior_ports.setdefault(dpid, set())
            self.access_ports.setdefault(dpid, set())

            for p in sw.ports:
                self.switch_port_table[dpid].add(p.port_no)

    def create_interior_links(self, link_list):
        """
            Get links`srouce port to dst port  from link_list,
            link_to_port:(src_dpid,dst_dpid)->(src_port,dst_port)
        """
        for link in link_list:
            src = link.src
            dst = link.dst
            self.link_to_port[
                (src.dpid, dst.dpid)] = (src.port_no, dst.port_no)

            # Find the access ports and interiorior ports
            if link.src.dpid in self.switches:
                self.interior_ports[link.src.dpid].add(link.src.port_no)
            if link.dst.dpid in self.switches:
                self.interior_ports[link.dst.dpid].add(link.dst.port_no)

    def create_access_ports(self):
        """
            Get ports without link into access_ports
        """
        for sw in self.switch_port_table:
            all_port_table = self.switch_port_table[sw]
            interior_port = self.interior_ports[sw]
            self.access_ports[sw] = all_port_table - interior_port

    def k_shortest_paths(self, graph, src, dst, weight='weight', k=1):
        """
            Great K shortest paths of src to dst.
        """
        generator = nx.shortest_simple_paths(graph, source=src,
                                             target=dst, weight=weight)
        shortest_paths = []
        try:
            for path in generator:
                if k <= 0:
                    break
                shortest_paths.append(path)                             
                k -= 1
            return shortest_paths
        except:
            self.logger.debug("No path between %s and %s" % (src, dst))

    def all_k_shortest_paths(self, graph, weight='weight', k=1):
        """
            Creat all K shortest paths between datapaths.
        """
        _graph = copy.deepcopy(graph)
        paths = {}

        # Find ksp in graph.
        for src in _graph.nodes():
            paths.setdefault(src, {src: [[src] for i in xrange(k)]})
            for dst in _graph.nodes():
                if src == dst:
                    continue
                paths[src].setdefault(dst, [])
                paths[src][dst] = self.k_shortest_paths(_graph, src, dst,
                                                        weight=weight, k=k)
        return paths

    # List the event list should be listened.
    events = [event.EventSwitchEnter,
              event.EventSwitchLeave, event.EventPortAdd,
              event.EventPortDelete, event.EventPortModify,
              event.EventLinkAdd, event.EventLinkDelete]

    @set_ev_cls(events)
    def get_topology(self, ev):
        """
            Get topology info and calculate shortest paths.
        """
        switch_list = get_switch(self.topology_api_app, None)
        self.create_port_map(switch_list)
        self.switches = self.switch_port_table.keys()
        links = get_link(self.topology_api_app, None)
        self.create_interior_links(links)
        self.create_access_ports()
        self.get_graph(self.link_to_port.keys())
        self.shortest_paths = self.all_k_shortest_paths(
            self.graph, weight='weight', k=2)

    def register_access_info(self, dpid, in_port, ip, mac):
        """
            Register access host info into access table.
        """
        if in_port in self.access_ports[dpid]:
            if (dpid, in_port) in self.access_table:
                if self.access_table[(dpid, in_port)] == ip:
                    return
                else:
                    self.access_table[(dpid, in_port)] = ip
                    return
            else:
                self.access_table.setdefault((dpid, in_port), None)
                self.access_table[(dpid, in_port)] = ip
                return
    def register_access_info_v6(self, dpid, in_port, ip, mac):
        """
            Register access host info into access table.
        """
        self.logger.info("1111")
        if in_port in self.access_ports[dpid]:
            
            if (dpid, in_port) in self.access_table_IPv6:
                if self.access_table_IPv6[(dpid, in_port)] == ip:
                    return
                else:
                    self.access_table_IPv6[(dpid, in_port)] = ip
                    self.logger.info("success register")
                    return
            else:
                self.access_table_IPv6.setdefault((dpid, in_port), None)
                self.access_table_IPv6[(dpid, in_port)] = ip
                
                return
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        """
            Hanle the packet in packet, and register the access info.
        """
        msg = ev.msg
        datapath = msg.datapath

        parser = datapath.ofproto_parser
        in_port = msg.match['in_port']
        pkt = packet.Packet(msg.data)

        eth_type = pkt.get_protocols(ethernet.ethernet)[0].ethertype
        arp_pkt = pkt.get_protocol(arp.arp)
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        ipv6_pkt = pkt.get_protocol(ipv6.ipv6)
        
        if ipv6_pkt:
            ipv6_src_ip = ipv6_pkt.src
            ipv6_dst_ip = ipv6_pkt.dst
            mac = None
#            if arp_src_ip == "192.168.1.106" or arp_dst_ip == "192.168.1.106":
#                return
            # Record the access info
            self.logger.info("ipv6 host record")
            self.register_access_info_v6(datapath.id, in_port, ipv6_src_ip, mac)
        if arp_pkt:
            src_ip = arp_pkt.src_ip
            dst_ip = arp_pkt.dst_ip
            mac = None
            self.logger.info("ipv4 host record %s" %src_ip)
            self.register_access_info(datapath.id, in_port, src_ip, mac)     
                   
    def show_topology(self):
        switch_num = len(self.graph.nodes())
        if setting.TOSHOW:#self.pre_graph != self.graph
            print "---------------------Topo Link---------------------"
            print '%10s' % ("switch"),
            for i in self.graph.nodes():
                print '%10d' % i,
            print ""
            for i in self.graph.nodes():
                print '%10d' % i,
                for j in self.graph[i].values():
                    print '%10.0f' % j['weight'],
                print ""
            self.pre_graph = copy.deepcopy(self.graph)

        if setting.TOSHOW:#self.pre_link_to_port != self.link_to_port and 
            print "---------------------Link Port---------------------"
            print '%10s' % ("switch"),
            for i in self.graph.nodes():
                print '%10d' % i,
            print ""
            for i in self.graph.nodes():
                print '%10d' % i,
                for j in self.graph.nodes():
                    if (i, j) in self.link_to_port.keys():
                        print '%10s' % str(self.link_to_port[(i, j)]),
                    else:
                        print '%10s' % "No-link",
                print ""
            self.pre_link_to_port = copy.deepcopy(self.link_to_port)

        if setting.TOSHOW:#self.pre_access_table != self.access_table and 
            print "----------------Access Host-------------------"
            print '%10s' % ("switch"), '%12s' % "Host"
            if not self.access_table.keys():
                print "    NO found host"
            else:
                for tup in self.access_table:
                    print '%10d:    ' % tup[0], self.access_table[tup]
            self.pre_access_table = copy.deepcopy(self.access_table)
        if setting.TOSHOW:#self.pre_access_table_IPv6 != self.access_table_IPv6 and 
            print "----------------Access Host IPv6-------------------"
            print '%10s' % ("switch"), '%12s' % "Host"
            if not self.access_table_IPv6.keys():
                print "    NO found host"
            else:
                for tup in self.access_table_IPv6:
                    print '%10d:    ' % tup[0], self.access_table_IPv6[tup]
            self.pre_access_table_IPv6 = copy.deepcopy(self.access_table_IPv6)
