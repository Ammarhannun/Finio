import test from 'node:test';
import assert from 'node:assert/strict';
import { mountSessionPrivacy } from '../frontend/session-privacy.js';

function fixture() {
  let listener, invalidated = 0, restored = 0, unsubscribed = 0;
  const target = new EventTarget();
  const destroy = mountSessionPrivacy({userId:'owner', target,
    subscribe: callback => { listener = callback; return {data:{subscription:{unsubscribe(){unsubscribed++;}}}}; },
    invalidate: () => invalidated++, restore: () => restored++});
  return {emit:(...args)=>listener(...args), destroy,
    show(persisted) { const event = new Event('pageshow'); event.persisted = persisted; target.dispatchEvent(event); },
    get counts(){return {invalidated,restored,unsubscribed};}};
}

test('cross-tab sign-out clears the private view exactly once', () => {
  const f=fixture(); f.emit('SIGNED_OUT',null); f.emit('SIGNED_OUT',null);
  assert.equal(f.counts.invalidated,1);
});
test('another account cannot inherit the previous account screen', () => {
  const f=fixture(); f.emit('SIGNED_IN',{user:{id:'other'}});
  assert.equal(f.counts.invalidated,1);
});
test('normal token renewal keeps the view while restored documents reauthenticate', () => {
  const f=fixture(); f.emit('TOKEN_REFRESHED',{user:{id:'owner'}});f.show(false);
  assert.deepEqual(f.counts,{invalidated:0,restored:0,unsubscribed:0});
  f.show(true);assert.equal(f.counts.restored,1);
});
test('destroyed or signed-out guards cannot restore a financial view', () => {
  const f=fixture();f.emit('SIGNED_OUT',null);f.show(true);
  assert.equal(f.counts.restored,0);
  f.destroy();f.emit('SIGNED_IN',{user:{id:'other'}});f.show(true);
  assert.deepEqual(f.counts,{invalidated:1,restored:0,unsubscribed:1});
});
