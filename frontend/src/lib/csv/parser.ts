/**
 * Client-side CSV parser and validator for the InsightFlow feedback import.
 *
 * Matches the backend contract (backend/app/api/feedback.py + schemas).
 *   - UTF-8 encoded (we re-decode as utf-8, stripping a BOM).
 *   - Max file size 10 MiB (server-side enforced; client surfaces a friendly
 *     error before round-tripping).
 *   - Max row count 10000 (server-side enforced; client warns early).
 *   - Required header columns: feedback_id, content.
 *   - Optional header columns: source, user_id, user_type, created_at.
 *
 * The parser is RFC-4180-ish: quoted fields may contain commas and newlines,
 * and double-quotes inside quoted fields are escaped as "". Backslashes and
 * other Excel-style escapes are NOT supported on purpose — the backend will
 * reject malformed files, so the client should fail visibly too.
 */

const REQUIRED_HEADERS = ['feedback_id', 'content'] as const;
const OPTIONAL_HEADERS = ['source', 'user_id', 'user_type', 'created_at'] as const;
export const MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024; // 10 MiB
export const MAX_ROW_COUNT = 10000;

export interface ParsedFeedbackRow {
  row_number: number;
  feedback_id: string;
  content: string;
  source?: string | null;
  user_id?: string | null;
  user_type?: string | null;
  created_at?: string | null;
}

export interface CsvParseError {
  type: 'max_size' | 'empty' | 'header' | 'row' | 'too_many_rows';
  message: string;
  row_number?: number;
}

export interface CsvParseResult {
  rows: ParsedFeedbackRow[];
  headers: string[];
  errors: CsvParseError[];
}

export function parseFeedbackCsv(input: string | File): Promise<CsvParseResult> {
  return new Promise(async (resolve, reject) => {
    let text: string;
    if (typeof input === 'string') {
      text = input;
    } else {
      if (input.size > MAX_FILE_SIZE_BYTES) {
        resolve({
          rows: [],
          headers: [],
          errors: [
            {
              type: 'max_size',
              message: `文件大小 ${formatBytes(input.size)} 超过 10 MiB 上限。`,
            },
          ],
        });
        return;
      }
      try {
        text = await input.text();
      } catch (e) {
        reject(e);
        return;
      }
    }

    // Strip BOM if present.
    if (text.charCodeAt(0) === 0xfeff) {
      text = text.slice(1);
    }

    if (text.trim().length === 0) {
      resolve({
        rows: [],
        headers: [],
        errors: [{ type: 'empty', message: '文件为空，未读取到任何数据。' }],
      });
      return;
    }

    const all = parseDelimited(text, ',');
    if (all.length === 0) {
      resolve({
        rows: [],
        headers: [],
        errors: [{ type: 'empty', message: '文件为空，未读取到任何数据。' }],
      });
      return;
    }

    const headerRow = all[0]!.map((h) => h.trim());
    const missing = REQUIRED_HEADERS.filter((h) => !headerRow.includes(h));
    if (missing.length > 0) {
      resolve({
        rows: [],
        headers: headerRow,
        errors: [
          {
            type: 'header',
            message: `缺少必填列：${missing.join(', ')}。`,
          },
        ],
      });
      return;
    }

    // Unknown header → warn by ignoring. Backend will accept unknown extras
    // silently for forward-compat, but per the contract only the optional
    // columns below will be picked up.
    const allowed = new Set<string>([...REQUIRED_HEADERS, ...OPTIONAL_HEADERS]);
    for (const h of headerRow) {
      if (!allowed.has(h)) {
        // extra columns are tolerated; downstream upload will still succeed
      }
    }

    const rows: ParsedFeedbackRow[] = [];
    const errors: CsvParseError[] = [];

    for (let i = 1; i < all.length; i += 1) {
      const row = all[i]!;
      const rowNumber = i + 1; // 1-indexed (header is row 1)
      if (row.length === 1 && row[0]!.trim() === '') {
        continue; // blank trailing line
      }
      if (rows.length >= MAX_ROW_COUNT) {
        errors.push({
          type: 'too_many_rows',
          message: `数据行数超过 ${MAX_ROW_COUNT} 上限，仅展示前 ${MAX_ROW_COUNT} 行。`,
        });
        break;
      }

      const record: Record<string, string> = {};
      for (let j = 0; j < headerRow.length; j += 1) {
        const key = headerRow[j]!;
        const val = (row[j] ?? '').trim();
        record[key] = val;
      }

      const feedback_id = record['feedback_id'] ?? '';
      const content = record['content'] ?? '';
      if (!feedback_id || !content) {
        errors.push({
          type: 'row',
          message: `第 ${rowNumber} 行缺少 feedback_id 或 content。`,
          row_number: rowNumber,
        });
        continue;
      }
      rows.push({
        row_number: rowNumber,
        feedback_id,
        content,
        source: record['source'] || null,
        user_id: record['user_id'] || null,
        user_type: record['user_type'] || null,
        created_at: record['created_at'] || null,
      });
    }

    resolve({ rows, headers: headerRow, errors });
  });
}

/* ---------- internal ---------- */

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}

/**
 * Parse a delimited string (RFC4180-style quoting) into an array of rows.
 * Exported for tests; production code should use parseFeedbackCsv().
 */
export function parseDelimited(input: string, delim: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = '';
  let inQuotes = false;
  for (let i = 0; i < input.length; i += 1) {
    const ch = input[i]!;
    if (inQuotes) {
      if (ch === '"') {
        if (input[i + 1] === '"') {
          field += '"';
          i += 1;
        } else {
          inQuotes = false;
        }
      } else {
        field += ch;
      }
      continue;
    }
    if (ch === '"') {
      inQuotes = true;
      continue;
    }
    if (ch === delim) {
      row.push(field);
      field = '';
      continue;
    }
    if (ch === '\n' || ch === '\r') {
      row.push(field);
      rows.push(row);
      row = [];
      field = '';
      if (ch === '\r' && input[i + 1] === '\n') {
        i += 1;
      }
      continue;
    }
    field += ch;
  }
  // flush trailing field/row
  if (field.length > 0 || row.length > 0) {
    row.push(field);
    rows.push(row);
  }
  // drop trailing completely empty rows that result from a final newline
  while (rows.length > 0) {
    const last = rows[rows.length - 1]!;
    if (last.length === 1 && last[0] === '') {
      rows.pop();
    } else {
      break;
    }
  }
  return rows;
}
