# localfamous — Architecture

## What It Is

localfamous is a single Python harness for running multiple AI agents on hardware you own.
Instead of separate codebases per agent, each agent is a **persona**: a directory with three config files.

```
personas/
  assistant/
    system.md      # who this agent is, rules, voice
    tools.toml     # allowed tools, provider, slack config, context level
    schedule.toml  # cron tasks
  interactive/
    system.md
    tools.toml
```

The harness provides everything else: the LLM loop, tool execution, session persistence,
context assembly, Slack interface, and scheduler. Adding a new agent means writing three files —
no new codebase.

## Runtime Modes

| Mode | Command | Purpose |
|------|---------|---------|
| Scheduler | `python main.py schedule` | Cron tasks for all personas |
| Slack bot | `python main.py slack --persona assistant` | Real-time Slack Socket Mode |
| API proxy | `python main.py serve` | OpenAI-compatible endpoint for Open WebUI |
| Interactive | `python main.py chat` | Ad-hoc terminal chat |

Each mode runs as a separate systemd user service. They share the same codebase and SQLite
session database but are otherwise independent processes.

## Context Assembly

On every request, the system prompt is assembled fresh from disk:

```
memory/*.md                ← operator context: machines, agents, services, rules
personas/<name>/system.md  ← persona identity, voice, rules
current date/time
```

No static prompts. Update a memory file on disk, the next message picks it up with no restart.

## Session Persistence

All conversations live in `data/sessions.db` (SQLite, WAL mode).

Slack threads map to sessions via `external_id = "{channel_id}:{thread_ts}"`. Pick up a thread
days later and the agent has full history. When context window fills, older messages are summarized
and replaced in the database — threads stay coherent indefinitely.

## Inference

localfamous routes to whatever provider the persona's `tools.toml` specifies:

| Config | Backend |
|--------|---------|
| `format = "anthropic"` | Anthropic API (claude-haiku, claude-sonnet, etc.) |
| `format = "openai"` + local endpoint | llama.cpp, Ollama, LM Studio, vLLM |
| `format = "openai"` + OpenRouter | Any model on OpenRouter |

A persona can use a cheap fast model (Haiku) for scheduled tasks and a smarter one for interactive
use. Switch without touching the harness — just update `tools.toml`.

## Tools

Drop a Python file into `tools/` with `SCHEMA` and `async def execute()`. It's auto-discovered
on next start. No registration needed.

Built-in tools: `shell`, `file_read`, `file_write`, `web_search`, `web_fetch`,
`slack_send`, `slack_read`, `telegram_send`, `orchestra_search`.

`orchestra_search` queries an [orchestra](https://github.com/randomchaos7800-hub/orchestra)
knowledge base — your compiled conversation history and research notes. Set `ORCHESTRA_PATH`
or `[orchestra] path` in `localfamous.toml` to enable it.

## Stuck Loop Detection

The loop engine detects three patterns that indicate the model is stuck:

1. Same tool call repeated 3× in a row
2. Alternating A-B-A-B oscillation
3. Hallucinated tool name called twice

On detection, the loop raises `StuckLoopError` rather than burning tokens indefinitely.

## File Layout

```
localfamous/
├── main.py              # CLI entry point
├── loop.py              # agentic loop (tool calling, stuck detection)
├── provider.py          # Anthropic + OpenAI-compat adapters
├── config.py            # TOML loader, vault integration
├── context.py           # system prompt assembly
├── session.py           # SQLite session persistence
├── scheduler.py         # cron scheduler
├── serve.py             # OpenAI-compatible API endpoint
├── config/
│   └── localfamous.toml # global config
├── personas/
│   ├── interactive/     # live chat persona
│   └── assistant/       # example scheduled persona
├── tools/               # tool modules (auto-discovered)
├── interfaces/
│   └── slack.py         # Socket Mode Slack interface
├── memory/              # operator context .md files (you write these)
└── data/                # sessions.db, logs (gitignored)
```
