import base64
import json
import hashlib
import io
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from managers import vkturn_manager as module


WG_CONFIG = '''[Interface]
PrivateKey = private-key
Address = 10.8.2.2/32
DNS = 1.1.1.1
MTU = 1420

[Peer]
PublicKey = public-key
PresharedKey = preshared-key
Endpoint = example.com:51820
AllowedIPs = 0.0.0.0/0, ::/0
PersistentKeepalive = 25
'''


class FakeSSH:
    def __init__(self, fail=''):
        self.commands = []
        self.files = {}
        self.running = False
        self.fail = fail

    def run_command(self, cmd, timeout=60):
        self.commands.append(cmd)
        return ('x86_64\n', '', 0) if cmd == 'uname -m' else ('', '', 0)

    def run_sudo_script(self, script, timeout=120):
        self.commands.append(script)
        if self.fail and self.fail in script:
            return '', 'injected failure', 1
        return '', '', 0

    def upload_file_sudo(self, content, path):
        self.files[path] = content

    def run_sudo_command(self, cmd, timeout=60):
        self.commands.append(cmd)
        if self.fail and self.fail in cmd:
            return '', 'injected failure', 1
        if cmd.startswith('cat '):
            path = cmd.split()[1]
            return (self.files[path], '', 0) if path in self.files else ('', '', 1)
        if cmd.startswith('docker port '):
            return '0.0.0.0:51820\n[::]:51820\n', '', 0
        if 'wg show wg0 listen-port' in cmd:
            return '51820', '', 0
        if cmd.startswith('docker ps -a '):
            return ('amnezia-vkturn\n' if self.running else ''), '', 0
        if cmd.startswith('docker inspect '):
            return ('true' if self.running else 'false'), '', 0
        if cmd.startswith('docker create '):
            return 'c' * 64, '', 0
        if cmd.startswith('docker start '):
            self.running = True
            return 'c' * 64, '', 0
        return '', '', 0


