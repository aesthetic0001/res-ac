// The same RAC1 value tags as command responses, with a bounded request budget.
export const requestMediaType = 'application/vnd.res-ac.value.v1';
const utf8 = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true });
export function decodeRequest(data) {
  if (!Buffer.isBuffer(data) || data.length > 32768 || data.subarray(0, 4).toString() !== 'RAC1')
    throw new Error('Invalid binary request');
  let position = 4, remaining = 8192;
  const take = size => {
    if (size > data.length - position) throw new Error('Truncated binary request');
    const result = data.subarray(position, position + size); position += size; return result;
  };
  const count = () => take(2).readUInt16BE();
  const read = depth => {
    if (depth > 16 || --remaining < 0) throw new Error('Binary request too complex');
    const tag = take(1)[0];
    if (tag === 110) return null;
    if (tag === 116) return true;
    if (tag === 102) return false;
    if (tag === 115) return utf8.decode(take(count()));
    if (tag === 105) return take(4).readInt32BE();
    if (tag === 100) {
      const value = take(8).readDoubleBE();
      if (!Number.isFinite(value)) throw new Error('Nonfinite binary request number');
      return value;
    }
    if (tag === 97) {
      const size = count();
      if (size > remaining) throw new Error('Binary request array too large');
      const result = []; for (let i = 0; i < size; i++) result.push(read(depth + 1));
      return result;
    }
    if (tag === 111) {
      const size = count();
      if (size * 2 > remaining) throw new Error('Binary request object too large');
      const result = {};
      for (let i = 0; i < size; i++) {
        const key = read(depth + 1);
        if (typeof key !== 'string' || Object.hasOwn(result, key)) throw new Error('Invalid binary request key');
        // Match JSON.parse's own data properties, including __proto__.
        Object.defineProperty(result, key, { value: read(depth + 1), enumerable: true, configurable: true, writable: true });
      }
      return result;
    }
    throw new Error('Unknown binary request tag');
  };
  const value = read(0);
  if (position !== data.length) throw new Error('Trailing binary request content');
  return value;
}
