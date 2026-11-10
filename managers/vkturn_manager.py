"""Moroka8 WRAP transport in front of the panel's standard WireGuard."""

import base64
import configparser
import io
import hashlib
import ipaddress
import json
import re
import secrets
import shlex
import time
import tarfile
from urllib.parse import urlsplit, quote

from managers.wireguard_manager import WireGuardManager, serialized_wireguard_lifecycle


def validate_vk_link(value):
    value = str(value or '').strip()
    if not value:
        return ''
    url = urlsplit(value)
    if (len(value) > 2048 or re.search(r'\s', value) or url.scheme != 'https'
            or url.netloc not in ('vk.com', 'vk.ru', 'vk.me')
            or not re.fullmatch(r'/(?:call/join|join)/[A-Za-z0-9_-]+/?', url.path)
            or url.query or url.fragment):
        raise ValueError('Use an HTTPS VK call link: https://vk.ru/call/join/...')
    return value


def build_client_bundle(config, endpoint, wrap_key, vk_link='', name='WireGuard'):
    vk_link = validate_vk_link(vk_link)
    if not re.fullmatch(r'[a-fA-F0-9]{64}', wrap_key or ''):
        raise ValueError('Invalid WRAP key')
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        parser.read_string(config or '')
        iface, peer = parser['Interface'], parser['Peer']
        if not all((iface.get('PrivateKey'), iface.get('Address'), peer.get('PublicKey'))):
            raise ValueError('WireGuard profile is missing its keys or address')
        addresses = [a.strip() for a in iface['Address'].split(',')
                     if ipaddress.ip_interface(a.strip()).version == 4]
        allowed = [a.strip() for a in peer.get('AllowedIPs', '').split(',')
                   if a.strip() and ipaddress.ip_network(a.strip(), strict=False).version == 4]
        if not addresses or not allowed:
            raise ValueError('An IPv4 WireGuard profile is required')
        iface['Address'] = ', '.join(addresses)
        iface['MTU'] = '1280'
        # The proxy must resolve/reach TURN outside the VPN; IPv6 is not routed
        # by the panel's standard WireGuard backend.
        iface['DNS'] = ', '.join(a.strip() for a in iface.get('DNS', '1.1.1.1').split(',')
                                if ':' not in a) or '1.1.1.1'
        peer['Endpoint'] = '127.0.0.1:9000'
        # Avoid WireGuard for Windows' /0 firewall kill switch, which would
        # block the separate TURN client even after its host routes are added.
        peer['AllowedIPs'] = ', '.join(
            part for network in allowed
            for part in (('0.0.0.0/1', '128.0.0.0/1') if network == '0.0.0.0/0' else (network,)))
        output = io.StringIO()
        parser.write(output)
    except (configparser.Error, KeyError) as exc:
        raise ValueError('A complete WireGuard profile with a stored private key is required') from exc

    settings = {
        'serverName': str(name or 'WireGuard')[:64],
        'privateKey': iface['PrivateKey'], 'peerPublicKey': peer['PublicKey'],
        'presharedKey': peer.get('PresharedKey', ''), 'tunnelAddress': iface['Address'],
        'dnsServers': iface['DNS'], 'peerAddress': endpoint, 'vkLink': vk_link,
        'useDTLS': True, 'useWrap': True, 'useSrtp': False, 'useUDP': False,
        'useWrapA': False, 'useWrapS': False, 'useCsqtt': False,
        'wrapKeyHex': wrap_key, 'numConnections': 4,
    }
    # The iOS app enforces a full IPv4 tunnel, so do not silently widen a
    # deliberately restricted profile when producing its import link.
    ios_link = ''
    if vk_link and '0.0.0.0/0' in allowed:
        payload = {'type': 'connection', 'version': 1, 'settings': settings}
        encoded = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()
        ios_link = 'vkturnproxy://import?data=' + quote(encoded, safe='')
    args = (' -listen 127.0.0.1:9000 -peer ' + shlex.quote(endpoint)
            + ' -vk-link ' + shlex.quote(vk_link or '<VK_CALL_LINK>')
            + ' -wrap -wrap-key ' + shlex.quote(wrap_key) + ' -n 4')
    command = ('# Linux (amd64)\n./client-linux-amd64' + args + ' | sudo ./routes.sh\n\n'
               '# macOS (Apple Silicon)\n./client-darwin-arm64' + args + ' | sudo ./routes-macos.sh\n\n'
               '# Windows PowerShell (Administrator)\n.\\client-windows-amd64.exe' + args + ' | .\\routes.ps1\n\n'
               '# Android (arm64; exclude the proxy app from WireGuard)\n./client-android-arm64' + args)
    return {'config': output.getvalue(), 'ios_link': ios_link, 'client_command': command,
            'endpoint': endpoint, 'wrap_key': wrap_key, 'vk_link': vk_link}


