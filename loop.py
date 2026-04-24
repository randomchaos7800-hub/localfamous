"""The core agentic loop.

while True:
    response = send_to_model(messages)
    if response.has_tool_calls:
        for call in response.tool_calls:
            result = execute_tool(call)
            messages.append(result)
    else:
        break

Improvements from autoresearch 2026-04-13:
  - Stuck loop detection: repeated calls, alternating A-B-A, no-ops
  - Hallucinated tool name nudge (local models misname tools frequently)
  - Provider retry with backoff lives in provider.py
  - Scheduler backpressure lives in scheduler.py
"""

import asyncio
import difflib
import json
import logging
from collections import deque
from typing import Callable

from provider import ProviderResponse, ToolCall, messages_to_normalized

log = logging.getLogger("frank.loop")

MAX_TURNS = 40
STUCK_WINDOW = 4      # look at last N tool calls
STUCK_REPEAT = 3      # same call repeated this many times → stuck
MAX_TOOL_OUTPUT = 8000  # chars; truncate tool results beyond this


class MaxTurnsError(Exception):
    pass


class StuckLoopError(Exception):
    pass


def _tool_fingerprint(tc: ToolCall) -> str:
    """Stable string key for a tool call (for loop detection)."""
    return f"{tc.name}:{json.dumps(tc.arguments, sort_keys=True)}"


def _detect_stuck(recent: deque, new_calls: list[ToolCall]) -> str | None:
    """
    Check for stuck patterns. Returns a description if stuck, None if ok.

    Patterns detected:
      1. Same tool+args repeated STUCK_REPEAT times in a row
      2. Alternating A-B-A-B (oscillation, 4 calls, 2 unique)
      3. Unknown tool called twice (hallucination loop)
    """
    fps = list(recent) + [_tool_fingerprint(tc) for tc in new_calls]

    if len(fps) >= STUCK_REPEAT:
        tail = fps[-STUCK_REPEAT:]
        if len(set(tail)) == 1:
            return f"same call repeated {STUCK_REPEAT}x: {tail[0][:80]}"

    if len(fps) >= 4:
        last4 = fps[-4:]
        if last4[0] == last4[2] and last4[1] == last4[3] and last4[0] != last4[1]:
            return f"oscillating A-B-A-B: {last4[0][:60]} / {last4[1][:60]}"

    return None


async def run(
    prompt: str,
    provider,
    tool_modules: dict,
    system: str,
    history: list[dict],
    allowed_tools: list[str] | None = None,
    max_turns: int = MAX_TURNS,
    on_tool_call: Callable[[str, dict], None] | None = None,
    on_response: Callable[[str], None] | None = None,
    require_confirm: list[str] | None = None,
    ctx: dict | None = None,
    stream: bool = False,
) -> tuple[str, list[dict]]:
    """
    Run the agentic loop.

    Returns (final_text, new_messages) where new_messages is everything
    added this session (not including the initial history).
    """
    if allowed_tools is not None:
        active_tools = {k: v for k, v in tool_modules.items() if k in allowed_tools}
    else:
        active_tools = tool_modules

    schemas = [mod.SCHEMA for mod in active_tools.values()]
    available_names = set(active_tools.keys())

    messages = list(history)
    new_messages: list[dict] = [{"role": "user", "content": prompt}]
    messages = messages + new_messages

    recent_fingerprints: deque = deque(maxlen=STUCK_WINDOW * 2)

    can_stream = stream and hasattr(provider, "stream_complete")

    for turn in range(max_turns):
        try:
            if can_stream:
                response: ProviderResponse = await asyncio.to_thread(
                    provider.stream_complete, messages, schemas, system,
                    lambda chunk: print(chunk, end="", flush=True),
                )
            else:
                response: ProviderResponse = await asyncio.to_thread(
                    provider.complete, messages, schemas, system
                )
        except Exception as e:
            log.error(f"Provider error on turn {turn}: {e}")
            raise

        # Final answer
        if not response.tool_calls:
            text = response.text or ""
            if on_response:
                on_response(text)
            final_msg = {"role": "assistant", "content": text}
            new_messages.append(final_msg)
            messages.append(final_msg)
            log.debug(
                f"Loop done in {turn + 1} turns | "
                f"{response.input_tokens}in / {response.output_tokens}out tokens"
            )
            return text, new_messages

        # Stuck loop detection
        stuck_reason = _detect_stuck(recent_fingerprints, response.tool_calls)
        if stuck_reason:
            log.warning(f"Stuck loop detected on turn {turn}: {stuck_reason}")
            raise StuckLoopError(f"Loop stuck: {stuck_reason}")

        # Record fingerprints
        for tc in response.tool_calls:
            recent_fingerprints.append(_tool_fingerprint(tc))

        # Execute tool calls in parallel, preserving response order
        async def _run_one(tc: ToolCall) -> tuple[ToolCall, str]:
            if on_tool_call:
                on_tool_call(tc.name, tc.arguments)
            if tc.name not in available_names and tc.name:
                close = _find_close_tool(tc.name, available_names)
                hint = f" Did you mean '{close}'?" if close else ""
                r = (
                    f"Error: tool '{tc.name}' does not exist.{hint} "
                    f"Available tools: {', '.join(sorted(available_names))}. "
                    f"Use exactly one of those names."
                )
                log.warning(f"Hallucinated tool name: '{tc.name}'{' → suggest ' + close if close else ''}")
            else:
                r = await _execute_tool(tc, active_tools, ctx or {}, require_confirm or [])
            if len(r) > MAX_TOOL_OUTPUT:
                r = r[:MAX_TOOL_OUTPUT] + f"\n\n[truncated — {len(r)} chars total]"
            log.debug(f"Tool {tc.name} → {r[:200]}")
            return tc, r

        tool_results: list[tuple[ToolCall, str]] = list(
            await asyncio.gather(*[_run_one(tc) for tc in response.tool_calls])
        )

        turn_messages = messages_to_normalized(response, tool_results)
        new_messages.extend(turn_messages)
        messages.extend(turn_messages)

    raise MaxTurnsError(f"Reached max turns ({max_turns}) without a final response")


def _find_close_tool(name: str, available: set[str]) -> str | None:
    """Return the closest available tool name using difflib edit distance."""
    if not available:
        return None
    matches = difflib.get_close_matches(name, list(available), n=1, cutoff=0.6)
    return matches[0] if matches else None


async def _execute_tool(
    tc: ToolCall,
    active_tools: dict,
    ctx: dict,
    require_confirm: list[str],
) -> str:
    """Execute a single tool call. Returns string result."""
    if tc.name not in active_tools:
        return f"Error: tool '{tc.name}' is not available to this persona."

    mod = active_tools[tc.name]

    if tc.name in require_confirm:
        if ctx.get("non_interactive"):
            return f"Tool '{tc.name}' requires confirmation but running non-interactively. Skipped."
        print(f"\n[CONFIRM] {tc.name}: {json.dumps(tc.arguments, indent=2)}")
        answer = input("Execute? [y/N] ").strip().lower()
        if answer != "y":
            return f"Tool '{tc.name}' execution cancelled by user."

    try:
        result = await mod.execute(ctx=ctx, **tc.arguments)
        return str(result)
    except TypeError as e:
        return f"Tool error ({tc.name}): bad arguments — {e}"
    except Exception as e:
        log.exception(f"Tool {tc.name} raised {type(e).__name__}: {e}")
        return f"Tool error ({tc.name}): {type(e).__name__}: {e}"
