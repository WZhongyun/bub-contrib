---
name: qq
description:
  QQ C2C and group channel skill. Use when Bub is handling a QQ conversation. In the default
  tool reply mode, reply by calling qq.send (text or media) and stay silent by
  not calling it. In direct reply mode, return the reply text; output exactly <no_reply/> to
  stay silent.
metadata:
  channel: qq
---

# QQ Skill

Use this skill when the current conversation is on QQ.

## Execution Policy

- QQ supports C2C and group **passive** replies (`msg_id` + plugin-managed `msg_seq`), with an optional active-message fallback in groups.
- The reply contract depends on `qq.reply_mode` (also in `<qq_response_instruct>`):
  - **tool** (default): the only delivery path is `qq.send` (text or media). Plain final text is discarded. Stay silent by not calling `qq.send`. After a successful `qq.send`, stop — do not send a status recap, and do not call `qq.send` with `<no_reply/>`.
  - **direct**: return the final text. Bub routes it through `QQChannel.send`. Stay silent by outputting exactly `<no_reply/>` and nothing else.
- Do not construct or pass `msg_seq`.
- If the current QQ payload is missing `sender_id` or `message_id`, do not invent protocol fields or shell commands.
- Empty replies are skipped.

## Group Chat

- Session is per group (`qq:group:<group_openid>`), not per sender.
- Inbound JSON includes `chat_type=group`, `group_openid`, `sender_id` (member openid), `sender_name`, and `was_mentioned`.
- You are woken for every group message QQ delivers. A group admin controls that scope in the QQ client (all messages, last 10 @mentions, or @only).
- When `was_mentioned` is false, reply only if you have something useful to add; otherwise stay silent.
- Prefer human messages over other bots. Do not compete or spam the group.
- To @ a member with the native QQ mention chip, call `qq.send` with `at_user_ids` set to their `sender_id`. Do not write `@nickname` as plain text.

## Context Mapping

Current QQ inbound JSON typically includes:

- `message`: normalized text (`<@bot>` mentions stripped)
- `message_id`: inbound id for passive reply
- `sender_id`: C2C `user_openid`, or group `member_openid`
- `sender_name`, `group_openid`, `chat_type`, `was_mentioned`, `date`
- `attachments` (downloaded files also have `local_path` under `inbox/`)
- `message_type` (0=text, 3=ARK card, 101/102/103=quote or chat record)
- `ark_data` (structured card when `message_type` is 3)
- `quoted_messages`
- `workspace`: pwd when `bub gateway` started; keep `fs` / `bash` / `media_path` inside it
- `type=interaction`: a button or menu click (`button_id`, `button_data`, `sender_id`)

## Markdown

QQ C2C and group replies can render native markdown:

- headings: `#` `##` `###`
- `**bold**`, `*italic*`, `~~strikethrough~~`
- unordered/ordered lists, `>` quotes, `***` horizontal rules
- links `[text](https://example.com)` and public-image links

Avoid GFM tables and fenced code blocks (the plugin falls back to plain text).

## Rich media

In tool mode, native image/video/voice/file is `qq.send(media_url=...)` or workspace-local `qq.send(media_path=...)`. Do not wrap the URL in markdown image syntax when you want a native bubble. Rich media is passive-only (not an active group message). The plugin downloads `media_url` into the workspace outbox and uploads the file to QQ — do not `bash`/`curl` it, and do not ask QQ to fetch the URL. If the file is already local, pass `media_path`. If you pass both `content` and media, only the media bubble is sent.

## Text chain (markdown)

- `@user`: `<qqbot-at-user id="<openid>" />` (group)
- insert-into-input: `<qqbot-cmd-input text="xxx" show="xxx" />` (`text`/`show` urlencoded, max 100 chars)
- send-on-click: `<qqbot-cmd-enter text="xxx" />` (not supported in group/text channels)

Do not use the deprecated `<@userid>` / `@everyone` forms.
