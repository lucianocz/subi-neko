# subi-neko 🐱

Automated subtitle translation pipeline for anime MKV files.

Extracts subtitles from MKV files, translates them using an OpenAI-compatible LLM, polishes the draft with a full-coverage naturalness pass, runs deterministic quality checks (Czech grammar-agreement flags, readability, untranslated-text detection), and muxes the finished subtitles back into the container — all driven by a web UI.

---

## Features

- **Automatic MKV processing** — drop files into an import folder; the app discovers, inspects, and processes them automatically (project import and series matching stay a deliberate manual step)
- **ASS and plain-text input** — imports ASS/SSA or UTF-8 SubRip tracks, always prefers ASS when both exist, and gives plain-text sources a readable default 1080p style; output remains ASS
- **Episode metadata** — AniDB per-episode titles are fetched at import, episode numbers are parsed from filenames (conservatively — no guess beats a wrong guess), and "Episode 12: *Title*" context reaches every analysis/translation/polish prompt; a Refresh-metadata button re-syncs characters and episodes without touching user edits
- **Automatic speaker→character mapping** — subtitle speaker labels are matched to the AniDB/AniList character roster automatically (exact/normalized name matching, extra-detection for "Boy A"/"Crowd"-style labels, then one LLM inference call with confidence scores). The pipeline never waits for a human; low-confidence matches surface in the Characters tab of the Style guide dialog for asynchronous correction, with one-click retranslation of affected chunks
- **Risk-based review** — files that finish with zero unresolved QA items auto-accept (configurable policy); only blocker-severity issues gate manual acceptance, and a keyboard-driven project-wide review queue triages everything else (most severe, least confident first). Accepted subtitles stay editable and are rendered/muxed only after every file in the project is accepted.
- **Chunked translation** — subtitles are split into configurable chunks and translated sequentially per file, each chunk seeing its predecessors' finished translations as context
- **ASS tag safety** — inline override tags and escapes are masked into opaque markers before the LLM call and deterministically reinserted afterwards; the model never sees raw markup
- **Multi-stage pipeline** per chunk:
  - **Translation** — cheap-model LLM translation with character context, per-line confidence, and structured (JSON-schema) outputs with automatic fallback for non-OpenAI backends
  - **Validation** — deterministic structural checks on the translated output
  - **Repair** — automated LLM repair pass when validation fails (one attempt before requiring user action)
  - **Polish** — better-model full-coverage pass reworking every line for naturalness, gender agreement, register, and length. Every edit is diffed against the draft (no extra LLM call): a rewrite that changes a number, flips a negation, or drops an established glossary term is flagged as `polish_drift` for review
  - **Final review** — deterministic Czech-agreement, T–V consistency, readability (CPS/row length), and untranslated-text checks; strong findings trigger one targeted polish re-pass
- **Content-type aware** — dialogue, signs, songs, and karaoke get separate prompts and rules; karaoke lines keep their per-syllable timing untouched by default
- **Series-wide consistency**:
  - **Style bible** — LLM-generated per project before the first episode translates (tone, register rules, honorific policy, character voices, T–V address pairs); learns additively from each accepted episode; fully user-editable in the Style guide dialog
  - **Glossary** — established translations for names, places, techniques, items, and catchphrases (with Czech vocative forms and grammatical gender), seeded from metadata, grown by the LLM, editable in the UI, and enforced in every translate/polish/repair prompt
  - **Script analysis** — a per-episode pass producing a synopsis, scene segmentation, and tricky-line translator notes injected into translation prompts; each episode's synopsis feeds the next episode's analysis
  - **Translation memory** — exact-match reuse of accepted translations across episodes (OP/ED lyrics become free from episode 2); human-edited lines always win and are protected from re-polishing
- **Cost tracking** — every LLM call is logged with tokens, latency, and (optionally) price per model
- **Quality metrics** — every completed file gets a quality snapshot (human-edit distance between AI output and what shipped, polish edit rate, QA density, confidence calibration, cost/tokens per episode), visible in the Metrics dialog; a falling edit-distance trend across episodes means the consistency layer is learning
- **Fuzzy translation memory** — near-match TM hits (≥92% similar) surface as `[TM] previously translated as:` hints in translation prompts, in addition to exact-match reuse
- **QA items** — flagged issues surfaced per subtitle event; visible in the UI for manual review
- **Output muxing** — renders translated subtitles back to ASS and muxes into the original MKV using mkvmerge
- **Configurable via UI** — all settings (API keys, models, prompts, providers, pipeline switches) editable from the Options drawer without restarting

