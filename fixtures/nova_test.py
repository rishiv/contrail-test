import fixtures
from novaclient import client as mynovaclient
from novaclient import exceptions as novaException
from fabric.context_managers import settings, hide, cd, shell_env
from fabric.api import run, local
from fabric.operations import get, put
from fabric.contrib.files import exists
from util import *
from tcutils.cfgparser import parse_cfg_file
import socket
import time
import re

#from contrail_fixtures import contrail_fix_ext

#@contrail_fix_ext (ignore_verify=True, ignore_verify_on_setup=True)


class NovaFixture(fixtures.Fixture):

    def __init__(self, inputs,
                 project_name,
                 key='key1',
                 username=None,
                 password=None):
        httpclient = None
        self.inputs = inputs
        self.username = username or inputs.stack_user
        self.password = password or inputs.stack_password
        self.project_name = project_name
        self.cfgm_ip = inputs.cfgm_ip
        self.openstack_ip = inputs.openstack_ip
        self.cfgm_host_user = inputs.username
        self.cfgm_host_passwd = inputs.password
        self.key = key
        self.obj = None
        self.auth_url = 'http://' + self.openstack_ip + ':5000/v2.0'
        self.logger = inputs.logger
        self.images_info = parse_cfg_file('../configs/images.cfg')
        self.flavor_info = parse_cfg_file('../configs/flavors.cfg')
    # end __init__

    def setUp(self):
        super(NovaFixture, self).setUp()
        self.obj = mynovaclient.Client('2',
                                       username=self.username,
                                       project_id=self.project_name,
                                       api_key=self.password,
                                       auth_url=self.auth_url)
        self._create_keypair(self.key)
        self.compute_nodes = self.get_compute_host()
    # end setUp

    def cleanUp(self):
        super(NovaFixture, self).cleanUp()

    def get_handle(self):
        return self.obj
    # end get_handle

    def get_image(self, image_name):
        default_image = 'ubuntu-traffic'
        try:
            image = self.obj.images.find(name=image_name)
        except novaException.NotFound:
            # In the field, not all kinds of images would be available
            # Just use a default image in such a case
            if not self._install_image(image_name=image_name):
                self._install_image(image_name=default_image)
            image = self.obj.images.find(name=image_name)
        return image
    # end get_image

    def get_flavor(self, name='contrail_flavor_small'):
        try:
            flavor = self.obj.flavors.find(name=name)
        except novaException.NotFound:
            self._install_flavor(name=name)
            flavor = self.obj.flavors.find(name=name)
        return flavor
    # end get_flavor

    def get_vm_if_present(self, vm_name, project_id=None):
        try:
            vm_list = self.obj.servers.list(search_opts={"all_tenants": True})
            for vm in vm_list:
                if project_id:
                    if vm.name == vm_name and vm.tenant_id == self.strip(project_id):
                        return vm
                else:
                    if vm.name == vm_name:
                        return vm
        except novaException.NotFound:
            return None
        except Exception:
            self.logger.exception('Exception while finding a VM')
            return None
        return None
    # end get_vm_if_present

    def get_vm_by_id(self, vm_id, project):
        try:
            vm = None
            vm = self.obj.servers.find(id=vm_id)
            if vm:
                return vm
        except novaException.NotFound:
            return None
        except Exception:
            self.logger.exception('Exception while finding a VM')
            return None
    # end get_vm_by_id

    def _install_flavor(self, name):
        flavor_info = self.flavor_info[name]
        try:
            self.obj.flavors.create(name=name,
                                    vcpus=flavor_info['vcpus'],
                                    ram=flavor_info['ram'],
                                    disk=flavor_info['disk'])
        except Exception, e:
            self.logger.exception('Exception adding flavor %s' % (name))
            raise e
    # end _install_flavor

    def _install_image(self, image_name):
        result = False
        image_info = self.images_info[image_name]
        webserver = image_info['webserver']
        location = image_info['location']
        image = image_info['name']

        with settings(
            host_string='%s@%s' % (self.cfgm_host_user, self.openstack_ip),
                password=self.cfgm_host_passwd, warn_only=True, abort_on_prompts=False):
            # Work arround to choose build server.
            if webserver == '':
                if '10.204' in self.openstack_ip:
                    webserver = r'http://10.204.216.51/'
                else:
                    webserver = r'http://10.84.5.100/'

            build_path = '%s/%s/%s' % (webserver, location, image)
            if image_name == 'cirros-0.3.0-x86_64-uec':
                run('source /etc/contrail/openstackrc')
                run('cd /tmp ; sudo rm -f /tmp/cirros-0.3.0-x86_64*')
                if not os.path.isfile("/root/cirros-0.3.0-x86_64-uec.tar.gz"):
                    run('cd /tmp; wget %s' % build_path)
                else:
                    run("cp /root/cirros-0.3.0-x86_64-uec.tar.gz /tmp/cirros-0.3.0-x86_64-uec.tar.gz")

                run('tar xvzf /tmp/cirros-0.3.0-x86_64-uec.tar.gz -C /tmp/')
                run('source /etc/contrail/openstackrc && glance add name=cirros-0.3.0-x86_64-kernel is_public=true ' +
                    'container_format=aki disk_format=aki < /tmp/cirros-0.3.0-x86_64-vmlinuz')
                run('source /etc/contrail/openstackrc && glance add name=cirros-0.3.0-x86_64-ramdisk is_public=true ' +
                    ' container_format=ari disk_format=ari < /tmp/cirros-0.3.0-x86_64-initrd')
                run('source /etc/contrail/openstackrc && glance add name=' + image_name + ' is_public=true ' +
                    'container_format=ami disk_format=ami ' +
                    '\"kernel_id=$(glance index | awk \'/cirros-0.3.0-x86_64-kernel/ {print $1}\')\" ' +
                    '\"ramdisk_id=$(glance index | awk \'/cirros-0.3.0-x86_64-ramdisk/ {print $1}\')\" ' +
                    ' < <(zcat --force /tmp/cirros-0.3.0-x86_64-blank.img)')
                return True
            # end if

            return self.copy_and_glance(build_path, image_name, image)
    # end _install_image

    def get_image_account(self, image_name):
        '''
        Return the username and password considered for the image name
        '''
        return([self.images_info[image_name]['username'],
                self.images_info[image_name]['password']])
    # end get_image_account

    def copy_and_glance(self, build_path, generic_image_name, image_name):
        """copies the image to the host and glances.
           Requires Image path
        """
        run('pwd')
        cmd = '(source /etc/contrail/openstackrc; wget -O - %s | gunzip | glance add name="%s" \
                    is_public=true container_format=ovf disk_format=qcow2)' % (build_path, generic_image_name)
        if self.inputs.http_proxy != 'None':
            with shell_env(http_proxy=self.inputs.http_proxy):
                run(cmd)
        else:
            run(cmd)

        return True

    def _create_keypair(self, key_name):
        if key_name in [str(key.id) for key in self.obj.keypairs.list()]:
            return
        with hide('everything'):
            with settings(
                host_string='%s@%s' % (self.cfgm_host_user, self.cfgm_ip),
                    password=self.cfgm_host_passwd, warn_only=True, abort_on_prompts=False):
                rsa_pub_file = os.environ.get('HOME') + '/.ssh/id_rsa.pub'
                rsa_pub_arg = os.environ.get('HOME') + '/.ssh/id_rsa'
                if exists('.ssh/id_rsa.pub'):  # If file exists on remote m/c
                    get('.ssh/id_rsa.pub', '/tmp/')
                else:
                    run('rm -f .ssh/id_rsa.pub')
                    run('ssh-keygen -f %s -t rsa -N \'\'' % (rsa_pub_arg))
                    get('.ssh/id_rsa.pub', '/tmp/')
                pub_key = open('/tmp/id_rsa.pub', 'r').read()
                self.obj.keypairs.create(key_name, public_key=pub_key)
                local('rm /tmp/id_rsa.pub')
    # end _create_keypair

    def get_nova_services(self, **kwargs):
        try:
            nova_services = self.obj.services.list(**kwargs)
            self.logger.info('Servies List from the nova obj: %s' %
                             nova_services)
            return nova_services
        except:
            self.logger.warn('Unable to retrieve services from nova obj')
            self.logger.info('Using \"nova service-list\" to retrieve'
                             ' services info')
            pass

        service_list = []
        with settings(
            host_string='%s@%s' % (self.cfgm_host_user, self.openstack_ip),
                password=self.cfgm_host_passwd):
            services_info = run(
                'source /etc/contrail/openstackrc; nova service-list')
        services_info = services_info.split('\r\n')
        get_rows = lambda row: map(str.strip, filter(None, row.split('|')))
        columns = services_info[1].split('|')
        columns = map(str.strip, filter(None, columns))
        columns = map(str.lower, columns)
        columns_no_binary = map(str.lower, columns)
        columns_no_binary.remove('binary')
        rows = map(get_rows, services_info[3:-1])
        nova_class = type('NovaService', (object,), {})
        for row in rows:
            datadict = dict(zip(columns, row))
            for fk, fv in kwargs.items():
                if datadict[fk] != fv:
                    break
                else:
                    service_obj = nova_class()
                    for key, value in datadict.items():
                        setattr(service_obj, key, value)

                    # Append the service into the list.
                    service_list.append(service_obj)
        return service_list

    def create_vm(self, project_uuid, image_name, vm_name, vn_ids, 
                  node_name=None, sg_ids=None, count=1, userdata=None, 
                 flavor='contrail_flavor_small',port_ids=None, fixed_ips=None):
        image = self.get_image(image_name=image_name)
        flavor = self.get_flavor(name=flavor)
        # flavor=self.obj.flavors.find(name=flavor_name)

        if node_name == 'disable':
            zone = None
        elif node_name:
            zone = None
            nova_services = self.get_nova_services(binary='nova-compute')
            for compute_svc in nova_services:
                if compute_svc.host == node_name:
                    zone = "nova:" + node_name
                    break
                elif (compute_svc.host in self.inputs.compute_ips and
                      self.inputs.host_data[node_name]['host_ip'] == compute_svc.host):
                    zone = "nova:" + compute_svc.host
            if not zone:
                raise RuntimeError(
                    "Compute host %s is not listed in nova serivce list" % node_name)
        else:
            zone = "nova:" + next(self.compute_nodes)
        if userdata:
            with open(userdata) as f:
                userdata = f.readlines()
            userdata = ''.join(userdata)
