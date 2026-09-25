// Run with: node tests/browser/page_speech.cjs (no browser packages required).
// The persona voice speaks a reply sentence by sentence: the first sentence
// plays before later ones are synthesized, and the speak call resolves then.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('src/agent_hub/server/_page_html.py', 'utf8')
  .split('<script>')[1].split('</script>')[0]
  // The page is a Python string: undo its escaping of backslashes.
  .replace(/\\\\/g, '\\')
  .replace('%%PERSONA%%', '""').replace(/start\(\);\s*$/, '');
const elements = new Map();
const el = id => {
  if (!elements.has(id)) elements.set(id, {value: '', textContent: '', style: {}, dataset: {},
    classList: {toggle() {}, add() {}, remove() {}}, addEventListener() {}, appendChild() {}, focus() {}});
  return elements.get(id);
};
const requested = [];
const pending = [];
const clips = [];
class FakeAudio {
  constructor(url) { this.url = url; this.listeners = {}; this.paused = false; clips.push(this); }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  play() { return Promise.resolve(); }
  pause() { this.paused = true; }
  end() { this.listeners.ended(); }
}
const context = vm.createContext({
  console, Promise, setTimeout, clearTimeout,
  sessionStorage: {getItem: () => 'test-page'},
  document: {getElementById: el, createElement: () => el('created'), body: el('body'),
    addEventListener() {}},
  URLSearchParams,
  window: {addEventListener() {}}, location: {protocol: 'https:', host: 'test', search: ''},
  navigator: {mediaDevices: {getUserMedia: () => new Promise(() => {})}}, WebSocket: class { static OPEN = 1; },
  Audio: FakeAudio,
  URL: {createObjectURL: blob => 'blob:' + blob.text, revokeObjectURL() {}},
  fetch: (url, init) => {
    if (url !== '/page-agent/tts') return new Promise(() => {});
    const text = JSON.parse(init.body).text;
    requested.push(text);
    return new Promise(resolve => pending.push(() => resolve({
      ok: true, headers: {get: () => ''}, blob: async () => ({text}),
    })));
  },
});
vm.runInContext(source, context);
vm.runInContext('token = "t"; deviceId = "page-test";', context);
const tick = () => new Promise(resolve => setImmediate(resolve));

(async () => {
  const chunks = vm.runInContext(
    'speechChunks("Sure. The weather today is sunny and warm! Bring \\"water.\\" Done?")', context);
  assert.deepEqual(Array.from(chunks), [
    'Sure. The weather today is sunny and warm!', 'Bring "water." Done?'],
    'short pieces ride with the next sentence; closing quotes stay with theirs');
  assert.deepEqual(Array.from(vm.runInContext('speechChunks("no punctuation here")', context)),
    ['no punctuation here']);
  assert.deepEqual(Array.from(vm.runInContext('speechChunks("   ")', context)), []);

  let resolved = false;
  const speaking = vm.runInContext(
    'speakHub("This is the first long sentence. This is the second long sentence. And a third one here.")',
    context).then(() => { resolved = true; });
  await tick();
  // Only the first sentence is synthesized before anything plays.
  assert.deepEqual(requested, ['This is the first long sentence.']);
  assert.equal(resolved, false);
  pending.shift()();
  await tick(); await tick();
  assert.equal(resolved, true, 'speak resolves once the first sentence is playing');
  assert.equal(vm.runInContext('replyPlaying', context), true);
  // The next sentence is fetched while the first plays.
  assert.deepEqual(requested.slice(1), ['This is the second long sentence.']);
  await assert.rejects(vm.runInContext('speakHub("Another reply entirely.")', context),
    /already speaking/);
  pending.shift()();
  await tick();
  clips[0].end();
  await tick(); await tick();
  assert.equal(clips.length, 2, 'second sentence plays after the first ends');
  assert.deepEqual(requested.slice(2), ['And a third one here.']);
  // Stopping (barge-in, new turn) cuts the rest off and frees the speaker.
  vm.runInContext('stopPlayback()', context);
  pending.shift()();
  await speaking;
  await tick(); await tick();
  assert.equal(clips[1].paused, true);
  assert.equal(clips.length, 2, 'nothing plays after a stop');
  assert.equal(vm.runInContext('hubAudio', context), null);
  assert.equal(vm.runInContext('replyPlaying', context), false);

  // A failed first sentence is reported to the caller.
  context.fetch = async () => ({ok: false, status: 502});
  await assert.rejects(vm.runInContext('speakHub("Hello there, this fails.")', context), /hub TTS 502/);
  assert.equal(vm.runInContext('hubAudio', context), null);

  // Code is dropped before splitting, even a fence left open, and long
  // stretches are capped so the voice engine never gets a wall of text.
  assert.deepEqual(Array.from(vm.runInContext(
    'speechChunks("I ran a simulation.\\n```python\\ndef f(x):\\n  return x\\n```\\nLevel four. Tail ```js\\nlet y")',
    context)), ['I ran a simulation. Level four.', 'Tail']);
  const long = Array.from(vm.runInContext('speechChunks("word, ".repeat(120))', context));
  assert.ok(long.length > 1 && long.every(p => p.length <= 251), 'long text is capped');

  // Once speech has started, a sentence the voice cannot say is skipped.
  requested.length = 0;
  clips.length = 0;
  let calls = 0;
  context.fetch = async (url, init) => {
    if (url !== '/page-agent/tts') return {ok: true, json: async () => ({})};
    calls += 1;
    requested.push(JSON.parse(init.body).text);
    return calls === 2 ? {ok: false, status: 502}
      : {ok: true, headers: {get: () => ''}, blob: async () => ({text: 'audio'})};
  };
  await vm.runInContext(
    'speakHub("The first sentence plays fine. The second sentence fails to say. The third sentence still plays.")',
    context);
  for (let i = 0; i < 20 && clips.length < 2; i++) {
    if (clips.length) clips[clips.length - 1].end();
    await tick();
  }
  assert.deepEqual(requested, ['The first sentence plays fine.',
    'The second sentence fails to say.', 'The third sentence still plays.']);
  assert.equal(clips.length, 2, 'the third sentence still plays after the second fails');
  console.log('PASS: sentence chunks, first-sentence start, prefetch, stop, failure, code, skip');
})().catch(error => { console.error(error); process.exitCode = 1; });