class VkTurnManager:
    PROTOCOL = 'vkturn'
    CONTAINER_NAME = 'amnezia-vkturn'
    CONFIG_DIR = '/opt/amnezia/vkturn'
    VERSION = 'v1.10.1'
    IMAGE = 'alpine:3.23'
    DEFAULT_PORT = 56000
    BINARIES = {
        'x86_64': ('amd64', '86f650b7222dc8143e5e984469fd51f13bbf9f580e4a5cfc3eff3e9f3fc05c60'),
        'aarch64': ('arm64', '5cfc4966a8cda90ab1a3ba56873202ca0782cb4ac4e9364e4fac1e36352e5d04'),
    }

    def __init__(self, ssh, protocol='vkturn'):
        if protocol != self.PROTOCOL:
            raise ValueError('VK TURN supports a single instance per server')
        self.ssh = ssh

    def _run(self, command, timeout=60):
        out, err, code = self.ssh.run_sudo_command(command, timeout=timeout)
        if code != 0:
            raise RuntimeError(err or out or 'VK TURN remote command failed')
        return out.strip()

    @staticmethod
    def _port(value):
        text = str(value)
        if not text.isascii() or not text.isdigit() or not 1024 <= int(text) <= 65535:
            raise ValueError('VK TURN port must be between 1024 and 65535')
        return int(text)

    def _assert_port_free(self, port):
        if self._run(f"ss -H -l -n -u 'sport = :{port}'"):
            raise ValueError(f'Port {port}/udp is already in use')

    def check_protocol_installed(self, protocol_type='vkturn'):
        return self.CONTAINER_NAME in self._run(
            "docker ps -a --filter name=^amnezia-vkturn$ --format '{{.Names}}'"
        ).splitlines()

    def check_container_running(self, protocol_type='vkturn'):
        return self._run("docker inspect -f '{{.State.Running}}' amnezia-vkturn") == 'true'

    def _metadata(self):
        out, _, code = self.ssh.run_sudo_command(f'cat {self.CONFIG_DIR}/settings.json')
        if code != 0:
            raise ValueError('VK TURN settings are missing; restore a backup or reinstall')
        data = json.loads(out)
        return {'port': self._port(data['port']),
                'backend_port': self._port(data['backend_port']), 'version': self.VERSION}

    def get_server_status(self, protocol_type='vkturn'):
        exists = self.check_protocol_installed()
        result = {'container_exists': exists, 'container_running': False,
                  'protocol': self.PROTOCOL, 'base_protocol': self.PROTOCOL,
                  'container_name': self.CONTAINER_NAME}
        if exists:
            result['container_running'] = self.check_container_running()
            result.update(self._metadata())
        return result

    def get_settings(self):
        settings = self._metadata()
        key = self._run(f'cat {self.CONFIG_DIR}/wrap.key')
        if not re.fullmatch(r'[a-f0-9]{64}', key):
            raise ValueError('VK TURN WRAP key is missing or invalid')
        return {**settings, 'wrap_key': key}

    def validate_backup(self, path):
        """Refuse a foreign binary/backend before generic restore touches files."""
        if not self.check_protocol_installed():
            raise ValueError('Install VK TURN before restoring its backup')
        arch, _, code = self.ssh.run_command('uname -m')
        if code or arch.strip() not in self.BINARIES:
            raise ValueError('Unsupported VK TURN backup target architecture')
        digest = self.BINARIES[arch.strip()][1]
        current_start = self._run(f'cat {self.CONFIG_DIR}/start.sh')
        with tarfile.open(path, 'r:gz') as archive:
            members = {}
            for member in archive.getmembers():
                name = member.name
                while name.startswith('./'):
                    name = name[2:]
                members[name] = member
            def read(name):
                member = members.get('host/opt/amnezia/vkturn/' + name)
                if not member or not member.isfile():
                    raise ValueError('VK TURN backup is missing ' + name)
                with archive.extractfile(member) as content:
                    return content.read()
            if hashlib.sha256(read('server')).hexdigest() != digest:
                raise ValueError('VK TURN backup binary does not match the pinned version and target CPU')
            if read('start.sh').decode('utf-8').strip() != current_start:
                raise ValueError('VK TURN backup requires the same listener and WireGuard backend binding')
            if not re.fullmatch(rb'[a-f0-9]{64}\s*', read('wrap.key')):
                raise ValueError('Invalid WRAP key in backup')

    @serialized_wireguard_lifecycle
    def install_protocol(self, protocol_type='vkturn', port=None):
        if protocol_type != self.PROTOCOL:
            raise ValueError('Invalid VK TURN instance')
        port = self._port(self.DEFAULT_PORT if port is None or port == '' else port)
        if self.check_protocol_installed():
            raise ValueError('VK TURN is already installed; remove it before reinstalling')
        self._assert_port_free(port)
        wg = WireGuardManager(self.ssh)
        status = wg.get_server_status()
        created = not status.get('container_exists')
        if not created and not status.get('container_running'):
            raise ValueError('Start the existing WireGuard backend before installing VK TURN')
        if created:
            if port == 51820:
                raise ValueError('VK TURN and the new WireGuard backend cannot both use 51820/UDP')
            self._assert_port_free(51820)
        arch_out, _, code = self.ssh.run_command('uname -m')
        if code or arch_out.strip() not in self.BINARIES:
            raise ValueError('VK TURN supports Linux x86_64 and aarch64 servers')
        arch, digest = self.BINARIES[arch_out.strip()]
        url = f'https://github.com/Moroka8/vk-turn-proxy/releases/download/{self.VERSION}/server-linux-{arch}'
        # Verify before creating a missing backend or replacing the binary.
        script = f'''set -eu
umask 077
mkdir -p {self.CONFIG_DIR}
chmod 700 {self.CONFIG_DIR}
tmp=$(mktemp {self.CONFIG_DIR}/server.XXXXXX)
trap 'rm -f "$tmp"' EXIT
if command -v curl >/dev/null 2>&1; then
  curl --fail --location --retry 2 --connect-timeout 15 --max-time 180 -o "$tmp" {shlex.quote(url)}
else
  wget -T 180 -O "$tmp" {shlex.quote(url)}
fi
printf '%s  %s\\n' {digest} "$tmp" | sha256sum -c -
chmod 555 "$tmp"
mv -f "$tmp" {self.CONFIG_DIR}/server
docker pull {self.IMAGE}
'''
        out, err, code = self.ssh.run_sudo_script(script, timeout=300)
        if code:
            raise RuntimeError(err or out or 'Failed to download and verify VK TURN')
        if created:
            result = wg.install_protocol(port='51820', replace_existing=False)
            if isinstance(result, dict) and (result.get('status') == 'error' or result.get('error')):
                raise RuntimeError(result.get('message') or result.get('error') or 'WireGuard installation failed')
            status = wg.get_server_status()
        if not status.get('container_running') or not status.get('port'):
            raise ValueError('WireGuard backend is not running or its port is unknown')
        backend_port = self._port(self._run('docker exec amnezia-wireguard wg show wg0 listen-port'))
        published = self._run(f'docker port amnezia-wireguard {backend_port}/udp')
        endpoint = None
        for binding in published.splitlines():
            host, _, bound_port = binding.rpartition(':')
            if host and not host.startswith('['):
                ipaddress.IPv4Address(host)
                endpoint = f"{'127.0.0.1' if host == '0.0.0.0' else host}:{self._port(bound_port)}"
                break
        if not endpoint:
            raise ValueError('WireGuard needs a published IPv4 UDP port on this server')
        key_path = f'{self.CONFIG_DIR}/wrap.key'
        old_key, _, key_code = self.ssh.run_sudo_command(f'cat {key_path}')
        if key_code == 0 and re.fullmatch(r'[a-f0-9]{64}', old_key.strip()):
            key = old_key.strip()
        else:
            key = secrets.token_hex(32)
        self.ssh.upload_file_sudo(key + '\n', key_path)
        self._run(f'chown 65534:65534 {key_path}')
        self._run(f'chmod 400 {key_path}')
        settings = {'port': port, 'backend_port': backend_port, 'version': self.VERSION}
        self.ssh.upload_file_sudo(json.dumps(settings), f'{self.CONFIG_DIR}/settings.json')
        self._run(f'chmod 600 {self.CONFIG_DIR}/settings.json')
        start = (f'#!/bin/sh\nset -eu\nexec /server -listen 0.0.0.0:{port} '
                 f'-connect {endpoint} -wrap -wrap-key "$(cat /wrap.key)"\n')
        self.ssh.upload_file_sudo(start, f'{self.CONFIG_DIR}/start.sh')
        self._run(f'chmod 555 {self.CONFIG_DIR}/start.sh')
        command = (
            f'docker create --restart unless-stopped --name {self.CONTAINER_NAME} '
            '--network host --user 65534:65534 --read-only --cap-drop ALL '
            '--security-opt no-new-privileges --pids-limit 128 '
            '--log-opt max-size=10m --log-opt max-file=2 '
            f'-v {self.CONFIG_DIR}/server:/server:ro '
            f'-v {key_path}:/wrap.key:ro '
            f'-v {self.CONFIG_DIR}/start.sh:/start.sh:ro {self.IMAGE} /start.sh'
        )
        container_id = self._run(command, timeout=60)
        if not re.fullmatch(r'[0-9a-f]{64}', container_id):
            raise RuntimeError('Docker did not return the newly created container ID')
        try:
            self._run(f'docker start {container_id}')
            time.sleep(1)
            if not self.check_container_running():
                raise RuntimeError('VK TURN container exited during startup')
        except Exception:
            self.ssh.run_sudo_command(f'docker rm -f {container_id}')
            raise
        return {'status': 'success', **settings, 'wireguard_created': created,
                'wireguard_port': backend_port,
                'log': [f'Moroka8 {self.VERSION}: WRAP on {port}/UDP',
                        f'WireGuard backend: {endpoint}',
                        '! Allow the VK TURN UDP port in the VPS/provider firewall.',
                        'Create profiles in WireGuard and use the VK TURN export action.']}

    @serialized_wireguard_lifecycle
    def remove_container(self, protocol_type='vkturn'):
        self._run(f'docker rm -f {self.CONTAINER_NAME}')
        self._run(f'rm -rf -- {self.CONFIG_DIR}')
        return True
