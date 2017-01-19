#!/usr/bin/env python
#

# nfdcpd: A promiscuous, NFQUEUE-based DHCP, DHCPv6 and Router Advertisement server for virtual machine hosting
# Copyright (c) 2010-2017 GRNET SA
#
#    This program is free software; you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation; either version 2 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License along
#    with this program; if not, write to the Free Software Foundation, Inc.,
#    51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#

import os
import signal
import errno
import re
import sys
import glob
import time
import logging
import logging.handlers
import threading
import traceback

import daemon
import daemon.runner
import daemon.pidlockfile
import nfqueue
import pyinotify
import setproctitle
from lockfile import LockTimeout

import IPy
import socket
import select
from socket import AF_INET, AF_INET6

from scapy.data import ETH_P_ALL
from scapy.packet import BasePacket
from scapy.layers.l2 import Ether
from scapy.layers.inet import IP, UDP
from scapy.layers.inet6 import IPv6, ICMPv6ND_RA, ICMPv6ND_NA, \
                               ICMPv6NDOptDstLLAddr, \
                               ICMPv6NDOptPrefixInfo, \
                               ICMPv6NDOptRDNSS, \
                               ICMPv6NDOptMTU
from scapy.layers.dhcp import BOOTP, DHCP
from scapy.layers.dhcp6 import DHCP6_Reply, DHCP6OptDNSServers, \
                               DHCP6OptServerId, DHCP6OptClientId, \
                               DUID_LLT, DHCP6_InfoRequest, DHCP6OptDNSDomains
from scapy.fields import ShortField
import scapy.layers.dhcp as scapy_dhcp

scapy_dhcp.DHCPOptions[26] = ShortField("interface_mtu", 1500)
scapy_dhcp.DHCPRevOptions["interface_mtu"] = (26, scapy_dhcp.DHCPOptions[26])



DEFAULT_CONFIG = "/etc/nfdhcpd/nfdhcpd.conf"
DEFAULT_LEASE_LIFETIME = 604800 # 1 week
DEFAULT_LEASE_RENEWAL = 600  # 10 min
DEFAULT_RA_PERIOD = 300 # seconds
DHCP_DUMMY_SERVER_IP = "1.2.3.4"

LOG_FILENAME = "nfdhcpd.log"

SYSFS_NET = "/sys/class/net"

LOG_FORMAT = "%(asctime)-15s %(levelname)-8s %(message)s"

# Configuration file specification (see configobj documentation)
CONFIG_SPEC = """
[general]
pidfile = string()
datapath = string()
logdir = string()
user = string()

[dhcp]
enable_dhcp = boolean(default=True)
lease_lifetime = integer(min=0, max=4294967295)
lease_renewal = integer(min=0, max=4294967295)
server_ip = ip_addr()
server_on_link = boolean(default=False)
dhcp_queue = integer(min=0, max=65535)
nameservers = ip_addr_list(family=4)
domain = string(default=None)

[ipv6]
enable_ipv6 = boolean(default=True)
enable_dhcpv6 = boolean(default=False)
ra_period = integer(min=1, max=4294967295)
rs_queue = integer(min=0, max=65535)
ns_queue = integer(min=0, max=65535)
dhcp_queue = integer(min=0, max=65535, default=None)
dhcpv6_queue = integer(min=0, max=65535, default=None)
nameservers = ip_addr_list(family=6)
domains = force_list(default=None)
"""


DHCPDISCOVER = 1
DHCPOFFER = 2
DHCPREQUEST = 3
DHCPDECLINE = 4
DHCPACK = 5
DHCPNAK = 6
DHCPRELEASE = 7
DHCPINFORM = 8

DHCP_TYPES = {
    DHCPDISCOVER: "DHCPDISCOVER",
    DHCPOFFER: "DHCPOFFER",
    DHCPREQUEST: "DHCPREQUEST",
    DHCPDECLINE: "DHCPDECLINE",
    DHCPACK: "DHCPACK",
    DHCPNAK: "DHCPNAK",
    DHCPRELEASE: "DHCPRELEASE",
    DHCPINFORM: "DHCPINFORM",
}

DHCP_REQRESP = {
    DHCPDISCOVER: DHCPOFFER,
    DHCPREQUEST: DHCPACK,
    DHCPINFORM: DHCPACK,
    }

def ipv62mac(ipv6):
    # remove subnet info if given
    subnetIndex = ipv6.find("/")
    if subnetIndex != -1:
        ipv6 = ipv6[:subnetIndex]

    ipv6Parts = ipv6.split(":")
    macParts = []
    for ipv6Part in ipv6Parts[-4:]:
        while len(ipv6Part) < 4:
            ipv6Part = "0" + ipv6Part
        macParts.append(ipv6Part[:2])
        macParts.append(ipv6Part[-2:])

    # modify parts to match MAC value
    macParts[0] = "%02x" % (int(macParts[0], 16) ^ 2)
    del macParts[4]
    del macParts[3]

    return ":".join(macParts)

def get_indev(payload):
    try:
        indev_ifindex = payload.get_physindev()
        if indev_ifindex:
            logging.debug(" - Incoming packet from device with ifindex %s",
                          indev_ifindex)
            return indev_ifindex
    except AttributeError:
        #TODO: return error value
        logging.error("No get_physindev() supported")
        return 0

    indev_ifindex = payload.get_indev()
    logging.debug(" - Incoming packet from device with ifindex %s", indev_ifindex)

    return indev_ifindex


