# -*- coding: utf-8 -*-
# Copyright (c) 2018, Stefan Heitmueller <stefan.heitmueller@gmx.com>
# Copyright (c) 2018 Ansible Project
# GNU General Public License v3.0+ (see LICENSES/GPL-3.0-or-later.txt or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import (absolute_import, division, print_function)

__metaclass__ = type

DOCUMENTATION = '''
    name: docker_swarm
    author:
      - Stefan Heitmüller (@morph027) <stefan.heitmueller@gmx.com>
    short_description: Ansible dynamic inventory plugin for Docker swarm nodes.
    requirements:
        - python >= 2.7
        - L(Docker SDK for Python,https://docker-py.readthedocs.io/en/stable/) >= 1.10.0
    extends_documentation_fragment:
        - constructed
    description:
        - Reads inventories from the Docker swarm API.
        - Uses a YAML configuration file docker_swarm.[yml|yaml].
        - "The plugin returns following groups of swarm nodes:  C(all) - all hosts; C(workers) - all worker nodes;
          C(managers) - all manager nodes; C(leader) - the swarm leader node;
          C(nonleaders) - all nodes except the swarm leader."
    options:
        plugin:
            description: The name of this plugin, it should always be set to V(community.docker.docker_swarm)
                         for this plugin to recognize it as it's own.
            type: str
            required: true
            choices: [ docker_swarm, community.docker.docker_swarm ]
        docker_host:
            description:
                - Socket of a Docker swarm manager node (C(tcp), C(unix)).
                - "Use V(unix:///var/run/docker.sock) to connect via local socket."
            type: str
            required: true
            aliases: [ docker_url ]
        verbose_output:
            description: Toggle to (not) include all available nodes metadata (for example C(Platform), C(Architecture), C(OS),
                         C(EngineVersion)).
            type: bool
            default: true
        tls:
            description: Connect using TLS without verifying the authenticity of the Docker host server.
            type: bool
            default: false
        validate_certs:
            description: Toggle if connecting using TLS with or without verifying the authenticity of the Docker
                         host server.
            type: bool
            default: false
            aliases: [ tls_verify ]
        client_key:
            description: Path to the client's TLS key file.
            type: path
            aliases: [ tls_client_key, key_path ]
        ca_path:
            description:
              - Use a CA certificate when performing server verification by providing the path to a CA certificate file.
              - This option was called O(ca_cert) and got renamed to O(ca_path) in community.docker 3.6.0. The old name has
                been added as an alias and can still be used.
            type: path
            aliases: [ ca_cert, tls_ca_cert, cacert_path ]
        client_cert:
            description: Path to the client's TLS certificate file.
            type: path
            aliases: [ tls_client_cert, cert_path ]
        tls_hostname:
            description: When verifying the authenticity of the Docker host server, provide the expected name of
                         the server.
            type: str
        ssl_version:
            description:
                - Provide a valid SSL version number. Default value determined
                  by L(SSL Python module, https://docs.python.org/3/library/ssl.html).
            type: str
        api_version:
            description:
                - The version of the Docker API running on the Docker Host.
                - Defaults to the latest version of the API supported by Docker SDK for Python.
            type: str
            aliases: [ docker_api_version ]
        timeout:
            description:
                - The maximum amount of time in seconds to wait on a response from the API.
                - If the value is not specified in the task, the value of environment variable E(DOCKER_TIMEOUT).
                  will be used instead. If the environment variable is not set, the default value will be used.
            type: int
            default: 60
            aliases: [ time_out ]
        use_ssh_client:
            description:
                - For SSH transports, use the C(ssh) CLI tool instead of paramiko.
                - Requires Docker SDK for Python 4.4.0 or newer.
            type: bool
            default: false
            version_added: 1.5.0
        include_host_uri:
            description: Toggle to return the additional attribute C(ansible_host_uri) which contains the URI of the
                         swarm leader in format of V(tcp://172.16.0.1:2376). This value may be used without additional
                         modification as value of option O(docker_host) in Docker Swarm modules when connecting via API.
                         The port always defaults to V(2376).
            type: bool
            default: false
        include_host_uri_port:
            description: Override the detected port number included in C(ansible_host_uri).
            type: int
'''

EXAMPLES = '''
# Minimal example using local docker
plugin: community.docker.docker_swarm
docker_host: unix:///var/run/docker.sock

# Minimal example using remote docker
plugin: community.docker.docker_swarm
docker_host: tcp://my-docker-host:2375

# Example using remote docker with unverified TLS
plugin: community.docker.docker_swarm
docker_host: tcp://my-docker-host:2376
tls: true

# Example using remote docker with verified TLS and client certificate verification
plugin: community.docker.docker_swarm
docker_host: tcp://my-docker-host:2376
validate_certs: true
ca_path: /somewhere/ca.pem
client_key: /somewhere/key.pem
client_cert: /somewhere/cert.pem

# Example using constructed features to create groups and set ansible_host
plugin: community.docker.docker_swarm
docker_host: tcp://my-docker-host:2375
strict: false
keyed_groups:
  # add for example x86_64 hosts to an arch_x86_64 group
  - prefix: arch
    key: 'Description.Platform.Architecture'
  # add for example linux hosts to an os_linux group
  - prefix: os
    key: 'Description.Platform.OS'
  # create a group per node label
  # for exomple a node labeled w/ "production" ends up in group "label_production"
  # hint: labels containing special characters will be converted to safe names
  - key: 'Spec.Labels'
    prefix: label
'''