# userdata = "#!/bin/sh\necho 'Hello World.  The time is now $(date -R)!'
# | tee /tmp/output.txt\n"
        if fixed_ips:
            if vn_ids:
                nics_list = [{'net-id': x, 'v4-fixed-ip':y} for x,y in zip(vn_ids, fixed_ips)]
            elif port_ids:
                nics_list = [{'port-id': x, 'v4-fixed-ip':y} for x,y in zip(port_ids, fixed_ips)]
        elif port_ids: 
            nics_list = [ {'port-id': x } for x in port_ids ]
        elif vn_ids:
            nics_list= [ {'net-id': x } for x in vn_ids ]

        self.obj.servers.create(name=vm_name, image=image,
                                security_groups=sg_ids,
                                flavor=flavor, nics=nics_list,
                                key_name=self.key, availability_zone=zone,
                                min_count=count, max_count=count, userdata=userdata)
        vm_objs = self.get_vm_list(name_pattern=vm_name,
                                   project_id=project_uuid)
        [vm_obj.get() for vm_obj in vm_objs]
        self.logger.info("VM Object: (%s) Nodename: (%s) Zone: (%s)" % (
                         str(vm_objs), node_name, zone))
        return vm_objs
    # end create_vm

    def add_security_group(self, vm_id, secgrp):
        self.obj.servers.add_security_group(vm_id, secgrp)

    def remove_security_group(self, vm_id, secgrp):
        self.obj.servers.remove_security_group(vm_id, secgrp)

    @retry(delay=5, tries=35)
    def get_vm_detail(self, vm_obj):
        try:
            vm_obj.get()
            if vm_obj.addresses == {} or vm_obj.status == 'BUILD':
                return False
            else:
                return True
        except novaException.ClientException:
            print 'Fatal Nova Exception'
            self.logger.exception('Exception while getting vm detail')
            return False
    # end def

    @retry(tries=10, delay=6)
    def is_ip_in_obj(self, vm_obj, vn_name):
        try:
            vm_obj.get()
            if len(vm_obj.addresses[vn_name]) > 0:
                return True
            else:
                self.logger.warn('Retrying to see if VM IP shows up in Nova ')
                return False
        except KeyError:
            self.logger.warn('Retrying to see if VM IP shows up in Nova ')
            return False
    # end is_ip_in_obj

    def get_vm_ip(self, vm_obj, vn_name):
        ''' Returns a list of IPs for the VM in VN.

        '''
