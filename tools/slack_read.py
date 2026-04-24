"""Read recent messages from a Slack channel."""

import logging

log = logging.getLogger("frank.tools.slack_read")

SCHEMA = {
    "name": "slack_read",
    "description": "Read recent messages from a Slack channel. Use to check for updates, commands, or context.",
    "parameters": {
        "channel": {
            "type": "string",
            "description": "Channel name (e.g. 'general') or channel ID",
        },
        "limit": {
            "type": "number",
            "description": "Number of recent messages to fetch (default: 20, max: 100)",
        },
    },
    "required": ["channel"],
}


async def execute(channel: str, limit: int = 20, ctx: dict = {}) -> str:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
    import asyncio

    persona = ctx.get("persona", "")
    vault_get = ctx.get("vault_get")
    persona_config = ctx.get("persona_config", {})

    slack_cfg = persona_config.get("slack", {})
    token_key = slack_cfg.get("bot_token_key", f"slack_{persona}_bot_token")
    token = vault_get(token_key) if vault_get else ""

    if not token:
        return f"Error: no Slack token found for key '{token_key}'"

    channel_map = slack_cfg.get("channels", {})
    resolved = channel_map.get(channel, channel)
    limit = min(int(limit), 100)

    def _fetch():
        client = WebClient(token=token)
        return client.conversations_history(channel=resolved, limit=limit)

    try:
        result = await asyncio.to_thread(_fetch)
        messages = result.get("messages", [])
        if not messages:
            return f"No messages in #{channel}"

        lines = []
        for msg in reversed(messages):  # oldest first
            user = msg.get("user", msg.get("username", "unknown"))
            text = msg.get("text", "").replace("\n", " ")[:300]
            ts = msg.get("ts", "")
            lines.append(f"[{ts}] {user}: {text}")

        return "\n".join(lines)
    except SlackApiError as e:
        return f"Slack error: {e.response['error']}"
    except Exception as e:
        return f"Error reading Slack: {e}"
