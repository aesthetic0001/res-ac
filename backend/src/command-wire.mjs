// Versioned, bounded JSON values for old devices whose JSON decoder is slow.
// This is only a response representation; command validation is unchanged.
export const commandMediaType = 'application/vnd.res-ac.command.v1';
export function encodeCommand(value) {
  const parts = [Buffer.from('RAC1')];
  let size = 4, remaining = 1024;
  const add = part => {
    size += part.length;
    if (size > 32768) throw new Error('Command response too large');
    parts.push(part);
  };
  const count = length => {
    if (length > 65535) throw new Error('Command field too large');
    const buffer = Buffer.allocUnsafe(2); buffer.writeUInt16BE(length); add(buffer);
  };
  const write = (item, depth) => {
    if (depth > 16 || --remaining < 0) throw new Error('Command response too complex');
    if (item === null) return add(Buffer.from('n'));
    if (typeof item === 'boolean') return add(Buffer.from(item ? 't' : 'f'));
    if (typeof item === 'string') {
      if (!item.isWellFormed()) throw new Error('Command string is not valid Unicode');
      const text = Buffer.from(item, 'utf8');
      add(Buffer.from('s')); count(text.length); return add(text);
    }
    if (typeof item === 'number' && Number.isFinite(item)) {
      const integer = Number.isInteger(item) && item >= -2147483648 && item <= 2147483647 && !Object.is(item, -0);
      const buffer = Buffer.allocUnsafe(integer ? 5 : 9);
      buffer[0] = integer ? 105 : 100;
      if (integer) buffer.writeInt32BE(item, 1); else buffer.writeDoubleBE(item, 1);
      return add(buffer);
    }
    if (Array.isArray(item)) {
      if (item.length > remaining) throw new Error('Command array too large');
      add(Buffer.from('a')); count(item.length);
      for (const child of item) write(child, depth + 1);
      return;
    }
    if (item && Object.getPrototypeOf(item) === Object.prototype) {
      const entries = Object.entries(item);
      if (entries.length * 2 > remaining) throw new Error('Command object too large');
      add(Buffer.from('o')); count(entries.length);
      for (const [key, child] of entries) { write(key, depth + 1); write(child, depth + 1); }
      return;
    }
    throw new Error('Unsupported command value');
  };
  write(value, 0);
  return Buffer.concat(parts, size);
}
