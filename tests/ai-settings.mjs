import test from 'node:test';
import assert from 'node:assert/strict';
import { mountAISettings, AI_CONSENT_VERSION } from '../frontend/ai-settings.js';

function fixture(initial = {enabled:false}) {
  const elements = new Map(['ai-enabled','ai-save','ai-status'].map(id => [id,
    {disabled:true,checked:false,textContent:'',dataset:{},addEventListener(){}}]));
  let state = initial, failure = false, writes = [], pause;
  const auth = {updateUser:async data => {
    writes.push(data);
    if(pause) await pause;
    if(failure) return {error:new Error('private provider detail')};
    state = {enabled:data.data.finio_ai.enabled};return {error:null};
  }};
  const controls = mountAISettings({auth,root:{getElementById:id=>elements.get(id)},
    apiFetch:async path=>{assert.equal(path,'/ai/preferences');if(failure)throw Error('private detail');return state;}});
  return {controls,elements,writes,fail(){failure=true;},delay(promise){pause=promise;}};
}

test('loading never silently opts in or coerces a string into consent',async()=>{
  for(const state of [{enabled:false},{},{enabled:'true'}]) {
    const f=fixture(state);await f.controls.load();
    assert.equal(f.elements.get('ai-enabled').checked,false);
    assert.equal(f.writes.length,0);
    assert.equal(f.elements.get('ai-save').disabled,typeof state.enabled !== 'boolean');
  }
});

test('a response that cannot verify the saved choice cannot enable the controls',async()=>{
  const f=fixture({enabled:true,available:false});await f.controls.load();
  assert.equal(f.elements.get('ai-save').disabled,true);
  assert.equal(f.elements.get('ai-enabled').checked,false);
});

test('saving opt-in and opt-out persists the exact version and verifies the server state',async()=>{
  const f=fixture();await f.controls.load();
  for(const enabled of [true,false]) {
    f.elements.get('ai-enabled').checked=enabled;await f.controls.savePreference();
    assert.deepEqual(f.writes.at(-1),{data:{finio_ai:{enabled,version:AI_CONSENT_VERSION}}});
    assert.equal(f.elements.get('ai-save').disabled,false);
    assert.match(f.elements.get('ai-status').textContent,/Saved/);
  }
});

test('failed preference reads leave controls disabled without exposing provider errors',async()=>{
  const f=fixture();f.fail();await f.controls.load();
  assert.equal(f.elements.get('ai-save').disabled,true);
  await f.controls.savePreference();assert.equal(f.writes.length,0);
  assert.doesNotMatch(f.elements.get('ai-status').textContent,/private/);
});

test('an uncertain save cannot be repeated before a reload confirms its state',async()=>{
  const f=fixture();await f.controls.load();f.fail();
  f.elements.get('ai-enabled').checked=true;
  await f.controls.savePreference();await f.controls.savePreference();
  assert.equal(f.writes.length,1);
  assert.equal(f.elements.get('ai-enabled').disabled,true);
  assert.equal(f.elements.get('ai-save').disabled,true);
  assert.doesNotMatch(f.elements.get('ai-status').textContent,/private/);
});

test('duplicate clicks during a slow save cannot start competing updates',async()=>{
  const f=fixture();await f.controls.load();let resolve;
  f.delay(new Promise(done=>{resolve=done;}));
  const pending=f.controls.savePreference();await f.controls.savePreference();
  assert.equal(f.writes.length,1);resolve();await pending;
});
