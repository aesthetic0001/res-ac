# Durable SQLite synchronization experiment

Production command completion still includes substantial durable SQLite I/O.
On the backend's ext4 volume, interleaved disposable WAL commits measured
14.42/38.46 ms median/p95 with Node's built-in driver and 6.88/20.25 ms with
data synchronization. Growing WAL files showed no median improvement.
An isolated test of the actual Store enqueue/lease/result flow, with synthetic
49,000-reading and 1,800-command history, measured 20.55/73.68 ms versus
12.65/47.34 ms. These are database measurements, not end-to-end command gains.

The experimental Docker image uses a custom `better-sqlite3` binding containing
the checksum-pinned SQLite 3.53.4 amalgamation from Node 24.20.0. It enables
SQLite's supported `HAVE_FDATASYNC` option for regular files, retains `fsync`
for directories, and falls back to `fsync` only for unsupported-operation errors.
Real I/O failures propagate. Store still sets `journal_mode=WAL`,
`synchronous=FULL`, and foreign keys. Command/result commits, handoff mappings,
and responses after commit keep the existing ordering and durability rules.
The device agent, AVR, journals, and physical fan behavior are unchanged.

The image pins Node 24.22.2, matching the current production runtime. Build
tools and downloaded source remain in the build stage. The normal local default
is Node's built-in SQLite. Build and test the candidate on Linux with Node
24.15 or later, Python 3, a C/C++ compiler, and make:

```
npm ci
npm run build:sqlite
RESAC_SQLITE_SYNC=data npm test
RESAC_SQLITE_SYNC=data RESAC_SQLITE_FAULT_TESTS=1 node --test test/sqlite.test.mjs
```

For a controlled comparison, override the container environment with
`RESAC_SQLITE_SYNC=builtin`. An invalid mode or missing candidate binding fails
startup. There is no silent driver fallback. Both drivers use the same database
file format; rollback must preserve the live data volume.

Acceptance requires at least a 20% reduction in measured end-to-end command
latency, repeated controls, successful restart/retry checks, and fresh reports.
Database-only gains do not satisfy acceptance or prove the system network bound.
If the real command path misses this gate, close the draft experiment and retain
the accepted release.

References: [SQLite platform configuration](https://www.sqlite.org/compile.html),
[custom better-sqlite3 builds](https://github.com/WiseLibs/better-sqlite3/blob/v13.0.3/docs/compilation.md).
