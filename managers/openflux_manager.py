"""Pinned OpenFlux L4 exit with service transports and encrypted direct fallback."""

import base64
import binascii
import json
import re
import secrets
import shlex
import threading
import time
from urllib.parse import urlsplit


class OpenFluxManager:
    PROTOCOL = 'openflux'
    CONTAINER_NAME = 'amnezia-openflux'
    CONFIG_DIR = '/opt/amnezia/openflux'
    VERSION = 'node-v1.3.1'
    IMAGE = 'alpine:3.23'
    DEFAULT_PORT = 8445
    BINARIES = {
        'x86_64': ('amd64', '94f0fbd2f7f60763bf6c4be0623e928ce878f846e33308dd41bc8d735e7b69b0'),
        'aarch64': ('arm64', 'dcf1fbf57bf535483c15b0aa0bd8bf6a3bc34248c1992db79b7ceeb818172f69'),
    }
    # Provisioning is infrequent; serialize it with exports/removal to protect keys.
    _lock = threading.RLock()

    def __init__(self, ssh, protocol='openflux'):
        if protocol != self.PROTOCOL:
            raise ValueError('OpenFlux supports a single instance per server')
        self.ssh = ssh

    def _run(self, command, timeout=60):
        out, _, code = self.ssh.run_sudo_command(command, timeout=timeout)
        if code:
            # Remote output can contain document credentials or core share payloads.
            raise RuntimeError('OpenFlux remote operation failed; check the VPS service')
        return out.strip()

    @staticmethod
    def _port(value):
        if not re.fullmatch(r'[0-9]{1,5}', str(value)) or not 1024 <= int(value) <= 65535:
            raise ValueError('OpenFlux port must be between 1024 and 65535')
        return int(value)

    @staticmethod
    def validate_options(yandex_url='', mailru_url='', cups_rooms=''):
        result = []
        for kind, value, pattern, priority in (
            ('vyandex', yandex_url, r'https://(?:docs|disk)\.yandex\.(?:ru|com|by|kz|uz)/edit/d/[A-Za-z0-9_-]{16,200}', 100),
            ('mailru', mailru_url, r'https://cloud\.mail\.ru/public/[A-Za-z0-9_-]{2,64}/[A-Za-z0-9_-]{2,128}', 90),
            ('cupsonline', cups_rooms, r'[A-Za-z0-9_-]{8,4096}', 70),
        ):
            value = str(value or '').strip()
            if not value:
                continue
            if len(value) > 4096 or re.search(r'\s', value):
                raise ValueError('Invalid OpenFlux transport address')
            if kind != 'cupsonline':
                parsed = urlsplit(value)
                value = parsed._replace(query='', fragment='').geturl().rstrip('/')
            if not re.fullmatch(pattern, value):
                raise ValueError(f'Invalid OpenFlux {kind} address')
            if kind == 'cupsonline':
                try:
                    rooms = json.loads(base64.urlsafe_b64decode(value + '=' * (-len(value) % 4)))
                except (ValueError, UnicodeError, binascii.Error) as exc:
                    raise ValueError('Invalid OpenFlux Cups.online room list') from exc
                if not isinstance(rooms, list) or not rooms or not all(isinstance(room, str) and room.strip() for room in rooms):
                    raise ValueError('OpenFlux Cups.online requires a nonempty packed room list')
            result.append({'type': kind, 'url': value, 'priority': priority})
        if not result:
            raise ValueError('Specify a Yandex document, a public Mail.ru document or Cups.online rooms')
        return result

    def check_protocol_installed(self, protocol_type='openflux'):
        return self.CONTAINER_NAME in self._run(
            f"docker ps -a --filter name=^{self.CONTAINER_NAME}$ --format '{{{{.Names}}}}'").splitlines()

    def check_container_running(self, protocol_type='openflux'):
        return self._run(f"docker inspect -f '{{{{.State.Running}}}}' {self.CONTAINER_NAME}") == 'true'

    def _metadata(self):
        data = json.loads(self._run(f'cat {self.CONFIG_DIR}/settings.json'))
        return {'port': self._port(data['port']), 'version': self.VERSION,
                'transports': self.validate_options(**data['options'])}

    def get_server_status(self, protocol_type='openflux'):
        exists = self.check_protocol_installed()
        status = {'container_exists': exists, 'container_running': False,
                  'protocol': self.PROTOCOL, 'container_name': self.CONTAINER_NAME}
        if exists:
            status['container_running'] = self.check_container_running()
            meta = self._metadata()
            status.update(port=meta['port'], version=meta['version'],
                          transports=[t['type'] for t in meta['transports']])
        return status

    def install_protocol(self, protocol_type='openflux', port=None, **options):
        if protocol_type != self.PROTOCOL:
            raise ValueError('Invalid OpenFlux instance')
        port = self._port(self.DEFAULT_PORT if port in (None, '') else port)
        transports = self.validate_options(**options)
        with self._lock:
            return self._install(port, options, transports)

    def _install(self, port, options, transports):
        if self.check_protocol_installed():
            raise ValueError('OpenFlux is already installed; remove it before reinstalling')
        if self._run(f"ss -H -l -n -t 'sport = :{port}'"):
            raise ValueError(f'Port {port}/tcp is already in use')
        arch, _, code = self.ssh.run_command('uname -m')
        if code or arch.strip() not in self.BINARIES:
            raise ValueError('OpenFlux supports Linux x86_64 and aarch64 servers')
        arch, digest = self.BINARIES[arch.strip()]
        url = f'https://github.com/p1neappleXpress/OpenFlux/releases/download/{self.VERSION}/openflux-linux-{arch}'
        script = f'''set -eu
umask 077
mkdir -p {self.CONFIG_DIR}
chmod 700 {self.CONFIG_DIR}
tmp=$(mktemp {self.CONFIG_DIR}/core.XXXXXX)
trap 'rm -f "$tmp"' EXIT
if command -v curl >/dev/null 2>&1; then
  curl --fail --location --retry 2 --connect-timeout 15 --max-time 180 -o "$tmp" {shlex.quote(url)}
else
  wget -T 180 -O "$tmp" {shlex.quote(url)}
fi
printf '%s  %s\\n' {digest} "$tmp" | sha256sum -c -
chmod 555 "$tmp"
mv -f "$tmp" {self.CONFIG_DIR}/openflux
docker pull {self.IMAGE}
mkdir -p {self.CONFIG_DIR}/state
chown 65534:65534 {self.CONFIG_DIR}/state
chmod 700 {self.CONFIG_DIR}/state
'''
        _, _, code = self.ssh.run_sudo_script(script, timeout=300)
        if code:
            raise RuntimeError('Failed to download and verify OpenFlux')
        key_path = f'{self.CONFIG_DIR}/encryption.key'
        old_key, _, code = self.ssh.run_sudo_command(f'cat {key_path}')
        key = old_key.strip() if code == 0 and re.fullmatch(r'[a-f0-9]{64}', old_key.strip()) else secrets.token_hex(32)
        self.ssh.upload_file_sudo(key + '\n', key_path)
        self._run(f'chown 65534:65534 {key_path}')
        self._run(f'chmod 400 {key_path}')
        context = next((t['url'] for t in transports if t['type'] != 'cupsonline'), '')
        config = 'Role = exit\nMode = l4\nEncryptionKeyFile = /encryption.key\nCookieStore = /state/cookies.json\n'
        if context:
            config += f'URL = {context}\n'
        for t in transports:
            config += f"\n[Transport {t['type']}]\nType = {t['type']}\nPriority = {t['priority']}\nURL = {t['url']}\n"
        config += f'\n[Transport direct]\nType = direct\nPriority = 50\nListen = 0.0.0.0:{port}\n'
        self.ssh.upload_file_sudo(config, f'{self.CONFIG_DIR}/node.conf')
        self._run(f'chown 65534:65534 {self.CONFIG_DIR}/node.conf')
        self._run(f'chmod 400 {self.CONFIG_DIR}/node.conf')
        self.ssh.upload_file_sudo(json.dumps({'port': port, 'options': options}), f'{self.CONFIG_DIR}/settings.json')
        self._run(f'chmod 600 {self.CONFIG_DIR}/settings.json')
        command = (f'docker create --restart unless-stopped --name {self.CONTAINER_NAME} '
                   '--user 65534:65534 --read-only --cap-drop ALL --security-opt no-new-privileges '
                   '--pids-limit 128 --log-opt max-size=10m --log-opt max-file=2 '
                   f'-p {port}:{port}/tcp '
                   f'-v {self.CONFIG_DIR}/openflux:/openflux:ro '
                   f'-v {key_path}:/encryption.key:ro '
                   f'-v {self.CONFIG_DIR}/node.conf:/node.conf:ro '
                   f'-v {self.CONFIG_DIR}/state:/state '
                   f'--entrypoint /openflux {self.IMAGE} --config /node.conf')
        container_id = self._run(command)
        if not re.fullmatch(r'[a-f0-9]{64}', container_id):
            raise RuntimeError('Docker did not return the newly created container ID')
        try:
            self._run(f'docker start {container_id}')
            time.sleep(4)
            if not self.check_container_running():
                raise RuntimeError('OpenFlux exited during startup')
        except Exception:
            self.ssh.run_sudo_command(f'docker rm -f {container_id}')
            raise
        return {'status': 'success', 'port': port, 'version': self.VERSION,
                'log': [f'OpenFlux {self.VERSION}: encrypted L4 exit on {port}/TCP',
                        '! Allow the TCP port in the VPS/provider firewall for direct fallback.',
                        'Import the connection link in an OpenFlux client.']}

    def get_settings(self, host, public_port=None):
        with self._lock:
            metadata = self._metadata()
            key = self._run(f'cat {self.CONFIG_DIR}/encryption.key')
            if not re.fullmatch(r'[a-f0-9]{64}', key):
                raise ValueError('OpenFlux key is missing or invalid')
            host = str(host).strip('[]')
            if not host or re.search(r'[\s/\\]', host):
                raise ValueError('Invalid OpenFlux public host')
            port = str(public_port or metadata['port'])
            if not re.fullmatch(r'[0-9]{1,5}', port) or not 1 <= int(port) <= 65535:
                raise ValueError('Invalid OpenFlux public port')
            endpoint = f"{'[' + host + ']' if ':' in host else host}:{int(port)}"
            transports = metadata['transports']
            config = {'name': 'OpenFlux', 'negotiate': True, 'secret': key,
                      'context': next((t['url'] for t in transports if t['type'] != 'cupsonline'), ''),
                      'transports': transports + [{'type': 'direct', 'dial': endpoint, 'priority': 50}]}
            path = f'{self.CONFIG_DIR}/share-{secrets.token_hex(12)}.json'
            try:
                self.ssh.upload_file_sudo(json.dumps(config), path)
                self._run(f'chown 65534:65534 {path}')
                self._run(f'chmod 400 {path}')
                result = json.loads(self._run(
                    'docker run --rm --network none --read-only --cap-drop ALL '
                    '--security-opt no-new-privileges --user 65534:65534 '
                    f'-v {self.CONFIG_DIR}/openflux:/openflux:ro -v {path}:/share.json:ro '
                    f'--entrypoint /bin/sh {self.IMAGE} -c ' + shlex.quote('exec /openflux --make-link - < /share.json')))
                if not str(result.get('link', '')).startswith('openflux://v1/'):
                    raise RuntimeError('OpenFlux could not generate a connection link')
                return {'link': result['link'], 'endpoint': endpoint, 'version': self.VERSION}
            finally:
                self._run(f'rm -f -- {path}')

    def remove_container(self, protocol_type='openflux'):
        with self._lock:
            self._run(f'docker rm -f {self.CONTAINER_NAME}')
            self._run(f'rm -rf -- {self.CONFIG_DIR}')
        return True
