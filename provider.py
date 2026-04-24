"""Provider abstraction — talks to model endpoints.

Three adapters:
  - anthropic: Anthropic API format (claude-*)
  - openai: OpenAI-compatible format (local llama.cpp, OpenRouter, etc.)
"""

import json
import os
import time
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("frank.provider")

# Errors worth retrying (transient). Everything else fails fast.
_RETRYABLE = ("timeout", "connection", "rate_limit", "overloaded", "503", "529", "502")


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ProviderResponse:
    text: str | None
    tool_calls: list[ToolCall]
    stop_reason: str
    input_tokens: int = 0
    output_tokens: int = 0


class AnthropicProvider:
    """Anthropic API (claude-* models). Handles tool_use natively."""

    def __init__(self, config: dict):
        import anthropic
        key = config.get("frontier_key") or os.environ.get("ANTHROPIC_API_KEY", "")
        self.client = anthropic.Anthropic(api_key=key)
        self.model = config.get("model", "claude-haiku-4-5-20251001")
        self.max_tokens = config.get("max_tokens", 4096)

    def complete(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str,
    ) -> ProviderResponse:
        # Convert normalized messages → Anthropic format
        anthropic_messages = _to_anthropic_messages(messages)

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": anthropic_messages,
        }
        if tools:
            kwargs["tools"] = _tools_to_anthropic(tools)

        response = _retry(lambda: self.client.messages.create(**kwargs))

        tool_calls = []
        text_parts = []
        for block in response.content:
            if block.type == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.id,
                    name=block.name,
                    arguments=block.input,
                ))
            elif block.type == "text":
                text_parts.append(block.text)

        return ProviderResponse(
            text="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            stop_reason=response.stop_reason or "end_turn",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

    def stream_complete(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str,
        on_chunk: Callable[[str], None],
    ) -> "ProviderResponse":
        """Stream text tokens via on_chunk callback. Returns full ProviderResponse when done."""
        anthropic_messages = _to_anthropic_messages(messages)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": anthropic_messages,
        }
        if tools:
            kwargs["tools"] = _tools_to_anthropic(tools)

        tool_calls = []
        with self.client.messages.stream(**kwargs) as stream:
            for text in stream.text_stream:
                on_chunk(text)
            final = stream.get_final_message()
            for block in final.content:
                if block.type == "tool_use":
                    tool_calls.append(ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=block.input,
                    ))

        full_text = stream.get_final_text() if not tool_calls else None
        return ProviderResponse(
            text=full_text,
            tool_calls=tool_calls,
            stop_reason=final.stop_reason or "end_turn",
            input_tokens=final.usage.input_tokens,
            output_tokens=final.usage.output_tokens,
        )


class OpenAICompatProvider:
    """OpenAI-compatible format (llama.cpp, OpenRouter, local models)."""

    def __init__(self, config: dict):
        import openai
        endpoint = config.get("endpoint", "http://localhost:8081/v1")
        key = (
            config.get("key")
            or os.environ.get("OPENROUTER_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or "sk-no-key"
        )
        extra_headers = {}
        if "openrouter.ai" in endpoint:
            extra_headers = {
                "HTTP-Referer": "https://your-domain.com",
                "X-Title": "localfamous",
            }
        self.client = openai.OpenAI(
            api_key=key,
            base_url=endpoint,
            default_headers=extra_headers,
        )
        self.model = config.get("model", "local")
        self.max_tokens = config.get("max_tokens", 4096)

    def complete(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str,
    ) -> ProviderResponse:
        # Prepend system as first message
        all_messages = [{"role": "system", "content": system}] + _to_openai_messages(messages)

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": all_messages,
        }
        if tools:
            kwargs["tools"] = _tools_to_openai(tools)
            kwargs["tool_choice"] = "auto"

        response = _retry(lambda: self.client.chat.completions.create(**kwargs))
        choice = response.choices[0]
        msg = choice.message

        tool_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {"raw": tc.function.arguments}
                tool_calls.append(ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=args,
                ))

        return ProviderResponse(
            text=msg.content,
            tool_calls=tool_calls,
            stop_reason=choice.finish_reason or "stop",
            input_tokens=response.usage.prompt_tokens if response.usage else 0,
            output_tokens=response.usage.completion_tokens if response.usage else 0,
        )

    def stream_complete(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str,
        on_chunk: Callable[[str], None],
    ) -> "ProviderResponse":
        """Stream text tokens via on_chunk callback. Returns full ProviderResponse when done."""
        all_messages = [{"role": "system", "content": system}] + _to_openai_messages(messages)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": all_messages,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = _tools_to_openai(tools)
            kwargs["tool_choice"] = "auto"

        full_text: list[str] = []
        tool_calls_raw: dict[int, dict] = {}

        for chunk in self.client.chat.completions.create(**kwargs):
            delta = chunk.choices[0].delta if chunk.choices else None
            if not delta:
                continue
            if delta.content:
                on_chunk(delta.content)
                full_text.append(delta.content)
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_calls_raw:
                        tool_calls_raw[idx] = {
                            "id": tc.id or "",
                            "name": tc.function.name or "" if tc.function else "",
                            "args": "",
                        }
                    if tc.function and tc.function.arguments:
                        tool_calls_raw[idx]["args"] += tc.function.arguments
                    if tc.id:
                        tool_calls_raw[idx]["id"] = tc.id
                    if tc.function and tc.function.name:
                        tool_calls_raw[idx]["name"] = tc.function.name

        tool_calls = []
        for _, tc in sorted(tool_calls_raw.items()):
            try:
                args = json.loads(tc["args"])
            except json.JSONDecodeError:
                args = {"raw": tc["args"]}
            tool_calls.append(ToolCall(id=tc["id"], name=tc["name"], arguments=args))

        return ProviderResponse(
            text="".join(full_text) or None,
            tool_calls=tool_calls,
            stop_reason="stop",
        )