---

## Requirements

- Docker & Docker Compose

---

## Quick start

```bash
# 1. Clone
git clone https://github.com/yourname/subi-neko.git
cd subi-neko

# 2. Create media directories (already done if you use docker-compose volumes as-is)
mkdir -p media/import media/output config

# 3. Start
docker compose up -d

# 4. Open the UI
open http://localhost:8000
```

The UI is served by the backend on port 8000. No separate frontend server is needed.

---

## Directory layout

| Path | Purpose |
|------|---------|
| `media/import/` | Place project source folders here; each subdirectory becomes a project |
| `media/output/` | Finished MKV files are written here |
| `config/` | SQLite database (`subi.db`) and persistent options |

---

## Configuration

All runtime settings are stored in the database and editable through the **Options drawer** in the UI (gear icon). Changes take effect immediately — no restart required.

### Environment variables

Only the directory paths and low-level flags are set via environment variables (in `docker-compose.yml` or `.env`):

| Variable | Default | Description |
|----------|---------|-------------|
| `IMPORT_ROOT` | `./media/import` | Source MKV import directory |
| `OUTPUT_ROOT` | `./media/output` | Finished output directory |
| `CONFIG_ROOT` | `./config` | SQLite database directory |
| `DEBUG` | `false` | Enable FastAPI debug mode + SQL query logging |

### Options (UI / database)

#### Translation

| Option key | Default | Description |
|------------|---------|-------------|
| `TARGET_LANG_NAME` | *(required)* | Full language name sent to the LLM (e.g. `Czech`) |
| `TARGET_LANG_CODE` | *(required)* | BCP-47 language code (e.g. `cs`) |
| `CHUNK_SIZE` | `100` | Subtitle events per translation chunk |
| `PREPEND_CONTEXT_SIZE` | `10` | Preceding events (with their translations) sent as read-only context |
| `LOOKAHEAD_CONTEXT_SIZE` | `5` | Following events sent as read-only **untranslated** English, so the tail of a chunk isn't translated blind to what comes next (`0` disables) |
| `TRANSLATION_PROMPT` | built-in | System prompt for the translation job; `{TARGET_LANG_NAME}` is substituted |
| `REPAIR_PROMPT` | built-in | System prompt for the repair job |
| `POLISH_PROMPT` | built-in | System prompt for the polish (naturalness) pass |
| `SIGN_TRANSLATION_PROMPT` | built-in | System prompt for on-screen text (signs/typesetting) |
| `SONG_TRANSLATION_PROMPT` | built-in | System prompt for song lyrics |
| `ANALYZE_PROMPT` | built-in | System prompt for the per-file script analysis pass |
| `MAPPING_PROMPT` | built-in | System prompt for speaker→character inference (language-neutral; no placeholder) |
| `STYLE_BIBLE_PROMPT` | built-in | System prompt for building the project style bible |
| `STYLE_BIBLE_UPDATE_PROMPT` | built-in | System prompt for the additive per-episode style-bible update |

All prompt defaults live in `backend/app/db/default_prompts.py`; each is overridable live from the Options drawer, and clearing an option restores the built-in default.

#### OpenAI / LLM

| Option key | Default | Description |
|------------|---------|-------------|
| `OPENAI_API_KEY` | *(required)* | API key; set to any value for local/proxy endpoints |
| `OPENAI_API_BASE` | `https://api.openai.com/v1` | API base URL; point to any OpenAI-compatible endpoint |
| `OPENAI_MODEL_CHEAP` | `gpt-5.4-mini` | Model used for translation |
| `OPENAI_MODEL_BETTER` | `gpt-5.4` | Model used for the polish and repair passes |
| `LLM_STRUCTURED_OUTPUTS` | `auto` | `auto`, `json_schema`, `json_object`, or `text`; auto probes the backend and downgrades automatically |
| `LLM_PRICES_JSON` | *(empty)* | Per-million-token prices for cost tracking, e.g. `{"gpt-5.4-mini": {"in": 0.4, "out": 1.6}}` |
| `LLM_MAX_COMPLETION_TOKENS` | `16384` | Upper bound on completion tokens per call |

#### Quality

