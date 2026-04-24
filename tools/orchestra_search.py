"""Search an orchestra knowledge base.

orchestra (https://github.com/randomchaos7800-hub/orchestra) turns your AI
conversation exports into a structured, searchable wiki. This tool lets your
agent query it.

Setup:
  1. Clone orchestra and run Capture on your conversation exports
  2. Set ORCHESTRA_PATH in your environment or frank config:
       export ORCHESTRA_PATH=/path/to/your/orchestra
  3. Add "orchestra_search" to allowed_tools in your persona's tools.toml
"""

import asyncio
import os
import subprocess
from pathlib import Path

SCHEMA = {
    "name": "orchestra_search",
    "description": (
        "Search the orchestra knowledge base — compiled notes, research, and conversation "
        "history. Use this before answering questions about topics the operator has "
        "researched, decisions that have been made, or project history."
    ),
    "parameters": {
        "query": {
            "type": "string",
            "description": "Search query",
        },
        "tag": {
            "type": "string",
            "description": "Filter results by tag (optional)",
        },
        "section": {
            "type": "string",
            "description": "Search within a specific wiki section directory (optional)",
        },
    },
    "required": ["query"],
}


async def execute(query: str, tag: str = "", section: str = "", ctx: dict = {}) -> str:
    orchestra_path = (
        ctx.get("frank_config", {}).get("orchestra", {}).get("path")
        or os.environ.get("ORCHESTRA_PATH")
    )
    if not orchestra_path:
        return (
            "ORCHESTRA_PATH is not set. Set it in your environment or in "
            "config/localfamous.toml under [orchestra] path = \"/path/to/orchestra\"."
        )

    search_script = Path(orchestra_path) / "tools" / "search.py"
    if not search_script.exists():
        return f"orchestra not found at {orchestra_path}. Check ORCHESTRA_PATH."

    cmd = ["python3", str(search_script), query]
    if tag:
        cmd += ["--tag", tag]
    if section:
        cmd += ["--section", section]

    def _run():
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=orchestra_path,
            )
            output = result.stdout.strip()
            if result.returncode != 0 and result.stderr:
                return f"Search error: {result.stderr.strip()}"
            return output or "No results found."
        except subprocess.TimeoutExpired:
            return "Search timed out."
        except Exception as e:
            return f"Search failed: {e}"

    return await asyncio.to_thread(_run)
