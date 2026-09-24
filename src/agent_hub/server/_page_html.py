"""The page-agent browser page, kept separate so its long inline HTML/JS lines
do not trip the project line-length lint. Served verbatim by
``server.page_agent`` at ``/dashboard/page-agent``.

This file intentionally contains wide minified-ish CSS/JS; ruff E501 is
suppressed for it in pyproject.toml.
"""

from agent_hub.dashboard.styles import WORKSPACE_CSS

PAGE_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>agent-hub · page agent</title>
<link rel="icon" href="data:,">
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
/* Voice state. The old UI was a small grey span that named the state but
   never said what to do about it, so "listening" and "ignored you" looked
   identical. Dot + label + instruction, sized to be readable at a glance. */
#voicestate{display:flex;align-items:center;gap:.5rem;padding:.45rem .7rem;
  border:1px solid #30363d;border-radius:6px;background:#161b22;min-width:20rem}
#voicedot{width:.6rem;height:.6rem;border-radius:50%;background:#6e7681;flex:none}
#voicelabel{font-weight:bold;font-size:.9rem}
#voicehint{font-size:.8rem;color:#8b949e}
/* Only pulses when the microphone is actually open, so "is it hearing me?"
   is answerable without reading anything. */
.voice-live #voicedot{animation:voicepulse 1.4s ease-in-out infinite}
@keyframes voicepulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.35;transform:scale(.8)}}
/* Animate input level between capture callbacks for a steady meter. */
#meter{width:12rem;height:.6rem;background:#010409;border:1px solid #30363d;
  border-radius:3px;overflow:hidden;flex:none}
#meterbar{height:100%;width:0%;background:#3fb950;transition:width .08s linear}
#meterlabel{font-size:.75rem;color:#8b949e}
[hidden]{display:none!important}
/* Choosing which agent this tab is. Shown until the tab has registered. */
#connect{border:1px solid #30363d;border-radius:6px;background:#161b22;padding:.8rem 1rem;
  margin:.8rem 0;max-width:40rem}
