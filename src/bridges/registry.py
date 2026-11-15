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
TELEGRAM_TOOLS = [
    ToolSpec(
        "telegram_send_text",
        "Send Telegram MarkdownV2 text to a configured conversation. Escape literal punctuation; see the text parameter for formatting rules.",
        {**CONNECTION, "text": MARKDOWN_STRING},
        ("text",),
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
        "Schedule a recurring prompt into a connection's agent session. The scheduler never posts the prompt to the external chat. Uses the configured cron timezone.",
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
        },
        ("connection_id", "name", "pattern", "prompt"),
    ),
    ToolSpec(
        "list_jobs",
        "List id, name, pattern, and active/paused status without prompts.",
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
                "message_id": "Telegram incoming message ID, scoped to its chat/connection. Present on every Telegram envelope; send tools return outgoing message IDs too.",
                "reply_to_message_id": "Optional original message ID for a reply in the same chat. Match against earlier incoming context or outgoing send results. The original content is not fetched or embedded; external-chat replies are not represented by this field.",
                "attachments": "Optional list of {type: image|audio|file, id: opaque handle}.",
                "body": "Everything after the closing metadata code fence and separating blank line is body text, including any further headers or code fences.",
            },
            "tools": list(self.by_name),
        }
        if self.config.telegram.connections:
            docs["cron"] = {
                "timezone": self.config.cron.timezone,
                "tools": [spec.name for spec in CRON_TOOLS],
                "routing": "Jobs bind to connection_id and inject prompts into its mapped session. The scheduler never posts prompts or typing indicators to the external chat.",
                "patterns": "Five-field Unix cron; Sunday 0/7; restricted month-day and weekday use OR.",
                "jobs": "list_jobs returns id/name/pattern/status only. get_job_prompt, delete_job, pause_job and resume_job accept only id. Status is active or paused.",
                "persistence": "Jobs and pause state survive restarts in state_dir/cron.sqlite. Missed downtime/paused occurrences are skipped. Delivery is attempted once per claimed occurrence; it is not exactly-once agent execution.",
                "metadata": "Cron envelopes use bridge=cron, an occurrence message_id, job_id, job_name, pattern, scheduled_at, and received_at. Original prompt follows the header unchanged.",
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
                "input": "Only text, photos, voice/audio and documents. No edits, reactions or service events. Documents remain file handles even if their MIME type is an image.",
                "formatting": MARKDOWN_V2,
                "attachments": f"Handles expire after {self.config.attachment_ttl_seconds} seconds, eviction, or restart. Download paths are local to the bridge host. Audio is not transcribed.",
                "typing": "Bridges automatically starts typing when an incoming message is accepted into the mapped session and refreshes about every four seconds for that turn. Completion, failure, interruption, replacement, session replacement, loss of lifecycle, or shutdown stops refreshes. Agents do not manage typing. Outbound messages do not stop refreshes. Telegram may display typing for up to five more seconds.",
                "errors": "Tool failures use isError and a machine-readable code. Never blindly retry delivery_unknown or a partially sent batch. Inspect completed_message_ids and failed_index.",
            }
        return docs