def parse_binding_file(path):
    """ Read a client configuration binding file

    """
    logging.info("Parsing binding file %s", path)
    try:
        iffile = open(path, 'r')
    except EnvironmentError, e:
        logging.warn(" - Unable to open binding file %s: %s", path, str(e))
        return None

    def get_value(line):
        v = line.strip().split('=')[1]
        if v == '':
            return None
        return v

    try:
        tap = os.path.basename(path)
        indev = None
        mac = None
        ip = None
        hostname = None
        subnet = None
        gateway = None
        subnet6 = None
        gateway6 = None
        eui64 = None
        macspoof = None
        mtu = None
        private = None

        for line in iffile:
            if line.startswith("IP="):
                ip = get_value(line)
            elif line.startswith("MAC="):
                mac = get_value(line)
            elif line.startswith("HOSTNAME="):
                hostname = get_value(line)
            elif line.startswith("INDEV="):
                indev = get_value(line)
            elif line.startswith("SUBNET="):
                subnet = get_value(line)
            elif line.startswith("GATEWAY="):
                gateway = get_value(line)
            elif line.startswith("SUBNET6="):
                subnet6 = get_value(line)
            elif line.startswith("GATEWAY6="):
                gateway6 = get_value(line)
            elif line.startswith("EUI64="):
                eui64 = get_value(line)
            elif line.startswith("MACSPOOF="):
                macspoof = get_value(line)
            elif line.startswith("MTU="):
                mtu = int(get_value(line))
            elif line.startswith("PRIVATE="):
                private = get_value(line)
    finally:
        iffile.close()

    try:
        return Client(tap=tap, mac=mac, ip=ip, hostname=hostname,
                      indev=indev, subnet=subnet, gateway=gateway,
                      subnet6=subnet6, gateway6=gateway6, eui64=eui64,
                      macspoof=macspoof, mtu=mtu, private=private)
    except ValueError:
        logging.warning(" - Cannot add client for host %s and IP %s on tap %s",
                        hostname, ip, tap)
        return None


class ClientFileHandler(pyinotify.ProcessEvent):
    def __init__(self, server):
        pyinotify.ProcessEvent.__init__(self)
        self.server = server

    def process_IN_DELETE(self, event):  # pylint: disable=C0103
        """ Delete file handler

        Currently this removes an interface from the watch list

        """
        self.server.remove_tap(event.name)

    def process_IN_CLOSE_WRITE(self, event):  # pylint: disable=C0103
        """ Add file handler

        Currently this adds an interface to the watch list

        """
        self.server.add_tap(os.path.join(event.path, event.name))

    def process_IN_Q_OVERFLOW(self, event): # pylint: disable=C0103
        """ Event overflow handler

        Currently this reads all interface configs

        """
        for path in glob.glob(os.path.join(self.server.data_path, "*")):
            self.server.add_tap(path)


class Client(object):
    def __init__(self, tap=None, indev=None,
                 mac=None, ip=None, hostname=None,
                 subnet=None, gateway=None,
                 subnet6=None, gateway6=None, eui64=None,
                 macspoof=None, mtu=None, private=None):
        self.mac = mac
        self.ip = ip
        self.hostname = hostname
        self.indev = indev
        self.tap = tap
        self.subnet = subnet
        self.gateway = gateway
        self.net = Subnet(net=subnet, gw=gateway, dev=tap)
        self.subnet6 = subnet6
        self.gateway6 = gateway6
        self.net6 = Subnet(net=subnet6, gw=gateway6, dev=tap)
        self.eui64 = eui64
        self.open_socket()
        self.macspoof = macspoof
        self.mtu = mtu
        self.private = private

    def is_valid(self):
        return self.mac is not None and self.hostname is not None


    def open_socket(self):

        logging.debug(" - Opening L2 socket and binding to %s", self.tap)
        try:
            s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, ETH_P_ALL)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 0)
            s.bind((self.tap, ETH_P_ALL))
            self.socket = s
        except socket.error, e:
            logging.warning(" - Cannot open socket %s", e)


    def sendp(self, data):

        if isinstance(data, BasePacket):
            data = str(data)

        #logging.debug(" - Sending raw packet %r", data)

        try:
            count = self.socket.send(data, socket.MSG_DONTWAIT)
        except socket.error, e:
            logging.warn(" - Send with MSG_DONTWAIT failed: %s", str(e))
            self.socket.close()
            self.open_socket()
            raise e

        ldata = len(data)
        logging.debug(" - Sent %d bytes on %s", count, self.tap)
        if count != ldata:
            logging.warn(" - Truncated msg: %d/%d bytes sent",
                         count, ldata)

    def __repr__(self):
        ret =  "hostname %s, tap %s, mac %s" % \
               (self.hostname, self.tap, self.mac)
        if self.ip:
            ret += ", ip %s" % self.ip
        if self.eui64:
            ret += ", eui64 %s" % self.eui64
        return ret


class Subnet(object):
    def __init__(self, net=None, gw=None, dev=None):
        if isinstance(net, str):
            try:
                self.net = IPy.IP(net)
            except ValueError, e:
                logging.warning(" - IPy error: %s", e)
                raise e
        else:
            self.net = net
        self.gw = gw
        self.dev = dev

    def __getitem__(self, idx):
        """ Return the n-th address (network+idx) in the subnet (self.net)

        """
        if self.net and isinstance(self.net, IPy.IP):
            return self.net[idx]
        else:
            return None

    @property
    def netmask(self):
        """ Return the netmask in textual representation

        """
        return str(self.net.netmask())

    @property
    def broadcast(self):
        """ Return the broadcast address in textual representation

        """
        return str(self.net.broadcast())

    @property
    def prefix(self):
        """ Return the network as an IPy.IP

        """
        return self.net.net()

    @property
    def prefixlen(self):
        """ Return the prefix length as an integer

        """
        return self.net.prefixlen()

    @staticmethod
    def _make_eui64(net, mac):
        """ Compute an EUI-64 address from an EUI-48 (MAC) address

        """
        if mac is None:
            return None
        comp = mac.split(":")
        prefix = IPy.IP(net).net().strFullsize().split(":")[:4]
        eui64 = comp[:3] + ["ff", "fe"] + comp[3:]
        eui64[0] = "%02x" % (int(eui64[0], 16) ^ 0x02)
        for l in range(0, len(eui64), 2):
            prefix += ["".join(eui64[l:l+2])]
        return IPy.IP(":".join(prefix))

    def make_ll64(self, mac):
        """ Compute an IPv6 Link-local address from an EUI-48 (MAC) address

        """
        return self._make_eui64("fe80::", mac)


