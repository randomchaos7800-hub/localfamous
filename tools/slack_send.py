"""Send a message to a Slack channel."""

import logging

log = logging.getLogger("frank.tools.slack_send")

SCHEMA = {
    "name": "slack_send",
    "description": "Send a message to a Slack channel. Use this to post updates, reports, or notifications.",
    "parameters": {
        "channel": {
            "type": "string",
            "description": "Channel name (e.g. 'general', 'ops-log') or channel ID",
        },
        "message": {
            "type": "string",
            "description": "Message text. Slack markdown supported.",
        },
        "thread_ts": {
            "type": "string",
            "description": "Thread timestamp to reply in a thread (optional)",
        },
    },
    "required": ["channel", "message"],
}


async def execute(channel: str, message: str, thread_ts: str = "", ctx: dict = {}) -> str:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
    import asyncio

    persona = ctx.get("persona", "")
    vault_get = ctx.get("vault_get")
    persona_config = ctx.get("persona_config", {})

    # Resolve token: persona config → vault
    slack_cfg = persona_config.get("slack", {})
    token_key = slack_cfg.get("bot_token_key", f"slack_{persona}_bot_token")
    token = vault_get(token_key) if vault_get else ""

    if not token:
        return f"Error: no Slack token found for key '{token_key}'"

    # Resolve channel ID if given a name
    channel_map = slack_cfg.get("channels", {})
    resolved = channel_map.get(channel, channel)
    if not resolved.startswith("C"):
        # Try to look it up by name prefix
        resolved = channel

    def _send():
        client = WebClient(token=token)
        kwargs = {"channel": resolved, "text": message}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        return client.chat_postMessage(**kwargs)

    try:
        result = await asyncio.to_thread(_send)
        ts = result.get("ts", "")
        log.info(f"Posted to #{channel} (ts={ts})")
        return f"Message sent to #{channel} (ts={ts})"
    except SlackApiError as e:
        log.error(f"Slack error: {e.response['error']}")
        return f"Slack error: {e.response['error']}"
    except Exception as e:
        return f"Error sending to Slack: {e}"
