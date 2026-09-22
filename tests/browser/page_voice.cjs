// Run with: node tests/browser/page_voice.cjs (no browser packages required).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('src/agent_hub/server/_page_html.py', 'utf8')
  .split('<script>')[1].split('</script>')[0]
  .replace('%%PERSONA%%', '""').replace(/start\(\);\s*$/, '');
const elements = new Map();
const el = id => {
  if (!elements.has(id)) elements.set(id, {value: '', style: {}, dataset: {},
    classList: {toggle() {}, add() {}, remove() {}}, addEventListener() {}, appendChild() {}, focus() {}});
  return elements.get(id);
};
const starts = [];
let finishPlayback;
let stoppedTracks = 0;
let resolveMic;
class Socket {
  static OPEN = 1;
  constructor() { this.readyState = 1; this.sent = []; }
  send(data) { this.sent.push(data); }
  close() { this.readyState = 3; }
}
const context = vm.createContext({
  console, Uint8Array, Int16Array, Float32Array, WebSocket: Socket,
  sessionStorage: {getItem: () => 'test-page'},
  navigator: {mediaDevices: {getUserMedia: () => new Promise(resolve => { resolveMic = resolve; })}},
  document: {getElementById: el, createElement: () => el('created')},
  window: {addEventListener() {}}, location: {protocol: 'https:', host: 'test'},
  setTimeout: (callback, ms) => { finishPlayback = {callback, ms}; return 1; },
  clearTimeout() {}, cancelAnimationFrame() {}, requestAnimationFrame() { return 1; }
});
vm.runInContext(source, context);
// Skip the name form: startListening needs the token registration hands out.
vm.runInContext('token = "test-token"; deviceId = "page-test";', context);
el('voiceMode').value = 'hub';  // the page's selected default
(async () => {
  await vm.runInContext('startListening()', context);
  // Clearing the wake word must actually request open mic.
  const socket = vm.runInContext('voiceWs', context);
  const opening = socket.onopen();
  assert.deepEqual(JSON.parse(socket.sent[0]), {type: 'wake_word', word: ''});
  vm.runInContext('stopListening()', context);
  resolveMic({getTracks: () => [{stop: () => { stoppedTracks++; }}]});
  await opening;
  assert.equal(stoppedTracks, 1, 'cancelled permission prompt must release the mic');
  await vm.runInContext('startListening()', context);
  const active = vm.runInContext('voiceWs', context);
  context.fakeAudio = {currentTime: 1, destination: {},
    createBuffer: (_, length, rate) => ({duration: length / rate, getChannelData: () => new Float32Array(length)}),
    createBufferSource: () => ({connect() {}, start: time => starts.push(time)})};
  vm.runInContext('audioCtx = fakeAudio; listening = true;', context);
  await active.onmessage({data: JSON.stringify({type: 'tts', state: 'start', text: 'hello'})});
  for (let i = 0; i < 3; i++) await active.onmessage({data: new Int16Array(960).buffer});
  assert.deepEqual(starts, [1, 1.06, 1.12], '60ms chunks must play sequentially');
  await active.onmessage({data: JSON.stringify({type: 'tts', state: 'stop'})});
  assert.equal(vm.runInContext('replyPlaying', context), true);
  assert.ok(Math.abs(finishPlayback.ms - 180) < 0.001);
  finishPlayback.callback();
  assert.equal(vm.runInContext('replyPlaying', context), false);
  await active.onmessage({data: JSON.stringify({type: 'thinking'})});
  await active.onmessage({data: JSON.stringify({type: 'error', message: 'failed'})});
  assert.equal(vm.runInContext('replyPlaying', context), false);
  vm.runInContext('speakHub = async text => { globalThis.spokenText = text; };', context);
  el('voiceMode').value = 'off';
  assert.equal(await vm.runInContext('speak("Persona test", "hub")', context), 'hub');
  assert.equal(context.spokenText, 'Persona test');
  vm.runInContext('speakHub = async () => { throw new Error("offline"); };', context);
  await assert.rejects(vm.runInContext('speak("test", "hub")', context), /Persona voice unavailable/);
  assert.match(el('voice-notice').textContent, /choose browser built-in explicitly/);
  console.log('PASS: sequential audio, playback state, open mic, cancellation, error recovery');
})().catch(error => { console.error(error); process.exitCode = 1; });
