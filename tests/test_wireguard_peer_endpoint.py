from managers.awg_manager import AWGManager
from managers.wireguard_manager import WireGuardManager


WG_SHOW_OUTPUT = """interface: wg0
  public key: server-key
  listening port: 51820

peer: peer-key
  endpoint: 198.51.100.25:49564
  allowed ips: 10.8.0.2/32
  latest handshake: 5 seconds ago
"""


class FakeSsh:
    def run_sudo_command(self, _command):
        return WG_SHOW_OUTPUT, "", 0


def test_awg_wg_show_retains_observed_peer_endpoint():
    manager = AWGManager.__new__(AWGManager)
    manager.ssh = FakeSsh()
    manager._container_name = lambda _protocol: "amnezia-awg"
    manager._wg_binary = lambda _protocol: "awg"

    peers = manager._wg_show("awg")

    assert peers["peer-key"]["endpoint"] == "198.51.100.25:49564"


def test_wireguard_wg_show_retains_observed_peer_endpoint():
    manager = WireGuardManager.__new__(WireGuardManager)
    manager.ssh = FakeSsh()

    peers = manager._wg_show()

    assert peers["peer-key"]["endpoint"] == "198.51.100.25:49564"
