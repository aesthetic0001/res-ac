import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {mkdtempSync,rmSync,readFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {execFileSync,fork,spawnSync} from 'node:child_process';
import {once} from 'node:events';

const driver=new URL('../src/sqlite.mjs',import.meta.url).href;
const store=new URL('../src/store.mjs',import.meta.url).href;
test('an unknown SQLite synchronization mode fails startup',()=>{
  const child=spawnSync(process.execPath,['--input-type=module','-e',`await import(${JSON.stringify(driver)})`],
    {env:{...process.env,RESAC_SQLITE_SYNC:'invalid'},encoding:'utf8'});
  assert.notEqual(child.status,0);assert.match(child.stderr,/Invalid RESAC_SQLITE_SYNC/);
});

test('committed command handoff survives a process kill while a later write is uncommitted',async t=>{
  const directory=mkdtempSync(join(tmpdir(),'sqlite-kill-')),path=join(directory,'state.sqlite');
  let db;
  t.after(()=>{db?.close();rmSync(directory,{recursive:true,force:true});});
  const source=`import {Store} from ${JSON.stringify(store)};
    const s=new Store(process.env.RESAC_TEST_DB,[{id:'probe',token:'disposable'}]);
    const a=s.enqueue('probe','device.ping',{},600),b=s.enqueue('probe','device.ping',{},600);
    s.claim('probe',30000);
    const result=s.resultAndClaim('probe',a.id,'succeeded',{reply:'pong'},30000);
    s.db.exec('BEGIN IMMEDIATE');s.db.prepare("UPDATE commands SET status='failed' WHERE id=?").run(b.id);
    process.send({a:a.id,b:b.id,next:result.commands[0].id,sync:s.db.prepare('PRAGMA synchronous').get().synchronous});
    process.on('message',()=>{});`;
  const child=fork('-e',[source],{execArgv:['--input-type=module'],
    env:{...process.env,RESAC_TEST_DB:path},stdio:['ignore','ignore','pipe','ipc']});
  t.after(()=>{if(child.exitCode===null)child.kill('SIGKILL');});
  const result=await Promise.race([once(child,'message'),once(child,'exit').then(()=>{throw Error('Writer exited before reporting committed data');}),
    new Promise((_,reject)=>{const timer=setTimeout(()=>reject(Error('Writer timed out')),10000);timer.unref();})]);
  const [message]=result;assert.equal(message.next,message.b);assert.equal(message.sync,2);
  const exited=once(child,'exit');child.kill('SIGKILL');await exited;
  db=new DatabaseSync(path);
  assert.equal(db.prepare('PRAGMA quick_check').get().quick_check,'ok');
  const a=db.prepare('SELECT status,attempts FROM commands WHERE id=?').get(message.a);
  const b=db.prepare('SELECT status,attempts FROM commands WHERE id=?').get(message.b);
  assert.equal(a.status,'succeeded');assert.equal(a.attempts,1);
  assert.equal(b.status,'inflight');assert.equal(b.attempts,1);
  assert.equal(db.prepare('SELECT next_id FROM command_handoffs WHERE previous_id=?').get(message.a).next_id,message.b);
});

test('data synchronization propagates I/O failures and falls back only for unsupported operations',
  {skip:process.env.RESAC_SQLITE_FAULT_TESTS!=='1'},t=>{
  assert.equal(process.env.RESAC_SQLITE_SYNC,'data');assert.equal(process.platform,'linux');
  const directory=mkdtempSync(join(tmpdir(),'sqlite-sync-errors-'));
  t.after(()=>rmSync(directory,{recursive:true,force:true}));
  const library=join(directory,'fault.so');
  execFileSync('cc',['-shared','-fPIC','-O2','-Wall','-Wextra','-Werror',
    fileURLToPath(new URL('./fixtures/sqlite-sync-fault.c',import.meta.url)),'-o',library,'-ldl']);
  for(const error of ['EIO','ENOSYS','EINVAL','EOPNOTSUPP']){
    const path=join(directory,error+'.sqlite'),arm=join(directory,error+'.armed'),log=join(directory,error+'.log');
    const source=`import {DatabaseSync} from ${JSON.stringify(driver)};
      import {writeFileSync,unlinkSync} from 'node:fs';
      const db=new DatabaseSync(process.env.RESAC_TEST_DB);
      db.exec('PRAGMA journal_mode=WAL;PRAGMA synchronous=FULL;CREATE TABLE sample(id INTEGER PRIMARY KEY)');
      writeFileSync(process.env.RESAC_TEST_SYNC_ARM,'armed');let committed=false,code=null;
      try{db.exec('BEGIN IMMEDIATE;INSERT INTO sample VALUES(1);COMMIT');committed=true;}
      catch(error){code=error.code;try{db.exec('ROLLBACK');}catch{}}
      finally{unlinkSync(process.env.RESAC_TEST_SYNC_ARM);db.close();}
      console.log(JSON.stringify({committed,code}));`;
    const child=spawnSync(process.execPath,['--input-type=module','-e',source],{encoding:'utf8',
      env:{...process.env,LD_PRELOAD:library,RESAC_TEST_DB:path,RESAC_TEST_SYNC_ARM:arm,
        RESAC_TEST_SYNC_LOG:log,RESAC_TEST_SYNC_ERROR:error}});
    assert.equal(child.status,0,child.stderr);
    const result=JSON.parse(child.stdout),calls=readFileSync(log,'utf8');
    assert.match(calls,/fdatasync/);
    if(error==='EIO'){
      assert.equal(result.committed,false);assert.match(result.code,/IOERR/);
      assert.doesNotMatch(calls,/fsync regular/);
    }else{
      assert.equal(result.committed,true);assert.equal(result.code,null);assert.match(calls,/fsync regular/);
    }
    const db=new DatabaseSync(path);
    try{assert.equal(db.prepare('SELECT count(*) n FROM sample').get().n,error==='EIO'?0:1);
      assert.equal(db.prepare('PRAGMA quick_check').get().quick_check,'ok');}finally{db.close();}
  }
});