class VMNetProxy(object):  # pylint: disable=R0902
    def __init__(self, data_path, dhcp_queue_num=None,  # pylint: disable=R0913
                 rs_queue_num=None, ns_queue_num=None, dhcpv6_queue_num=None,
                 dhcp_lease_lifetime=DEFAULT_LEASE_LIFETIME,
                 dhcp_lease_renewal=DEFAULT_LEASE_RENEWAL,
                 dhcp_domain=None, dhcp_server_on_link=False,
                 dhcp_server_ip=DHCP_DUMMY_SERVER_IP, dhcp_nameservers=None,
                 ra_period=DEFAULT_RA_PERIOD, ipv6_nameservers=None,
                 dhcpv6_domains=None):

        try:
            getattr(nfqueue.payload, 'get_physindev')
            self.mac_indexed_clients = False
        except AttributeError:
            self.mac_indexed_clients = True
        self.data_path = data_path
        self.lease_lifetime = dhcp_lease_lifetime
        self.lease_renewal = dhcp_lease_renewal
        self.dhcp_domain = dhcp_domain
        self.dhcp_server_ip = dhcp_server_ip
        self.dhcp_server_on_link = dhcp_server_on_link
        self.ra_period = ra_period
        if dhcp_nameservers is None:
            self.dhcp_nameserver = []
        else:
            self.dhcp_nameservers = dhcp_nameservers

        if ipv6_nameservers is None:
            self.ipv6_nameservers = []
        else:
            self.ipv6_nameservers = ipv6_nameservers

        if dhcpv6_domains is None:
            self.dhcpv6_domains = []
        else:
            self.dhcpv6_domains = dhcpv6_domains

        self.ipv6_enabled = False

        self.clients = {}
        #self.subnets = {}
        #self.ifaces = {}
        #self.v6nets = {}
        self.nfq = {}

        # Inotify setup
        self.wm = pyinotify.WatchManager()
        mask = pyinotify.EventsCodes.ALL_FLAGS["IN_DELETE"]
        mask |= pyinotify.EventsCodes.ALL_FLAGS["IN_CLOSE_WRITE"]
        mask |= pyinotify.EventsCodes.ALL_FLAGS["IN_Q_OVERFLOW"]
        inotify_handler = ClientFileHandler(self)
        self.notifier = pyinotify.Notifier(self.wm, inotify_handler)
        self.wm.add_watch(self.data_path, mask, rec=True)

        # NFQUEUE setup
        if dhcp_queue_num is not None:
            self._setup_nfqueue(dhcp_queue_num, AF_INET, self.dhcp_response, 0)

        if rs_queue_num is not None:
            self._setup_nfqueue(rs_queue_num, AF_INET6, self.rs_response, 10)
            self.ipv6_enabled = True

        if ns_queue_num is not None:
            self._setup_nfqueue(ns_queue_num, AF_INET6, self.ns_response, 10)
            self.ipv6_enabled = True

        if dhcpv6_queue_num is not None:
            self._setup_nfqueue(dhcpv6_queue_num, AF_INET6, self.dhcpv6_response, 10)
            self.ipv6_enabled = True

    def get_binding(self, ifindex, mac):
        try:
            if self.mac_indexed_clients:
                logging.debug(" - Binding: Getting binding for mac %s", mac)
                b = self.clients[mac]
            else:
                logging.debug(" - Binding: Getting binding for ifindex %s", ifindex)
                b = self.clients[ifindex]
            logging.debug(" - Binding: Client found. %s", b)
            return b
        except KeyError:
            logging.debug(" - Binding: No client found for mac:%s / ifindex:%s",
                          mac, ifindex)
            return None

    def dhcpv6_response(self, arg1, arg2=None):  # pylint: disable=W0613

        logging.info(" * DHCPv6: Processing pending request")
        # Workaround for supporting both squeezy's nfqueue-bindings-python
        # and wheezy's python-nfqueue because for some reason the function's
        # signature has changed and has broken compatibility
        # See bug http://bugs.debian.org/cgi-bin/bugreport.cgi?bug=718894
        if arg2:
            payload = arg2
        else:
            payload = arg1
        pkt = IPv6(payload.get_data())
        indev = get_indev(payload)

        #logging.debug(pkt.show())
        #TODO: figure out how to find the src mac
        mac = None
        binding = self.get_binding(indev, mac)
        if binding is None:
            # We don't know anything about this interface, so accept the packet
            # and return and let the kernel handle it
            payload.set_verdict(nfqueue.NF_ACCEPT)
            return

        # Signal the kernel that it shouldn't further process the packet
        payload.set_verdict(nfqueue.NF_DROP)

        subnet = binding.net6

        if subnet.net is None:
            logging.debug(" - DHCPv6: No IPv6 network assigned to %s", binding)
            return

        indevmac = self.get_iface_hw_addr(binding.indev)
        if not indevmac:
            logging.debug(" - DHCPv6: Could not get MAC for %s", binding)
            return
        ifll = subnet.make_ll64(indevmac)
        if ifll is None:
            return

        ofll = subnet.make_ll64(binding.mac)
        if ofll is None:
            return

        logging.debug(" - DHCPv6: Generating response for %s", binding)

        if self.dhcpv6_domains:
            domains = self.dhcpv6_domains
        else:
            domains = [binding.hostname.split('.', 1)[-1]]

        # We do this in order not to caclulate optlen ourselves
        dnsdomains = str(DHCP6OptDNSDomains(dnsdomains=domains))
        dnsservers = str(DHCP6OptDNSServers(dnsservers=self.ipv6_nameservers))

        resp = Ether(src=indevmac, dst=binding.mac)/\
               IPv6(tc=192, src=str(ifll), dst=str(ofll))/\
               UDP(sport=pkt.dport, dport=pkt.sport)/\
               DHCP6_Reply(trid=pkt[DHCP6_InfoRequest].trid)/\
               DHCP6OptClientId(duid=pkt[DHCP6OptClientId].duid)/\
               DHCP6OptServerId(duid=DUID_LLT(lladdr=indevmac, timeval=time.time()))/\
               DHCP6OptDNSDomains(dnsdomains)/\
               DHCP6OptDNSServers(dnsservers)

        logging.info(" - DHCPv6: Response for %s", binding)
        try:
            binding.sendp(resp)
        except socket.error, e:
            logging.warn(" - DHCPv6: Response on %s failed: %s",
                         binding, str(e))
        except Exception, e:
            logging.warn(" - DHCPv6: Unkown error during response on %s: %s",
                         binding, str(e))


    @staticmethod
    def get_addr_on_link(binding, af=AF_INET):
        """ For a given client and address family return either the gateway
        or the first address in the subnet.

        """
        if af is AF_INET:
            gw = binding.gateway
            net = binding.net
        elif af is AF_INET6:
            gw = binding.gateway6
            net = binding.net6

        if gw is not None:
            return gw
        elif net is not None:
            return net[1]
        else:
            return None

    def _cleanup(self):
        """ Free all resources for a graceful exit

        """
        logging.info("Cleaning up")

        logging.debug(" - Closing netfilter queues")
        for q, _ in self.nfq.values():
            q.close()

        logging.debug(" - Stopping inotify watches")
        self.notifier.stop()

        logging.info(" - Cleanup finished")

    def _setup_nfqueue(self, queue_num, family, callback, pending):
        logging.info("Setting up NFQUEUE for queue %d, AF %s",
                      queue_num, family)
        q = nfqueue.queue()
        q.set_callback(callback)
        q.fast_open(queue_num, family)
        q.set_queue_maxlen(5000)
        # This is mandatory for the queue to operate
        q.set_mode(nfqueue.NFQNL_COPY_PACKET)
        self.nfq[q.get_fd()] = (q, pending)
        logging.debug(" - Successfully set up NFQUEUE %d", queue_num)

    def build_config(self):
        self.clients.clear()

        for path in glob.glob(os.path.join(self.data_path, "*")):
            self.add_tap(path)

        self.print_clients()

    def get_ifindex(self, iface):
        """ Get the interface index from sysfs

        """
        logging.debug(" - Getting ifindex for interface %s from sysfs", iface)

        path = os.path.abspath(os.path.join(SYSFS_NET, iface, "ifindex"))
        if not path.startswith(SYSFS_NET):
            return None

        ifindex = None

        try:
            f = open(path, 'r')
        except EnvironmentError:
            logging.error(" - %s is probably down, removing", iface)
            self.remove_tap(iface)

            return ifindex

        try:
            ifindex = f.readline().strip()
            try:
                ifindex = int(ifindex)
            except ValueError, e:
                logging.warn(" - Failed to get ifindex for %s, cannot parse"
                             " sysfs output '%s'", iface, ifindex)
                self.remove_tap(iface)
        except EnvironmentError, e:
            logging.warn(" - Error reading %s's ifindex from sysfs: %s",
                         iface, str(e))
            self.remove_tap(iface)
        finally:
            f.close()

        return ifindex

    def get_iface_hw_addr(self, iface):
        """ Get the interface hardware address from sysfs

        """
        logging.debug(" - Getting mac for iface %s", iface)
        path = os.path.abspath(os.path.join(SYSFS_NET, iface, "address"))
        if not path.startswith(SYSFS_NET):
            return None

        addr = None
        try:
            f = open(path, 'r')
        except EnvironmentError:
            logging.error(" - %s is probably down, removing", iface)
            self.remove_tap(iface)
            return addr

        try:
            addr = f.readline().strip()
        except EnvironmentError, e:
            logging.warn(" - Failed to read hw address for %s from sysfs: %s",
                         iface, str(e))
            self.remove_tap(iface)
        finally:
            f.close()

        return addr

    def add_tap(self, path):
        """ Add an interface to monitor

        """
        try:
            tap = os.path.basename(path)

            logging.debug("Updating configuration for %s", tap)
            binding = parse_binding_file(path)
            if binding is None:
                return
            ifindex = self.get_ifindex(binding.tap)

            if ifindex is None:
                logging.warn(" - Stale configuration for %s found", tap)
            else:
                if binding.is_valid():
                    if self.mac_indexed_clients:
                        self.clients[binding.mac] = binding
                        client = binding.mac
                    else:
                        self.clients[ifindex] = binding
                        client = ifindex
                    logging.debug(" - Added client %s. %s", client, binding)
        except Exception, e:
            logging.warn("Error while adding interface from path %s: %s",
path, str(e))

    def remove_tap(self, tap):
        """ Cleanup clients on a removed interface

        """
        try:
            for k, cl in self.clients.items():
                if cl.tap == tap:
                    cl.socket.close()
                    del self.clients[k]
                    logging.info("Removed client %s. %s", k, cl)
        except:
            logging.error("Client on %s disappeared!!!", tap)


    def dhcp_response(self, arg1, arg2=None):  # pylint: disable=W0613,R0914
        """ Generate a reply to bnetfilter-queue-deva BOOTP/DHCP request

        """
        logging.info(" * DHCP: Processing pending request")
        # Workaround for supporting both squeezy's nfqueue-bindings-python
        # and wheezy's python-nfqueue because for some reason the function's
        # signature has changed and has broken compatibility
        # See bug http://bugs.debian.org/cgi-bin/bugreport.cgi?bug=718894
        if arg2:
            payload = arg2
        else:
            payload = arg1
        # Decode the response - NFQUEUE relays IP packets
        pkt = IP(payload.get_data())
        #logging.debug(pkt.show())

        # Get the client MAC address
        try:
            resp = pkt.getlayer(BOOTP).copy()
        except Exception, e:
            logging.error(" - DHCP: Packet read failed: %s", str(e))
        hlen = resp.hlen
        mac = resp.chaddr[:hlen].encode("hex")
        mac, _ = re.subn(r'([0-9a-fA-F]{2})', r'\1:', mac, hlen - 1)

        # Server responses are always BOOTREPLYs
        resp.op = "BOOTREPLY"
        del resp.payload

        indev = get_indev(payload)

        binding = self.get_binding(indev, mac)
        if binding is None:
            # We don't know anything about this interface, so accept the packet
            # and return to let the kernel handle it
            payload.set_verdict(nfqueue.NF_ACCEPT)
            return

        # Signal the kernel that it shouldn't further process the packet
        payload.set_verdict(nfqueue.NF_DROP)

        if mac != binding.mac and binding.macspoof is None:
            logging.debug(" - DHCP: Received spoofed request from %s (and not %s)",
                         mac, binding)
            return

        if not binding.ip:
            logging.debug(" - DHCP: No IP found in binding file %s.", binding)
            return

        if self.dhcp_server_on_link is True:
            dhcp_srv_ip = self.get_addr_on_link(binding)
            if dhcp_srv_ip is None:
                logging.warn(" - DHCP: Could not get on-link address to use for DHCP response")
                return
        else:
            dhcp_srv_ip = self.dhcp_server_ip

        if not DHCP in pkt:
            logging.warn(" - DHCP: Invalid request with no DHCP payload found. %s", binding)
            return

        logging.debug(" - DHCP: Generating response for %s, src %s", binding, dhcp_srv_ip)

        resp = Ether(dst=mac, src=self.get_iface_hw_addr(binding.indev))/\
               IP(src=dhcp_srv_ip, dst=binding.ip)/\
               UDP(sport=pkt.dport, dport=pkt.sport)/resp
        subnet = binding.net

        dhcp_options = []
        requested_addr = binding.ip
        for opt in pkt[DHCP].options:
            if type(opt) is tuple and opt[0] == "message-type":
                req_type = opt[1]
            if type(opt) is tuple and opt[0] == "requested_addr":
                requested_addr = opt[1]

        logging.info(" - DHCP: %s from %s",
                     DHCP_TYPES.get(req_type, "UNKNOWN"), binding)

        if self.dhcp_domain:
            domainname = self.dhcp_domain
        else:
            domainname = binding.hostname.split('.', 1)[-1]

        if req_type == DHCPREQUEST and requested_addr != binding.ip:
            resp_type = DHCPNAK
            logging.info(" - DHCP: Sending DHCPNAK to %s (because requested %s)",
                         binding, requested_addr)

        elif req_type in (DHCPDISCOVER, DHCPREQUEST):
            resp_type = DHCP_REQRESP[req_type]
            resp.yiaddr = binding.ip
            dhcp_options += [
                 ("hostname", binding.hostname),
                 ("domain", domainname),
                 ("broadcast_address", str(subnet.broadcast)),
                 ("subnet_mask", str(subnet.netmask)),
                 ("renewal_time", self.lease_renewal),
                 ("lease_time", self.lease_lifetime),
            ]
            if subnet.gw and binding.private is None:
                dhcp_options += [("router", subnet.gw)]
            if binding.mtu:
                dhcp_options += [("interface_mtu", binding.mtu)]
            dhcp_options += [("name_server", x) for x in self.dhcp_nameservers]

        elif req_type == DHCPINFORM:
            resp_type = DHCP_REQRESP[req_type]
            dhcp_options += [
                 ("hostname", binding.hostname),
                 ("domain", domainname),
            ]
            dhcp_options += [("name_server", x) for x in self.dhcp_nameservers]

        elif req_type == DHCPRELEASE:
            # Log and ignore
            logging.info(" - DHCP: DHCPRELEASE from %s", binding)
            return

        # Finally, always add the server identifier and end options
        dhcp_options += [
            ("message-type", resp_type),
            ("server_id", dhcp_srv_ip),
            "end"
        ]
        resp /= DHCP(options=dhcp_options)

        logging.info(" - DHCP: %s for %s", DHCP_TYPES[resp_type], binding)
        try:
            binding.sendp(resp)
        except socket.error, e:
            logging.warn(" - DHCP: Response on %s failed: %s", binding, str(e))
        except Exception, e:
            logging.warn(" - DHCP: Unkown error during DHCP response on %s: %s",
                         binding, str(e))

    def rs_response(self, arg1, arg2=None):  # pylint: disable=W0613
        """ Generate a reply to an ICMPv6 router solicitation

        """
        logging.info(" * RS: Processing pending request")
        # Workaround for supporting both squeezy's nfqueue-bindings-python
        # and wheezy's python-nfqueue because for some reason the function's
        # signature has changed and has broken compatibility
        # See bug http://bugs.debian.org/cgi-bin/bugreport.cgi?bug=718894
        if arg2:
            payload = arg2
        else:
            payload = arg1
        pkt = IPv6(payload.get_data())
        #logging.debug(pkt.show())
        try:
            mac = ipv62mac(pkt.src)
            logging.debug(" - RS: MAC %s", mac)
        except:
            logging.error(" - RS: Cannot obtain MAC in RS")
            return

        indev = get_indev(payload)

        binding = self.get_binding(indev, mac)
        if binding is None:
            # We don't know anything about this interface, so accept the packet
            # and return and let the kernel handle it
            payload.set_verdict(nfqueue.NF_ACCEPT)
            return

        # Signal the kernel that it shouldn't further process the packet
        payload.set_verdict(nfqueue.NF_DROP)

        if mac != binding.mac and binding.macspoof is None:
            logging.debug(" - RS: Received spoofed request from %s (and not %s)",
                         mac, binding)
            return

        subnet = binding.net6

        if subnet.net is None:
            logging.debug(" - RS: No IPv6 network assigned to %s", binding)
            return

        indevmac = self.get_iface_hw_addr(binding.indev)
        if not indevmac:
            logging.debug(" - RS: Could not get MAC for %s", binding)
            return

        ifll = subnet.make_ll64(indevmac)
        if ifll is None:
            return

        logging.debug(" - RS: Generating response for %s", binding)

        # Enable Other Configuration Flag only when the DHCPv6 functionality is enabled
        other_config=0
        if config["ipv6"].as_bool("enable_dhcpv6"):
            other_config=1

        resp = Ether(src=indevmac)/\
               IPv6(src=str(ifll))/ICMPv6ND_RA(O=other_config, routerlifetime=14400)/\
               ICMPv6NDOptPrefixInfo(prefix=subnet.gw or str(subnet.prefix),
                                     prefixlen=subnet.prefixlen,
                                     R=1 if subnet.gw else 0)

        if self.ipv6_nameservers:
            resp /= ICMPv6NDOptRDNSS(dns=self.ipv6_nameservers,
                                     lifetime=self.ra_period * 3)
        if binding.mtu:
            resp /= ICMPv6NDOptMTU(mtu=binding.mtu)

        logging.info(" - RS: Sending RA for %s", binding)

        try:
            binding.sendp(resp)
        except socket.error, e:
            logging.warn(" - RS: RA failed on %s: %s",
                         binding, str(e))
        except Exception, e:
            logging.warn(" - RS: Unkown error during RA on %s: %s",
                         binding, str(e))

    def ns_response(self, arg1, arg2=None):  # pylint: disable=W0613
        """ Generate a reply to an ICMPv6 neighbour solicitation

        """

        logging.info(" * NS: Processing pending request")
        # Workaround for supporting both squeezy's nfqueue-bindings-python
        # and wheezy's python-nfqueue because for some reason the function's
        # signature has changed and has broken compatibility
        # See bug http://bugs.debian.org/cgi-bin/bugreport.cgi?bug=718894
        if arg2:
            payload = arg2
        else:
            payload = arg1

        ns = IPv6(payload.get_data())
        #logging.debug(ns.show())
        try:
            mac = ns.lladdr
            logging.debug(" - NS: MAC: %s", mac)
        except:
            logging.debug(" - NS: LLaddr not contained in NS. Ignoring.")
            return


        indev = get_indev(payload)

        binding = self.get_binding(indev, mac)
        if binding is None:
            # We don't know anything about this interface, so accept the packet
            # and return and let the kernel handle it
            payload.set_verdict(nfqueue.NF_ACCEPT)
            return

        payload.set_verdict(nfqueue.NF_DROP)

        if mac != binding.mac and binding.macspoof is None:
            logging.debug(" - NS: Received spoofed request from %s (and not %s)", mac, binding)
            return

        subnet = binding.net6
        if subnet.net is None:
            logging.debug(" - NS: No IPv6 network assigned to %s", binding)
            return

        indevmac = self.get_iface_hw_addr(binding.indev)
        if not indevmac:
            logging.debug(" - NS: Could not get MAC for %s", binding)
            return

        ifll = subnet.make_ll64(indevmac)
        if ifll is None:
            return

        if not (subnet.net.overlaps(ns.tgt) or str(ns.tgt) == str(ifll)):
            logging.debug(" - NS: Received NS for a non-routable IP (%s)", ns.tgt)
            return 1

        logging.debug(" - NS: Generating NA for %s", binding)

        resp = Ether(src=indevmac, dst=binding.mac)/\
               IPv6(src=str(ifll), dst=ns.src)/\
               ICMPv6ND_NA(R=1, O=0, S=1, tgt=ns.tgt)/\
               ICMPv6NDOptDstLLAddr(lladdr=indevmac)

        logging.info(" - NS: Sending NA for %s ", binding)

        try:
            binding.sendp(resp)
        except socket.error, e:
            logging.warn(" - NS: NA on %s failed: %s",
                         binding, str(e))
        except Exception, e:
            logging.warn(" - NS: Unkown error during NA to %s: %s",
                         binding, str(e))

    def send_periodic_ra(self):
        # Use a separate thread as this may take a _long_ time with
        # many interfaces and we want to be responsive in the mean time
        threading.Thread(target=self._send_periodic_ra).start()

    def _send_periodic_ra(self):
        logging.info(" * Periodic RA: Starting...")
        start = time.time()
        i = 0
        for binding in self.clients.values():
            tap = binding.tap
            indev = binding.indev
            # mac = binding.mac
            subnet = binding.net6
            if subnet.net is None:
                logging.debug(" - RA: Skipping %s", binding)
                continue
            indevmac = self.get_iface_hw_addr(indev)
            if not indevmac:
                logging.debug(" - RA: Could not get MAC for %s", binding)
                return
            ifll = subnet.make_ll64(indevmac)
            if ifll is None:
                continue
            resp = Ether(src=indevmac)/\
                   IPv6(src=str(ifll))/ICMPv6ND_RA(O=1, routerlifetime=14400)/\
                   ICMPv6NDOptPrefixInfo(prefix=subnet.gw or str(subnet.prefix),
                                         prefixlen=subnet.prefixlen,
                                         R=1 if subnet.gw else 0)
            if self.ipv6_nameservers:
                resp /= ICMPv6NDOptRDNSS(dns=self.ipv6_nameservers,
                                         lifetime=self.ra_period * 3)
            if binding.mtu:
                resp /= ICMPv6NDOptMTU(mtu=binding.mtu)

            try:
                binding.sendp(resp)
            except socket.error, e:
                logging.warn(" - RA: Failed on %s: %s",
                             binding, str(e))
            except Exception, e:
                logging.warn(" - RA: Unkown error on %s: %s", binding, str(e))
            i += 1
        logging.info(" - RA: Sent %d RAs in %.2f seconds", i, time.time() - start)

    def serve(self):
        """ Safely perform the main loop, freeing all resources upon exit

        """
        try:
            self._serve()
        finally:
            self._cleanup()

    def _serve(self):
        """ Loop forever, serving requests

        """
        self.build_config()

        # Yes, we are accessing _fd directly, but it's the only way to have a
        # single select() loop ;-)
        iwfd = self.notifier._fd  # pylint: disable=W0212

        start = time.time()
        if self.ipv6_enabled:
            timeout = self.ra_period
            self.send_periodic_ra()
        else:
            timeout = None

        while True:
            try:
                rlist, _, xlist = select.select(self.nfq.keys() + [iwfd],
                                                [], [], timeout)
            except select.error, e:
                if e[0] == errno.EINTR:
                    logging.debug("select() got interrupted")
                    continue

            if xlist:
                logging.warn("Warning: Exception on %s",
                             ", ".join([str(fd) for fd in xlist]))

            if rlist:
                if iwfd in rlist:
                # First check if there are any inotify (= configuration change)
                # events
                    self.notifier.read_events()
                    self.notifier.process_events()
                    rlist.remove(iwfd)

                logging.debug("Pending requests on fds %s", rlist)

                for fd in rlist:
                    try:
                        q, num = self.nfq[fd]
                        cnt = q.process_pending(num)
                        logging.debug(" * Processed %d requests on NFQUEUE"
                                      " with fd %d", cnt, fd)
                    except RuntimeError, e:
                        logging.warn("Error processing fd %d: %s", fd, str(e))
                    except Exception, e:
                        logging.warn("Unknown error processing fd %d: %s",
                                     fd, str(e))

            if self.ipv6_enabled:
                # Calculate the new timeout
                timeout = self.ra_period - (time.time() - start)

                if timeout <= 0:
                    start = time.time()
                    self.send_periodic_ra()
                    timeout = self.ra_period - (time.time() - start)

    def print_clients(self):
        logging.info("%10s   %20s %20s %10s %20s %40s",
                     'Key', 'Client', 'MAC', 'TAP', 'IP', 'IPv6')
        for k, cl in self.clients.items():
            logging.info("%10s | %20s %20s %10s %20s %40s",
                         k, cl.hostname, cl.mac, cl.tap, cl.ip, cl.eui64)



