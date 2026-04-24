"""Send a message via Telegram."""

import logging

log = logging.getLogger("frank.tools.telegram_send")

SCHEMA = {
    "name": "telegram_send",
    "description": "Send a message to a Telegram chat. Use for personal notifications or Sabrina grocery updates.",
    "parameters": {
        "chat_id": {
            "type": "string",
            "description": "Telegram chat ID (numeric) or use 'default' for the persona's configured chat",
        },
        "message": {
            "type": "string",
            "description": "Message text. Markdown supported.",
        },
        "parse_mode": {
            "type": "string",
            "description": "Parse mode: 'Markdown' or 'HTML' (default: Markdown)",
        },
    },
    "required": ["message"],
}


async def execute(message: str, chat_id: str = "default", parse_mode: str = "Markdown", ctx: dict = {}) -> str:
    import asyncio
    import requests

    persona = ctx.get("persona", "")
    vault_get = ctx.get("vault_get")
    persona_config = ctx.get("persona_config", {})

    tg_cfg = persona_config.get("telegram", {})
    token_key = tg_cfg.get("bot_token_key", f"telegram_bot_token_{persona}")
    token = vault_get(token_key) if vault_get else ""

    if not token:
        return f"Error: no Telegram token found for key '{token_key}'"

    if chat_id == "default":
        chat_id = str(tg_cfg.get("default_chat_id", ""))
    if not chat_id:
        return "Error: no chat_id provided and no default configured"

    def _send():
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": parse_mode,
        }
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        return r.json()

    try:
        result = await asyncio.to_thread(_send)
        msg_id = result.get("result", {}).get("message_id", "?")
        return f"Telegram message sent (id={msg_id})"
    except Exception as e:
        return f"Error sending Telegram message: {e}"
