import Database from 'better-sqlite3';
import fs from 'node:fs';
import path from 'node:path';

type SqliteValue = string | number | null;

export function sqlitePath() {
  return path.join(/* turbopackIgnore: true */ process.cwd(), '.bookwiki', 'bookwiki.sqlite');
}

let cached: Database.Database | null = null;

export function openReadonlyDb() {
  if (cached) return cached;

  const file = sqlitePath();
  if (!fs.existsSync(file)) {
    throw new Error(`BookWiki SQLite database not found at ${file}`);
  }

  cached = new Database(file, { readonly: true, fileMustExist: true });
  return cached;
}

export function queryAll<T>(sql: string, params: SqliteValue[] = []) {
  return openReadonlyDb().prepare(sql).all(...params) as T[];
}
