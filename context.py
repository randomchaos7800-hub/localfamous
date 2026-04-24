"""Context engine — reads memory and persona files, assembles system prompt.

On every conversation start:
  1. Read operator memory (SUMMARY.md, CASE_LAW.md, topic files from ~/.claude/)
  2. Read all .md files from frank memory/ directory
  3. Read the persona's system.md
  4. Assemble in priority order: operator context → frank memory → persona → date

Context window management:
  - Track rough token count (4 chars ≈ 1 token)
  - When history approaches limit, summarize oldest messages
  - Memory files are never compacted — history is

Context levels (for benchmarking):
  - "none"    — no memory injected, persona system.md only
  - "summary" — SUMMARY.md + CASE_LAW.md only
  - "full"    — everything (default)
"""

from pathlib import Path
from datetime import datetime


def count_tokens(text: str) -> int:
    """Rough token estimate: 4 chars per token."""
    return len(text) // 4

# Keep private alias for internal use
_count_tokens = count_tokens


def _read_md_files(directory: Path, exclude: set[str] | None = None) -> list[tuple[str, str]]:
    """Read all .md files in a directory. Returns list of (stem, content)."""
    if not directory.exists():
        return []
    files = sorted(directory.glob("*.md"))
    result = []
    for f in files:
        if exclude and f.name in exclude:
            continue
        content = f.read_text(encoding="utf-8").strip()
        if content:
            result.append((f.stem, content))
    return result


def _read_file(path: Path) -> str | None:
    """Read a single file, return content or None if missing."""
    p = Path(str(path).replace("~", str(Path.home())))
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8").strip() or None


def load_operator_memory(operator_cfg: dict, level: str = "full") -> list[tuple[str, str]]:
    """
    Load operator memory files based on context level.

    Returns list of (label, content) pairs in injection order.

    Levels:
      none    — empty list
      summary — named files only (SUMMARY.md, CASE_LAW.md)
      full    — named files + all topic dir files
    """
    if level == "none":
        return []

    result = []

    # Named files (SUMMARY.md, CASE_LAW.md, etc.)
    for raw_path in operator_cfg.get("files", []):
        p = Path(str(raw_path).replace("~", str(Path.home())))
        content = _read_file(p)
        if content:
            result.append((p.stem, content))

    if level == "summary":
        return result

    # Topic directory (all .md files)
    topic_raw = operator_cfg.get("topic_dir", "")
    if topic_raw:
        topic_dir = Path(str(topic_raw).replace("~", str(Path.home())))
        exclude = set(operator_cfg.get("topic_exclude", []))
        result.extend(_read_md_files(topic_dir, exclude=exclude))

    return result


def assemble(
    persona_dir: Path,
    memory_dir: Path,
    extra: str | None = None,
    operator_cfg: dict | None = None,
    context_level: str = "full",
) -> str:
    """
    Assemble the full system prompt for a persona.

    Args:
        persona_dir:    Path to the persona directory (contains system.md)
        memory_dir:     Path to frank-local memory dir
        extra:          Optional extra text appended at end
        operator_cfg:   [operator_memory] section from frank.toml
        context_level:  "none" | "summary" | "full"

    Returns:
        Assembled system prompt string and its token count.
    """
    parts = []

    # 1. Operator memory (SUMMARY, CASE_LAW, topic files)
    if operator_cfg and context_level != "none":
        op_files = load_operator_memory(operator_cfg, level=context_level)
        if op_files:
            op_sections = [f"# {name}\n\n{content}" for name, content in op_files]
            parts.append("# Operator Context\n\n" + "\n\n---\n\n".join(op_sections))

    # 2. Frank-local memory files
    memory_files = _read_md_files(memory_dir)
    if memory_files:
        mem_sections = [f"# {name}\n\n{content}" for name, content in memory_files]
        parts.append("# Operator Memory\n\n" + "\n\n---\n\n".join(mem_sections))

    # 3. Persona system prompt
    system_file = persona_dir / "system.md"
    if system_file.exists():
        parts.append(system_file.read_text(encoding="utf-8").strip())

    # 4. Current date/time
    now = datetime.now()
    parts.append(
        f"Current date: {now.strftime('%Y-%m-%d')}\n"
        f"Current time: {now.strftime('%H:%M')} (local)\n"
        f"Day: {now.strftime('%A')}"
    )

    if extra:
        parts.append(extra)

    return "\n\n---\n\n".join(p for p in parts if p)


