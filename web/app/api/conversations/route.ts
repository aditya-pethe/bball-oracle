import { requireSession } from "../../../lib/require-session";
import { createConversation, listConversations, titleFrom } from "../../../lib/conversations";

/**
 * The agent tab's thread list. Session-gated and scoped to the caller's own rows in SQL, the
 * same ownership shape as `/api/history` — a signed-in user can never name someone else's
 * conversation id and read it, because the id is never the only filter.
 */

export async function GET() {
  const session = await requireSession();
  if (!session) {
    return Response.json({ error: "authentication required" }, { status: 401 });
  }
  return Response.json({ conversations: await listConversations(session.userId) });
}

export async function POST(req: Request) {
  const session = await requireSession();
  if (!session) {
    return Response.json({ error: "authentication required" }, { status: 401 });
  }

  let body: unknown = {};
  try {
    body = await req.json();
  } catch {
    // An empty POST is the common case (the rail's "+ New"), so a missing/unparseable body
    // means "no title", not an error.
  }

  const raw = (body as { title?: unknown })?.title;
  const title = typeof raw === "string" && raw.trim() !== "" ? titleFrom(raw) : null;

  return Response.json(
    { conversation: await createConversation(session.userId, title) },
    { status: 201 },
  );
}
