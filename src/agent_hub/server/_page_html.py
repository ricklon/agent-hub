"""The page-agent browser page, kept separate so its long inline HTML/JS lines
do not trip the project line-length lint. Served verbatim by
``server.page_agent`` at ``/dashboard/page-agent``.

This file intentionally contains wide minified-ish CSS/JS; ruff E501 is
suppressed for it in pyproject.toml.
"""

PAGE_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>agent-hub · page agent</title>
<style>
body{font-family:monospace;background:#0d1117;color:#c9d1d9;padding:1.5rem;margin:0}
h1{color:#58a6ff;margin:0 0 .25rem}
h2{color:#58a6ff;margin:1.5rem 0 .5rem;font-size:1.1rem}
.row{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center;margin:.4rem 0}
#status{font-size:.85rem;color:#8b949e}
button{background:#238636;color:#fff;border:none;padding:.45rem .9rem;
  border-radius:4px;cursor:pointer}
button:hover{background:#2ea043}
input,textarea{background:#161b22;color:#c9d1d9;border:1px solid #30363d;
  padding:.4rem .6rem;border-radius:4px;font-family:monospace}
textarea{width:100%;box-sizing:border-box}
pre{background:#010409;border:1px solid #30363d;padding:.6rem;overflow:auto;
  max-height:18rem;border-radius:4px}
video{border:1px solid #30363d;border-radius:4px;max-width:320px}
.badge{font-size:.7rem;background:#1a2a3a;color:#79c0ff;border-radius:3px;padding:.1rem .35rem}
</style></head><body>
<h1>Page Agent</h1>
<div id="status">initialising…</div>

<h2>Camera (seeing)</h2>
<div class="row"><button id="cam">Start camera</button><span id="camstate">off</span></div>
<video id="video" autoplay playsinline muted style="display:none"></video>

<h2>Speak</h2>
<div class="row"><input id="say" value="Hello from the page agent." style="flex:1;min-width:12rem">
<button id="speak">Speak</button></div>

<h2>Discussion</h2>
<div class="row"><input id="discuss" placeholder="ask the agent — e.g. 'what do you see?'"
  style="flex:1;min-width:12rem" autofocus>
<button id="post">Send</button>
<label style="display:inline-flex;align-items:center;gap:.2rem;font-size:.8rem">
<input type="checkbox" id="speakReply" checked> speak reply</label></div>
<pre id="log" data-empty="1">dialogue will appear here…</pre>

<script>
const LS_KEY = "agenthub.pageAgent.deviceId";
let deviceId = localStorage.getItem(LS_KEY);
if (!deviceId) {
  deviceId = "page-" + crypto.randomUUID();
  localStorage.setItem(LS_KEY, deviceId);
}
let token = "", respondUrl = "", eventUrl = "", hbUrl = "", hbInterval = 30;
let volume = 1.0;
let stream = null;
let asking = false;

const TOOLS = [
  {name: "page.audio_speaker.speak", description: "Speak text aloud via SpeechSynthesis.",
    inputSchema: {type: "object", properties: {text: {type: "string"}}, required: ["text"]}},
  {name: "page.audio_speaker.set_volume", description: "Set speech volume 0..100.",
    inputSchema: {type: "object", properties: {volume: {type: "integer", minimum: 0, maximum: 100}}, required: ["volume"]}},
  {name: "page.camera.take_photo", description: "Capture one webcam frame as a JPEG data URL.",
    inputSchema: {type: "object", properties: {}}},
  {name: "page.site.get", description: "Fetch a URL from the page and return its text body. Subject to CORS.",
    inputSchema: {type: "object", properties: {url: {type: "string"}}, required: ["url"]}},
  {name: "page.agent.status", description: "Return page agent status JSON.",
    inputSchema: {type: "object", properties: {}}},
];

function setStatus(s) { document.getElementById("status").textContent = s; }

async function register() {
  const label = navigator.userAgent.includes("Mobile") ? "page-mobile" : "page";
  const resp = await fetch("/page-agent/register", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({device_id: deviceId, label: label, tools: TOOLS})
  });
  const data = await resp.json();
  if (!data.ok) { setStatus("register failed: " + JSON.stringify(data)); return; }
  token = data.token;
  respondUrl = data.mcp_respond_url;
  eventUrl = data.mcp_event_url;
  hbUrl = data.heartbeat_url;
  hbInterval = data.heartbeat_interval_seconds || 30;
  deviceId = data.device_id;
  localStorage.setItem(LS_KEY, deviceId);
  setStatus("registered " + deviceId + " · " + TOOLS.length + " tools");
  openStream();
  startHeartbeat();
  registerWebMcp();
}

function openStream() {
  const u = eventUrl + "?device_id=" + encodeURIComponent(deviceId) + "&token=" + encodeURIComponent(token);
  const es = new EventSource(u);
  es.onopen = () => setStatus("MCP stream open: " + deviceId);
  es.onerror = () => setStatus("MCP stream error (reconnecting…) — " + deviceId);
  es.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    if (msg.error) { setStatus("stream error: " + msg.error); return; }
    handleRequest(msg);
  };
}

async function handleRequest(req) {
  const id = req.id;
  const name = req.params && req.params.name;
  const args = (req.params && req.params.arguments) || {};
  let result, isError = false;
  try {
    result = await dispatch(name, args);
  } catch (e) {
    isError = true;
    result = {content: [{type: "text", text: String(e)}], isError: true};
  }
  await fetch(respondUrl, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({device_id: deviceId, token: token, id: id, result: result, isError: isError})
  });
}

function textResult(t) {
  return {content: [{type: "text", text: String(t)}], isError: false};
}
function imageResult(dataUrl, mime) {
  return {content: [{type: "image", mimeType: mime, data: dataUrl.split(",")[1] || ""}], isError: false};
}

async function dispatch(name, args) {
  switch (name) {
    case "page.audio_speaker.speak": {
      const u = new SpeechSynthesisUtterance(args.text || "");
      u.volume = volume;
      speechSynthesis.speak(u);
      return textResult("spoken: " + (args.text || ""));
    }
    case "page.audio_speaker.set_volume":
      volume = (args.volume | 0) / 100;
      return textResult("volume=" + args.volume);
    case "page.agent.status":
      return textResult(JSON.stringify({
        device_id: deviceId, connected: true,
        tools: TOOLS.map(t => t.name), volume: volume
      }));
    case "page.site.get": {
      const r = await fetch(args.url);
      const t = await r.text();
      return textResult(t.slice(0, 4000));
    }
    case "page.camera.take_photo": {
      if (!stream) { stream = await navigator.mediaDevices.getUserMedia({video: true}); }
      const v = document.getElementById("video");
      v.srcObject = stream; v.style.display = "block"; v.play();
      const c = document.createElement("canvas");
      c.width = v.videoWidth || 320;
      c.height = v.videoHeight || 240;
      c.getContext("2d").drawImage(v, 0, 0, c.width, c.height);
      const url = c.toDataURL("image/jpeg", 0.8);
      return imageResult(url, "image/jpeg");
    }
    default:
      throw new Error("unknown tool: " + name);
  }
}

function startHeartbeat() {
  setInterval(async () => {
    try {
      await fetch(hbUrl, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          device_id: deviceId, token: token,
          activity: "idle", mcp_tools: TOOLS.map(t => t.name)
        })
      });
    } catch (e) {}
  }, hbInterval * 1000);
}