def main():
    import capng
    import optparse
    from cStringIO import StringIO
    from pwd import getpwnam, getpwuid
    from configobj import ConfigObj, ConfigObjError, flatten_errors

    import validate

    validator = validate.Validator()

    def is_ip_list(value, family=4):
        try:
            family = int(family)
        except ValueError:
            raise validate.VdtParamError(family)
        if isinstance(value, (str, unicode)):
            value = [value]
        if not isinstance(value, list):
            raise validate.VdtTypeError(value)

        for entry in value:
            try:
                ip = IPy.IP(entry)
            except ValueError:
                raise validate.VdtValueError(entry)

            if ip.version() != family:
                raise validate.VdtValueError(entry)
        return value

    validator.functions["ip_addr_list"] = is_ip_list
    config_spec = StringIO(CONFIG_SPEC)

    parser = optparse.OptionParser()
    parser.add_option("-c", "--config", dest="config_file",
                      help="The location of the data files", metavar="FILE",
                      default=DEFAULT_CONFIG)
    parser.add_option("-d", "--debug", action="store_true", dest="debug",
                      help="Turn on debugging messages")
    parser.add_option("-f", "--foreground", action="store_false",
                      dest="daemonize", default=True,
                      help="Do not daemonize, stay in the foreground")

    opts, args = parser.parse_args()

    try:
        config = ConfigObj(opts.config_file, configspec=config_spec)
    except ConfigObjError, err:
        sys.stderr.write("Failed to parse config file %s: %s" %
                         (opts.config_file, str(err)))
        sys.exit(1)

    results = config.validate(validator)
    if results != True:
        logging.fatal("Configuration file validation failed! See errors below:")
        for (section_list, key, unused) in flatten_errors(config, results):
            if key is not None:
                logging.fatal(" '%s' in section '%s' failed validation",
                              key, ", ".join(section_list))
            else:
                logging.fatal(" Section '%s' is missing",
                              ", ".join(section_list))
        sys.exit(1)

    try:
        uid = getpwuid(config["general"].as_int("user"))
    except ValueError:
        uid = getpwnam(config["general"]["user"])

    # Keep only the capabilities we need
    # CAP_NET_ADMIN: we need to send nfqueue packet verdicts to a netlinkgroup
    # CAP_NET_RAW: we need to reopen socket in case the buffer gets full
    # CAP_SETPCAP: needed by capng_change_id()
    capng.capng_clear(capng.CAPNG_SELECT_BOTH)
    capng.capng_update(capng.CAPNG_ADD,
                       capng.CAPNG_EFFECTIVE | capng.CAPNG_PERMITTED,
                       capng.CAP_NET_ADMIN)
    capng.capng_update(capng.CAPNG_ADD,
                       capng.CAPNG_EFFECTIVE | capng.CAPNG_PERMITTED,
                       capng.CAP_NET_RAW)
    capng.capng_update(capng.CAPNG_ADD,
                       capng.CAPNG_EFFECTIVE | capng.CAPNG_PERMITTED,
                       capng.CAP_SETPCAP)
    # change uid
    capng.capng_change_id(uid.pw_uid, uid.pw_gid,
                          capng.CAPNG_DROP_SUPP_GRP | \
                          capng.CAPNG_CLEAR_BOUNDING)

    logger = logging.getLogger()
    if opts.debug:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)

    if opts.daemonize:
        logfile = os.path.join(config["general"]["logdir"], LOG_FILENAME)
        try:
            handler = logging.handlers.WatchedFileHandler(logfile)
        except IOError:
            sys.stderr.write(" - Failed to open logging directory, exiting.\n")
            sys.exit(1)
    else:
        handler = logging.StreamHandler()

    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(handler)

    # Rename this process so 'ps' output looks like
    # this is a native executable.
    # NOTE: due to a bug in python-setproctitle, one cannot yet
    # set individual values for command-line arguments, so only show
    # the name of the executable instead.
    # setproctitle.setproctitle("\x00".join(sys.argv))
    setproctitle.setproctitle(sys.argv[0])

    if opts.daemonize:
        pidfile = daemon.pidlockfile.TimeoutPIDLockFile(
            config["general"]["pidfile"], 10)
        # Remove any stale PID files, left behind by previous invocations
        if daemon.runner.is_pidfile_stale(pidfile):
            logger.warning("Removing stale PID lock file %s", pidfile.path)
            pidfile.break_lock()

        d = daemon.DaemonContext(pidfile=pidfile,
                                 umask=0022,
                                 stdout=handler.stream,
                                 stderr=handler.stream,
                                 files_preserve=[handler.stream])
        try:
            d.open()
        except (daemon.pidlockfile.AlreadyLocked, LockTimeout):
            logger.critical("Failed to lock pidfile %s,"
                            " another instance running?", pidfile.path)
            sys.exit(1)

    logging.info("Starting up nfdhcpd v%s", __version__)
    logging.info("Running as %s (uid:%d, gid: %d)",
                  config["general"]["user"], uid.pw_uid, uid.pw_gid)

    proxy_opts = {}
    if config["dhcp"].as_bool("enable_dhcp"):
        proxy_opts.update({
            "dhcp_queue_num": config["dhcp"].as_int("dhcp_queue"),
            "dhcp_lease_lifetime": config["dhcp"].as_int("lease_lifetime"),
            "dhcp_lease_renewal": config["dhcp"].as_int("lease_renewal"),
            "dhcp_server_ip": config["dhcp"]["server_ip"],
            "dhcp_server_on_link": config["dhcp"]["server_on_link"],
            "dhcp_nameservers": config["dhcp"]["nameservers"],
            "dhcp_domain": config["dhcp"]["domain"],
        })

    if config["ipv6"].as_bool("enable_ipv6"):
        proxy_opts.update({
            "rs_queue_num": config["ipv6"].as_int("rs_queue"),
            "ns_queue_num": config["ipv6"].as_int("ns_queue"),
            "ra_period": config["ipv6"].as_int("ra_period"),
            "ipv6_nameservers": config["ipv6"]["nameservers"],
            "dhcpv6_domains": config["ipv6"]["domains"],
        })

    if config["ipv6"].as_bool("enable_ipv6") and config["ipv6"].as_bool("enable_dhcpv6"):
        try:
            transition_dhcpv6_queue = config["ipv6"].as_int("dhcpv6_queue")
        except:
            transition_dhcpv6_queue = config["ipv6"].as_int("dhcp_queue")
        proxy_opts.update({
            "dhcpv6_queue_num": transition_dhcpv6_queue,
        })

    # pylint: disable=W0142
    proxy = VMNetProxy(data_path=config["general"]["datapath"], **proxy_opts)

    logging.info("Ready to serve requests")


    def debug_handler(signum, _):
        logging.debug('Received signal %d. Printing proxy state...', signum)
        proxy.print_clients()

    # Set the signal handler for debuging clients
    signal.signal(signal.SIGUSR1, debug_handler)
    signal.siginterrupt(signal.SIGUSR1, False)

    try:
        proxy.serve()
    except Exception:
        if opts.daemonize:
            exc = "".join(traceback.format_exception(*sys.exc_info()))
            logging.critical(exc)
        raise


# vim: set ts=4 sts=4 sw=4 et :