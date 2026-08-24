"""
AIVPN Protocol Manager - builds and runs an AIVPN gateway
(https://github.com/infosave2007/aivpn) in Docker on a remote node, and
manages clients entirely through AIVPN's own Management HTTP API.

AIVPN ships a Rust server binary with no public prebuilt image, so
installation clones the upstream repository and builds the official
multi-stage Dockerfile (features: management-api, metrics, neural) — this
mirrors `make server-docker` from the project's own docs and is the build
the maintainers explicitly designed to work with a web panel.

The management API only listens on a Unix domain socket *inside* the
container (`/run/aivpn/api.sock`, tmpfs, not exposed to the host), and the
runtime image ships no HTTP client (curl/wget) — only netcat-openbsd. So
every client CRUD operation here is a raw HTTP/1.1 request assembled by hand
and piped into `nc -U` via `docker exec`, exactly the tool the upstream image
bundles for this purpose. No config files are hand-edited for client
management: create/list/enable/disable/delete/connection-key all go through
`/api/v1/...` on that socket, per crates/aivpn-server/src/management_api.rs.
"""

import json
import logging
import shlex
import uuid
from datetime import datetime

logger = logging.getLogger(__name__)

AIVPN_DEFAULTS = {
    'port': '443',
    'idle_timeout_secs': 300,
}

REPO_URL = 'https://github.com/infosave2007/aivpn.git'
TARBALL_URL = 'https://github.com/infosave2007/aivpn/archive/refs/heads/master.tar.gz'
SRC_DIR = '/opt/amnezia/aivpn-src'
IMAGE_TAG = 'aivpn-server:latest'
API_SOCKET = '/run/aivpn/api.sock'

# Official prebuilt server binary. Upstream CI builds this via
# `make server-docker`, which compiles with "management-api,metrics,neural" —
# the management API this panel talks to is therefore present.
RELEASE_ASSET = 'aivpn-server-linux-x86_64'
RELEASE_URL = (
    f'https://github.com/infosave2007/aivpn/releases/latest/download/{RELEASE_ASSET}'
)


