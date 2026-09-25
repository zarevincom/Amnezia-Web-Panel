"""Profile delivery, bulk transfer, activity digest and Telegram backup.

No live SSH or Telegram: managers and the Bot API are replaced with fakes.
"""
import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

import activity_stats
import app as panel
import telegram_bot as tg_bot
import user_notifications


def _data(**overrides):
    data = {
        'servers': [
            {'name': 'Finland', 'host': '192.0.2.1', 'protocols': {'awg2': {'port': '55424'}}},
            {'name': 'Germany', 'host': '192.0.2.2', 'protocols': {'awg2': {'port': '55425'}}},
        ],
        'users': [
            {'id': 'mom', 'username': 'Мама', 'telegramId': '111', 'enabled': True, 'traffic_total': 0},
            {'id': 'guest', 'username': 'Гость', 'telegramId': None, 'enabled': True, 'traffic_total': 0},
        ],
        'user_connections': [],
        'settings': {'telegram': {'token': 'test-token'}},
    }
    data.update(overrides)
    return data


class FakeManager:
    def __init__(self, clients, fail_add=(), fail_remove=()):
        self.clients = [dict(c) for c in clients]
        self.fail_add = set(fail_add)
        self.fail_remove = set(fail_remove)
        self.removed = []

    def get_clients(self, protocol):
        return [dict(c) for c in self.clients]

    def add_client(self, protocol, name, host, port):
        if name in self.fail_add:
            raise RuntimeError('target is full')
        client_id = f'new-{name}'
        self.clients.append({'clientId': client_id, 'userData': {'clientName': name}})
        return {'client_id': client_id, 'config': f'[Interface]\n# {name}'}

    def remove_client(self, protocol, client_id):
        if client_id in self.fail_remove:
            raise RuntimeError('source unreachable')
        self.removed.append(client_id)
        self.clients = [c for c in self.clients if c['clientId'] != client_id]


class UserNotificationDecisionTests(unittest.TestCase):
    def test_linked_user_is_queued(self):
        status, user = user_notifications.resolve_recipient(_data(), 'mom', user_notifications.KIND_CREATED)
        self.assertEqual(status, user_notifications.QUEUED)
        self.assertEqual(user['id'], 'mom')

    def test_user_without_telegram_is_reported(self):
        status, _ = user_notifications.resolve_recipient(_data(), 'guest', user_notifications.KIND_CREATED)
        self.assertEqual(status, user_notifications.NO_TELEGRAM)

    def test_missing_bot_token(self):
        data = _data(settings={})
        status, _ = user_notifications.resolve_recipient(data, 'mom', user_notifications.KIND_CREATED)
        self.assertEqual(status, user_notifications.NO_BOT)

    def test_request_flag_and_per_event_settings(self):
        self.assertEqual(
            user_notifications.resolve_recipient(_data(), 'mom', user_notifications.KIND_CREATED, requested=False)[0],
            user_notifications.DISABLED,
        )
        data = _data()
        data['settings']['user_notifications'] = {'enabled': True, 'on_create': True, 'on_transfer': False}
        self.assertEqual(
            user_notifications.resolve_recipient(data, 'mom', user_notifications.KIND_TRANSFERRED)[0],
            user_notifications.DISABLED,
        )
        self.assertEqual(
            user_notifications.resolve_recipient(data, 'mom', user_notifications.KIND_ASSIGNED)[0],
            user_notifications.QUEUED,
        )

    def test_disabled_or_unknown_user(self):
        data = _data()
        data['users'][0]['enabled'] = False
        self.assertEqual(user_notifications.resolve_recipient(data, 'mom', 'created')[0], user_notifications.NO_USER)
        self.assertEqual(user_notifications.resolve_recipient(data, None, 'created')[0], user_notifications.NO_USER)

    def test_schedule_from_thread_uses_bound_loop(self):
        delivered = []

        async def main():
            user_notifications.bind_loop(asyncio.get_running_loop())

            async def deliver():
                delivered.append(True)

            scheduled = await asyncio.to_thread(user_notifications.schedule, lambda: deliver())
            await asyncio.sleep(0.05)
            return scheduled

        self.assertTrue(asyncio.run(main()))
        self.assertEqual(delivered, [True])


