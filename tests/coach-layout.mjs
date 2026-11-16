import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const source=await readFile(new URL('../frontend/coach-layout.js',import.meta.url),'utf8');
const {splitLayout}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
test('both panes remain usable at the boundary and at the reproduced 792px width',()=>{
  for(const width of [780,792,1280,1920]) for(const percentage of [0,10,42,75,100,NaN]) {
    const layout=splitLayout(width,percentage);
    assert.equal(layout.mode,'split');
    const coach=width*layout.percentage/100;
    assert.ok(coach>=340-0.001);
    assert.ok(width-coach>=440-0.001);
  }
});
test('narrow screens explicitly use full chat with its close control',()=>{
  for(const width of [390,720,746,779]) assert.deepEqual(splitLayout(width),{mode:'full',percentage:100});
});
