# localfamous

Run your own AI agent on hardware you own. Stays on, remembers things, does work while you sleep.

---

## What it actually does

You write three files. The harness turns them into a running AI agent with:

- A persistent identity (system prompt in a Markdown file)
- Tool access (shell, web search, file read/write, Slack — you pick per-agent)
- A cron schedule (morning briefing at 7am, health check every hour, whatever you want)
- A Slack interface (send it a message, it replies; it posts to channels on schedule)
- Full conversation history that survives restarts

This repo runs a household of agents in production — an ops monitor, a research assistant, a content writer. The harness is public. The personas aren't.

---

## Before you start: what you actually need

| Requirement | Notes |
|-------------|-------|
| Python 3.11+ | Standard install |
| An LLM API key | Anthropic (recommended), OpenRouter, or a local llama.cpp/Ollama server |
| A Linux server or VPS | To run 24/7. A $6/mo VPS works. Your home server works. Your laptop works for testing. |
| Slack workspace (optional) | Only needed for the Slack interface. Not required for scheduling or terminal use. |

That's it. No Docker. No cloud accounts. No databases to provision.

---

## Quick start (5 minutes)

```bash
git clone https://github.com/randomchaos7800-hub/localfamous.git
cd localfamous
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
python main.py chat --persona interactive
```

You're now talking to an agent. Type `exit` to quit.

---

## The core idea: personas

Every agent is a **persona** — a folder with three files:

```
personas/
  my-agent/
    system.md      ← who this agent is
    tools.toml     ← what tools it can use, which model, Slack config
    schedule.toml  ← what it does automatically on a cron schedule
```

The harness reads these files and does the rest. Adding a new agent means writing three files. No code.

### system.md — the agent's identity

This is the system prompt. Plain Markdown. Write it like you're briefing a new hire:

```markdown
# Alex — Research Assistant

You research topics thoroughly and report clearly.

## Rules
- Always cite sources
- If you can't find something, say so directly
- Use web_search before answering questions about current events
```

### tools.toml — what the agent can do

```toml
# Which tools this agent is allowed to use
allowed_tools = ["web_search", "web_fetch", "file_read", "file_write"]

# Which model to use
[provider]
format = "anthropic"
model = "claude-haiku-4-5-20251001"

# How much context to load (none | summary | full)
[context]
level = "full"
```

Available tools out of the box: `shell`, `file_read`, `file_write`, `web_search`, `web_fetch`, `slack_send`, `slack_read`, `telegram_send`.

### schedule.toml — what it does automatically

```toml
[[task]]
name = "morning-briefing"
cron = "0 7 * * *"       # 7am every day
enabled = true
prompt = """
Check the weather, scan for AI news from the last 24 hours, and post a morning briefing
to #general with: weather, top 3 AI stories, and one sentence of commentary on each.
"""
```

The prompt is plain English. The agent figures out the tool calls.

---

## What goes in memory/

The `memory/` folder holds context that every agent gets injected into their system prompt — things like:

- What machines you run and what's on them
- What services are running and their status
- Standing rules that apply to all agents
- Notes you want all agents to have access to

These are just Markdown files. Create as many as you want. Update them and the next message picks up the change — no restart needed.

```
memory/
  infrastructure.md    ← "home server is at 192.168.1.10, runs Ubuntu 24.04..."
  rules.md             ← "never commit secrets, always use UTC timestamps..."
  services.md          ← "nginx on port 443, postgres on 5432..."
```

Start with an empty `memory/` folder and add files as you learn what context your agents need.

---

## Runtime modes

```bash
# Terminal chat — talk to any persona interactively
python main.py chat --persona assistant

# One-shot — run a prompt and exit (good for scripts)
python main.py run --persona assistant --prompt "Summarize what happened in AI today"

# Scheduler — runs all persona cron tasks (keep this running 24/7)
python main.py schedule

# Slack bot — listens for messages in configured channels
python main.py slack --persona assistant

# API server — OpenAI-compatible endpoint (works with Open WebUI)
python main.py serve
```

---

