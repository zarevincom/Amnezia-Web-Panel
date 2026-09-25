"""Public claim profiles must remain private to the browser that created them."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import app as panel


TOKEN = "public-claim-token"


def _request(owner_id=None):
    session = {}
    if owner_id:
        session[panel._claim_owner_session_key(TOKEN)] = owner_id
    return SimpleNamespace(
        session=session,
        client=SimpleNamespace(host="198.51.100.10"),
    )


def _data():
    return {
        "invites": [{
            "id": "invite-1",
            "token_hash": panel._invite_hash(TOKEN),
            "enabled": True,
            "max_reissues": 2,
            "reissue_cooldown_hours": 24,
        }],
        "invite_claims": [
            {
                "id": "claim-owner-one",
                "invite_id": "invite-1",
                "owner_id": "owner-one",
                "server_id": 0,
                "protocol": "awg",
                "client_id": "client-owner-one",
                "device_name": "Owner one phone",
                "reissues": 0,
                "last_reissue_at": None,
            },
            {
                "id": "claim-owner-two",
                "invite_id": "invite-1",
                "owner_id": "owner-two",
                "server_id": 0,
                "protocol": "awg",
                "client_id": "client-owner-two",
                "device_name": "Owner two phone",
                "reissues": 0,
                "last_reissue_at": None,
            },
        ],
        "servers": [{
            "name": "Test VPS",
            "host": "vpn.example.test",
            "protocols": {"awg": {"port": "55424"}},
        }],
        "user_connections": [{
            "id": "connection-owner-one",
            "invite_claim_id": "claim-owner-one",
            "server_id": 0,
            "protocol": "awg",
            "client_id": "client-owner-one",
            "name": "Owner one phone",
        }],
        "audit_log": [],
    }


class TestPublicClaimOwnership:
    def setup_method(self):
        panel.INVITE_ATTEMPTS.clear()
        self.data = _data()

    def test_profiles_require_the_claim_owner_session(self):
        with patch.object(panel, "load_data", return_value=self.data), \
             patch.object(panel, "get_ssh") as get_ssh:
            result = asyncio.run(panel.api_claim_profiles(TOKEN, _request()))

        assert result == {"profiles": []}
        get_ssh.assert_not_called()

    def test_profiles_do_not_include_another_owner_claim(self):
        ssh = MagicMock()
        with patch.object(panel, "load_data", return_value=self.data), \
             patch.object(panel, "get_ssh", return_value=ssh) as get_ssh, \
             patch.object(panel, "get_protocol_manager"), \
             patch.object(panel, "_manager_call", return_value="owner-one-config"):
            result = asyncio.run(
                panel.api_claim_profiles(TOKEN, _request("owner-one"))
            )

        assert [profile["id"] for profile in result["profiles"]] == [
            "claim-owner-one"
        ]
        assert get_ssh.call_count == 1

    def test_reissue_rejects_a_claim_owned_by_another_browser_session(self):
        with patch.object(panel, "load_data", return_value=self.data), \
             patch.object(panel, "get_ssh") as get_ssh, \
             patch.object(panel, "save_data") as save_data:
            result = asyncio.run(
                panel.api_reissue_claim(
                    TOKEN,
                    "claim-owner-one",
                    panel.ClaimRequest(device_name="Replacement phone"),
                    _request("owner-two"),
                )
            )

        assert result.status_code == 400
        assert get_ssh.call_count == 0
        assert save_data.call_count == 0

    def test_reissue_allows_the_claim_owner(self):
        ssh = MagicMock()
        with patch.object(panel, "load_data", return_value=self.data), \
             patch.object(panel, "get_ssh", return_value=ssh), \
             patch.object(panel, "get_protocol_manager"), \
             patch.object(panel, "_manager_call"), \
             patch.object(
                 panel,
                 "_create_invite_profile",
                 new=AsyncMock(return_value=("client-replacement", "new-config")),
             ), \
             patch.object(panel, "save_data") as save_data:
            result = asyncio.run(
                panel.api_reissue_claim(
                    TOKEN,
                    "claim-owner-one",
                    panel.ClaimRequest(device_name="Replacement phone"),
                    _request("owner-one"),
                )
            )

        claim = self.data["invite_claims"][0]
        assert result["claim_id"] == "claim-owner-one"
        assert claim["client_id"] == "client-replacement"
        assert claim["reissues"] == 1
        assert self.data["user_connections"][0]["client_id"] == "client-replacement"
        save_data.assert_called_once_with(self.data)
