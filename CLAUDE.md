# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

subi-neko is an automated subtitle translation pipeline for anime MKV files. It extracts subtitles, translates them via an OpenAI-compatible LLM (cheap model), reworks every line with a full-coverage "polish" pass (better model), runs deterministic final checks (Czech gender agreement for speaker and addressee, T–V mixing, readability with auto line-breaking of over-long rows via `app/subs/line_breaking.py` — which picks the break point by a cost function weighing row balance against Czech phrase structure, not balance alone — untranslated English, and a no-LLM drift check on what the polish pass rewrote), and muxes the finished subtitles back into the container — all driven by a FastAPI backend + React web UI. See `README.md` for the full user-facing feature list, options reference, and pipeline diagram — don't duplicate that here.

Stack: Python/FastAPI/SQLAlchemy (async) + SQLite backend, React 19/TypeScript/Vite/Mantine frontend, single Docker image (FastAPI serves the built frontend as static files).

## Commands

### Backend (from `backend/`)

```bash
python -m venv .venv && .venv\Scripts\activate   # Windows; source .venv/bin/activate on Linux/macOS
pip install -r requirements.txt
alembic upgrade head                              # run DB migrations
uvicorn app.main:app --reload --port 8000         # dev server

pytest tests/                                     # run all tests
pytest tests/test_validate_chunk.py               # run one file
pytest tests/test_orchestrator.py::TestRetryEndpoint::test_retry_restores_file_to_processing_from_waiting  # single test
```

Tests use an in-memory SQLite DB (`sqlite+aiosqlite://`) with the production schema, and monkeypatch `AsyncSessionLocal` in each module under test (see `tests/test_orchestrator.py`) so orchestrator/handler code under test talks to the fixture DB. Job enqueue calls are mocked (`AsyncMock`) rather than actually run.

### Frontend (from `frontend/`)

```bash
npm install
npm run dev       # Vite dev server on :5173; proxies /api and /ws to http://localhost:8000 (override with VITE_BACKEND_URL)
npm run build     # tsc -b && vite build
npm run lint      # eslint .
```

### Docker (full stack)

```bash
docker compose up -d --build   # builds frontend, bundles into backend image, serves on :8000
```

## Architecture

### Job system (`backend/app/jobs/`)

All background work — MKV inspection, subtitle extraction, translation, validation, review, publishing — runs as **jobs**: DB-persisted (`JobRecord` in `app/db/models.py` is the source of truth for status), dequeued by a fixed-size async worker pool (`JobManager` in `jobs/manager.py`), and executed via a plain sync handler function run in a thread (`asyncio.to_thread`). Handlers self-register with `@register_job_handler("job_type")` (`jobs/registry.py`) and must be imported once in `app/main.py` for that registration to happen.

