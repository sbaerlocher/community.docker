# Copyright (c) 2023, Felix Fontein <felix@fontein.de>
# Copyright (c) 2023, Léo El Amri (@lel-amri)
# GNU General Public License v3.0+ (see LICENSES/GPL-3.0-or-later.txt or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import (absolute_import, division, print_function)
__metaclass__ = type


import os
import re
from collections import namedtuple

from ansible.module_utils.common.text.converters import to_native
from ansible.module_utils.six.moves import shlex_quote

from ansible_collections.community.docker.plugins.module_utils.util import DockerBaseClass
from ansible_collections.community.docker.plugins.module_utils.version import LooseVersion


DOCKER_COMPOSE_FILES = 'docker-compose.yml', 'docker-compose.yaml'

DOCKER_STATUS_DONE = frozenset((
    'Started',
    'Healthy',
    'Exited',
    'Restarted',
    'Running',
    'Created',
    'Stopped',
    'Killed',
    'Removed',
    # An extra, specific to containers
    'Recreated',
    # Extras for pull events
    'Pulled',
))
DOCKER_STATUS_WORKING = frozenset((
    'Creating',
    'Starting',
    'Waiting',
    'Restarting',
    'Stopping',
    'Killing',
    'Removing',
    # An extra, specific to containers
    'Recreate',
    # Extras for pull events
    'Pulling',
))
DOCKER_STATUS_PULL = frozenset((
    'Pulled',
    'Pulling',
))
DOCKER_STATUS_ERROR = frozenset((
    'Error',
))
DOCKER_STATUS = frozenset(DOCKER_STATUS_DONE | DOCKER_STATUS_WORKING | DOCKER_STATUS_PULL | DOCKER_STATUS_ERROR)


class ResourceType(object):
    UNKNOWN = "unknown"
    NETWORK = "network"
    IMAGE = "image"
    VOLUME = "volume"
    CONTAINER = "container"
    SERVICE = "service"

    @classmethod
    def from_docker_compose_event(cls, resource_type):
        # type: (Type[ResourceType], Text) -> Any
        return {
            "Network": cls.NETWORK,
            "Image": cls.IMAGE,
            "Volume": cls.VOLUME,
            "Container": cls.CONTAINER,
        }[resource_type]


Event = namedtuple(
    'Event',
    ['resource_type', 'resource_id', 'status', 'msg']
)


_DRY_RUN_MARKER = 'DRY-RUN MODE -'

_RE_RESOURCE_EVENT = re.compile(
    r'^'
    r'\s*'
    r'(?P<resource_type>Network|Image|Volume|Container)'
    r'\s+'
    r'(?P<resource_id>\S+)'
    r'\s+'
    r'(?P<status>\S(?:|.*\S))'
    r'\s*'
    r'$'
)

_RE_PULL_EVENT = re.compile(
    r'^'
    r'\s*'
    r'(?P<service>\S+)'
    r'\s+'
    r'(?P<status>%s)'
    r'\s*'
    r'$'
    % '|'.join(re.escape(status) for status in DOCKER_STATUS_PULL)
)

_RE_ERROR_EVENT = re.compile(
    r'^'
    r'\s*'
    r'(?P<resource_id>\S+)'
    r'\s+'
    r'(?P<status>%s)'
    r'\s*'
    r'$'
    % '|'.join(re.escape(status) for status in DOCKER_STATUS_ERROR)
)


