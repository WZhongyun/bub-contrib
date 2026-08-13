# bub-qq

QQ Open Platform channel adapter for [Bub](https://bub.build).

Chinese documentation: [README.zh-CN.md](./README.zh-CN.md)

## What it provides

- Bub channel plugin (`entry point`: `qq` → `bub_qq.plugin`)
- Inbound C2C messages (`C2C_MESSAGE_CREATE`) adapted to Bub `ChannelMessage`
- Outbound C2C text replies via QQ OpenAPI (passive reply only)
- Receive transport switch: **webhook** or **websocket** (mutually exclusive on the QQ platform side)
- `onboard_config` so `bub onboard` can collect `appid` / `secret` / `receive_mode`
- Bundled skill resources under `src/skills/qq`

Current focus is **single-chat (C2C)** receive and reply. Group / guild flows are not covered yet.

## Prerequisites

1. [Install Bub](https://bub.build/docs/getting-started/install/) (recommended: `uv tool install bub`)
2. Run `bub onboard` and ensure model access works (`bub chat` or `bub run`)
3. Create a QQ bot on the [QQ Open Platform](https://bot.q.qq.com/wiki/develop/api-v2/) and obtain `APPID` / `SECRET`

## Install (end users)

`bub-qq` is not on PyPI. With a global Bub install (`uv tool install bub`), install the plugin into **Bub’s own environment**:

```bash
bub install bub-qq@main
```

This resolves to the official monorepo package:

```text
git+https://github.com/bubbuild/bub-contrib.git@main#subdirectory=packages/bub-qq
```

Equivalent forms:

```bash
# Full Git URL (use https:// …, not git+https://, when passing to bub install)
bub install "https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-qq"

# Pin a tag or commit when available
bub install bub-qq@<tag-or-sha>
```

Verify the plugin is loaded:

```bash
bub hooks
```

You should see the `qq` plugin among discovered entry points / hook providers.

### Upgrade / uninstall

```bash
bub update bub-qq
bub uninstall bub-qq
```

### Notes

- Do **not** use bare `bub install bub-qq`. A name without `@ref` is treated as a PyPI package name.
- `bub install` requires Bub to run inside a virtual environment (including the environment created by `uv tool install bub`) and `uv` on `PATH`.

## Install (local development)

Editable install into the same environment that runs `bub`.

### Option A — global Bub (`uv tool`)

```bash
uv pip install -e /path/to/bub-contrib/packages/bub-qq \
  --python ~/.local/share/uv/tools/bub/bin/python
```

Then use the global CLI as usual:

```bash
bub hooks
bub gateway
```

### Option B — Bub / monorepo project venv

From a uv project that already depends on Bub:

```bash
uv add --editable /path/to/bub-contrib/packages/bub-qq
# or, from bub-contrib workspace workflows:
uv pip install -e packages/bub-qq
```

### Option C — raw Git install into a chosen interpreter

```bash
uv pip install \
  "git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-qq" \
  --python /path/to/the/python/that/runs/bub
```

## Configuration

Settings can come from:

- the `qq:` section in `~/.bub/config.yml`
- `BUB_QQ_*` environment variables (including values loaded from `.env`)
- `bub onboard`, which interactively collects the required fields when the `qq` channel is enabled

Env vars override YAML, so shared policy can live in `config.yml` while secrets stay in the environment.

### Required

| YAML field (`qq.*`) | Env var | Description |
| --- | --- | --- |
| `appid` | `BUB_QQ_APPID` | QQ bot app ID |
| `secret` | `BUB_QQ_SECRET` | QQ bot secret |
| `receive_mode` | `BUB_QQ_RECEIVE_MODE` | Inbound transport: `webhook` or `websocket` |

`receive_mode` must match the QQ developer console:

- `webhook` — starts the embedded webhook server only; WebSocket is not started
- `websocket` — starts the WebSocket client only; the embedded webhook server is not started

QQ treats webhook and WebSocket as **mutually exclusive**. After a valid HTTPS webhook callback URL is configured successfully, WebSocket delivery is no longer supported on the platform side.

Gateway start fails if `appid` / `secret` are empty, or if `receive_mode` is not `webhook` / `websocket`.

### Optional

| YAML field (`qq.*`) | Env var | Default | Description |
| --- | --- | --- | --- |
| `token_url` | `BUB_QQ_TOKEN_URL` | `https://bots.qq.com/app/getAppAccessToken` | Access token endpoint |
| `openapi_base_url` | `BUB_QQ_OPENAPI_BASE_URL` | `https://api.sgroup.qq.com` | OpenAPI base URL |
| `timeout_seconds` | `BUB_QQ_TIMEOUT_SECONDS` | `30` | HTTP timeout for token and OpenAPI |
| `token_refresh_skew_seconds` | `BUB_QQ_TOKEN_REFRESH_SKEW_SECONDS` | `60` | Refresh token this many seconds before expiry |
| `webhook_host` | `BUB_QQ_WEBHOOK_HOST` | `127.0.0.1` | Embedded webhook bind host |
| `webhook_port` | `BUB_QQ_WEBHOOK_PORT` | `8080` | Embedded webhook port (`80` / `443` / `8080` / `8443` allowed by QQ) |
| `webhook_path` | `BUB_QQ_WEBHOOK_PATH` | `/qq/webhook` | Webhook path |
| `webhook_callback_timeout_seconds` | `BUB_QQ_WEBHOOK_CALLBACK_TIMEOUT_SECONDS` | `15` | Reserved for future callback controls |
| `verify_signature` | `BUB_QQ_VERIFY_SIGNATURE` | `true` | Enforce webhook signature verification |
| `inbound_dedupe_size` | `BUB_QQ_INBOUND_DEDUPE_SIZE` | `1024` | Recent inbound `msg_id` cache size |
| `websocket_intents` | `BUB_QQ_WEBSOCKET_INTENTS` | `1 << 25` | WebSocket identify intents (`GROUP_AND_C2C_EVENT`) |
| `websocket_use_shard_gateway` | `BUB_QQ_WEBSOCKET_USE_SHARD_GATEWAY` | `false` | Use `/gateway/bot` recommended shard count |
| `websocket_reconnect_delay_seconds` | `BUB_QQ_WEBSOCKET_RECONNECT_DELAY_SECONDS` | `5` | Delay before WebSocket reconnect |

Example:

```yaml
qq:
  appid: your_app_id
  secret: your_secret
  receive_mode: websocket
```

```bash
export BUB_QQ_APPID=your_app_id
export BUB_QQ_SECRET=your_secret
export BUB_QQ_RECEIVE_MODE=websocket
```

## Run

QQ is a channel listener surface. Start Bub gateway after the plugin is installed and configured:

```bash
bub gateway
```

For webhook mode, expose a public HTTPS URL that reaches the embedded server (host/port/path above) and register it in the QQ bot console. For websocket mode, ensure the console is **not** locked into a successful webhook-only configuration.

CLI chat (`bub chat`) does not replace the QQ channel; use gateway for QQ IO.

## Session and message mapping

| Concept | Format / behavior |
| --- | --- |
| Session ID (C2C) | `qq:c2c:<user_openid>` |
| Chat ID (C2C) | `c2c:<user_openid>` |
| Inbound event | `C2C_MESSAGE_CREATE` |
| Command messages | inbound text starting with `,` is forwarded as Bub `kind=command` |
| Outbound | Text (`msg_type = 0`), **passive reply** only (`msg_id` + plugin-managed `msg_seq`) |
| Passive window | replies are skipped once the latest inbound timestamp is older than 60 minutes |
| Debounce | `needs_debounce = true` |

Active push is intentionally not used: official docs state active C2C push stopped being provided on 2025-04-21.

## Payload shape

Inbound non-command messages are encoded as a JSON string, including fields like:

- `message`
- `message_id`
- `type` (`text` or `attachment`)
- `sender_id` (QQ `user_openid`)
- `date`
- `attachments` (when present)

Normal replies should return final text and let Bub outbound routing call `QQChannel.send`. Do not call `qq_send.py` or invent `msg_seq` for ordinary C2C replies.

## Status

### Supported today

- Config via `qq:` YAML, `BUB_QQ_*`, and `bub onboard`
- Access token from `https://bots.qq.com/app/getAppAccessToken` with cached refresh (60s renewal window)
- `aiohttp` OpenAPI client with `Authorization: QQBot {ACCESS_TOKEN}`
- Embedded webhook receiver, callback validation (`op = 13`), ed25519 signature flows
- Webhook request verification (`X-Signature-Ed25519`, `X-Signature-Timestamp`)
- WebSocket receive path with reconnect / resume and optional sharding
- C2C inbound adaptation, `msg_id` dedupe, 60-minute passive text replies
- In-memory send idempotency for the same `session_id + msg_id + msg_seq`
- OpenAPI error surfacing (HTTP status, platform code, `X-Tps-trace-ID`) and error catalog metadata
- Automated tests for config, auth, signatures, channel, webhook, websocket, gateway, plugin onboarding, and C2C services

### Not yet

- QQ group / channel / broader DM send APIs
- Wider webhook event coverage beyond validation and basic `{"op":12}` ack
- Group and other non-C2C event types
- Dynamic in-process shard rebalancing after startup

## Confirmed interface rules

From official QQ Bot docs (API auth + event subscription):

**Auth / OpenAPI**

- Token: `POST https://bots.qq.com/app/getAppAccessToken` body `{ appId, clientSecret }`
- Token lifetime up to `7200` seconds; refresh within `60` seconds of expiry returns a new token while the old remains valid during the overlap
- OpenAPI base: `https://api.sgroup.qq.com`
- Header: `Authorization: QQBot {ACCESS_TOKEN}`; trace header `X-Tps-trace-ID`

**Events / transport**

- Production webhooks require HTTPS; ports `80`, `443`, `8080`, `8443`
- Webhook and WebSocket are mutually exclusive once a valid HTTPS callback is configured
- Validation requests use `op = 13`; response must include `plain_token` and ed25519 signature over `event_ts + plain_token`
- Normal webhook verification uses `timestamp + raw_body`
- Event payload shape: `{ id, op, d, s, t }`
- `C2C_MESSAGE_CREATE` intent: `GROUP_AND_C2C_EVENT` (`1 << 25`)
- Documented `C2C_MESSAGE_CREATE.d` fields used here: `id`, `author.user_openid`, `content`, `timestamp`, `attachments`
- WebSocket close codes `4914` / `4915` are fatal; codes such as `4006`–`4009` and `4900`–`4913` are treated as reconnectable

## Official documentation

- [QQ Bot Developer Documentation](https://bot.q.qq.com/wiki/develop/api-v2/)
- [Bub docs](https://bub.build/docs/)
- [Bub plugin install](https://bub.build/docs/getting-started/install/) / CLI: `bub install`

Use the QQ docs for app creation, credentials, event subscription, and callback settings (`APPID`, `SECRET`, webhook URL, intents, etc.).

## Development

```bash
uv run --package bub-qq pytest -q
```

Tests use mocks — no live QQ network required.

## License

Same as the [bub-contrib](https://github.com/bubbuild/bub-contrib) repository.