class ActivityStatsTests(unittest.TestCase):
    def test_record_traffic_rolls_month_counter(self):
        user, conn = {'traffic_month_key': '2026-08', 'traffic_month': 500}, {}
        now = datetime(2026, 9, 1, 0, 5, tzinfo=timezone.utc)
        activity_stats.record_traffic(user, conn, 100, now)
        self.assertEqual(user['traffic_month'], 100)
        self.assertEqual(user['traffic_month_key'], '2026-09')
        self.assertEqual(conn['last_active_at'], now.isoformat())
        activity_stats.record_traffic(user, conn, 50, now)
        self.assertEqual(user['traffic_month'], 150)

    def test_zero_delta_is_not_activity(self):
        user, conn = {}, {}
        activity_stats.record_traffic(user, conn, 0, datetime.now(timezone.utc))
        self.assertEqual(user, {})
        self.assertEqual(conn, {})

    def test_digest_due_once_per_matching_day(self):
        cfg = {'enabled': True, 'chat_id': '1', 'weekday': 0, 'hour': 10, 'timezone': 'Europe/Moscow'}
        monday_11_msk = datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc)
        self.assertTrue(activity_stats.digest_due(cfg, {}, monday_11_msk))
        self.assertFalse(activity_stats.digest_due(cfg, {'last_sent_at': monday_11_msk.isoformat()}, monday_11_msk + timedelta(hours=2)))
        self.assertFalse(activity_stats.digest_due(cfg, {}, monday_11_msk - timedelta(hours=2)))
        self.assertFalse(activity_stats.digest_due(cfg, {}, monday_11_msk + timedelta(days=1)))
        self.assertFalse(activity_stats.digest_due(dict(cfg, enabled=False), {}, monday_11_msk))

    def test_backup_due_interval(self):
        now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
        cfg = {'enabled': True, 'chat_id': '1', 'interval_hours': 24}
        self.assertTrue(activity_stats.backup_due(cfg, now))
        self.assertFalse(activity_stats.backup_due(dict(cfg, last_sent_at=(now - timedelta(hours=3)).isoformat()), now))
        self.assertTrue(activity_stats.backup_due(dict(cfg, last_sent_at=(now - timedelta(hours=25)).isoformat()), now))
        self.assertFalse(activity_stats.backup_due(dict(cfg, chat_id=''), now))

    def test_digest_uses_baseline_and_lists_silent_users(self):
        now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
        gb = 1024 ** 3
        data = _data()
        data['users'][0].update(traffic_total=12 * gb, last_active_at=now.isoformat())
        data['users'][1].update(traffic_total=3 * gb, last_active_at=(now - timedelta(days=12)).isoformat())
        data['users'].append({'id': 'nobody', 'username': 'Без профиля', 'traffic_total': 99 * gb})
        data['user_connections'] = [
            {'id': 'c1', 'user_id': 'mom', 'server_id': 0},
            {'id': 'c2', 'user_id': 'guest', 'server_id': 0},
        ]
        state = {'baseline_at': (now - timedelta(days=7)).isoformat(), 'baseline': {'mom': 2 * gb, 'guest': 3 * gb}}
        digest = activity_stats.build_digest(data, state, now, inactive_days=7)
        text = digest['text']
        self.assertIn('Мама — 10.0 GB · сегодня', text)
        self.assertIn('Гость — 12 дн. назад', text)
        self.assertIn('Не пользовались 7+ дней', text)
        self.assertNotIn('Без профиля', text)
        self.assertEqual(digest['totals'], {'mom': 12 * gb, 'guest': 3 * gb})


