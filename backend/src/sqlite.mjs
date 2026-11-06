import { DatabaseSync as BuiltinDatabase } from 'node:sqlite';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

// Keep the standard driver available for controlled before/after trials.
const mode = process.env.RESAC_SQLITE_SYNC || 'builtin';
if (!['builtin', 'data'].includes(mode)) throw new Error('Invalid RESAC_SQLITE_SYNC');
let Database = BuiltinDatabase;
if (mode === 'data') {
  if (process.platform !== 'linux') throw new Error('The data-sync experiment requires Linux');
  const require = createRequire(import.meta.url), BetterDatabase = require('better-sqlite3');
  const nativeBinding = fileURLToPath(new URL('../native/sqlite.node', import.meta.url));
  Database = function(path) { return new BetterDatabase(path, { nativeBinding }); };
}
export const DatabaseSync = Database;
