# Usage and billing data privacy

This guide describes the implementation at revision `0e37c6d` and the boundary
for the planned Phase 10 usage ledger. It is not a provider retention policy.

## Current implementation and the planned ledger

There is **no separate usage/billing ledger implemented yet**: no ledger model,
migration, writer, export or deletion endpoint is registered. The ledger,
`/observability/*` endpoints and `LEDGER_*` settings described in
[Observability](observability.md) are design proposals, not active features.

**Prompts and completions must never be stored in the usage ledger.** This is
the privacy requirement for that planned metadata-only store, not a claim that
the application never stores conversation content. Today, the
[`Message` model](../app/models/message.py) stores user prompts and assistant
responses verbatim in `content`, alongside token counters. The separate
[`Conversation` model](../app/models/conversation.py) can also store a system
prompt, title, provider/model and generation settings.

## What usage data is stored

| Data | Current storage and meaning |
| --- | --- |
| `prompt_tokens`, `completion_tokens`, `total_tokens` | Nullable integer columns on `messages`; associated with a conversation through `conversation_id`. User messages and older rows may have no counts. |
| Message identity and time | Message ID, role and `created_at`; the conversation identifies its owning user and optional workspace. Usage is therefore attributable, not anonymous. |
| Daily and overall totals | Computed from existing messages on read, not an independent retained billing record. Deleting a conversation changes these totals. |
| Prices and estimated costs | [`pricing.py`](../app/services/pricing.py) defines estimated provider/model rates and calculation helpers; it is not an invoice or a vendor billing source of truth. |

Counts use provider-reported usage where available. The
[token-usage helpers](../app/services/token_usage.py) estimate missing values
from text (roughly four characters per token); the UI streaming path estimates
from prompt/reply text. These estimates do not establish an amount charged by a
provider. Processing text to count tokens is distinct from storing that text in
a future ledger.

The [provider retry wrapper](../app/services/providers/retry.py) also emits
`chat.provider_call` events through [structured logging](../app/services/chat_audit.py).
These contain provider, status, retries and latency, plus `token_count` for a
successful non-streaming call, or stream/error metadata where applicable. The
logger adds a timestamp and request ID; these provider calls leave `user_id`
null. These call sites do not pass prompts or replies. Logging defaults to enabled
at INFO via `CHAT_AUDIT_LOG_ENABLED` and `CHAT_AUDIT_LOG_LEVEL`, writing to stderr.
Operators control access to any collected logs and their retention; conversation
deletion does not remove those logs. They are not a billing ledger.

## Who can see it

The registered routes are in [`chat/routes.py`](../app/chat/routes.py) and
[`chat/api.py`](../app/chat/api.py):

| Surface | Scope and returned data |
| --- | --- |
| `GET /api/usage` | Authenticated user's totals across their own conversations. No message bodies. |
| `GET /chat/api/usage/daily?days=30` | Authenticated user's daily counters; default 30 days, clamped to 1-365. No message bodies. This is a query window, **not a retention period**. |
| `GET /api/conversations/<id>` | Owner-only conversation, messages and token totals. |
| `GET /chat/conversations/<id>` | Owner or an explicitly shared recipient; returns message content, per-message counters and conversation totals. |
| `GET /chat/shared/<token>` | A valid, unexpired read-only share link exposes message history (role, content and time), not token counters. Treat the link as access to the conversation text. |

An optional workspace groups a conversation; it does not grant every workspace
member access to the owner's personal usage totals. The usage queries filter by
`Conversation.user_id`; there is no workspace-wide billing role or usage-ledger
admin API. Application route restrictions do not prevent a deployment operator
with database or backup access from reading stored data.

## Retention

There is no automatic expiry, usage retention duration or scheduled purge for
message token counts. They remain with their messages until the conversation is
deleted. Neither the daily query's 30-day default nor the proposed
`LEDGER_MAX_RECORDS` setting deletes these rows.

`PROMPT_VERSION_RETENTION_DAYS` applies to history of deleted **prompt-library
entries**, not chat messages or usage counters. No ledger-specific retention
or deletion guarantee can be made until its storage and cleanup are implemented.
Deployment backup and log retention must be established by the operator; this
repository does not enforce their expiry.

## Export

Owners can download `GET /chat/conversations/<id>/export` as JSON. It includes
conversation metadata and message objects, including `content` and
`token_usage`; it is **not a metadata-only usage export**. The owner's
`.../export.md` endpoint exports conversation text and document metadata, not a
usage ledger. Both use the owner check, even when a recipient can read the
conversation through sharing.

For counters without message text, authenticated users can save the JSON from
`/api/usage` or `/chat/api/usage/daily`. There is no separate ledger CSV/JSON
export, workspace billing export or invoice download implemented. Exported
files are outside application retention controls; deleting a conversation does
not remove copies someone already downloaded.

## Deletion and its limits

An owner can call `DELETE /chat/conversations/<id>` or
`DELETE /api/conversations/<id>`. The conversation's SQLAlchemy relationship
cascades deletion to its messages and their token counters. Subsequent aggregate
usage queries no longer count those messages. Another user cannot delete the
conversation merely because it was shared with them.

Deleting a workspace does not mean deleting its conversations: the conversation
model's workspace foreign key uses `SET NULL`, preserving the conversation and
its counts. There is no independent counter-only deletion or ledger erasure API.

Conversation deletion is not erasure from every system:

- The `/chat` deletion route records a separate audit event with actor, target
  and conversation title. [`audit.py`](../app/services/audit.py) appends those
  records; the [admin audit viewer](../app/admin/routes.py) provides no deletion
  route. The `/api` deletion route does not create that same audit event.
- The separate [LLM response cache](../app/services/llm_cache.py) may contain
  reply content in process memory. It is user-scoped, with TTL/LRU eviction
  (defaults: 300 seconds, 256 entries), and conversation deletion does not
  explicitly flush it. It is not the usage ledger.
- Backups, prior exports, recipients' copies and provider-held records are not
  purged by these conversation endpoints. An operator must address their own
  copies and consult the configured provider's policy separately.

## Implementation references and verification

The stored counters are introduced by
[`d8e9f0a1b2c3_add_message_token_usage.py`](../migrations/versions/d8e9f0a1b2c3_add_message_token_usage.py).
Existing [token-usage tests](../tests/test_token_usage.py) exercise persistence,
estimates, daily aggregation and user isolation;
[chat API tests](../tests/test_chat_api.py) cover owner-scoped access and deletion.
No runtime or storage behavior is changed by this guide. When the Phase 10
ledger lands, update this document against its actual schema, authorization,
export and cleanup code before presenting any proposed retention as a guarantee.
