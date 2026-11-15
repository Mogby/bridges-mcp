import jsonschema
from mcp.server.lowlevel import Server

from .registry import INSTRUCTIONS
from .runtime import result


def create_server(runtime):
    server = Server("bridges", version="0.4.1", instructions=INSTRUCTIONS)

    @server.list_tools()
    async def list_tools():
        return [spec.tool() for spec in runtime.registry.specs]

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        spec = runtime.registry.by_name.get(name)
        if spec:
            try:
                jsonschema.validate(arguments, spec.tool().inputSchema)
            except jsonschema.ValidationError:
                return result(
                    {
                        "code": "invalid_payload",
                        "message": "Arguments do not match the tool schema.",
                    },
                    error=True,
                )
        meta = server.request_context.meta
        metadata = meta.model_dump(by_alias=True) if meta else {}
        request = server.request_context.request
        client_id = request.scope.get("bridges_client") if request is not None else None
        return await runtime.call(name, arguments, metadata, client_id)

    return server
