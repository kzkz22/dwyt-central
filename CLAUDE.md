# dwyt-central

The **dwyt** central service: web UI, sign-in (Google OIDC + invitations, admin fallback with password + TOTP),
per-user libraries (strictly separated; PostgreSQL row-level security as a second line), search
(full text + pg_trgm + pgvector with bge-m3 on CPU), YouTube subscriptions inbox, job queue for runners, MCP
with per-user tokens. It never holds users' API keys and never calls a runner: runners connect out
(HTTPS long-poll) and claim jobs by their declared capabilities.

Status: rounds 1–5 implemented (sign-in, invitations, RLS library, runner protocol, captions/cloud/local
transcription, summary and translate jobs; search: `indexing.py` shared chunks + indexer thread in the web process,
`search.py` FTS + pg_trgm + pgvector fused by RRF, `embedder.py` = separate `dwyt-central-embed` service;
`reuse.py`: shareable results taken over at submit; `ownfiles.py`: browser uploads staged until the runner
has them, runner watched folders via `/runner/v1/local-items`, quotas; 3c: `items.py` main transcript / removal /
summary versions / bulk backfill, `render.py` summary Markdown subset, TXT/SRT/MD downloads, re-transcription,
LLM models chosen from the runners' `llm_models` capability, job progress on the item page, unreferenced shared
results dropped by `indexing.drop_unreferenced_results`); 4a: `subscriptions.py` (shared channels/feed videos,
per-user subscriptions + inbox, fan-out, the captions → local → cloud chain via `chain_step` in `jobs.fail`,
`Background` thread), `youtube.py` (RSS, Shorts, handles, Takeout, Web OAuth youtube.readonly), `secretbox.py`
(TOKEN_KEY Fernet for refresh tokens); 4b: `notify.py` (per-user ntfy / Home Assistant, secrets sealed, outbox +
retries + daily digest in the `Background` thread, user URLs never private unless NOTIFY_PRIVATE_NETS or admin).
4c: `mcp_server.py` (/mcp, stateless Streamable HTTP, per-user hashed tokens with can_start /
include_private, caller in a contextvar, two SDK servers by permission, rate limit per token), `submit.py` (shared
submit / re-transcribe logic for web and MCP); round 5: `personal.py` (tags, note, speaker names applied at display/
download/export/MCP), `export.py` (ZIP: Markdown or JSON), account deletion (`accounts.delete_user`), own TOTP after
Google (`totp_after_google`, REQUIRE_TOTP, `/belepes/kod`, `/belepes/kod-beallitas`), `mailer.py` (SMTP invites).
Open: passkeys (waiting for the owner's decision).
Source of truth: `dwyt-docs` (`docs/terv.md`, `docs/protokoll.md`). Install: `docs/telepites.md`; tests need PostgreSQL
(throw-away cluster from tests/pgcluster.py, or TEST_DATABASE_URL as superuser).
License: AGPL-3.0.

## Conventions

- The owner writes in Hungarian. User-facing docs (`README.md`, `docs/`) and any UI are Hungarian;
  code, comments and this file are English.
- The plan and the decision log live in `dwyt-docs` (`docs/terv.md`, section 21). Read the relevant
  sections before changing anything; when a decision changes, update it there in the same piece of work.
- The central–runner protocol is specified in `dwyt-docs/docs/protokoll.md`; both sides follow it.
- Never commit tokens or keys (API keys, OAuth secrets, runner/MCP tokens). Secrets live in env files or
  the runner's local store, never in the repo.
- Add a test for every behaviour change.
- The old system (`yt-transcribe`, `yt-transcribe-webui`) stays separate and unchanged; code may be
  ported from it, but nothing here may depend on it at runtime.