class AIVPNManager:
    """Manages AIVPN protocol installation and client management."""

    PROTOCOL = 'aivpn'
    CONTAINER_NAME = 'amnezia-aivpn'

    def __init__(self, ssh_manager, protocol='aivpn'):
        self.ssh = ssh_manager
        self.protocol = protocol or self.PROTOCOL
        self.instance = self._instance_index(self.protocol)
        self.container_name = self._container_name(self.protocol)
        self.config_dir = self._config_dir(self.protocol)

    # ===================== NAMING / PATHS =====================

    def _instance_index(self, protocol=None):
        parts = str(protocol or self.protocol or '').split('__', 1)
        if len(parts) == 2:
            try:
                return max(1, int(parts[1]))
            except ValueError:
                return 1
        return 1

    def _container_name(self, protocol=None):
        idx = self._instance_index(protocol or self.protocol)
        return self.CONTAINER_NAME if idx <= 1 else f'{self.CONTAINER_NAME}-{idx}'

    def _config_dir(self, protocol=None):
        idx = self._instance_index(protocol or self.protocol)
        base = '/opt/amnezia/aivpn'
        return base if idx <= 1 else f'{base}-{idx}'

    def _config_path(self):
        return f'{self.config_dir}/config/server.json'

    def _mask_dir_host(self):
        return f'{self.config_dir}/masks'

    def _tun_name(self):
        # Unique per instance so multiple gateways can coexist on one host
        # (network_mode: host puts every TUN interface in the same namespace).
        return f'aivpn{self.instance - 1}'

    def _vpn_subnet(self):
        # 10.201.x.0/24 — distinct from AWG's default 10.8.x.0/24 and other
        # protocols on this panel, so multiple instances never collide.
        return f'10.201.{self.instance}.1'

    # ===================== STATUS =====================

    def check_docker_installed(self):
        out, _, code = self.ssh.run_command("docker --version 2>/dev/null")
        if code != 0:
            return False
        out2, _, _ = self.ssh.run_command(
            "systemctl is-active docker 2>/dev/null || service docker status 2>/dev/null"
        )
        return 'active' in out2 or 'running' in out2.lower()

    def check_protocol_installed(self, protocol_type=None):
        name = self._container_name(protocol_type or self.protocol)
        out, _, _ = self.ssh.run_sudo_command(
            f"docker ps -a --filter name=^{name}$ --format '{{{{.Names}}}}'"
        )
        return name in out.strip().split('\n')

    def check_container_running(self, protocol_type=None):
        name = self._container_name(protocol_type or self.protocol)
        out, _, _ = self.ssh.run_sudo_command(
            f"docker ps --filter name=^{name}$ --format '{{{{.Status}}}}'"
        )
        return 'Up' in out

    def get_server_status(self, protocol=None):
        exists = self.check_protocol_installed(protocol)
        running = self.check_container_running(protocol)
        clients_count = 0
        version = None
        port = self._read_listen_port()
        if running:
            status_code, data = self._api_request('GET', '/api/v1/status')
            if status_code == 200 and isinstance(data, dict):
                clients_count = data.get('clients_total', 0)
                version = data.get('version')
            else:
                # Management API not reachable yet (still booting) — fall back
                # to counting clients.json directly so the panel isn't blank.
                clients = self.get_clients()
                clients_count = len(clients)
        return {
            'container_exists': exists,
            'container_running': running,
            'clients_count': clients_count,
            'port': port,
            'version': version,
        }

    def _read_listen_port(self):
        content = self._read_host_file(f'{self.config_dir}/config/server.json')
        if not content:
            return None
        try:
            data = json.loads(content)
            listen = data.get('listen_addr', '')
            if ':' in listen:
                return listen.rsplit(':', 1)[1]
        except Exception:
            pass
        return None

    def _read_host_file(self, path):
        out, _, code = self.ssh.run_sudo_command(f"cat {path} 2>/dev/null")
        if code != 0 or not out.strip():
            return None
        return out

    # ===================== MANAGEMENT API (over unix socket) =====================

    def _api_request(self, method, path, body=None, timeout=30):
        """Issue a raw HTTP/1.1 request against the AIVPN management API's
        Unix socket inside the container, via `docker exec ... nc -U`.

        Returns (status_code, parsed_json_or_text_or_None). On a transport
        failure returns (0, {'error': <human-readable diagnosis>}) so callers
        surface the real cause instead of a generic message.
        """
        body_json = json.dumps(body, ensure_ascii=False) if body is not None else None
        script = self._build_http_script(method, path, body_json)
        cmd = f"docker exec {self.container_name} sh -c {shlex.quote(script)}"
        out, err, code = self.ssh.run_sudo_command(cmd, timeout=timeout)

        if not (out or '').strip():
            reason = self._diagnose(err)
            logger.warning(f"AIVPN API {method} {path} failed: {reason}")
            return 0, {'error': reason}

        return self._parse_http_response(out)

    def _build_http_script(self, method, path, body_json=None):
        """Build a POSIX-sh snippet that speaks HTTP/1.1 to the API socket.

        The request body is written inside the container with a quoted
        heredoc rather than copied in from the host. `docker cp` into the
        container's /tmp is unreliable because /tmp is a tmpfs mount, and the
        heredoc also avoids leaving temp files on the host and any quoting
        problems with client names containing shell metacharacters.
        """
        request_line = (
            'printf "%s %s HTTP/1.1\\r\\nHost: localhost\\r\\n'
        )
        if body_json is None:
            return (
                request_line + 'Connection: close\\r\\n\\r\\n" '
                f'"{method}" "{path}" | nc -U {API_SOCKET}'
            )

        token = uuid.uuid4().hex[:8]
        delim = f'AIVPN_BODY_{token}'
        tmp = f'/tmp/aivpn-req-{token}.json'
        return (
            f"cat > {tmp} <<'{delim}'\n"
            f"{body_json}\n"
            f"{delim}\n"
            f"LEN=$(wc -c < {tmp} | tr -d ' ')\n"
            '{ ' + request_line +
            'Content-Type: application/json\\r\\nContent-Length: %s\\r\\n'
            f'Connection: close\\r\\n\\r\\n" "{method}" "{path}" "$LEN"; '
            f"cat {tmp}; }} | nc -U {API_SOCKET}\n"
            f"rm -f {tmp}\n"
        )

    def _diagnose(self, stderr=''):
        """Work out why the API didn't answer, so the panel can show an
        actionable message instead of 'API failed'."""
        out, _, code = self.ssh.run_sudo_command(
            f"docker inspect -f '{{{{.State.Status}}}}' {self.container_name} 2>/dev/null"
        )
        state = (out or '').strip()
        if code != 0 or not state:
            return (f"контейнер {self.container_name} не найден — "
                    f"AIVPN не установлен на этом сервере или был удалён")

        if state != 'running':
            logs, _, _ = self.ssh.run_sudo_command(
                f"docker logs --tail 15 {self.container_name} 2>&1"
            )
            return (f"контейнер {self.container_name} не запущен (состояние: {state}). "
                    f"Последние строки лога:\n{(logs or '').strip()}")

        sock, _, _ = self.ssh.run_sudo_command(
            f"docker exec {self.container_name} sh -c 'test -S {API_SOCKET} && echo present'"
        )
        if 'present' not in (sock or ''):
            logs, _, _ = self.ssh.run_sudo_command(
                f"docker logs --tail 15 {self.container_name} 2>&1"
            )
            return (f"управляющий API недоступен: сокет {API_SOCKET} не создан. "
                    f"Вероятно, сервер собран без функции management-api либо запущен "
                    f"без --management-socket. Последние строки лога:\n{(logs or '').strip()}")

        nc_check, _, _ = self.ssh.run_sudo_command(
            f"docker exec {self.container_name} sh -c 'command -v nc || true'"
        )
        if not (nc_check or '').strip():
            return ("в образе AIVPN нет netcat (nc) — панель не может обратиться "
                    "к unix-сокету управляющего API")

        return (f"управляющий API не ответил. stderr: "
                f"{(stderr or '').strip() or '(пусто)'}")

    def _parse_http_response(self, raw):
        if not raw:
            return 0, None
        text = raw.replace('\r\n', '\n')
        if '\n\n' in text:
            head, _, body = text.partition('\n\n')
        else:
            head, body = text, ''
        lines = head.split('\n')
        status_code = 0
        if lines:
            parts = lines[0].split(' ')
            if len(parts) >= 2:
                try:
                    status_code = int(parts[1])
                except ValueError:
                    status_code = 0
        data = None
        if body.strip():
            try:
                data = json.loads(body)
            except Exception:
                data = body.strip()
        return status_code, data

    # ===================== BUILD / INSTALL =====================

    def _fetch_sources(self, results):
        """Fetch the AIVPN repo — needed for the Dockerfiles, entrypoint,
        example config and preset masks regardless of which build path we take.
        Falls back to a tarball when git isn't installed on the node."""
        check_out, _, _ = self.ssh.run_sudo_command(
            f"git -C {SRC_DIR} rev-parse --is-inside-work-tree 2>/dev/null"
        )
        if check_out.strip() == 'true':
            self.ssh.run_sudo_command(f"git -C {SRC_DIR} pull --ff-only", timeout=180)
            results.append("AIVPN sources updated")
            return

        self.ssh.run_sudo_command(f"rm -rf {SRC_DIR}")
        self.ssh.run_sudo_command("mkdir -p /opt/amnezia")

        has_git, _, _ = self.ssh.run_sudo_command("command -v git || true")
        if has_git.strip():
            _, err, code = self.ssh.run_sudo_command(
                f"git clone --depth 1 {REPO_URL} {SRC_DIR}", timeout=300
            )
            if code == 0:
                results.append("AIVPN sources downloaded")
                return
            logger.warning(f"git clone failed, falling back to tarball: {err}")

        _, err, code = self.ssh.run_sudo_command(
            f"mkdir -p {SRC_DIR} && curl -fsSL {TARBALL_URL} "
            f"| tar xz -C {SRC_DIR} --strip-components=1",
            timeout=300,
        )
        if code != 0:
            raise RuntimeError(
                f"Не удалось скачать исходники AIVPN (нужен git или curl): {err}"
            )
        results.append("AIVPN sources downloaded")

    def _ensure_image_built(self, results, force=False):
        if not force:
            out, _, _ = self.ssh.run_sudo_command(f"docker images -q {IMAGE_TAG}")
            if out.strip():
                results.append("AIVPN image already present, reusing")
                return

        self._fetch_sources(results)

        # Preferred path: the project publishes an official prebuilt server
        # binary, and its CI builds it via `make server-docker` — i.e. with
        # "management-api,metrics,neural", exactly the feature set this panel
        # needs. Downloading it takes seconds instead of a 5-20 minute Rust
        # release build that also wants several GB of RAM, which is what
        # makes installs fail on modest VPSes running other services.
        results.append("Downloading official AIVPN server binary...")
        _, err, code = self.ssh.run_sudo_command(
            f"mkdir -p {SRC_DIR}/releases && "
            f"curl -fL --retry 3 -o {SRC_DIR}/releases/{RELEASE_ASSET} {RELEASE_URL} && "
            f"chmod +x {SRC_DIR}/releases/{RELEASE_ASSET}",
            timeout=600,
        )

        if code == 0:
            results.append("Binary downloaded, packing it into a container image...")
            _, berr, bcode = self.ssh.run_sudo_command(
                f"docker build -f {SRC_DIR}/deploy/docker/Dockerfile.prebuilt "
                f"-t {IMAGE_TAG} {SRC_DIR}",
                timeout=900,
            )
            if bcode == 0:
                results.append("AIVPN image ready (official prebuilt binary)")
                return
            logger.warning(f"prebuilt image build failed, falling back to source: {berr}")
            results.append("Prebuilt image failed, falling back to building from source...")
        else:
            logger.warning(f"release download failed, falling back to source: {err}")
            results.append("Could not download the release binary, building from source...")

        # Fallback: compile from source. Slow and memory-hungry, but works for
        # architectures or releases with no published asset.
        results.append(
            "Building AIVPN from source (Rust release build — 5-20+ minutes, "
            "needs roughly 2 GB of free RAM)..."
        )
        out, err, code = self.ssh.run_sudo_command(
            f"docker build -t {IMAGE_TAG} {SRC_DIR}", timeout=2400
        )
        if code != 0:
            tail = (err or out or '')[-1500:]
            raise RuntimeError(
                "Не удалось собрать образ AIVPN. Частые причины — нехватка "
                "оперативной памяти или места на диске при сборке Rust. "
                f"Вывод сборки:\n{tail}"
            )
        results.append("AIVPN image built from source")

    def install_protocol(self, protocol_type=None, port=None, force_rebuild=False):
        """Full installation: build image (if needed) -> write per-instance
        config -> run container with management API + connection-key support
        enabled -> wait for the API to come up."""
        protocol_type = protocol_type or self.protocol
        self.instance = self._instance_index(protocol_type)
        self.container_name = self._container_name(protocol_type)
        self.config_dir = self._config_dir(protocol_type)

        port = str(port or AIVPN_DEFAULTS['port'])
        results = []

        if not self.check_docker_installed():
            return {'status': 'error', 'message': 'Docker not installed'}

        self._ensure_image_built(results, force=force_rebuild)

        if self.check_protocol_installed(protocol_type):
            results.append("Removing old container...")
            self.remove_container(protocol_type, keep_config=True)

        results.append("Preparing configuration...")
        config_subdir = f'{self.config_dir}/config'
        mask_dir = self._mask_dir_host()
        self.ssh.run_sudo_command(f"mkdir -p {config_subdir} {mask_dir}")

        server_json = self._build_server_json(port)
        self.ssh.upload_file_sudo(
            json.dumps(server_json, indent=2), f'{config_subdir}/server.json'
        )
        results.append("Configuration written")

        server_host = self.ssh.host
        run_cmd = (
            "docker run -d "
            "--restart unless-stopped "
            "--network host "
            "--cap-add NET_ADMIN --cap-add NET_RAW "
            "--device /dev/net/tun:/dev/net/tun "
            "--tmpfs /run:mode=1777,size=64M "
            "--tmpfs /tmp:mode=1777,size=128M "
            f"-v {config_subdir}:/etc/aivpn "
            f"-v {mask_dir}:/var/lib/aivpn/masks "
            f"--label com.amnezia.protocol=aivpn "
            f"--name {self.container_name} "
            f"{IMAGE_TAG} "
            "--config /etc/aivpn/server.json "
            f"--listen 0.0.0.0:{port} "
            "--key-file /etc/aivpn/server.key "
            "--clients-db /etc/aivpn/clients.json "
            f"--server-ip {server_host}:{port} "
            f"--management-socket {API_SOCKET}"
        )
        results.append("Starting AIVPN container...")
        out, err, code = self.ssh.run_sudo_command(run_cmd)
        if code != 0:
            raise RuntimeError(f"Failed to start AIVPN container: {err}")

        results.append("Waiting for AIVPN gateway to become ready...")
        if not self._wait_for_api():
            results.append(
                "Warning: management API did not respond yet — the gateway "
                "may still be starting. Check status again in a moment."
            )
        else:
            results.append("AIVPN gateway is up")

        return {
            'status': 'success',
            'protocol': protocol_type,
            'port': port,
            'log': results,
        }

    def _build_server_json(self, port):
        return {
            "listen_addr": f"0.0.0.0:{port}",
            "tun_name": self._tun_name(),
            "tun_mtu": "auto",
            "network_config": {
                "server_vpn_ip": self._vpn_subnet(),
                "prefix_len": 24,
                "mtu": "auto",
                "keepalive_secs": 8,
                "ipv6_enabled": False,
                "ipv6_prefix": "fd10:cafe::/48",
            },
            "mask_dir": "/var/lib/aivpn/masks",
            "bootstrap_mask_files": [],
            "idle_timeout_secs": AIVPN_DEFAULTS['idle_timeout_secs'],
            "session_timeout_secs": None,
            "allow_peer_routing": False,
            "neural_enabled": True,
            "downlink_shaping": True,
            "mask_verify_mode": "warn",
            "mask_signing_key": None,
            "mask_operator_pubkey": None,
            "pool": {"peers": [], "sync_key": "", "exit_node": None, "exit_node_enabled": False},
            "site_to_site": {"local_subnets": [], "peers": []},
            "mtls": {"ca_public_key_hex": "", "required": False},
            "dns": {
                "upstream_doh": "https://1.1.1.1/dns-query",
                "fallback_doh": "https://8.8.8.8/dns-query",
                "block_plain_dns": False,
            },
        }

    def _wait_for_api(self, attempts=20, delay=2):
        import time
        for _ in range(attempts):
            status_code, _ = self._api_request('GET', '/api/v1/status', timeout=10)
            if status_code == 200:
                return True
            time.sleep(delay)
        return False

    def remove_container(self, protocol_type=None, keep_config=False):
        name = self._container_name(protocol_type or self.protocol)
        cfg_dir = self._config_dir(protocol_type or self.protocol)
        self.ssh.run_sudo_command(f"docker stop {name} 2>/dev/null || true")
        self.ssh.run_sudo_command(f"docker rm -fv {name} 2>/dev/null || true")
        if not keep_config:
            self.ssh.run_sudo_command(f"rm -rf {cfg_dir}")
        return True

    # ===================== CLIENTS =====================

    def _format_bytes(self, size):
        try:
            size = float(size)
        except (TypeError, ValueError):
            return ''
        power = 2 ** 10
        n = 0
        powers = {0: 'B', 1: 'KiB', 2: 'MiB', 3: 'GiB', 4: 'TiB'}
        while size >= power and n < 4:
            size /= power
            n += 1
        v = round(size, 2)
        if v == int(v):
            v = int(v)
        return f"{v} {powers.get(n, 'B')}"

    def _client_to_panel_shape(self, c):
        """Map one AIVPN ClientResponse onto the panel's client/userData shape.

        Everything the profile view needs is already in the list response
        (see ClientResponse in management_api.rs), so the panel can render a
        full profile card without a second API round-trip per client.
        """
        stats = c.get('stats') or {}
        bytes_in = stats.get('bytes_in') or 0
        bytes_out = stats.get('bytes_out') or 0
        user_data = {
            'clientName': c.get('name', ''),
            'creationDate': c.get('created_at', '') or datetime.now().isoformat(),
            'enabled': c.get('enabled', True),
            # AIVPN-specific profile fields, consumed by the profile modal.
            'vpnIp': c.get('vpn_ip', ''),
            'oneTime': bool(c.get('one_time')),
            'deviceBound': bool(c.get('device_bound')),
            'expiresAt': c.get('expires_at'),
            'totalConnections': stats.get('total_connections', 0),
            'dataReceivedBytes': bytes_in,
            'dataSentBytes': bytes_out,
        }
        last_seen = stats.get('last_connected') or stats.get('last_handshake')
        if last_seen:
            user_data['latestHandshake'] = last_seen
        if bytes_in:
            user_data['dataReceived'] = self._format_bytes(bytes_in)
        if bytes_out:
            user_data['dataSent'] = self._format_bytes(bytes_out)
        qos = c.get('qos')
        if qos:
            user_data['qos'] = qos
        return {
            'clientId': c.get('id', ''),
            'enabled': c.get('enabled', True),
            'userData': user_data,
        }

    def get_clients(self, protocol=None):
        status_code, data = self._api_request('GET', '/api/v1/clients')
        if status_code != 200 or not isinstance(data, list):
            return []
        return [self._client_to_panel_shape(c) for c in data]

    def get_client_details(self, protocol, client_id):
        """Fresh profile data for one client, plus its connection key — this is
        what the panel's 'view profile' action renders."""
        status_code, data = self._api_request('GET', f'/api/v1/clients/{client_id}')
        if status_code != 200 or not isinstance(data, dict):
            message = (data or {}).get('error') if isinstance(data, dict) else None
            raise RuntimeError(message or f"AIVPN profile '{client_id}' not found.")
        shaped = self._client_to_panel_shape(data)
        shaped['config'] = self._connection_key(client_id) or ''
        return shaped

    def add_client(self, protocol, client_name, server_host=None, port=None,
                   expires_at=None, one_time=False):
        body = {'name': client_name, 'one_time': bool(one_time)}
        if expires_at:
            body['expires_at'] = expires_at
        status_code, data = self._api_request('POST', '/api/v1/clients', body=body)
        if status_code != 201 or not isinstance(data, dict) or not data.get('id'):
            message = (data or {}).get('error') if isinstance(data, dict) else None
            raise RuntimeError(message or "AIVPN API failed to create client.")

        client_id = data['id']
        config = self._connection_key(client_id)
        return {
            'client_id': client_id,
            'config': config or '',
            'vpn_ip': data.get('vpn_ip', ''),
            'one_time': bool(data.get('one_time')),
            'expires_at': data.get('expires_at'),
        }

    def edit_client(self, protocol, client_id, params=None):
        """Update a profile in place. Only keys present in `params` are sent,
        so omitted fields keep their current value; `expires_at: None` clears
        the expiry (the API distinguishes absent from null on purpose)."""
        params = params or {}
        body = {}
        if params.get('name'):
            body['name'] = params['name']
        if 'one_time' in params and params['one_time'] is not None:
            body['one_time'] = bool(params['one_time'])
        if 'expires_at' in params:
            # Explicit null clears the expiry; a value sets it.
            body['expires_at'] = params['expires_at'] or None
        if 'enabled' in params and params['enabled'] is not None:
            body['enabled'] = bool(params['enabled'])
        if not body:
            return {'status': 'success', 'message': 'Nothing to update'}

        status_code, data = self._api_request(
            'PATCH', f'/api/v1/clients/{client_id}', body=body
        )
        if status_code != 200:
            message = (data or {}).get('error') if isinstance(data, dict) else None
            raise RuntimeError(message or "AIVPN API failed to update the profile.")
        return {'status': 'success', 'client': self._client_to_panel_shape(data)}

    def reset_device(self, protocol, client_id):
        """Clear the bound device key so a one-time profile can be enrolled
        again (e.g. the user's phone was lost or replaced)."""
        status_code, data = self._api_request(
            'POST', f'/api/v1/clients/{client_id}/reset-device'
        )
        if status_code != 200:
            message = (data or {}).get('error') if isinstance(data, dict) else None
            raise RuntimeError(message or "AIVPN API failed to reset device binding.")
        return {'status': 'success'}

    def _connection_key(self, client_id):
        status_code, data = self._api_request(
            'GET', f'/api/v1/clients/{client_id}/connection-key'
        )
        if status_code == 200 and isinstance(data, dict):
            return data.get('connection_key', '')
        return None

    def get_client_config(self, protocol, client_id, server_host=None, port=None):
        return self._connection_key(client_id) or ''

    def toggle_client(self, protocol, client_id, enable):
        status_code, data = self._api_request(
            'PATCH', f'/api/v1/clients/{client_id}', body={'enabled': bool(enable)}
        )
        if status_code not in (200,):
            message = (data or {}).get('error') if isinstance(data, dict) else None
            raise RuntimeError(message or "AIVPN API failed to update client.")
        return True

    def remove_client(self, protocol, client_id):
        status_code, data = self._api_request('DELETE', f'/api/v1/clients/{client_id}')
        if status_code not in (204, 200):
            message = (data or {}).get('error') if isinstance(data, dict) else None
            raise RuntimeError(message or "AIVPN API failed to remove client.")
        return True

    # ===================== SERVER CONFIG (raw editor) =====================

    def _get_server_config(self, protocol_type=None):
        status_code, data = self._api_request('GET', '/api/v1/config')
        if status_code == 200 and data is not None:
            if isinstance(data, (dict, list)):
                return json.dumps(data, indent=2, ensure_ascii=False)
            return str(data)
        # Fall back to reading the file directly off the host if the API
        # is unreachable (e.g. container just restarted).
        return self._read_host_file(self._config_path()) or ''

    def save_server_config(self, config_content, protocol_type=None):
        try:
            parsed = json.loads(config_content)
        except Exception as e:
            raise RuntimeError(f"Invalid JSON: {e}")
        status_code, data = self._api_request('PUT', '/api/v1/config', body=parsed)
        if status_code != 200:
            message = (data or {}).get('error') if isinstance(data, dict) else None
            raise RuntimeError(message or "AIVPN API rejected the config.")
        return True
