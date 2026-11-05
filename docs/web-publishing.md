# Import and WEB publishing

Import has two independent outcomes. **Import** on the Source step creates the
original source-only project and starts Discover, without TMDB lookup, WEB
configuration, or publishing intent. **Publish settings** opens the shared TMDB
editor without creating anything. **Save & Publish** commits the approved mapping
and expected episode count, then starts a separate metadata-only `publish_web`
job. Publication failure leaves the imported project intact. A stable import
request ID recovers the same project after a lost response; publication retries
reuse that project.

The Import and WEB dialogs share `TmdbMappingEditor` and `PublicationProgress`.
WEB publishing is available without accepted files. Its episode screen lists
stored project episode metadata, suggests files by their stored episode number,
and allows manual associations without changing those numbers or episode
identities. Only checked, accepted files upload ASS. Title, selected season,
every stored TMDB episode, and poster synchronize independently of that selection.
Release details are cleared and disabled when no uploads are selected.

Once remote identity binding is confirmed, title identity, order, structure and
season are locked. Descriptive metadata and the user-confirmed expected count
remain editable. Pending writes and remote UUID receipts survive partial failures;
subsequent reads reconcile ambiguous outcomes before another identity can be used.
Optimistic versions protect approved configuration from stale writes.

## Current website API

The client uses the current website's entity-oriented external-ID API, including
camelCase JSON and multipart fields. It does not send the older batch/provider
mapping payloads described by this repository's historical
`contracts/publisher-api.yaml`. Neither that file nor the website's authoritative
OpenAPI specification is changed by this integration.

The existing publisher service retains its normalized internal snapshots and UUID
receipts. `WebClient` translates them at the HTTP boundary. URL identities are
deterministic external IDs: `tmdb-tv-{id}` / `tmdb-movie-{id}`, TMDB season or
episode-group subgroup IDs, and `tmdb-episode-{id}`. Existing bare numeric TMDB
aliases are adopted when the media type matches. Movie content uses a durable
publisher key, never a fabricated TMDB episode ID. Unrecognized arbitrary remote
external IDs require explicit recovery; title text is not proof of identity.

The website returns entity receipts without `created` / `updated` / `noop`.
The client derives those outcomes from read-before-write metadata/hash comparison.
Remote UUIDs remain durable receipts, not URL path IDs. Identical retry requests
reuse external IDs; subtitle uploads include the current revision number and
the documented `releaseLabel` field.

## Translation completion and job progress

Translation completion is calculated separately for each project:

`floor(clamp(100 * unique confirmed subtitle episodes / expected_episode_count, 0, 100))`

Only confirmed episode-level subtitle receipts contribute. Accepted files and
metadata identities alone contribute nothing. Receipts survive file reassignment,
and reconciliation can confirm an upload whose response was lost. The expected
count is persisted exactly as approved and is never recomputed. Its next
synchronization uses the edited denominator, including a legitimate decrease in
percentage. A new metadata-only project preserves an existing remote percentage
when it has no subtitle receipts of its own.

Only `translationProgress` is sent; episode counts and translation statuses are
not sent. The current website has one such scalar on the shared title, not a
season/project field. Local calculations never aggregate projects: the latest
explicit synchronization of a project with confirmed subtitles supplies that
project's percentage to the shared scalar.

The wizard's single progress bar measures completed publication work, not
translation completion. The compact scrollable log comes from persisted job
operations and retains successful, unchanged, failed, uncertain and skipped
results after the attempt finishes.

## Persistence and verification

Migration `9c41d7a2e5b3`, following the existing TMDB import migration
`8bb829bd511f`, adds an import replay ID and remote/subtitle receipts to existing
Project and ProjectEpisode records. No second title/episode identity store is
introduced. Apply migrations through the normal deployment process; no live
database is migrated by the development/QA workflow.

Backend tests use temporary databases and mocked transports. Current-contract
tests include the real `publish_web` handler with thirteen metadata episodes and
zero Files. Frontend tests cover source-only requests, assignment eligibility,
stale-response guards, shared components and monotonic work progress. The isolated
desktop browser harness and its request audits are under `.tmp/workflow-qa`;
it never calls the real website or reads source media.
