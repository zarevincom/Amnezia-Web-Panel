# Run after prepending managers/openflux_manager.py, over SSH on a Docker host.
import os
import socket
import subprocess
import tempfile
import shutil


class LocalSSH:
    def run_command(self, command, timeout=60):
        p = subprocess.run(command, shell=True, text=True, capture_output=True, timeout=timeout)
        return p.stdout, p.stderr, p.returncode

    run_sudo_command = run_command

    def run_sudo_script(self, script, timeout=300):
        p = subprocess.run(['bash', '-c', script], text=True, capture_output=True, timeout=timeout)
        return p.stdout, p.stderr, p.returncode

    def upload_file_sudo(self, content, path):
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        os.chmod(path, 0o600)


root = tempfile.mkdtemp(prefix='codex-openflux-smoke-')
suffix = root.rsplit('-', 1)[-1]
name = 'codex-openflux-smoke-' + suffix
client = name + '-client'


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class SmokeManager(OpenFluxManager):
    CONFIG_DIR = root
    CONTAINER_NAME = name


ssh = LocalSSH()
manager = SmokeManager(ssh)
try:
    # Real service transport configuration: an unavailable document retries while
    # direct stays usable. External account/captcha behavior is checked manually.
    port = free_port()
    result = manager.install_protocol(port=port, mailru_url='https://cloud.mail.ru/public/aa/bb')
    assert result['status'] == 'success'
    settings = manager.get_settings('127.0.0.1')
    assert settings['link'].startswith('openflux://v1/')
    print('Pinned binary, service startup and core-generated link: OK', flush=True)
    # Match the service transport's encryption context, using direct for traffic.
    proxy_port = free_port()
    args = ['docker', 'run', '-d', '--name', client, '--network', 'host',
            '--user', '65534:65534', '--read-only', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges',
            '-v', root + '/openflux:/openflux:ro',
            '-v', root + '/encryption.key:/encryption.key:ro',
            '--entrypoint', '/openflux', manager.IMAGE,
            '--role=client', '--inbound=socks5', '--negotiate',
            '--transports=direct:50', '--direct-dial=127.0.0.1:' + str(port),
            '--url=https://cloud.mail.ru/public/aa/bb',
            '--encryption-key-file=/encryption.key', '--socks5=127.0.0.1:' + str(proxy_port)]
    subprocess.run(args, check=True, capture_output=True)
    time.sleep(6)
    response = subprocess.run(['curl', '--fail', '--silent', '--show-error', '--max-time', '30',
        '--socks5-hostname', '127.0.0.1:' + str(proxy_port), 'https://example.com'], capture_output=True)
    assert response.returncode == 0, response.stderr.decode()
    assert b'Example Domain' in response.stdout
    print('Encrypted client -> direct -> L4 exit -> HTTPS: OK', flush=True)
    ssh.run_sudo_command('docker stop ' + name)
    assert not manager.check_container_running()
    # Link export works for a stopped exit, without starting the service.
    assert manager.get_settings('127.0.0.1')['link'].startswith('openflux://v1/')
    print('Stopped-service link export: OK', flush=True)
finally:
    subprocess.run(['docker', 'rm', '-f', client, name], capture_output=True)
    assert os.path.realpath(root).startswith('/tmp/codex-openflux-smoke-')
    shutil.rmtree(root)
    print('Disposable containers and files removed', flush=True)