class VkTurnManagerTests(unittest.TestCase):
    def backend(self, exists=True):
        wg = Mock()
        wg.get_server_status.return_value = {
            'container_exists': exists, 'container_running': exists,
            'port': '51820' if exists else None,
        }
        wg.install_protocol.return_value = {'status': 'success'}
        return wg

    def test_installs_pinned_verified_server_and_reuses_wireguard(self):
        ssh, wg = FakeSSH(), self.backend()
        with patch.object(module, 'WireGuardManager', return_value=wg):
            result = module.VkTurnManager(ssh).install_protocol(port='56000')
        self.assertEqual(result['status'], 'success')
        self.assertFalse(result['wireguard_created'])
        wg.install_protocol.assert_not_called()
        commands = '\n'.join(ssh.commands)
        self.assertIn('/v1.10.1/server-linux-amd64', commands)
        self.assertIn('sha256sum -c', commands)
        self.assertIn('--user 65534:65534', commands)
        self.assertIn('--read-only', commands)
        self.assertIn('--network host', commands)
        self.assertNotIn('latest/download', commands)
        key = ssh.files[module.VkTurnManager.CONFIG_DIR + '/wrap.key']
        self.assertEqual(len(key.strip()), 64)
        self.assertNotIn(key.strip(), json.dumps(result))
        self.assertNotIn(key.strip(), commands)
        status = module.VkTurnManager(ssh).get_server_status()
        self.assertEqual(status['port'], 56000)
        self.assertNotIn('wrap_key', status)

    def test_download_failure_does_not_install_wireguard_or_start_proxy(self):
        ssh, wg = FakeSSH(fail='sha256sum -c'), self.backend(False)
        with patch.object(module, 'WireGuardManager', return_value=wg):
            with self.assertRaises(RuntimeError):
                module.VkTurnManager(ssh).install_protocol(port=56000)
        wg.install_protocol.assert_not_called()
        self.assertFalse(ssh.running)

    def test_creates_missing_backend_once(self):
        ssh, wg = FakeSSH(), self.backend(False)
        wg.get_server_status.side_effect = [
            {'container_exists': False},
            {'container_exists': True, 'container_running': True, 'port': '51820'},
        ]
        with patch.object(module, 'WireGuardManager', return_value=wg):
            result = module.VkTurnManager(ssh).install_protocol(port=56000)
        self.assertTrue(result['wireguard_created'])
        wg.install_protocol.assert_called_once()
        self.assertFalse(wg.install_protocol.call_args.kwargs['replace_existing'])

    def test_stopped_backend_and_bad_ports_are_rejected_without_reinstall(self):
        for port in ('0', '65536', '56000;id'):
            ssh = FakeSSH()
            with self.assertRaises(ValueError):
                module.VkTurnManager(ssh).install_protocol(port=port)
            self.assertFalse(ssh.commands)
        wg = self.backend()
        wg.get_server_status.return_value['container_running'] = False
        with patch.object(module, 'WireGuardManager', return_value=wg):
            with self.assertRaisesRegex(ValueError, 'WireGuard'):
                module.VkTurnManager(FakeSSH()).install_protocol(port=56000)
        wg.install_protocol.assert_not_called()

    def test_remove_only_removes_proxy(self):
        ssh = FakeSSH()
        module.VkTurnManager(ssh).remove_container()
        commands = '\n'.join(ssh.commands)
        self.assertIn('amnezia-vkturn', commands)
        self.assertNotIn('amnezia-wireguard', commands)

    def test_every_post_upload_command_works_with_non_root_sudo(self):
        class SudoSSH(FakeSSH):
            def run_sudo_command(self, cmd, timeout=60):
                if '&&' in cmd:
                    return '', 'second command is not privileged', 1
                return super().run_sudo_command(cmd, timeout)
        with patch.object(module, 'WireGuardManager', return_value=self.backend()), \
             patch.object(module.time, 'sleep'):
            self.assertEqual(module.VkTurnManager(SudoSSH()).install_protocol(port=56000)['status'], 'success')

    def test_fresh_backend_port_cannot_equal_proxy_listener(self):
        ssh, wg = FakeSSH(), self.backend(False)
        with patch.object(module, 'WireGuardManager', return_value=wg):
            with self.assertRaisesRegex(ValueError, '51820'):
                module.VkTurnManager(ssh).install_protocol(port=51820)
        wg.install_protocol.assert_not_called()

    def test_create_failure_never_deletes_an_existing_container_by_name(self):
        ssh = FakeSSH(fail='docker create ')
        with patch.object(module, 'WireGuardManager', return_value=self.backend()):
            with self.assertRaises(RuntimeError):
                module.VkTurnManager(ssh).install_protocol(port=56000)
        self.assertFalse(any(cmd.startswith('docker rm') for cmd in ssh.commands))

    def test_start_failure_cleans_up_only_the_created_container_id(self):
        ssh = FakeSSH(fail='docker start ')
        with patch.object(module, 'WireGuardManager', return_value=self.backend()):
            with self.assertRaises(RuntimeError):
                module.VkTurnManager(ssh).install_protocol(port=56000)
        self.assertIn('docker rm -f ' + 'c' * 64, ssh.commands)
        self.assertNotIn('docker rm -f amnezia-vkturn', ssh.commands)

    def test_running_container_without_wireguard_interface_is_rejected(self):
        ssh = FakeSSH(fail='wg show wg0 listen-port')
        with patch.object(module, 'WireGuardManager', return_value=self.backend()):
            with self.assertRaises(RuntimeError):
                module.VkTurnManager(ssh).install_protocol(port=56000)
        self.assertFalse(ssh.running)

    def test_export_is_separate_and_ios_import_preserves_profile_keys(self):
        result = module.build_client_bundle(
            WG_CONFIG, '203.0.113.2:56000', 'a' * 64,
            'https://vk.ru/call/join/test-call', 'Phone',
        )
        self.assertIn('Endpoint = 127.0.0.1:9000', result['config'])
        self.assertIn('MTU = 1280', result['config'])
        self.assertNotIn('::/0', result['config'])
        self.assertIn('AllowedIPs = 0.0.0.0/1, 128.0.0.0/1', result['config'])
        self.assertIn('Endpoint = example.com:51820', WG_CONFIG)
        payload = json.loads(base64.b64decode(parse_qs(urlsplit(result['ios_link']).query)['data'][0]))
        self.assertEqual(payload['type'], 'connection')
        settings = payload['settings']
        self.assertTrue(settings['useWrap'])
        self.assertEqual(settings['privateKey'], 'private-key')
        self.assertEqual(settings['presharedKey'], 'preshared-key')
        self.assertEqual(settings['peerAddress'], '203.0.113.2:56000')
        self.assertEqual(settings['wrapKeyHex'], 'a' * 64)
        self.assertFalse(settings['useSrtp'])
        self.assertIn('| sudo ./routes.sh', result['client_command'])
        self.assertIn('| .\\routes.ps1', result['client_command'])

    def test_export_validates_call_link_and_does_not_guess_missing_private_key(self):
        for link in ('https://evil.test/call/join/x', 'https://vk.ru.evil.test/call/join/x', 'https://vk.ru/call/join/x\n;id'):
            with self.assertRaises(ValueError):
                module.build_client_bundle(WG_CONFIG, 'host:56000', 'a' * 64, link)
        with self.assertRaises(ValueError):
            module.build_client_bundle('[Peer]\nPublicKey = key\n', 'host:56000', 'a' * 64)

    def test_backend_lifecycle_refuses_live_proxy_even_without_saved_panel_state(self):
        from managers.wireguard_manager import WireGuardManager
        class ExistingProxy(FakeSSH):
            def run_sudo_command(self, command, timeout=60):
                if 'docker ps -a' in command and 'amnezia-vkturn' in command:
                    return 'amnezia-vkturn', '', 0
                return super().run_sudo_command(command, timeout)
        wg = WireGuardManager(ExistingProxy())
        with self.assertRaisesRegex(ValueError, 'VK TURN'):
            wg.remove_container()

    def test_wireguard_no_replace_fails_closed_if_docker_state_cannot_be_read(self):
        from managers.wireguard_manager import WireGuardManager
        ssh = FakeSSH(fail='docker ps -a')
        wg = WireGuardManager(ssh)
        with patch.object(wg, 'check_docker_installed', return_value=True):
            with self.assertRaises(RuntimeError):
                wg.install_protocol(port='51820', replace_existing=False)
        self.assertFalse(any(cmd.startswith('docker rm') for cmd in ssh.commands))

    def test_backup_rejects_wrong_binary_or_backend_before_restore(self):
        ssh = FakeSSH()
        ssh.running = True
        ssh.files[module.VkTurnManager.CONFIG_DIR + '/start.sh'] = '#!/bin/sh\ncurrent backend\n'
        binary = b'pinned test binary'
        def archive(path, startup, data):
            with tarfile.open(path, 'w:gz') as tar:
                for filename, content in [('server', data), ('start.sh', startup), ('wrap.key', b'a' * 64)]:
                    member = tarfile.TarInfo('host/opt/amnezia/vkturn/' + filename)
                    member.size = len(content)
                    tar.addfile(member, io.BytesIO(content))
        with tempfile.TemporaryDirectory() as temp, patch.dict(module.VkTurnManager.BINARIES, {
                'x86_64': ('amd64', hashlib.sha256(binary).hexdigest())}):
            path = temp + '/backup.tar.gz'
            archive(path, b'#!/bin/sh\ncurrent backend\n', b'wrong architecture')
            with self.assertRaisesRegex(ValueError, 'binary'):
                module.VkTurnManager(ssh).validate_backup(path)
            archive(path, b'#!/bin/sh\ndifferent backend\n', binary)
            with self.assertRaisesRegex(ValueError, 'binding'):
                module.VkTurnManager(ssh).validate_backup(path)
            archive(path, b'#!/bin/sh\ncurrent backend\n', binary)
            module.VkTurnManager(ssh).validate_backup(path)


if __name__ == '__main__':
    unittest.main()
