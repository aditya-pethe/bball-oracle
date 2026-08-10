import { requireSession } from "../../../../lib/require-session";
import {
  deleteConversation,
  getConversation,
  readConversationId,
} from "../../../../lib/conversations";

/**
 * One thread: its messages, or its deletion. Both are scoped to the session's own rows.
 *
 * A conversation belonging to someone else answers 404, not 403: a 403 would confirm that the
 * id exists, which turns a sequential id space into an enumeration oracle for how many threads
 * other people have.
 */

// `params` is a promise in this Next version (node_modules/next/dist/docs — route.md,
// "context.params is now a promise" as of 15.0.0-RC).
type Ctx = { params: Promise<{ id: string }> };

export async function GET(_req: Request, ctx: Ctx) {
  const session = await requireSession();
  if (!session) {
    return Response.json({ error: "authentication required" }, { status: 401 });
  }

  const id = readConversationId((await ctx.params).id);
  if (id === null) {
    return Response.json({ error: "conversation not found" }, { status: 404 });
  }

  const found = await getConversation(session.userId, id);
  if (!found) {
    return Response.json({ error: "conversation not found" }, { status: 404 });
  }
  return Response.json(found);
}

export async function DELETE(_req: Request, ctx: Ctx) {
  const session = await requireSession();
  if (!session) {
    return Response.json({ error: "authentication required" }, { status: 401 });
  }

  const id = readConversationId((await ctx.params).id);
  if (id === null || !(await deleteConversation(session.userId, id))) {
    return Response.json({ error: "conversation not found" }, { status: 404 });
  }
  return Response.json({ deleted: id });
}
