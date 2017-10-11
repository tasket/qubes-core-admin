#
# The Qubes OS Project, https://www.qubes-os.org/
#
# Copyright (C) 2010  Joanna Rutkowska <joanna@invisiblethingslab.com>
# Copyright (C) 2013-2016  Marek Marczykowski-Górecki
#                               <marmarek@invisiblethingslab.com>
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, see <https://www.gnu.org/licenses/>.
#

import datetime
import qubes.ext
import qubes.firewall
import qubes.vm.qubesvm
import qubes.vm.appvm
import qubes.vm.templatevm
import qubes.utils

yum_proxy_ip = '10.137.255.254'
yum_proxy_port = '8082'


class R3Compatibility(qubes.ext.Extension):
    '''Maintain VM interface compatibility with R3.0 and R3.1.
    At lease where possible.
    '''

    features_to_services = {
        'service.ntpd': 'ntpd',
        'check-updates': 'qubes-update-check',
        'dvm': 'qubes-dvm',

    }

    # noinspection PyUnusedLocal
    @qubes.ext.handler('domain-qdb-create')
    def on_domain_qdb_create(self, vm, event):
        '''
        :param qubes.vm.qubesvm.QubesVM vm: \
            VM on which QubesDB entries were just created
        ''' # pylint: disable=unused-argument
        # /qubes-vm-type: AppVM, NetVM, ProxyVM, TemplateVM
        if isinstance(vm, qubes.vm.templatevm.TemplateVM):
            vmtype = 'TemplateVM'
        elif vm.netvm is not None and vm.provides_network:
            vmtype = 'ProxyVM'
        elif vm.netvm is None and vm.provides_network:
            vmtype = 'NetVM'
        else:
            vmtype = 'AppVM'
        vm.untrusted_qdb.write('/qubes-vm-type', vmtype)

        vm.untrusted_qdb.write("/qubes-iptables-error", '')
        self.write_iptables_qubesdb_entry(vm)

        self.write_services(vm)

    @qubes.ext.handler('domain-spawn')
    def on_domain_started(self, vm, event, **kwargs):
        # pylint: disable=unused-argument
        if vm.netvm:
            self.write_iptables_qubesdb_entry(vm.netvm)

    @qubes.ext.handler('firewall-changed')
    def on_firewall_changed(self, vm, event):
        # pylint: disable=unused-argument
        if vm.is_running() and vm.netvm:
            self.write_iptables_qubesdb_entry(vm.netvm)

    def write_iptables_qubesdb_entry(self, firewallvm):
        # pylint: disable=no-self-use
        firewallvm.untrusted_qdb.rm("/qubes-iptables-domainrules/")
        iptables = "# Generated by Qubes Core on {0}\n".format(
            datetime.datetime.now().ctime())
        iptables += "*filter\n"
        iptables += ":INPUT DROP [0:0]\n"
        iptables += ":FORWARD DROP [0:0]\n"
        iptables += ":OUTPUT ACCEPT [0:0]\n"

        # Strict INPUT rules
        iptables += "-A INPUT -i vif+ -p udp -m udp --dport 68 -j DROP\n"
        iptables += "-A INPUT -m conntrack --ctstate RELATED,ESTABLISHED " \
                    "-j ACCEPT\n"
        iptables += "-A INPUT -p icmp -j ACCEPT\n"
        iptables += "-A INPUT -i lo -j ACCEPT\n"
        iptables += "-A INPUT -j REJECT --reject-with icmp-host-prohibited\n"

        iptables += "-A FORWARD -m conntrack --ctstate RELATED,ESTABLISHED " \
                    "-j ACCEPT\n"
        # Deny inter-VMs networking
        iptables += "-A FORWARD -i vif+ -o vif+ -j DROP\n"
        iptables += "COMMIT\n"
        firewallvm.untrusted_qdb.write("/qubes-iptables-header", iptables)

        for vm in firewallvm.connected_vms:
            iptables = "*filter\n"
            conf = vm.firewall

            xid = vm.xid
            if xid < 0:  # VM not active ATM
                continue

            ip = vm.ip
            if ip is None:
                continue

            # Anti-spoof rules are added by vif-script (vif-route-qubes),
            # here we trust IP address

            for rule in conf.rules:
                if rule.specialtarget == 'dns':
                    if rule.dstports not in ('53', None):
                        continue
                    if rule.proto:
                        protos = {'tcp', 'udp'}.intersection(str(rule.proto))
                    else:
                        protos = {'tcp', 'udp'}
                    for proto in protos:
                        if rule.dsthost:
                            dsthosts = set(vm.dns).intersection(
                                [str(rule.dsthost).replace('/24', '')])
                        else:
                            dsthosts = vm.dns
                        for dsthost in dsthosts:
                            iptables += '-A FORWARD -s {}'.format(ip)
                            iptables += ' -d {!s}'.format(dsthost)
                            iptables += ' -p {!s}'.format(proto)
                            iptables += ' --dport 53'
                            iptables += ' -j {}\n'.format(
                                str(rule.action).upper())
                else:
                    iptables += '-A FORWARD -s {}'.format(ip)
                    if rule.dsthost:
                        iptables += ' -d {!s}'.format(rule.dsthost)
                    if rule.proto:
                        iptables += ' -p {!s}'.format(rule.proto)
                    if rule.dstports:
                        iptables += ' --dport {}'.format(
                            str(rule.dstports).replace('-', ':'))
                    iptables += ' -j {0}\n'.format(str(rule.action).upper())

            iptables += '-A FORWARD -s {0} -j {1}\n'.format(ip,
                str(conf.policy).upper())
            iptables += 'COMMIT\n'
            firewallvm.untrusted_qdb.write(
                '/qubes-iptables-domainrules/' + str(xid),
                iptables)
        # no need for ending -A FORWARD -j DROP, cause default action is DROP

        firewallvm.untrusted_qdb.write('/qubes-iptables', 'reload')

    def write_services(self, vm):
        for feature, value in vm.features.items():
            service = self.features_to_services.get(feature, None)
            if service is None:
                continue
            # forcefully convert to '0' or '1'
            vm.untrusted_qdb.write('/qubes-service/{}'.format(service),
                str(int(bool(value))))
        if 'updates-proxy-setup' in vm.features.keys():
            vm.untrusted_qdb.write(
                '/qubes-service/{}'.format('yum-proxy-setup'),
                str(int(bool(vm.features['updates-proxy-setup']))))
