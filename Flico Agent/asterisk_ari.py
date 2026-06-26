from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import aiohttp

from asterisk_rtp import AsteriskRtpTransport, RtpPortAllocator

logger = logging.getLogger(__name__)


@dataclass
class AsteriskCallResources:
    call_channel_id: str
    external_channel_id: str
    bridge_id: str
    rtp: AsteriskRtpTransport
    lang: str


class AsteriskAriClient:
    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        app_name: str,
        rtp_bind_host: str,
        rtp_advertise_host: str,
        port_allocator: RtpPortAllocator,
        session_factory: Callable[[str, str, str, AsteriskRtpTransport], Awaitable[Any]],
    ):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.app_name = app_name
        self.rtp_bind_host = rtp_bind_host
        self.rtp_advertise_host = rtp_advertise_host
        self.port_allocator = port_allocator
        self.session_factory = session_factory
        self.http: aiohttp.ClientSession | None = None
        self.resources: dict[str, AsteriskCallResources] = {}
        self.sessions: dict[str, Any] = {}
        self._cleanup_lock = asyncio.Lock()

    def snapshot(self) -> dict[str, Any]:
        calls = []
        remote_known = 0
        remote_unknown = 0

        for channel_id, res in sorted(self.resources.items()):
            remote = res.rtp.remote
            if remote:
                remote_known += 1
            else:
                remote_unknown += 1
            calls.append({
                "call_channel_id": res.call_channel_id,
                "call_id": f"sip-{res.call_channel_id}",
                "external_channel_id": res.external_channel_id,
                "bridge_id": res.bridge_id,
                "language": res.lang,
                "rtp_port": res.rtp.bind_port,
                "rtp_remote_known": remote is not None,
                "rtp_remote": {
                    "host": remote[0],
                    "port": remote[1],
                } if remote else None,
            })

        return {
            "app_name": self.app_name,
            "base_url": self.base_url,
            "active_calls": len(self.resources),
            "sessions": len(self.sessions),
            "languages": sorted({res.lang for res in self.resources.values()}),
            "rtp_ports": sorted(res.rtp.bind_port for res in self.resources.values()),
            "call_ids": [call["call_id"] for call in calls],
            "remote_rtp": {
                "known": remote_known,
                "unknown": remote_unknown,
            },
            "calls": calls,
            "allocator": self.port_allocator.snapshot(),
            "connected": self.http is not None,
        }

    async def run_forever(self) -> None:
        auth = aiohttp.BasicAuth(self.username, self.password)
        async with aiohttp.ClientSession(auth=auth) as http:
            self.http = http
            ws_url = self._ws_url()
            while True:
                try:
                    logger.info("Connecting to Asterisk ARI events: %s", ws_url)
                    async with http.ws_connect(ws_url, heartbeat=30) as ws:
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await self._handle_event(msg.json())
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Asterisk ARI event loop failed; retrying")
                await asyncio.sleep(3)

    def _ws_url(self) -> str:
        if self.base_url.startswith("https://"):
            root = self.base_url.replace("https://", "wss://", 1)
        else:
            root = self.base_url.replace("http://", "ws://", 1)
        return f"{root}/events?app={self.app_name}&api_key={self.username}:{self.password}"

    async def _handle_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        channel = event.get("channel") or {}
        channel_id = channel.get("id", "")
        name = channel.get("name", "")

        if event_type == "StasisStart":
            args = event.get("args") or []
            if name.startswith("UnicastRTP/"):
                logger.info("Ignoring External Media StasisStart: %s", channel_id)
                return
            lang = args[0] if args else "en"
            await self._start_call(channel, lang)
        elif event_type in ("StasisEnd", "ChannelDestroyed"):
            await self._cleanup(channel_id)

    async def _start_call(self, channel: dict[str, Any], lang: str) -> None:
        if not self.http:
            return
        call_channel_id = channel["id"]
        caller = ((channel.get("caller") or {}).get("number") or "unknown")
        call_id = f"sip-{call_channel_id}"
        bridge_id = f"bridge-{call_channel_id}"
        external_channel_id = f"external-{call_channel_id}"
        rtp_port = self.port_allocator.allocate()
        rtp = AsteriskRtpTransport(
            bind_host=self.rtp_bind_host,
            bind_port=rtp_port,
            on_audio=lambda payload: self._feed_audio(call_channel_id, payload),
        )
        resources_registered = False

        try:
            await rtp.start()
            session = await self.session_factory(call_id, caller, lang, rtp)
            self.sessions[call_channel_id] = session
            self.resources[call_channel_id] = AsteriskCallResources(
                call_channel_id=call_channel_id,
                external_channel_id=external_channel_id,
                bridge_id=bridge_id,
                rtp=rtp,
                lang=lang,
            )
            resources_registered = True

            await self._post(f"/channels/{call_channel_id}/answer")
            await self._post(f"/bridges", params={"type": "mixing", "bridgeId": bridge_id})
            await self._post(f"/bridges/{bridge_id}/addChannel", params={"channel": call_channel_id})
            await self._post(
                "/channels/externalMedia",
                params={
                    "channelId": external_channel_id,
                    "app": self.app_name,
                    "external_host": f"{self.rtp_advertise_host}:{rtp_port}",
                    "format": "ulaw",
                    "encapsulation": "rtp",
                    "transport": "udp",
                    "connection_type": "client",
                    "direction": "both",
                },
            )
            await self._post(f"/bridges/{bridge_id}/addChannel", params={"channel": external_channel_id})
            remote_host = await self._get_var(external_channel_id, "UNICASTRTP_LOCAL_ADDRESS")
            remote_port = await self._get_var(external_channel_id, "UNICASTRTP_LOCAL_PORT")
            if remote_host and remote_port:
                rtp.set_remote(remote_host, int(remote_port))
            await session.start()
            logger.info("Started Asterisk SIP call %s from %s lang=%s", call_id, caller, lang)
        except Exception:
            logger.exception("Failed to start Asterisk call %s", call_id)
            if resources_registered:
                await self._cleanup(call_channel_id)
            else:
                await rtp.stop()
                self.port_allocator.release(rtp_port)

    async def _feed_audio(self, call_channel_id: str, payload: bytes) -> None:
        session = self.sessions.get(call_channel_id)
        if session:
            await session.feed_audio(payload)

    async def _cleanup(self, channel_id: str) -> None:
        resource_key = channel_id
        if channel_id.startswith("external-"):
            resource_key = channel_id[len("external-"):]
        async with self._cleanup_lock:
            res = self.resources.pop(resource_key, None)
            session = self.sessions.pop(resource_key, None)
        if session:
            await session.stop()
        if res:
            await res.rtp.stop()
            self.port_allocator.release(res.rtp.bind_port)
            await self._delete(f"/channels/{res.external_channel_id}")
            await self._delete(f"/bridges/{res.bridge_id}")
            logger.info("Cleaned Asterisk SIP call resources for %s", resource_key)

    async def _post(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        assert self.http is not None
        async with self.http.post(f"{self.base_url}{path}", params=params) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"ARI POST {path} failed {resp.status}: {await resp.text()}")
            if resp.content_type == "application/json":
                return await resp.json()
            return {}

    async def _delete(self, path: str) -> None:
        if not self.http:
            return
        try:
            async with self.http.delete(f"{self.base_url}{path}") as resp:
                if resp.status not in (200, 202, 204, 404):
                    logger.warning("ARI DELETE %s returned %s: %s", path, resp.status, await resp.text())
        except Exception:
            logger.exception("ARI DELETE failed for %s", path)

    async def _get_var(self, channel_id: str, name: str) -> str | None:
        assert self.http is not None
        async with self.http.get(
            f"{self.base_url}/channels/{channel_id}/variable",
            params={"variable": name},
        ) as resp:
            if resp.status >= 400:
                logger.warning("ARI variable %s for %s failed: %s", name, channel_id, resp.status)
                return None
            data = await resp.json()
            return data.get("value")
