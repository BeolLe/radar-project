import "server-only";
import { Pool, type QueryResultRow } from "pg";
import taxonomy from "../../taxonomy.json";

const state = globalThis as typeof globalThis & { radarPool?: Pool };
export const tagNames: Record<string, string> = taxonomy.tags;
export const signalNames: Record<string, string> = {
  first_seen: "최초 관측", reentry: "재진입", improved: "상승", consecutive_improvement: "연속 상승",
};
export type Tag = { code: string; confidence: number };
export type Ranking = QueryResultRow & {
  id: string; name: string; value: number; signals: string[]; tags: Tag[] | null; phase: string | null;
};

export async function query<T extends QueryResultRow>(sql: string, values: unknown[] = []): Promise<T[]> {
  if (!process.env.DATABASE_URL) throw new Error("DATABASE_URL is not configured");
  state.radarPool ??= new Pool({
    connectionString: process.env.DATABASE_URL,
    max: 5, connectionTimeoutMillis: 3000, idleTimeoutMillis: 10000,
    options: "-c default_transaction_read_only=on -c statement_timeout=5000",
  });
  return (await state.radarPool.query<T>(sql, values)).rows;
}

export type Params = Record<string, string | string[] | undefined>;
export function one(params: Params, key: string): string {
  const value = params[key];
  return typeof value === "string" ? value : "";
}

export function filters(params: Params) {
  const kind = one(params, "kind") === "daily" ? "daily" : "weekly";
  const rawLocation = one(params, "location");
  const location = kind === "weekly" ? "WORLD" : (/^[A-Z]{2}$/.test(rawLocation) ? rawLocation : "WORLD");
  return { kind, location };
}