class BulkTransferTests(unittest.TestCase):
    def _run(self, data, source, target, client_ids):
        ssh_by_host = {'192.0.2.1': Mock(name='src'), '192.0.2.2': Mock(name='dst')}
        managers = {id(ssh_by_host['192.0.2.1']): source, id(ssh_by_host['192.0.2.2']): target}
        request = panel.BulkTransferConnectionsRequest(protocol='awg2', client_ids=client_ids, target_server_id=1)
        with patch.object(panel, '_check_admin', return_value=True), \
             patch.object(panel, 'load_data', return_value=data), \
             patch.object(panel, 'save_data') as save, \
             patch.object(panel, 'get_ssh', side_effect=lambda server: ssh_by_host[server['host']]), \
             patch.object(panel, 'get_protocol_manager', side_effect=lambda ssh, proto: managers[id(ssh)]), \
             patch.object(panel, 'notify_user_profile', return_value='queued') as notify:
            result = asyncio.run(panel.api_transfer_connections_bulk(Mock(), 0, request))
        return result, save, notify

    def test_moves_selected_profiles_and_isolates_failures(self):
        data = _data(user_connections=[
            {'id': 'c1', 'user_id': 'mom', 'server_id': 0, 'protocol': 'awg2', 'client_id': 'k1', 'name': 'mom-phone'},
            {'id': 'c2', 'user_id': 'guest', 'server_id': 0, 'protocol': 'awg2', 'client_id': 'k2', 'name': 'guest'},
        ])
        source = FakeManager([
            {'clientId': 'k1', 'userData': {'clientName': 'mom-phone'}},
            {'clientId': 'k2', 'userData': {'clientName': 'guest'}},
            {'clientId': 'k3', 'userData': {'clientName': 'untouched'}},
        ])
        target = FakeManager([], fail_add={'guest'})

        result, save, notify = self._run(data, source, target, ['k1', 'k2'])

        self.assertEqual(result['status'], 'partial')
        self.assertEqual((result['transferred'], result['failed']), (1, 1))
        ok, failed = result['results']
        self.assertEqual((ok['client_id'], ok['status'], ok['user_notification']), ('k1', 'success', 'queued'))
        self.assertEqual((failed['client_id'], failed['status']), ('k2', 'error'))
        self.assertIn('target is full', failed['error'])
        # Only the successful profile left the source; the untouched one stays.
        self.assertEqual(source.removed, ['k1'])
        moved = data['user_connections'][0]
        self.assertEqual((moved['server_id'], moved['client_id']), (1, 'new-mom-phone'))
        self.assertEqual(data['user_connections'][1]['server_id'], 0)
        self.assertGreaterEqual(save.call_count, 2)
        notify.assert_called_once()
        self.assertEqual(notify.call_args[0][2], user_notifications.KIND_TRANSFERRED)
        events = [entry['event'] for entry in data['audit_log']]
        self.assertIn('connection_transferred', events)
        self.assertIn('connections_bulk_transferred', events)

    def test_data_lock_is_free_during_remote_work(self):
        data = _data()
        source = FakeManager([{'clientId': 'k1', 'userData': {'clientName': 'phone'}}])
        target = FakeManager([])
        seen = []
        original_add = target.add_client

        def add_client(*args):
            seen.append(panel.DATA_LOCK.locked())
            return original_add(*args)

        target.add_client = add_client
        result, _, _ = self._run(data, source, target, ['k1'])
        self.assertEqual(result['status'], 'success')
        self.assertEqual(seen, [False])

    def test_links_follow_servers_reordered_during_transfer(self):
        data = _data(user_connections=[
            {'id': 'c1', 'user_id': 'mom', 'server_id': 0, 'protocol': 'awg2', 'client_id': 'k1', 'name': 'phone'},
        ])
        data['servers'][0]['uid'], data['servers'][1]['uid'] = 'fin', 'ger'
        # Another admin swapped the servers while SSH work was running.
        data['servers'].reverse()
        for conn in data['user_connections']:
            conn['server_id'] = 1
        moved = {'source_client_id': 'k1', 'client_id': 'new-k1', 'name': 'phone', 'config': ''}
        user_ids = panel._record_transfer(data, 'fin', 'ger', (0, 1), 'awg2', moved)
        self.assertEqual(user_ids, ['mom'])
        self.assertEqual((data['user_connections'][0]['server_id'], data['user_connections'][0]['client_id']), (0, 'new-k1'))

    def test_failed_source_removal_rolls_back_target(self):
        data = _data()
        source = FakeManager([{'clientId': 'k1', 'userData': {'clientName': 'phone'}}], fail_remove={'k1'})
        target = FakeManager([])
        result, _, notify = self._run(data, source, target, ['k1'])
        self.assertEqual(result['status'], 'error')
        self.assertIn('rolled back', result['results'][0]['error'])
        self.assertEqual(target.clients, [])
        notify.assert_not_called()

    def test_rejects_same_server_and_empty_selection(self):
        with patch.object(panel, '_check_admin', return_value=True), \
             patch.object(panel, 'load_data', return_value=_data()):
            empty = asyncio.run(panel.api_transfer_connections_bulk(
                Mock(), 0, panel.BulkTransferConnectionsRequest(protocol='awg2', client_ids=[], target_server_id=1)))
            same = asyncio.run(panel.api_transfer_connections_bulk(
                Mock(), 0, panel.BulkTransferConnectionsRequest(protocol='awg2', client_ids=['k1'], target_server_id=0)))
        self.assertEqual(empty.status_code, 400)
        self.assertEqual(same.status_code, 400)


