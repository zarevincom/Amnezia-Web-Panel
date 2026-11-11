import asyncio
import base64
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app as panel


class OpenFluxTests(unittest.TestCase):
    def test_registered_service(self):
        self.assertIn('openflux', panel.BASE_PROTOCOLS)
        self.assertEqual(panel.protocol_container_name('openflux'), 'amnezia-openflux')
        self.assertEqual(panel.INSTALL_PORT_TRANSPORT['openflux'], 'tcp')

    def test_carrier_validation(self):
        from managers.openflux_manager import OpenFluxManager
        for kwargs in ({}, {'yandex_url': 'https://evil.test/edit/d/' + 'a' * 16},
                       {'mailru_url': 'https://cloud.mail.ru/public/aa/bb\nRole = client'},
                       {'cups_rooms': '$(id)'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                OpenFluxManager.validate_options(**kwargs)
        self.assertEqual(OpenFluxManager.validate_options(
            yandex_url='https://docs.yandex.ru/edit/d/' + 'a' * 16 + '?sharing=1')[0]['type'], 'vyandex')

    def test_cups_requires_an_actual_packed_room_list(self):
        from managers.openflux_manager import OpenFluxManager
        def pack(value):
            return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')
        for value in ('abcdefgh', '1c031f41-e832-404c-a7b4-92f24c993b84',
                      pack([]), pack({'room': 'test'}), pack([3]), pack([''])):
            with self.subTest(value=value), self.assertRaises(ValueError):
                OpenFluxManager.validate_options(cups_rooms=value)
        value = pack(['1c031f41-e832-404c-a7b4-92f24c993b84'])
        self.assertEqual(OpenFluxManager.validate_options(cups_rooms=value)[0]['url'], value)

    def test_yandex_share_and_private_paths_explain_required_editor_address(self):
        from managers.openflux_manager import OpenFluxManager
        for value in ('https://disk.yandex.ru/i/AbCdEfGhIjKlMn',
                      'https://disk.yandex.ru/d/AbCdEfGhIjKlMn',
                      'https://yadi.sk/i/AbCdEfGhIjKlMn',
                      'https://docs.yandex.ru/edit/disk/disk%2FDocument.docx?source=docs'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'editor address.*https://docs.yandex.ru/edit/d/'):
                OpenFluxManager.validate_options(yandex_url=value)

    def test_invalid_install_rejected_before_ssh(self):
        with patch.object(panel, '_check_admin', return_value=True), \
             patch.object(panel, 'load_data', return_value={'servers': [{'protocols': {}}]}), \
             patch.object(panel, 'get_ssh', side_effect=AssertionError('must not reach SSH')):
            response = asyncio.run(panel.api_install_protocol(Mock(), 0,
                panel.InstallProtocolRequest(protocol='openflux', port='0')))
        self.assertEqual(response.status_code, 400)

    def test_marketplace_and_form_are_present(self):
        source = (Path(__file__).resolve().parents[1] / 'templates/server.html').read_text(encoding='utf-8')
        for marker in ('id="proto-openflux"', "proto: 'openflux'", 'id="openfluxOptions"',
                       'id="openfluxModal"', 'function openOpenfluxSettings'):
            self.assertIn(marker, source)

    def test_settings_are_private_and_use_public_endpoint(self):
        from managers.openflux_manager import OpenFluxManager
        data = {'servers': [{'host': 'ssh.example', 'protocols': {'openflux': {
            'port': 8445, 'public_host': 'public.example', 'public_port': '9445'}}}]}
        with patch.object(panel, '_check_admin', return_value=True), \
             patch.object(panel, 'load_data', return_value=data), \
             patch.object(panel, 'get_ssh', return_value=Mock()) as ssh, \
             patch.object(OpenFluxManager, 'get_settings', return_value={'link': 'openflux://v1/test'}) as settings:
            response = panel.api_openflux_settings(Mock(), 0)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        settings.assert_called_once_with('public.example', '9445')
        ssh.return_value.disconnect.assert_called_once()

    def test_admin_boundary(self):
        with patch.object(panel, '_check_admin', return_value=False), \
             patch.object(panel, 'get_ssh', side_effect=AssertionError('unauthorized SSH')):
            self.assertEqual(panel.api_openflux_settings(Mock(), 0).status_code, 403)

    def test_install_uses_latest_state_and_default_port(self):
        before = {'servers': [{'uid': 'target', 'host': 'host', 'protocols': {}}]}
        after = {'servers': [{'uid': 'other'}, {'uid': 'target', 'host': 'host', 'protocols': {}}], 'users': ['new']}
        manager = Mock()
        manager.install_protocol.return_value = {'status': 'success', 'port': 8445, 'version': 'node-v1.3.1'}
        with patch.object(panel, '_check_admin', return_value=True), \
             patch.object(panel, 'load_data', side_effect=[before, after]), \
             patch.object(panel, 'save_data') as save, \
             patch.object(panel, 'get_ssh', return_value=Mock()), \
             patch.object(panel, 'get_used_ports', return_value={'tcp': {}}), \
             patch.object(panel, 'ensure_docker_installed', return_value=''), \
             patch.object(panel, 'get_protocol_manager', return_value=manager):
            result = asyncio.run(panel.api_install_protocol(Mock(), 0,
                panel.InstallProtocolRequest(protocol='openflux', openflux_mailru_url='https://cloud.mail.ru/public/aa/bb')))
        self.assertEqual(result['protocol'], 'openflux')
        self.assertEqual(save.call_args.args[0]['users'], ['new'])
        self.assertEqual(after['servers'][1]['protocols']['openflux']['port'], 8445)
        self.assertEqual(manager.install_protocol.call_args.kwargs['port'], '8445')


class FakeSSH:
    def __init__(self):
        self.commands, self.scripts, self.uploads = [], [], {}
        self.exists, self.running, self.download_code = False, True, 0

    def run_command(self, command):
        return 'x86_64', '', 0

    def run_sudo_command(self, command, timeout=60):
        self.commands.append(command)
        if command.startswith('docker ps'):
            return ('amnezia-openflux' if self.exists else ''), '', 0
        if command.startswith('docker inspect'):
            return ('true' if self.running else 'false'), '', 0
        if command.startswith('docker create'):
            return 'a' * 64, '', 0
        if command.startswith('docker run'):
            return json.dumps({'link': 'openflux://v1/test'}), '', 0
        if command.startswith('cat '):
            path = command[4:]
            return self.uploads.get(path, ''), '', 0 if path in self.uploads else 1
        return '', '', 0

    def run_sudo_script(self, script, timeout=60):
        self.scripts.append(script)
        return '', '', self.download_code

    def upload_file_sudo(self, content, path):
        self.uploads[path] = content


class OpenFluxManagerTests(unittest.TestCase):
    def manager(self):
        from managers.openflux_manager import OpenFluxManager
        self.ssh = FakeSSH()
        return OpenFluxManager(self.ssh)

    def install(self, manager):
        with patch('managers.openflux_manager.time.sleep'):
            return manager.install_protocol(mailru_url='https://cloud.mail.ru/public/aa/bb')

    def test_pinned_download_unprivileged_container_and_secret_isolation(self):
        manager = self.manager()
        result = self.install(manager)
        script = self.ssh.scripts[0]
        self.assertIn('node-v1.3.1/openflux-linux-amd64', script)
        self.assertIn(manager.BINARIES['x86_64'][1], script)
        self.assertIn('sha256sum -c -', script)
        create = next(c for c in self.ssh.commands if c.startswith('docker create'))
        for flag in ('--cap-drop ALL', '--read-only', '--user 65534:65534', '-p 8445:8445/tcp'):
            self.assertIn(flag, create)
        self.assertNotIn('--network host', create)
        self.assertNotIn('--privileged', create)
        config = self.ssh.uploads[manager.CONFIG_DIR + '/node.conf']
        self.assertIn('Mode = l4', config)
        self.assertIn('[Transport mailru]', config)
        self.assertIn('[Transport direct]', config)
        key = self.ssh.uploads[manager.CONFIG_DIR + '/encryption.key'].strip()
        self.assertEqual(len(key), 64)
        self.assertNotIn(key, json.dumps(result))
        self.ssh.exists = True
        status = manager.get_server_status()
        self.assertEqual(status['transports'], ['mailru'])
        self.assertNotIn(key, json.dumps(status))
        self.assertNotIn('cloud.mail.ru', json.dumps(status))

    def test_download_failure_never_creates_container(self):
        manager = self.manager()
        self.ssh.download_code = 1
        with self.assertRaises(RuntimeError):
            self.install(manager)
        self.assertFalse(any(c.startswith('docker create') for c in self.ssh.commands))

    def test_existing_container_refused_before_download(self):
        manager = self.manager()
        self.ssh.exists = True
        with self.assertRaises(ValueError):
            self.install(manager)
        self.assertEqual(self.ssh.scripts, [])

    def test_failed_start_removes_only_new_container(self):
        manager = self.manager()
        self.ssh.running = False
        with self.assertRaises(RuntimeError):
            self.install(manager)
        self.assertIn('docker rm -f ' + 'a' * 64, self.ssh.commands)
        self.assertFalse(any('rm -rf' in c for c in self.ssh.commands))

    def test_busy_port_refused_before_download(self):
        manager = self.manager()
        run = self.ssh.run_sudo_command
        self.ssh.run_sudo_command = lambda c, **kw: ('LISTEN', '', 0) if c.startswith('ss ') else run(c, **kw)
        with self.assertRaises(ValueError):
            self.install(manager)
        self.assertEqual(self.ssh.scripts, [])

    def test_forwarded_public_port_can_be_privileged(self):
        manager = self.manager()
        self.install(manager)
        settings = manager.get_settings('public.example', '443')
        self.assertEqual(settings['endpoint'], 'public.example:443')
        payload = next(json.loads(body) for path, body in self.ssh.uploads.items() if '/share-' in path)
        self.assertEqual(payload['transports'][-1]['dial'], 'public.example:443')
        self.assertTrue(any(c.startswith('rm -f -- ') and '/share-' in c for c in self.ssh.commands))


if __name__ == '__main__':
    unittest.main()
