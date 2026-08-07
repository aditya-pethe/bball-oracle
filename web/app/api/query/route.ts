import { requireSession } from "../../../lib/require-session";
import { queryResponse, readSql, runUserSql } from "../../../lib/run-user-sql";

function clientIpFrom(req: Request): string | null {
  const forwarded = req.headers.get("x-forwarded-for");
  if (!forwarded) return null;
  const first = forwarded.split(",")[0].trim();
  return first || null;
}

export async function POST(req: Request) {
  const session = await requireSession();
  if (!session) {
    return Response.json({ error: "authentication required" }, { status: 401 });
  }

  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return Response.json(
      { error: "request body must be JSON: { \"sql\": \"...\" }" },
      { status: 400 },
    );
  }
  const sql = readSql(body);
  if (sql === null) {
    return Response.json({ error: "missing sql" }, { status: 400 });
  }

  // Hardcoded, not read from `body`: this is the human-editor door, and that is the only
  // thing it is ever allowed to claim.
  return queryResponse(
    await runUserSql({
      sql,
      userId: session.userId,
      clientIp: clientIpFrom(req),
      source: "editor",
    }),
  );
}