class ProfileNotificationRouteTests(unittest.TestCase):
    def test_admin_created_profile_is_queued_for_owner(self):
        data = _data()
        manager = FakeManager([])
        request = panel.AddConnectionRequest(protocol='awg2', name='mom-laptop', user_id='mom')
        with patch.object(panel, '_check_admin', return_value=True), \
             patch.object(panel, 'load_data', return_value=data), \
             patch.object(panel, 'save_data'), \
             patch.object(panel, 'get_ssh', return_value=Mock()), \
             patch.object(panel, 'get_protocol_manager', return_value=manager), \
             patch.object(panel.user_notifications, 'schedule', return_value=True) as schedule:
            result = panel.api_add_connection(Mock(), 0, request)
        self.assertEqual(result['user_notification'], 'queued')
        schedule.assert_called_once()

    def test_opt_out_skips_delivery(self):
        data = _data()
        request = panel.AddConnectionRequest(protocol='awg2', name='x', user_id='mom', notify_user=False)
        with patch.object(panel, '_check_admin', return_value=True), \
             patch.object(panel, 'load_data', return_value=data), \
             patch.object(panel, 'save_data'), \
             patch.object(panel, 'get_ssh', return_value=Mock()), \
             patch.object(panel, 'get_protocol_manager', return_value=FakeManager([])), \
             patch.object(panel.user_notifications, 'schedule') as schedule:
            result = panel.api_add_connection(Mock(), 0, request)
        self.assertEqual(result['user_notification'], 'disabled')
        schedule.assert_not_called()


class TelegramBackupTests(unittest.TestCase):
    def test_backup_refused_without_master_key(self):
        data = _data()
        data['settings']['telegram_backup'] = {'enabled': True, 'chat_id': '1'}
        store = Mock(key_fingerprint='')
        with patch.object(panel, 'load_data', return_value=data), \
             patch.object(panel, 'save_data'), \
             patch.object(panel, 'STATE_STORE', store), \
             patch.object(panel, '_send_telegram_document', new=AsyncMock()) as send:
            with self.assertRaisesRegex(RuntimeError, 'PANEL_MASTER_KEY'):
                asyncio.run(panel.send_telegram_backup())
        send.assert_not_awaited()
        store.export_database.assert_not_called()
        self.assertEqual(data['settings']['telegram_backup']['last_status'], 'error')

    def test_backup_sends_snapshot(self):
        data = _data()
        data['settings']['telegram_backup'] = {'enabled': True, 'chat_id': '42'}
        store = Mock(key_fingerprint='abc123')
        store.plaintext_secret_paths.return_value = []
        store.export_database.return_value = b'SQLite format 3\x00'
        with patch.object(panel, 'load_data', return_value=data), \
             patch.object(panel, 'save_data'), \
             patch.object(panel, 'STATE_STORE', store), \
             patch.object(panel, '_send_telegram_document', new=AsyncMock()) as send:
            asyncio.run(panel.send_telegram_backup())
        token, chat_id, filename, content, caption = send.await_args[0]
        self.assertEqual((token, chat_id, content), ('test-token', '42', b'SQLite format 3\x00'))
        self.assertTrue(filename.endswith('.db'))
        self.assertIn('abc123', caption)
        self.assertEqual(data['settings']['telegram_backup']['last_status'], 'success')
        store.reseal.assert_not_called()
        store.export_database.assert_called_once_with(True)

    def test_plaintext_legacy_secrets_are_resealed_before_sending(self):
        data = _data()
        data['settings']['telegram_backup'] = {'enabled': True, 'chat_id': '42'}
        store = Mock(key_fingerprint='abc123')
        store.plaintext_secret_paths.side_effect = [['servers[0].password'], []]
        store.export_database.return_value = b'SQLite format 3\x00'
        with patch.object(panel, 'load_data', return_value=data), \
             patch.object(panel, 'save_data'), \
             patch.object(panel, 'STATE_STORE', store), \
             patch.object(panel, '_send_telegram_document', new=AsyncMock()) as send:
            asyncio.run(panel.send_telegram_backup())
        store.reseal.assert_called_once()
        send.assert_awaited_once()

    def test_backup_refused_when_reseal_leaves_plaintext(self):
        data = _data()
        data['settings']['telegram_backup'] = {'enabled': True, 'chat_id': '42'}
        store = Mock(key_fingerprint='abc123')
        store.plaintext_secret_paths.return_value = ['servers[0].password']
        with patch.object(panel, 'load_data', return_value=data), \
             patch.object(panel, 'save_data'), \
             patch.object(panel, 'STATE_STORE', store), \
             patch.object(panel, '_send_telegram_document', new=AsyncMock()) as send:
            with self.assertRaisesRegex(RuntimeError, 'still unencrypted'):
                asyncio.run(panel.send_telegram_backup())
        send.assert_not_awaited()
        store.export_database.assert_not_called()


