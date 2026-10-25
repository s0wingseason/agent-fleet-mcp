"""Newline-delimited JSON-RPC over a child process's stdio (used for `codex app-server`)."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
from typing import Any, Awaitable, Callable

from agent_fleet.backends.procs import NO_WINDOW

log = logging.getLogger("agent_fleet.jsonrpc")

Notify = Callable[[str, dict[str, Any]], None]
Request = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any] | None]]


class RpcError(Exception):
    def __init__(self, message: str, code: int | None = None, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


class JsonRpcProcess:
    def __init__(self, argv: list[str], *, cwd: str, env: dict[str, str],
                 on_notification: Notify, on_request: Request, stderr_path: str | None = None):
        self.argv = argv
        self.cwd = cwd
        self.env = env
        self.on_notification = on_notification
        self.on_request = on_request
        self.stderr_path = stderr_path
        self.proc: asyncio.subprocess.Process | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._tasks: list[asyncio.Task] = []
        self._write_lock = asyncio.Lock()

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv, cwd=self.cwd, env=self.env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=64 * 1024 * 1024, creationflags=NO_WINDOW)
        self._tasks.append(asyncio.create_task(self._read_stdout()))
        self._tasks.append(asyncio.create_task(self._drain_stderr()))

    async def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        sink = open(self.stderr_path, "ab") if self.stderr_path else None
        try:
            while True:
                chunk = await self.proc.stderr.read(65536)
                if not chunk:
                    break
                if sink:
                    sink.write(chunk)
                    sink.flush()
        finally:
            if sink:
                sink.close()

    async def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                await self._dispatch(msg)
        finally:
            err = RpcError("process exited")
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(err)
            self._pending.clear()

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        if "id" in msg and ("result" in msg or "error" in msg) and "method" not in msg:
            fut = self._pending.pop(msg["id"], None)
            if fut and not fut.done():
                if "error" in msg and msg["error"] is not None:
                    e = msg["error"]
                    fut.set_exception(RpcError(e.get("message", "error"), e.get("code"), e.get("data")))
                else:
                    fut.set_result(msg.get("result"))
            return
        method = msg.get("method")
        if not method:
            return
        if "id" in msg:  # server -> client request
            try:
                result = await self.on_request(method, msg.get("params") or {})
                if result is None:
                    await self._write({"id": msg["id"], "error": {"code": -32601, "message": "unsupported"}})
                else:
                    await self._write({"id": msg["id"], "result": result})
            except Exception as e:  # never let a handler kill the reader
                log.exception("request handler failed")
                await self._write({"id": msg["id"], "error": {"code": -32603, "message": str(e)}})
            return
        try:
            self.on_notification(method, msg.get("params") or {})
        except Exception:
            log.exception("notification handler failed")

    async def _write(self, obj: dict[str, Any]) -> None:
        if not self.alive:
            raise RpcError("process is not running")
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            assert self.proc and self.proc.stdin
            self.proc.stdin.write(data)
            await self.proc.stdin.drain()

    async def request(self, method: str, params: dict[str, Any] | None = None,
                      timeout: float = 120) -> Any:
        rid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self._write({"method": method, "id": rid, "params": params or {}})
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        msg: dict[str, Any] = {"method": method}
        if params is not None:
            msg["params"] = params
        await self._write(msg)

    async def close(self) -> None:
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), 5)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        _ = os  # keep import for type checkers on some platforms
