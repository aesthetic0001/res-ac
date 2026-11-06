// Reproducible Linux-only experiment. Retain FULL commits and directory fsync.
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync, mkdirSync, copyFileSync, existsSync } from 'node:fs';
import { dirname, resolve, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';

if (process.platform !== 'linux') throw new Error('SQLite data-sync build requires Linux');
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const sourceDir = join(root, 'native/source');
mkdirSync(sourceDir, { recursive: true });
// This is the same SQLite amalgamation as the measured Node 24 build.
const sourceRoot = 'https://raw.githubusercontent.com/nodejs/node/v24.20.0/deps/sqlite/';
const expected = {
  'sqlite3.c': 'b1dd5d74ec7f29055a6684fa06fb3c2f6821c87dd38f9a458dfd2e8a1db28189',
  'sqlite3.h': '919e7f2e8ed1d8f56ac17b412b8971c76aa5d1a879752cc6058f75e7d5910e1d',
  'sqlite3ext.h': 'ac9645e5c9ff0cf176efdd6e75cb5e98f46295d38e02db5c4d208826a39ab4be'
};
const hash = data => createHash('sha256').update(data).digest('hex');
for (const [name, digest] of Object.entries(expected)) {
  const response = await fetch(sourceRoot + name, { signal: AbortSignal.timeout(60_000) });
  if (!response.ok) throw new Error('SQLite source download failed');
  const data = Buffer.from(await response.arrayBuffer());
  if (hash(data) !== digest) throw new Error('SQLite source checksum differs: ' + name);
  writeFileSync(join(sourceDir, name), data);
}
const path = join(sourceDir, 'sqlite3.c');
let source = readFileSync(path, 'utf8');
const original = '  rc = fdatasync(fd);';
if (source.split(original).length !== 2) throw new Error('SQLite sync implementation changed');
source = source.replace(original, `  {
    struct stat syncStat;
    if( osFstat(fd, &syncStat)==0 && S_ISREG(syncStat.st_mode) ){
      rc = fdatasync(fd);
      if( rc==-1 && (errno==ENOSYS || errno==EINVAL || errno==EOPNOTSUPP) ){
        rc = fsync(fd);
      }
    }else{
      rc = fsync(fd);
    }
  }`);
source = '#define HAVE_FDATASYNC 1\n#define SQLITE_THREADSAFE 1\n#define SQLITE_DEFAULT_MEMSTATUS 0\n#define SQLITE_DQS 0\n' + source;
writeFileSync(path, source);
const nodeRoot = resolve(dirname(process.execPath), '..');
const args = [join(root, 'node_modules/node-gyp/bin/node-gyp.js'), 'rebuild', '--release',
  '--force_build=1', '--jobs=2', '--sqlite3=' + sourceDir];
if (existsSync(join(nodeRoot, 'include/node/node.h'))) args.push('--nodedir=' + nodeRoot);
execFileSync(process.execPath, args, { cwd: join(root, 'node_modules/better-sqlite3'), stdio: 'inherit' });
const binding = join(root, 'native/sqlite.node');
copyFileSync(join(root, 'node_modules/better-sqlite3/build/Release/better_sqlite3.node'), binding);
writeFileSync(join(root, 'native/build.json'), JSON.stringify({ sqlite: '3.53.4',
  sourceRoot, sourceHashes: expected, patchedSourceSha256: hash(source),
  bindingSha256: hash(readFileSync(binding)), directorySync: 'fsync', regularFileSync: 'fdatasync',
  fallbackErrnos: ['ENOSYS', 'EINVAL', 'EOPNOTSUPP'] }, null, 2) + '\n');
