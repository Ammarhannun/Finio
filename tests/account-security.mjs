import test from 'node:test';
import assert from 'node:assert/strict';
import { mfaState, safeReturnPage, verifiedFactors, verifyAuthenticator, reauthenticatePassword } from '../frontend/account-security.js';

test('verified factors require the second step until the session has aal2', async () => {
  for (const [currentLevel,nextLevel,required] of [['aal1','aal2',true],['aal2','aal2',false],['aal1','aal1',false]]) {
    const auth={mfa:{getAuthenticatorAssuranceLevel:async()=>({data:{currentLevel,nextLevel}})}};
    assert.equal((await mfaState(auth)).required,required);
  }
});
test('an assurance check error fails closed with a private message', async () => {
  await assert.rejects(mfaState({mfa:{getAuthenticatorAssuranceLevel:async()=>({error:new Error('private provider details')})}}),
    error=>/Could not check/.test(error.message) && !error.message.includes('private provider'));
});
test('a sign-in return path cannot redirect to an external or unexpected page', () => {
  for (const value of ['https://untrusted.invalid','//untrusted.invalid','../profile.html','profile.html?next=bad',null])
    assert.equal(safeReturnPage(value),'dashboard.html');
  assert.equal(safeReturnPage('profile.html'),'profile.html');
});
test('unverified factors do not count as enabled authenticators', async () => {
  const factors=await verifiedFactors({mfa:{listFactors:async()=>({data:{totp:[{id:'pending',status:'unverified'},{id:'ready',status:'verified'}]}})}});
  assert.deepEqual(factors.map(f=>f.id),['ready']);
});
test('invalid codes do not reach the provider', async () => {
  for (const code of ['12345','1234567','123456\n','abcdef',''])
    await assert.rejects(verifyAuthenticator({},'factor',code),/six-digit/);
});
test('a valid code is bound to the provider challenge and selected factor', async () => {
  let verified;
  const result=await verifyAuthenticator({mfa:{challenge:async args=>{assert.equal(args.factorId,'chosen');return {data:{id:'challenge'}};},
    verify:async args=>{verified=args;return {data:{verified:true}};}}},'chosen','123456');
  assert.deepEqual(verified,{factorId:'chosen',challengeId:'challenge',code:'123456'});
  assert.equal(result.verified,true);
});
test('failed verification does not return provider error bodies', async () => {
  await assert.rejects(verifyAuthenticator({mfa:{challenge:async()=>({data:{id:'challenge'}}),
    verify:async()=>({error:new Error('private token')})}},'chosen','123456'),
    error=>!error.message.includes('private token'));
});
test('password reauthentication uses the current account and rejects account switches', async () => {
  let signedOut=false;
  const auth={signInWithPassword:async value=>{assert.equal(value.email,'synthetic@example.invalid');return {data:{user:{id:'different'}}};},
    signOut:async()=>{signedOut=true;}};
  await assert.rejects(reauthenticatePassword(auth,{user:{id:'original',email:'synthetic@example.invalid'}},'synthetic-only'),/account changed/);
  assert.equal(signedOut,true);
});
