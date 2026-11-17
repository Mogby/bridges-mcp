# Telegram events, quotes, and reactions

Telegram connections route to the configured sessions. Normal messages retain the
original text or caption after the fenced `bridge_context` metadata. The enabled
tool schemas and `get_protocol_docs` describe the connected deployment.

## Reply quotes

When Telegram supplies a selected reply quote, Bridges forwards `quote` in the
metadata. It contains `text`, `position`, and any `entities`, `is_manual`, or
`rich_texts` fields supplied by Telegram. Quotes from rich messages may have empty
`text` and carry their content in `rich_texts`.

Positions and entity offsets use UTF-16 code units, as Telegram defines them.
Bridges preserves them without slicing or rewriting the quote. The new message
body remains unchanged. Quoted commands do not become Telegram admin commands.

`reply_to_message_id` still refers to the original message in the same chat when
Telegram provides it. Bridges does not fetch or archive that message, and has no
message-history lookup tool. A quote can be present for an external reply without
a same-chat `reply_to_message_id`.

## Incoming reaction events

The poller explicitly subscribes to `message`, `message_reaction`, and
`message_reaction_count`. Telegram controls whether it delivers reaction updates.
The [Bot API](https://core.telegram.org/bots/api#update) documents administrator
rights and explicit subscription as requirements. Private-chat reaction delivery
is not guaranteed by this implementation. Reactions set by bots do not generate
reaction updates. Anonymous reaction counts may be grouped and delayed.

Reaction envelopes have the normal `bridge`, `connection_id`, `message_id`, and
`received_at` fields, plus:

- `event`: `message_reaction` or `message_reaction_count`.
- `update_id`: Telegram's update identifier. The poller deduplicates a bounded
  window of updates while it runs; this is not a durable event inbox.
- `message_id`: the message being reacted to, not a new message or event ID.
- `reaction`: `date` as Telegram's Unix timestamp, plus the fields below.

For `message_reaction`, `reaction` contains `actor` with `type` (`user` or `chat`)
and `id`, and the `old_reaction` and `new_reaction` arrays. An empty new array means
the actor removed their reaction. Actor names and unrelated profile fields are
not forwarded. For `message_reaction_count`, `reaction.reactions` contains the
current `{type, total_count}` entries; no individual actor is inferred.

Reaction bodies are empty. They enter the normal session queue and retain the
normal turn and typing lifecycle, but are not parsed as admin commands. Treat an
emoji as event data; do not infer approval or unrelated instructions from it alone.

## Send or remove a reaction

`telegram_set_reaction` accepts a configured `connection_id`, positive integer
`message_id`, required `reaction`, and optional boolean `is_big`:

```json
{"connection_id":"personal","message_id":123,"reaction":{"type":"emoji","emoji":"👍"}}
{"connection_id":"personal","message_id":123,"reaction":{"type":"custom_emoji","custom_emoji_id":"1234567890"}}
{"connection_id":"personal","message_id":123,"reaction":null}
```

`null` clears the bot's reaction. Bots can currently set one reaction per message;
paid reactions are rejected locally. Telegram validates available emoji, custom
emoji permissions, and whether the target message permits reactions. The tool
does not accept arbitrary bot or chat IDs. Its calling conversation must be
loaded in the authenticated backend, and its destination connection must belong
to that backend.

Success returns `message_id`, the selected `reaction` (or null), and `applied:true`.
An uncertain acknowledgement returns `delivery_unknown` and is never retried
automatically. Rate limiting and other API errors follow the normal send rules.

## Forum-topic routing

Reaction updates do not carry a forum-topic ID. Bridges keeps a routing-only,
in-memory map from `(bot, chat, message_id)` to the observed topic. It records
incoming messages in configured routes and successfully acknowledged outgoing
text, file, image, and admin-command responses. No message content is stored.

The map holds at most 8192 messages for 24 hours and is lost on restart. A
topic-specific connection receives a reaction only when its target mapping is
known and matches. The send-reaction tool returns `message_unavailable` for an
unknown, expired, evicted, or different-topic target. Unknown reactions are never
broadcast across topic connections. Whole-chat connections already identify the
destination and do not require this map.

Messages from before startup may therefore receive no reaction event in a
topic-specific connection, even if Telegram delivers the update. Message lookup
and historical backfill are outside this feature.
