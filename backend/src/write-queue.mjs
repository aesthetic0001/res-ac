import { AsyncResource } from 'node:async_hooks';

// Coalesce work already ready this turn. Never hold a SQLite transaction
// across an await, or wait for a timer/count to fill a batch.
export class WriteQueue {
  constructor(store, { enabled = true, observability } = {}) {
    this.store = store;
    this.enabled = enabled;
    this.observability = observability;
    this.pending = [];
    this.scheduled = null;
    this.closed = false;
  }
  run(fn) {
    if (this.closed || this.pending.length >= 128)
      return Promise.reject(Object.assign(new Error('Writes are temporarily unavailable'), { status: 503 }));
    if (typeof fn !== 'function' || fn.constructor.name === 'AsyncFunction')
      return Promise.reject(new TypeError('Write callbacks must be synchronous'));
    if (!this.enabled) {
      try { return Promise.resolve(this.store.transaction(fn)); }
      catch (error) { return Promise.reject(error); }
      finally { this.recordSize(1); }
    }
    const scope = new AsyncResource('resac-write');
    let span;
    try { span = this.observability?.span('db.write_wait', { 'db.system.name': 'sqlite' }); } catch {}
    return new Promise((resolve, reject) => {
      this.pending.push({ fn, scope, span, resolve, reject });
      this.schedule();
    });
  }
  schedule() {
    if (this.scheduled === null)
      this.scheduled = setImmediate(() => { this.scheduled = null; this.flush(); });
  }
  recordSize(size) {
    try { this.observability?.writeBatchSize?.observe(size); } catch {}
  }
  flush() {
    if (!this.pending.length) return;
    const jobs = this.pending.splice(0, 32);
    const results = [];
    let failure;
    try {
      // Keep the physical transaction in the first request's trace. Other
      // requests retain their own context and a wait span through this commit.
      jobs[0].scope.runInAsyncScope(() => this.store.transaction(() => {
        try { this.observability?.local?.getStore()?.setAttribute('db.batch.size', jobs.length); } catch {}
        if (jobs.length === 1) {
          const value = jobs[0].fn();
          if (value && typeof value.then === 'function') throw new TypeError('Write callbacks cannot return promises');
          results.push({ value });
          return;
        }
        for (let i = 0; i < jobs.length; i++) {
          const job = jobs[i];
          const execute = () => {
            try { return { value: this.store.savepoint(job.fn) }; }
            catch (error) {
              // Expected request rejection is isolated by its savepoint. A
              // storage/programming error aborts the entire physical commit.
              if (Number.isInteger(error.status) && error.status >= 400 && error.status < 500)
                return { error };
              throw error;
            }
          };
          results.push(i === 0 ? execute() : job.scope.runInAsyncScope(execute));
        }
      }));
    } catch (error) { failure = error; }
    this.recordSize(jobs.length);
    for (let i = 0; i < jobs.length; i++) {
      const job = jobs[i], error = failure || results[i]?.error;
      try {
        job.span?.setAttribute('db.batch.size', jobs.length);
        job.span?.setStatus({ code: error ? 2 : 1 });
        job.span?.end();
      } catch {} // Optional diagnostics cannot hide a committed result.
      job.scope.emitDestroy();
      if (error) job.reject(error);
      else job.resolve(results[i].value);
    }
    if (this.pending.length) this.schedule();
  }
  close() {
    this.closed = true;
    // Drain validated work before the server closes its database. No new job
    // can enter; each flush still has the same bounded transaction size.
    if (this.scheduled !== null) { clearImmediate(this.scheduled); this.scheduled = null; }
    while (this.pending.length) {
      this.flush();
      if (this.scheduled !== null) { clearImmediate(this.scheduled); this.scheduled = null; }
    }
  }
}
