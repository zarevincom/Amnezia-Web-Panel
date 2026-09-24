"""Telegram deep-link invitation tests without live Telegram or SSH calls."""
import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

import app as panel
import telegram_bot as tg_bot


def _start_update(telegram_id, payload, chat_type="private"):
    return {
        "message": {
            "chat": {"id": telegram_id, "type": chat_type},
            "from": {"id": telegram_id, "first_name": "Test"},
            "text": f"/start {payload}",
        }
    }


def _invite(payload, user_id="user-1", expires_at=None):
    return {
        "id": "invite-1",
        "user_id": user_id,
        "token_hash": tg_bot._telegram_invite_hash(payload),
        "enabled": True,
        "expires_at": (expires_at or datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        "accepted_at": None,
    }


class TestTelegramBotInvites:
    def setup_method(self):
        self.payload = "tg_" + "A" * 32
        self.data = {
            "settings": {"self_service": {"enabled": False, "telegram_enabled": False}},
            "users": [{"id": "user-1", "username": "alice", "enabled": True}],
            "user_connections": [
                {"id": "conn-1", "user_id": "user-1", "name": "Alice phone"}
            ],
            "telegram_invites": [_invite(self.payload)],
            "audit_log": [],
        }
        self.api = AsyncMock()
        self.api.send_message = AsyncMock()
        self.saved = []

    async def _dispatch(self, update):
        await tg_bot._dispatch(
            self.api,
            update,
            lambda: self.data,
            lambda config: f"vpn://{config}",
            lambda data: self.saved.append(copy.deepcopy(data)),
        )

    def test_start_payload_must_have_telegram_invite_shape(self):
        assert tg_bot._start_payload("/start tg_" + "A" * 32) == self.payload
        assert tg_bot._start_payload("/start other") is None
        assert tg_bot._start_payload("/start") is None

    def test_deep_link_binds_user_and_exposes_only_own_profiles(self):
        asyncio.run(self._dispatch(_start_update(333, self.payload)))

        user = self.data["users"][0]
        invite = self.data["telegram_invites"][0]
        assert user["telegramId"] == "333"
        assert invite["telegram_id"] == "333"
        assert invite["accepted_at"]
        assert not invite["enabled"]
        assert self.saved
        assert self.data["audit_log"][-1]["event"] == "telegram_invite_accepted"
        messages = [call.args[1] for call in self.api.send_message.call_args_list]
        assert any("Alice phone" in json.dumps(call.kwargs.get("reply_markup", {})) for call in self.api.send_message.call_args_list)
        assert any("linked" in message.lower() for message in messages)

    def test_expired_link_does_not_mutate_user_or_state(self):
        self.data["telegram_invites"][0] = _invite(
            self.payload,
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        asyncio.run(self._dispatch(_start_update(333, self.payload)))

        assert "telegramId" not in self.data["users"][0]
        assert not self.saved
        assert "unavailable" in self.api.send_message.call_args.args[1].lower()

    def test_used_link_cannot_be_reused(self):
        asyncio.run(self._dispatch(_start_update(333, self.payload)))
        self.api.send_message.reset_mock()
        asyncio.run(self._dispatch(_start_update(444, self.payload)))

        assert self.data["users"][0]["telegramId"] == "333"
        assert "unavailable" in self.api.send_message.call_args.args[1].lower()

    def test_group_chat_cannot_claim_link(self):
        asyncio.run(self._dispatch(_start_update(-100, self.payload, chat_type="group")))

        assert "telegramId" not in self.data["users"][0]
        assert not self.saved
        assert "private" in self.api.send_message.call_args.args[1].lower()

    def test_telegram_account_already_linked_elsewhere_cannot_claim(self):
        self.data["users"].append({"id": "user-2", "username": "bob", "enabled": True, "telegramId": "333"})
        asyncio.run(self._dispatch(_start_update(333, self.payload)))

        assert "telegramId" not in self.data["users"][0]
        assert not self.saved
        assert "unavailable" in self.api.send_message.call_args.args[1].lower()


class TestTelegramInviteApi:
    def setup_method(self):
        self.data = {
            "settings": {"telegram": {"token": "not-a-real-token", "enabled": True}},
            "users": [{"id": "user-1", "username": "alice", "enabled": True}],
            "telegram_invites": [],
            "audit_log": [],
        }
        self.request = Mock()
        self.admin = {"id": "admin-1", "role": "admin"}

    def test_admin_can_issue_hashed_one_time_deep_link(self):
        with patch.object(panel, "get_current_user", return_value=self.admin), \
             patch.object(panel, "load_data", return_value=self.data), \
             patch.object(panel, "save_data") as save_data, \
             patch.object(panel, "_get_telegram_bot_username", new=AsyncMock(return_value="panel_bot")):
            result = asyncio.run(
                panel.api_create_telegram_invite(
                    self.request,
                    "user-1",
                    panel.TelegramInviteRequest(),
                )
            )

        payload = result["url"].split("?start=", 1)[1]
        invite = self.data["telegram_invites"][0]
        assert result["url"].startswith("https://t.me/panel_bot?start=tg_")
        assert invite["token_hash"] == panel._telegram_invite_hash(payload)
        assert payload not in json.dumps(invite)
        assert invite["created_by"] == "admin-1"
        assert self.data["audit_log"][-1]["event"] == "telegram_invite_created"
        save_data.assert_called_once_with(self.data)

    def test_replacement_revokes_previous_pending_link(self):
        self.data["telegram_invites"] = [{
            "id": "old-invite",
            "user_id": "user-1",
            "enabled": True,
            "accepted_at": None,
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        }]
        with patch.object(panel, "get_current_user", return_value=self.admin), \
             patch.object(panel, "load_data", return_value=self.data), \
             patch.object(panel, "save_data"), \
             patch.object(panel, "_get_telegram_bot_username", new=AsyncMock(return_value="panel_bot")):
            asyncio.run(panel.api_create_telegram_invite(self.request, "user-1", panel.TelegramInviteRequest()))

        assert not self.data["telegram_invites"][0]["enabled"]
        assert self.data["telegram_invites"][0]["revoked_at"]
        assert self.data["telegram_invites"][1]["enabled"]

    def test_admin_api_is_not_available_without_an_admin_session(self):
        with patch.object(panel, "get_current_user", return_value=None):
            response = asyncio.run(
                panel.api_create_telegram_invite(
                    self.request,
                    "user-1",
                    panel.TelegramInviteRequest(),
                )
            )

        assert response.status_code == 403

    def test_link_is_not_issued_when_the_bot_is_disabled(self):
        self.data["settings"]["telegram"]["enabled"] = False
        with patch.object(panel, "get_current_user", return_value=self.admin), \
             patch.object(panel, "load_data", return_value=self.data), \
             patch.object(panel, "_get_telegram_bot_username", new=AsyncMock()) as get_bot:
            response = asyncio.run(
                panel.api_create_telegram_invite(
                    self.request,
                    "user-1",
                    panel.TelegramInviteRequest(),
                )
            )

        assert response.status_code == 400
        get_bot.assert_not_awaited()

    def test_openapi_documents_telegram_invitation_endpoint(self):
        schema = panel.app.openapi()
        assert "/api/users/{user_id}/telegram-invites" in schema["paths"]
