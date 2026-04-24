"""Execute a shell command. Requires allow_shell=true in frank config."""

import logging
import re
import subprocess

log = logging.getLogger("frank.tools.shell")

SCHEMA = {
    "name": "shell",
    "description": (
        "Execute a shell command and return stdout + stderr. "
        "Use for running scripts, checking system state, or calling existing CLI tools. "
        "Avoid destructive commands. Prefer idempotent operations. "
        "One command per call — do not chain with ; && || or backticks."
    ),
    "parameters": {
        "command": {
            "type": "string",
            "description": "Single shell command to execute. No chaining operators (;, &&, ||, |, backticks).",
        },
        "timeout": {
            "type": "number",
            "description": "Timeout in seconds (default: 30, max: 120)",
        },
        "cwd": {
            "type": "string",
            "description": "Working directory (optional, defaults to frank root)",
        },
    },
    "required": ["command"],
}

# Injection operators — block any attempt to chain commands
_INJECTION_PATTERNS = [
    r";\s*\S",           # semicolon chaining: cmd; cmd2
    r"\|\|",             # or-chaining: cmd || cmd2
    r"&&",               # and-chaining: cmd && cmd2
    r"`[^`]+`",          # backtick substitution: `cmd`
    r"\$\([^)]+\)",      # $() substitution: $(cmd)
]

# Destructive command patterns — never execute regardless of context
_DESTRUCTIVE_PATTERNS = [
    r"rm\s+(-\S+\s+)*-[a-z]*r[a-z]*\s+/",   # rm -rf / or variants targeting root paths
    r"rm\s+(-\S+\s+)*-[a-z]*f[a-z]*\s+/",   # rm -f / variants
    r"\bmkfs\b",                              # format filesystem
    r"\bdd\s+if=",                            # disk dump
    r"(?<![012])\s*>\s*/dev/(?!null|zero|stdin|stdout|stderr)",  # write to raw device (not null/zero/std*)
    r"chmod\s+[0-7]*7+\s+/",                 # chmod 777 /
    r":\(\)\s*\{.*:\|:.*\}",                 # fork bomb
    r"\bshred\b",                             # shred files
    r"\bwipefs\b",                            # wipe filesystem signatures
    r"cat\s+/etc/shadow",                     # shadow password file
    r"cat\s+/etc/passwd",                     # passwd file (direct read)
]

# Sensitive file reads to flag (warn but allow — operator decides)
_SENSITIVE_READS = [
    "/etc/passwd", "/etc/shadow", "/etc/sudoers",
    "~/.ssh/", ".vault/", "secrets", ".env",
]


def _check_injection(command: str) -> str | None:
    """Return description of injection attempt, or None if clean."""
    for pattern in _INJECTION_PATTERNS:
        if re.search(pattern, command):
            return f"command chaining detected (pattern: {pattern!r})"
    return None


def _check_destructive(command: str) -> str | None:
    """Return description of destructive pattern, or None if clean."""
    for pattern in _DESTRUCTIVE_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return f"destructive pattern blocked (pattern: {pattern!r})"
    return None


async def execute(command: str, timeout: int = 30, cwd: str = "", ctx: dict = {}) -> str:
    import asyncio

    frank_config = ctx.get("frank_config", {})
    if not frank_config.get("permissions", {}).get("allow_shell", True):
        return "Error: shell execution is disabled in frank config."

    # Block injection attempts
    injection = _check_injection(command)
    if injection:
        log.warning(f"Shell injection blocked: {command[:100]!r} — {injection}")
        return (
            f"Error: {injection}. "
            f"Submit one command at a time — do not use ; && || backticks or $() substitution. "
            f"Make separate tool calls for separate commands."
        )

    # Block destructive commands
    destructive = _check_destructive(command)
    if destructive:
        log.warning(f"Destructive shell command blocked: {command[:100]!r} — {destructive}")
        return f"Error: {destructive}. This command is not permitted."

    # Warn on sensitive reads (log but don't block — operator controls permissions)
    for sensitive in _SENSITIVE_READS:
        if sensitive in command:
            log.warning(f"Shell accessing sensitive path: {command[:100]!r}")
            break

    timeout = min(int(timeout), 120)
    work_dir = cwd or ctx.get("frank_root") or str(Path.home())

    log.info(f"Shell exec: {command[:100]}")

    def _run():
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=work_dir,
                env={**__import__("os").environ},
            )
            out = result.stdout
            err = result.stderr
            parts = []
            if out:
                parts.append(out.strip())
            if err:
                parts.append(f"[stderr] {err.strip()}")
            if result.returncode != 0:
                parts.append(f"[exit code: {result.returncode}]")
            return "\n".join(parts) if parts else "(no output)"
        except subprocess.TimeoutExpired:
            return f"Error: command timed out after {timeout}s"
        except Exception as e:
            return f"Error running command: {e}"

    return await asyncio.to_thread(_run)