def parse_events(stderr, dry_run=False, warn_function=None):
    events = []
    error_event = None
    for line in stderr.splitlines():
        line = to_native(line.strip())
        if not line:
            continue
        if dry_run:
            if line.startswith(_DRY_RUN_MARKER):
                line = line[len(_DRY_RUN_MARKER):].lstrip()
            elif error_event is None and warn_function:
                # This could be a bug, a change of docker compose's output format, ...
                # Tell the user to report it to us :-)
                warn_function(
                    'Event line is missing dry-run mode marker: {0!r}. Please report this at '
                    'https://github.com/ansible-collections/community.docker/issues/new?assignees=&labels=&projects=&template=bug_report.md'
                    .format(line)
                )
        match = _RE_RESOURCE_EVENT.match(line)
        if match is not None:
            status = match.group('status')
            msg = None
            if status not in DOCKER_STATUS:
                status, msg = msg, status
            event = Event(
                ResourceType.from_docker_compose_event(match.group('resource_type')),
                match.group('resource_id'),
                status,
                msg,
            )
            events.append(event)
            if status in DOCKER_STATUS_ERROR:
                error_event = event
            else:
                error_event = None
            continue
        match = _RE_PULL_EVENT.match(line)
        if match:
            events.append(
                Event(
                    ResourceType.SERVICE,
                    match.group('service'),
                    match.group('status'),
                    None,
                )
            )
            error_event = None
            continue
        match = _RE_ERROR_EVENT.match(line)
        if match:
            error_event = Event(
                ResourceType.UNKNOWN,
                match.group('resource_id'),
                match.group('status'),
                None,
            )
            events.append(error_event)
            continue
        if error_event is not None:
            # Unparsable line that apparently belongs to the previous error event
            error_event = Event(
                error_event.resource_type,
                error_event.resource_id,
                error_event.status,
                '\n'.join(msg for msg in [error_event.msg, line] if msg is not None),
            )
            events[-1] = error_event
            continue
        if line.startswith('Error '):
            # Error message that is independent of an error event
            error_event = Event(
                ResourceType.UNKNOWN,
                '',
                'Error',
                line,
            )
            events.append(error_event)
            continue
        # This could be a bug, a change of docker compose's output format, ...
        # Tell the user to report it to us :-)
        if warn_function:
            warn_function(
                'Cannot parse event from line: {0!r}. Please report this at '
                'https://github.com/ansible-collections/community.docker/issues/new?assignees=&labels=&projects=&template=bug_report.md'
                .format(line)
            )
    return events


def has_changes(events):
    for event in events:
        if event.status in DOCKER_STATUS_WORKING:
            return True
    return False


def extract_actions(events):
    actions = []
    for event in events:
        if event.status in DOCKER_STATUS_WORKING:
            actions.append({
                'what': event.resource_type,
                'id': event.resource_id,
                'status': event.status,
            })
    return actions


def emit_warnings(events, warn_function):
    for event in events:
        # If a message is present, assume it is a warning
        if event.status is None and event.msg is not None:
            warn_function('Docker compose: {resource_type} {resource_id}: {msg}'.format(
                resource_type=event.resource_type,
                resource_id=event.resource_id,
                msg=event.msg,
            ))


def is_failed(events, rc):
    if rc:
        return True
    for event in events:
        if event.status in DOCKER_STATUS_ERROR:
            return True
    return False


def update_failed(result, events, args, stdout, stderr, rc, cli):
    errors = []
    for event in events:
        if event.status in DOCKER_STATUS_ERROR:
            msg = 'Error when processing {resource_type} {resource_id}: '
            if event.resource_type == 'unknown':
                msg = 'Error when processing {resource_id}: '
                if event.resource_id == '':
                    msg = 'General error: '
            msg += '{status}' if event.msg is None else '{msg}'
            errors.append(msg.format(
                resource_type=event.resource_type,
                resource_id=event.resource_id,
                status=event.status,
                msg=event.msg,
            ))
    if not errors and not rc:
        return False
    if not errors:
        errors.append('Return code {code} is non-zero'.format(code=rc))
    result['failed'] = True
    result['msg'] = '\n'.join(errors)
    result['cmd'] = ' '.join(shlex_quote(arg) for arg in [cli] + args)
    result['stdout'] = to_native(stdout)
    result['stderr'] = to_native(stderr)
    result['rc'] = rc
    return True


def common_compose_argspec():
    return dict(
        project_src=dict(type='path', required=True),
        project_name=dict(type='str'),
        env_files=dict(type='list', elements='path'),
        profiles=dict(type='list', elements='str'),
    )


def combine_binary_output(*outputs):
    return b'\n'.join(out for out in outputs if out)


def combine_text_output(*outputs):
    return '\n'.join(out for out in outputs if out)