#connect p{margin:.2rem 0 .5rem;font-size:.8rem;color:#8b949e}
#connecterr{font-size:.85rem;color:#f85149;margin:.4rem 0}
#myagents{list-style:none;padding:0;margin:.6rem 0 0}
#myagents li{display:flex;gap:.6rem;align-items:center;padding:.25rem 0;font-size:.85rem}
#myagents .meta{color:#8b949e;font-size:.75rem}
button.secondary{background:#21262d;border:1px solid #30363d}
button.secondary:hover{background:#30363d}
</style></head><body>
<h1>Browser agent</h1>
<p>A member of your fleet, running in this tab.</p>
<div class="row"><a href="/dashboard/" style="color:#58a6ff">← Dashboard</a></div>
<div id="status">initialising…</div>
<div id="voice-notice" role="status" aria-live="polite" style="color:#fbbf24"></div>
<div id="personaline" style="font-size:.8rem;color:#8b949e"></div>
<div id="agentline" class="row" hidden><span id="agentname"></span>
<button id="newconversation" class="secondary"
  title="End the current conversation; the next thing you say starts a new one">New conversation</button>
<button id="switchagent" class="secondary" title="Close this agent and pick another">Switch agent</button></div>

<form id="connect" hidden>
  <strong>Which agent is this tab?</strong>
  <p>Pick a name. Opening the same name again, in any tab on any day, is the same agent with its history.</p>
  <div class="row"><label for="agentName">Name</label>
  <input id="agentName" maxlength="64" required autocomplete="off" data-1p-ignore data-lpignore="true" style="flex:1;min-width:12rem">
  <button id="openagent" type="submit">Open</button></div>
  <div id="connecterr" role="alert" hidden></div>
  <button id="takeover" type="button" class="secondary" hidden>Take over</button>
  <ul id="myagents"></ul>
</form>

<div id="agentui" hidden>
<section class="workspace-panel">
<h2>Discussion</h2>
<div class="row"><input aria-label="Message to agent" id="discuss" placeholder="ask the agent — e.g. 'what do you see?'"
  style="flex:1;min-width:12rem">
<button id="post">Send</button>
<label style="display:inline-flex;align-items:center;gap:.3rem;font-size:.8rem">
Text reply voice: <select id="voiceMode" title="How replies and page.audio_speaker.speak are voiced">
  <option value="hub" selected>persona voice (hub TTS)</option>
  <option value="browser">browser built-in</option>
  <option value="off">silent</option>
</select></label></div>
<div id="log" data-empty="1" style="background:#010409;border:1px solid #30363d;padding:.6rem;overflow:auto;max-height:24rem;border-radius:4px;white-space:pre-wrap;font-family:monospace;color:#c9d1d9">dialogue will appear here…</div>

</section>
<section class="workspace-panel">
<h2>Talk to your agent</h2>
<p style="color:#94a3b8">Hands-free replies use the persona voice on this browser’s speaker.</p>
<div class="row">
<button id="listen">Listen</button>
<label style="display:inline-flex;align-items:center;gap:.2rem;font-size:.8rem">
Wake word: <input id="wakeWord" value="computer" style="width:8rem"></label>
<span style="font-size:.75rem;color:#8b949e">a name with a model installed
  (e.g. computer) is detected by sound; clear it for open mic</span>
</div>
<div class="row"><div id="voicestate" role="status" aria-live="polite">
  <span id="voicedot"></span>
  <span id="voicelabel">off</span>
  <span id="voicehint">press Listen to start</span>
</div>
<div id="meter" title="microphone input level"><div id="meterbar"></div></div>
<span id="meterlabel">mic</span></div>

</section>
<details class="workspace-panel"><summary>Camera and speaker controls</summary>
<h2>Camera (seeing)</h2>
<div class="row"><button id="cam">Start camera</button><span id="camstate">off</span></div>
<video id="video" autoplay playsinline muted style="display:none"></video>

<h2>Speak</h2>
<div class="row"><input aria-label="Text to speak" id="say" value="Hello from the page agent." style="flex:1;min-width:12rem">
<button id="speak">Speak</button></div>

</details>
</div>
<script>
// The hub derives the agent id from who is signed in and the name picked
// here, so the page never makes one up. Two storage keys, two jobs:
// - TAB_NAME_KEY (sessionStorage): the agent this tab has open, so a reload
//   reopens it. Per tab, so two tabs can run two agents side by side.
// - LAST_NAME_KEY (localStorage): a convenience to prefill the name field.
// Storage can be unavailable (private windows, blocked site data); the page
// then just asks for the name again.
const TAB_NAME_KEY = "agenthub.pageAgent.name";
const LAST_NAME_KEY = "agenthub.pageAgent.lastName";
function storeGet(store, key) { try { return store.getItem(key) || ""; } catch (e) { return ""; } }
function storeSet(store, key, value) { try { store.setItem(key, value); } catch (e) {} }
function storeDel(store, key) { try { store.removeItem(key); } catch (e) {} }
let deviceId = "";
let agentName = "";
let es = null, hbTimer = null;
let token = "", respondUrl = "", eventUrl = "", hbUrl = "", hbInterval = 30;
let volume = 1.0;
let hubAudio = null;
let stream = null;
let asking = false;
// What the heartbeat reports; the dashboard shows it next to health.
let activity = "idle";
// Persona to register with, injected from ?persona= by the server ("" = default).
const PERSONA = %%PERSONA%%;
if (PERSONA) document.getElementById("personaline").textContent = "persona: " + PERSONA;
// Camera and microphone are only available in secure contexts. Say so up
// front instead of letting every getUserMedia call fail with "denied".
const MEDIA_OK = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
if (!MEDIA_OK) {
  const warn = document.createElement("div");
  warn.id = "insecure";
  warn.style.cssText = "font-size:.8rem;color:#d29922;margin:.3rem 0";
  warn.textContent = "camera and microphone need https or localhost — text chat still works";
  document.getElementById("personaline").after(warn);
}

const TOOLS = [
  {name: "page.audio_speaker.speak", description: "Speak text aloud on this page with its selected voice (persona TTS or browser built-in).",
    inputSchema: {type: "object", properties: {text: {type: "string"}, voice_mode: {type: "string", enum: ["hub", "browser", "off"]}}, required: ["text"]}},
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

// Returns {ok, status, message}. Only a successful registration starts the
// stream and heartbeat, so a refused one can be retried without leaking them.
async function register(name, takeover) {
  let resp, data;
  try {
    resp = await fetch("/page-agent/register", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name: name, takeover: !!takeover, tools: TOOLS, persona: PERSONA})
    });
    data = await resp.json();
  } catch (e) {
    return {ok: false, status: 0, message: "could not reach the hub: " + e};
  }
  if (!data.ok) return {ok: false, status: resp.status, message: data.message || ("HTTP " + resp.status)};
  token = data.token;
  respondUrl = data.mcp_respond_url;
  eventUrl = data.mcp_event_url;
  hbUrl = data.heartbeat_url;
  hbInterval = data.heartbeat_interval_seconds || 30;
  deviceId = data.device_id;
  agentName = data.name || name;
  storeSet(sessionStorage, TAB_NAME_KEY, agentName);
  storeSet(localStorage, LAST_NAME_KEY, agentName);
  document.title = agentName + " · page agent";
  document.getElementById("agentname").textContent = "agent: " + agentName;
  document.getElementById("connect").hidden = true;
  document.getElementById("agentline").hidden = false;
  document.getElementById("agentui").hidden = false;
  setStatus("registered " + agentName + " (" + deviceId + ") · " + TOOLS.length + " tools");
  openStream();
  startHeartbeat();
  registerWebMcp();
  return {ok: true};
}

function showConnectError(message, canTakeOver) {
  const err = document.getElementById("connecterr");
  err.textContent = message;
  err.hidden = !message;
  document.getElementById("takeover").hidden = !canTakeOver;
}

async function openAgent(takeover) {
  const name = document.getElementById("agentName").value.trim();
  if (!name) return;
  const btn = document.getElementById("openagent");
  btn.disabled = true;
  showConnectError("", false);
  const result = await register(name, takeover);
  btn.disabled = false;
  if (result.ok) return;
  // 409 covers "open in another tab" (which the owner may take over) and
  // "belongs to someone else" (which nobody may).
  document.getElementById("takeover").textContent = "Take over";
  showConnectError(result.message, result.status === 409 && /already open/.test(result.message));
}

