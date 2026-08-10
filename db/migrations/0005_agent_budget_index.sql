-- 0005_agent_budget_index.sql
-- Phase 4 follow-up: index backing the per-user daily agent-message budget.
--
-- The budget is counted from app.conversation_message, NOT from app.query_log. That looks
-- like the wrong table until you notice which agent turns execute no SQL: a `clarify` or a
-- `decline` outcome costs a model call and writes zero query_log rows, so a query_log-based
-- count leaves exactly those requests unlimited -- and "ask something ambiguous in a loop"
-- is the cheapest way to spend the owner's Anthropic key. One assistant message per agent
-- turn is the unit the user is actually billed for, so that is the unit the cap counts.
--
-- The count is "this user's assistant messages since UTC midnight", which reads as
--   app.conversation (user_id) -> app.conversation_message (conversation_id, role, created_at).
-- 0004's conversation_message_conversation_idx is on (conversation_id, id) and cannot serve
-- the created_at range without reading every message in every one of the user's threads.
-- This index is partial on role='assistant' because user turns are never counted, which keeps
-- it to roughly half the table and lets the planner skip the role filter entirely.

BEGIN;

CREATE INDEX conversation_message_assistant_created_idx
    ON app.conversation_message (conversation_id, created_at)
    WHERE role = 'assistant';

COMMIT;
