#!/usr/bin/env python3
"""A robot agent for agent-hub — copy this file, edit the TOOLS, run it.

This is the whole client. It registers your robot with the hub, keeps a
heartbeat going, and answers tool calls the hub sends. Your robot then shows
up on the dashboard next to everyone else's, can be given a persona, and can
be tested from the browser without you writing any UI.

Run it:

    python robot_agent.py --hub http://hub.local:8003 --name "Rick's gripper" \\
        --owner rick --token YOUR_ENROLLMENT_TOKEN

Only `--hub` is required on an open LAN hub. The enrollment token is required
on a hub that is reachable from the internet; ask whoever runs it.

Adding a tool is two steps: write a function, add an entry to TOOLS. A tool
receives the arguments the model (or a person in the dashboard console) sent
and returns a string, which is what the model sees. Raise an exception to
report failure; the hub shows the message.

Dependencies: httpx (`pip install httpx`). Nothing else.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import platform
import random
import sys
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

# ── Your robot's tools ───────────────────────────────────────────────────────
#
# Replace these three with whatever your robot can actually do. Keep the
# descriptions concrete: they are the only thing the model reads when deciding
# whether to call a tool, so "drive forward at speed 1-10 for N seconds" beats
# "movement control".


async def drive(args: dict[str, Any]) -> str:
    """Example: move the robot. Wire this to your motor library."""
    speed = int(args.get("speed", 5))
    seconds = float(args.get("seconds", 1))
    if not 1 <= speed <= 10:
        raise ValueError("speed must be between 1 and 10")
    # your_motor_library.drive(speed=speed, seconds=seconds)
    await asyncio.sleep(min(seconds, 5))
    return f"drove at speed {speed} for {seconds}s"


async def read_distance(_args: dict[str, Any]) -> str:
    """Example: read a sensor. Wire this to your sensor library."""
    # return str(your_sensor.read_cm())
    return f"{random.randint(5, 200)} cm to the nearest obstacle"


async def status(_args: dict[str, Any]) -> str:
    """Example: report what the robot is."""
    return json.dumps({"host": platform.node(), "python": platform.python_version()})


ToolHandler = Callable[[dict[str, Any]], Awaitable[str]]

TOOLS: list[dict[str, Any]] = [
    {
        "name": "robot.drive",
        "description": "Drive the robot forward at a speed of 1-10 for a number of seconds.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "speed": {"type": "integer", "minimum": 1, "maximum": 10},
                "seconds": {"type": "number"},
            },
            "required": ["speed"],
        },
        # Tools that move something are destructive: the hub will not lend
        # them to another agent through persona linking. Read-only tools can
        # say {"readOnlyHint": True} instead.
        "annotations": {"destructiveHint": True},
        "handler": drive,
    },
    {
        "name": "robot.read_distance",
        "description": "Read the distance in centimetres to the nearest obstacle ahead.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
        "handler": read_distance,
    },
    {
        "name": "robot.status",
        "description": "Report the robot's host name and software version.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
        "handler": status,
    },
]


# ── The client. You should not need to change anything below here. ───────────


class RobotAgent:
    """Registers with the hub, heartbeats, and serves tool calls."""

    def __init__(
        self,
        hub: str,
        name: str,
        owner: str = "",
        agent_id: str = "",
        persona: str = "",
        enrollment_token: str = "",
    ) -> None:
        self.hub = hub.rstrip("/")
        self.name = name
        self.owner = owner
        self.agent_id = agent_id
        self.persona = persona
        self.enrollment_token = enrollment_token
        self.token = ""
        self.heartbeat_seconds = 30
        self.handlers: dict[str, ToolHandler] = {t["name"]: t["handler"] for t in TOOLS}
        self._client = httpx.AsyncClient(timeout=30.0)

    async def register(self) -> None:
        """Announce the robot and its tools; store the token we get back."""
        body = {
            "agent_id": self.agent_id or None,
            "label": self.name,
            "owner": self.owner,
            "kind": "mcp",
            "persona": self.persona,
            "enrollment_token": self.enrollment_token,
            "version": "robot_agent.py/1.0",
            # The handler is ours; the hub only wants the declaration.
            "tools": [{k: v for k, v in t.items() if k != "handler"} for t in TOOLS],
        }
        resp = await self._client.post(
            f"{self.hub}/agent/register", json={k: v for k, v in body.items() if v is not None}
        )
        if resp.status_code == 401:
            raise SystemExit(
                "The hub refused registration: it needs an enrollment token.\n"
                "Pass --token, or ask whoever runs the hub for it."
            )
        resp.raise_for_status()
        data = resp.json()
        self.agent_id = data["agent_id"]
        self.token = data["token"]
        self.heartbeat_seconds = int(data.get("heartbeat_interval_seconds") or 30)
        print(f"registered as {self.agent_id} with {len(TOOLS)} tools", flush=True)

    async def heartbeat_forever(self) -> None:
        """Tell the hub we are alive, so the dashboard shows us healthy."""
        while True:
            try:
                await self._client.post(
                    f"{self.hub}/agent/heartbeat",
                    json={
                        "agent_id": self.agent_id,
                        "token": self.token,
                        "activity": "idle",
                        "tools": list(self.handlers),
                    },
                )
            except Exception as exc:
                print(f"heartbeat failed: {exc}", file=sys.stderr, flush=True)
            await asyncio.sleep(self.heartbeat_seconds)

    async def serve_forever(self) -> None:
        """Read tool calls off the hub's event stream and answer them.

        The hub cannot make requests *to* a robot on someone's laptop, so the
        robot holds open a long-lived GET and the hub pushes calls down it —
        the same trick the browser page agent uses.
        """
        url = f"{self.hub}/mcp/v1/events?device_id={self.agent_id}&token={self.token}"
        while True:
            try:
                async with self._client.stream("GET", url, timeout=None) as stream:
                    if stream.status_code != 200:
                        raise RuntimeError(f"event stream returned {stream.status_code}")
                    print("connected — waiting for tool calls", flush=True)
                    async for line in stream.aiter_lines():
                        if line.startswith("data: "):
                            await self._dispatch(json.loads(line[6:]))
            except Exception as exc:
                print(f"stream dropped ({exc}); reconnecting in 3s", file=sys.stderr, flush=True)
                await asyncio.sleep(3)

    async def _dispatch(self, request: dict[str, Any]) -> None:
        """Run one tool call and post the result back."""
        if request.get("error"):
            print(f"hub says: {request['error']}", file=sys.stderr)
            return
        call_id = request.get("id")
        params = request.get("params") or {}
        name = params.get("name", "")
        args = params.get("arguments") or {}
        print(f"→ {name}({args})", flush=True)
        handler = self.handlers.get(name)
        try:
            if handler is None:
                raise KeyError(f"this robot has no tool called {name!r}")
            text = await handler(args)
            result = {"content": [{"type": "text", "text": str(text)}], "isError": False}
        except Exception as exc:
            result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
            print(f"  failed: {exc}", file=sys.stderr, flush=True)
        await self._client.post(
            f"{self.hub}/mcp/v1/respond",
            json={
                "device_id": self.agent_id,
                "token": self.token,
                "id": call_id,
                "result": result,
            },
        )

    async def goodbye(self) -> None:
        """Leave cleanly so the dashboard shows us offline straight away."""
        with contextlib.suppress(Exception):
            await self._client.post(
                f"{self.hub}/agent/goodbye",
                json={"agent_id": self.agent_id, "token": self.token},
            )
        await self._client.aclose()

    async def run(self) -> None:
        await self.register()
        tasks = [
            asyncio.create_task(self.serve_forever()),
            asyncio.create_task(self.heartbeat_forever()),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await self.goodbye()


def main() -> None:
    parser = argparse.ArgumentParser(description="Connect a robot to agent-hub.")
    parser.add_argument("--hub", required=True, help="hub base URL, e.g. http://hub.local:8003")
    parser.add_argument("--name", default=f"robot-{platform.node()}", help="label on the dashboard")
    parser.add_argument("--owner", default="", help="your name, so you can filter to your robots")
    parser.add_argument("--id", default="", help="stable agent id (defaults to a new one)")
    parser.add_argument("--persona", default="", help="persona to bind, e.g. hero-robot")
    parser.add_argument("--token", default="", help="enrollment token, if the hub needs one")
    args = parser.parse_args()

    agent = RobotAgent(
        hub=args.hub,
        name=args.name,
        owner=args.owner,
        agent_id=args.id,
        persona=args.persona,
        enrollment_token=args.token,
    )
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
