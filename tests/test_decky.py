"""The Decky panel's Python backend."""
import asyncio
import os
import sys
import types
import unittest
from unittest import mock

sys.modules.setdefault("decky", types.SimpleNamespace(logger=mock.Mock()))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "decky"))
import main as deck  # noqa: E402


class DeckyBackendTests(unittest.TestCase):
    def run_(self, coro):
        return asyncio.run(coro)

    def test_move_always_builds_full_default_order(self):
        calls = []
        state = {"players": ["a", "b", "c"], "controllers": [{"id": x} for x in "abcd"]}

        def fake(path, body=None):
            calls.append((path, body))
            return state if path == "/api/state" else {"ok": True}
        with mock.patch.object(deck, "_call", fake):
            self.run_(deck.Plugin().move("c", -1, True))
        self.assertEqual(calls[-1], ("/api/order", {"ids": ["a", "c", "b", "d"], "which": "default"}))

    def test_move_now_uses_move_endpoint(self):
        with mock.patch.object(deck, "_call", lambda path, body=None: {"path": path, "body": body}):
            r = self.run_(deck.Plugin().move("c", 1, False))
        self.assertEqual(r, {"path": "/api/move", "body": {"id": "c", "delta": 1}})

    def test_service_down(self):
        with mock.patch.object(deck.urllib.request, "urlopen", side_effect=OSError("refused")):
            self.assertEqual(deck._call("/api/state"), {"error": "padnamer isn't running"})


if __name__ == "__main__":
    unittest.main()
