import { test } from 'node:test';
import assert from 'node:assert/strict';
import { moveThreshold, validThresholds } from '../public/fan-curve.js';

test('graph editing carries neighboring thresholds without breaking nondecreasing order', () => {
  assert.deepEqual(moveThreshold([22,24,26,28,30],2,29),[22,24,29,29,30]);
  assert.deepEqual(moveThreshold([22,24,26,28,30],2,23),[22,23,23,28,30]);
  assert.deepEqual(moveThreshold([24,24,24,24,24],2,26),[24,24,26,26,26]);
  assert.deepEqual(moveThreshold([24,24,24,24,24],2,22),[22,22,22,24,24]);
  for(let index=0;index<5;index++) for(const value of [-100,-20,0,22.25,24,40,60,100]) {
    const result=moveThreshold([22,24,26,28,30],index,value);
    assert.equal(validThresholds(result),true);
    assert.ok(result[index]>=-20 && result[index]<=60);
  }
});

test('graph validation accepts equal thresholds and rejects reversed, missing, or nonfinite values', () => {
  for(const values of [[24,24,24,24,24],[22,22.1,22.1,28,30]]) assert.equal(validThresholds(values),true);
  for(const values of [[22,21,26,28,30],[22,24,26,28],[22,24,26,28,NaN],[22,24,26,28,61]]) assert.equal(validThresholds(values),false);
});
