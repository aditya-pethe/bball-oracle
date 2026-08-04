import { parse } from "libpg-query";

export type ValidationResult = { ok: true } | { ok: false; reason: string };

const MAX_SQL_LENGTH = 20_000;

function findViolation(node: unknown): string | null {
  if (Array.isArray(node)) {
    for (const item of node) {
      const violation = findViolation(item);
      if (violation) return violation;
    }
    return null;
  }
  if (node === null || typeof node !== "object") return null;

  for (const [key, value] of Object.entries(node as Record<string, unknown>)) {
    if (key.endsWith("Stmt") && key !== "SelectStmt") {
      return `statement type ${key} is not allowed`;
    }
    if (key === "SelectStmt") {
      const select = value as Record<string, unknown>;
      if (select.lockingClause) {
        return "SELECT ... FOR UPDATE/SHARE/NO KEY UPDATE/KEY SHARE (locking clause) is not allowed";
      }
      if (select.intoClause) {
        return "SELECT INTO is not allowed";
      }
    }
    const violation = findViolation(value);
    if (violation) return violation;
  }
  return null;
}

export async function validateSql(sql: string): Promise<ValidationResult> {
  if (sql.length > MAX_SQL_LENGTH) {
    return { ok: false, reason: "query is too long" };
  }
  if (sql.trim().length === 0) {
    return { ok: false, reason: "query is empty" };
  }

  let result;
  try {
    result = await parse(sql);
  } catch (error) {
    // Only the parser's own SqlError means invalid SQL; anything else (e.g. the
    // WASM module failing to load) is an infrastructure fault that must surface,
    // not be reported to the user as a problem with their query.
    if (error instanceof Error && error.name === "SqlError") {
      return { ok: false, reason: error.message };
    }
    throw error;
  }

  const stmts = result.stmts;
  if (!stmts || stmts.length !== 1) {
    return { ok: false, reason: "only a single SELECT statement is allowed" };
  }

  const stmt = stmts[0].stmt as Record<string, unknown> | undefined;
  if (!stmt || !("SelectStmt" in stmt)) {
    const statementType = stmt ? Object.keys(stmt)[0] : "unknown";
    return { ok: false, reason: `statement type ${statementType} is not allowed` };
  }

  const violation = findViolation(stmt);
  if (violation) {
    return { ok: false, reason: violation };
  }

  return { ok: true };
}