#        return vm.obj[vn_name][0]['addr']
        if self.is_ip_in_obj(vm_obj, vn_name):
            try:
                return [x['addr'] for x in vm_obj.addresses[vn_name]]
            except KeyError:
                self.logger.error(
                    'VM does not seem to have got an IP in VN %s' % (vn_name))
                return []
        else:
            return []
    # end get_vm_ip

    def strip(self, uuid):
        return uuid.replace('-', '')

    def get_vm_list(self, name_pattern='', project_id=None):
        ''' Returns a list of VM objects currently present.

        '''
        final_vm_list = []
        vm_list = self.obj.servers.list(search_opts={"all_tenants": True})
        for vm_obj in vm_list:
            match_obj = re.match(r'%s' %
                                 name_pattern, vm_obj.name, re.M | re.I)
            if project_id:
                if match_obj and vm_obj.tenant_id == self.strip(project_id):
                    final_vm_list.append(vm_obj)
            else:
                if match_obj:
                    final_vm_list.append(vm_obj)
        # end for
        return final_vm_list

    # end get_vm_list

    def get_nova_host_of_vm(self, vm_obj):
        return vm_obj.__dict__['OS-EXT-SRV-ATTR:host']
    # end

    def delete_vm(self, vm_obj):
        vm_obj.delete()
    # end _delete_vm

    def put_key_file_to_host(self, host_ip):
        with hide('everything'):
            with settings(host_string='%s@%s' % (
                    self.cfgm_host_user, self.cfgm_ip),
                    password=self.cfgm_host_passwd,
                    warn_only=True, abort_on_prompts=False):
                get('.ssh/id_rsa', '/tmp/')
                get('.ssh/id_rsa.pub', '/tmp/')
        with hide('everything'):
            with settings(
                host_string='%s@%s' % (self.inputs.host_data[host_ip]['username'],
                                       host_ip), password=self.inputs.host_data[
                    host_ip]['password'],
                    warn_only=True, abort_on_prompts=False):
                # Put the key only is the test node and cfgm node in which key
                # is generated is different.
                if self.inputs.cfgm_ips[0] != host_ip:
                    put('/tmp/id_rsa', '/tmp/id_rsa')
                    put('/tmp/id_rsa.pub', '/tmp/id_rsa.pub')
                run('chmod 600 /tmp/id_rsa')
                self.tmp_key_file = '/tmp/id_rsa'

    @threadsafe_generator
    def get_compute_host(self):
        while True:
            nova_services = self.get_nova_services(binary='nova-compute')
            for compute_svc in nova_services:
                yield compute_svc.host
    # end get_compute_host

    @retry(tries=20, delay=5)
    def wait_till_vm_is_active(self, vm_obj):
        try:
            vm_obj.get()
            if vm_obj.status == 'ACTIVE':
                self.logger.info('VM %s is ACTIVE now' % vm_obj)
                return True
            else:
                self.logger.debug('VM %s is still in %s state' %
                                  (vm_obj, vm_obj.status))
                return False
        except novaException.NotFound:
            self.logger.debug('VM console log not formed yet')
            return False
        except novaException.ClientException:
            self.logger.error('Fatal Nova Exception while getting VM detail')
            return False
    # end wait_till_vm_is_active

    @retry(tries=20, delay=5)
    def wait_till_vm_is_up(self, vm_obj):
        try:
            vm_obj.get()
            if 'login:' in vm_obj.get_console_output():
                self.logger.info('VM has booted up..')
                return True
            else:
                self.logger.debug('VM not yet booted fully .. ')
                return False
        except novaException.NotFound:
            self.logger.debug('VM console log not formed yet')
            return False
        except novaException.ClientException:
            self.logger.error('Fatal Nova Exception while getting VM detail')
            return False
    # end wait_till_vm_is_up

    def get_vm_in_nova_db(self, vm_obj, node_ip):
        issue_cmd = 'mysql -u root --password=%s -e \'use nova; select vm_state, uuid, task_state from instances where uuid=\"%s\" ; \' ' % (
            self.inputs.mysql_token, vm_obj.id)
        username = self.inputs.host_data[node_ip]['username']
        password = self.inputs.host_data[node_ip]['password']
        output = self.inputs.run_cmd_on_server(
            server_ip=node_ip, issue_cmd=issue_cmd, username=username, password=password)
        return output
    # end get_vm_in_nova_db

    @retry(tries=10, delay=5)
    def is_vm_deleted_in_nova_db(self, vm_obj, node_ip):
        output = self.get_vm_in_nova_db(vm_obj, node_ip)
        if 'deleted' in output and 'NULL' in output:
            self.logger.info('VM %s is removed in Nova DB' % (vm_obj.name))
            return True
        else:
            self.logger.warn('VM %s is still found in Nova DB : %s' %
                             (vm_obj.name, output))
            return False
    # end is_vm_in_nova_db

# end NovaFixture
