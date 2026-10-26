"""Local models (Ollama, LM Studio, vLLM, ...) through an OpenAI-compatible /chat/completions API.

Plain chat: the agent's history is replayed each turn. No file or shell tools in this version.
"""

from __future__ import annotations

from typing import Any

import httpx

from agent_fleet.backends.base import Backend, BackendError, TurnOutcome, TurnRequest


class OpenAICompatBackend(Backend):
    type_name = "openai"
    label = "OpenAI-compatible local model server"

    def _headers(self) -> dict[str, str]:
        key = self.cfg.get("api_key")
        return {"Authorization": f"Bearer {key}"} if key else {}

    def _base(self) -> str:
        return str(self.cfg.get("base_url", "")).rstrip("/")

    async def _models(self, timeout: float = 3) -> list[str]:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(f"{self._base()}/models", headers=self._headers())
            r.raise_for_status()
            return [m.get("id") for m in r.json().get("data", []) if m.get("id")]

    async def probe(self) -> dict[str, Any]:
        try:
            models = await self._models()
        except Exception as e:
            return {"available": False, "version": "", "detail": f"not reachable at {self._base()} ({type(e).__name__})"}
        return {"available": bool(models), "version": "", "detail": f"{len(models)} model(s): {', '.join(models[:6])}"}

    async def run_turn(self, req: TurnRequest) -> TurnOutcome:
        a = req.agent
        model = a.get("model") or self.cfg.get("model")
        if not model:
            models = await self._models()
            if not models:
                raise BackendError(f"No models available at {self._base()}")
            model = models[0]
        msgs: list[dict[str, str]] = []
        if a.get("instructions"):
            msgs.append({"role": "system", "content": a["instructions"]})
        msgs += a.get("_history") or [{"role": "user", "content": req.text}]
        async with httpx.AsyncClient(timeout=req.timeout) as c:
            r = await c.post(f"{self._base()}/chat/completions", headers=self._headers(),
                             json={"model": model, "messages": msgs})
        if r.status_code >= 400:
            return TurnOutcome("failed", "", f"HTTP {r.status_code}: {r.text[:800]}")
        text = (r.json().get("choices") or [{}])[0].get("message", {}).get("content") or ""
        req.emit("assistant", text, {"model": model})
        return TurnOutcome("completed", text)