def _retry(fn, max_attempts: int = 3, base_delay: float = 1.0):
    """
    Retry a callable on transient errors with exponential backoff + jitter.
    Fails fast on auth errors, bad requests, and unknown errors.
    """
    import random
    last_err = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as e:
            err_str = str(e).lower()
            is_retryable = any(k in err_str for k in _RETRYABLE)
            if not is_retryable:
                raise
            last_err = e
            delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
            log.warning(f"Transient error (attempt {attempt + 1}/{max_attempts}): {e} — retrying in {delay:.1f}s")
            time.sleep(delay)
    raise last_err


def make_provider(frank_config: dict, persona_config: dict) -> AnthropicProvider | OpenAICompatProvider:
    """Factory: build the right provider from config."""
    # Persona-level provider overrides frank defaults
    merged = {**frank_config.get("provider", {}), **persona_config.get("provider", {})}
    fmt = merged.get("format", "anthropic")

    if fmt == "anthropic":
        return AnthropicProvider(merged)
    else:
        return OpenAICompatProvider(merged)


# ── Message format converters ─────────────────────────────────────────────────

def _to_anthropic_messages(messages: list[dict]) -> list[dict]:
    """Normalized → Anthropic message format."""
    result = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg["role"]

        if role == "user":
            result.append({"role": "user", "content": msg["content"]})

        elif role == "assistant":
            content = []
            if msg.get("content"):
                content.append({"type": "text", "text": msg["content"]})
            for tc in msg.get("tool_calls", []):
                content.append({
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["name"],
                    "input": tc["arguments"],
                })
            result.append({"role": "assistant", "content": content})

        elif role == "tool":
            # Tool results must be batched into a single user message
            tool_results = []
            while i < len(messages) and messages[i]["role"] == "tool":
                tr = messages[i]
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tr["tool_call_id"],
                    "content": str(tr["content"]),
                })
                i += 1
            result.append({"role": "user", "content": tool_results})
            continue  # already advanced i

        i += 1
    return result


def _to_openai_messages(messages: list[dict]) -> list[dict]:
    """Normalized → OpenAI message format."""
    result = []
    for msg in messages:
        role = msg["role"]

        if role == "user":
            result.append({"role": "user", "content": msg["content"]})

        elif role == "assistant":
            out: dict[str, Any] = {"role": "assistant"}
            if msg.get("content"):
                out["content"] = msg["content"]
            if msg.get("tool_calls"):
                out["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc["arguments"]),
                        },
                    }
                    for tc in msg["tool_calls"]
                ]
            result.append(out)

        elif role == "tool":
            result.append({
                "role": "tool",
                "tool_call_id": msg["tool_call_id"],
                "content": str(msg["content"]),
            })

    return result


def _tools_to_anthropic(tools: list[dict]) -> list[dict]:
    """Normalize tool schemas → Anthropic tool format."""
    result = []
    for t in tools:
        params = t.get("parameters", {})
        result.append({
            "name": t["name"],
            "description": t.get("description", ""),
            "input_schema": {
                "type": "object",
                "properties": {
                    k: {"type": v.get("type", "string"), "description": v.get("description", "")}
                    for k, v in params.items()
                },
                "required": t.get("required", list(params.keys())),
            },
        })
    return result


def _tools_to_openai(tools: list[dict]) -> list[dict]:
    """Normalize tool schemas → OpenAI function format."""
    result = []
    for t in tools:
        params = t.get("parameters", {})
        result.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": {
                    "type": "object",
                    "properties": {
                        k: {"type": v.get("type", "string"), "description": v.get("description", "")}
                        for k, v in params.items()
                    },
                    "required": t.get("required", list(params.keys())),
                },
            },
        })
    return result


def messages_to_normalized(
    assistant_response: ProviderResponse,
    tool_results: list[tuple[ToolCall, str]],
) -> list[dict]:
    """Build normalized message dicts from a completed turn."""
    out = []

    # Assistant message (may have text + tool calls)
    assistant_msg: dict[str, Any] = {"role": "assistant"}
    if assistant_response.text:
        assistant_msg["content"] = assistant_response.text
    else:
        assistant_msg["content"] = None
    if assistant_response.tool_calls:
        assistant_msg["tool_calls"] = [
            {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
            for tc in assistant_response.tool_calls
        ]
    out.append(assistant_msg)

    # Tool result messages
    for tc, result in tool_results:
        out.append({
            "role": "tool",
            "tool_call_id": tc.id,
            "name": tc.name,
            "content": result,
        })

    return out
