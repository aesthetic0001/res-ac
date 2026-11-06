import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {encodeCommand} from '../src/command-wire.mjs';
import {decodeRequest} from '../src/request-wire.mjs';

const fixtures=JSON.parse(readFileSync(new URL('../../test/fixtures/command-wire.json',import.meta.url)));
test('request decoding interoperates with existing command values and Python cooperative encoding',()=>{
  const values=fixtures.map(f=>f.value).concat([{'text':'\ufefftext\0é😀','number':1.234567890123456},true,null,2147483648]);
  for(const value of values)assert.deepEqual(decodeRequest(encodeCommand(value)),value);
  const source=`import sys,json;sys.path.insert(0,${JSON.stringify(fileURLToPath(new URL('../../device',import.meta.url)))});from request_wire import encode
values=json.loads(sys.stdin.read());print(json.dumps({'values':[encode(v).hex() for v in values],'negativeZero':encode(-0.0).hex()}))`;
  const encoded=JSON.parse(execFileSync('python3',['-c',source],{input:JSON.stringify(values),encoding:'utf8',timeout:10000}));
  for(let i=0;i<values.length;i++)assert.deepEqual(decodeRequest(Buffer.from(encoded.values[i],'hex')),values[i]);
  assert.ok(Object.is(decodeRequest(Buffer.from(encoded.negativeZero,'hex')),-0));
});
test('binary request decoding bounds bytes, depth, nodes, Unicode, keys and complete consumption',()=>{
  const wrap=bytes=>Buffer.concat([Buffer.from('RAC1'),Buffer.from(bytes)]);
  const invalid=[Buffer.alloc(32769),wrap([97,255,255]),wrap([111,255,255]),
    wrap([115,0,2,192,128]),wrap([115,0,3,237,160,128]),wrap([111,0,1,105,0,0,0,1,110]),
    wrap([111,0,2,115,0,1,120,110,115,0,1,120,110]),wrap([110,110]),wrap([255])];
  for(const number of [NaN,Infinity,-Infinity]){
    const value=Buffer.alloc(9);value[0]=100;value.writeDoubleBE(number,1);invalid.push(wrap(value));
  }
  const valid=encodeCommand({nested:[{value:'string'}]});
  for(let length=0;length<valid.length;length++)invalid.push(valid.subarray(0,length));
  invalid.push(wrap([...Array.from({length:17},()=>[97,0,1]).flat(),110]));
  invalid.push(wrap([97,32,0,...Array(8192).fill(110)]));
  for(const value of invalid)assert.throws(()=>decodeRequest(value));
  assert.equal(decodeRequest(wrap([97,31,255,...Array(8191).fill(110)])).length,8191);
  const value=JSON.parse('{"__proto__":{"polluted":true},"constructor":"data"}');
  const decoded=decodeRequest(encodeCommand(value));assert.deepEqual(decoded,value);
  assert.equal(Object.getPrototypeOf(decoded),Object.prototype);assert.equal({}.polluted,undefined);
});
