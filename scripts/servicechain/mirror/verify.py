import os
from time import sleep

from util import retry
from config import ConfigSvcMirror
from servicechain.verify import VerifySvcChain
from floatingip.config import CreateAssociateFip
try:
    from quantumclient.common import exceptions
except ImportError:
    from neutronclient.common import exceptions


class VerifySvcMirror(ConfigSvcMirror, VerifySvcChain):

    def verify_svc_mirroring(self, si_count=1, svc_mode='transparent'):
        """Validate the service chaining datapath
           Test steps:
           1. Create the SI/ST in svc_mode specified.
           2. Create vn11/vm1, vn21/vm2
           3. Create the policy rule for ICMP/UDP and attach to vn's
           4. Send the traffic from vm1 to vm2 and verify if the packets gets mirrored to the analyzer
           5. If its a single analyzer only ICMP(5 pkts) will be sent else ICMP and UDP traffic will be sent.
           Pass criteria :
           count = sent
           single node : Pkts mirrored to the analyzer should be equal to 'count'
           multinode :Pkts mirrored to the analyzer should be equal to '2xcount'
        """
        if getattr(self, 'res', None):
            self.vn1_fq_name = "default-domain:admin:" + self.res.vn1_name
            self.vn1_name = self.res.vn1_name
            self.vn1_subnets = self.res.vn1_subnets
            self.vm1_name = self.res.vn1_vm1_name
            self.vn2_fq_name = "default-domain:admin:" + self.res.vn2_name
            self.vn2_name = self.res.vn2_name
            self.vn2_subnets = self.res.vn2_subnets
            self.vm2_name = self.res.vn2_vm2_name
        else:
            self.vn1_fq_name = "default-domain:admin:in_network_vn1%s" % si_count
            self.vn1_name = "in_network_vn1%s" % si_count
            self.vn1_subnets = ['31.1.1.0/24']
            self.vm1_name = 'vm1'
            self.vn2_fq_name = "default-domain:admin:in_network_vn2%s" % si_count
            self.vn2_name = "in_network_vn2%s" % si_count
            self.vn2_subnets = ['32.2.2.0/24']
            self.vm2_name = 'vm2'

        si_count = si_count
        self.action_list = []
        self.if_list = []
        self.st_name = 'st1'
        self.si_prefix = 'mirror_si_'
        self.policy_name = 'mirror_policy'

        self.st_fixture, self.si_fixtures = self.config_st_si(self.st_name,
                                                              self.si_prefix, si_count, left_vn=self.vn1_fq_name, svc_type='analyzer', svc_mode=svc_mode)
        self.action_list = self.chain_si(si_count, self.si_prefix)

        self.rules = [{'direction': '<>',
                       'protocol': 'icmp',
                       'source_network': self.vn1_name,
                       'src_ports': [0, -1],
                       'dest_network': self.vn2_name,
                       'dst_ports': [0, -1],
                       'simple_action': 'pass',
                       'action_list': {'simple_action': 'pass',
                                       'mirror_to': {'analyzer_name': self.action_list[0]}}
                       }
                      ]
        if len(self.action_list) == 2:
            self.rules.append({'direction': '<>',
                               'protocol': 'udp',
                               'source_network': self.vn1_name,
                               'src_ports': [0, -1],
                               'dest_network': self.vn2_name,
                               'dst_ports': [0, -1],
                               'simple_action': 'pass',
                               'action_list': {'simple_action': 'pass',
                                               'mirror_to': {'analyzer_name': self.action_list[1]}}
                               }
                              )
        self.policy_fixture = self.config_policy(self.policy_name, self.rules)
        if getattr(self, 'res', None):
            self.vn1_fixture = self.res.get_vn1_fixture()
            self.vn2_fixture = self.res.get_vn2_fixture()
            assert self.vn1_fixture.verify_on_setup()
            assert self.vn2_fixture.verify_on_setup()
        else:
            self.vn1_fixture = self.config_vn(self.vn1_name, self.vn1_subnets)
            self.vn2_fixture = self.config_vn(self.vn2_name, self.vn2_subnets)

        self.vn1_policy_fix = self.attach_policy_to_vn(
            self.policy_fixture, self.vn1_fixture)
        self.vn2_policy_fix = self.attach_policy_to_vn(
            self.policy_fixture, self.vn2_fixture)

        if getattr(self, 'res', None):
            self.vm1_fixture = self.res.get_vn1_vm1_fixture()
            self.vm2_fixture = self.res.get_vn2_vm2_fixture()
        else:
            # Making sure VM falls on diffrent compute host
            host_list = []
            for host in self.inputs.compute_ips:
                host_list.append(self.inputs.host_data[host]['name'])
            compute_1 = host_list[0]
            compute_2 = host_list[0]
            if len(host_list) > 1:
                compute_1 = host_list[0]
                compute_2 = host_list[1]
            self.vm1_fixture = self.config_vm(
                self.vn1_fixture, self.vm1_name, node_name=compute_1)
            self.vm2_fixture = self.config_vm(
                self.vn2_fixture, self.vm2_name, node_name=compute_2)
        assert self.vm1_fixture.verify_on_setup()
        assert self.vm2_fixture.verify_on_setup()
        self.vm1_fixture.wait_till_vm_is_up()
        self.vm2_fixture.wait_till_vm_is_up()
        result, msg = self.validate_vn(self.vn1_name)
        assert result, msg
        result, msg = self.validate_vn(self.vn2_name)
        assert result, msg
        self.verify_si(self.si_fixtures)
        # Wait for 90sec before tap interface info is updated in agent.
        # need to have code which checks svm status to be active instead of
        # blind sleep
        sleep(90)
        # Verify ICMP traffic mirror
        sessions = self.tcpdump_on_all_analyzer(self.si_prefix, si_count)
        errmsg = "Ping to right VM ip %s from left VM failed" % self.vm2_fixture.vm_ip
        assert self.vm1_fixture.ping_with_certainty(
            self.vm2_fixture.vm_ip), errmsg
        for svm_name, (session, pcap) in sessions.items():
            count = 10
            if svm_name == 'mirror_si_2_1':
                count = 0
            if self.vm1_fixture.vm_node_ip != self.vm2_fixture.vm_node_ip:
                count = count * 2
            self.verify_icmp_mirror(svm_name, session, pcap, count)

        # One mirror instance
        if len(self.action_list) != 2:
            return True

        # Verify UDP traffic mirror
        sessions = self.tcpdump_on_all_analyzer(self.si_prefix, si_count)
        # Install traffic package in VM
        self.vm1_fixture.install_pkg("Traffic")
        self.vm2_fixture.install_pkg("Traffic")

        sport = 8001
        dport = 9001
        sent, recv = self.verify_traffic(self.vm1_fixture, self.vm2_fixture,
                                         'udp', sport=sport, dport=dport)
        errmsg = "UDP traffic with src port %s and dst port %s failed" % (
            sport, dport)
        assert sent and recv == sent, errmsg
        for svm_name, (session, pcap) in sessions.items():
            count = sent
            if svm_name == 'mirror_si_1_1':
                count = 0
            if self.vm1_fixture.vm_node_ip != self.vm2_fixture.vm_node_ip:
                count = count * 2
            self.verify_l4_mirror(svm_name, session, pcap, count, 'udp')

        return True

    def verify_svc_mirroring_with_floating_ip(self, si_count=1):
        """Validate the service mirrroring with flaoting IP
           Test steps:
           1. Create the SI/ST in svc_mode specified.
           2. Create vn11/vm1, vn21/vm2
           3. Assosciate vm2 with floating IP
           3. Create the policy rule for ICMP/UDP and attach to vn's
           4. Send the traffic from vm1 to vm2(floating ip) and verify if the packets gets mirrored to the analyzer
           5. If its a single analyzer only ICMP(5 pkts) will be sent else ICMP and UDP traffic will be sent.
           Pass criteria :
           count = sent
           single node : Pkts mirrored to the analyzer should be equal to 'count'
           multinode :Pkts mirrored to the analyzer should be equal to '2xcount'
        """
        if getattr(self, 'res', None):
            self.vn1_name = self.res.vn1_name
            self.vn1_subnets = self.res.vn1_subnets
            self.vm1_name = self.res.vn1_vm1_name
            self.vn2_name = self.res.vn2_name
            self.vn2_subnets = self.res.vn2_subnets
            self.vm2_name = self.res.vn2_vm2_name
        else:
            self.vn1_name = "vn1%s" % si_count
            self.vn1_subnets = ['31.1.1.0/24']
            self.vm1_name = 'vm1'
            self.vn2_name = "vn2%s" % si_count
            self.vn2_subnets = ['32.2.2.0/24']
            self.vm2_name = 'vm2'

        si_count = si_count
        self.action_list = []
        self.if_list = []
        self.st_name = 'st1'
        self.si_prefix = 'mirror_si_'
        self.policy_name = 'mirror_policy'

        fip_pool_name = 'testpool'

        self.st_fixture, self.si_fixtures = self.config_st_si(self.st_name,
                                                              self.si_prefix, si_count, svc_type='analyzer', left_vn=self.vn1_name)
        self.action_list = self.chain_si(si_count, self.si_prefix)
        self.rules = [{'direction': '<>',
                       'protocol': 'icmp',
                       'source_network': self.vn1_name,
                       'src_ports': [0, -1],
                       'dest_network': self.vn1_name,
                       'dst_ports': [0, -1],
                       'simple_action': 'pass',
                       'action_list': {'simple_action': 'pass',
                                       'mirror_to': {'analyzer_name': self.action_list[0]}}
                       }
                      ]
        if len(self.action_list) == 2:
            self.rules.append({'direction': '<>',
                               'protocol': 'udp',
                               'source_network': self.vn1_name,
                               'src_ports': [8001, 8001],
                               'dest_network': self.vn1_name,
                               'dst_ports': [9001, 9001],
                               'simple_action': 'pass',
                               'action_list': {'simple_action': 'pass',
                                               'mirror_to': {'analyzer_name': self.action_list[1]}}
                               }
                              )
        self.policy_fixture = self.config_policy(self.policy_name, self.rules)
        if getattr(self, 'res', None):
            self.vn1_fixture = self.res.get_vn1_fixture()
            self.vn2_fixture = self.res.get_vn2_fixture()
            assert self.vn1_fixture.verify_on_setup()
            assert self.vn2_fixture.verify_on_setup()
        else:
            self.vn1_fixture = self.config_vn(self.vn1_name, self.vn1_subnets)
            self.vn2_fixture = self.config_vn(self.vn2_name, self.vn2_subnets)

        self.vn1_policy_fix = self.attach_policy_to_vn(
            self.policy_fixture, self.vn1_fixture)

        if getattr(self, 'res', None):
            self.vm1_fixture = self.res.get_vn1_vm1_fixture()
            self.vm2_fixture = self.res.get_vn2_vm2_fixture()
        else:
            # Making sure VM falls on diffrent compute host
            host_list = []
            for host in self.inputs.compute_ips:
                host_list.append(self.inputs.host_data[host]['name'])
            compute_1 = host_list[0]
            compute_2 = host_list[0]
            if len(host_list) > 1:
                compute_1 = host_list[0]
                compute_2 = host_list[1]
            self.vm1_fixture = self.config_vm(
                self.vn1_fixture, self.vm1_name, node_name=compute_1)
            self.vm2_fixture = self.config_vm(
                self.vn2_fixture, self.vm2_name, node_name=compute_2)
        assert self.vm1_fixture.verify_on_setup()
        assert self.vm2_fixture.verify_on_setup()
        self.vm1_fixture.wait_till_vm_is_up()
        self.vm2_fixture.wait_till_vm_is_up()

        result, msg = self.validate_vn(self.vn1_name)
        assert result, msg
        self.verify_si(self.si_fixtures)

        self.fip_fixture = self.config_fip(
            self.vn1_fixture.vn_id, pool_name=fip_pool_name)
        self.fip_ca = self.useFixture(
            CreateAssociateFip(self.inputs, self.fip_fixture,
                               self.vn1_fixture.vn_id,
                               self.vm2_fixture.vm_id))
        fip = self.vm2_fixture.vnc_lib_h.floating_ip_read(id=self.fip_ca.fip_id).\
            get_floating_ip_address()

        # Verify ICMP traffic mirror
        sessions = self.tcpdump_on_all_analyzer(self.si_prefix, si_count)
        errmsg = "Ping to right VM ip %s from left VM failed" % self.vm2_fixture.vm_ip
        assert self.vm1_fixture.ping_with_certainty(fip), errmsg
        for svm_name, (session, pcap) in sessions.items():
            count = 10
            if svm_name == 'mirror_si_2_1':
                count = 0
            if self.vm1_fixture.vm_node_ip != self.vm2_fixture.vm_node_ip:
                count = count * 2
            self.verify_icmp_mirror(svm_name, session, pcap, count)

        # One mirror instance
        if len(self.action_list) != 2:
            return True

        # Verify UDP traffic mirror
        sessions = self.tcpdump_on_all_analyzer(self.si_prefix, si_count)
        # Install traffic package in VM
        self.vm1_fixture.install_pkg("Traffic")
        self.vm2_fixture.install_pkg("Traffic")

        sport = 8001
        dport = 9001
        sent, recv = self.verify_traffic(self.vm1_fixture, self.vm2_fixture,
                                         'udp', sport=sport, dport=dport, fip=fip)
        errmsg = "UDP traffic with src port %s and dst port %s failed" % (
            sport, dport)
        assert sent and recv == sent, errmsg
        for svm_name, (session, pcap) in sessions.items():
            count = sent
            if svm_name == 'mirror_si_1_1':
                count = 0
            if self.vm1_fixture.vm_node_ip != self.vm2_fixture.vm_node_ip:
                count = count * 2
            self.verify_l4_mirror(svm_name, session, pcap, count, 'udp')

        return True

    def verify_svc_mirror_with_deny(self, si_count=1):
        """Validate the service chaining mirroring with deny rule
           Test steps:
           1. Create the SI/ST in svc_mode specified.
           2. Create vn11/vm1, vn21/vm2
           3. Create the policy rule for ICMP/UDP with deny rule and attach to vn's
           4. Cretae the dynamic policy with rule to mirror the pkts to analyzer and attach to VN's
           5. Send the traffic from vm1 to vm2 and verify if the packets gets mirrored to the analyzer
           5. If its a single analyzer only ICMP(5 pkts) will be sent else ICMP and UDP traffic will be sent.
           Pass criteria :
           Ping from should fail, only the pkts from vm1 should get mirrored.
           count = sent
           single node : Pkts mirrored to the analyzer should be equal to 'count'
           multinode :Pkts mirrored to the analyzer should be equal to '2xcount'
        """
        self.domain_name = "default-domain"
        self.project_name = "admin"

        if getattr(self, 'res', None):
            self.vn1_name = self.res.vn1_name
            self.vn1_subnets = self.res.vn1_subnets
            self.vm1_name = self.res.vn1_vm1_name
            self.vn2_name = self.res.vn2_name
            self.vn2_subnets = self.res.vn2_subnets
            self.vm2_name = self.res.vn2_vm2_name
        else:
            self.vn1_name = "vn1%s" % si_count
            self.vn1_subnets = ['31.1.1.0/24']
            self.vm1_name = 'vm1'
            self.vn2_name = "vn2%s" % si_count
            self.vn2_subnets = ['32.2.2.0/24']
            self.vm2_name = 'vm2'

        si_count = si_count
        self.action_list = []
        self.if_list = []
        self.st_name = 'st1'
        self.si_prefix = 'mirror_si_'
        self.policy_name = 'policy'
        self.dynamic_policy_name = 'mirror_policy'

        self.rules = [{'direction': '<>',
                       'protocol': 'icmp',
                       'source_network': self.vn1_name,
                       'src_ports': [0, -1],
                       'dest_network': self.vn2_name,
                       'dst_ports': [0, -1],
                       'simple_action': 'deny',
                       }]
        self.policy_fixture = self.config_policy(self.policy_name, self.rules)
        if getattr(self, 'res', None):
            self.vn1_fixture = self.res.get_vn1_fixture()
            self.vn2_fixture = self.res.get_vn2_fixture()
            assert self.vn1_fixture.verify_on_setup()
            assert self.vn2_fixture.verify_on_setup()
        else:
            self.vn1_fixture = self.config_vn(self.vn1_name, self.vn1_subnets)
            self.vn2_fixture = self.config_vn(self.vn2_name, self.vn2_subnets)

        self.vn1_policy_fix = self.attach_policy_to_vn(
            self.policy_fixture, self.vn1_fixture)
        self.vn2_policy_fix = self.attach_policy_to_vn(
            self.policy_fixture, self.vn2_fixture)

        if getattr(self, 'res', None):
            self.vm1_fixture = self.res.get_vn1_vm1_fixture()
            self.vm2_fixture = self.res.get_vn2_vm2_fixture()
        else:
            # Making sure VM falls on diffrent compute host
            host_list = []
            for host in self.inputs.compute_ips:
                host_list.append(self.inputs.host_data[host]['name'])
            compute_1 = host_list[0]
            compute_2 = host_list[0]
            if len(host_list) > 1:
                compute_1 = host_list[0]
                compute_2 = host_list[1]
            self.vm1_fixture = self.config_vm(
                self.vn1_fixture, self.vm1_name, node_name=compute_1)
            self.vm2_fixture = self.config_vm(
                self.vn2_fixture, self.vm2_name, node_name=compute_2)
        assert self.vm1_fixture.verify_on_setup()
        assert self.vm2_fixture.verify_on_setup()
        self.vm1_fixture.wait_till_vm_is_up()
        self.vm2_fixture.wait_till_vm_is_up()

        self.st_fixture, self.si_fixtures = self.config_st_si(self.st_name,
                                                              self.si_prefix, si_count, svc_type='analyzer', left_vn=self.vn1_name)
        self.action_list = self.chain_si(si_count, self.si_prefix)

        dynamic_rules = [{'direction': '<>',
                          'protocol': 'icmp',
                          'source_network': self.vn1_name,
                          'src_ports': [0, -1],
                          'dest_network': self.vn2_name,
                          'dst_ports': [0, -1],
                          'simple_action': 'pass',
                          'action_list': {'simple_action': 'pass',
                                          'mirror_to': {'analyzer_name': self.action_list[0]}}
                          }]
        dynamic_policy_fixture = self.config_policy(
            self.dynamic_policy_name, dynamic_rules)
        vn1_dynamic_policy_fix = self.attach_policy_to_vn(
            dynamic_policy_fixture, self.vn1_fixture, policy_type='dynamic')
        vn2_dynamic_policy_fix = self.attach_policy_to_vn(
            dynamic_policy_fixture, self.vn2_fixture, policy_type='dynamic')
        result, msg = self.validate_vn(self.vn1_name)
        assert result, msg
        result, msg = self.validate_vn(self.vn2_name)
        assert result, msg
        self.verify_si(self.si_fixtures)

        # Verify ICMP traffic mirror
        sessions = self.tcpdump_on_all_analyzer(self.si_prefix, si_count)
        errmsg = "Ping to right VM ip %s from left VM passed; expected to fail" % self.vm2_fixture.vm_ip
        assert self.vm1_fixture.ping_with_certainty(
            self.vm2_fixture.vm_ip, expectation=False), errmsg
        for svm_name, (session, pcap) in sessions.items():
            count = 5
            if svm_name == 'mirror_si_2_1':
                count = 0
            self.verify_icmp_mirror(svm_name, session, pcap, count)

        return True

    def verify_icmp_mirror_on_all_analyzer(self, sessions, left_vm_fix, right_vm_fix, expectation=True):
        # Ping from left VM to right VM
        errmsg = "Ping to right VM ip %s from left VM failed" % right_vm_fix.vm_ip
        if not expectation:
            errmsg = "Ping to right VM ip %s from left VM passed, Expected to fail" % right_vm_fix.vm_ip
        assert left_vm_fix.ping_with_certainty(
            right_vm_fix.vm_ip, expectation=expectation), errmsg

        count = 10
        if not expectation:
            count = 0
        if left_vm_fix.vm_node_ip != right_vm_fix.vm_node_ip:
            count = count * 2
        for svm_name, (session, pcap) in sessions.items():
            self.verify_icmp_mirror(svm_name, session, pcap, count)

        return True

    def verify_icmp_mirror(self, svm_name, session, pcap, exp_count):
        mirror_pkt_count = self.stop_tcpdump(session, pcap)
        errmsg = "%s ICMP Packets mirrored to the analyzer VM %s,"\
                 "Expected %s packets" % (
                     mirror_pkt_count, svm_name, exp_count)
        if not mirror_pkt_count == exp_count:
            self.logger.error(errmsg)
            assert False, errmsg
        self.logger.info("%s ICMP packets are mirrored to the analyzer "
                         "service VM '%s'", mirror_pkt_count, svm_name)

        return True

    def verify_udp_mirror_on_all_analyzer(self, sessions, left_vm_fix, right_vm_fix, expectation=True):
        return self.verify_l4_mirror_on_all_analyzer(sessions, left_vm_fix, right_vm_fix, proto='udp', expectation=True)

    def verify_tcp_mirror_on_all_analyzer(self, sessions, left_vm_fix, right_vm_fix, expectation=True):
        return self.verify_l4_mirror_on_all_analyzer(sessions, left_vm_fix, right_vm_fix, proto='tcp', expectation=True)

    def verify_l4_mirror_on_all_analyzer(self, sessions, left_vm_fix, right_vm_fix, proto, expectation=True):
        # Install traffic package in VM
        left_vm_fix.install_pkg("Traffic")
        right_vm_fix.install_pkg("Traffic")

        sport = 8001
        dport = 9001
        sent, recv = self.verify_traffic(left_vm_fix, right_vm_fix,
                                         proto, sport=sport, dport=dport)
        errmsg = "'%s' traffic with src port %s and dst port %s failed" % (
            proto, sport, dport)
        count = sent
        if not expectation:
            count = 0
            errmsg = "'%s' traffic with src port %s and dst port %s passed; Expected to fail" % (
                proto, sport, dport)
        if left_vm_fix.vm_node_ip != right_vm_fix.vm_node_ip:
            count = count * 2
        assert sent and recv == sent, errmsg
        for svm_name, (session, pcap) in sessions.items():
            self.verify_l4_mirror(svm_name, session, pcap, exp_count, proto)

        return True

    @retry(delay=2, tries=6)
    def verify_l4_mirror(self, svm_name, session, pcap, exp_count, proto):
        mirror_pkt_count = self.stop_tcpdump(session, pcap)
        errmsg = "%s '%s' Packets mirrored to the analyzer VM, "\
                 "Expected %s packets" % (mirror_pkt_count, proto, exp_count)
        if not mirror_pkt_count == exp_count:
            self.logger.error(errmsg)
            return False, errmsg
        self.logger.info("%s '%s' packets are mirrored to the analyzer "
                         "service VM '%s'", mirror_pkt_count, proto, svm_name)
        return True

    def verify_policy_delete_add(self, si_prefix, si_count=1):
        # Delete policy
        self.detach_policy(self.vn1_policy_fix)
        self.detach_policy(self.vn2_policy_fix)
        self.unconfig_policy(self.policy_fixture)
        # Ping from left VM to right VM; expected to fail
        errmsg = "Ping to right VM ip %s from left VM passed; expected to fail" % self.vm2_fixture.vm_ip
        assert self.vm1_fixture.ping_with_certainty(
            self.vm2_fixture.vm_ip, expectation=False), errmsg
        sessions = self.tcpdump_on_all_analyzer(si_prefix, si_count)
        for svm_name, (session, pcap) in sessions.items():
            count = 0
            self.verify_icmp_mirror(svm_name, session, pcap, count)

        # Create policy again
        self.policy_fixture = self.config_policy(self.policy_name, self.rules)
        self.attach_policy_to_vn(self.policy_fixture, self.vn1_fixture)
        self.attach_policy_to_vn(self.policy_fixture, self.vn2_fixture)
        self.verify_si(self.si_fixtures)

        # Verify ICMP traffic mirror
        sessions = self.tcpdump_on_all_analyzer(si_prefix, si_count)
        errmsg = "Ping to right VM ip %s from left VM failed" % self.vm2_fixture.vm_ip
        assert self.vm1_fixture.ping_with_certainty(
            self.vm2_fixture.vm_ip), errmsg
        for svm_name, (session, pcap) in sessions.items():
            count = 10
            if svm_name == 'mirror_si_2_1':
                count = 0
            if self.vm1_fixture.vm_node_ip != self.vm2_fixture.vm_node_ip:
                count = count * 2
            self.verify_icmp_mirror(svm_name, session, pcap, count)

        return True

    def verify_add_new_vns(self, si_prefix, si_count=1):
        # Delete policy
        self.detach_policy(self.vn1_policy_fix)
        self.detach_policy(self.vn2_policy_fix)
        self.unconfig_policy(self.policy_fixture)

        # Create one more left and right VN's
        new_left_vn = "new_left_bridge_vn"
        new_left_vn_net = ['51.1.1.0/24']
        new_right_vn = "new_right_bridge_vn"
        new_right_vn_net = ['52.2.2.0/24']
        new_left_vn_fix = self.config_vn(new_left_vn, new_left_vn_net)
        new_right_vn_fix = self.config_vn(new_right_vn, new_right_vn_net)

        # Launch VMs in new left and right VN's
        new_left_vm = 'new_left_bridge_vm'
        new_right_vm = 'new_right_bridge_vm'
        new_left_vm_fix = self.config_vm(new_left_vn_fix, new_left_vm)
        new_right_vm_fix = self.config_vm(new_right_vn_fix, new_right_vm)
        assert new_left_vm_fix.verify_on_setup()
        assert new_right_vm_fix.verify_on_setup()
        # Wait for VM's to come up
        new_left_vm_fix.wait_till_vm_is_up()
        new_right_vm_fix.wait_till_vm_is_up()

        # Add rule to policy to allow traffic from new left_vn to right_vn
        # through SI
        new_rule = {'direction': '<>',
                    'protocol': 'udp',
                    'source_network': new_left_vn,
                    'src_ports': [0, -1],
                    'dest_network': new_right_vn,
                    'dst_ports': [0, -1],
                    'simple_action': 'pass',
                    'action_list': {'simple_action': 'pass',
                                    'mirror_to': {'analyzer_name': self.action_list[0]}}
                    }
        self.rules.append(new_rule)
        if len(self.action_list) == 2:
            self.rules.append({'direction': '<>',
                               'protocol': 'icmp',
                               'source_network': new_left_vn,
                               'src_ports': [0, -1],
                               'dest_network': new_right_vn,
                               'dst_ports': [0, -1],
                               'simple_action': 'pass',
                               'action_list': {'simple_action': 'pass',
                                               'mirror_to': {'analyzer_name': self.action_list[1]}}
                               }
                              )

        # Create new policy with rule to allow traffic from new VN's
        self.policy_fixture = self.config_policy(self.policy_name, self.rules)
        self.attach_policy_to_vn(self.policy_fixture, self.vn1_fixture)
        self.attach_policy_to_vn(self.policy_fixture, self.vn2_fixture)
        self.attach_policy_to_vn(self.policy_fixture, new_left_vn_fix)
        self.attach_policy_to_vn(self.policy_fixture, new_right_vn_fix)
        self.verify_si(self.si_fixtures)

        # Verify ICMP traffic mirror between existing VN's
        sessions = self.tcpdump_on_all_analyzer(si_prefix, si_count)
        errmsg = "Ping to right VM ip %s from left VM failed" % self.vm2_fixture.vm_ip
        assert self.vm1_fixture.ping_with_certainty(
            self.vm2_fixture.vm_ip), errmsg
        for svm_name, (session, pcap) in sessions.items():
            count = 10
            if svm_name == 'mirror_si_2_1':
                count = 0
            if self.vm1_fixture.vm_node_ip != self.vm2_fixture.vm_node_ip:
                count = count * 2
            self.verify_icmp_mirror(svm_name, session, pcap, count)

        # Verify UDP traffic mirror between New VN's
        sessions = self.tcpdump_on_all_analyzer(self.si_prefix, si_count)
        # Install traffic package in VM
        new_left_vm_fix.install_pkg("Traffic")
        new_right_vm_fix.install_pkg("Traffic")

        sport = 8001
        dport = 9001
        sent, recv = self.verify_traffic(new_left_vm_fix, new_right_vm_fix,
                                         'udp', sport=sport, dport=dport)
        errmsg = "UDP traffic with src port %s and dst port %s failed" % (
            sport, dport)
        assert sent and recv == sent, errmsg
        for svm_name, (session, pcap) in sessions.items():
            count = sent
            if svm_name == 'mirror_si_2_1':
                count = 0
            if new_left_vm_fix.vm_node_ip != new_right_vm_fix.vm_node_ip:
                count = count * 2
            self.verify_l4_mirror(svm_name, session, pcap, count, 'udp')

        # One mirror instance
        if len(self.action_list) != 2:
            return True

        # Verify UDP traffic mirror traffic between existing VN's
        sessions = self.tcpdump_on_all_analyzer(self.si_prefix, si_count)
        # Install traffic package in VM
        self.vm1_fixture.install_pkg("Traffic")
        self.vm2_fixture.install_pkg("Traffic")

        sport = 8001
        dport = 9001
        sent, recv = self.verify_traffic(self.vm1_fixture, self.vm2_fixture,
                                         'udp', sport=sport, dport=dport)
        errmsg = "UDP traffic with src port %s and dst port %s failed" % (
            sport, dport)
        assert sent and recv == sent, errmsg
        for svm_name, (session, pcap) in sessions.items():
            count = sent
            if svm_name == 'mirror_si_1_1':
                count = 0
            if self.vm1_fixture.vm_node_ip != self.vm2_fixture.vm_node_ip:
                count = count * 2
            self.verify_l4_mirror(svm_name, session, pcap, count, 'udp')

        # Verify ICMP traffic mirror between new VN's
        sessions = self.tcpdump_on_all_analyzer(si_prefix, si_count)
        errmsg = "Ping to right VM ip %s from left VM failed" % new_right_vm_fix.vm_ip
        assert new_left_vm_fix.ping_with_certainty(
            new_right_vm_fix.vm_ip), errmsg
        for svm_name, (session, pcap) in sessions.items():
            count = 10
            if svm_name == 'mirror_si_1_1':
                count = 0
            if left_vm_fix.vm_node_ip != right_vm_fix.vm_node_ip:
                count = count * 2
            self.verify_icmp_mirror(svm_name, session, pcap, count)

        return True

    def verify_svc_mirroring_unidirection(self, si_count=1, svc_mode='transparent'):
        """Validate the service chaining datapath with unidirection traffic
           Test steps:
           1. Create the SI/ST in svc_mode specified.
           2. Create vn11/vm1, vn21/vm2
           3. Create the policy rule for ICMP/UDP with 'unidirection rule' and attach to vn's
           4. Send the traffic from vm1 to vm2 and verify if the packets gets mirrored to the analyzer
           5. If its a single analyzer only ICMP(5 pkts) will be sent else ICMP and UDP traffic will be sent.
           Pass criteria :
           Pinf from vm1 to vm2 should fail. Only the pkts from vm1 should get mirrored.
           count = sent
           single node : Pkts mirrored to the analyzer should be equal to 'count'
           multinode :Pkts mirrored to the analyzer should be equal to '2xcount'
        """
        if getattr(self, 'res', None):
            self.vn1_name = self.res.vn1_name
            self.vn1_subnets = self.res.vn1_subnets
            self.vm1_name = self.res.vn1_vm1_name
            self.vn2_name = self.res.vn2_name
            self.vn2_subnets = self.res.vn2_subnets
            self.vm2_name = self.res.vn2_vm2_name
        else:
            self.vn1_name = "vn1%s" % si_count
            self.vn1_subnets = ['31.1.1.0/24']
            self.vm1_name = 'vm1'
            self.vn2_name = "vn2%s" % si_count
            self.vn2_subnets = ['32.2.2.0/24']
            self.vm2_name = 'vm2'

        si_count = si_count
        self.action_list = []
        self.if_list = []
        self.st_name = 'st1'
        self.si_prefix = 'mirror_si_'
        self.policy_name = 'mirror_policy'

        self.st_fixture, self.si_fixtures = self.config_st_si(self.st_name,
                                                              self.si_prefix, si_count, left_vn=self.vn1_name, svc_type='analyzer', svc_mode=svc_mode)
        self.action_list = self.chain_si(si_count, self.si_prefix)

        self.rules = [{'direction': '>',
                       'protocol': 'icmp',
                       'source_network': self.vn1_name,
                       'src_ports': [0, -1],
                       'dest_network': self.vn2_name,
                       'dst_ports': [0, -1],
                       'simple_action': 'pass',
                       'action_list': {'simple_action': 'pass',
                                       'mirror_to': {'analyzer_name': self.action_list[0]}}
                       }
                      ]
        if len(self.action_list) == 2:
            self.rules.append({'direction': '>',
                               'protocol': 'udp',
                               'source_network': self.vn1_name,
                               'src_ports': [0, -1],
                               'dest_network': self.vn2_name,
                               'dst_ports': [0, -1],
                               'simple_action': 'pass',
                               'action_list': {'simple_action': 'pass',
                                               'mirror_to': {'analyzer_name': self.action_list[1]}}
                               }
                              )
        self.policy_fixture = self.config_policy(self.policy_name, self.rules)
        if getattr(self, 'res', None):
            self.vn1_fixture = self.res.get_vn1_fixture()
            self.vn2_fixture = self.res.get_vn2_fixture()
            assert self.vn1_fixture.verify_on_setup()
            assert self.vn2_fixture.verify_on_setup()
        else:
            self.vn1_fixture = self.config_vn(self.vn1_name, self.vn1_subnets)
            self.vn2_fixture = self.config_vn(self.vn2_name, self.vn2_subnets)

        self.vn1_policy_fix = self.attach_policy_to_vn(
            self.policy_fixture, self.vn1_fixture)
        self.vn2_policy_fix = self.attach_policy_to_vn(
            self.policy_fixture, self.vn2_fixture)

        if getattr(self, 'res', None):
            self.vm1_fixture = self.res.get_vn1_vm1_fixture()
            self.vm2_fixture = self.res.get_vn2_vm2_fixture()
        else:
            # Making sure VM falls on diffrent compute host
            host_list = []
            for host in self.inputs.compute_ips:
                host_list.append(self.inputs.host_data[host]['name'])
            compute_1 = host_list[0]
            compute_2 = host_list[0]
            if len(host_list) > 1:
                compute_1 = host_list[0]
                compute_2 = host_list[1]
            self.vm1_fixture = self.config_vm(
                self.vn1_fixture, self.vm1_name, node_name=compute_1)
            self.vm2_fixture = self.config_vm(
                self.vn2_fixture, self.vm2_name, node_name=compute_2)
        assert self.vm1_fixture.verify_on_setup()
        assert self.vm2_fixture.verify_on_setup()
        self.vm1_fixture.wait_till_vm_is_up()
        self.vm2_fixture.wait_till_vm_is_up()

        result, msg = self.validate_vn(self.vn1_name)
        assert result, msg
        result, msg = self.validate_vn(self.vn2_name)
        assert result, msg
        self.verify_si(self.si_fixtures)

        # Verify ICMP traffic mirror
        sessions = self.tcpdump_on_all_analyzer(self.si_prefix, si_count)
        ret = self.vm1_fixture.ping_with_certainty(
            self.vm2_fixture.vm_ip, expectation=False)
        errmsg = "Error: Got the ping reply from right VM %s: Expected to fail" % self.vm2_fixture.vm_ip
        assert ret, errmsg
        for svm_name, (session, pcap) in sessions.items():
            count = 5
            if svm_name == 'mirror_si_2_1':
                count = 0
            if self.vm1_fixture.vm_node_ip != self.vm2_fixture.vm_node_ip:
                count = count * 2
            self.verify_icmp_mirror(svm_name, session, pcap, count)

        # One mirror instance
        if len(self.action_list) != 2:
            return True

        # Verify UDP traffic mirror
        sessions = self.tcpdump_on_all_analyzer(self.si_prefix, si_count)
        # Install traffic package in VM
        self.vm1_fixture.install_pkg("Traffic")
        self.vm2_fixture.install_pkg("Traffic")

        sport = 8001
        dport = 9001
        sent, recv = self.verify_traffic(self.vm1_fixture, self.vm2_fixture,
                                         'udp', sport=sport, dport=dport)
        errmsg = "UDP traffic with src port %s and dst port %s failed" % (
            sport, dport)
        assert sent and recv == sent, errmsg
        for svm_name, (session, pcap) in sessions.items():
            count = sent
            if svm_name == 'mirror_si_1_1':
                count = 0
            if self.vm1_fixture.vm_node_ip != self.vm2_fixture.vm_node_ip:
                count = count * 2
            self.verify_l4_mirror(svm_name, session, pcap, count, 'udp')

        return True

    def verify_attach_detach_policy_with_svc_mirroring(self, si_count=1):
        """Validate the detach and attach policy with SI doesn't block traffic"""
        self.domain_name = "default-domain"
        self.project_name = "admin"

        if getattr(self, 'res', None):
            self.vn1_name = self.res.vn1_name
            self.vn1_subnets = self.res.vn1_subnets
            self.vm1_name = self.res.vn1_vm1_name
            self.vn2_name = self.res.vn2_name
            self.vn2_subnets = self.res.vn2_subnets
            self.vm2_name = self.res.vn2_vm2_name
        else:
            self.vn1_name = "vn1%s" % si_count
            self.vn1_subnets = ['10.1.1.0/24']
            self.vm1_name = 'vm1'
            self.vn2_name = "vn2%s" % si_count
            self.vn2_subnets = ['20.1.1.0/24']
            self.vm2_name = 'vm2'
        self.vn1_fq_name = ':'.join(
            [self.domain_name, self.project_name, self.vn1_name])
        self.vn2_fq_name = ':'.join(
            [self.domain_name, self.project_name, self.vn2_name])

        si_count = si_count
        self.action_list = []
        self.if_list = []
        self.st_name = 'st1'
        self.si_prefix = 'mirror_si_'
        self.policy_name = 'mirror_policy'
        self.svc_mode = 'in-network'

        fip_pool_name = 'testpool'

        self.st_fixture, self.si_fixtures = self.config_st_si(self.st_name,
                                                              self.si_prefix, si_count, left_vn=self.vn1_fq_name, svc_mode=self.svc_mode)
        self.action_list = self.chain_si(si_count, self.si_prefix)
        self.rules = [{'direction': '<>',
                       'protocol': 'icmp',
                       'source_network': self.vn1_name,
                       'src_ports': [0, -1],
                       'dest_network': self.vn2_name,
                       'dst_ports': [0, -1],
                       'simple_action': 'pass',
                       'action_list': {'simple_action': 'pass',
                                       'mirror_to': {'analyzer_name': self.action_list[0]}}
                       }
                      ]

        self.policy_fixture = self.config_policy(self.policy_name, self.rules)
        if getattr(self, 'res', None):
            self.vn1_fixture = self.res.get_vn1_fixture()
            self.vn2_fixture = self.res.get_vn2_fixture()
            assert self.vn1_fixture.verify_on_setup()
            assert self.vn2_fixture.verify_on_setup()
        else:
            self.vn1_fixture = self.config_vn(self.vn1_name, self.vn1_subnets)
            self.vn2_fixture = self.config_vn(self.vn2_name, self.vn2_subnets)

        self.vn1_policy_fix = self.attach_policy_to_vn(
            self.policy_fixture, self.vn1_fixture)
        self.vn2_policy_fix = self.attach_policy_to_vn(
            self.policy_fixture, self.vn2_fixture)

        if getattr(self, 'res', None):
            self.vm1_fixture = self.res.get_vn1_vm1_fixture()
            self.vm2_fixture = self.res.get_vn2_vm2_fixture()
        else:
            # Making sure VM falls on diffrent compute host
            host_list = []
            for host in self.inputs.compute_ips:
                host_list.append(self.inputs.host_data[host]['name'])
            compute_1 = host_list[0]
            compute_2 = host_list[0]
            if len(host_list) > 1:
                compute_1 = host_list[0]
                compute_2 = host_list[1]
            self.vm1_fixture = self.config_vm(
                self.vn1_fixture, self.vm1_name, node_name=compute_1)
            self.vm2_fixture = self.config_vm(
                self.vn2_fixture, self.vm2_name, node_name=compute_2)
        assert self.vm1_fixture.verify_on_setup()
        assert self.vm2_fixture.verify_on_setup()
        self.vm1_fixture.wait_till_vm_is_up()
        self.vm2_fixture.wait_till_vm_is_up()

        result, msg = self.validate_vn(self.vn1_name)
        assert result, msg
        self.verify_si(self.si_fixtures)

        self.fip_fixture = self.config_fip(
            self.vn2_fixture.vn_id, pool_name=fip_pool_name)
        self.fip_ca = self.useFixture(
            CreateAssociateFip(self.inputs, self.fip_fixture,
                               self.vn2_fixture.vn_id,
                               self.vm1_fixture.vm_id))
        fip = self.vm1_fixture.vnc_lib_h.floating_ip_read(id=self.fip_ca.fip_id).\
            get_floating_ip_address()

        # Verify ICMP traffic mirror
        sessions = self.tcpdump_on_all_analyzer(self.si_prefix, si_count)
        errmsg = "Ping to right VM ip %s from left VM failed" % self.vm1_fixture.vm_ip
        assert self.vm1_fixture.ping_with_certainty(
            self.vm2_fixture.vm_ip), errmsg
        for svm_name, (session, pcap) in sessions.items():
            count = 10
            if svm_name == 'mirror_si_2_1':
                count = 0
            if self.vm1_fixture.vm_node_ip != self.vm2_fixture.vm_node_ip:
                count = count * 2
            self.verify_icmp_mirror(svm_name, session, pcap, count)

        # detach the policy and attach again to both the network
        self.detach_policy(self.vn1_policy_fix)
        self.detach_policy(self.vn2_policy_fix)

        self.vn1_policy_fix = self.attach_policy_to_vn(
            self.policy_fixture, self.vn1_fixture)
        self.vn2_policy_fix = self.attach_policy_to_vn(
            self.policy_fixture, self.vn2_fixture)

        # Verify ICMP traffic mirror after attaching the policy again
        sessions = self.tcpdump_on_all_analyzer(self.si_prefix, si_count)
        errmsg = "Ping to right VM ip %s from left VM failed" % self.vm1_fixture.vm_ip
        assert self.vm1_fixture.ping_with_certainty(
            self.vm2_fixture.vm_ip), errmsg
        for svm_name, (session, pcap) in sessions.items():
            count = 10
            if svm_name == 'mirror_si_2_1':
                count = 0
            if self.vm1_fixture.vm_node_ip != self.vm2_fixture.vm_node_ip:
                count = count * 2
            self.verify_icmp_mirror(svm_name, session, pcap, count)

        return True

    def verify_detach_attach_diff_policy_with_mirroring(self, si_count=1):
        """validate attaching a policy with analyzer and detaching again removes all the routes and does not impact other policies"""
        self.domain_name = "default-domain"
        self.project_name = "admin"

        if getattr(self, 'res', None):
            self.vn1_name = self.res.vn1_name
            self.vn1_subnets = self.res.vn1_subnets
            self.vm1_name = self.res.vn1_vm1_name
            self.vn2_name = self.res.vn2_name
            self.vn2_subnets = self.res.vn2_subnets
            self.vm2_name = self.res.vn2_vm2_name
        else:
            self.vn1_name = "VN1%s" % si_count
            self.vn1_subnets = ['1.1.1.0/24']
            self.vm1_name = 'VM-traffic'
            self.vn2_name = "VN2%s" % si_count
            self.vn2_subnets = ['2.2.2.0/24']
            self.vm2_name = 'VM-ubuntu'
        self.vn1_fq_name = ':'.join(
            [self.domain_name, self.project_name, self.vn1_name])
        self.vn2_fq_name = ':'.join(
            [self.domain_name, self.project_name, self.vn2_name])

        if getattr(self, 'res', None):
            self.vn1_fixture = self.res.get_vn1_fixture()
            self.vn2_fixture = self.res.get_vn2_fixture()
            assert self.vn1_fixture.verify_on_setup()
            assert self.vn2_fixture.verify_on_setup()
        else:
            self.vn1_fixture = self.config_vn(self.vn1_name, self.vn1_subnets)
            self.vn2_fixture = self.config_vn(self.vn2_name, self.vn2_subnets)

        si_count = si_count
        self.action_list = []
        self.if_list = []
        self.st_name = 'st-analyzer-left'
        self.si_prefix = 'mirror_si_'
        self.policy_name1 = 'pol1'
        self.policy_name2 = 'pol-analyzer'
        self.svc_mode = 'in-network'
        self.svc_type = 'analyzer'

        self.st_fixture, self.si_fixtures = self.config_st_si(self.st_name,
                                                              self.si_prefix, si_count, left_vn=self.vn1_fq_name, svc_type=self.svc_type, svc_mode=self.svc_mode)
        self.action_list = self.chain_si(si_count, self.si_prefix)
        self.rules1 = [{'direction': '<>',
                       'protocol': 'any',
                        'source_network': self.vn1_name,
                        'src_ports': [0, -1],
                        'dest_network': self.vn2_name,
                        'dst_ports': [0, -1],
                        'simple_action': 'pass',
                        'action_list': {'simple_action': 'pass'}
                        }
                       ]

        self.rules2 = [{'direction': '<>',
                       'protocol': 'any',
                        'source_network': self.vn1_name,
                        'src_ports': [0, -1],
                        'dest_network': self.vn2_name,
                        'dst_ports': [0, -1],
                        'simple_action': 'pass',
                        'action_list': {'simple_action': 'pass',
                                        'mirror_to': {'analyzer_name': self.action_list[0]}}
                        }
                       ]

        self.pol1_fixture = self.config_policy(self.policy_name1, self.rules1)
        self.pol_analyzer_fixture = self.config_policy(
            self.policy_name2, self.rules2)

        self.vn1_policy_fix = self.attach_policy_to_vn(
            self.pol1_fixture, self.vn1_fixture)
        self.vn2_policy_fix = self.attach_policy_to_vn(
            self.pol1_fixture, self.vn2_fixture)

        if getattr(self, 'res', None):
            self.vm1_fixture = self.res.get_vn1_vm1_fixture()
            self.vm2_fixture = self.res.get_vn2_vm2_fixture()
        else:
            self.vm1_fixture = self.config_vm(
                self.vn1_fixture, self.vm1_name, image_name='ubuntu-traffic')
            self.vm2_fixture = self.config_vm(
                self.vn2_fixture, self.vm2_name, image_name='ubuntu')
        assert self.vm1_fixture.verify_on_setup()
        assert self.vm2_fixture.verify_on_setup()

        self.vm1_fixture.wait_till_vm_is_up()
        self.vm2_fixture.wait_till_vm_is_up()

        result, msg = self.validate_vn(self.vn1_name)
        assert result, msg
        result, msg = self.validate_vn(self.vn2_name)
        assert result, msg
        self.verify_si(self.si_fixtures)

        # Verify ICMP traffic b/w VN1 and VN2
        errmsg = "Ping b/w VN1 and VN2 failed in step1"
        assert self.vm1_fixture.ping_with_certainty(
            self.vm2_fixture.vm_ip), errmsg
        assert self.vm2_fixture.ping_with_certainty(
            self.vm1_fixture.vm_ip), errmsg

        self.detach_policy(self.vn1_policy_fix)
        # Verify no ICMP traffic b/w VN1 and VN2
        errmsg = "Ping b/w VN1 and VN2 success in step2"
        assert self.vm1_fixture.ping_with_certainty(
            self.vm2_fixture.vm_ip, expectation=False), errmsg
        assert self.vm2_fixture.ping_with_certainty(
            self.vm1_fixture.vm_ip, expectation=False), errmsg

        self.detach_policy(self.vn2_policy_fix)
        self.vn2_policy_fix = self.attach_policy_to_vn(
            self.pol1_fixture, self.vn2_fixture)
        # Verify no ICMP traffic b/w VN1 and VN2
        errmsg = "Ping b/w VN1 and VN2 success in step3"
        assert self.vm1_fixture.ping_with_certainty(
            self.vm2_fixture.vm_ip, expectation=False), errmsg
        assert self.vm2_fixture.ping_with_certainty(
            self.vm1_fixture.vm_ip, expectation=False), errmsg

        self.detach_policy(self.vn2_policy_fix)
        self.vn2_policy_fix = self.attach_policy_to_vn(
            self.pol_analyzer_fixture, self.vn2_fixture)
        # Verify ICMP traffic b/w VN1 and VN2
        errmsg = "Ping b/w VN1 and VN2 failed in step4"
        assert self.vm1_fixture.ping_with_certainty(
            self.vm2_fixture.vm_ip), errmsg
        assert self.vm2_fixture.ping_with_certainty(
            self.vm1_fixture.vm_ip), errmsg

        self.detach_policy(self.vn2_policy_fix)
        # Verify no ICMP traffic b/w VN1 and VN2
        errmsg = "Ping b/w VN1 and VN2 success in step5"
        assert self.vm1_fixture.ping_with_certainty(
            self.vm2_fixture.vm_ip, expectation=False), errmsg
        assert self.vm2_fixture.ping_with_certainty(
            self.vm1_fixture.vm_ip, expectation=False), errmsg

        self.vn2_policy_fix = self.attach_policy_to_vn(
            self.pol1_fixture, self.vn2_fixture)
        # Verify no ICMP traffic b/w VN1 and VN2
        errmsg = "Ping b/w VN1 and VN2 success in step6"
        assert self.vm1_fixture.ping_with_certainty(
            self.vm2_fixture.vm_ip, expectation=False), errmsg
        assert self.vm2_fixture.ping_with_certainty(
            self.vm1_fixture.vm_ip, expectation=False), errmsg

        return True

    def verify_detach_attach_policy_change_rules(self, si_count=1):
        self.domain_name = "default-domain"
        self.project_name = "admin"

        if getattr(self, 'res', None):
            self.vn1_name = self.res.vn1_name
            self.vn1_subnets = self.res.vn1_subnets
            self.vm1_name = self.res.vn1_vm1_name
            self.vn2_name = self.res.vn2_name
            self.vn2_subnets = self.res.vn2_subnets
            self.vm2_name = self.res.vn2_vm2_name
        else:
            self.vn1_name = "VN1%s" % si_count
            self.vn1_subnets = ['10.1.1.0/24']
            self.vm1_name = 'VM-traffic'
            self.vn2_name = "VN2%s" % si_count
            self.vn2_subnets = ['20.2.2.0/24']
            self.vm2_name = 'VM-ubuntu'

        self.vn1_fq_name = ':'.join(
            [self.domain_name, self.project_name, self.vn1_name])
        self.vn2_fq_name = ':'.join(
            [self.domain_name, self.project_name, self.vn2_name])
        if getattr(self, 'res', None):
            self.vn1_fixture = self.res.get_vn1_fixture()
            self.vn2_fixture = self.res.get_vn2_fixture()
            assert self.vn1_fixture.verify_on_setup()
            assert self.vn2_fixture.verify_on_setup()
        else:
            self.vn1_fixture = self.config_vn(self.vn1_name, self.vn1_subnets)
            self.vn2_fixture = self.config_vn(self.vn2_name, self.vn2_subnets)

        si_count = si_count
        self.action_list = []
        self.if_list = []
        self.st_name = 'st-analyzer-left'
        self.si_prefix = 'mirror_si_'
        self.policy_name1 = 'pol1'
        self.policy_name2 = 'pol-analyzer'
        self.svc_mode = 'in-network'
        self.svc_type = 'analyzer'

        self.st_fixture, self.si_fixtures = self.config_st_si(self.st_name,
                                                              self.si_prefix, si_count, left_vn=self.vn1_fq_name, svc_type=self.svc_type, svc_mode=self.svc_mode)
        self.action_list = self.chain_si(si_count, self.si_prefix)
        self.rules1 = [{'direction': '<>',
                       'protocol': 'any',
                        'source_network': self.vn1_name,
                        'src_ports': [0, -1],
                        'dest_network': self.vn2_name,
                        'dst_ports': [0, -1],
                        'simple_action': 'pass',
                        'action_list': {'simple_action': 'pass'}
                        }
                       ]

        self.rules2 = [{'direction': '<>',
                       'protocol': 'any',
                        'source_network': self.vn1_name,
                        'src_ports': [0, -1],
                        'dest_network': self.vn2_name,
                        'dst_ports': [0, -1],
                        'simple_action': 'pass',
                        'action_list': {'simple_action': 'pass',
                                        'mirror_to': {'analyzer_name': self.action_list[0]}}
                        }
                       ]

        self.pol1_fixture = self.config_policy(self.policy_name1, self.rules1)
        self.pol_analyzer_fixture = self.config_policy(
            self.policy_name2, self.rules2)
        self.vn1_policy_fix = self.attach_policy_to_vn(
            self.pol_analyzer_fixture, self.vn1_fixture)
        self.vn2_policy_fix = self.attach_policy_to_vn(
            self.pol_analyzer_fixture, self.vn2_fixture)

        if getattr(self, 'res', None):
            self.vm1_fixture = self.res.get_vn1_vm1_fixture()
            self.vm2_fixture = self.res.get_vn2_vm2_fixture()
        else:
            self.vm1_fixture = self.config_vm(
                self.vn1_fixture, self.vm1_name, image_name='ubuntu-traffic')
            self.vm2_fixture = self.config_vm(
                self.vn2_fixture, self.vm2_name, image_name='ubuntu')
        assert self.vm1_fixture.verify_on_setup()
        assert self.vm2_fixture.verify_on_setup()

        self.vm1_fixture.wait_till_vm_is_up()
        self.vm2_fixture.wait_till_vm_is_up()

        result, msg = self.validate_vn(self.vn1_name)
        assert result, msg
        result, msg = self.validate_vn(self.vn2_name)
        assert result, msg
        self.verify_si(self.si_fixtures)

        # Verify ICMP traffic b/w VN1 and VN2
        errmsg = "Ping b/w VN1 and VN2 failed in step1"
        assert self.vm1_fixture.ping_with_certainty(
            self.vm2_fixture.vm_ip), errmsg
        assert self.vm2_fixture.ping_with_certainty(
            self.vm1_fixture.vm_ip), errmsg

        self.detach_policy(self.vn1_policy_fix)
        # Verify ICMP traffic b/w VN1 and VN2
        errmsg = "Ping b/w VN1 and VN2 failed in step2"
        assert self.vm1_fixture.ping_with_certainty(
            self.vm2_fixture.vm_ip), errmsg
        assert self.vm2_fixture.ping_with_certainty(
            self.vm1_fixture.vm_ip), errmsg

        # change policy rules to rules1 and Verify no ICMP traffic b/w VN1 and
        # VN2
        data = {'policy':
                {'entries': self.pol1_fixture.policy_obj['policy']['entries']}}
        self.pol_analyzer_fixture.update_policy(
            self.pol_analyzer_fixture.policy_obj['policy']['id'], data)
        errmsg = "Ping b/w VN1 and VN2 success in step3"
        assert self.vm1_fixture.ping_with_certainty(
            self.vm2_fixture.vm_ip, expectation=False), errmsg
        assert self.vm2_fixture.ping_with_certainty(
            self.vm1_fixture.vm_ip, expectation=False), errmsg

        self.detach_policy(self.vn2_policy_fix)
        self.vn2_policy_fix = self.attach_policy_to_vn(
            self.pol_analyzer_fixture, self.vn2_fixture)
        # Verify no ICMP traffic b/w VN1 and VN2
        errmsg = "Ping b/w VN1 and VN2 success in step5"
        assert self.vm1_fixture.ping_with_certainty(
            self.vm2_fixture.vm_ip, expectation=False), errmsg
        assert self.vm2_fixture.ping_with_certainty(
            self.vm1_fixture.vm_ip, expectation=False), errmsg

        return True

    def verify_policy_order_change(self, si_count=1):
        self.domain_name = "default-domain"
        self.project_name = "admin"

        if getattr(self, 'res', None):
            self.vn1_name = self.res.vn1_name
            self.vn1_subnets = self.res.vn1_subnets
            self.vm1_name = self.res.vn1_vm1_name
            self.vn2_name = self.res.vn2_name
            self.vn2_subnets = self.res.vn2_subnets
            self.vm2_name = self.res.vn2_vm2_name
        else:
            self.vn1_name = "VN1%s" % si_count
            self.vn1_subnets = ['10.1.1.0/24']
            self.vm1_name = 'VM-traffic'
            self.vn2_name = "VN2%s" % si_count
            self.vn2_subnets = ['20.2.2.0/24']
            self.vm2_name = 'VM-ubuntu'

        self.vn1_fq_name = ':'.join(
            [self.domain_name, self.project_name, self.vn1_name])
        self.vn2_fq_name = ':'.join(
            [self.domain_name, self.project_name, self.vn2_name])

        if getattr(self, 'res', None):
            self.vn1_fixture = self.res.get_vn1_fixture()
            self.vn2_fixture = self.res.get_vn2_fixture()
            assert self.vn1_fixture.verify_on_setup()
            assert self.vn2_fixture.verify_on_setup()
        else:
            self.vn1_fixture = self.config_vn(self.vn1_name, self.vn1_subnets)
            self.vn2_fixture = self.config_vn(self.vn2_name, self.vn2_subnets)

        si_count = si_count
        self.action_list = []
        self.if_list = []
        self.st_name = 'st-analyzer-left'
        self.si_prefix = 'mirror_si_'
        self.policy_name1 = 'pol1'
        self.policy_name2 = 'pol-analyzer'
        self.svc_mode = 'transparent'
        self.svc_type = 'analyzer'

        self.st_fixture, self.si_fixtures = self.config_st_si(self.st_name,
                                                              self.si_prefix, si_count, left_vn=self.vn1_fq_name, svc_type=self.svc_type, svc_mode=self.svc_mode)
        self.action_list = self.chain_si(si_count, self.si_prefix)
        self.rules1 = [{'direction': '<>',
                       'protocol': 'any',
                        'source_network': 'any',
                        'src_ports': [0, -1],
                        'dest_network': 'any',
                        'dst_ports': [0, -1],
                        'simple_action': 'pass',
                        'action_list': {'simple_action': 'pass'}
                        }
                       ]

        self.rules2 = [{'direction': '<>',
                       'protocol': 'any',
                        'source_network': self.vn1_name,
                        'src_ports': [0, -1],
                        'dest_network': self.vn2_name,
                        'dst_ports': [0, -1],
                        'simple_action': 'pass',
                        'action_list': {'simple_action': 'pass',
                                        'mirror_to': {'analyzer_name': self.action_list[0]}}
                        }
                       ]

        self.pol1_fixture = self.config_policy(self.policy_name1, self.rules1)
        self.pol_analyzer_fixture = self.config_policy(
            self.policy_name2, self.rules2)
        self.vn1_policy_fix = self.attach_policy_to_vn(
            self.pol_analyzer_fixture, self.vn1_fixture)
        self.vn2_policy_fix = self.attach_policy_to_vn(
            self.pol_analyzer_fixture, self.vn2_fixture)

        if getattr(self, 'res', None):
            self.vm1_fixture = self.res.get_vn1_vm1_fixture()
            self.vm2_fixture = self.res.get_vn2_vm2_fixture()
        else:
            self.vm1_fixture = self.config_vm(
                self.vn1_fixture, self.vm1_name, image_name='ubuntu-traffic')
            self.vm2_fixture = self.config_vm(
                self.vn2_fixture, self.vm2_name, image_name='ubuntu')
        assert self.vm1_fixture.verify_on_setup()
        assert self.vm2_fixture.verify_on_setup()

        self.vm1_fixture.wait_till_vm_is_up()
        self.vm2_fixture.wait_till_vm_is_up()

        result, msg = self.validate_vn(self.vn1_name)
        assert result, msg
        result, msg = self.validate_vn(self.vn2_name)
        assert result, msg
        self.verify_si(self.si_fixtures)

        # Verify ICMP traffic b/w VN1 and VN2 and mirror
        errmsg = "Ping b/w VN1 and VN2 failed in step1"
        sessions = self.tcpdump_on_all_analyzer(self.si_prefix, si_count)
        assert self.vm1_fixture.ping_with_certainty(
            self.vm2_fixture.vm_ip), errmsg
        assert self.vm2_fixture.ping_with_certainty(
            self.vm1_fixture.vm_ip), errmsg
        for svm_name, (session, pcap) in sessions.items():
            count = 20
            self.verify_icmp_mirror(svm_name, session, pcap, count)

        self.detach_policy(self.vn1_policy_fix)
        self.detach_policy(self.vn2_policy_fix)
        self.vn1_policy_fix = self.attach_policy_to_vn(
            self.pol1_fixture, self.vn1_fixture)
        self.vn2_policy_fix = self.attach_policy_to_vn(
            self.pol1_fixture, self.vn2_fixture)
        self.vn1_policy_a_fix = self.attach_policy_to_vn(
            self.pol_analyzer_fixture, self.vn1_fixture)
        self.vn2_policy_a_fix = self.attach_policy_to_vn(
            self.pol_analyzer_fixture, self.vn2_fixture)

        # Verify ICMP traffic b/w VN1 and VN2 but no mirror
        errmsg = "Ping b/w VN1 and VN2 failed in step2"
        sessions = self.tcpdump_on_all_analyzer(self.si_prefix, si_count)
        assert self.vm1_fixture.ping_with_certainty(
            self.vm2_fixture.vm_ip), errmsg
        assert self.vm2_fixture.ping_with_certainty(
            self.vm1_fixture.vm_ip), errmsg
        for svm_name, (session, pcap) in sessions.items():
            count = 0
            self.verify_icmp_mirror(svm_name, session, pcap, count)

        self.detach_policy(self.vn1_policy_fix)
        self.vn1_policy_fix = self.attach_policy_to_vn(
            self.pol1_fixture, self.vn1_fixture)
        self.detach_policy(self.vn2_policy_fix)
        self.vn2_policy_fix = self.attach_policy_to_vn(
            self.pol1_fixture, self.vn2_fixture)

        # Verify ICMP traffic b/w VN1 and VN2 and mirror
        errmsg = "Ping b/w VN1 and VN2 failed in step3 and step4"
        sessions = self.tcpdump_on_all_analyzer(self.si_prefix, si_count)
        assert self.vm1_fixture.ping_with_certainty(
            self.vm2_fixture.vm_ip), errmsg
        assert self.vm2_fixture.ping_with_certainty(
            self.vm1_fixture.vm_ip), errmsg
        for svm_name, (session, pcap) in sessions.items():
            count = 20
            self.verify_icmp_mirror(svm_name, session, pcap, count)

        self.detach_policy(self.vn1_policy_fix)
        self.detach_policy(self.vn2_policy_fix)

        # Verify ICMP traffic b/w VN1 and VN2 and mirror
        errmsg = "Ping b/w VN1 and VN2 failed in step5"
        sessions = self.tcpdump_on_all_analyzer(self.si_prefix, si_count)
        assert self.vm1_fixture.ping_with_certainty(
            self.vm2_fixture.vm_ip), errmsg
        assert self.vm2_fixture.ping_with_certainty(
            self.vm1_fixture.vm_ip), errmsg
        for svm_name, (session, pcap) in sessions.items():
            count = 20
            self.verify_icmp_mirror(svm_name, session, pcap, count)

        return True

    def cleanUp(self):
        super(VerifySvcMirror, self).cleanUp()