from ansible.errors import AnsibleError
from ansible.module_utils.common.text.converters import to_native
from ansible_collections.community.docker.plugins.module_utils.common import get_connect_params
from ansible_collections.community.docker.plugins.module_utils.util import update_tls_hostname
from ansible.plugins.inventory import BaseInventoryPlugin, Constructable
from ansible.parsing.utils.addresses import parse_address

try:
    import docker
    HAS_DOCKER = True
except ImportError:
    HAS_DOCKER = False


class InventoryModule(BaseInventoryPlugin, Constructable):
    ''' Host inventory parser for ansible using Docker swarm as source. '''

    NAME = 'community.docker.docker_swarm'

    def _fail(self, msg):
        raise AnsibleError(msg)

    def _populate(self):
        raw_params = dict(
            docker_host=self.get_option('docker_host'),
            tls=self.get_option('tls'),
            tls_verify=self.get_option('validate_certs'),
            key_path=self.get_option('client_key'),
            cacert_path=self.get_option('ca_path'),
            cert_path=self.get_option('client_cert'),
            tls_hostname=self.get_option('tls_hostname'),
            api_version=self.get_option('api_version'),
            timeout=self.get_option('timeout'),
            ssl_version=self.get_option('ssl_version'),
            use_ssh_client=self.get_option('use_ssh_client'),
            debug=None,
        )
        update_tls_hostname(raw_params)
        connect_params = get_connect_params(raw_params, fail_function=self._fail)
        self.client = docker.DockerClient(**connect_params)
        self.inventory.add_group('all')
        self.inventory.add_group('manager')
        self.inventory.add_group('worker')
        self.inventory.add_group('leader')
        self.inventory.add_group('nonleaders')

        if self.get_option('include_host_uri'):
            if self.get_option('include_host_uri_port'):
                host_uri_port = str(self.get_option('include_host_uri_port'))
            elif self.get_option('tls') or self.get_option('validate_certs'):
                host_uri_port = '2376'
            else:
                host_uri_port = '2375'

        try:
            self.nodes = self.client.nodes.list()
            for self.node in self.nodes:
                self.node_attrs = self.client.nodes.get(self.node.id).attrs
                self.inventory.add_host(self.node_attrs['ID'])
                self.inventory.add_host(self.node_attrs['ID'], group=self.node_attrs['Spec']['Role'])
                self.inventory.set_variable(self.node_attrs['ID'], 'ansible_host',
                                            self.node_attrs['Status']['Addr'])
                if self.get_option('include_host_uri'):
                    self.inventory.set_variable(self.node_attrs['ID'], 'ansible_host_uri',
                                                'tcp://' + self.node_attrs['Status']['Addr'] + ':' + host_uri_port)
                if self.get_option('verbose_output'):
                    self.inventory.set_variable(self.node_attrs['ID'], 'docker_swarm_node_attributes', self.node_attrs)
                if 'ManagerStatus' in self.node_attrs:
                    if self.node_attrs['ManagerStatus'].get('Leader'):
                        # This is workaround of bug in Docker when in some cases the Leader IP is 0.0.0.0
                        # Check moby/moby#35437 for details
                        swarm_leader_ip = parse_address(self.node_attrs['ManagerStatus']['Addr'])[0] or \
                            self.node_attrs['Status']['Addr']
                        if self.get_option('include_host_uri'):
                            self.inventory.set_variable(self.node_attrs['ID'], 'ansible_host_uri',
                                                        'tcp://' + swarm_leader_ip + ':' + host_uri_port)
                        self.inventory.set_variable(self.node_attrs['ID'], 'ansible_host', swarm_leader_ip)
                        self.inventory.add_host(self.node_attrs['ID'], group='leader')
                    else:
                        self.inventory.add_host(self.node_attrs['ID'], group='nonleaders')
                else:
                    self.inventory.add_host(self.node_attrs['ID'], group='nonleaders')
                # Use constructed if applicable
                strict = self.get_option('strict')
                # Composed variables
                self._set_composite_vars(self.get_option('compose'),
                                         self.node_attrs,
                                         self.node_attrs['ID'],
                                         strict=strict)
                # Complex groups based on jinja2 conditionals, hosts that meet the conditional are added to group
                self._add_host_to_composed_groups(self.get_option('groups'),
                                                  self.node_attrs,
                                                  self.node_attrs['ID'],
                                                  strict=strict)
                # Create groups based on variable values and add the corresponding hosts to it
                self._add_host_to_keyed_groups(self.get_option('keyed_groups'),
                                               self.node_attrs,
                                               self.node_attrs['ID'],
                                               strict=strict)
        except Exception as e:
            raise AnsibleError('Unable to fetch hosts from Docker swarm API, this was the original exception: %s' %
                               to_native(e))

    def verify_file(self, path):
        """Return the possibly of a file being consumable by this plugin."""
        return (
            super(InventoryModule, self).verify_file(path) and
            path.endswith(('docker_swarm.yaml', 'docker_swarm.yml')))

    def parse(self, inventory, loader, path, cache=True):
        if not HAS_DOCKER:
            raise AnsibleError('The Docker swarm dynamic inventory plugin requires the Docker SDK for Python: '
                               'https://github.com/docker/docker-py.')
        super(InventoryModule, self).parse(inventory, loader, path, cache)
        self._read_config_data(path)
        self._populate()
