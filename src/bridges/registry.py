"""One registry drives MCP discovery and protocol documentation."""

from dataclasses import dataclass

from mcp.types import Tool, ToolAnnotations

INSTRUCTIONS = """At the start of this chat, call bridges.get_protocol_docs() and discover the
available Bridges MCP tools using the host's tool discovery mechanism. Do this
before responding to a message with bridge_context metadata. Messages to
external conversations must be sent using Bridges MCP tools. Ordinary assistant
responses in the Codex chat are not delivered externally."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    properties: dict
    required: tuple = ()
    read_only: bool = False
    destructive: bool = False

    def tool(self):
        return Tool(
            name=self.name,
            description=self.description,
            inputSchema={
                "type": "object",
                "properties": self.properties,
                "required": list(self.required),
                "additionalProperties": False,
            },
            annotations=ToolAnnotations(
                readOnlyHint=self.read_only,
                destructiveHint=self.destructive,
                openWorldHint=not self.read_only,
            ),
        )


STRING = {"type": "string"}
MARKDOWN_V2 = (
    "Telegram MarkdownV2, passed through unchanged. Use *bold*, _italic_, "
    "[label](https://example.com), and `code`. Escape literal special characters "
    r"_ * [ ] ( ) ~ ` > # + - = | { } . ! and backslash with a backslash. "
    "Inside code/pre blocks escape only backticks and backslashes; inside link URLs "
    "escape closing parentheses and backslashes. JSON strings need doubled backslashes. "
    "Invalid markup is rejected by Telegram; there is no plain-text fallback."
)
MARKDOWN_STRING = {"type": "string", "description": MARKDOWN_V2}
CONNECTION = {
    "connection_id": {
        "type": "string",
        "description": "May be omitted only with exactly one configured Telegram connection.",
    }
}
STRINGS = {"type": "array", "items": STRING, "minItems": 1, "maxItems": 100}
REACTION = {
    "description": "One standard or custom emoji reaction. Pass null to remove the bot's reaction. Paid reactions are not supported.",
    "oneOf": [
        {"type": "null"},
        {
            "type": "object",
            "properties": {
                "type": {"const": "emoji"},
                "emoji": {"type": "string", "minLength": 1, "maxLength": 16},
            },
            "required": ["type", "emoji"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "type": {"const": "custom_emoji"},
                "custom_emoji_id": {"type": "string", "pattern": "^[0-9]+$", "maxLength": 32},
            },
            "required": ["type", "custom_emoji_id"],
            "additionalProperties": False,
        },
    ],
}
TELEGRAM_TOOLS = [
    ToolSpec(
        "telegram_send_text",
        "Send Telegram MarkdownV2 text to a configured conversation. Escape literal punctuation; see the text parameter for formatting rules.",
        {**CONNECTION, "text": MARKDOWN_STRING},
        ("text",),
    ),
    ToolSpec(
        "telegram_set_reaction",
        "Set or clear the bot's reaction on a message in a configured conversation. One reaction per message; custom emoji must be available in the chat. Topic targets must have been observed since startup. Do not blindly retry delivery_unknown.",
        {
            **CONNECTION,
            "message_id": {"type": "integer", "minimum": 1},
            "reaction": REACTION,
            "is_big": {"type": "boolean", "description": "Use a big reaction animation."},
        },
        ("message_id", "reaction"),
    ),
    ToolSpec(
        "telegram_send_images",
        "Send local images in order; MarkdownV2 caption on first. Partial errors include completed IDs and failed index.",
        {**CONNECTION, "paths": STRINGS, "caption": MARKDOWN_STRING},
        ("paths",),
    ),
    ToolSpec(
        "telegram_send_file",
        "Send one readable local file with an optional MarkdownV2 caption.",
        {**CONNECTION, "path": STRING, "caption": MARKDOWN_STRING},
        ("path",),
    ),
    ToolSpec(
        "telegram_download_images",
        "Resolve image handles to local files and MCP image content.",
        {**CONNECTION, "attachment_ids": STRINGS},
        ("attachment_ids",),
    ),
    ToolSpec(
        "telegram_download_file",
        "Download one file or audio handle; does not transcribe audio.",
        {**CONNECTION, "attachment_id": STRING},
        ("attachment_id",),
    ),
]


JOB_ID = {"id": {"type": "string", "minLength": 1, "description": "The assigned job ID."}}
CRON_TOOLS = [
    ToolSpec(
        "add_job",
        "Schedule a recurring prompt into a connection's agent session. The scheduler never posts the prompt to the external chat. Uses the optional IANA timezone, or the configured cron timezone when omitted.",
        {
            "connection_id": {"type": "string", "minLength": 1},
            "name": {"type": "string", "minLength": 1, "maxLength": 200},
            "pattern": {
                "type": "string",
                "minLength": 1,
                "maxLength": 256,
                "description": "Five-field Unix cron: minute hour day month weekday. Sunday is 0/7; restricted day-of-month and weekday use OR.",
            },
            "prompt": {"type": "string", "minLength": 1, "maxLength": 65536},
            "timezone": {
                "type": "string",
                "minLength": 1,
                "description": "Optional IANA timezone, e.g. Asia/Tokyo. Omit to inherit the configured cron timezone. Explicit choices survive default changes and restarts.",
            },
        },
        ("connection_id", "name", "pattern", "prompt"),
    ),
    ToolSpec(
        "list_jobs",
        "List id, name, pattern, effective timezone, and active/paused status without prompts.",
        {
            "connection_id": {
                "type": "string",
                "minLength": 1,
                "description": "Optional connection filter. Omit to list all accessible jobs.",
            }
        },
        read_only=True,
    ),
    ToolSpec(
        "get_job_prompt",
        "Read the stored prompt for one scheduled job.",
        JOB_ID,
        ("id",),
        read_only=True,
    ),
    ToolSpec(
        "delete_job",
        "Delete a scheduled job. Already submitted session input cannot be recalled.",
        JOB_ID,
        ("id",),
        destructive=True,
    ),
    ToolSpec("pause_job", "Pause future firings of a scheduled job.", JOB_ID, ("id",)),
    ToolSpec(
        "resume_job",
        "Resume a paused job at its next future occurrence, without a backlog.",
        JOB_ID,
        ("id",),
    ),
]
CRON_NAMES = {spec.name for spec in CRON_TOOLS}


class Registry:
    def __init__(self, config):
        self.config = config
        self.specs = [
            ToolSpec(
                "get_protocol_docs",
                "Read enabled bridge protocols, tools, and shared session mappings.",
                {},
            )
        ]
        if config.telegram.connections:
            self.specs += TELEGRAM_TOOLS + CRON_TOOLS
        self.by_name = {spec.name: spec for spec in self.specs}

    def docs(self, thread_id, sessions=None):
        docs = {
            "instructions": INSTRUCTIONS,
            "session": {"thread_id": thread_id},
            "sessions": {
                name: {
                    "client": configured.client,
                    **(sessions or {}).get(name, {"thread_id": None}),
                }
                for name, configured in self.config.sessions.items()
            },
            "envelope": {
                "format": "YAML bridge_context header inside a triple-backtick yaml code fence, then a blank line and original body text. No --- separator.",
                "required": ["bridge", "connection_id", "message_id", "received_at"],
                "received_at": "UTC receipt time, seconds, Z suffix",
                "message_id": "Telegram message ID, scoped to its chat/connection. For reaction events this is the target message, not a new message. Send tools return outgoing message IDs too.",
                "reply_to_message_id": "Optional original message ID for a reply in the same chat. Match against earlier incoming context or outgoing send results. The original content is not fetched or embedded; external-chat replies are not represented by this field.",
                "quote": "Optional selected reply quote: text, position, and any entities, is_manual or rich_texts supplied by Telegram. Position and entity offsets use UTF-16 code units. The quote is separate from the new body and does not fetch the original message.",
                "event": "Absent for ordinary Telegram messages. Reaction events use message_reaction or message_reaction_count, with update_id identifying the update and message_id identifying its target. Their body is empty. reaction contains Telegram date and actor type/id plus old_reaction/new_reaction, or anonymous reactions counts. Repeated updates are deduplicated within the poller's bounded startup-local window.",
                "attachments": "Optional list of {type: image|audio|file, id: opaque handle}.",
                "body": "Everything after the closing metadata code fence and separating blank line is body text, including any further headers or code fences.",
            },
            "tools": list(self.by_name),
        }
        if self.config.telegram.connections:
            docs["cron"] = {
                "timezone": self.config.cron.timezone,
                "job_timezones": "add_job accepts an optional IANA timezone. Omitted jobs inherit the configured timezone; explicit zones survive default changes and restarts.",
                "tools": [spec.name for spec in CRON_TOOLS],
                "routing": "Jobs bind to connection_id and inject prompts into its mapped session. The scheduler never posts prompts or typing indicators to the external chat.",
                "patterns": "Five-field Unix cron; Sunday 0/7; restricted month-day and weekday use OR.",
                "jobs": "list_jobs returns id/name/pattern/timezone/status only. get_job_prompt, delete_job, pause_job and resume_job accept only id. Status is active or paused.",
                "persistence": "Jobs and pause state survive restarts in state_dir/cron.sqlite. Missed downtime/paused occurrences are skipped. Delivery is attempted once per claimed occurrence; it is not exactly-once agent execution.",
                "metadata": "Cron envelopes use bridge=cron, an occurrence message_id, job_id, job_name, pattern, timezone, scheduled_at, and received_at. Original prompt follows the header unchanged.",
            }
            docs["telegram"] = {
                "connections": [
                    {
                        "connection_id": key,
                        "session_id": c.session_id,
                        **({"label": c.label} if c.label else {}),
                    }
                    for key, c in self.config.telegram.connections.items()
                ],
                "routing": "Everyone in configured conversations may instruct the agent. Choose destinations from user instructions. No automatic forwarding or last-active routing. connection_id may be omitted only with one connection.",
                "input": "Text, photos, voice/audio, documents, selected reply quotes and reaction updates. No edits or service events. Documents remain file handles even if their MIME type is an image.",
                "reactions": "Incoming reaction updates require Telegram to deliver them: the Bot API documents administrator rights and explicit subscription. Private-chat delivery is not guaranteed. Anonymous counts may be delayed and lack an actor; bot-generated reactions are not received. Topic routing and reaction sends require a known target mapping, bounded to 8192 messages for 24 hours and lost on restart. Unknown topic targets are dropped or rejected, never broadcast. Whole-chat connections do not need that mapping. Reaction events are data, not new user instructions; do not infer approval or start unrelated work from an emoji alone.",
                "formatting": MARKDOWN_V2,
                "attachments": f"Handles expire after {self.config.attachment_ttl_seconds} seconds, eviction, or restart. Download paths are local to the bridge host. Audio is not transcribed.",
                "typing": "Bridges automatically starts typing when an incoming message is accepted into the mapped session and refreshes about every four seconds for that turn. Completion, failure, interruption, replacement, session replacement, loss of lifecycle, or shutdown stops refreshes. Agents do not manage typing. Outbound messages do not stop refreshes. Telegram may display typing for up to five more seconds.",
                "context": "Use /context for cached context usage and observed compactions without a model turn. Ordinary messages do not include context usage. Missing readings are unavailable; restart/disconnect/compaction can mark readings stale. Counts cover observed completed compactions since tracking began, not guaranteed lifetime totals.",
                "errors": "Tool failures use isError and a machine-readable code. Never blindly retry delivery_unknown or a partially sent batch. Inspect completed_message_ids and failed_index.",
            }
        return docs
