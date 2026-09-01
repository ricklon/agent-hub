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
/* Input level. The audio callback fires every 256ms, which is too slow to
   look live, so RMS is sampled there and animated on rAF. */
#meter{width:12rem;height:.6rem;background:#010409;border:1px solid #30363d;
  border-radius:3px;overflow:hidden;flex:none}
#meterbar{height:100%;width:0%;background:#3fb950;transition:width .08s linear}
#meterlabel{font-size:.75rem;color:#8b949e}
</style></head><body>
<h1>Page Agent</h1>
<div class="row"><a href="/dashboard/" style="color:#58a6ff">← Dashboard</a></div>
<div id="status">initialising…</div>
<div id="personaline" style="font-size:.8rem;color:#8b949e"></div>

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
<div id="log" data-empty="1" style="background:#010409;border:1px solid #30363d;padding:.6rem;overflow:auto;max-height:24rem;border-radius:4px;white-space:pre-wrap;font-family:monospace;color:#c9d1d9">dialogue will appear here…</div>

<h2>Voice (hands-free with wake word)</h2>
<div class="row">
<button id="listen">Listen</button>
<label style="display:inline-flex;align-items:center;gap:.2rem;font-size:.8rem">
Wake word: <input id="wakeWord" value="computer" style="width:8rem"></label>
<span style="font-size:.75rem;color:#8b949e">clear it for open mic</span>
</div>
<div class="row"><div id="voicestate">
  <span id="voicedot"></span>
  <span id="voicelabel">off</span>
  <span id="voicehint">press Listen to start</span>
</div>
<div id="meter" title="microphone input level"><div id="meterbar"></div></div>
<span id="meterlabel">mic</span></div>

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
// Persona to register with, injected from ?persona= by the server ("" = default).
const PERSONA = %%PERSONA%%;
if (PERSONA) document.getElementById("personaline").textContent = "persona: " + PERSONA;

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
    body: JSON.stringify({device_id: deviceId, label: label, tools: TOOLS, persona: PERSONA})
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
      if (document.getElementById("speakReply").checked) {
        dispatch("page.audio_speaker.speak", {text: data.reply});
      }
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
  if (voiceStateTimer) { clearTimeout(voiceStateTimer); voiceStateTimer = null; }
  // Transient states (like "heard you, ignored") fall back to the real one.
  if (revertAfterMs) {
    voiceStateTimer = setTimeout(() => setVoiceState(listening ? "listening" : "off"), revertAfterMs);
  }
}

// Keep the hint honest while the wake word is edited mid-session.
document.getElementById("wakeWord").addEventListener("input", () => {
  if (listening) setVoiceState("listening");
});

// ── Voice WebSocket: hands-free with wake word ──────────────────────────
let voiceWs = null;
let audioCtx = null;
let micStream = null;
let micSource = null;
let processor = null;
let listening = false;

function voiceLog(msg, color) {
  const logEl = document.getElementById("log");
  const line = document.createElement("div");
  line.textContent = "[voice] " + msg;
  line.style.color = color || "#8b949e";
  line.style.fontSize = "0.85rem";
  logEl.appendChild(line);
  logEl.scrollTop = logEl.scrollHeight;
}

