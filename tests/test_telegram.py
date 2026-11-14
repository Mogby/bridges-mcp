import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image

from bridges.envelope import Attachment, Handles
from bridges.errors import BridgeError
from bridges.telegram import DOWNLOAD_LIMIT, TelegramAPI, TelegramBridge, validate_text


async def test_api_timeout_is_unknown_and_never_retried(caplog):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("secret URL " + str(request.url))

    api = TelegramAPI("123:secret", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(BridgeError) as exc:
        await api.call("sendMessage", {"text": "private"}, mutating=True)
    assert exc.value.code == "delivery_unknown"
    assert len(calls) == 1
    assert "secret" not in str(exc.value) + caplog.text
    await api.close()


async def test_rate_limit_is_explicit_non_delivery():
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 9}}
            )
        )
    )
    api = TelegramAPI("123:secret", client)
    with pytest.raises(BridgeError) as exc:
        await api.call("sendMessage", mutating=True)
    assert exc.value.result()["retry_after"] == 9
    assert exc.value.code == "rate_limited"
    await api.close()


async def test_outbound_markdown_preserved_for_text_and_captions(config, tmp_path):
    path = tmp_path / "photo.png"
    Image.new("RGB", (8, 8)).save(path)
    api = AsyncMock()
    api.call.return_value = {"message_id": 10}
    bridge = TelegramBridge(config, {}, Handles(), lambda _: None, {"main": api})
    connection = config.telegram.connections["two"]
    markdown = r"*Bold* _italic_ [link](https://example.com) `code` \!"
    assert await bridge.send_text(connection, markdown) == {"message_id": 10}
    assert await bridge.send_file(connection, str(path), markdown) == {"message_id": 10}
    assert await bridge.send_images(connection, [str(path)] * 2, markdown) == {
        "message_ids": [10, 10]
    }
    calls = api.call.call_args_list
    assert [call.args[0] for call in calls] == [
        "sendMessage",
        "sendDocument",
        "sendPhoto",
        "sendPhoto",
    ]
    for call in calls:
        assert call.args[1]["parse_mode"] == "MarkdownV2"
        assert call.args[1]["chat_id"] == "2" and call.args[1]["message_thread_id"] == 42
        assert call.kwargs["mutating"] is True
    assert calls[0].args[1]["text"] == markdown
    assert calls[1].args[1]["caption"] == calls[2].args[1]["caption"] == markdown
    assert calls[3].args[1]["caption"] == ""


async def test_image_batch_partial_result(config, tmp_path):
    path = tmp_path / "photo.png"
    Image.new("RGB", (8, 8)).save(path)
    api = AsyncMock()
    api.call.side_effect = [{"message_id": 10}, BridgeError("delivery_unknown", "timeout")]
    bridge = TelegramBridge(config, {}, Handles(), lambda _: None, {"main": api})
    with pytest.raises(BridgeError) as exc:
        await bridge.send_images(config.telegram.connections["one"], [str(path)] * 3, "caption")
    assert exc.value.details == {
        "completed_message_ids": [10],
        "failed_index": 1,
        "failed_delivery_unknown": True,
    }
    assert api.call.call_count == 2
    assert api.call.call_args_list[0].args[1]["caption"] == "caption"
    assert api.call.call_args_list[1].args[1]["caption"] == ""


async def test_batch_validates_all_files_before_sending(config, tmp_path):
    path = tmp_path / "photo.png"
    Image.new("RGB", (8, 8)).save(path)
    api = AsyncMock()
    bridge = TelegramBridge(config, {}, Handles(), lambda _: None, {"main": api})
    with pytest.raises(BridgeError):
        await bridge.send_images(
            config.telegram.connections["one"], [str(path), str(tmp_path / "missing")]
        )
    api.call.assert_not_called()


@pytest.mark.parametrize("text,limit", [("x" * 4097, 4096), ("x" * 1025, 1024), ("", 4096)])
def test_limits(text, limit):
    with pytest.raises(BridgeError):
        validate_text(text, limit, "text")


async def test_download_paths_metadata_and_no_secret(tmp_path):
    def handler(request):
        if request.url.path.endswith("getFile"):
            return httpx.Response(
                200,
                json={"ok": True, "result": {"file_path": "documents/file.txt", "file_size": 3}},
            )
        return httpx.Response(200, content=b"abc")

    api = TelegramAPI("123:secret", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    item = Attachment("one", "main", "file", "private-id", "../../evil.txt", "text/plain", 3)
    result = await api.download(item, tmp_path)
    from pathlib import Path

    path = Path(result["path"])
    assert path.parent == tmp_path and path.read_bytes() == b"abc"
    assert result["name"] == "../../evil.txt" and result["byte_size"] == 3
    assert "secret" not in str(result) and "private-id" not in str(result)
    item.size = DOWNLOAD_LIMIT + 1
    with pytest.raises(BridgeError) as exc:
        await api.download(item, tmp_path)
    assert exc.value.code == "file_too_large"
    await api.close()


async def test_poller_offsets_duplicates_and_unconfigured_chats(config):
    requests, envelopes = [], []
    api = AsyncMock()
    ready = asyncio.Event()

    async def call(method, data=None):
        if method == "getWebhookInfo":
            return {"url": ""}
        requests.append(data)
        if len(requests) == 1:
            message = {
                "update_id": 5,
                "message": {"message_id": 123, "chat": {"id": 1, "type": "private"}, "text": "a"},
            }
            return [
                message,
                message,
                {
                    "update_id": 6,
                    "message": {"chat": {"id": 999, "type": "private"}, "text": "ignored"},
                },
                {
                    "update_id": 7,
                    "message": {
                        "message_id": 456,
                        "chat": {"id": 2, "type": "supergroup"},
                        "message_thread_id": 42,
                        "text": "b",
                    },
                },
            ]
        ready.set()
        await asyncio.Event().wait()

    api.call.side_effect = call
    bridge = TelegramBridge(config, {}, Handles(), envelopes.append, {"main": api})
    await bridge.start()
    await asyncio.wait_for(ready.wait(), 1)
    assert len(bridge.tasks) == 1 and len(envelopes) == 2
    assert requests[1]["offset"] == 8 and requests[0]["allowed_updates"] == ["message"]
    await bridge.close()


async def test_webhook_conflict_never_deletes_or_polls(config):
    api = AsyncMock()
    api.call.return_value = {"url": "https://example.test/webhook"}
    hit = asyncio.Event()

    async def sleep(_):
        hit.set()
        await asyncio.Event().wait()

    bridge = TelegramBridge(config, {}, Handles(), lambda _: None, {"main": api}, sleep=sleep)
    await bridge.start()
    await asyncio.wait_for(hit.wait(), 1)
    assert [c.args[0] for c in api.call.call_args_list] == ["getWebhookInfo"]
    await bridge.close()
