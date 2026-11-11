"""Check the local authorizer's network and cookie boundaries."""
import http.cookiejar
import importlib.util
from pathlib import Path
import tempfile
import socket
import threading
import time
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / 'scripts/openflux-router/authorize-yandex.py'
spec = importlib.util.spec_from_file_location('openflux_wan_auth', SOURCE)
auth = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auth)


class WanAuthTests(unittest.TestCase):
    def test_redirects_allow_default_https_port_and_keep_query_private(self):
        auth.validate_target('https://docs.yandex.ru/showcaptcha?token=private')
        for url in ('http://docs.yandex.ru/showcaptcha', 'https://evil.test/',
                    'https://user@docs.yandex.ru/showcaptcha'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                auth.validate_target(url)

    def test_proxy_only_accepts_https_yandex_destinations(self):
        self.assertEqual(auth.proxy_destination('docs.yandex.ru:443'), 'docs.yandex.ru')
        self.assertEqual(auth.proxy_destination('captcha-api.yandex.ru:443'), 'captcha-api.yandex.ru')
        for address in ('localhost:443', '192.168.1.1:443', 'docs.yandex.ru:22',
                        'evil-yandex.ru:443', 'yandex.ru.attacker.test:443',
                        'user@docs.yandex.ru:443', 'docs.yandex.ru:443/path'):
            with self.subTest(address=address), self.assertRaises(ValueError):
                auth.proxy_destination(address)

    def test_export_round_trips_and_excludes_other_sites(self):
        cookies = [self.cookie('spravka', httpOnly=True), self.cookie('yandexuid'),
                   self.cookie('unrelated', domain='.other.test')]
        export = auth.cookie_export(cookies)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cookies.txt'
            path.write_text(export, encoding='utf-8')
            jar = http.cookiejar.MozillaCookieJar()
            jar.load(str(path), ignore_discard=True)
            self.assertEqual({cookie.name for cookie in jar}, {'spravka', 'yandexuid'})

    def test_export_refuses_account_sessions(self):
        for name in ('Session_id', 'sessionid2', 'L'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                auth.cookie_export([self.cookie('spravka'), self.cookie(name)])

    def test_export_accepts_anonymous_session_without_captcha_marker(self):
        self.assertIn('yandexuid', auth.cookie_export([self.cookie('yandexuid')]))

    def test_export_requires_live_cookies(self):
        for cookies in ([], [self.cookie('spravka', expires=time.time() - 30)]):
            with self.subTest(cookies=cookies), self.assertRaises(ValueError):
                auth.cookie_export(cookies)

    def test_probe_reads_captcha_redirect_and_new_cookies_without_logging_values(self):
        raw = ('HTTP/1.1 302 Found\r\nLocation: /showcaptcha?token=private\r\n'
               'Set-Cookie: yandexuid=anonymous; Domain=.yandex.ru; Path=/; Secure; HttpOnly\r\n'
               '\r\n\x000')
        result = auth.parse_probe(raw, 'https://docs.yandex.ru/edit/d/document')
        self.assertEqual(result['status'], 302)
        self.assertEqual(result['location'], 'https://docs.yandex.ru/showcaptcha?token=private')
        self.assertFalse(result['editor'])
        self.assertEqual(result['cookies'][0]['name'], 'yandexuid')
        self.assertTrue(result['cookies'][0]['httpOnly'])

    def test_probe_requires_editor_page_not_just_http_200(self):
        self.assertFalse(auth.parse_probe('HTTP/1.1 200 OK\r\n\r\n\x000',
                                         'https://docs.yandex.ru/')['editor'])
        self.assertTrue(auth.parse_probe('HTTP/1.1 200 OK\r\n\r\n\x001',
                                        'https://docs.yandex.ru/')['editor'])

    def test_export_rejects_cookie_line_injection(self):
        with self.assertRaises(ValueError):
            auth.cookie_export([self.cookie('spravka', value='value\nforged\tline')])

    def test_connect_proxy_relays_bytes_over_ssh_channel(self):
        local, upstream = socket.socketpair()
        transport = mock.Mock()
        transport.open_channel.return_value = local
        proxy = auth.ThreadingHTTPServer(('127.0.0.1', 0), auth.WanProxy)
        proxy.daemon_threads = True
        proxy.ssh = transport
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        try:
            with mock.patch.object(auth.socket, 'getaddrinfo', return_value=[
                    (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('1.1.1.1', 443))]):
                with socket.socket() as client:
                    client.settimeout(3)
                    client.connect(proxy.server_address)
                    client.sendall(b'CONNECT docs.yandex.ru:443 HTTP/1.1\r\nHost: docs.yandex.ru:443\r\n\r\n')
                    response = b''
                    while b'\r\n\r\n' not in response:
                        response += client.recv(1024)
                    self.assertIn(b'200 Connection established', response)
                    client.sendall(b'outbound-bytes')
                    upstream.settimeout(3)
                    self.assertEqual(upstream.recv(1024), b'outbound-bytes')
                    upstream.sendall(b'inbound-bytes')
                    self.assertEqual(client.recv(1024), b'inbound-bytes')
            transport.open_channel.assert_called_once()
            self.assertEqual(transport.open_channel.call_args.args[:2],
                             ('direct-tcpip', ('1.1.1.1', 443)))
        finally:
            local.close()
            upstream.close()
            proxy.shutdown()
            proxy.server_close()
            thread.join(timeout=3)

    @staticmethod
    def cookie(name, **changes):
        return dict(name=name, value='test-value', domain='.yandex.ru', path='/',
                    secure=True, httpOnly=False, expires=int(time.time()) + 3600) | changes


if __name__ == '__main__':
    unittest.main()
