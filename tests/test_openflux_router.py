"""Exercise the real activation shell with isolated command stubs."""
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import unittest

BASH = 'C:/Program Files/Git/bin/bash.exe' if os.name == 'nt' else shutil.which('bash')
SOURCE = Path(__file__).resolve().parents[1] / 'scripts/openflux-router/activate.sh'


def posix(path):
    value = str(path.resolve()).replace('\\', '/')
    return '/' + value[0].lower() + value[2:] if os.name == 'nt' else value


@unittest.skipUnless(BASH and Path(BASH).is_file(), 'bash is required')
class ActivationTests(unittest.TestCase):
    def run_script(self, mode, answer='YES\n'):
        with tempfile.TemporaryDirectory(prefix='openflux-test-') as temporary:
            root = Path(temporary)
            for name in ('bin', 'tmp', 'config', 'init'):
                (root / name).mkdir()
            def write(path, body):
                (root / path).write_bytes(('#!/bin/sh\n' + body).encode())
            base = posix(root)
            write('bin/curl', '''case "$*" in
*--interface*)
 if [ -e "$BASE/switched" ] && [ -e "$BASE/config/route-enabled" ]; then echo pending-marker-leaked >> "$BASE/calls"; fi
 if [ "$MODE" = unready ]; then exit 28; fi
 if [ -e "$BASE/switched" ] && [ "$MODE" = retry ] && [ ! -e "$BASE/retried" ]; then touch "$BASE/retried"; exit 28; fi ;;
esac
printf '37.252.5.64'
''')
            write('bin/ip', '''case "$*" in
*"route show table 200"*) echo 'default dev vkturn scope link' ;;
*"route get"*) echo '9.9.9.9 from 198.19.0.1 dev oflux0 table 201' ;;
*"rule show"*) echo '40: from all iif br-lan lookup 201' ;;
esac
''')
            write('bin/sleep', 'exit 0\n')
            write('bin/nslookup', 'exit 0\n')
            write('bin/sing-box', 'exit 0\n')
            write('init/vkturn', 'exit 0\n')
            write('init/openflux', 'echo "openflux-$1" >> "$BASE/calls"\n')
            write('config/route-up.sh', 'echo route-up >> "$BASE/calls"\ntouch "$BASE/switched"\n')
            write('config/restore-vkturn.sh', 'echo restore >> "$BASE/calls"\nrm -f "$BASE/switched"\n')
            probe = SOURCE.with_name('check-tun.sh')
            if probe.exists():
                (root / 'config/check-tun.sh').write_bytes(probe.read_bytes())
            source = SOURCE.read_text().replace('/etc/openflux', base + '/config')
            source = source.replace('/etc/init.d/openflux', base + '/init/openflux')
            source = source.replace('/etc/init.d/vkturn', base + '/init/vkturn')
            source = source.replace('/usr/bin/sing-box', base + '/bin/sing-box')
            source = source.replace('/tmp/', base + '/tmp/')
            # Do not launch a three-minute daemon in the isolated test.
            source = source.replace('  sleep 180', '  exit 0')
            (root / 'activate.sh').write_bytes(source.encode())
            environment = dict(os.environ, BASE=base, MODE=mode)
            command = f'export PATH="{base}/bin:$PATH"; chmod +x "{base}"/bin/* "{base}"/init/* "{base}"/config/*; sh "{base}/activate.sh"'
            result = subprocess.run([BASH, '-c', command], input=answer.encode(),
                                    capture_output=True, env=environment, timeout=15)
            result.stdout = result.stdout.decode()
            result.stderr = result.stderr.decode()
            calls = (root / 'calls').read_text().splitlines() if (root / 'calls').exists() else []
            return result, calls

    def test_bad_tun_never_switches_lan(self):
        result, calls = self.run_script('unready')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('route-up', calls, result.stdout + result.stderr)

    def test_transient_post_switch_failure_is_retried(self):
        result, calls = self.run_script('retry')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('openflux-enable', calls)
        self.assertNotIn('restore', calls)

    def test_missing_confirmation_restores_vkturn(self):
        result, calls = self.run_script('healthy', answer='')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('restore', calls)
        self.assertNotIn('openflux-enable', calls)

    def test_unconfirmed_cutover_has_no_persistent_boot_marker(self):
        result, calls = self.run_script('healthy')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('pending-marker-leaked', calls)

    def test_failed_preflight_stops_its_background_client(self):
        result, calls = self.run_script('unready')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('openflux-stop', calls)

    def test_rollback_restores_routes_before_stopping_processes(self):
        with tempfile.TemporaryDirectory(prefix='openflux-rollback-') as temporary:
            root = Path(temporary)
            for name in ('bin', 'config', 'init'):
                (root / name).mkdir()
            base = posix(root)
            def write(path, body):
                (root / path).write_bytes(('#!/bin/sh\n' + body).encode())
            write('bin/ip', 'exit 0\n')
            write('bin/logger', 'exit 0\n')
            write('init/vkturn', 'exit 0\n')
            write('init/openflux', 'echo "openflux-$1" >> "$BASE/calls"\n')
            write('config/route-down.sh', 'echo route-down >> "$BASE/calls"\n')
            source = SOURCE.with_name('restore-vkturn.sh').read_text()
            source = source.replace('/etc/openflux', base + '/config')
            source = source.replace('/etc/init.d/', base + '/init/')
            (root / 'restore.sh').write_bytes(source.encode())
            command = f'export PATH="{base}/bin:$PATH"; chmod +x "{base}"/bin/* "{base}"/init/* "{base}"/config/*; sh "{base}/restore.sh"'
            result = subprocess.run([BASH, '-c', command], capture_output=True,
                                    env=dict(os.environ, BASE=base), timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = (root / 'calls').read_text().splitlines()
            self.assertLess(calls.index('route-down'), calls.index('openflux-stop'))

    def test_gateway_does_not_restore_lan_when_tun_probe_fails(self):
        with tempfile.TemporaryDirectory(prefix='openflux-gateway-') as temporary:
            root = Path(temporary)
            for name in ('bin', 'config', 'tmp'):
                (root / name).mkdir()
            base = posix(root)
            def write(path, body):
                (root / path).write_bytes(('#!/bin/sh\n' + body).encode())
            write('bin/sing-box', 'exec /usr/bin/sleep 2\n')
            write('bin/ip', 'exit 0\n')
            write('bin/curl', "printf '37.252.5.64'\n")
            write('bin/sleep', 'exit 0\n')
            write('bin/logger', 'exit 0\n')
            write('config/route-up.sh', 'echo route-up >> "$BASE/calls"\n')
            write('config/route-down.sh', 'echo route-down >> "$BASE/calls"\n')
            write('config/check-tun.sh', 'echo tun-probe >> "$BASE/calls"\nexit 1\n')
            (root / 'config/route-enabled').touch()
            source = SOURCE.with_name('run-gateway.sh').read_text()
            source = source.replace('/etc/openflux', base + '/config')
            source = source.replace('/usr/bin/sing-box', base + '/bin/sing-box')
            source = source.replace('/tmp/', base + '/tmp/')
            (root / 'gateway.sh').write_bytes(source.encode())
            command = f'export PATH="{base}/bin:$PATH"; chmod +x "{base}"/bin/* "{base}"/config/*; sh "{base}/gateway.sh"'
            subprocess.run([BASH, '-c', command], capture_output=True,
                           env=dict(os.environ, BASE=base), timeout=10)
            calls = (root / 'calls').read_text().splitlines()
            self.assertNotIn('route-up', calls)
            self.assertIn('route-down', calls)


@unittest.skipUnless(BASH and Path(BASH).is_file(), 'bash is required')
class InitTests(unittest.TestCase):
    def run_init(self, cookies):
        with tempfile.TemporaryDirectory(prefix='openflux-init-') as temporary:
            root = Path(temporary)
            (root / 'bin').mkdir()
            (root / 'config').mkdir()
            base = posix(root)
            for name in ('sha256sum', 'sing-box'):
                (root / 'bin' / name).write_bytes(b'#!/bin/sh\nexit 0\n')
            if cookies is not None:
                (root / 'config/yandex-cookies.txt').write_text(cookies)
            (root / 'tmp').mkdir()
            cache = root / 'tmp/openflux-browser-cookies.json'
            cache.write_text('{"stale": {"old": "cookie"}}')
            original = root / 'config/cookies.json'
            original.write_text('{"kept": {"existing": "cookie"}}')
            source = SOURCE.with_name('openflux.init').read_text()
            source = source.replace('/etc/openflux', base + '/config')
            source = source.replace('/usr/bin/sing-box', base + '/bin/sing-box')
            source = source.replace('/tmp/', base + '/tmp/')
            (root / 'openflux.init').write_bytes(source.encode())
            command = '''export PATH="$BASE/bin:$PATH"
chmod +x "$BASE"/bin/*
procd_open_instance() { :; }
procd_close_instance() { :; }
procd_set_param() { if [ "$1" = command ]; then printf '%s\\n' "$@"; fi; }
. "$BASE/openflux.init"
start_service
'''
            result = subprocess.run([BASH, '-c', command], capture_output=True,
                                    env=dict(os.environ, BASE=base), timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual(original.read_text(), '{"kept": {"existing": "cookie"}}')
            return result.stdout.decode().splitlines(), base, cache.exists()

    def test_init_passes_yandex_cookies_file_when_present(self):
        commands, base, cache_exists = self.run_init('# Netscape HTTP Cookie File\n')
        self.assertIn('--yandex-cookies-file', commands)
        index = commands.index('--yandex-cookies-file')
        self.assertEqual(commands[index + 1], base + '/config/yandex-cookies.txt')
        self.assertIn('--cookie-store', commands)
        index = commands.index('--cookie-store')
        self.assertEqual(commands[index + 1], base + '/tmp/openflux-browser-cookies.json')
        self.assertFalse(cache_exists, 'Stale JSON must not replace imported browser cookies')

    def test_init_keeps_command_without_missing_or_empty_cookie_file(self):
        for cookies in (None, ''):
            with self.subTest(cookies=cookies):
                commands, base, cache_exists = self.run_init(cookies)
                self.assertTrue(cache_exists)
                self.assertEqual(commands, [
                    'command', base + '/tmp/openflux-core', '--config',
                    base + '/config/client.conf', 'command', base + '/config/run-gateway.sh'])


if __name__ == '__main__':
    unittest.main()
