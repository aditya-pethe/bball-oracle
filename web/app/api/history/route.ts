import { requireSession } from "../../../lib/require-session";
import { getAppPool } from "../../../lib/execute-query";

type QueryLogRow = {
  query_text: string;
  status: string;
  started_at: Date;
  duration_ms: number | null;
  row_count: number | null;
  truncated: boolean;
};

export async function GET() {
  const session = await requireSession();
  if (!session) {
    return Response.json({ error: "authentication required" }, { status: 401 });
  }

  const { rows } = await getAppPool().query<QueryLogRow>(
    `SELECT query_text, status, started_at, duration_ms, row_count, truncated
       FROM app.query_log
      WHERE user_id = $1
      ORDER BY started_at DESC, id DESC
      LIMIT 50`,
    [session.userId],
  );

  return Response.json({
    queries: rows.map((row) => ({
      sql: row.query_text,
      status: row.status,
      startedAt: row.started_at.toISOString(),
      durationMs: row.duration_ms,
      rowCount: row.row_count,
      truncated: row.truncated,
    })),
  });
}