class StorageResealTests(unittest.TestCase):
    def test_key_added_later_reseals_and_compact_export_drops_plaintext(self):
        import tempfile
        from pathlib import Path
        from storage import SQLiteStateStore

        with tempfile.TemporaryDirectory() as tmp:
            db, legacy = Path(tmp) / 'panel.db', Path(tmp) / 'data.json'
            plain = SQLiteStateStore(str(db), str(legacy))
            plain.load()
            plain.save({'servers': [{'host': '192.0.2.1', 'password': 'hunter2-secret'}]})

            keyed = SQLiteStateStore(str(db), str(legacy), master_key='later-key')
            self.assertEqual(keyed.plaintext_secret_paths(), ['servers[0].password'])
            keyed.reseal()
            self.assertEqual(keyed.plaintext_secret_paths(), [])
            self.assertEqual(keyed.load()['servers'][0]['password'], 'hunter2-secret')
            self.assertNotIn(b'hunter2-secret', keyed.export_database(compact=True))


class BotFamilyTests(unittest.IsolatedAsyncioTestCase):
    def test_user_menu_has_stats_button(self):
        keyboard = json.dumps(tg_bot._build_connections_keyboard([], _data(), 'ru'), ensure_ascii=False)
        self.assertIn('user_stats', keyboard)

    def test_stats_text(self):
        data = _data(user_connections=[
            {'id': 'c1', 'user_id': 'mom', 'server_id': 0, 'name': 'phone',
             'last_active_at': datetime.now(timezone.utc).isoformat()},
        ])
        user = dict(data['users'][0], traffic_month=2 * 1024 ** 3,
                    traffic_month_key=datetime.now(timezone.utc).strftime('%Y-%m'))
        text = tg_bot._user_stats_text(data, user, 'ru')
        self.assertIn('2.00 GB', text)
        self.assertIn('phone', text)
        self.assertIn('сегодня', text)

    async def test_delivery_raises_when_chat_unreachable(self):
        api = AsyncMock()
        api.send_message = AsyncMock(return_value={'ok': False, 'description': 'Forbidden: bot was blocked by the user'})
        with self.assertRaisesRegex(RuntimeError, 'blocked'):
            await tg_bot._deliver_profile(
                api, '111', kind='transferred', server={'name': 'Germany'}, proto='awg2',
                conn_name='phone', config='[Interface]', generate_vpn_link_fn=lambda *a: '', lang='ru',
            )
        api.send_message.assert_awaited_once()

    async def test_delivery_fails_when_config_file_is_refused(self):
        api = AsyncMock()
        api.send_message = AsyncMock(return_value={'ok': True})
        api.send_document = AsyncMock(return_value={'ok': False, 'description': 'Bad Request: file is empty'})
        with self.assertRaisesRegex(RuntimeError, 'file is empty'):
            await tg_bot._deliver_profile(
                api, '111', kind='created', server={'name': 'Germany'}, proto='awg2',
                conn_name='phone', config='[Interface]', generate_vpn_link_fn=lambda *a: '', lang='ru',
            )

    async def test_admin_config_view_stays_lenient(self):
        api = AsyncMock()
        api.send_message = AsyncMock(return_value={'ok': False})
        api.send_document = AsyncMock(return_value={'ok': False})
        await tg_bot._send_config_text(api, 1, {'name': 'x'}, 'awg2', 'phone', '[Interface]', lambda *a: '', 'en')
        api.send_document.assert_awaited_once()

    async def test_delivery_sends_intro_then_config(self):
        api = AsyncMock()
        api.send_message = AsyncMock(return_value={'ok': True})
        api.send_document = AsyncMock(return_value={'ok': True})
        await tg_bot._deliver_profile(
            api, '111', kind='transferred', server={'name': 'Germany'}, proto='awg2',
            conn_name='phone', config='[Interface]', generate_vpn_link_fn=lambda *a: 'vpn://x', lang='ru',
        )
        self.assertIn('перенесён', api.send_message.await_args_list[0][0][1])
        api.send_document.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