| Option key | Default | Description |
|------------|---------|-------------|
| `TRANSLATE_KARAOKE` | `0` | `0` = keep karaoke lines untranslated (preserves per-syllable `\k` timing); `1` = translate them |
| `REQUIRE_STYLE_BIBLE` | `1` | Generate the project style bible before the first file translates (a permanently failed generation does not block) |
| `CPS_LIMIT` | `20` | Reading speed (chars/second) above which a line is flagged for condensing |
| `MAX_ROW_CHARS` | `42` | Row length above which a line is flagged |
| `AUTO_LINE_BREAK` | `1` | `1` = automatically rebalance over-long dialogue rows onto two lines at a word boundary before the readability check (`long_row` then only flags lines a split can't fix); `0` = flag-only |
| `TRANSLATION_CONFIDENCE_FLAG_THRESHOLD` | `0.55` | Model-reported per-line confidence below which a line is flagged for triage |

#### Review & mapping automation

| Option key | Default | Description |
|------------|---------|-------------|
| `AUTO_ACCEPT_POLICY` | `fully_clean` | `fully_clean` = auto-accept files with zero unresolved QA items; `no_blockers` = auto-accept unless blockers remain; `manual` = always require an explicit accept |
| `AUTO_MAPPING_ACCEPT_THRESHOLD` | `0.8` | Speaker-mapping confidence below which a match is highlighted for verification in the Characters tab of the Style guide dialog |

#### Worker

| Option key | Default | Description |
|------------|---------|-------------|
| `JOB_WORKER_COUNT` | `4` | Number of parallel job worker threads |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` |

---

## Translation pipeline

Each file goes through this sequence automatically:

```
MKV file discovered
  └─ inspect_mkv         – probe tracks, detect subtitle format
  └─ extract_subtitles   – extract ASS/SSA or UTF-8 SubRip track, classify content type
  └─ scan_project        – parse subtitle events into DB, parse episode numbers
  └─ aggregate_speakers  – collect speaker names + line counts + samples
  └─ infer_character_mapping – automatic speaker→character mapping
  │                        (fuzzy name match → LLM inference w/ confidence)
  └─ resolve_style_fonts – load font/style metadata
  └─ plan_translation_chunks – split events into chunks per content type

Once per project (from the first ready file):
  └─ generate_style_bible – tone/register/honorifics + glossary + voices + T–V pairs

Once per file:
  └─ analyze_script       – synopsis, scenes, tricky-line notes, new terms/pairs

Per chunk (dialogue translation serialized per file — dialogue chunk N starts
only once chunk N-1 is polished, so later chunks see the final wording as
context; signs/karaoke/songs and all later stages run in parallel):
  └─ translate_chunk     – cheap-model LLM translation (masked ASS tags,
  │                        per-line confidence, structured outputs)
  └─ validate_chunk      – deterministic structural validation
  └─ repair_chunk        – LLM repair (if validation failed once)
  └─ validate_chunk      – re-validate after repair
  └─ polish_chunk        – better-model naturalness pass over every line
  └─ review_chunk_final  – deterministic Czech/readability checks;
                           strong findings trigger one targeted re-polish

All chunks complete
  └─ clean file (no unresolved QA) → auto-accepted; flagged file → review queue
  └─ (on accept: translation memory populated, style bible learns from the episode)
  └─ all project files accepted – release output for the whole project
  └─ render_output_ass   – write translated ASS file
  └─ mux_output_mkv      – mux back into MKV
```

Karaoke chunks are completed without translation by default (`TRANSLATE_KARAOKE=0`) so their per-syllable timing survives.

Transient LLM failures (API errors, malformed responses) are retried automatically — inside the LLM client with backoff, and up to 3 times per chunk stage by the orchestrator — before a chunk becomes `job_failed`.

---

## Chunk failure states

| Status | Meaning | Retry available |
|--------|---------|-----------------|
| `job_failed` | Technical failure (API error, parse error, crash) | ✅ |
| `validate_trans_failed` | First validation rejection — repair will run automatically | — |
| `validate_repair_failed` | Validation rejected again after repair — requires user action | ✅ |

When a chunk is retried manually, it resumes from the correct stage based on where it failed.

---

## Development

### Backend

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate      # Windows
# source .venv/bin/activate  # Linux/macOS
pip install -r requirements.txt

# Run migrations
alembic upgrade head

# Start dev server
uvicorn app.main:app --reload --port 8000
```

### Frontend

```bash
cd frontend
npm install
npm run dev     # Vite dev server on http://localhost:5173
```

Set `VITE_API_BASE_URL=http://localhost:8000` (or configure the Vite proxy) to point the frontend at the local backend.

### Tests

```bash
cd backend
pytest tests/
```
