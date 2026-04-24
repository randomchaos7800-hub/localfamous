# localfamous

Run your own AI agents on hardware you own. Stays on, remembers things, does work while you sleep.

---

## What You're Building

A personal AI agent stack that:
- Runs 24/7 on your home server or a cheap VPS
- Has multiple personas — each with its own identity, tools, and schedule
- Talks to you through Slack (or a terminal)
- Remembers conversations across sessions
- Runs cron jobs while you sleep (morning briefings, health checks, research digests)
- Routes to local models or cloud APIs per-persona

This is the architecture that runs a household of 7 agents in production. The harness is public. The personas aren't.

---

## Why Build Instead of Using a Pre-Built Tool?

**Your memory is yours.** Conversations live in a SQLite file on your machine. Query it, back it up, migrate it.

**No abstraction layer.** Nothing is hidden. You see exactly what goes into every system prompt.

**No extra subscription.** The only cost is your LLM provider (Anthropic, OpenRouter, or local inference).

**Wire in anything.** Your tools. Your APIs. Your file system. Your Slack channels.

**It doesn't disappear.** Hosted services get acquired, reprice, or shut down. Your systemd service runs until you turn it off.

---

## Quick Start

```bash
git clone https://github.com/randomchaos7800-hub/localfamous.git
cd localfamous
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Set your API key
export ANTHROPIC_API_KEY=sk-ant-...

# Start an interactive chat
python main.py chat --persona interactive
```

---

## Architecture

Each agent is a **persona** — three files in a directory:

```
personas/
  assistant/
    system.md      # who this agent is, rules, voice
    tools.toml     # allowed tools, provider, Slack config
    schedule.toml  # cron tasks
```

The harness provides everything else: the LLM loop, tool execution, session persistence, context assembly, Slack interface, and scheduler.

**Adding a new agent = writing three files. No new codebase.**

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full breakdown.

---

## Runtime Modes

| Mode | Command |
|------|---------|
| Interactive chat | `python main.py chat --persona interactive` |
| Cron scheduler | `python main.py schedule` |
| Slack bot | `python main.py slack --persona assistant` |
| OpenAI-compatible API | `python main.py serve` |

---

## Context Assembly

The system prompt is assembled fresh on every request from files on disk:

```
memory/*.md                ← operator context: your machines, services, rules
personas/<name>/system.md  ← persona identity and voice
current date/time
```

Update a file, the next message picks it up. No restart needed.

---

## Inference

Route each persona to whatever backend fits:

```toml
# Anthropic (cloud)
[provider]
format = "anthropic"
model = "claude-haiku-4-5-20251001"

# Local inference (llama.cpp, Ollama, LM Studio)
[provider]
format = "openai"
endpoint = "http://localhost:11434/v1"
model = "llama3.2"

# OpenRouter
[provider]
format = "openai"
endpoint = "https://openrouter.ai/api/v1"
model = "google/gemma-3-27b-it"
```

Cheap model for scheduled tasks, smarter model for live chat. Switch without touching the harness.

---

## Tools

Drop a Python file in `tools/` with `SCHEMA` and `async def execute()`. Auto-discovered on next start.

Built-in: `shell`, `file_read`, `file_write`, `web_search`, `web_fetch`, `slack_send`, `slack_read`, `telegram_send`.

---

## systemd Setup

```bash
# Install as a user service
mkdir -p ~/.config/systemd/user/
cp localfamous.service ~/.config/systemd/user/
systemctl --user enable localfamous
systemctl --user start localfamous
journalctl --user -u localfamous -f
```

---

## What's Not Included

This is the harness. The personas (the actual agent identities, cron schedules, memory files) are yours to write.

The `personas/assistant/` directory is an example to get you started. The `personas/interactive/` persona is ready to use out of the box.

---

## Connecting to orchestra

[orchestra](https://github.com/randomchaos7800-hub/orchestra) turns your AI conversation exports into a structured, searchable knowledge base. Pair it with localfamous and your agents can query everything you've ever discussed, researched, or decided.

**1. Set up orchestra**

```bash
git clone https://github.com/randomchaos7800-hub/orchestra.git
cd orchestra
pip install -r requirements.txt

# Export your conversations and run Capture
python capture/extract.py --input ~/Downloads/claude-export.json

# Compile the wiki
python tools/compile.py
```

**2. Tell localfamous where it lives**

In `config/localfamous.toml`:

```toml
[orchestra]
path = "/path/to/your/orchestra"
```

Or set an environment variable:

```bash
export ORCHESTRA_PATH=/path/to/your/orchestra
```

**3. Add the tool to a persona**

In `personas/assistant/tools.toml`:

```toml
allowed_tools = [
    "web_search",
    "shell",
    "orchestra_search",   # ← add this
]
```

**4. Use it**

The agent now has a `orchestra_search` tool. It queries your knowledge base before answering questions about anything you've researched. No manual retrieval. The agent decides when to look things up.

```
you: what did we decide about the memory architecture?
agent: [calls orchestra_search("memory architecture decision")]
       → finds the relevant article from your conversation history
       → answers with your actual decision, not a guess
```

The tool supports filtering by tag (`--tag agents`) and section (`--section concepts`) — see the orchestra README for how articles are organized.

---

## Built On

- Python 3.11+
- Anthropic SDK / OpenAI SDK (for OpenAI-compatible backends)
- SQLite (session persistence)
- croniter (scheduling)
- Starlette + uvicorn (API server)
- slack_sdk (Slack Socket Mode)

---

*The harness is open. Make it yours.*
