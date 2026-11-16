import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const source=await readFile(new URL('../frontend/preferences.js',import.meta.url),'utf8');
const url='data:text/javascript;base64,'+Buffer.from(source).toString('base64');
const {preferenceKey,readPreference,writePreference}=await import(url);
const widgetSource=(await readFile(new URL('../frontend/dashboard-widgets.js',import.meta.url),'utf8')).replace("'./preferences.js'",JSON.stringify(url));
const {BASIC_WIDGETS,validWidgets,initialWidgets}=await import('data:text/javascript;base64,'+Buffer.from(widgetSource).toString('base64'));
const {DEFAULT_PLAN,validPlan,planAmounts,goalFigures}=await import('data:text/javascript;base64,'+Buffer.from(await readFile(new URL('../frontend/invest-plan.js',import.meta.url))).toString('base64'));

test('widget choices are separate for each signed-in account and permit an empty dashboard',()=>{
  const map=new Map();const storage={getItem:k=>map.get(k)??null,setItem:(k,v)=>map.set(k,v)};
  const a=preferenceKey('widgets','a'),b=preferenceKey('widgets','b');
  assert.notEqual(a,b);assert.equal(preferenceKey('widgets',null),null);
  writePreference(storage,a,[]);
  assert.deepEqual(readPreference(storage,a,BASIC_WIDGETS,validWidgets),[]);
  assert.deepEqual(readPreference(storage,b,BASIC_WIDGETS,validWidgets),BASIC_WIDGETS);
  assert.equal(validWidgets(['constructor']),false);
  assert.equal(validWidgets(['income','income']),false);
});
test('corrupt or unavailable browser storage cannot break the financial pages',()=>{
  const corrupt={getItem:()=>'{broken'};
  const blocked={getItem:()=>{throw Error('blocked')},setItem:()=>{throw Error('blocked')}};
  assert.deepEqual(readPreference(corrupt,'key',DEFAULT_PLAN,validPlan),DEFAULT_PLAN);
  assert.deepEqual(readPreference(blocked,'key',DEFAULT_PLAN,validPlan),DEFAULT_PLAN);
  assert.equal(writePreference(blocked,'key',{}),false);
});
test('balance is added once to existing layouts, preserving later removal and other accounts',()=>{
  const map=new Map(); const storage={getItem:k=>map.get(k)??null,setItem:(k,v)=>map.set(k,v)};
  writePreference(storage,preferenceKey('dashboard-widgets-v1','a'),['spending','goal']);
  assert.deepEqual(initialWidgets(storage,'a'),['balance','spending','goal']);
  assert.deepEqual(initialWidgets(storage,'b'),BASIC_WIDGETS);
  writePreference(storage,preferenceKey('dashboard-widgets-v2','a'),['goal']);
  assert.deepEqual(initialWidgets(storage,'a'),['goal']);
  writePreference(storage,preferenceKey('dashboard-widgets-v2','a'),[]);
  assert.deepEqual(initialWidgets(storage,'a'),[]);
});
test('50/30/20 is automatic and missing income produces no invented amounts',()=>{
  assert.equal(DEFAULT_PLAN.mode,'example');
  assert.deepEqual(planAmounts(2000),{needs:1000,wants:600,savings:400});
  assert.equal(planAmounts(0),null);
  assert.equal(planAmounts(NaN),null);
});
test('custom splits require bounded whole percentages totalling exactly 100',()=>{
  assert.equal(validPlan({mode:'custom',needs:60,wants:25,savings:15}),true);
  for(const plan of [{mode:'custom',needs:60,wants:25,savings:20},{mode:'custom',needs:-1,wants:81,savings:20},
    {mode:'custom',needs:50.5,wants:29.5,savings:20},{mode:'other',needs:50,wants:30,savings:20}]) assert.equal(Boolean(validPlan(plan)),false);
});
test('allocation preserves every cent, including one-cent income and zero-percent savings',()=>{
  for(const cents of [1,3,101,10001,982799]) for(const [needs,wants,savings] of [[50,30,20],[50,50,0],[33,33,34],[0,100,0],[100,0,0]]){
    const value=planAmounts(cents/100,{mode:'custom',needs,wants,savings});
    assert.equal(Math.round((value.needs+value.wants+value.savings)*100),cents);
    assert.ok(Object.values(value).every(n=>n>=0));
  }
});
test('recorded goal progress uses the current forecast input, never stale data or its projection',()=>{
  const value=goalFigures({forecast:{current_saved:3031.97,target_amount:1100,projected_total:4147.02},goal:{current_saved:9999}});
  assert.equal(value.current,3031.97);assert.equal(value.projected,4147.02);
  assert.equal(goalFigures({}).current,null);assert.equal(goalFigures({forecast:{current_saved:null}}).current,null);
});