class BaseComposeManager(DockerBaseClass):
    def __init__(self, client, min_version='2.18.0'):
        super(BaseComposeManager, self).__init__()
        self.client = client
        self.check_mode = self.client.check_mode
        parameters = self.client.module.params

        self.project_src = parameters['project_src']
        self.project_name = parameters['project_name']
        self.env_files = parameters['env_files']
        self.profiles = parameters['profiles']

        compose = self.client.get_client_plugin_info('compose')
        if compose is None:
            self.client.fail('Docker CLI {0} does not have the compose plugin installed'.format(self.client.get_cli()))
        compose_version = compose['Version'].lstrip('v')
        self.compose_version = LooseVersion(compose_version)
        if self.compose_version < LooseVersion(min_version):
            self.client.fail('Docker CLI {cli} has the compose plugin with version {version}; need version {min_version} or later'.format(
                cli=self.client.get_cli(),
                version=compose_version,
                min_version=min_version,
            ))

        if not os.path.isdir(self.project_src):
            self.client.fail('"{0}" is not a directory'.format(self.project_src))

        if all(not os.path.isfile(os.path.join(self.project_src, f)) for f in DOCKER_COMPOSE_FILES):
            self.client.fail('"{0}" does not contain {1}'.format(self.project_src, ' or '.join(DOCKER_COMPOSE_FILES)))

    def get_base_args(self):
        args = ['compose', '--ansi', 'never']
        if self.compose_version >= LooseVersion('2.19.0'):
            # https://github.com/docker/compose/pull/10690
            args.extend(['--progress', 'plain'])
        args.extend(['--project-directory', self.project_src])
        if self.project_name:
            args.extend(['--project-name', self.project_name])
        for env_file in self.env_files or []:
            args.extend(['--env-file', env_file])
        for profile in self.profiles or []:
            args.extend(['--profile', profile])
        return args

    def list_containers_raw(self):
        args = self.get_base_args() + ['ps', '--format', 'json', '--all']
        if self.compose_version >= LooseVersion('2.23.0'):
            # https://github.com/docker/compose/pull/11038
            args.append('--no-trunc')
        kwargs = dict(cwd=self.project_src, check_rc=True)
        if self.compose_version >= LooseVersion('2.21.0'):
            # Breaking change in 2.21.0: https://github.com/docker/compose/pull/10918
            dummy, containers, dummy = self.client.call_cli_json_stream(*args, **kwargs)
        else:
            dummy, containers, dummy = self.client.call_cli_json(*args, **kwargs)
        return containers

    def list_containers(self):
        result = []
        for container in self.list_containers_raw():
            labels = {}
            if container.get('Labels'):
                for part in container['Labels'].split(','):
                    label_value = part.split('=', 1)
                    labels[label_value[0]] = label_value[1] if len(label_value) > 1 else ''
            container['Labels'] = labels
            container['Names'] = container.get('Names', container['Name']).split(',')
            container['Networks'] = container.get('Networks', '').split(',')
            container['Publishers'] = container.get('Publishers') or []
            result.append(container)
        return result

    def list_images(self):
        args = self.get_base_args() + ['images', '--format', 'json']
        kwargs = dict(cwd=self.project_src, check_rc=True)
        dummy, images, dummy = self.client.call_cli_json(*args, **kwargs)
        return images

    def parse_events(self, stderr, dry_run=False):
        return parse_events(stderr, dry_run=dry_run, warn_function=self.client.warn)

    def emit_warnings(self, events):
        emit_warnings(events, warn_function=self.client.warn)

    def update_result(self, result, events, stdout, stderr):
        result['changed'] = result.get('changed', False) or has_changes(events)
        result['actions'] = result.get('actions', []) + extract_actions(events)
        result['stdout'] = combine_text_output(result.get('stdout'), to_native(stdout))
        result['stderr'] = combine_text_output(result.get('stderr'), to_native(stderr))

    def update_failed(self, result, events, args, stdout, stderr, rc):
        return update_failed(
            result,
            events,
            args=args,
            stdout=stdout,
            stderr=stderr,
            rc=rc,
            cli=self.client.get_cli(),
        )

    def cleanup_result(self, result):
        if not result.get('failed'):
            # Only return stdout and stderr if it's not empty
            for res in ('stdout', 'stderr'):
                if result.get(res) == '':
                    result.pop(res)
