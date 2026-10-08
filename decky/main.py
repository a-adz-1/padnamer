# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the padnamer authors
"""padnamer Decky backend: relays the Quick Access panel to the padnamer service on this machine."""
import asyncio
import json
import urllib.error
import urllib.request

import decky

BASE = "http://127.0.0.1:8742"


def _call(path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, headers={
        "Content-Type": "application/json", "X-Padnamer": "1"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read() or b"{}").get("error")
        except ValueError:
            msg = None
        return {"error": msg or f"padnamer said {e.code}"}
    except OSError:
        return {"error": "padnamer isn't running"}


async def _run(path, body=None):
    return await asyncio.to_thread(_call, path, body)


class Plugin:
    async def get_state(self):
        return await _run("/api/state")

    async def move(self, cid: str, delta: int, always: bool):
        if not always:
            return await _run("/api/move", {"id": cid, "delta": delta})
        state = await _run("/api/state")
        if "error" in state:
            return state
        order = list(state.get("players", []))
        if cid not in order:
            return {"error": "that controller isn't connected"}
        i = order.index(cid)
        j = max(0, min(len(order) - 1, i + delta))
        order.insert(j, order.pop(i))
        rest = [c["id"] for c in state.get("controllers", []) if c["id"] not in order]
        return await _run("/api/order", {"ids": order + rest, "which": "default"})

    async def identify(self, cid: str):
        return await _run("/api/identify", {"id": cid})

    async def reset(self):
        return await _run("/api/order/reset", {})

    async def disconnect(self, cid: str):
        return await _run("/api/disconnect", {"id": cid})

    async def disconnect_all(self):
        return await _run("/api/disconnect_all", {})

    async def _main(self):
        decky.logger.info("padnamer panel loaded")

    async def _unload(self):
        pass

    async def _uninstall(self):
        pass
