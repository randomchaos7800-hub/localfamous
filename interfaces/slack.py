"""
Frank Slack interface — Socket Mode per-persona bot.

Each persona runs as its own Socket Mode connection.
Slack threads map to persistent frank sessions via external_id.
"""

import asyncio
import logging
import sys
from pathlib import Path

from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient

FRANK_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(FRANK_ROOT))

import config as cfg
import context
import loop as loop_mod
import tools as tools_mod
from provider import make_provider
from session import Session

log = logging.getLogger("frank.slack")

MAX_SLACK_MSG = 3800  # Slack limit is 4000; leave headroom


class SlackInterface:
    def __init__(
        self,
        persona_name: str,
        persona_dir: Path,
        memory_dir: Path,
        data_dir: Path,
        frank_config: dict,
        persona_config: dict,
    ):
        self.persona_name = persona_name
        self.frank_config = frank_config
        self.persona_config = persona_config

        slack_cfg = persona_config.get("slack", {})
        app_token = cfg.vault_get(slack_cfg["app_token_key"])
        bot_token = cfg.vault_get(slack_cfg["bot_token_key"])

        if not app_token or not bot_token:
            raise RuntimeError(
                f"[{persona_name}] Missing Slack tokens — "
                f"app_token_key={slack_cfg.get('app_token_key')} "
                f"bot_token_key={slack_cfg.get('bot_token_key')}"
            )

        self.web = AsyncWebClient(token=bot_token)
        self.socket = SocketModeClient(app_token=app_token, web_client=self.web)

        self.system = context.assemble(
            persona_dir,
            memory_dir,
            operator_cfg=frank_config.get("operator_memory"),
            context_level=persona_config.get("context_level", "none"),
        )

        self.provider = make_provider(frank_config, persona_config)
        self.tool_modules = tools_mod.get_modules(persona_config.get("allowed_tools"))
        self.sessions = Session(data_dir / "sessions.db")
        self.max_turns = frank_config.get("loop", {}).get("max_turns", 30)

        # Channel names from config — resolved to IDs at startup
        self._listen_channel_names: list[str] = slack_cfg.get("listen_channels", [])
        self.listen_channel_ids: set[str] = set()
        self.bot_user_id: str | None = None

    async def _resolve_bot_id(self) -> None:
        resp = await self.web.auth_test()
        self.bot_user_id = resp.get("user_id")
        log.info(f"[{self.persona_name}] connected as {resp.get('user')} ({self.bot_user_id})")

    async def _resolve_channel_ids(self) -> None:
        """Resolve configured channel names to IDs."""
        if not self._listen_channel_names:
            return
        try:
            cursor = None
            name_map: dict[str, str] = {}
            while True:
                kwargs: dict = {"limit": 200, "exclude_archived": True}
                if cursor:
                    kwargs["cursor"] = cursor
                resp = await self.web.conversations_list(**kwargs)
                for ch in resp.get("channels", []):
                    name_map[ch["name"]] = ch["id"]
                cursor = resp.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break
            for entry in self._listen_channel_names:
                if entry.startswith("C") and entry.isupper():
                    # Already a channel ID
                    self.listen_channel_ids.add(entry)
                else:
                    ch_id = name_map.get(entry)
                    if ch_id:
                        self.listen_channel_ids.add(ch_id)
                    else:
                        log.warning(f"[{self.persona_name}] Channel #{entry} not found — bot may not be invited")
            log.info(f"[{self.persona_name}] listening on channel IDs: {self.listen_channel_ids}")
        except Exception as e:
            log.error(f"[{self.persona_name}] Failed to resolve channel IDs: {e}")

    async def run(self) -> None:
        await self._resolve_bot_id()
        await self._resolve_channel_ids()
        self.socket.socket_mode_request_listeners.append(self._handle_request)
        await self.socket.connect()
        log.info(f"[{self.persona_name}] Socket Mode connected")
        await asyncio.sleep(float("inf"))

    async def _handle_request(self, client: SocketModeClient, req: SocketModeRequest) -> None:
        await client.send_socket_mode_response(
            SocketModeResponse(envelope_id=req.envelope_id)
        )

        if req.type != "events_api":
            return

        event = req.payload.get("event", {})
        event_type = event.get("type")

        if event_type not in ("message", "app_mention"):
            return
        if event.get("subtype"):
            return
        if event.get("bot_id"):
            return
        if event.get("user") == self.bot_user_id:
            return

        channel = event.get("channel", "")
        ts = event.get("ts", "")
        thread_ts = event.get("thread_ts") or ts
        text: str = event.get("text", "").strip()

        # For message events, only respond in watched channels
        if event_type == "message" and self.listen_channel_ids:
            if channel not in self.listen_channel_ids:
                return

        # Strip @mention prefix
        if self.bot_user_id and text.startswith(f"<@{self.bot_user_id}>"):
            text = text[len(f"<@{self.bot_user_id}>"):].strip()

        if not text:
            return

        asyncio.create_task(self._respond(channel, ts, thread_ts, text, event))

    async def _respond(
        self,
        channel: str,
        ts: str,
        thread_ts: str,
        text: str,
        event: dict,
    ) -> None:
        log.info(f"[{self.persona_name}] handling message in {channel} thread={thread_ts}: {text[:120]}")
        placeholder = None
        try:
            result = await self.web.chat_postMessage(
                channel=channel,
                text="...",
            )
            placeholder = result["ts"]
        except Exception as e:
            log.error(f"[{self.persona_name}] Failed to post placeholder: {e}")
            return

        external_id = f"{channel}:{thread_ts}"
        session_id = self.sessions.get_or_create(self.persona_name, external_id)
        history = self.sessions.get_history(session_id)

        ctx = {
            "persona": self.persona_name,
            "vault_get": cfg.vault_get,
            "frank_config": self.frank_config,
            "persona_config": self.persona_config,
            "non_interactive": True,
            "frank_root": str(FRANK_ROOT),
        }

        try:
            response_text, new_msgs = await loop_mod.run(
                prompt=text,
                provider=self.provider,
                tool_modules=self.tool_modules,
                system=self.system,
                history=history,
                max_turns=self.max_turns,
                ctx=ctx,
            )
            self.sessions.add_messages_bulk(session_id, new_msgs)
        except Exception as e:
            log.exception(f"[{self.persona_name}] Loop error: {e}")
            response_text = f"Error: {e}"

        await self._post_response(channel, thread_ts, placeholder, response_text)

    async def _post_response(
        self,
        channel: str,
        thread_ts: str,
        placeholder_ts: str,
        text: str,
    ) -> None:
        chunks = _split(text, MAX_SLACK_MSG)
        log.debug(f"[{self.persona_name}] response {len(text)} chars → {len(chunks)} chunk(s)")
        try:
            # Delete placeholder, then post response — avoids chat.update length quirks
            await self.web.chat_delete(channel=channel, ts=placeholder_ts)
        except Exception:
            pass  # best effort
        try:
            for chunk in chunks:
                await self.web.chat_postMessage(channel=channel, text=chunk)
        except Exception as e:
            log.error(f"[{self.persona_name}] Failed to post response: {e}")


def _split(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks
