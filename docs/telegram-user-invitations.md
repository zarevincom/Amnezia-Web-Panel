# Telegram User Invitations

Telegram user invitations bind an existing panel user to a Telegram account
without asking an administrator to obtain the person's numeric Telegram ID.
After the binding, the existing Telegram bot shows the VPN connections assigned
to that panel user and can deliver their configurations through its normal
connection menu.

## Prerequisites

1. Configure a valid Telegram Bot API token in **Settings**.
2. Enable the Telegram bot and make sure it is running.
3. Create a panel user and assign any existing VPN connections to that user.
   The user must be enabled and must not already have a Telegram ID.

The panel checks the bot with Telegram's `getMe` API before it creates an
invitation. This prevents issuing a deep link for an invalid bot token. The
feature does not require exposing the panel over the public internet: Telegram
delivers the deep-link update to the bot's existing polling connection.

## Administrator Workflow

1. Open **Users** as an administrator.
2. Find a user without a Telegram ID and select the Telegram invitation button.
3. Choose an expiry date and time. The default validity is seven days; the
   maximum validity is 30 days.
4. Select **Create link** and copy the generated `t.me/<bot>?start=tg_...`
   URL.
5. Send the URL to the intended recipient using a channel appropriate for the
   access being granted.

The raw URL is shown only in the creation response and in the open modal. It
cannot be retrieved from the API later. If the recipient has not used it yet,
issuing another link automatically revokes the previous pending link for that
user.

The user card displays a pending-invitation marker and its expiry. Once the
recipient accepts the invitation, the marker is removed and the numeric
Telegram ID appears on the user card.

## Recipient Workflow

1. Open the invitation link.
2. Telegram opens a private chat with the configured bot and sends
   `/start tg_...`.
3. The bot validates the invitation, links the sender's immutable numeric
   Telegram ID to the panel user, and invalidates the invitation.
4. The bot confirms the linking and displays the recipient's current VPN
   connections. The recipient can use the existing buttons or `/connections`
   to request configurations.

The initial bot response does not broadcast configurations. A recipient can
only retrieve a configuration after selecting a connection assigned to their
own panel user.

## Security Model

- Each deep-link payload is generated with `secrets.token_urlsafe(24)` and has
  well over 128 bits of entropy.
- The panel stores only `SHA-256(payload)` in the encrypted SQLite state; it
  never stores the usable URL or raw payload.
- A payload is valid once, for one enabled panel user, until its expiry. It is
  deactivated immediately after successful binding.
- Only an administrator can issue a link. Support users and external bearer
  tokens cannot issue one through this endpoint.
- The bot accepts invitation payloads only in private chats. Group messages
  cannot bind an account or reveal a user's VPN connections.
- A numeric Telegram ID may be linked to only one panel user. Mutable Telegram
  usernames are recorded as audit metadata only and are never used for
  authentication.
- Invalid, expired, already-used, revoked, disabled-user, and conflicting
  invitations receive the same generic bot error. The response does not reveal
  usernames, server details, profile names, or invitation state.
- The panel writes `telegram_invite_created` and `telegram_invite_accepted`
  events to the audit log. They contain IDs and timestamps, never the raw
  invitation payload.

## API

The endpoint appears in Swagger UI and ReDoc under the **Invites** group.

### Create a Telegram invitation

`POST /api/users/{user_id}/telegram-invites`

The endpoint requires an authenticated administrator session. It is intended
for the admin UI and deliberately does not accept API bearer tokens.

Request body:

```json
{
  "expires_at": "2026-10-01T12:00:00Z"
}
```

`expires_at` is optional. Omit it to use the seven-day default. The accepted
range is greater than the current time and no more than 30 days in the future.

Successful response:

```json
{
  "invite_id": "8b3f...",
  "expires_at": "2026-10-01T12:00:00+00:00",
  "url": "https://t.me/panel_bot?start=tg_..."
}
```

The `url` property is the only response field that contains a usable secret.
Do not place it in logs, issue trackers, or public chats.

Common responses:

| Status | Meaning |
| --- | --- |
| `400` | The bot is disabled, the token cannot be verified, the user is disabled, or the expiry is invalid. |
| `403` | No administrator session is present. |
| `404` | The target panel user does not exist. |
| `409` | The target user already has a Telegram ID. Clear or replace that binding deliberately before issuing a new link. |

`GET /api/users` exposes only `telegram_invite_pending` and
`telegram_invite_expires_at` for the admin UI. It never exposes the raw
payload, hash, or `t.me` URL.

## Troubleshooting

- **The Users page says the bot must be enabled.** Enable the Telegram bot in
  Settings, save the token, and start it before generating a link.
- **Telegram opens the bot but no confirmation arrives.** Confirm that the bot
  is running and that its long-polling process has outbound access to
  `api.telegram.org`.
- **The bot says the invitation is unavailable.** Create a replacement link.
  The original may have expired, been used, or been revoked by a newer link.
- **The recipient sees no profiles.** The account was linked successfully, but
  no `user_connections` are assigned to that panel user. Assign or create a
  VPN connection for the same user in the panel.
- **The recipient needs a different Telegram account.** An administrator must
  intentionally clear the existing Telegram ID from the panel user, then issue
  a new invitation. This avoids silently transferring VPN access between
  Telegram accounts.
