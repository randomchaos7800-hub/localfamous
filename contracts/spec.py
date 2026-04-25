from __future__ import annotations
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib


@dataclass
class Clause:
    id: str
    description: str
    expression: str
    tool: str | list[str]
    recovery: str = "warn"


@dataclass
class Contract:
    persona: str
    hard: list[Clause] = field(default_factory=list)
    soft: list[Clause] = field(default_factory=list)


def load_contracts(persona_dir: Path | str) -> Contract:
    """Load contracts.toml from a persona directory. Returns empty Contract if file missing."""
    persona_dir = Path(persona_dir)
    name = persona_dir.name
    path = persona_dir / "contracts.toml"

    if not path.exists():
        return Contract(persona=name)

    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except Exception as e:
        raise ValueError(f"Failed to parse {path}: {e}") from e

    def _parse_clauses(raw: list[dict]) -> list[Clause]:
        clauses = []
        for item in raw:
            tool = item.get("tool", "*")
            clauses.append(Clause(
                id=item["id"],
                description=item.get("description", ""),
                expression=item["expression"],
                tool=tool,
                recovery=item.get("recovery", "warn"),
            ))
        return clauses

    return Contract(
        persona=name,
        hard=_parse_clauses(data.get("hard", [])),
        soft=_parse_clauses(data.get("soft", [])),
    )
