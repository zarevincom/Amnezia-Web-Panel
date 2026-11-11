"""Pass Yandex's check in an isolated Windows browser through the router WAN.

Uses the project's existing paramiko/playwright dependencies and installed Chrome.
Does not start OpenFlux, change routing, or read a personal browser profile.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from email.parser import Parser
from http.cookiejar import CookieJar
import ipaddress
import json
from pathlib import Path
import re
import shlex
import socket
import threading
import time
from urllib.parse import urljoin, urlsplit
from urllib.request import Request
from types import SimpleNamespace


USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:153.0) Gecko/20100101 Firefox/153.0'
YANDEX_DOMAINS = ('yandex.ru', 'yandex.net', 'yastatic.net', 'smartcaptcha.yandexcloud.net', 'yandex.com')


def proxy_destination(address):
    target = urlsplit('//' + address)
    host = (target.hostname or '').lower().rstrip('.')
    if (target.port != 443 or target.username or target.password or target.path or
            target.query or target.fragment or not any(
                host == domain or host.endswith('.' + domain) for domain in YANDEX_DOMAINS)):
        raise ValueError('Proxy accepts only Yandex HTTPS destinations')
    return host


def validate_target(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https':
        raise ValueError('Only HTTPS checks are supported')
    proxy_destination(parsed.netloc if parsed.port is not None else parsed.netloc + ':443')


def cookie_export(cookies):
    relevant = [c for c in cookies if c['domain'].lstrip('.') == 'yandex.ru' or
                c['domain'].lstrip('.').endswith('.yandex.ru')]
    if any(c['name'] in ('Session_id', 'sessionid2', 'sessionid', 'L') for c in relevant):
        raise ValueError('Account cookies found. Close this window and retry without signing in.')
    live = [c for c in relevant if c.get('expires', -1) <= 0 or c['expires'] > time.time()]
    if not live:
        raise ValueError('No unexpired Yandex cookies in this browser yet.')
    lines = ['# Netscape HTTP Cookie File']
    for cookie in live:
        domain = ('#HttpOnly_' if cookie.get('httpOnly') else '') + cookie['domain']
        fields = (domain, 'TRUE' if cookie['domain'].startswith('.') else 'FALSE',
                  cookie['path'], 'TRUE' if cookie.get('secure') else 'FALSE',
                  str(max(0, int(cookie.get('expires', 0)))), cookie['name'], cookie['value'])
        if any(any(char in field for char in '\t\r\n\0') for field in fields):
            raise ValueError('Invalid cookie characters')
        lines.append('\t'.join(fields))
    return '\n'.join(lines) + '\n'


def parse_probe(raw, url):
    header_text, editor = raw.split('\0', 1)
    block = header_text.replace('\r\n', '\n').strip().split('\n\n')[-1]
    status_line, _, fields = block.partition('\n')
    status = int(status_line.split()[1])
    headers = Parser().parsestr(fields)
    location = urljoin(url, headers['Location']) if headers['Location'] else None
    if location:
        validate_target(location)
    jar = CookieJar()
    jar.extract_cookies(SimpleNamespace(info=lambda: headers), Request(url))
    cookies = []
    for cookie in jar:
        item = dict(name=cookie.name, value=cookie.value, domain=cookie.domain,
                    path=cookie.path, secure=cookie.secure,
                    httpOnly=cookie.has_nonstandard_attr('HttpOnly'))
        if cookie.expires is not None:
            item['expires'] = cookie.expires
        cookies.append(item)
    return dict(status=status, location=location, cookies=cookies,
                editor=status == 200 and editor.strip() == '1')


def probe_document(ssh, context, doc):
    target = doc
    for attempt in range(10):
        validate_target(target)
        remote(ssh, 'umask 077; cat > /etc/openflux/yandex-cookies.txt.next',
               cookie_export(context.cookies()))
        raw = remote(ssh, "set -e\numask 077\n"
                     "state=$(mktemp -d /tmp/openflux-auth.XXXXXX)\n"
                     "trap 'rm -f \"$state/headers\" \"$state/body\"; rmdir \"$state\"' EXIT\n"
                     "curl -q -4 --noproxy '*' --proto '=https' -sS --connect-timeout 10 --max-time 30 "
                     "--max-filesize 4194304 -b /etc/openflux/yandex-cookies.txt.next "
                     '-D "$state/headers" -o "$state/body" '
                     '-H ' + shlex.quote('Accept-Language: ru-RU,ru;q=0.9') + ' '
                     '-H ' + shlex.quote('Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8') + ' '
                     '-A ' + shlex.quote(USER_AGENT) + ' ' + shlex.quote(target) + '\n'
                     'head -c 65536 "$state/headers"\nprintf "\\0"\n'
                     'if grep -q \'id="client-config"\' "$state/body"; then printf 1; else printf 0; fi')
        result = parse_probe(raw, target)
        if result['cookies']:
            context.add_cookies(result['cookies'])
        if result['editor'] or not result['location']:
            return result
        if 'showcaptcha' in urlsplit(result['location']).path:
            return result
        target = result['location']
    raise RuntimeError('Too many document redirects')


class WanProxy(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Never log URLs or challenge tokens.

    def do_CONNECT(self):
        channel = None
        try:
            host = proxy_destination(self.path)
            addresses = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
            address = addresses[0][4][0]
            if not ipaddress.ip_address(address).is_global:
                raise ValueError('Non-public address')
            channel = self.server.ssh.open_channel('direct-tcpip', (address, 443),
                                                 self.client_address, timeout=20)
            channel.settimeout(60)
            self.connection.settimeout(60)
            self.send_response(200, 'Connection established')
            self.end_headers()

            def upload():
                try:
                    while data := self.connection.recv(65536):
                        channel.sendall(data)
                except (OSError, EOFError):
                    pass
                finally:
                    channel.close()

            threading.Thread(target=upload, daemon=True).start()
            while data := channel.recv(65536):
                self.connection.sendall(data)
        except Exception:
            pass
        finally:
            if channel is not None:
                channel.close()
            self.close_connection = True


def remote(ssh, command, data=None):
    stdin, stdout, stderr = ssh.exec_command(command, timeout=30)
    if data is not None:
        stdin.write(data)
    stdin.channel.shutdown_write()
    output = stdout.read()
    stderr.read()  # Remote messages can contain private URLs; do not print them.
    if stdout.channel.recv_exit_status():
        raise RuntimeError('Router command failed')
    return output.decode()


def main():
    import tkinter as tk
    from tkinter import messagebox, simpledialog
    import paramiko
    from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout

    root = tk.Tk()
    root.withdraw()
    ssh = paramiko.SSHClient()
    ssh.load_host_keys(str(Path.home() / '.ssh/known_hosts'))
    ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
    proxy = None
    try:
        password = simpledialog.askstring('Router SSH - OpenFlux',
                                         'Пароль root@192.168.1.1 (только локально):',
                                         show='*', parent=root)
        if not password:
            return
        ssh.connect('192.168.1.1', username='root', password=password,
                    look_for_keys=False, allow_agent=False, timeout=10)
        password = None
        doc = remote(ssh, "awk -F' *= *' '/^URL *=/ {print $2; exit}' /etc/openflux/client.conf").strip()
        if not re.fullmatch(r'https://(?:docs|disk)\.yandex\.ru/edit/d/[A-Za-z0-9_-]{16,200}', doc):
            raise ValueError('Unsupported document address in router config')
        proxy = ThreadingHTTPServer(('127.0.0.1', 0), WanProxy)
        proxy.daemon_threads = True
        proxy.ssh = ssh.get_transport()
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        proxy_settings = {'server': 'http://127.0.0.1:' + str(proxy.server_port)}
        print('WAN_BROWSER_STARTING', flush=True)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='chrome', headless=False, proxy=proxy_settings)
            context = browser.new_context(user_agent=USER_AGENT, locale='ru-RU', proxy=proxy_settings)
            page = context.new_page()
            # API requests share cookies with this isolated browser. This can expose
            # the HTTP client's challenge that a browser document visit skips.
            target = doc
            for attempt in range(10):
                response = context.request.get(target, max_redirects=0, timeout=30000)
                location = response.headers.get('location')
                status = response.status
                response.dispose()
                if not location or status < 300 or status >= 400:
                    break
                target = urljoin(target, location)
                parsed = urlsplit(target)
                validate_target(target)
                if 'showcaptcha' in parsed.path:
                    break
            try:
                page.goto(target, wait_until='domcontentloaded', timeout=60000)
            except BrowserTimeout:
                pass  # Leave the interactive browser available even if assets stall.
            print('AWAITING_CAPTCHA_IN_ISOLATED_BROWSER', flush=True)
            root.deiconify()
            root.title('OpenFlux — капча через WAN')
            tk.Label(root, text='Пройдите капчу или дождитесь открытия документа в Chrome.\n'
                     'Не входите в личный аккаунт Яндекса.\n'
                     'Затем нажмите «Сохранить результат на роутер».\n'
                     'VK TURN и маршруты остаются без изменений.', padx=20, pady=15).pack()
            state = {'save': False, 'cancel': False}
            tk.Button(root, text='Сохранить результат на роутер',
                      command=lambda: state.update(save=True)).pack(pady=5)
            tk.Button(root, text='Отмена', command=lambda: state.update(cancel=True)).pack(pady=5)
            root.protocol('WM_DELETE_WINDOW', lambda: state.update(cancel=True))
            deadline = time.monotonic() + 900
            while not state['cancel'] and browser.is_connected() and time.monotonic() < deadline:
                root.update()
                page.wait_for_timeout(200)
                if not state['save']:
                    continue
                state['save'] = False
                try:
                    result = probe_document(ssh, context, doc)
                    if not result['editor']:
                        print('ROUTER_HTTP_PROBE_REQUIRES_CHECK', flush=True)
                        if result['location']:
                            page.goto(result['location'], wait_until='domcontentloaded', timeout=60000)
                        messagebox.showwarning('OpenFlux', 'Запрос с роутера ещё требует проверки.\n'
                                               'Пройдите её в Chrome и снова нажмите сохранение.', parent=root)
                        continue
                    export = cookie_export(context.cookies())
                except ValueError as error:
                    messagebox.showwarning('OpenFlux', str(error), parent=root)
                    continue
                remote(ssh, 'umask 077; cat > /etc/openflux/yandex-cookies.txt.next', export)
                remote(ssh, 'set -e\n'
                       'if [ -f /etc/openflux/yandex-cookies.txt ]; then '
                       'cp /etc/openflux/yandex-cookies.txt /etc/openflux/yandex-cookies.before-wan-auth.txt; fi\n'
                       'chmod 600 /etc/openflux/yandex-cookies.txt.next\n'
                       'mv /etc/openflux/yandex-cookies.txt.next /etc/openflux/yandex-cookies.txt')
                print('ROUTER_DOC_HTTP_VERIFIED_COOKIES_INSTALLED_NO_SERVICES_STARTED', flush=True)
                break
            browser.close()
    except Exception as error:
        print('AUTH_HELPER_FAILED: ' + type(error).__name__, flush=True)
        messagebox.showerror('OpenFlux', 'Не удалось завершить авторизацию.\n'
                             'Маршруты и сервисы не менялись.', parent=root)
    finally:
        if proxy is not None:
            proxy.shutdown()
            proxy.server_close()
        ssh.close()
        root.destroy()


if __name__ == '__main__':
    main()