Key mechanics in `JobManager`:
- **Dedupe**: jobs are keyed by `dedupe_key` (typically `f"{job_type}:{file_id}:{chunk_index}"`); enqueueing with an existing queued/running key returns the existing record instead of creating a duplicate. Stale completed/failed jobs with the same key are reset and re-enqueued rather than inserted fresh (avoids a UNIQUE constraint race).
- **Worker pool resize**: `JOB_WORKER_COUNT` can change live via the options UI; `resize()` grows or gracefully shrinks (retiring workers drain their current job before stopping).
- **Chunk-level jobs** (`translate_chunk`, `validate_chunk`, `repair_chunk`, `polish_chunk`, `review_chunk_final`) get `max_attempts=1` per enqueue; retry happens at the *chunk* level instead: `_mark_chunk_job_failed` classifies the error — retryable codes (`OPENAI_API_ERROR`, `RESPONSE_PARSE_ERROR`, `UNEXPECTED_ERROR`) leave the chunk status untouched (so the orchestrator re-enqueues the same stage) and burn one unit of a 3-per-stage budget tracked in `chunk.retry_count` (reset on stage success by `_clear_chunk_retry_state`); terminal codes or an exhausted budget set `status=job_failed`. The LLM client additionally retries transient API noise in-call with backoff.
- On any terminal state (success, failure, cancellation, max-attempts-exceeded), `_on_complete` fires — this is wired to `orchestrate_on_job_complete`, which is what actually drives the pipeline forward (jobs don't chain themselves).

### Orchestrator (`backend/app/orchestrator/`)

Three-level state machine, re-entered after **every** job completion (and by a periodic sweep every 10s as a safety net — `orchestrator.py`'s `start_sweep_loop`). Orchestration for a given project is serialized with a per-project `asyncio.Lock` to avoid races when jobs for the same project finish concurrently.

- `project_orchestrator.py` — drives `Project.status` (`new → discovering → context_review → processing → review_required? → completed`). Fans out to `orchestrate_file` for each file. Discovery builds the **translation context** sequentially once every file is past discovery: `aggregate_speakers` (names + line counts + sample lines, `speaker_mapping_status` `awaiting_discovery → aggregated|complete`) → `infer_character_mapping` (fuzzy name/alias match + extra-detection, then one cheap-model structured LLM call with per-speaker confidence; manual rows never overwritten; an LLM *error* fails the job and leaves status `aggregated`) → `generate_style_bible` (project-level gate, skippable via `REQUIRE_STYLE_BIBLE`). A permanently failed context job **blocks** the project in `discovering` (perma-fail guards stop re-enqueues; failure surfaces via `GET /projects/{id}/context-status` with a retry via `POST .../context/retry`). When all components are built the project stops at **`context_review`** — gate 1 — until the user calls `POST .../approve-context` (sets `Project.context_approved_at`). Readiness/confidence derivation lives in `orchestrator/context_status.py` (shared by the endpoint and the orchestrator's exit condition). Low-confidence matches surface in the context panel + Characters tab; corrections can retranslate affected chunks via `POST .../speakers/{id}/retranslate-affected`.
- `file_orchestrator.py` — drives `File.status` (`new → discovering → ready → processing → review_required → accepted`, with `waiting` as an error-parking state). Files go `ready` as soon as subtitles are extracted, then sit **inert** until both gates are open: project context approved AND the file's own `POST .../files/{id}/translate` action (sets `File.translation_requested_at` — gate 2; from then on all stages run automatically). `_handle_ready` gates in order: fonts resolved → chunks planned → file analysis exists (`analyze_script`, **hard** gate: a permanently failed job parks the file in `waiting`/`analysis_failed`, retried via the same translate endpoint which re-enqueues with the canonical dedupe key to trigger the manager's stale-reset). In `_handle_processing` the project treats `ready`+unrequested files as inert — the project never auto-completes while unstarted files remain.
- `chunk_orchestrator.py` — drives `SubtitleChunk.status` through `pending → translated → validated → polished → final_reviewed → complete` via `_CHUNK_TRANSITIONS`, with two loops: `validate_trans_failed → repair_chunk → translated` (one repair attempt, then terminal `validate_repair_failed`) and `needs_polish → polish_chunk → polished` (one targeted re-polish when `review_chunk_final` finds strong issues, gated by `polish_attempt_count`). **Dialogue** translation is **serialized per file**: a `pending` dialogue chunk only gets `translate_chunk` once the previous *dialogue* chunk has cleared its polish pass (`_PRE_POLISH_STATUSES` — `pending`/`translated`/`validate_trans_failed`/`validated` all hold it back), so the rolling context window quotes the wording that actually ships rather than the cheap model's draft. `needs_polish` releases the gate (the full-coverage pass has already run); a terminal predecessor also releases it, so one broken chunk can't wedge the file. The sign/karaoke/song partitions are **ungated** — no conversational continuity to preserve — and translate in parallel with each other and with the dialogue chain. The gate tracks the preceding chunk per content-type partition, so `plan_translation_chunks`' contiguous partition layout (all dialogue, then all signs, …) never makes one partition inherit another's gate. Matching this, the rolling context window (`prompt_context.load_preceding_context_events`, shared by `translate_chunk` and `polish_chunk`) is **partition-scoped**: `PREPEND_CONTEXT_SIZE` counts lines of the chunk's own `content_type`, so interleaved signs/songs — whose partitions may not have run yet — can't spend the window's slots on untranslated source-only rows. The mirror-image `load_following_context_events` adds `LOOKAHEAD_CONTEXT_SIZE` *source-only* `[AHEAD]` rows after the targets (same partition scoping) so a chunk's last lines aren't translated blind to what follows. Karaoke chunks are auto-completed without translation when `TRANSLATE_KARAOKE` is off (events get `translation_status="skipped"`).

**Nothing auto-chains.** Every transition is *re-derived from current DB state* each time the orchestrator runs — there's no "job A enqueues job B" logic in the handlers themselves. This makes the pipeline crash-safe (a restart just re-sweeps and continues) but means new pipeline stages must be added to the orchestrator's status tables, not just written as a handler.

**Terminal/blocked states requiring user action** (`CHUNK_TERMINAL_STATUSES` = `job_failed`, `validate_repair_failed`): the orchestrator stops enqueueing for that chunk and bubbles a blocking reason up to the file (`FileBlockingReason`), which surfaces in the UI for manual retry. A chunk only reaches `validate_repair_failed` after one automatic `repair_chunk` attempt following the first validation failure (`validate_trans_failed`) — see the pipeline diagram in `README.md`.

When touching orchestration logic, `backend/tests/test_orchestrator.py` is the reference for expected state transitions (including edge cases like stale job completions arriving after a chunk has already advanced further).

### Output / publishing (`app/db/output_state.py`, `orchestrator/publish.py`, `jobs/handlers/publish_project.py`)

Nothing publishes automatically. `Project.output_revision` is bumped (`touch_output`, once per transaction) whenever data feeding the final ASS/MKV changes — a `before_flush` listener covers ORM edits of `SubtitleEvent`/`SubtitleStyle`/`Subtitle` render columns, Core/bulk flows (re-extraction, full retranslate) and the output-affecting options (`REPLACE_INCOMPATIBLE_FONTS`, `TARGET_LANG_*`) call it explicitly. The user-facing state (`not_ready|ready|publishing|published|failed`, exposed as `ProjectOut.output`) is *derived* by `derive_output_state` from accepted-file counts, `publish_state`, `output_revision`, `published_revision`, `publish_target_revision`. `POST /projects/{id}/publish` atomically claims the project (`publish_attempt += 1`) and enqueues `publish_project:{id}:{attempt}` (`max_attempts=1`); the per-attempt key lets every click run even after completed runs, the claim prevents concurrent runs. The handler captures the revision, reads all data in one pass, stages temp ASS/MKV files and only then `os.replace`s them; failure sets `publish_state=failed` and is never auto-retried. The sweep only *reconciles* runs whose job died.

### Final QC (`api/routes/qc.py`, `db/qc_state.py`)

Post-pipeline line editing. `qc_available` is *derived* (`NOT EXISTS chunk WHERE status != 'complete'`; zero chunks ⇒ available; independent of `FileStatus`) and enforced by every `/projects/{pid}/files/{fid}/qc/*` endpoint (409 otherwise); exposed as `FileOut.qc_available`. Endpoints: compact all-events list (sorted `start_ms, line_index`, `show_hidden`), per-event detail (QA items, watched matches), PATCH (text/timing only; timing quantized to 10 ms; real edits set `is_locked`; no-op saves change nothing), hide/restore, manual-event POST, and `preview.ass` (same `build_ass` as publish; ETag = `output_revision`). `SubtitleEvent.is_hidden`/`is_manual` are honored centrally in `build_ass`: translated drops hidden and keeps manual, original keeps hidden and drops manual. Output invalidation relies on the `output_state` ORM listener (`is_hidden` is a tracked column). Timing is split: `original_start_ms/original_end_ms` are immutable source timing (set at extraction; `build_ass` uses them for the `original` variant), `start_ms/end_ms` are the editable QC timing (translated variant); full retranslate resets current to original, manual events store original=current. QA is not re-run after an edit (stale by design). Full retranslate deletes manual events and clears hidden/lock; `retranslate-affected` and re-extraction don't preserve QC state specially (re-extraction recreates all events).

### QC media & fonts (`api/routes/media.py`, `subs/font_registry.py`, `subs/font_attachments.py`, `subs/qc_fonts.py`)

Infrastructure for the future JASSUB preview. `GET|HEAD /projects/{pid}/files/{fid}/media` streams the ORIGINAL source MKV via Starlette `FileResponse` (Range/206/416 are the framework's; no transcode/remux). All source paths go through `core/media_paths.resolve_source_path` (must stay inside `IMPORT_ROOT/<source_directory>`); job handlers use `resolve_source_path_parts(..., must_exist=False)`. Behind a reverse proxy: pass `Range` through, don't gzip, disable response buffering. Fonts: libass matches **internal** name-table names (IDs 1/16/21/4/6, all platforms), never filenames. Sources = MKV attachments (`mkvmerge -J` + `mkvextract attachments`, lazily extracted into `<config>/cache/font_attachments/<pathkey>-<size/mtime key>/`, temp dir + atomic rename, manifest last) and the configured dir (`FONTS_ROOT`, default `<config>/fonts`, scanned at startup, `refresh_configured_registry()`). Font ids = content hash (immutable caching). `find_font_by_family`: attachment registry first, then configured, never mixed, no aliasing. `GET .../qc/fonts` (QC-gated) returns all attachments + faces for required families (effective `build_ass` font incl. `REPLACE_INCOMPATIBLE_FONTS`, plus `n` overrides), `missing_families`, and `attachment_error` instead of failing; bytes at `.../qc/fonts/{id}` (attachments) and `/api/fonts/configured/{id}`. No DB tables.

### Data model (`backend/app/db/models.py`)

Hierarchy: `Project` → `File` (one MKV) → `Subtitle` (ASS metadata) + `SubtitleEvent` (one row per subtitle line) + `SubtitleStyle` + `SubtitleChunk` (a contiguous range of events processed together) + `QaItem` (flagged issues, optionally tied to an event; one severity scale `blocker|warning|info`). Speaker/character mapping is N:1 on `ProjectSpeaker` itself (`character_id` + `match_confidence`/`match_origin` manual|fuzzy|llm/`match_rationale`/`line_count`/`sample_lines_json`/`is_extra`) pointing at `ProjectCharacter`; `match_origin="manual"` rows are never overwritten by inference. `JobRecord` ties back to both `Project` and optionally `File`.

File acceptance is risk-based: `_handle_chunks_result` honors `AUTO_ACCEPT_POLICY` (`fully_clean` default — zero unresolved QA items of any severity auto-accepts; `no_blockers`; `manual`), the accept endpoint 409s only on unresolved blockers (optional `resolve_warnings`), and both paths share `finalize_accepted_file` (TM population + style bible update + ACCEPTED). Accepted files remain editable. Acceptance never produces output: publishing is a separate, explicit project-level lifecycle (below), and `FileStatus.MUXING/COMPLETED` are retired (nothing sets them; the migration converts legacy rows to `ACCEPTED`; `ProjectStatus.COMPLETED` now just means "every file accepted"). `GET /projects/{id}/review-queue` is the flat triage feed; `POST /projects/{id}/qa-items/bulk-resolve` batch-resolves.

Quality measurement: `FileQualityMetric` (one row per completed file, computed by the `compute_file_metrics` job which the project orchestrator enqueues for completed files lacking a row — same perma-failed-job guard as the LLM gates). Two distance signals, both mean normalized Levenshtein (rapidfuzz) on `plain_text`-stripped strings: `edit_distance_norm` = **human** correction distance (`original_ai_translated_text` vs shipped `translated_text` — polish and auto-line-break keep the reference in sync with their output, so only user edits move it) and `polish_churn_norm` = how much the polish pass rewrote the cheap-model draft (from `polish_edit` QaItem before/after details). `GET /projects/{id}/metrics` serves the dashboard. Fuzzy TM (`translation_memory.fuzzy_suggest`, rapidfuzz ratio ≥ 92, suggestion-only) supplements the exact lookup in `translate_chunk`.

Consistency layer (all per-project, all injected into prompts by `prompt_context.py` builders): `ProjectGlossaryTerm` (established translations; names/places always injected, other categories matched against chunk text; user edits set `locked=1` and LLM jobs only ever INSERT new terms), `ProjectStyleBible` (versioned tone/register/honorific guidance), `ProjectCharacterStyle` (voice notes), `ProjectAddressPair` (T–V modes, keyed by raw speaker names), `FileAnalysis` (per-file synopsis/scenes/tricky lines from `analyze_script`), `TranslationMemoryEntry` (exact-match TM in `app/subs/translation_memory.py`: normalized-hash lookup, raw-equality required for auto-apply, human origin never downgraded; populated at file acceptance via `finalize_accepted_file`, which also enqueues the additive `update_style_bible` job; **post-acceptance** subtitle-event edits/reverts additionally auto-sync single entries via `upsert_event_entry`/`downgrade_event_entry` — forward-only, so the next translated file benefits). `LlmCall` logs every LLM attempt.

Migrations are managed with Alembic (`backend/alembic/`, config in `alembic.ini`). Run `alembic upgrade head` after pulling schema changes; generate new migrations with `alembic revision --autogenerate` when models change.

### Runtime configuration (`backend/app/db/options.py`, `app/core/config.py`)

Two distinct config layers — don't confuse them:
- **Environment variables** (`app/core/config.py`, `Settings`): only directory paths (`IMPORT_ROOT`, `OUTPUT_ROOT`, `CONFIG_ROOT`) and `DEBUG`. Read once at startup.
- **DB-backed options** (`app/db/options.py`, table `options`): everything else (API keys, models, prompts, chunk size, structured-output mode, CPS/row limits, worker count, log level). Editable live from the UI's Options drawer; changes take effect immediately via `options_store.register_change_listener` (see `_on_option_change` in `main.py`) without a restart. `JobContext.options` is a snapshot taken per-job-run, so handlers see options as of when they started. **Every** prompt the pipeline sends is one of these options (translation, repair, polish, sign, song, analyze, mapping, style_bible, style_bible_update) — handlers must read them via `ctx.options.resolved_*_prompt()` and never import a `DEFAULT_*_PROMPT` constant directly, or the user's saved prompt is silently ignored. The defaults are plain string constants in `app/db/default_prompts.py` (no data files).

### LLM client (`backend/app/llm/`)

Every LLM call goes through `llm/client.py::complete()`: strict `json_schema` structured outputs from Pydantic schemas (`llm/schemas.py`), with a per-(base_url, model) cached downgrade ladder `json_schema → json_object → text+sanitize_llm_json` for non-OpenAI backends; transient-error retries with backoff; one corrective retry on schema-validation failure; per-task temperature policy with capability detection; token/cost/latency logging to the `llm_calls` table (prices from `LLM_PRICES_JSON`). Handlers catch `LlmError` and map `exc.code`/`exc.message` into the `JobResult` — the codes (`OPENAI_API_ERROR`, `RESPONSE_PARSE_ERROR`) feed the retryable-error classification in `jobs/manager.py`.

### ASS tag masking (`backend/app/subs/tag_masking.py`)

The LLM never sees raw ASS markup. `mask_line()` strips the leading override run into a hidden prefix, replaces inline `{\...}` blocks with indexed markers `⟦n⟧`, and turns `\N`/`\n`/`\h` into single characters `⏎`/`␤`/`␣`. `unmask_line()` verifies marker/escape integrity (errors trigger a corrective LLM retry in the handler) and reassembles the exact original markup; `force_unmask()` is the never-lose-a-tag fallback. Because reinsertion follows marker position, tags legitimately move with the words they wrap — `validate_chunk`'s dialogue tag check therefore compares the **multiset** of exact blocks, not their order. Czech-specific deterministic checks live in `app/subs/czech_checks.py` (flaggers only, never auto-fixers). Most run in `review_chunk_final`; `check_polish_drift` runs in `polish_chunk` on that pass's own before/after pairs. Past-tense agreement is checked for **both** persons: 1st (`jsem`/`bych`, against the speaker's gender) and 2nd (`jsi`/`bys`, against the **addressee's**). The addressee comes from `infer_addressee`, which reads a glossary name in direct-address position **in the translated line** rather than the event's speaker field — real files often carry no speaker attribution, but a line that addresses someone by name says so in the text. Knowing the addressee also lets `check_tv_against_pairs` use the exact speaker→addressee `ProjectAddressPair`; without one it falls back to the speaker's uniform mode, which only fires when they address everyone alike.

### Metadata providers (pluggable)

`app/metadata/registry.py`: a small `_PROVIDER_CLASSES` dict mapping a string key (from options, e.g. `anime_provider`) to a class implementing `MetadataProvider` (`metadata/base.py`). Add a new provider by implementing the base interface and registering it in the dict — nothing else needs to change. (`app/grammar/providers.py` follows the same pattern but is no longer part of the chunk pipeline — kept as an optional plugin.)

Project import and series matching are a deliberate manual step (owner decision — no auto-import). After import, metadata flows automatically: `get_episodes()` (AniDB parses the `<episodes>` block of the same cached anime XML; old cache entries lack the key — always `.get("episodes", [])`) → `metadata/sync.py` upserts characters/episodes (provider-sourced fields only; user-owned fields and set genders preserved) — shared by import and `POST /projects/{id}/refresh-metadata`. `scan_project` parses `File.episode_number` from filenames via `app/subs/episode_parsing.py` (conservative: returns None rather than guessing; fractional recaps and years rejected), and `prompt_context.load_episode_context` injects `Episode N: "Title"` into analyze/translate/polish prompts; `analyze_script` orders previous-episode synopses by episode number, falling back to path order.

### WebSocket + frontend sync

`app/ws/connection_manager.py` broadcasts job/project/chunk events (`job_created`, `job_update`, `job_progress`, `project_updated`, `chunk_progress`) to all connected clients; the frontend's `useJobSocket` hook consumes these to keep the UI live without polling. When adding a new state transition that the UI needs to reflect, broadcast it through `connection_manager.broadcast` rather than relying on the client to poll REST endpoints.

### Frontend structure (`frontend/src/`)

Standard Vite/React SPA: `api/client.ts` (axios instance), `hooks/` (React Query hooks per resource — `useProjects`, `useJobStats`, `useCharacterMapping`, etc.), `pages/` (dialogs and full pages), `components/` (shared layout/widgets). Mantine is the UI kit; TanStack Query handles server state.
