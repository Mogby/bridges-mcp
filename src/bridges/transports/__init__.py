"""Adapter registry. Harness-specific protocol handling stays in adapters."""

from ..errors import BridgeError
from .app_server import AppServer

ADAPTERS = {"codex-app-server": AppServer}


def build_backend(config):
    factory = ADAPTERS.get(config.adapter)
    if factory is None:
        raise BridgeError("unsupported_adapter", "The configured harness adapter is unavailable.")
    return factory(config)
