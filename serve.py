"""
Frank API — agentic OpenAI-compatible endpoint for Open WebUI.

Every request runs the full Frank agentic loop (tools included).
Tool calls happen internally; status indicators stream to the client.
Final answer streams as text. Looks like claude.ai from the outside.
"""

import asyncio
import json
import logging
import sys
import time
import uuid
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

FRANK_ROOT = Path(__file__).parent
sys.path.insert(0, str(FRANK_ROOT))

import config as cfg
import context
import loop as loop_mod
import tools as tools_mod
from provider import make_provider

log = logging.getLogger("frank.serve")

_frank_config: dict = {}
_persona_dir: Path = FRANK_ROOT / "personas" / "interactive"
_memory_dir: Path = FRANK_ROOT / "memory"

TOOL_LABELS = {
    "web_fetch":      "fetching URL",
    "web_search":     "searching the web",
    "shell":          "running command",
    "file_read":      "reading file",
    "wiki_search":    "searching wiki",
}


def _assemble_system() -> str:
    persona_cfg = cfg.load_persona(_persona_dir)
    return context.assemble(
        _persona_dir, _memory_dir,
        operator_cfg=_frank_config.get("operator_memory"),
        context_level=persona_cfg.get("context_level", "none"),
    )


def _extract_text(content) -> str:
    """Handle string or list content from OpenAI message format."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            p.get("text", "") for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return str(content or "")


def _sse_chunk(chunk_id: str, text: str) -> str:
    data = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "frank",
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    }
    return f"data: {json.dumps(data)}\n\n"


def _sse_stop(chunk_id: str) -> str:
    data = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "frank",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    return f"data: {json.dumps(data)}\n\ndata: [DONE]\n\n"


async def handle_chat_completions(request: Request) -> StreamingResponse | JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    messages = body.get("messages", [])
    is_stream = body.get("stream", True)

    # Split messages into history + current prompt.
    # Skip any system messages (we inject our own).
    prompt = ""
    history: list[dict] = []
    for i, msg in enumerate(messages):
        role = msg.get("role", "")
        if role == "system":
            continue
        content = _extract_text(msg.get("content", ""))
        if i == len(messages) - 1 and role == "user":
            prompt = content
        else:
            history.append({"role": role, "content": content})

    if not prompt:
        return JSONResponse({"error": "no user message found"}, status_code=400)

    # Load persona, build provider and tools
    persona_cfg = cfg.load_persona(_persona_dir)
    system = _assemble_system()
    provider = make_provider(_frank_config, persona_cfg)
    tool_modules = tools_mod.get_modules(persona_cfg.get("allowed_tools", []))
    ctx = {
        "persona": "interactive",
        "vault_get": cfg.vault_get,
        "frank_config": _frank_config,
        "persona_config": persona_cfg,
        "non_interactive": True,
        "frank_root": str(FRANK_ROOT),
    }

    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:16]}"

    # Non-streaming path
    if not is_stream:
        try:
            final_text, _ = await loop_mod.run(
                prompt=prompt,
                provider=provider,
                tool_modules=tool_modules,
                system=system,
                history=history,
                allowed_tools=persona_cfg.get("allowed_tools"),
                ctx=ctx,
            )
        except Exception as e:
            log.exception(f"Loop error (non-stream): {e}")
            final_text = f"Error: {e}"
        return JSONResponse({
            "id": chunk_id,
            "object": "chat.completion",
            "model": "frank",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": final_text},
                "finish_reason": "stop",
            }],
        })

    # Streaming path: loop runs as a Task, SSE gen consumes from queue
    queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

    async def run_loop():
        def on_tool_call(name: str, args: dict):
            label = TOOL_LABELS.get(name, name)
            queue.put_nowait(("tool", label))

        try:
            final_text, _ = await loop_mod.run(
                prompt=prompt,
                provider=provider,
                tool_modules=tool_modules,
                system=system,
                history=history,
                allowed_tools=persona_cfg.get("allowed_tools"),
                on_tool_call=on_tool_call,
                ctx=ctx,
            )
            await queue.put(("done", final_text))
        except loop_mod.MaxTurnsError as e:
            await queue.put(("error", f"Reached max turns: {e}"))
        except Exception as e:
            log.exception(f"Loop error (stream): {e}")
            await queue.put(("error", str(e)))

    asyncio.create_task(run_loop())

    async def sse_gen():
        tools_used: list[str] = []

        while True:
            try:
                event_type, data = await asyncio.wait_for(queue.get(), timeout=300.0)
            except asyncio.TimeoutError:
                yield _sse_chunk(chunk_id, "\n[timed out]")
                yield _sse_stop(chunk_id)
                return

            if event_type == "tool":
                tools_used.append(data)
                yield _sse_chunk(chunk_id, f"*{data}...*\n")

            elif event_type == "done":
                if tools_used:
                    yield _sse_chunk(chunk_id, "\n")
                # Stream final response in small chunks
                text = data
                chunk_size = 8
                for i in range(0, len(text), chunk_size):
                    yield _sse_chunk(chunk_id, text[i:i + chunk_size])
                    await asyncio.sleep(0)
                yield _sse_stop(chunk_id)
                return

            elif event_type == "error":
                yield _sse_chunk(chunk_id, f"\nError: {data}")
                yield _sse_stop(chunk_id)
                return

    return StreamingResponse(
        sse_gen(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


async def handle_models(request: Request) -> JSONResponse:
    return JSONResponse({
        "object": "list",
        "data": [{
            "id": "frank",
            "object": "model",
            "created": 1700000000,
            "owned_by": "localfamous",
            "description": "Frank — local AI agent harness",
        }],
    })


app = Starlette(routes=[
    Route("/v1/chat/completions", handle_chat_completions, methods=["POST"]),
    Route("/v1/models", handle_models, methods=["GET"]),
])


def run(host: str = "127.0.0.1", port: int = 8890) -> None:
    global _frank_config
    _frank_config = cfg.load_frank(FRANK_ROOT / "config")

    import uvicorn
    log.info(f"Frank API starting on {host}:{port} — agentic mode")
    log.info(f"Persona: {_persona_dir.name} | Tools: {cfg.load_persona(_persona_dir).get('allowed_tools', [])}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