async function startListening() {
  if (listening) return;
  // The mic permission prompt and WS handshake take a moment; without this the
  // badge sits on "off" and the button says "Stop", which reads as broken.
  setVoiceState("starting");
  const wakeWord = document.getElementById("wakeWord").value.trim().toLowerCase();
  const wsUrl = (location.protocol === "https:" ? "wss:" : "ws:") + "//" + location.host
    + "/page-agent/voice?device_id=" + encodeURIComponent(deviceId)
    + "&token=" + encodeURIComponent(token);
  voiceWs = new WebSocket(wsUrl);
  voiceWs.binaryType = "arraybuffer";
  voiceWs.onopen = async () => {
    if (wakeWord) voiceWs.send(JSON.stringify({type: "wake_word", word: wakeWord}));
    try {
      micStream = await navigator.mediaDevices.getUserMedia({audio: {sampleRate: 16000, channelCount: 1}});
    } catch (e) {
      voiceLog("mic denied: " + e, "#f85149");
      stopListening();
      return;
    }
    audioCtx = new (window.AudioContext || window.webkitAudioContext)({sampleRate: 16000});
    micSource = audioCtx.createMediaStreamSource(micStream);
    processor = audioCtx.createScriptProcessor(4096, 1, 1);
    processor.onaudioprocess = (e) => {
      if (!listening || !voiceWs || voiceWs.readyState !== 1) return;
      const input = e.inputBuffer.getChannelData(0);
      const pcm = new Int16Array(input.length);
      let sumSq = 0, peak = 0;
      for (let i = 0; i < input.length; i++) {
        const v = input[i];
        sumSq += v * v;
        const a = v < 0 ? -v : v;
        if (a > peak) peak = a;
        let s = v * 32768;
        s = Math.max(-32768, Math.min(32767, s));
        pcm[i] = s;
      }
      // RMS is quiet for speech, so scale it into a usable range rather than
      // showing a bar that never leaves the left edge.
      const level = Math.min(1, Math.sqrt(sumSq / input.length) * 5);
      if (level > micLevel) micLevel = level;   // fast attack, rAF handles decay
      if (peak > micPeak) micPeak = peak;
      voiceWs.send(pcm.buffer);
    };
    micSource.connect(processor);
    processor.connect(audioCtx.destination);
    listening = true;
    document.getElementById("listen").textContent = "Stop";
    setVoiceState("listening");
    startMeter();
    voiceLog("listening" + (wakeWord ? " for wake word '" + wakeWord + "'" : ""), "#3fb950");
  };
  voiceWs.onmessage = async (ev) => {
    if (typeof ev.data === "string") {
      const msg = JSON.parse(ev.data);
      if (msg.type === "stt") {
        voiceLog("heard: " + msg.text, "#58a6ff");
      } else if (msg.type === "wake") {
        voiceLog("wake word detected: '" + msg.word + "' → " + msg.command, "#f0883e");
        const logEl = document.getElementById("log");
        if (logEl.dataset.empty) { logEl.textContent = ""; delete logEl.dataset.empty; }
        const line = document.createElement("div");
        line.textContent = "you: " + msg.command;
        line.style.color = "#58a6ff";
        logEl.appendChild(line);
      } else if (msg.type === "thinking") {
        setVoiceState("thinking");
      } else if (msg.type === "tts" && msg.state === "start") {
        setVoiceState("speaking");
        const logEl = document.getElementById("log");
        const line = document.createElement("div");
        line.textContent = "agent: " + msg.text;
        line.style.color = "#3fb950";
        logEl.appendChild(line);
        logEl.scrollTop = logEl.scrollHeight;
      } else if (msg.type === "tts" && msg.state === "stop") {
        setVoiceState("listening");
      } else if (msg.type === "transcript") {
        voiceLog("(not wake word) " + msg.text, "#6e7681");
        setVoiceState("ignored", 2500);
      } else if (msg.type === "error") {
        voiceLog("error: " + msg.message, "#f85149");
      }
    } else {
      // Binary PCM audio — play it through WebAudio
      if (!audioCtx) return;
      const pcm16 = new Int16Array(ev.data);
      const float32 = new Float32Array(pcm16.length);
      for (let i = 0; i < pcm16.length; i++) float32[i] = pcm16[i] / 32768;
      const buf = audioCtx.createBuffer(1, float32.length, 16000);
      buf.getChannelData(0).set(float32);
      const src = audioCtx.createBufferSource();
      src.buffer = buf;
      src.connect(audioCtx.destination);
      src.start();
    }
  };
  voiceWs.onerror = () => { voiceLog("WS error", "#f85149"); };
  voiceWs.onclose = () => { stopListening(); };
}

function stopListening() {
  listening = false;
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
  if (listening) stopListening(); else startListening();
};

register();
</script>
</body></html>
"""