function ago(iso) {
  if (!iso) return "";
  const secs = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  if (secs < 90) return "just now";
  if (secs < 5400) return Math.round(secs / 60) + " min ago";
  if (secs < 129600) return Math.round(secs / 3600) + " h ago";
  return Math.round(secs / 86400) + " days ago";
}

async function loadMyAgents() {
  const list = document.getElementById("myagents");
  let data;
  try {
    data = await (await fetch("/page-agent/mine")).json();
  } catch (e) { return; }
  if (!data.ok) { showConnectError(data.message || "could not list your agents", false); return; }
  list.replaceChildren();
  for (const a of data.agents) {
    const li = document.createElement("li");
    const pick = document.createElement("button");
    pick.type = "button";
    pick.className = "secondary";
    pick.textContent = a.name;
    pick.onclick = () => { document.getElementById("agentName").value = a.name; openAgent(false); };
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = [a.persona, a.open ? "open in another tab" : "last seen " + ago(a.last_seen)]
      .filter(Boolean).join(" · ");
    li.append(pick, meta);
    list.appendChild(li);
  }
}

document.getElementById("connect").addEventListener("submit", (ev) => { ev.preventDefault(); openAgent(false); });
document.getElementById("takeover").onclick = () => openAgent(true);
document.getElementById("newconversation").onclick = async () => {
  if (!token) return;
  const btn = document.getElementById("newconversation");
  btn.disabled = true;
  try {
    const resp = await fetch("/page-agent/conversation/new", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({device_id: deviceId, token: token})
    });
    const data = await resp.json();
    voiceLog(data.ok ? "new conversation started" : "could not start one: " + (data.message || resp.status),
      data.ok ? "#3fb950" : "#f85149");
    if (data.ok) {
      const logEl = document.getElementById("log");
      logEl.textContent = "dialogue will appear here…";
      logEl.dataset.empty = "1";
    }
  } catch (e) {
    voiceLog("could not start one: " + e, "#f85149");
  }
  btn.disabled = false;
};

document.getElementById("switchagent").onclick = () => {
  storeDel(sessionStorage, TAB_NAME_KEY);
  // Reloading closes the stream, voice socket, and timers in one go; the
  // pagehide beacon marks this agent offline on the way out.
  location.reload();
};

async function start() {
  const tabName = storeGet(sessionStorage, TAB_NAME_KEY);
  // Launch links from the dashboard name the agent (?name=), so the tab opens
  // it straight away. Not a takeover: if it is already open elsewhere, the
  // form says so and offers Take over.
  const launchName = (new URLSearchParams(location.search).get("name") || "").trim();
  if (launchName && launchName !== tabName) {
    document.getElementById("agentName").value = launchName;
    document.getElementById("connect").hidden = false;
    setStatus("opening " + launchName + "…");
    await openAgent(false);
    if (token) return;
    setStatus("choose an agent to open");
    loadMyAgents();
    return;
  }
  // A tab reopening its own agent after a reload: the stream it is replacing
  // was this tab's, so taking it over is always right.
  if (tabName) {
    setStatus("reopening " + tabName + "…");
    const result = await register(tabName, true);
    if (result.ok) return;
    storeDel(sessionStorage, TAB_NAME_KEY);
    showConnectError(result.message, false);
  }
  document.getElementById("agentName").value = tabName || storeGet(localStorage, LAST_NAME_KEY) || PERSONA || "";
  document.getElementById("connect").hidden = false;
  setStatus("choose an agent to open");
  document.getElementById("agentName").focus();
  loadMyAgents();
}

// Tell the hub the page is going away so the dashboard shows it offline now
// rather than after the heartbeat timeout. sendBeacon survives tab close.
window.addEventListener("pagehide", () => {
  if (!token) return;
  const body = new Blob([JSON.stringify({device_id: deviceId, token: token})],
    {type: "application/json"});
  navigator.sendBeacon("/page-agent/goodbye", body);
});

