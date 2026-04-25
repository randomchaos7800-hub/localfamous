from __future__ import annotations
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from contracts.spec import Clause, Contract
    from contracts.store import ViolationStore

log = logging.getLogger("frank.contracts")


def _contains_pii(s: str) -> bool:
    patterns = [
        r"\b\d{3}-\d{2}-\d{4}\b",
        r"\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b",
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",
    ]
    return any(re.search(p, s) for p in patterns)


def _is_safe_path(p: str) -> bool:
    safe_prefixes = ("/home/dino", "/tmp", "/var/tmp")
    normalized = str(p).rstrip("/")
    return any(normalized.startswith(prefix) for prefix in safe_prefixes)


def _matches(pattern: str, s: str) -> bool:
    return bool(re.search(pattern, s))


_HELPERS = {
    "contains_pii": _contains_pii,
    "is_safe_path": _is_safe_path,
    "matches": _matches,
    "re": re,
    "len": len,
    "any": any,
    "all": all,
    "str": str,
    "int": int,
    "bool": bool,
    "list": list,
    "dict": dict,
    "True": True,
    "False": False,
}


@dataclass
class Violation:
    clause_id: str
    severity: Literal["hard", "soft"]
    tool: str
    description: str
    message: str


def _tool_matches(clause_tool: str | list, tool_name: str) -> bool:
    if isinstance(clause_tool, list):
        return tool_name in clause_tool
    return clause_tool == "*" or clause_tool == tool_name


def _safe_eval(expression: str, context: dict) -> bool:
    sandbox = {**_HELPERS, **context, "__builtins__": {}}
    try:
        result = eval(expression, sandbox)  # noqa: S307
        return bool(result)
    except Exception as e:
        raise RuntimeError(f"Expression error in '{expression}': {e}") from e


class ContractEvaluator:
    def __init__(self, contract: "Contract", store: "ViolationStore", session_id: str, persona: str):
        self._contract = contract
        self._store = store
        self._session_id = session_id
        self._persona = persona
        self._total_calls = 0
        self._hard_count = 0
        self._soft_count = 0
        self._recovered = 0

    def check_pre(self, tool_name: str, kwargs: dict) -> Violation | None:
        """Check precondition clauses. Returns first hard violation, logs soft violations."""
        self._total_calls += 1
        context = {k: v for k, v in kwargs.items() if isinstance(v, (str, int, float, bool, list, dict))}

        first_hard: Violation | None = None

        for clause in self._contract.hard:
            if not _tool_matches(clause.tool, tool_name):
                continue
            if "result" in clause.expression:
                continue
            try:
                passes = _safe_eval(clause.expression, context)
            except RuntimeError as e:
                log.warning(f"[contracts] Hard clause eval error ({clause.id}): {e}")
                continue
            if not passes:
                v = Violation(
                    clause_id=clause.id,
                    severity="hard",
                    tool=tool_name,
                    description=clause.description,
                    message=f"Hard contract violated: {clause.description}",
                )
                self._hard_count += 1
                self._store.record(self._session_id, self._persona, tool_name, clause.id, "hard", clause.description)
                self._flush_metrics()
                if first_hard is None:
                    first_hard = v

        if first_hard:
            return first_hard

        for clause in self._contract.soft:
            if not _tool_matches(clause.tool, tool_name):
                continue
            if "result" in clause.expression:
                continue
            try:
                passes = _safe_eval(clause.expression, context)
            except RuntimeError as e:
                log.warning(f"[contracts] Soft clause eval error ({clause.id}): {e}")
                self._soft_count += 1
                self._store.record(self._session_id, self._persona, tool_name, clause.id, "soft", f"eval error: {e}")
                self._flush_metrics()
                continue
            if not passes:
                self._soft_count += 1
                self._store.record(self._session_id, self._persona, tool_name, clause.id, "soft", clause.description)
                self._flush_metrics()
                log.info(f"[contracts] Soft violation: {clause.id} on {tool_name}")

        return None

    def check_post(self, tool_name: str, kwargs: dict, result: str) -> list[Violation]:
        """Check postcondition clauses (those referencing 'result'). Returns violations found."""
        context = {k: v for k, v in kwargs.items() if isinstance(v, (str, int, float, bool, list, dict))}
        context["result"] = result

        violations: list[Violation] = []

        for severity, clauses in [("hard", self._contract.hard), ("soft", self._contract.soft)]:
            for clause in clauses:
                if not _tool_matches(clause.tool, tool_name):
                    continue
                if "result" not in clause.expression:
                    continue
                try:
                    passes = _safe_eval(clause.expression, context)
                except RuntimeError as e:
                    log.warning(f"[contracts] Post clause eval error ({clause.id}): {e}")
                    continue
                if not passes:
                    v = Violation(
                        clause_id=clause.id,
                        severity=severity,
                        tool=tool_name,
                        description=clause.description,
                        message=f"{severity.capitalize()} post-contract violated: {clause.description}",
                    )
                    violations.append(v)
                    if severity == "hard":
                        self._hard_count += 1
                    else:
                        self._soft_count += 1
                    self._store.record(self._session_id, self._persona, tool_name, clause.id, severity, clause.description)
                    self._flush_metrics()
                    log.info(f"[contracts] Post violation ({severity}): {clause.id} on {tool_name}")

        return violations

    def record_recovery(self):
        self._recovered += 1
        self._flush_metrics()

    def _flush_metrics(self):
        from contracts.drift import compute_drift
        metrics = compute_drift(self._total_calls, self._hard_count, self._soft_count, self._recovered)
        self._store.update_metrics(
            self._session_id, self._persona,
            self._total_calls, self._hard_count, self._soft_count, self._recovered,
            metrics["alpha"], metrics["gamma"], metrics["drift"],
        )

    def get_stats(self) -> dict:
        from contracts.drift import compute_drift
        m = compute_drift(self._total_calls, self._hard_count, self._soft_count, self._recovered)
        return {
            "total_calls": self._total_calls,
            "hard_violations": self._hard_count,
            "soft_violations": self._soft_count,
            "recovered": self._recovered,
            **m,
        }
