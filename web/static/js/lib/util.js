/** 纯函数工具：转义、格式化、HEX、S7 地址、数值解码。 */

export function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]))
}

export function clampInt(value, min, max, fallback) {
  const number = Number.parseInt(value, 10)
  if (!Number.isFinite(number)) return fallback
  return Math.min(max, Math.max(min, number))
}

export function fmtTime(ts) {
  if (!ts) return '--:--:--'
  const date = new Date(Number(ts) * 1000)
  const pad = (n, width = 2) => String(n).padStart(width, '0')
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.${pad(date.getMilliseconds(), 3)}`
}

export function fmtDuration(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  if (h) return `${h}h ${String(m).padStart(2, '0')}m ${String(s).padStart(2, '0')}s`
  if (m) return `${m}m ${String(s).padStart(2, '0')}s`
  return `${s}s`
}

export function fmtBytes(value) {
  const n = Number(value) || 0
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(2)} MB`
}

export function hexPreview(bytes, limit = 48) {
  const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes || [])
  const shown = Array.from(view.slice(0, limit), (b) => b.toString(16).padStart(2, '0').toUpperCase())
  return shown.join(' ') + (view.length > limit ? ' …' : '')
}

export function hexFromBytes(bytes) {
  return Array.from(bytes || [], (b) => b.toString(16).padStart(2, '0')).join('')
}

/** 解析用户输入的 HEX；返回 Uint8Array，非法时抛错。 */
export function parseHex(text) {
  const cleaned = String(text ?? '')
    .replace(/0x/gi, '')
    .replace(/[\s:\-,_]/g, '')
  if (!cleaned) throw new Error('HEX 数据不能为空')
  if (cleaned.length % 2 !== 0) throw new Error('HEX 长度必须是偶数（每字节两位）')
  if (!/^[0-9a-fA-F]+$/.test(cleaned)) throw new Error('HEX 含非法字符')
  const out = new Uint8Array(cleaned.length / 2)
  for (let i = 0; i < out.length; i += 1) {
    out[i] = Number.parseInt(cleaned.slice(i * 2, i * 2 + 2), 16)
  }
  return out
}

export function areaLabel(area, db = 0) {
  return String(area).toUpperCase() === 'DB' ? `DB${db}` : String(area).toUpperCase()
}

export function byteAddressLabel(area, db, offset) {
  const name = String(area).toUpperCase()
  if (name === 'DB') return `DB${db}.DBB${offset}`
  return `${name}${offset}`
}

export function bitAddressLabel(area, db, byte, bit) {
  const name = String(area).toUpperCase()
  if (name === 'DB') return `DB${db}.DBX${byte}.${bit}`
  return `${name}${byte}.${bit}`
}

function f32Text(value) {
  if (!Number.isFinite(value)) return '—'
  if (value === 0) return '0'
  if (Math.abs(value) >= 1e7 || Math.abs(value) < 1e-4) return value.toExponential(4)
  return value.toFixed(4).replace(/0+$/, '').replace(/\.$/, '')
}

/** 按 S7 大端顺序解码 16 位表。 */
export function decode16(bytes, startOffset = 0) {
  const rows = []
  for (let i = 0; i + 1 < bytes.length; i += 2) {
    const u16 = (bytes[i] << 8) | bytes[i + 1]
    const i16 = u16 >= 0x8000 ? u16 - 0x10000 : u16
    rows.push({
      offset: startOffset + i,
      hex: `${u16.toString(16).padStart(4, '0').toUpperCase()}`,
      u16,
      i16,
    })
  }
  return rows
}

/** 按 S7 大端顺序解码 32 位表（含 IEEE-754 REAL）。 */
export function decode32(bytes, startOffset = 0) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  const rows = []
  for (let i = 0; i + 3 < bytes.length; i += 4) {
    const u32 = view.getUint32(i, false)
    const i32 = view.getInt32(i, false)
    const f32 = view.getFloat32(i, false)
    rows.push({
      offset: startOffset + i,
      hex: u32.toString(16).padStart(8, '0').toUpperCase(),
      u32,
      i32,
      f32: f32Text(f32),
    })
  }
  return rows
}

export function downloadText(filename, text) {
  const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    return false
  }
}
