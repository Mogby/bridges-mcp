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
                readOnlyHint=self.read_only, destructiveHint=False, openWorldHint=not self.read_only
            ),
        )


STRING = {"type": "string"}
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
        "Send plain text to a configured Telegram conversation.",
        {**CONNECTION, "text": STRING},
        ("text",),
    ),
    ToolSpec(
        "telegram_send_images",
        "Send local images in order; caption on first. Partial errors include completed IDs and failed index.",
        {**CONNECTION, "paths": STRINGS, "caption": STRING},
        ("paths",),
    ),
    ToolSpec(
        "telegram_send_file",
        "Send one readable local file.",
        {**CONNECTION, "path": STRING, "caption": STRING},
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
    ToolSpec(
        "telegram_set_typing",
        "Maintain typing for the calling turn, or stop refreshes; visible cancellation may take five seconds.",
        {**CONNECTION, "enabled": {"type": "boolean"}},
        ("enabled",),
    ),
]


class Registry:
    def __init__(self, config):
        self.config = config
        self.specs = [
            ToolSpec(
                "get_protocol_docs",
                "Read enabled bridge protocols, tools, and current connection focus.",
                {},
            )
        ]
        if config.telegram.connections:
            self.specs.append(
                ToolSpec(
                    "focus",
                    "Route incoming messages for one connection to the calling session. Replaces that connection's previous focus; other connections are unchanged.",
                    {"connection_id": {"type": "string", "minLength": 1}},
                    ("connection_id",),
                )
            )
            self.specs += TELEGRAM_TOOLS
        self.by_name = {spec.name: spec for spec in self.specs}

    def docs(self, thread_id, focuses=None):
        docs = {
            "instructions": INSTRUCTIONS,
            "session": {"thread_id": thread_id},
            "focus": focuses or {},
            "envelope": {
                "format": "YAML bridge_context header inside a triple-backtick yaml code fence, then a blank line and original body text. No --- separator.",
                "required": ["bridge", "connection_id", "received_at"],
                "received_at": "UTC receipt time, seconds, Z suffix",
                "attachments": "Optional list of {type: image|audio|file, id: opaque handle}.",
                "body": "Everything after the closing metadata code fence and separating blank line is body text, including any further headers or code fences.",
            },
            "tools": list(self.by_name),
        }
        if self.config.telegram.connections:
            docs["telegram"] = {
                "connections": [
                    {"connection_id": key, **({"label": c.label} if c.label else {})}
                    for key, c in self.config.telegram.connections.items()
                ],
                "routing": "Everyone in configured conversations may instruct the agent. Choose destinations from user instructions. No automatic forwarding or last-active routing. connection_id may be omitted only with one connection.",
                "input": "Only text, photos, voice/audio and documents. No edits, reactions or service events. Documents remain file handles even if their MIME type is an image.",
                "attachments": f"Handles expire after {self.config.attachment_ttl_seconds} seconds, eviction, or restart. Download paths are local to the bridge host. Audio is not transcribed.",
                "typing": "Only the focused session may change typing for a connection. Enable once to refresh about every four seconds for the verified calling turn. Disable, completion, failure, interruption, loss of lifecycle, or shutdown stops refreshes. Outbound messages do not stop refreshes. Telegram may display typing for up to five more seconds.",
                "errors": "Tool failures use isError and a machine-readable code. Never blindly retry delivery_unknown or a partially sent batch. Inspect completed_message_ids and failed_index.",
            }
        return docs
