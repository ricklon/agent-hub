# Robot build night

How to bring your own robot to an agent-hub and drive it. Written for the
person holding the robot, not the person running the hub. If you are running
the hub, the setup notes are at the bottom.

## What you get

Your robot joins the hub as an agent. That means:

- it appears on the dashboard next to everyone else's, with your name on it
- you can call any of its tools by hand from the browser, no code
- you can give it a **persona** — a model, a voice, a personality — and talk
  to it in plain language, and it will decide which of your tools to call
- everything it costs is metered against your agent

You need Python and about twenty lines of your own code.

## 1. Get the client

Copy [`examples/robot_agent.py`](../examples/robot_agent.py) onto whatever
runs your robot — a Pi, a laptop, anything with Python 3.10+ and network
access to the hub.

```bash
pip install httpx
```

## 2. Describe what your robot can do

Open the file. The top half is three example tools; replace them with yours.
A tool is a function plus an entry in `TOOLS`:

```python
async def open_gripper(args: dict) -> str:
    my_servo.angle = int(args.get("angle", 90))
    return f"gripper at {args.get('angle', 90)} degrees"

TOOLS = [
    {
        "name": "robot.open_gripper",
        "description": "Open the gripper to an angle between 0 (closed) and 180 (wide).",
        "inputSchema": {
            "type": "object",
            "properties": {"angle": {"type": "integer", "minimum": 0, "maximum": 180}},
            "required": ["angle"],
        },
        "annotations": {"destructiveHint": True},
        "handler": open_gripper,
    },
]
```

Three things matter more than they look:

- **The description is a prompt.** It is the only thing the model reads when
  deciding whether to call your tool. "Open the gripper to an angle between 0
  and 180" gets called correctly; "gripper control" gets called at random.
- **Return a string.** Whatever you return is what the model sees next. Say
  what actually happened, including the values you used.
- **Mark anything that moves** with `"annotations": {"destructiveHint": True}`.
  The hub refuses to lend destructive tools to *other* people's agents. Tools
  that only read can say `{"readOnlyHint": True}`.

Raise an exception to report failure; the message reaches the model and the
dashboard.

## 3. Plug in

```bash
python robot_agent.py --hub http://HUB_ADDRESS:8003 --owner your-name --name "your robot"
```

Add `--token TOKEN` if the hub asks for one (any hub on the public internet
will). The script prints:

```
registered as mcp-3f9c2b81a04e with 3 tools
connected — waiting for tool calls
```

Pass `--id my-robot` to keep the same identity across restarts, so your
history and settings survive a reboot. Without it you get a fresh agent each
run — fine for a first try, annoying by the third.

Leave it running. It reconnects on its own if the wifi drops.

## 4. Drive it from the dashboard

Open the dashboard, find your robot (filter by your name with the **Show:**
chips), and click it.

- **Tool console** — every tool you declared, each with a box for JSON
  arguments and a Call button. No model, no cost, no guessing: this answers
  "did I wire that servo up right?" in one click.
- **Ask this agent** — type a sentence. The hub runs a full turn with your
  robot's persona: the model reads your tool descriptions, calls the ones it
  needs, and answers from what they returned. This costs one model call.
- **Persona** — assign one to change how it talks and which model it uses.
  Build your own on the Personas page, then come back and assign it.

If a tool misbehaves, fix your Python, restart the script, and reload the
page. Your agent keeps its id, persona, owner and history.

## 5. Give it a personality (optional)

On the **Personas** page, make a new persona, pick a starter prompt, choose a
voice, and save. Then either assign it on your robot's page, or pass
`--persona your-persona-name` when you start the script.

Tools from *other* people's robots can be borrowed by your persona too, if
their owner marked them safe. That is the **Linked agents** section of the
persona editor.

## Troubleshooting

| What you see | What it means |
| --- | --- |
| `it needs an enrollment token` | The hub is not open to anyone; ask for the token and pass `--token`. |
| `stream dropped; reconnecting` | Network hiccup. It retries by itself; if it repeats, check you can reach the hub address. |
| Robot shows **not connected** on the dashboard | The script is not running, or it cannot reach the hub. Its own terminal says which. |
| `does not expose a tool called …` | The name in the console does not match a `name` in your `TOOLS`. |
| The model answers without calling your tool | Your description is too vague. Say what the tool does and when to use it, in plain words. |
| The model calls the tool with junk arguments | It gets one chance to correct itself automatically. If it keeps happening, tighten `inputSchema` and mention the units in the description. |

## For whoever runs the hub

Robots register on the **device** port (`http_port`, 8003 by default), not
the dashboard port — a headless robot cannot get through Cloudflare Access.
Behind Caddy, `/agent/*` and `/mcp/v1/*` are proxied to that port.

- **Set `server.enrollment_token`** on any hub reachable from the internet.
  Without it, registration is open to anyone who can reach the port, which is
  the right default only on a trusted LAN. Hand the token out at the start of
  the night.
- **Ownership is a label, not a permission.** Anyone with dashboard access
  can drive anyone's robot. That is deliberate for a room where everyone is
  building together; it is not multi-tenancy.
- **Cleanup**: robots idle for 14 days are offered for removal on the
  dashboard home; browser page agents go after 24 hours. Pin anything
  permanent with **Keep as long-term agent**.
- A robot that reboots and re-registers with the same `--id` keeps its row,
  persona and history, and gets a fresh token. An old token stops working at
  that moment, which is what you want if someone copied it.
