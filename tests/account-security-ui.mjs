import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createContext, SourceTextModule } from 'node:vm';

const source = await readFile(new URL('../frontend/account-security-ui.js', import.meta.url), 'utf8');
const helpers = await readFile(new URL('../frontend/account-security.js', import.meta.url), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return {promise, resolve}; };

async function fixture() {
  const elements = new Map();
  function element(tag = 'div') {
    return { tag, hidden:false, disabled:false, value:'', textContent:'', children:[], attrs:{}, listeners:new Map(),
      group:{hidden:false}, open:false, focused:false,
      addEventListener(name, callback) { const callbacks=this.listeners.get(name)||[];callbacks.push(callback);this.listeners.set(name,callbacks); },
      async emit(name) { const event={currentTarget:this,preventDefault(){}}; return Promise.all((this.listeners.get(name)||[]).map(fn=>fn(event))); },
      replaceChildren(...children) { this.children=children; }, append(...children) { this.children.push(...children); },
      closest() { return this.group; }, removeAttribute(name) { delete this.attrs[name]; if(name==='src')delete this.src; },
      setAttribute(name,value) {this.attrs[name]=value;}, focus(){this.focused=true;},
      showModal(){this.open=true;}, close(){this.open=false;},
    };
  }
  const field=id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id);};
  field('security-setup').hidden=true;
  const window=new EventTarget();
  const metrics={enroll:0,unenroll:[],verify:0,status:0};
  let verified=[], status={session_active:true,mfa_satisfied:true,recent_auth:true};
  let statusWait=null, verifyWait=null;
  const auth={
    signInWithPassword:async()=>{status.recent_auth=true;return {data:{user:{id:'owner'}}};},
    mfa:{
      listFactors:async()=>({data:{totp:verified}}),
      enroll:async()=>{metrics.enroll++;return {data:{id:'factor-'+metrics.enroll,totp:{qr_code:'<svg xmlns="http://www.w3.org/2000/svg"/>',secret:'SYNTHETIC'}}};},
      challenge:async()=>({data:{id:'challenge'}}),
      verify:async args=>{metrics.verify++;if(verifyWait)await verifyWait.promise;status.recent_auth=true;verified=[{id:args.factorId,status:'verified'}];return {data:{verified:true}};},
      unenroll:async args=>{metrics.unenroll.push(args.factorId);verified=[];return {error:null};},
    },refreshSession:async()=>({data:{},error:null}),
  };
  const context=createContext({window,document:{getElementById:field,createElement:element}});
  const module=new SourceTextModule(source,{context});
  const dependency=new SourceTextModule(helpers,{context});
  await module.link(()=>dependency);await module.evaluate();
  const controls=module.namespace.mountAccountSecurity({auth,getSession:async()=>({user:{id:'owner',email:'owner@example.invalid'}}),
    apiFetch:async()=>{metrics.status++;if(statusWait)await statusWait.promise;return {...status};}});
  return {controls,field,metrics,window,
    stale(){status.recent_auth=false;}, delayStatus(){statusWait=deferred();return statusWait;},
    enrolled(){verified=[{id:'ready',status:'verified'}];field('security-factor').value='ready';},
    delayVerify(){verifyWait=deferred();return verifyWait;},
    async init(){await controls.init();},
  };
}

test('one authenticator setup remains pending until verified or cancelled',async()=>{
  const f=await fixture();await f.init();
  await f.field('security-enable').emit('click');
  assert.equal(f.metrics.enroll,1);
  assert.equal(f.field('security-enable').hidden,true);
  await f.field('security-enable').emit('click');
  assert.equal(f.metrics.enroll,1,'a repeated setup action must not create another factor');
  await f.field('security-setup-cancel').emit('click');
  assert.deepEqual(f.metrics.unenroll,['factor-1']);
  assert.equal(f.field('security-secret').textContent,'');
  assert.equal(f.field('security-qr').src,undefined);
  assert.equal(f.field('security-enable').hidden,false);
});

test('overlapping sensitive actions cannot start competing verification checks',async()=>{
  const f=await fixture();const wait=f.delayStatus();
  const first=f.controls.confirmRecent();
  const second=f.controls.confirmRecent().then(value=>({value}),error=>({error}));
  wait.resolve();assert.equal(await first,true);
  const result=await second;
  assert.match(result.error?.message||'',/verification/i);
  assert.equal(f.metrics.status,1);
});

test('cancelling slow proof settles the action and ignores its late result',async()=>{
  const f=await fixture();f.stale();f.enrolled();
  const pending=f.controls.confirmRecent();await tick();
  f.field('security-proof').value='123456';
  const wait=f.delayVerify();
  // A real enrolled factor is selected in the proof form; only the fake Auth
  // provider is replaced, while the actual verification helper runs.
  const proof=f.field('security-proof-form').emit('submit');
  await tick();await f.field('security-proof-cancel').emit('click');
  assert.equal(await pending,false);
  wait.resolve();await proof;
  assert.equal(f.field('security-proof').value,'');
  assert.equal(f.field('security-confirm').open,false);
  assert.equal(f.metrics.status,1,'cancelled proof must not continue its follow-up request');
});

test('an in-flight setup verification cannot concurrently remove its factor',async()=>{
  const f=await fixture();await f.init();await f.field('security-enable').emit('click');
  const wait=f.delayVerify();f.field('security-setup-code').value='123456';
  const verifying=f.field('security-setup-form').emit('submit');await tick();
  assert.equal(f.field('security-setup-cancel').disabled,true);
  await f.field('security-setup-cancel').emit('click');
  assert.deepEqual(f.metrics.unenroll,[]);
  wait.resolve();await verifying;
  assert.equal(f.field('security-secret').textContent,'');
  assert.equal(f.field('security-enable').hidden,true);
});

test('leaving the page cancels pending sensitive proof and clears setup secrets',async()=>{
  const f=await fixture();f.stale();const pending=f.controls.confirmRecent();await tick();
  f.field('security-proof').value='synthetic-secret';
  f.window.dispatchEvent(new Event('pagehide'));
  const result=await Promise.race([pending,new Promise(resolve=>setTimeout(()=>resolve('unsettled'),30))]);
  assert.equal(result,false);
  assert.equal(f.field('security-proof').value,'');
  assert.equal(f.field('security-confirm').open,false);
});
