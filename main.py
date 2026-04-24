#!/usr/bin/env python3
"""
Frank — one runtime for all functional agents.

Usage:
  python -m localfamous chat --persona assistant
  python -m localfamous run --persona assistant --prompt "Research latest AI news"
  python -m localfamous schedule
  python -m localfamous sessions [--persona assistant]
  python -m localfamous personas
  python -m localfamous tasks
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# Add frank root to path
FRANK_ROOT = Path(__file__).parent
sys.path.insert(0, str(FRANK_ROOT))

import config as cfg
import context
import loop as loop_mod
import tools as tools_mod
from provider import make_provider
from scheduler import Scheduler
from session import Session


def setup_logging(verbose: bool = False, log_dir: Path | None = None, persona: str | None = None) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]

    if log_dir and persona:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / f"{persona}.log")
        fh.setFormatter(logging.Formatter(fmt))
        handlers.append(fh)

    logging.basicConfig(format=fmt, level=level, handlers=handlers)
    # Quiet noisy libs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def resolve_paths(args) -> tuple[Path, Path, Path, Path]:
    """Resolve frank directory paths."""
    root = FRANK_ROOT
    personas_dir = root / "personas"
    memory_dir = root / "memory"
    config_dir = root / "config"
    data_dir = root / "data"
    return personas_dir, memory_dir, config_dir, data_dir


def build_ctx(persona_name: str, frank_config: dict, persona_config: dict, non_interactive: bool = False) -> dict:
    """Build the tool execution context."""
    return {
        "persona": persona_name,
        "vault_get": cfg.vault_get,
        "frank_config": frank_config,
        "persona_config": persona_config,
        "non_interactive": non_interactive,
        "frank_root": str(FRANK_ROOT),
    }


async def cmd_chat(args, frank_config: dict, personas_dir: Path, memory_dir: Path, data_dir: Path) -> None:
    """Interactive chat with a persona."""
    persona_name = args.persona
    persona_dir = personas_dir / persona_name
    if not persona_dir.exists():
        print(f"Persona '{persona_name}' not found in {personas_dir}", file=sys.stderr)
        sys.exit(1)

    persona_config = cfg.load_persona(persona_dir)
    provider = make_provider(frank_config, persona_config)
    system = context.assemble(persona_dir, memory_dir, operator_cfg=frank_config.get("operator_memory"), context_level=persona_config.get("context_level", "full"))
    max_ctx = frank_config.get("context", {}).get("max_context_tokens", 32000)

    db_path = data_dir / "sessions.db"
    sessions = Session(db_path)
    session_id = args.session or sessions.create(persona_name)

    history = sessions.get_history(session_id) if args.session else []

    tool_modules = tools_mod.get_modules(persona_config.get("allowed_tools"))
    ctx = build_ctx(persona_name, frank_config, persona_config)
    require_confirm = frank_config.get("permissions", {}).get("require_confirmation", [])

    print(f"\n[{persona_name}] Session {session_id} — type 'exit' or Ctrl+C to quit\n")

    while True:
        try:
            prompt = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye.")
            break

        if not prompt:
            continue
        if prompt.lower() in ("exit", "quit", "bye"):
            break

        try:
            # Compact if needed (summaries are persisted back to SQLite)
            history = context.compact_messages(
                history, provider, system, max_ctx,
                session=sessions, session_id=session_id,
            )

            print(f"\n{persona_name}: ", end="", flush=True)
            text, new_msgs = await loop_mod.run(
                prompt=prompt,
                provider=provider,
                tool_modules=tool_modules,
                system=system,
                history=history,
                max_turns=frank_config.get("loop", {}).get("max_turns", 30),
                on_tool_call=lambda name, args: print(f"\n  → [{name}] {list(args.keys())}", flush=True),
                require_confirm=require_confirm,
                ctx=ctx,
                stream=True,
            )
            history.extend(new_msgs)
            sessions.add_messages_bulk(session_id, new_msgs)
            print("\n")

        except loop_mod.MaxTurnsError as e:
            print(f"\nError: {e}\n", file=sys.stderr)
        except KeyboardInterrupt:
            print("\nInterrupted.")
            break


async def cmd_run(args, frank_config: dict, personas_dir: Path, memory_dir: Path, data_dir: Path) -> None:
    """One-shot: run a prompt through a persona."""
    persona_name = args.persona
    persona_dir = personas_dir / persona_name
    if not persona_dir.exists():
        print(f"Persona '{persona_name}' not found.", file=sys.stderr)
        sys.exit(1)

    persona_config = cfg.load_persona(persona_dir)
    provider = make_provider(frank_config, persona_config)
    system = context.assemble(persona_dir, memory_dir, operator_cfg=frank_config.get("operator_memory"), context_level=persona_config.get("context_level", "full"))

    db_path = data_dir / "sessions.db"
    sessions = Session(db_path)
    session_id = args.session or sessions.create(persona_name)
    history = sessions.get_history(session_id) if args.session else []

    tool_modules = tools_mod.get_modules(persona_config.get("allowed_tools"))
    ctx = build_ctx(persona_name, frank_config, persona_config, non_interactive=True)

    prompt = args.prompt
    if not prompt:
        print("--prompt required for 'run' mode", file=sys.stderr)
        sys.exit(1)

    try:
        text, new_msgs = await loop_mod.run(
            prompt=prompt,
            provider=provider,
            tool_modules=tool_modules,
            system=system,
            history=history,
            max_turns=frank_config.get("loop", {}).get("max_turns", 30),
            on_tool_call=lambda name, args: print(f"  → [{name}]", file=sys.stderr),
            ctx=ctx,
        )
        sessions.add_messages_bulk(session_id, new_msgs)
        print(text)
    except loop_mod.MaxTurnsError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


async def cmd_schedule(args, frank_config: dict, personas_dir: Path, memory_dir: Path, data_dir: Path) -> None:
    """Run the scheduler daemon."""
    db_path = data_dir / "sessions.db"
    sessions = Session(db_path)

    async def run_persona(persona_name: str, prompt: str) -> None:
        persona_dir = personas_dir / persona_name
        if not persona_dir.exists():
            logging.error(f"Scheduler: persona '{persona_name}' not found")
            return

        persona_config = cfg.load_persona(persona_dir)
        provider = make_provider(frank_config, persona_config)
        system = context.assemble(persona_dir, memory_dir, operator_cfg=frank_config.get("operator_memory"), context_level=persona_config.get("context_level", "full"))
        tool_modules = tools_mod.get_modules(persona_config.get("allowed_tools"))
        ctx = build_ctx(persona_name, frank_config, persona_config, non_interactive=True)

        session_id = sessions.create(persona_name)
        try:
            text, new_msgs = await loop_mod.run(
                prompt=prompt,
                provider=provider,
                tool_modules=tool_modules,
                system=system,
                history=[],
                max_turns=frank_config.get("loop", {}).get("max_turns", 30),
                ctx=ctx,
            )
            sessions.add_messages_bulk(session_id, new_msgs)
            logging.info(f"[{persona_name}] scheduled task done: {text[:100]}")

            # Deliver output to configured channel
            sched_cfg = persona_config.get("scheduler", {})
            output_channel = sched_cfg.get("output_channel")
            output_platform = sched_cfg.get("output_platform", "slack")
            if output_channel and text:
                try:
                    if output_platform == "slack":
                        from tools.slack_send import execute as slack_send
                        await slack_send(
                            channel=output_channel,
                            message=f"*[{persona_name} / {prompt[:60]}]*\n{text}",
                            ctx=ctx,
                        )
                    elif output_platform == "telegram":
                        from tools.telegram_send import execute as telegram_send
                        await telegram_send(
                            message=f"[{persona_name}]\n{text}",
                            ctx=ctx,
                        )
                except Exception as delivery_err:
                    logging.error(f"[{persona_name}] output delivery failed: {delivery_err}")

        except Exception as e:
            logging.error(f"[{persona_name}] scheduled task error: {e}", exc_info=True)

    scheduler = Scheduler(personas_dir, run_persona)
    print(f"Scheduler started. {len(scheduler.tasks)} tasks loaded. Ctrl+C to stop.")
    try:
        await scheduler.run_forever()
    except KeyboardInterrupt:
        print("\nScheduler stopped.")


def cmd_sessions(args, data_dir: Path) -> None:
    """List sessions."""
    db_path = data_dir / "sessions.db"
    if not db_path.exists():
        print("No sessions found.")
        return
    sessions = Session(db_path)
    rows = sessions.list_sessions(persona=getattr(args, "persona", None))
    if not rows:
        print("No sessions.")
        return
    print(f"{'ID':<12} {'PERSONA':<15} {'UPDATED':<22}")
    print("-" * 50)
    for r in rows:
        print(f"{r['id']:<12} {r['persona']:<15} {r['updated_at']:<22}")


def cmd_personas(personas_dir: Path) -> None:
    """List available personas."""
    if not personas_dir.exists():
        print("No personas directory found.")
        return
    print(f"{'PERSONA':<20} {'TOOLS':<40} SCHEDULE")
    print("-" * 70)
    for d in sorted(personas_dir.iterdir()):
        if not d.is_dir():
            continue
        pc = cfg.load_persona(d)
        tools = ", ".join(pc.get("allowed_tools", [])[:4])
        if len(pc.get("allowed_tools", [])) > 4:
            tools += "..."
        tasks = len(pc.get("tasks", []))
        print(f"{d.name:<20} {tools:<40} {tasks} tasks")


async def cmd_slack(args, frank_config: dict, personas_dir: Path, memory_dir: Path, data_dir: Path) -> None:
    """Run a persona as a Slack Socket Mode bot."""
    from interfaces.slack import SlackInterface
    persona_name = args.persona
    persona_dir = personas_dir / persona_name
    if not persona_dir.exists():
        print(f"Persona '{persona_name}' not found.", file=sys.stderr)
        sys.exit(1)

    persona_config = cfg.load_persona(persona_dir)
    interface = SlackInterface(
        persona_name=persona_name,
        persona_dir=persona_dir,
        memory_dir=memory_dir,
        data_dir=data_dir,
        frank_config=frank_config,
        persona_config=persona_config,
    )
    try:
        await interface.run()
    except KeyboardInterrupt:
        print(f"\n[{persona_name}] Slack interface stopped.")


async def cmd_bench(args, frank_config: dict, personas_dir: Path, memory_dir: Path, data_dir: Path) -> None:
    """Benchmark the local model through the frank."""
    from bench import run_all
    await run_all(
        endpoint=args.endpoint,
        model=args.model,
        context_level=args.context_level,
    )


def cmd_tasks(personas_dir: Path) -> None:
    """List all scheduled tasks."""
    from scheduler import load_tasks
    from croniter import croniter
    tasks = load_tasks(personas_dir)
    if not tasks:
        print("No scheduled tasks found.")
        return
    print(f"{'NAME':<30} {'PERSONA':<15} {'CRON':<20} NEXT RUN")
    print("-" * 85)
    for t in tasks:
        from datetime import datetime
        next_run = croniter(t.cron, datetime.now()).get_next(datetime).strftime("%Y-%m-%d %H:%M")
        status = "  " if t.enabled else "⏸ "
        print(f"{status}{t.name:<28} {t.persona:<15} {t.cron:<20} {next_run}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="frank",
        description="Vitale Dynamics agent frank",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    parser.add_argument("--config-dir", default=None, help="Config directory")

    subs = parser.add_subparsers(dest="command", required=True)

    # chat
    p_chat = subs.add_parser("chat", help="Interactive chat with a persona")
    p_chat.add_argument("--persona", "-p", default="interactive", help="Persona name")
    p_chat.add_argument("--session", "-s", default=None, help="Resume session ID")

    # run
    p_run = subs.add_parser("run", help="One-shot prompt through a persona")
    p_run.add_argument("--persona", "-p", required=True, help="Persona name")
    p_run.add_argument("--prompt", required=True, help="Prompt to run")
    p_run.add_argument("--session", "-s", default=None, help="Session ID to append to")

    # schedule
    subs.add_parser("schedule", help="Run the scheduler daemon")

    # sessions
    p_sess = subs.add_parser("sessions", help="List conversation sessions")
    p_sess.add_argument("--persona", "-p", default=None, help="Filter by persona")

    # personas
    subs.add_parser("personas", help="List available personas")

    # tasks
    subs.add_parser("tasks", help="List scheduled tasks")

    # bench
    p_bench = subs.add_parser("bench", help="Benchmark the local model through the frank")
    p_bench.add_argument("--endpoint", default="http://localhost:8081", help="llama.cpp server endpoint")
    p_bench.add_argument("--model", default="supergemma4-26b-uncensored-fast-v2-Q4_K_M.gguf", help="Model filename")
    p_bench.add_argument("--context-level", default="full", choices=["none", "summary", "full"],
                         help="Operator context to inject: none | summary | full (default: full)")

    # slack
    p_slack = subs.add_parser("slack", help="Run a persona as a Slack Socket Mode bot")
    p_slack.add_argument("--persona", "-p", required=True, help="Persona name")

    # serve
    p_serve = subs.add_parser("serve", help="Run frank as an OpenAI-compatible API proxy (for Open WebUI)")
    p_serve.add_argument("--host", default="127.0.0.1", help="Bind address")
    p_serve.add_argument("--port", type=int, default=8890, help="Port (default: 8890)")

    args = parser.parse_args()

    personas_dir, memory_dir, config_dir, data_dir = resolve_paths(args)
    if args.config_dir:
        config_dir = Path(args.config_dir)

    frank_config = cfg.load_frank(config_dir)
    log_dir = Path(frank_config.get("logging", {}).get("log_dir", str(FRANK_ROOT / "data" / "logs")))
    persona_name = getattr(args, "persona", None)
    setup_logging(args.verbose, log_dir=log_dir, persona=persona_name)

    if args.command == "slack":
        asyncio.run(cmd_slack(args, frank_config, personas_dir, memory_dir, data_dir))
    elif args.command == "chat":
        asyncio.run(cmd_chat(args, frank_config, personas_dir, memory_dir, data_dir))
    elif args.command == "run":
        asyncio.run(cmd_run(args, frank_config, personas_dir, memory_dir, data_dir))
    elif args.command == "schedule":
        asyncio.run(cmd_schedule(args, frank_config, personas_dir, memory_dir, data_dir))
    elif args.command == "sessions":
        cmd_sessions(args, data_dir)
    elif args.command == "personas":
        cmd_personas(personas_dir)
    elif args.command == "tasks":
        cmd_tasks(personas_dir)
    elif args.command == "bench":
        asyncio.run(cmd_bench(args, frank_config, personas_dir, memory_dir, data_dir))
    elif args.command == "serve":
        from serve import run as serve_run
        serve_run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