function openStream() {
  const u = eventUrl + "?device_id=" + encodeURIComponent(deviceId) + "&token=" + encodeURIComponent(token);
  es = new EventSource(u);
  es.onopen = () => setStatus("MCP stream open: " + deviceId);
  es.onerror = () => setStatus("MCP stream error (reconnecting…) — " + deviceId);
  es.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    // The hub ends the stream when this tab no longer holds the agent.
    if (msg.error === "replaced") { agentLost(agentName + " was opened in another tab."); return; }
    if (msg.error === "unregistered") { agentLost(agentName + " was closed by the hub."); return; }
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

// ── Voicing ─────────────────────────────────────────────────────────────
// "hub" speaks with the persona's TTS system and voice (what a device would
// sound like); "browser" uses SpeechSynthesis; "off" is silent. The MCP
// speak tool honours the same choice, so other agents driving this page
// sound consistent with it.
function voiceMode() { return document.getElementById("voiceMode").value; }
document.getElementById("voiceMode").addEventListener("change", () => {
  if (voiceWs && voiceWs.readyState === WebSocket.OPEN) {
    voiceWs.send(JSON.stringify({type: "voice_mode", mode: voiceMode()}));
  }
});

function speakBuiltin(text) {
  const u = new SpeechSynthesisUtterance(text || "");
  u.volume = volume;
  speechSynthesis.speak(u);
}

// Split a reply the way the hub's voice session does (a run of .!? then
// optional closing quotes/brackets, then space or the end), so a long reply
// starts speaking after its first sentence rather than after all of it.
// Very short pieces ("Sure.") ride with the next: a request each costs more
// than the audio they carry.
function speechChunks(text) {
  const parts = [];
  const re = /([.!?]+)(["')\\]]*)(\\s+|$)/g;
  let start = 0, m;
  while ((m = re.exec(text)) !== null) {
    const end = m.index + m[0].length;
    const piece = text.slice(start, end).trim();
    if (piece) parts.push(piece);
    start = end;
    if (end >= text.length) break;
  }
  const rest = text.slice(start).trim();
  if (rest) parts.push(rest);
  const merged = [];
  for (const part of parts) {
    if (merged.length && merged[merged.length - 1].length < 24) merged[merged.length - 1] += " " + part;
    else merged.push(part);
  }
  return merged;
}

async function fetchSpeech(text) {
  const resp = await fetch("/page-agent/tts", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({device_id: deviceId, token: token, text: text})
  });
  if (!resp.ok) throw new Error("hub TTS " + resp.status);
  document.getElementById("voice-notice").textContent = resp.headers.get("X-Voice-Notice") || "";
  return URL.createObjectURL(await resp.blob());
}

// Speak with the persona voice one sentence at a time, fetching the next
// sentence while the current one plays. Resolves once the first sentence
// starts playing (so a speak tool call returns in seconds, not after the
// whole reply); later failures stop the rest and are logged.
async function speakHub(text) {
  if (hubAudio) throw new Error("The agent is already speaking");
  const parts = speechChunks(text);
  if (!parts.length) return;
  const speech = {audio: null, stopped: false, endClip: null,
    stop() { this.stopped = true; if (this.audio) this.audio.pause(); if (this.endClip) this.endClip(); }};
  hubAudio = speech;
  const wasReplying = replyPlaying;
  replyPlaying = true;
  if (!wasReplying) setActivity("speaking");
  let started = false;
  let signal;
  const firstStart = new Promise((resolve, reject) => { signal = {resolve, reject}; });
  const playClip = url => new Promise((resolve, reject) => {
    const audio = new Audio(url);
    audio.volume = volume;
    speech.audio = audio;
    const end = () => { speech.endClip = null; URL.revokeObjectURL(url); resolve(); };
    speech.endClip = end;
    audio.addEventListener("ended", end, {once: true});
    audio.addEventListener("error", end, {once: true});
    audio.play().then(
      () => { started = true; signal.resolve(); },
      error => { speech.endClip = null; URL.revokeObjectURL(url); reject(error); });
  });
  (async () => {
    let next = fetchSpeech(parts[0]);
    try {
      for (let i = 0; i < parts.length && !speech.stopped; i++) {
        const url = await next;
        next = i + 1 < parts.length ? fetchSpeech(parts[i + 1]) : null;
        if (next) next.catch(() => {});  // awaited next round; silenced if we stop first
        if (speech.stopped) { URL.revokeObjectURL(url); break; }
        await playClip(url);
      }
      signal.resolve();
    } catch (error) {
      signal.reject(error);
      if (started) voiceLog("speech stopped part-way: " + error, "#d29922");
    } finally {
      if (hubAudio === speech) {
        hubAudio = null;
        replyPlaying = wasReplying;
        if (!wasReplying) setActivity(listening ? "listening" : "idle");
      }
    }
  })();
  return firstStart;
}

async function speak(text, requestedMode) {
  const mode = requestedMode || voiceMode();
  if (mode === "off" || !text) return "silent";
  if (mode === "browser") { speakBuiltin(text); return "browser"; }
  try { await speakHub(text); return "hub"; }
  catch (e) {
    const message = "Persona voice unavailable: " + e + ". Check audio permissions or the provider; choose browser built-in explicitly to use a different voice.";
    document.getElementById("voice-notice").textContent = message;
    voiceLog(message, "#d29922");
    throw new Error(message);
  }
}

async function dispatch(name, args) {
  switch (name) {
    case "page.audio_speaker.speak": {
      const how = await speak(args.text || "", args.voice_mode);
      return textResult("spoken via " + how + ": " + (args.text || ""));
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
      if (!MEDIA_OK) throw new Error("camera unavailable: page needs https or localhost");
      if (!stream) { stream = await navigator.mediaDevices.getUserMedia({video: true}); }
      const v = document.getElementById("video");
      v.srcObject = stream; v.style.display = "block"; v.play();
      // Wait for the video to have a real frame ready to draw.
      if (v.readyState < 2) {
        await new Promise((resolve) => v.addEventListener("loadeddata", resolve, {once: true}));
      }
      await new Promise((r) => setTimeout(r, 200));
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

async function sendHeartbeat() {
  try {
    const resp = await fetch(hbUrl, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        device_id: deviceId, token: token,
        activity: activity, mcp_tools: TOOLS.map(t => t.name)
      })
    });
    if (resp.status === 401) agentLost("This tab lost " + agentName + " (it was opened elsewhere).");
  } catch (e) {}
}

function startHeartbeat() {
  sendHeartbeat();
  hbTimer = setInterval(sendHeartbeat, hbInterval * 1000);
}

// This tab no longer holds its agent (another tab took it over, or the hub
// dropped it). Stop everything that uses the dead token and say so, rather
// than leaving a page that looks connected and silently does nothing.
function agentLost(reason) {
  if (!token) return;
  token = "";
  if (es) { es.close(); es = null; }
  if (hbTimer) { clearInterval(hbTimer); hbTimer = null; }
  if (listening) stopListening();
  storeDel(sessionStorage, TAB_NAME_KEY);
  document.title = "agent-hub · page agent";
  document.getElementById("agentui").hidden = true;
  document.getElementById("agentline").hidden = true;
  document.getElementById("connect").hidden = false;
  document.getElementById("agentName").value = agentName;
  document.getElementById("takeover").textContent = "Reopen here";
  showConnectError(reason, true);
  setStatus("not connected");
  loadMyAgents();
}

// Activity changes are pushed straight away so the dashboard does not wait a
// whole heartbeat interval to notice the page started listening.
function setActivity(a) {
  if (a === activity) return;
  activity = a;
  if (token) sendHeartbeat();
}

// Optional WebMCP: expose the same tools to browser agents (Chrome flag/origin-trial).
// Chrome's WebMCP takes one tool object per call and returns a promise. The
// old four-argument call was rejected asynchronously, so try/catch never saw it
// and every load logged "not of type 'ModelContextTool'".
async function registerWebMcp() {
  const mc = document.modelContext || navigator.modelContext;
  if (!mc || typeof mc.registerTool !== "function") return;
  let registered = 0;
  for (const t of TOOLS) {
    try {
      await mc.registerTool({
        name: t.name,
        description: t.description,
        inputSchema: t.inputSchema,
        execute: async (args) => dispatch(t.name, args || {}),
      });
      registered++;
    } catch (e) {
      console.warn("WebMCP registerTool failed for " + t.name + ": " + e);
    }
  }
  if (registered) {
    setStatus((document.getElementById("status").textContent || "") + " · webmcp native");
  }
}

document.getElementById("speak").onclick = () => speak(document.getElementById("say").value).catch(() => {});

async function askAgent() {
  if (asking) return;
  const input = document.getElementById("discuss");
  const text = input.value.trim();
  if (!text || !token) return;
  asking = true;
  setActivity("thinking");
  const btn = document.getElementById("post");
  btn.disabled = true;
  btn.textContent = "…";
  const logEl = document.getElementById("log");
  if (logEl.dataset.empty) { logEl.textContent = ""; delete logEl.dataset.empty; }
  const lineYou = document.createElement("div");
  lineYou.textContent = "you: " + text;
  lineYou.style.color = "#58a6ff";
  logEl.appendChild(lineYou);
  input.value = "";
  try {
    const resp = await fetch("/page-agent/ask", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({device_id: deviceId, token: token, text: text})
    });
    if (resp.status === 401) {
      asking = false;
      btn.disabled = false;
      btn.textContent = "Send";
      agentLost("This tab lost " + agentName + " (it was opened elsewhere).");
      return;
    }
    const data = await resp.json();
    if (data.ok && data.reply) {
      if (data.images && data.images.length) {
        for (const img of data.images) {
          const imgEl = document.createElement("img");
          imgEl.src = img;
          imgEl.style.maxWidth = "320px";
          imgEl.style.borderRadius = "4px";
          imgEl.style.margin = "0.2rem 0";
          imgEl.style.display = "block";
          logEl.appendChild(imgEl);
        }
      }
      const lineAgent = document.createElement("div");
      lineAgent.textContent = "agent: " + data.reply;
      lineAgent.style.color = "#3fb950";
      logEl.appendChild(lineAgent);
      speak(data.reply).catch(() => {});
    } else {
      const lineErr = document.createElement("div");
      lineErr.textContent = "agent: (error) " + (data.message || "no reply");
      lineErr.style.color = "#f85149";
      logEl.appendChild(lineErr);
    }
  } catch (e) {
    const lineErr = document.createElement("div");
    lineErr.textContent = "agent: (error) " + e;
    lineErr.style.color = "#f85149";
    logEl.appendChild(lineErr);
  }
  logEl.scrollTop = logEl.scrollHeight;
  asking = false;
  setActivity(listening ? "listening" : "idle");
  btn.disabled = false;
  btn.textContent = "Send";
}

document.getElementById("post").onclick = askAgent;
document.getElementById("discuss").addEventListener("keydown", (e) => {
  if (e.key === "Enter") askAgent();
});
document.getElementById("cam").onclick = async () => {
  if (!MEDIA_OK) {
    document.getElementById("camstate").textContent = "needs https or localhost";
    return;
  }
  try {
    stream = await navigator.mediaDevices.getUserMedia({video: true});
    const v = document.getElementById("video");
    v.srcObject = stream; v.style.display = "block"; v.play();
    document.getElementById("camstate").textContent = "on";
  } catch (e) {
    document.getElementById("camstate").textContent = "denied";
  }
};

// ── Voice state display ─────────────────────────────────────────────────
// One place decides what the voice UI says. Previously five call sites built
// the string inline, which is how "listening" ended up meaning both "waiting
// for the wake word" and "heard you and ignored it".
function currentWakeWord() {
  return document.getElementById("wakeWord").value.trim();
}

const VOICE_STATES = {
  off:       {label: "off",           color: "#6e7681", live: false,
              hint: () => "press Listen to start"},
  starting:  {label: "starting…",     color: "#d29922", live: false,
              hint: () => "allow microphone access when prompted"},
  listening: {label: "listening",     color: "#3fb950", live: true,
              hint: () => { const w = currentWakeWord();
                return w ? "say “" + w + "”, then your question"
                         : "open mic — everything you say is sent"; }},
  ignored:   {label: "heard you",     color: "#d29922", live: true,
              hint: () => { const w = currentWakeWord();
                return w ? "ignored — start with “" + w + "”"
                         : "ignored — no speech recognised"; }},
  thinking:  {label: "thinking…",     color: "#d29922", live: false,
              hint: () => "working on it — mic paused"},
  speaking:  {label: "speaking…",     color: "#58a6ff", live: false,
              hint: () => "replying — wait for it to finish"},
};

// ── Microphone level meter ──────────────────────────────────────────────
// Answers "is it hearing me?" without needing the agent to respond, which
// separates a mic problem from a wake-word problem.
let micLevel = 0;        // smoothed 0..1
let micPeak = 0;         // recent peak, for clip detection
let meterRaf = null;

function pumpMeter() {
  const bar = document.getElementById("meterbar");
  const pct = Math.round(Math.min(1, micLevel) * 100);
  bar.style.width = pct + "%";
  // Red only when actually clipping — otherwise the meter reads as an alarm.
  bar.style.background = micPeak >= 0.99 ? "#f85149" : (pct > 4 ? "#3fb950" : "#30363d");
  micLevel *= 0.86;      // decay between audio callbacks so it falls smoothly
  micPeak *= 0.9;
  meterRaf = requestAnimationFrame(pumpMeter);
}

function startMeter() {
  if (meterRaf === null) meterRaf = requestAnimationFrame(pumpMeter);
  document.getElementById("meterlabel").textContent = "mic";
}

function stopMeter() {
  if (meterRaf !== null) { cancelAnimationFrame(meterRaf); meterRaf = null; }
  micLevel = 0; micPeak = 0;
  document.getElementById("meterbar").style.width = "0%";
  document.getElementById("meterlabel").textContent = "mic off";
}

let voiceStateTimer = null;
function setVoiceState(name, revertAfterMs) {
  const s = VOICE_STATES[name] || VOICE_STATES.off;
  const box = document.getElementById("voicestate");
  document.getElementById("voicelabel").textContent = s.label;
  document.getElementById("voicelabel").style.color = s.color;
  document.getElementById("voicehint").textContent = s.hint();
  document.getElementById("voicedot").style.background = s.color;
  box.classList.toggle("voice-live", s.live);
  // Mirror the voice state into the heartbeat activity the dashboard shows.
  setActivity({listening: "listening", ignored: "listening", thinking: "thinking",
               speaking: "speaking"}[name] || "idle");
  if (voiceStateTimer) { clearTimeout(voiceStateTimer); voiceStateTimer = null; }
  // Transient states (like "heard you, ignored") fall back to the real one.
  if (revertAfterMs) {
    voiceStateTimer = setTimeout(() => setVoiceState(listening ? "listening" : "off"), revertAfterMs);
  }
}

// Keep the hint honest while the wake word is edited mid-session.
document.getElementById("wakeWord").addEventListener("input", () => {
  if (voiceWs && voiceWs.readyState === WebSocket.OPEN) {
    const word = document.getElementById("wakeWord").value.trim().toLowerCase();
    voiceWs.send(JSON.stringify({type: "wake_word", word: word}));
  }
  if (listening && !replyPlaying) setVoiceState("listening");
});

// ── Voice WebSocket: hands-free with wake word ──────────────────────────
let voiceWs = null;
// How the current hands-free reply is voiced ("hub", "browser", "off"),
// captured when the reply starts so switching mid-reply doesn't mix voices.
let handsFreeVoice = "hub";
// Reply audio currently queued, so it can be cut off when interrupted.
const playing = new Set();
let playAt = 0;

function stopPlayback() {
  if (hubAudio) hubAudio.stop();
  for (const src of playing) { try { src.stop(); } catch (e) {} }
  playing.clear();
  playAt = 0;
  clearTimeout(playbackTimer);
  replyPlaying = false;
  if (window.speechSynthesis) speechSynthesis.cancel();
}
let audioCtx = null;
let micStream = null;
let micSource = null;
let processor = null;
let listening = false;
let starting = false;
let replyPlaying = false;
let playbackTimer = null;
// True when the hub listens for the wake word with a model, which can hear
// its name over the reply: then the mic stays open so a reply can be
// interrupted. Otherwise the reply's echo could trigger a command, so the
// mic is muted while it plays.
let bargeIn = false;

function voiceLog(msg, color) {
  const logEl = document.getElementById("log");
  const line = document.createElement("div");
  line.textContent = "[voice] " + msg;
  line.style.color = color || "#8b949e";
  line.style.fontSize = "0.85rem";
  logEl.appendChild(line);
  logEl.scrollTop = logEl.scrollHeight;
}

// Linear-interpolation downsample of a Float32 mono buffer to 16 kHz. Good
// enough for VAD + ASR; a proper anti-alias filter is not worth it here.
function downsampleTo16k(buf, inRate) {
  const ratio = inRate / 16000;
  const outLen = Math.max(1, Math.floor(buf.length / ratio));
  const out = new Float32Array(outLen);
  for (let i = 0; i < outLen; i++) {
    const pos = i * ratio;
    const lo = Math.floor(pos);
    const hi = Math.min(lo + 1, buf.length - 1);
    const frac = pos - lo;
    out[i] = buf[lo] * (1 - frac) + buf[hi] * frac;
  }
  return out;
}

async function startListening() {
  if (listening || starting || !token) return;
  if (!MEDIA_OK) {
    voiceLog("microphone unavailable: the page needs https or localhost", "#f85149");
    return;
  }
  // The mic permission prompt and WS handshake take a moment; without this the
  // badge sits on "off" and the button says "Stop", which reads as broken.
  starting = true;
  document.getElementById("listen").textContent = "Cancel";
  setVoiceState("starting");
  const wakeWord = document.getElementById("wakeWord").value.trim().toLowerCase();
  const wsUrl = (location.protocol === "https:" ? "wss:" : "ws:") + "//" + location.host
    + "/page-agent/voice?device_id=" + encodeURIComponent(deviceId)
    + "&token=" + encodeURIComponent(token);
  const socket = new WebSocket(wsUrl);
  voiceWs = socket;
  voiceWs.binaryType = "arraybuffer";
  voiceWs.onopen = async () => {
    if (voiceWs !== socket) return;
    // Always sent, even empty: an empty wake word means open mic, and not
    // sending it left the hub on its default "computer".
    voiceWs.send(JSON.stringify({type: "wake_word", word: wakeWord}));
    voiceWs.send(JSON.stringify({type: "voice_mode", mode: voiceMode()}));
    try {
      // Ask for the mic with the browser's own cleanup on. autoGainControl in
      // particular is what makes a laptop mic loud enough for the wake word.
      // Do NOT constrain sampleRate here — it is advisory, browsers ignore it,
      // and asking can trigger OverconstrainedError on some devices.
      const acquiredStream = await navigator.mediaDevices.getUserMedia({audio: {
        channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true
      }});
      if (voiceWs !== socket || socket.readyState !== WebSocket.OPEN) {
        acquiredStream.getTracks().forEach(t => t.stop());
        return;
      }
      micStream = acquiredStream;
    } catch (e) {
      if (voiceWs !== socket) return;
      voiceLog("mic denied: " + e, "#f85149");
      stopListening();
      return;
    }
    // The server pipeline (Silero VAD + ASR) assumes 16 kHz mono. Browsers do
    // NOT reliably honour new AudioContext({sampleRate: 16000}) — Firefox
    // throws, some Chrome builds silently stay at 48 kHz — so capture at the
    // native rate and downsample in JS. Sending 48 kHz PCM labelled 16 kHz is
    // exactly what made the wake word "not hear anything".
    try {
      const context = new (window.AudioContext || window.webkitAudioContext)();
      audioCtx = context;
      await context.resume();
      if (voiceWs !== socket) { context.close(); return; }
    } catch (e) {
      if (voiceWs !== socket) return;
      voiceLog("audio init failed: " + e, "#f85149");
      stopListening();
      return;
    }
    const inRate = audioCtx.sampleRate;
    voiceLog("capturing at " + inRate + " Hz → 16000 Hz", "#8b949e");
    micSource = audioCtx.createMediaStreamSource(micStream);
    processor = audioCtx.createScriptProcessor(1024, 1, 1);
    processor.onaudioprocess = (e) => {
      if (!listening || !voiceWs || voiceWs.readyState !== 1) return;
      let input = e.inputBuffer.getChannelData(0);
      let sumSq = 0, peak = 0;
      for (let i = 0; i < input.length; i++) {
        const v = input[i];
        sumSq += v * v;
        const a = v < 0 ? -v : v;
        if (a > peak) peak = a;
      }
      // RMS is quiet for speech, so scale it into a usable range rather than
      // showing a bar that never leaves the left edge.
      const level = Math.min(1, Math.sqrt(sumSq / input.length) * 5);
      if (level > micLevel) micLevel = level;   // fast attack, rAF handles decay
      if (peak > micPeak) micPeak = peak;
      if (inRate !== 16000) input = downsampleTo16k(input, inRate);
      const pcm = new Int16Array(input.length);
      for (let i = 0; i < input.length; i++) {
        let s = input[i] * 32768;
        s = Math.max(-32768, Math.min(32767, s));
        pcm[i] = s;
      }
      // Drop microphone audio while replying (unless barge-in is possible),
      // and bound queued capture to 250ms.
      if ((bargeIn || !replyPlaying) && !hubAudio && voiceWs.bufferedAmount < 8000) voiceWs.send(pcm.buffer);
    };
    micSource.connect(processor);
    processor.connect(audioCtx.destination);
    starting = false;
    listening = true;
    document.getElementById("listen").textContent = "Stop";
    setVoiceState("listening");
    startMeter();
    voiceLog("listening" + (wakeWord ? " for wake word '" + wakeWord + "'" : ""), "#3fb950");
  };
  voiceWs.onmessage = async (ev) => {
    if (voiceWs !== socket) return;
    if (typeof ev.data === "string") {
      const msg = JSON.parse(ev.data);
      if (msg.type === "stt") {
        voiceLog("heard: " + msg.text, "#58a6ff");
      } else if (msg.type === "wake") {
        // A new request while it is talking: drop the rest of the old reply.
        stopPlayback();
        voiceLog(msg.command
          ? "wake word '" + msg.word + "' → " + msg.command
          : "wake word '" + msg.word + "' heard", "#f0883e");
        if (!msg.command) return;
        const logEl = document.getElementById("log");
        if (logEl.dataset.empty) { logEl.textContent = ""; delete logEl.dataset.empty; }
        const line = document.createElement("div");
        line.textContent = "you: " + msg.command;
        line.style.color = "#58a6ff";
        logEl.appendChild(line);
      } else if (msg.type === "thinking") {
        replyPlaying = true;
        setVoiceState("thinking");
      } else if (msg.type === "heard") {
        // Something was picked up but not acted on: say so instead of nothing.
        voiceLog(msg.text ? "(" + msg.reason + ") " + msg.text : "heard sound, but no words", "#6e7681");
      } else if (msg.type === "tts" && (msg.state === "start" || msg.state === "more")) {
        if (msg.state === "start") {
          replyPlaying = true;
          document.getElementById("voice-notice").textContent = msg.voice_notice || "";
        }
        setVoiceState("speaking");
        // The voice option applies here too: the hub streams audio only in
        // "hub" mode; "browser" speaks the text; "off" stays quiet.
        if (msg.state === "start") handsFreeVoice = voiceMode();
        if (handsFreeVoice === "browser") speakBuiltin(msg.text || "");
        const logEl = document.getElementById("log");
        const line = document.createElement("div");
        line.textContent = "agent: " + msg.text;
        line.style.color = "#3fb950";
        logEl.appendChild(line);
        logEl.scrollTop = logEl.scrollHeight;
      } else if (msg.type === "tts" && msg.state === "stop") {
        clearTimeout(playbackTimer);
        if (msg.interrupted) {
          stopPlayback();
          if (listening) setVoiceState("listening");
        } else {
          // Delivery is done, but queued audio may still be playing.
          playbackTimer = setTimeout(() => {
            replyPlaying = false;
            if (listening) setVoiceState("listening");
          }, Math.max(0, playAt - (audioCtx ? audioCtx.currentTime : 0)) * 1000);
        }
      } else if (msg.type === "tts_error") {
        voiceLog("could not speak that: " + msg.message, "#d29922");
      } else if (msg.type === "wake_mode") {
        bargeIn = !!msg.model;
      } else if (msg.type === "transcript") {
        voiceLog("(not wake word) " + msg.text, "#6e7681");
        setVoiceState("ignored", 2500);
      } else if (msg.type === "error") {
        replyPlaying = false;
        setVoiceState("listening");
        voiceLog("error: " + msg.message, "#f85149");
      }
    } else {
      // Binary PCM audio — play it through WebAudio (hub voice only)
      if (!audioCtx || handsFreeVoice !== "hub") return;
      const pcm16 = new Int16Array(ev.data);
      const float32 = new Float32Array(pcm16.length);
      for (let i = 0; i < pcm16.length; i++) float32[i] = pcm16[i] / 32768;
      const buf = audioCtx.createBuffer(1, float32.length, 16000);
      buf.getChannelData(0).set(float32);
      const src = audioCtx.createBufferSource();
      src.buffer = buf;
      src.connect(audioCtx.destination);
      // Network chunks arrive in bursts; schedule contiguous audio, never overlap.
      src.onended = () => { playing.delete(src); };
      playing.add(src);
      const startsAt = Math.max(audioCtx.currentTime, playAt);
      src.start(startsAt);
      playAt = startsAt + buf.duration;
    }
  };
  voiceWs.onerror = () => { voiceLog("WS error", "#f85149"); };
  voiceWs.onclose = () => { if (voiceWs === socket) stopListening(); };
}

function stopListening() {
  starting = false;
  listening = false;
  replyPlaying = false;
  playAt = 0;
  bargeIn = false;
  clearTimeout(playbackTimer);
  if (processor) { processor.disconnect(); processor = null; }
  if (micSource) { micSource.disconnect(); micSource = null; }
  if (micStream) { micStream.getTracks().forEach(t => t.stop()); micStream = null; }
  if (audioCtx) { audioCtx.close(); audioCtx = null; }
  if (voiceWs) { voiceWs.close(); voiceWs = null; }
  document.getElementById("listen").textContent = "Listen";
  setVoiceState("off");
  stopMeter();
}

document.getElementById("listen").onclick = () => {
  if (listening || starting) stopListening(); else startListening();
};

start();
</script>
</body></html>
"""

PAGE_HTML = PAGE_HTML.replace("</style>", WORKSPACE_CSS + "</style>")
