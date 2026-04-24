"""Read a file from disk."""

import logging
from pathlib import Path

log = logging.getLogger("frank.tools.file_read")

SCHEMA = {
    "name": "file_read",
    "description": "Read the contents of a file from disk. Use for reading notes, configs, logs, or source files.",
    "parameters": {
        "path": {
            "type": "string",
            "description": "Absolute or home-relative path (~/) to the file",
        },
        "max_chars": {
            "type": "number",
            "description": "Max characters to return (default: 16000)",
        },
    },
    "required": ["path"],
}

# Paths that are never writable from any persona
READ_ONLY_DIRS = [
    
    
    
]


async def execute(path: str, max_chars: int = 16000, ctx: dict = {}) -> str:
    import asyncio

    resolved = str(Path(path).expanduser().resolve())

    def _read():
        p = Path(resolved)
        if not p.exists():
            return f"Error: file not found: {resolved}"
        if not p.is_file():
            return f"Error: not a file: {resolved}"

        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"Error reading file: {e}"

        if len(content) > max_chars:
            content = content[:max_chars] + f"\n\n[Truncated at {max_chars} chars]"
        return content

    return await asyncio.to_thread(_read)
