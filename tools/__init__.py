"""Tool registry — auto-discovers tools in this directory.

Each tool is a module with:
  SCHEMA: dict  — name, description, parameters, required
  async def execute(ctx: dict, **kwargs) -> str

Drop a file in tools/, restart the frank. No registration needed.
"""

import importlib
import logging
import pkgutil
from pathlib import Path
from typing import Any

log = logging.getLogger("frank.tools")

_modules: dict[str, Any] = {}


def _load():
    tools_dir = Path(__file__).parent
    for finder, module_name, _ in pkgutil.iter_modules([str(tools_dir)]):
        if module_name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"tools.{module_name}")
            if hasattr(mod, "SCHEMA") and hasattr(mod, "execute"):
                _modules[mod.SCHEMA["name"]] = mod
                log.debug(f"Loaded tool: {mod.SCHEMA['name']}")
        except Exception as e:
            log.warning(f"Failed to load tool module '{module_name}': {e}")


def get_modules(allowed: list[str] | None = None) -> dict:
    """Return tool modules filtered by allowlist."""
    if not _modules:
        _load()
    if allowed is None:
        return dict(_modules)
    return {k: v for k, v in _modules.items() if k in allowed}


def get_schemas(allowed: list[str] | None = None) -> list[dict]:
    """Return tool schemas for the model."""
    return [mod.SCHEMA for mod in get_modules(allowed).values()]


def list_tools() -> list[str]:
    """Return all available tool names."""
    if not _modules:
        _load()
    return sorted(_modules.keys())


# Load on import
_load()
