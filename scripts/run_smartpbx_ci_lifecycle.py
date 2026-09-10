#!/usr/bin/env python3
"""Run one generated SmartPBX service only inside a disposable CI sandbox.

This is intentionally a CI entry point, not a factory API.  It owns every
Docker resource it creates and emits only event booleans and cleanup counters.
The review-only template is rejected until a runtime integrator supplies a
real service implementation.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any


PROVENANCE = ".smartpbx-factory-provenance.json"
SAFE_COUNTERS = ("active_sessions", "active_tasks", "active_resources")
AUTH_HEADER = re.compile(r"^\\s*SMARTPBX_AUTH_HEADER_NAME:\\s*['\\\"]([^'\\\"]+)['\\\"]\\s*$", re.MULTILINE)
AUTH_CASES = (("missing", None), ("wrong", secrets.token_urlsafe(32)), ("cross-agent", secrets.token_urlsafe(32)))


class LifecycleError(RuntimeError):
    """A local lifecycle observation did not meet the CI contract."""


def command(argv: list[str], *, capture: bool = False, allow_failure: bool = False) -> str:
    result = subprocess.run(argv, check=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode and not allow_failure:
        raise LifecycleError("owned Docker operation failed")
    return result.stdout if capture else ""


def artifact_digest(agent_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(agent_dir.rglob("*")):
        if path.is_symlink():
            raise LifecycleError("generated agent contains a symlink")
        if path.is_file() and path.name != PROVENANCE:
            digest.update(path.relative_to(agent_dir).as_posix().encode("utf-8"))
            digest.update(b"\\0")
            digest.update(path.read_bytes())
            digest.update(b"\\0")
    return digest.hexdigest()


def provenance_for(agent_dir: Path) -> dict[str, str]:
    try:
        value = json.loads((agent_dir / PROVENANCE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleError("generated agent is missing usable provenance") from exc
    if not isinstance(value, dict):
        raise LifecycleError("generated provenance must be an object")
    required = ("artifact_digest", "source_revision", "template_version")
    if any(not isinstance(value.get(key), str) for key in required):
        raise LifecycleError("generated provenance is incomplete")
    actual = artifact_digest(agent_dir)
    if value["artifact_digest"] != actual:
        raise LifecycleError("generated tree does not match provenance artifact digest")
    return {key: value[key] for key in required}


def require_real_runtime(agent_dir: Path) -> None:
    runbook = agent_dir / "SMARTPBX_RUNBOOK.md"
    if runbook.is_file() and "review-only" in runbook.read_text(encoding="utf-8").lower():
        raise LifecycleError("generated runtime remains review-only and cannot receive lifecycle approval")
    for required in ("Dockerfile", "server.py", "smartpbx_gateway.py", "smartpbx_diagnostics.py", "docker-compose.yml"):
        if not (agent_dir / required).is_file():
            raise LifecycleError("generated agent is incomplete")


def authentication_header(agent_dir: Path) -> str:
    compose = (agent_dir / "docker-compose.yml").read_text(encoding="utf-8")
    match = AUTH_HEADER.search(compose)
    if not match:
        raise LifecycleError("generated compose does not declare an agent authentication header")
    return match.group(1)


def http_request(url: str, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            return response.status, response.read(16_384)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(16_384)
    except OSError as exc:
        raise LifecycleError("loopback HTTP request failed") from exc


def wait_for_health(base_url: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            status, _ = http_request(f"{base_url}/health")
            if status == 200:
                return
        except LifecycleError:
            pass
        time.sleep(0.25)
    raise LifecycleError("generated service did not become healthy on loopback")


def cleanup_counters(base_url: str, status_token: str) -> None:
    status, body = http_request(f"{base_url}/smartpbx/status", {"Authorization": f"Bearer {status_token}"})
    if status != 200:
        raise LifecycleError("authenticated status was not accepted")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise LifecycleError("authenticated status was not JSON") from exc
    if not isinstance(payload, dict):
        raise LifecycleError("authenticated status was not an object")
    for key in SAFE_COUNTERS:
        value = payload.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value != 0:
            raise LifecycleError("status did not prove zero session/task/resource cleanup")


class LoopbackWebSocket:
    def __init__(self, host: str, port: int, header: str | None, token: str | None) -> None:
        self.socket = socket.create_connection((host, port), timeout=5)
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        lines = [
            "GET /ws/v1/smartpbx/media HTTP/1.1", f"Host: {host}:{port}", "Upgrade: websocket",
            "Connection: Upgrade", f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13",
        ]
        if header and token:
            lines.append(f"{header}: {token}")
        self.socket.sendall(("\\r\\n".join(lines) + "\\r\\n\\r\\n").encode("ascii"))
        response = self._read_headers()
        if b" 101 " not in response.split(b"\\r\\n", 1)[0]:
            raise LifecycleError("WebSocket upgrade was rejected before an observable close code")

    def _read_headers(self) -> bytes:
        data = b""
        while b"\\r\\n\\r\\n" not in data and len(data) < 16_384:
            chunk = self.socket.recv(1024)
            if not chunk:
                break
            data += chunk
        return data

    def _read_exact(self, length: int) -> bytes:
        data = b""
        while len(data) < length:
            chunk = self.socket.recv(length - len(data))
            if not chunk:
                raise LifecycleError("WebSocket closed before a complete frame")
            data += chunk
        return data

    def send_event(self, event: str) -> None:
        body = json.dumps({"event": event, event: {"synthetic": True}}, separators=(",", ":")).encode("utf-8")
        mask = secrets.token_bytes(4)
        length = len(body)
        header = bytes([0x81, 0x80 | length]) if length < 126 else bytes([0x81, 0x80 | 126]) + struct.pack("!H", length)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(body))
        self.socket.sendall(header + mask + masked)

    def close_code(self) -> int:
        self.socket.settimeout(5)
        while True:
            first, second = self._read_exact(2)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            payload = self._read_exact(length) if length else b""
            if first & 0x0F == 0x8:
                return struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1005

    def close(self) -> None:
        self.socket.close()


def rejected_connection(host: str, port: int, header: str, token: str | None) -> None:
    client = LoopbackWebSocket(host, port, header if token is not None else None, token)
    try:
        if client.close_code() != 1008:
            raise LifecycleError("missing, wrong, or cross-agent authentication was not rejected")
    finally:
        client.close()


def valid_lifecycle(host: str, port: int, header: str, token: str, terminal: str) -> None:
    client = LoopbackWebSocket(host, port, header, token)
    try:
        for event in ("connected", "start", "media", terminal):
            client.send_event(event)
        if client.close_code() != 1000:
            raise LifecycleError("valid lifecycle did not close cleanly")
    finally:
        client.close()


def mapped_port(container: str) -> int:
    raw = command(["docker", "port", container, "8080/tcp"], capture=True).strip()
    host, separator, port = raw.rpartition(":")
    if not separator or host not in {"127.0.0.1", "[::1]"} or not port.isdecimal():
        raise LifecycleError("container did not receive an ephemeral loopback-only port")
    return int(port)


def inspect_image(image: str, provenance: dict[str, str]) -> None:
    raw = command(["docker", "image", "inspect", image], capture=True)
    try:
        labels = json.loads(raw)[0]["Config"]["Labels"]
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise LifecycleError("built image did not expose provenance labels") from exc
    if labels.get("org.taskforce.smartpbx.artifact") != provenance["artifact_digest"] or labels.get("org.opencontainers.image.revision") != provenance["source_revision"]:
        raise LifecycleError("built image provenance differs from the generated tree")


def main() -> int:
    parser = argparse.ArgumentParser(description="CI-only disposable SmartPBX lifecycle verifier")
    parser.add_argument("agent_dir", type=Path)
    args = parser.parse_args()
    agent_dir = args.agent_dir.resolve()
    if not agent_dir.is_dir():
        raise LifecycleError("agent directory is unavailable")
    require_real_runtime(agent_dir)
    provenance = provenance_for(agent_dir)
    header = authentication_header(agent_dir)
    run_id = uuid.uuid4().hex
    image = f"smartpbx-ci-{provenance['artifact_digest'][:16]}-{run_id[:8]}"
    container = f"smartpbx-ci-{run_id}"
    network = f"smartpbx-ci-{run_id}"
    token = secrets.token_urlsafe(32)
    status_token = secrets.token_urlsafe(32)
    try:
        command(["docker", "network", "create", "--internal", "--label", f"com.taskforce.smartpbx.lifecycle={run_id}", network])
        command([
            "docker", "build", "--label", f"org.taskforce.smartpbx.artifact={provenance['artifact_digest']}",
            "--label", f"org.opencontainers.image.revision={provenance['source_revision']}", "--tag", image, str(agent_dir),
        ])
        inspect_image(image, provenance)
        command([
            "docker", "run", "--detach", "--name", container, "--network", network,
            "--label", f"com.taskforce.smartpbx.lifecycle={run_id}", "--publish", "127.0.0.1::8080",
            "--env", f"SMARTPBX_WS_TOKEN={token}", "--env", "SMARTPBX_ACCOUNT_ID=account-synthetic",
            "--env", f"SMARTPBX_STATUS_TOKEN={status_token}", "--env", f"SMARTPBX_AUTH_HEADER_NAME={header}", image,
        ])
        port = mapped_port(container)
        base_url = f"http://127.0.0.1:{port}"
        wait_for_health(base_url)
        for _case, candidate in AUTH_CASES:
            rejected_connection("127.0.0.1", port, header, candidate)
            cleanup_counters(base_url, status_token)
        for terminal in ("stop", "hangup"):
            valid_lifecycle("127.0.0.1", port, header, token, terminal)
            cleanup_counters(base_url, status_token)
    finally:
        command(["docker", "rm", "--force", container], allow_failure=True)
        command(["docker", "image", "rm", "--force", image], allow_failure=True)
        command(["docker", "network", "rm", network], allow_failure=True)
    print("smartpbx lifecycle: verified events=5 cleanup_counters=3")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LifecycleError as exc:
        print(f"smartpbx lifecycle: blocked reason={exc}", file=sys.stderr)
        raise SystemExit(1)
