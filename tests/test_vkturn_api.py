import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app as panel
from managers.vkturn_manager import VkTurnManager
from test_vkturn_manager import WG_CONFIG


class VkTurnAPITests(unittest.TestCase):
    def data(self):
        return {'servers': [{'host': '203.0.113.7', 'protocols': {}}],
                'settings': {}, 'user_connections': []}

    def test_registration_and_marketplace(self):
        self.assertIn('vkturn', panel.BASE_PROTOCOLS)
        self.assertEqual(panel.protocol_container_name('vkturn'), 'amnezia-vkturn')
        self.assertEqual(panel.INSTALL_PORT_TRANSPORT['vkturn'], 'udp')
        self.assertIsInstance(panel.get_protocol_manager(Mock(), 'vkturn'), VkTurnManager)

    def test_marketplace_exposes_install_settings_and_wireguard_export(self):
        from managers.backup_manager import BackupManager
        source = (Path(__file__).resolve().parents[1] / 'templates/server.html').read_text(encoding='utf-8')
        for marker in ('id="proto-vkturn"', "proto: 'vkturn'", 'id="vkTurnModal"',
                       'function openVkTurnSettings', 'function exportVkTurnClient',
                       "openVkTurnSettings('${escapeJs(client.clientId)}'"):
            self.assertTrue(marker in source, marker)
        self.assertEqual(BackupManager(Mock())._paths_for('vkturn', 'amnezia-vkturn'),
                         {'host': ['/opt/amnezia/vkturn'], 'container': []})

    def test_install_records_new_backend_without_secret_in_state(self):
        data, ssh, manager = self.data(), Mock(), Mock()
        manager.install_protocol.return_value = {
            'status': 'success', 'port': 56000, 'backend_port': 51820,
            'wireguard_created': True, 'wireguard_port': 51820, 'version': 'v1.10.1',
        }
        with patch.object(panel, '_check_admin', return_value=True), \
             patch.object(panel, 'load_data', return_value=data), \
             patch.object(panel, 'save_data'), \
             patch.object(panel, 'get_ssh', return_value=ssh), \
             patch.object(panel, 'get_used_ports', return_value={'udp': {}}), \
             patch.object(panel, 'ensure_docker_installed', return_value=''), \
             patch.object(panel, 'get_protocol_manager', return_value=manager):
            result = asyncio.run(panel.api_install_protocol(
                Mock(), 0, panel.InstallProtocolRequest(protocol='vkturn', port='56000')))
        self.assertIsInstance(result, dict)
        self.assertEqual(data['servers'][0]['protocols']['vkturn']['backend_port'], 51820)
        self.assertEqual(data['servers'][0]['protocols']['wireguard']['port'], 51820)
        self.assertNotIn('wrap_key', json.dumps(data))

    def test_admin_boundary_precedes_ssh_for_settings_and_export(self):
        with patch.object(panel, '_check_admin', return_value=False), \
             patch.object(panel, 'get_ssh', side_effect=AssertionError('unauthorized SSH')):
            self.assertEqual(panel.api_vkturn_settings(Mock(), 0).status_code, 403)
            self.assertEqual(panel.api_vkturn_client(Mock(), 0, panel.VkTurnClientRequest(client_id='x')).status_code, 403)

    def test_wireguard_cannot_be_deleted_or_reinstalled_while_proxy_depends_on_it(self):
        data = self.data()
        data['servers'][0]['protocols']['vkturn'] = {'installed': True}
        with patch.object(panel, '_check_admin', return_value=True), \
             patch.object(panel, 'load_data', return_value=data), \
             patch.object(panel, 'get_ssh', side_effect=AssertionError('must reject before SSH')):
            response = asyncio.run(panel.api_uninstall_protocol(Mock(), 0, panel.ProtocolRequest(protocol='wireguard')))
            self.assertEqual(response.status_code, 409)
            response = asyncio.run(panel.api_install_protocol(Mock(), 0, panel.InstallProtocolRequest(protocol='wireguard')))
            self.assertEqual(response.status_code, 409)

    def test_export_returns_local_config_and_respects_public_proxy_endpoint(self):
        data, ssh, manager = self.data(), Mock(), Mock()
        data['servers'][0]['protocols']['vkturn'] = {
            'installed': True, 'port': 56000, 'public_host': 'proxy.example.com', 'public_port': '57000'}
        manager.get_client_config.return_value = WG_CONFIG
        with patch.object(panel, '_check_admin', return_value=True), \
             patch.object(panel, 'load_data', return_value=data), \
             patch.object(panel, 'get_ssh', return_value=ssh), \
             patch.object(panel, 'get_protocol_manager', return_value=manager), \
             patch.object(VkTurnManager, 'get_settings', return_value={'port': 56000, 'wrap_key': 'a' * 64}):
            result = panel.api_vkturn_client(Mock(), 0, panel.VkTurnClientRequest(
                client_id='peer', vk_link='https://vk.ru/call/join/test'))
        self.assertEqual(result['endpoint'], 'proxy.example.com:57000')
        self.assertIn('Endpoint = 127.0.0.1:9000', result['config'])
        self.assertTrue(result['ios_link'].startswith('vkturnproxy://'))
        ssh.disconnect.assert_called_once()

    def test_export_rejects_bad_server_and_call_link_before_ssh(self):
        with patch.object(panel, '_check_admin', return_value=True), \
             patch.object(panel, 'load_data', return_value=self.data()), \
             patch.object(panel, 'get_ssh', side_effect=AssertionError('invalid SSH')):
            self.assertEqual(panel.api_vkturn_settings(Mock(), -1).status_code, 404)
            self.assertEqual(panel.api_vkturn_client(Mock(), 0, panel.VkTurnClientRequest(
                client_id='peer', vk_link='https://evil.test/x')).status_code, 400)

    def test_install_merges_fresh_state_by_uid_and_applies_default_proxy_port(self):
        before, after = self.data(), self.data()
        before['servers'][0]['uid'] = 'target'
        after['servers'][0].update(uid='target', name='Edited while installing')
        after['users'] = [{'id': 'new-user'}]
        after['servers'].insert(0, {'uid': 'unrelated', 'host': '203.0.113.8', 'protocols': {}})
        ssh, manager = Mock(), Mock()
        manager.install_protocol.return_value = {
            'status': 'success', 'port': 56000, 'backend_port': 51820,
            'wireguard_created': False, 'wireguard_port': 51820, 'version': 'v1.10.1',
        }
        with patch.object(panel, '_check_admin', return_value=True), \
             patch.object(panel, 'load_data', side_effect=[before, after]), \
             patch.object(panel, 'save_data') as save, \
             patch.object(panel, 'get_ssh', return_value=ssh), \
             patch.object(panel, 'get_used_ports', return_value={'udp': {}}), \
             patch.object(panel, 'ensure_docker_installed', return_value=''), \
             patch.object(panel, 'get_protocol_manager', return_value=manager):
            result = asyncio.run(panel.api_install_protocol(Mock(), 0, panel.InstallProtocolRequest(protocol='vkturn')))
        self.assertIsInstance(result, dict)
        saved = save.call_args.args[0]
        self.assertEqual(saved['users'], [{'id': 'new-user'}])
        self.assertEqual(saved['servers'][1]['name'], 'Edited while installing')
        self.assertIn('vkturn', saved['servers'][1]['protocols'])
        self.assertNotIn('vkturn', saved['servers'][0]['protocols'])
        self.assertEqual(manager.install_protocol.call_args.kwargs['port'], '56000')


if __name__ == '__main__':
    unittest.main()
