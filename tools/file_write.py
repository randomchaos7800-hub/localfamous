"""Write content to a file on disk."""

import logging
import re
from pathlib import Path

log = logging.getLogger("frank.tools.file_write")

SCHEMA = {
    "name": "file_write",
    "description": "Write content to a file. Creates the file if it doesn't exist, creates parent directories as needed.",
    "parameters": {
        "path": {
            "type": "string",
            "description": "Absolute or home-relative path (~/) to the file. Must be a clean path with no shell operators.",
        },
        "content": {
            "type": "string",
            "description": "Content to write to the file",
        },
        "mode": {
            "type": "string",
            "description": "'overwrite' (default) or 'append'",
        },
    },
    "required": ["path", "content"],
}

# Paths that are never writable from any persona
PROTECTED_DIRS = [
    "/etc/",
    "/usr/",
    "/bin/",
    "/sbin/",
    "/boot/",
    "/sys/",
    "/proc/",
]

# Shell injection characters that don't belong in a file path
_PATH_INJECTION_RE = re.compile(r"[;&|`$><\n\r]")


async def execute(path: str, content: str, mode: str = "overwrite", ctx: dict = {}) -> str:
    import asyncio

    # Block injection characters in the path itself
    if _PATH_INJECTION_RE.search(path):
        bad_chars = set(_PATH_INJECTION_RE.findall(path))
        log.warning(f"file_write path injection blocked: {path!r} (chars: {bad_chars})")
        return (
            f"Error: path contains invalid characters {bad_chars}. "
            f"Provide a clean file path with no shell operators."
        )

    resolved = str(Path(path).expanduser().resolve())

    # Block protected directories
    for protected in PROTECTED_DIRS:
        if resolved.startswith(protected):
            log.warning(f"file_write protected path blocked: {resolved!r}")
            return f"Error: '{resolved}' is in a protected directory. Write refused."

    # Block path traversal attempts that escape expected dirs
    if ".." in path:
        log.warning(f"file_write path traversal blocked: {path!r}")
        return f"Error: path traversal ('..') is not allowed."

    def _write():
        p = Path(resolved)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            if mode == "append":
                with open(p, "a", encoding="utf-8") as f:
                    f.write(content)
                log.info(f"file_write append: {resolved} ({len(content)} chars)")
                return f"Appended {len(content)} chars to {resolved}"
            else:
                p.write_text(content, encoding="utf-8")
                log.info(f"file_write overwrite: {resolved} ({len(content)} chars)")
                return f"Wrote {len(content)} chars to {resolved}"
        except Exception as e:
            return f"Error writing file: {e}"

    return await asyncio.to_thread(_write)
