import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mcp_probe import Probe, identify_thread


class ProbeTests(unittest.TestCase):
    def test_metadata_formats(self):
        for metadata in [{"threadId": "target"},
                         {"x-codex-turn-metadata": '{"thread_id":"target"}'},
                         {"x-codex-turn-metadata": {"thread_id": "target"}},
                         {"thread": {"id": "target"}}]:
            self.assertEqual(identify_thread(metadata)[0], "target")
        self.assertEqual(identify_thread({"x-codex-turn-metadata": "invalid"}), (None, None))

    def test_no_identity_never_sends(self):
        with tempfile.TemporaryDirectory() as directory:
            probe = Probe(Path("/unused.sock"), Path(directory))
            with patch("mcp_probe.DesktopIPC") as ipc:
                result = probe.handle("tools/call", {"name": "steer_test"})
                self.assertTrue(result["isError"])
                ipc.assert_not_called()
                self.assertEqual(probe.jobs, [])

    def test_delayed_send_uses_call_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            probe = Probe(Path("/unused.sock"), Path(directory))
            with patch("mcp_probe.DesktopIPC") as ipc:
                ipc.return_value.owner.return_value = "owner"
                ipc.return_value.steer.return_value = {"result": {"result": {"turnId": "active"}}}
                result = probe.handle("tools/call", {
                    "name": "steer_test", "arguments": {"delay_seconds": 0},
                    "_meta": {"threadId": "target"}})
                probe.jobs[-1].join(timeout=2)
                args = ipc.return_value.steer.call_args.args
                self.assertEqual(args[:2], ("target", "owner"))
                self.assertIn("bridge_context:", args[2])
                self.assertFalse(json.loads(result["content"][0]["text"])["delivered"])

    def test_steer_failure_does_not_retry_or_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            probe = Probe(Path("/unused.sock"), Path(directory))
            with patch("mcp_probe.DesktopIPC") as ipc:
                ipc.return_value.owner.return_value = "owner"
                ipc.return_value.steer.side_effect = TimeoutError("outcome unknown")
                probe.deliver("target", "marker", 0)
                ipc.return_value.steer.assert_called_once()
                ipc.return_value.close.assert_called_once()
            entry = json.loads(probe.log_path.read_text().splitlines()[-1])
            self.assertEqual(entry["event"], "steer_error")
            self.assertFalse(entry["retried"])
            self.assertFalse(entry["queued_fallback"])

    def test_stdio_handshake_and_context(self):
        with tempfile.TemporaryDirectory() as directory:
            messages = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2025-11-25", "clientInfo": {"name": "test", "version": "1"},
                    "capabilities": {}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                    "name": "inspect_context", "_meta": {"threadId": "synthetic-test-only"}}}
            ]
            result = subprocess.run([sys.executable, str(Path(__file__).with_name("mcp_probe.py")),
                                     "--codex", "/unused", "--log-dir", directory],
                                    input="".join(json.dumps(m) + "\n" for m in messages),
                                    text=True, capture_output=True, timeout=5, check=True)
            replies = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual([r["id"] for r in replies], [1, 2, 3])
            self.assertEqual(len(replies[1]["result"]["tools"]), 2)
            self.assertEqual(json.loads(replies[2]["result"]["content"][0]["text"])["thread_id"],
                             "synthetic-test-only")


if __name__ == "__main__":
    unittest.main()
