# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright (c) 2013 dotCloud, Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
A Docker Hypervisor which allows running Linux Containers instead of VMs.
"""

# Cloudbuilders fix imports
import re 
from nova.openstack.common import processutils
from nova.network import neutronv2
import netaddr
# end

import os
import random
import socket
import time

from oslo.config import cfg

from nova.compute import power_state
from nova.compute import task_states
from nova import exception
from nova.image import glance
from nova.openstack.common.gettextutils import _
from nova.openstack.common import jsonutils
from nova.openstack.common import log
from nova import utils
import nova.virt.docker.client
from nova.virt.docker import hostinfo
from nova.virt import driver


docker_opts = [
    cfg.IntOpt('docker_registry_default_port',
               default=5042,
               help=_('Default TCP port to find the '
                      'docker-registry container')),
]

CONF = cfg.CONF
CONF.register_opts(docker_opts)
CONF.import_opt('my_ip', 'nova.netconf')

LOG = log.getLogger(__name__)


class DockerDriver(driver.ComputeDriver):
    """Docker hypervisor driver."""

    capabilities = {
        'has_imagecache': True,
        'supports_recreate': True,
    }

    def __init__(self, virtapi):
        super(DockerDriver, self).__init__(virtapi)
        self._docker = None

    @property
    def docker(self):
        if self._docker is None:
            self._docker = nova.virt.docker.client.DockerHTTPClient()
        return self._docker

    def init_host(self, host):
        if self.is_daemon_running() is False:
            raise exception.NovaException(_('Docker daemon is not running or '
                'is not reachable (check the rights on /var/run/docker.sock)'))

    def is_daemon_running(self):
        try:
            self.docker.list_containers()
            return True
        except socket.error:
            # NOTE(samalba): If the daemon is not running, we'll get a socket
            # error. The list_containers call is safe to call often, there
            # is an internal hard limit in docker if the amount of containers
            # is huge.
            return False

    def list_instances(self, inspect=False):
        res = []
        for container in self.docker.list_containers():
            info = self.docker.inspect_container(container['id'])
            if inspect:
                res.append(info)
            else:
                res.append(info['Config'].get('Hostname'))
        return res

    def plug_vifs(self, instance, network_info):
        """Plug VIFs into networks."""
        pass

    def unplug_vifs(self, instance, network_info):
        """Unplug VIFs from networks."""
        pass

    def find_container_by_name(self, name):
        for info in self.list_instances(inspect=True):
            if info['Config'].get('Hostname') == name:
                return info
        return {}

    def get_info(self, instance):
        container = self.find_container_by_name(instance['name'])
        if not container:
            raise exception.InstanceNotFound(instance_id=instance['name'])
        running = container['State'].get('Running')
        info = {
            'max_mem': 0,
            'mem': 0,
            'num_cpu': 1,
            'cpu_time': 0
        }
        info['state'] = power_state.RUNNING if running \
            else power_state.SHUTDOWN
        return info

    def get_host_stats(self, refresh=False):
        hostname = socket.gethostname()
        memory = hostinfo.get_memory_usage()
        disk = hostinfo.get_disk_usage()
        stats = self.get_available_resource(hostname)
        stats['hypervisor_hostname'] = stats['hypervisor_hostname']
        stats['host_hostname'] = stats['hypervisor_hostname']
        stats['host_name_label'] = stats['hypervisor_hostname']
        return stats

    def get_available_resource(self, nodename):
        if not hasattr(self, '_nodename'):
            self._nodename = nodename
        if nodename != self._nodename:
            LOG.error(_('Hostname has changed from %(old)s to %(new)s. '
                        'A restart is required to take effect.'
                        ) % {'old': self._nodename,
                             'new': nodename})

        memory = hostinfo.get_memory_usage()
        disk = hostinfo.get_disk_usage()
        stats = {
            'vcpus': 1,
            'vcpus_used': 0,
            'memory_mb': memory['total'] / (1024 ** 2),
            'memory_mb_used': memory['used'] / (1024 ** 2),
            'local_gb': disk['total'] / (1024 ** 3),
            'local_gb_used': disk['used'] / (1024 ** 3),
            'disk_available_least': disk['available'] / (1024 ** 3),
            'hypervisor_type': 'docker',
            'hypervisor_version': '1.0',
            'hypervisor_hostname': self._nodename,
            'cpu_info': '?',
            'supported_instances': jsonutils.dumps([
                    ('i686', 'docker', 'lxc'),
                    ('x86_64', 'docker', 'lxc')
                ])
        }
        return stats

    def _find_cgroup_devices_path(self):
        for ln in open('/proc/mounts'):
            if ln.startswith('cgroup ') and 'devices' in ln:
                return ln.split(' ')[1]

    def _find_container_pid(self, container_id):
        cgroup_path = self._find_cgroup_devices_path()
        lxc_path = os.path.join(cgroup_path, 'lxc')
        tasks_path = os.path.join(lxc_path, container_id, 'tasks')
        n = 0
        while True:
            # NOTE(samalba): We wait for the process to be spawned inside the
            # container in order to get the the "container pid". This is
            # usually really fast. To avoid race conditions on a slow
            # machine, we allow 10 seconds as a hard limit.
            if n > 20:
                return
            try:
                with open(tasks_path) as f:
                    pids = f.readlines()
                    if pids:
                        return int(pids[0].strip())
            except IOError:
                pass
            time.sleep(0.5)
            n += 1

    def _find_fixed_ip(self, subnets):
        for subnet in subnets:
            for ip in subnet['ips']:
                if ip['type'] == 'fixed' and ip['address']:
                    return ip['address']

    def _find_vlan_id(self, netid, project_id, context):
        neutron = neutronv2.get_client(context)
        #Builders Hack we NEED the vlan id to tag the pvnet interfaces
        #so we import neutron client to call the neutron api and get it
        #local def import to avoid circular and irrelevant imports
        if netid:
            # If user has specified to attach instance only to specific
            # networks then only add these to **search_opts. This search will
            # also include 'shared' networks.
            search_opts = {'id': netid}
            nets = neutron.list_networks(**search_opts).get('networks', [])
            vlan_id = nets[0]["provider:segmentation_id"]
        else:
            # (1) Retrieve non-public network list owned by the tenant.
            search_opts = {'tenant_id': project_id, 'shared': False}
            nets = neutron.list_networks(**search_opts).get('networks', [])
            vlan_id = nets[0]["provider:segmentation_id"]

        return vlan_id
        

    def _clear_vifs(self, container_id):
        stderr=None
        try:
            stdout, stderr =  utils.execute('ip', 'netns', 'exec', container_id, 'ifconfig', run_as_root=True)
        except processutils.ProcessExecutionError:
            if stderr:
                LOG.info(_('ERROR: %s - Triying to delete network interfaces in a namespace'), stderr)
            else:
                LOG.info(_('ERROR: Triying to delete network interfaces in a namespace'))
        else:
            LOG.info(_('LOG: STDOUT (ifconfig): %s '), stdout)
            match = re.search(r'pvnetr\w*', stdout)
            if match:
                if_local = match.group()
                if_local_name = if_local.replace("r","l")
                LOG.info(_('LOG: Triying to delete network iface: %s '), if_local_name)
                try:
                    utils.execute('ovs-vsctl', 'del-port', if_local_name , run_as_root=True)
                except processutils.ProcessExecutionError:
                    LOG.info(_('ERROR: Triying to delete network iface running comand ovs-vsctl del-port %s'), if_local_name)

    def _setup_vifs(self, context, instance, network_info, container_id):
        network_info = network_info[0]['network']
        netns_path = '/var/run/netns'
        if not os.path.exists(netns_path):
            utils.execute(
                'mkdir', '-p', netns_path, run_as_root=True)
        nspid = self._find_container_pid(container_id)
        if not nspid:
            msg = _('Cannot find any PID under container "{0}"')
            raise RuntimeError(msg.format(container_id))
        utils.execute(
            'ln', '-sf', '/proc/{0}/ns/net'.format(nspid),
            '/var/run/netns/{0}'.format(container_id),
            run_as_root=True)
        rand = random.randint(0, 100000)
        if_local_name = 'pvnetl{0}'.format(rand)
        if_remote_name = 'pvnetr{0}'.format(rand)
        vlan_id = self._find_vlan_id(network_info['id'], network_info['meta']['tenant_id'], context)
        ip = self._find_fixed_ip(network_info['subnets'])
        if not ip:
            raise RuntimeError(_('Cannot set fixed ip'))
        undo_mgr = utils.UndoManager()

        gateway = network_info['subnets'][0]['gateway']['address']
        cidr = netaddr.IPNetwork(network_info['subnets'][0]['cidr'])
        bridge = network_info['bridge']
        try:
            utils.execute(
                'ip', 'link', 'add', 'name', if_local_name, 'type',
                'veth', 'peer', 'name', if_remote_name,
                run_as_root=True)
            undo_mgr.undo_with(lambda: utils.execute(
                'ip', 'link', 'delete', if_local_name, run_as_root=True))
            # NOTE(samalba): Deleting the interface will delete all associated
            # resources (remove from the bridge, its pair, etc...)
            utils.execute(
                'ip', 'link', 'set', if_local_name, 'up',
                run_as_root=True)
            utils.execute(
                'ip', 'link', 'set', if_remote_name, 'netns', nspid,
                run_as_root=True)
            utils.execute(
                'ip', 'netns', 'exec', container_id, 'ifconfig',
                if_remote_name, ip,'netmask', cidr.netmask,
                run_as_root=True)
            utils.execute(
                'ip', 'netns', 'exec', container_id, 'route', 'del', 'default',
                run_as_root=True)
            utils.execute(
                'ip', 'netns', 'exec', container_id, 'route', 'add', 'default', 'gw',
                gateway, run_as_root=True)
            utils.execute(
                'ovs-vsctl', 'add-port', bridge, if_local_name, 'tag=%s' % vlan_id,
                run_as_root=True)
        except Exception:
            msg = _('Failed to setup the network, rolling back')
            undo_mgr.rollback_and_reraise(msg=msg, instance=instance)

    def _setup_network(self, context, instance, network_info):
        container_id = self.find_container_by_name(instance['name']).get('id')
        self._setup_vifs(context, instance, network_info, container_id)

    def _get_memory_limit_bytes(self, instance):
        for metadata in instance.get('system_metadata', []):
            if metadata['deleted']:
                continue
            if metadata['key'] == 'instance_type_memory_mb':
                return int(metadata['value']) * 1024 * 1024
        return 0

    def _get_image_name(self, context, instance, image):
        fmt = image['container_format']
        if fmt != 'docker':
            msg = _('Image container format not supported ({0})')
            raise exception.InstanceDeployFailure(msg.format(fmt),
                instance_id=instance['name'])
        registry_port = self._get_registry_port()
        return '{0}:{1}/{2}'.format('docker-registry.melicloud.com',
                                    registry_port,
                                    image['name'])

    def _get_default_cmd(self, image_name):
        default_cmd = ['sh']
        info = self.docker.inspect_image(image_name)
        if not info:
            return default_cmd
        if not info['container_config']['Cmd']:
            return default_cmd
        else:
            default_cmd = info['container_config']['Cmd']
        return default_cmd

    def spawn(self, context, instance, image_meta, injected_files,
              admin_password, network_info=None, block_device_info=None):
        image_name = self._get_image_name(context, instance, image_meta)
        args = {
            'Hostname': instance['name'],
            'Image': image_name,
            'Memory': self._get_memory_limit_bytes(instance)
        }
        container_id = self.docker.create_container(args)
        if not container_id:
            msg = _('Image name "{0}" does not exist, fetching it...')
            LOG.info(msg.format(image_name))
            res = self.docker.pull_repository(image_name)
            if res is False:
                raise exception.InstanceDeployFailure(
                    _('Cannot pull missing image'),
                    instance_id=instance['name'])
            #HACKME - trying to get cmd ok 
            default_cmd = self._get_default_cmd(image_name)
            if default_cmd:
                args['Cmd'] = default_cmd
            container_id = self.docker.create_container(args)
            if not container_id:
                raise exception.InstanceDeployFailure(
                    _('Cannot create container'),
                    instance_id=instance['name'])
        self.docker.start_container(container_id)
        try:
            self._setup_network(context, instance, network_info)
        except Exception as e:
            msg = _('Cannot setup network: {0}')
            raise exception.InstanceDeployFailure(msg.format(e),instance_id=instance['name'])


    def destroy(self, instance, network_info, block_device_info=None,
                destroy_disks=True):
        container_id = self.find_container_by_name(instance['name']).get('id')
        if not container_id:
            return
        self._clear_vifs(container_id)
        self.docker.stop_container(container_id)
        self.docker.destroy_container(container_id)


    def reboot(self, context, instance, network_info, reboot_type,
               block_device_info=None, bad_volumes_callback=None):
        container_id = self.find_container_by_name(instance['name']).get('id')
        if not container_id:
            return
        self._clear_vifs(container_id)
        if not self.docker.stop_container(container_id):
            LOG.warning(_('Cannot stop the container, '
                          'please check docker logs'))
        if not self.docker.start_container(container_id):
            LOG.warning(_('Cannot restart the container, '
                          'please check docker logs'))
        self._setup_vifs(context, instance, network_info, container_id)

    def power_on(self, context, instance, network_info, block_device_info):
        container_id = self.find_container_by_name(instance['name']).get('id')
        if not container_id:
            return
        self.docker.start_container(container_id)
        self._setup_vifs(context, instance, network_info, container_id)

    def power_off(self, instance):
        container_id = self.find_container_by_name(instance['name']).get('id')
        if not container_id:
            return
        self._clear_vifs(container_id)
        self.docker.stop_container(container_id)

    def get_console_output(self, instance):
        container_id = self.find_container_by_name(instance['name']).get('id')
        if not container_id:
            return
        return self.docker.get_container_logs(container_id)

    def _get_registry_port(self):
        default_port = CONF.docker_registry_default_port
        registry = None
        for container in self.docker.list_containers(_all=False):
            container = self.docker.inspect_container(container['id'])
            if 'docker-registry' in container['Path']:
                registry = container
                break
        if not registry:
            return default_port
        # NOTE(samalba): The registry service always binds on port 5000 in the
        # container
        try:
            return container['NetworkSettings']['PortMapping']['Tcp']['5000']
        except (KeyError, TypeError):
            # NOTE(samalba): Falling back to a default port allows more
            # flexibility (run docker-registry outside a container)
            return default_port

    def snapshot(self, context, instance, image_href, update_task_state):
        container_id = self.find_container_by_name(instance['name']).get('id')
        if not container_id:
            raise exception.InstanceNotRunning(instance_id=instance['uuid'])
        update_task_state(task_state=task_states.IMAGE_PENDING_UPLOAD)
        (image_service, image_id) = glance.get_remote_image_service(
            context, image_href)
        image = image_service.show(context, image_id)
        registry_port = self._get_registry_port()
        name = image['name']
        default_tag = (':' not in name)
        name = '{0}:{1}/{2}'.format('docker-registry.melicloud.com',
                                    registry_port,
                                    name)
        commit_name = name if not default_tag else name + ':latest'
        self.docker.commit_container(container_id, commit_name)
        update_task_state(task_state=task_states.IMAGE_UPLOADING,
                          expected_state=task_states.IMAGE_PENDING_UPLOAD)
        headers = {'X-Meta-Glance-Image-Id': image_href}
        self.docker.push_repository(name, headers=headers)
