"""Config loader — reads TOML files, returns plain dicts. That's all it does."""

import sys
import os
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


def _read_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def load_frank(config_dir: Path | str | None = None) -> dict:
    """Load global frank config from config/frank.toml."""
    if config_dir is None:
        config_dir = Path(__file__).parent / "config"
    path = Path(config_dir) / "localfamous.toml"
    data = _read_toml(path)

    # Apply env overrides
    if "provider" not in data:
        data["provider"] = {}
    if "ANTHROPIC_API_KEY" in os.environ:
        data["provider"].setdefault("frontier_key", os.environ["ANTHROPIC_API_KEY"])
    if "OPENROUTER_API_KEY" in os.environ:
        data["provider"].setdefault("openrouter_key", os.environ["OPENROUTER_API_KEY"])

    return data


def load_persona(persona_dir: Path | str) -> dict:
    """Load persona config: system.md, tools.toml, schedule.toml."""
    persona_dir = Path(persona_dir)
    result: dict[str, Any] = {"name": persona_dir.name}

    # System prompt
    system_file = persona_dir / "system.md"
    result["system"] = system_file.read_text() if system_file.exists() else ""

    # Tools config
    tools_data = _read_toml(persona_dir / "tools.toml")
    result["allowed_tools"] = tools_data.get("allowed_tools", [])
    result["provider"] = tools_data.get("provider", {})
    result["slack"] = tools_data.get("slack", {})
    result["telegram"] = tools_data.get("telegram", {})
    result["scheduler"] = tools_data.get("scheduler", {})
    result["context_level"] = tools_data.get("context", {}).get("level", "full")

    # Schedule
    schedule_data = _read_toml(persona_dir / "schedule.toml")
    result["tasks"] = schedule_data.get("task", [])

    return result


def vault_get(key: str) -> str:
    """Read a secret from the age-encrypted vault."""
    import subprocess
    vault_script = Path.home() / ".vault" / "vault.sh"
    if not vault_script.exists():
        return os.environ.get(key.upper(), "")
    try:
        r = subprocess.run(
            [str(vault_script), "get", key],
            capture_output=True, text=True, timeout=10
        )
        value = r.stdout.strip()
        if not value:
            # Fallback to environment variable
            value = os.environ.get(key.upper(), "")
        return value
    except Exception:
        return os.environ.get(key.upper(), "")
