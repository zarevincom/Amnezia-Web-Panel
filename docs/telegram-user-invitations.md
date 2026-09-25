# Telegram User Invitations

Telegram invitations bind a panel user to a Telegram account without asking an
administrator to obtain a numeric Telegram ID. The administrator creates a
new `tg_user` by name, or selects an existing user, directly in the panel's
Telegram bot. A `tg_user` has the same profile-level permissions as `user`,
but has no password by default because Telegram authenticates the recipient.
The bot then returns a one-time `t.me` link that the administrator forwards to
the recipient.

Once the recipient opens the link in a private chat, the bot binds the
recipient's immutable Telegram ID to that panel user and displays only that
user's VPN profiles.

## Prerequisites

1. Configure a valid Telegram Bot API token in **Settings**.
2. Enable the Telegram bot and make sure it is running.
3. The administrator's panel account must already be bound to their Telegram
   account and have the `admin` role.

The bot reads its own username from Telegram at startup with `getMe`. It does
not need the panel to be public: Telegram delivers deep-link updates through
the existing long-polling connection.

## Administrator Workflow

### Create a new user and invitation

1. Open a private chat with the bot and send `/start`.
2. Select **Create user**.
3. Send the display name for the new panel user.
4. The bot creates a passwordless Telegram user (`role: tg_user`) and sends the one-time
   `t.me/<bot>?start=tg_...` URL.
5. Use **Copy invitation link** to copy it, then forward the URL to the
   recipient without opening it yourself.

### Invite an existing user

1. Select **Users** and choose a user that does not have a Telegram ID.
2. Select **Create Telegram invitation**.
3. Use **Copy invitation link** and forward the URL to the intended recipient.

Links do not expire automatically. A link becomes invalid after it is used,
when its user is disabled, or when an administrator creates a replacement
link for the same user. The web Users page shows a pending marker, but it does
not create or display invitation URLs.

## Recipient Workflow

1. Open the invitation URL.
2. Telegram opens a private chat with the configured bot and sends
   `/start tg_...`.
3. The bot validates the link, associates the sender's numeric Telegram ID
   with the panel user, and invalidates the link immediately.
4. The bot confirms the association and displays the recipient's current VPN
   connections. The recipient can use the existing buttons or `/connections`
   to request their configurations.

The initial response does not broadcast configurations. A recipient can only
retrieve a configuration assigned to their own panel user.

### Report a VPN issue

The recipient can press **VPN is not working** in their private connection
menu. The bot sends the report to every enabled `admin` account linked to the
bot. If none are linked, it falls back to the configured server-alert chat.

For AmneziaWG and WireGuard profiles, the report includes the public endpoint
observed by the VPS for the most recent peer handshake. Telegram does not expose
the sender's network IP to bots, and other protocols report the IP as
unavailable. The observed IP is sent only to administrators and is not retained
in the audit log.

## Security Model

- Each deep-link payload is generated with `secrets.token_urlsafe(24)` and
  has well over 128 bits of entropy.
- The panel stores only `SHA-256(payload)` in encrypted SQLite state. It never
  stores the usable URL or raw payload.
- A payload is single-use. A replacement revokes every older pending link for
  the same user before the new link is created.
- Only an administrator in a private chat can create users or issue links
  through the bot. The HTTP endpoint separately requires an admin session.
- The bot accepts invitation payloads only in private chats. Group messages
  cannot bind an account or reveal a user's VPN connections.
- A numeric Telegram ID can be linked to only one panel user. Mutable Telegram
  usernames are audit metadata only and are never used for authentication.
- Invalid, used, revoked, disabled-user, and conflicting invitations receive
  the same generic bot error. The response does not reveal usernames, server
  details, profile names, or invitation state.
- The panel writes `telegram_user_created`, `telegram_invite_created`,
  `telegram_invite_accepted`, and `telegram_vpn_problem_reported` audit events.
  They contain IDs and timestamps, never raw invitation payloads or observed IPs.

## API

The endpoint remains available in Swagger UI and ReDoc under **Invites** for
authenticated administrative integrations. The Telegram bot and the endpoint
use the same invitation service, so they apply identical token hashing,
single-use, and replacement rules.

### Create a Telegram invitation for an existing user

`POST /api/users/{user_id}/telegram-invites`

The endpoint requires an authenticated administrator session and is not
available to external bearer tokens. It accepts an empty JSON body:

```json
{}
```

Successful response:

```json
{
  "invite_id": "8b3f...",
  "url": "https://t.me/panel_bot?start=tg_..."
}
```

The `url` property is the only response field that contains a usable secret.
Do not place it in logs, issue trackers, or public chats.

Common responses:

| Status | Meaning |
| --- | --- |
| `400` | The bot is disabled or cannot be verified, or the user is disabled. |
| `403` | No administrator session is present. |
| `404` | The target panel user does not exist. |
| `409` | The target user already has a Telegram ID. Clear or replace that binding deliberately before issuing a new link. |

`GET /api/users` exposes only `telegram_invite_pending` and
`telegram_invite_expires_at` (which is `null` for the new non-expiring links)
for the admin interface. It never exposes the raw payload, hash, or `t.me`
URL.

## Troubleshooting

- **The bot cannot create an invitation.** Enable the Telegram bot in Settings,
  save the token, and start it. The bot must have a public Telegram username.
- **Telegram opens the bot but no confirmation arrives.** Confirm that the bot
  is running and that its long-polling process has outbound access to
  `api.telegram.org`.
- **The bot says the invitation is unavailable.** Create a replacement link.
  The original may have already been used, revoked, or have a disabled user.
- **The recipient sees no profiles.** The account was linked successfully, but
  no `user_connections` are assigned to that panel user. Assign or create a
  VPN connection for the same user in the panel.
- **The recipient needs a different Telegram account.** An administrator must
  intentionally clear the existing Telegram ID from the panel user, then issue
  a new invitation. This avoids silently transferring VPN access between
  Telegram accounts.