// Optional WebMCP: expose the same tools to browser agents (Chrome flag/origin-trial).
function registerWebMcp() {
  const mc = document.modelContext;
  if (!mc) return;
  for (const t of TOOLS) {
    try { mc.registerTool(t.name, t.description, t.inputSchema, async (args) => dispatch(t.name, args || {})); }
    catch (e) {}
  }
  setStatus((document.getElementById("status").textContent || "") + " · webmcp native");
}

document.getElementById("speak").onclick = () =>
  dispatch("page.audio_speaker.speak", {text: document.getElementById("say").value});

async function askAgent() {
  if (asking) return;
  const input = document.getElementById("discuss");
  const text = input.value.trim();
  if (!text || !token) return;
  asking = true;
  const btn = document.getElementById("post");
  btn.disabled = true;
  btn.textContent = "…";
  const logEl = document.getElementById("log");
  if (logEl.dataset.empty) { logEl.textContent = ""; delete logEl.dataset.empty; }
  logEl.textContent += "you: " + text + "\n";
  input.value = "";
  try {
    const resp = await fetch("/page-agent/ask", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({device_id: deviceId, token: token, text: text})
    });
    const data = await resp.json();
    if (data.ok && data.reply) {
      logEl.textContent += "agent: " + data.reply + "\n";
      if (document.getElementById("speakReply").checked) {
        dispatch("page.audio_speaker.speak", {text: data.reply});
      }
    } else {
      logEl.textContent += "agent: (error) " + (data.message || "no reply") + "\n";
    }
  } catch (e) {
    logEl.textContent += "agent: (error) " + e + "\n";
  }
  logEl.scrollTop = logEl.scrollHeight;
  asking = false;
  btn.disabled = false;
  btn.textContent = "Send";
}

document.getElementById("post").onclick = askAgent;
document.getElementById("discuss").addEventListener("keydown", (e) => {
  if (e.key === "Enter") askAgent();
});
document.getElementById("cam").onclick = async () => {
  try {
    stream = await navigator.mediaDevices.getUserMedia({video: true});
    const v = document.getElementById("video");
    v.srcObject = stream; v.style.display = "block"; v.play();
    document.getElementById("camstate").textContent = "on";
  } catch (e) {
    document.getElementById("camstate").textContent = "denied";
  }
};

register();
</script>
</body></html>
"""