def assemble_with_stats(
    persona_dir: Path,
    memory_dir: Path,
    operator_cfg: dict | None = None,
    context_level: str = "full",
) -> tuple[str, dict]:
    """
    Like assemble(), but also returns a stats dict for benchmarking.

    Returns: (system_prompt, stats)
    stats keys: total_tokens, operator_tokens, memory_tokens, persona_tokens
    """
    # Operator memory
    op_files = load_operator_memory(operator_cfg or {}, level=context_level)
    op_text = "\n".join(c for _, c in op_files)
    op_tokens = _count_tokens(op_text)

    # Frank memory
    mem_files = _read_md_files(memory_dir)
    mem_text = "\n".join(c for _, c in mem_files)
    mem_tokens = _count_tokens(mem_text)

    # Persona system
    system_file = persona_dir / "system.md"
    persona_text = system_file.read_text(encoding="utf-8").strip() if system_file.exists() else ""
    persona_tokens = _count_tokens(persona_text)

    system = assemble(persona_dir, memory_dir, operator_cfg=operator_cfg, context_level=context_level)
    total_tokens = _count_tokens(system)

    stats = {
        "total_tokens": total_tokens,
        "operator_tokens": op_tokens,
        "memory_tokens": mem_tokens,
        "persona_tokens": persona_tokens,
        "context_level": context_level,
    }
    return system, stats


def token_count_messages(messages: list[dict]) -> int:
    """Estimate token count of message history."""
    total = 0
    for msg in messages:
        if isinstance(msg.get("content"), str):
            total += _count_tokens(msg["content"])
        for tc in msg.get("tool_calls", []):
            total += _count_tokens(str(tc))
    return total


def compact_messages(
    messages: list[dict],
    provider,
    system: str,
    max_tokens: int,
    keep_recent: int = 6,
    session=None,
    session_id: str | None = None,
) -> list[dict]:
    """
    Summarize old messages when context is getting full.

    Strategy:
      - Always keep the last `keep_recent` messages intact
      - Summarize everything before that into a single system-injected note
      - Uses the same provider (same model) for summarization
    """
    if not messages:
        return messages

    system_tokens = _count_tokens(system)
    history_tokens = token_count_messages(messages)
    total = system_tokens + history_tokens

    if total < max_tokens * 0.7:
        return messages  # plenty of room

    # Split: old messages to summarize vs recent to keep
    if len(messages) <= keep_recent:
        return messages  # can't compact further

    old = messages[:-keep_recent]
    recent = messages[-keep_recent:]

    # Build a summarization prompt
    old_text = []
    for msg in old:
        role = msg["role"]
        content = msg.get("content", "")
        if msg.get("tool_calls"):
            tc_names = [tc["name"] for tc in msg["tool_calls"]]
            content = f"[Called tools: {', '.join(tc_names)}] {content or ''}"
        old_text.append(f"{role.upper()}: {content}")

    summary_prompt = (
        "Summarize the following conversation history in 3-5 bullet points. "
        "Preserve key decisions, facts discovered, and actions taken. Be specific.\n\n"
        + "\n".join(old_text)
    )

    try:
        resp = provider.complete(
            messages=[{"role": "user", "content": summary_prompt}],
            tools=[],
            system="You are a conversation summarizer. Be concise and factual.",
        )
        summary = resp.text or "(summary unavailable)"
    except Exception as e:
        summary = f"(earlier conversation — {len(old)} messages — summary failed: {e})"

    summary_message = {
        "role": "user",
        "content": f"[CONVERSATION SUMMARY — earlier context]\n{summary}",
    }

    # Persist compacted form back to SQLite so next load gets the summary, not raw history
    if session and session_id:
        try:
            boundary_id = session.get_nth_from_last_message_id(session_id, keep_recent)
            if boundary_id:
                session.replace_with_summary(session_id, summary, boundary_id)
        except Exception:
            pass  # compaction is best-effort; never crash the loop over it

    return [summary_message] + recent
