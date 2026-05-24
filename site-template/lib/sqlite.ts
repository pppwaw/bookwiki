import Database from 'better-sqlite3';
import fs from 'node:fs';
import path from 'node:path';

type SqliteValue = string | number | null;

export function sqlitePath() {
  return path.join(/* turbopackIgnore: true */ process.cwd(), '.bookwiki', 'bookwiki.sqlite');
}

export function openReadonlyDb() {
  const file = sqlitePath();

  if (!fs.existsSync(file)) {
    throw new Error(`BookWiki SQLite database not found at ${file}`);
  }

  return new Database(file, { readonly: true, fileMustExist: true });
}

export function queryAll<T>(sql: string, params: SqliteValue[] = []) {
  const db = openReadonlyDb();

  try {
    return db.prepare(sql).all(...params) as T[];
  } finally {
    db.close();
  }
}