## Running 24/7 with systemd

To keep the scheduler running after you close your terminal or the server reboots:

```bash
# Copy the service file
mkdir -p ~/.config/systemd/user/
cp localfamous.service ~/.config/systemd/user/

# Enable and start
systemctl --user enable localfamous
systemctl --user start localfamous

# Watch logs
journalctl --user -u localfamous -f
```

The service file runs `python main.py schedule`. To also run a Slack bot 24/7, copy and edit the service file to run `python main.py slack --persona assistant` instead (and give it a different name).

---

## Adding a tool

Drop a Python file in `tools/` with two things: a `SCHEMA` dict and an `async execute()` function. It's auto-discovered on next start.

```python
# tools/my_tool.py

SCHEMA = {
    "name": "my_tool",
    "description": "Does the thing",
    "parameters": {
        "input": {"type": "string", "description": "What to process"},
    },
    "required": ["input"],
}

async def execute(input: str, ctx: dict = {}) -> str:
    return f"processed: {input}"
```

Add `"my_tool"` to `allowed_tools` in any persona's `tools.toml` and it's available.

---

## Behavioral contracts

Every tool call can be gated by a contract — a set of rules evaluated before and after execution.

Add a `contracts.toml` to any persona folder:

```toml
# personas/assistant/contracts.toml

# Hard = block execution if violated
[[hard]]
id = "no-destructive-shell"
tool = "shell"
description = "Shell must not run destructive commands"
expression = "not any(p in command for p in ['rm -rf /', 'mkfs', 'dd if='])"
recovery = "reject"

# Soft = allow execution but log the violation
[[soft]]
id = "non-empty-result"
tool = "*"
description = "Tool results should not be empty"
expression = "len(result.strip()) > 0"
recovery = "warn"
```

Expressions are plain Python, evaluated in a sandbox. Variables available in the sandbox: `command`, `path`, `query`, `content`, `url` (tool inputs), `result` (tool output, for postconditions). Helper functions: `is_safe_path(p)`, `contains_pii(s)`, `matches(pattern, s)`.

View contract compliance stats:

```bash
python main.py contracts --list                    # show all loaded contracts
python main.py contracts --persona assistant       # 7-day summary
python main.py contracts --persona assistant --all # all-time
```

Based on [Bhardwaj (2025) "Agent Behavioral Contracts"](https://arxiv.org/abs/2602.22302). Overhead is under 1ms per tool call.

---

## Routing to local models

No cloud required. Point any persona at a local inference server:

```toml
# personas/assistant/tools.toml
[provider]
format = "openai"
endpoint = "http://localhost:11434/v1"   # Ollama
model = "llama3.2"
```

Works with Ollama, llama.cpp, LM Studio, or anything that speaks the OpenAI chat completions format. Mix and match: cheap local model for scheduled tasks, Sonnet for live chat.

---

## Connecting to orchestra (optional)

[orchestra](https://github.com/randomchaos7800-hub/orchestra) turns your AI conversation exports into a searchable knowledge base. Pair it with localfamous and your agents can query everything you've researched.

```toml
# Add to a persona's allowed_tools
allowed_tools = ["web_search", "orchestra_search"]
```

---

## Example personas

See [EXAMPLES.md](EXAMPLES.md) for five production personas with complete three-file configs:

- **Kato** — ops monitor, morning briefings, AI news digest, GitHub scout
- **CJ** — research writer, arXiv sweeps, weekly article drafts
- **Morty** — social content strategist, drafts 3x daily
- **Sabrina** — single-purpose grocery bot (event-driven, no crons)
- **Mike** — long-running AI consciousness research agent

Each one shows a different scope, model choice, schedule pattern, and personality.

---

## What's not in this repo

This is the harness. The personas you'll build — the actual agent identities, schedules, and memory files — are yours to write. `personas/assistant/` is a starting point. `personas/interactive/` works out of the box.

---

## Built on

Python 3.11 · Anthropic SDK · OpenAI SDK · SQLite · croniter · Starlette · slack_sdk

---

*The harness is open. The agents are yours.*
