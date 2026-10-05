// ─── Projects ────────────────────────────────────────────────────────────────

export type ProjectStatus =
  | 'new'
  | 'discovering'
  | 'context_review'
  | 'processing'
  | 'review_required'
  | 'completed'
  | 'failed';

export type SpeakerMappingStatus =
  | 'awaiting_discovery'
  | 'aggregated'
  | 'complete';

export interface Project {
  id: number;
  name: string;
  source_directory: string;
  anime_provider: string;
  anime_external_id: string;
  speaker_mapping_status: SpeakerMappingStatus;
  status: ProjectStatus;
  is_paused: boolean;
  context_approved_at: string | null;
  created_at: string;
  updated_at: string;
}

// ─── Translation context (gate 1) ────────────────────────────────────────────

export type ContextState = 'building' | 'failed' | 'ready_for_review' | 'approved';

export type ContextComponentStatus =
  | 'pending'
  | 'running'
  | 'complete'
  | 'failed'
  | 'skipped'
  | 'info';

export type ContextComponentKey =
  | 'discovery'
  | 'speaker_aggregation'
  | 'character_mapping'
  | 'style_bible'
  | 'glossary'
  | 'address_pairs'
  | 'character_styles';

export interface ContextComponent {
  key: ContextComponentKey;
  status: ContextComponentStatus;
  detail: Record<string, unknown>;
  error_code?: string | null;
  error_message?: string | null;
  retryable?: boolean;
}

export interface ContextAttentionItem {
  type: 'unmapped_speaker' | 'low_confidence_mapping' | 'empty_character_roster';
  speaker_id?: number;
  name?: string;
  line_count?: number;
  confidence?: number | null;
  character_name?: string | null;
  message?: string;
}

export interface ContextConfidence {
  speakers_total: number;
  non_extra: number;
  unmapped_non_extra: number;
  below_threshold: number;
  threshold: number;
  line_weighted_coverage: number | null;
  overall: 'high' | 'medium' | 'low';
}

export interface ContextStatus {
  state: ContextState;
  approved_at: string | null;
  components: ContextComponent[];
  attention: ContextAttentionItem[];
  confidence: ContextConfidence;
}

// ─── Files ────────────────────────────────────────────────────────────────────

export type FileStatus =
  | 'new'
  | 'discovering'
  | 'waiting'
  | 'ready'
  | 'processing'
  | 'review_required'
  | 'accepted'
  | 'muxing'
  | 'completed'
  | 'paused'
  | 'failed';

export type FileBlockingReason =
  | 'user_review_required'
  | 'subtitle_missing'
  | 'subtitle_parse_failed'
  | 'analysis_failed'
  | 'translation_failed'
  | 'validation_failed'
  | 'mux_failed'
  | 'paused';

export interface SubtitleChunk {
  id: number;
  chunk_index: number;
  translate_from_line: number;
  translate_to_line: number;
  content_type: string;
  status: string;
  model: string | null;
  llm_review_needed: boolean;
  retry_count: number;
  repair_attempt_count: number;
  last_error_code: string | null;
  last_error_message: string | null;
  failed_job_type: string | null;
  qa_errors: number;
  qa_warnings: number;
  jobs: Record<string, ChunkJob>;
}

export interface ChunkJob {
  id: number;
  job_type: string;
  status: JobStatus;
  attempt_count: number;
  result: Record<string, unknown> | null;
  error_code: string | null;
  error_message: string | null;
  scheduled_at: string;
  started_at: string | null;
  finished_at: string | null;
  updated_at: string;
}

export type WatchedWordType = 'original' | 'translated';

export interface ProjectWatchedWord {
  id: number;
  project_id: number;
  word: string;
  word_type: WatchedWordType;
  created_at: string;
  updated_at: string;
}

export interface VideoFile {
  id: number;
  project_id: number;
  filename: string;
  relative_path: string;
  status: FileStatus;
  blocking_reason: FileBlockingReason | null;
  translation_requested_at: string | null;
  detected_subtitle_format: string | null;
  subtitle_track_index: number | null;
  retry_count: number;
  last_error_code: string | null;
  last_error_message: string | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  chunks_done?: number | null;
  chunks_total?: number | null;
  qa_issues: number;
  qa_errors: number;
  qa_warnings: number;
}

export interface QaIssue {
  id: number;
  severity: string;
  qa_type: string;
  message: string;
  details_json: string | null;
  is_resolved: boolean;
  resolution_note: string | null;
  created_at: string;
}

export interface SubtitleEventEditorRow {
  id: number;
  file_id: number;
  line_index: number;
  event_type: string;
  source_text: string;
  translated_text: string | null;
  original_ai_translated_text: string | null;
  speaker_name: string | null;
  speaker_gender: string | null;
  character_name: string | null;
  character_gender: string | null;
  is_user_edited: boolean;
  is_locked: boolean;
  is_approved: boolean;
  /** Raw (unrounded) reading speed from the backend; null when unavailable. */
  cps: number | null;
  issues: QaIssue[];
}

export interface SubtitleEventsIssueCount {
  qa_type: string;
  severity: string;
  count: number;
}

export interface SubtitleEventsSummary {
  total_events: number;
  unresolved_issue_count: number;
  issue_counts: SubtitleEventsIssueCount[];
  watched_occurrences: number;
  cps_limit: number;
}

export interface SubtitleEventPage {
  items: SubtitleEventEditorRow[];
  page: number;
  page_size: number;
  filtered_events: number;
  total_events: number;
  total_pages: number;
  summary: SubtitleEventsSummary;
}

// ─── Characters & Speakers ───────────────────────────────────────────────────

export interface ProjectCharacter {
  id: number;
  project_id: number;
  external_id: string | null;
  name: string;
  role: string | null;
  gender: string | null;
  social_position: string | null;
  aliases: string | null;
  note: string | null;
  created_at: string;
  updated_at: string;
}

export interface ProjectCharacterWithSpeakers extends ProjectCharacter {
  speaker_ids: number[];
}

export type MatchOrigin = 'manual' | 'fuzzy' | 'llm';

export type SpeakerContentTag = 'sign' | 'karaoke' | 'song';

export interface ProjectSpeaker {
  id: number;
  project_id: number;
  name: string;
  gender: string | null;
  character_id: number | null;
  character_name: string | null;
  match_confidence: number | null;
  match_origin: MatchOrigin | null;
  match_rationale: string | null;
  line_count: number;
  sample_lines: string[];
  is_extra: boolean;
  content_tag: SpeakerContentTag | null;
  created_at: string;
  updated_at: string;
}

export interface SpeakerUpdateResult {
  speaker: ProjectSpeaker;
  affected_chunk_count: number;
}

// ─── Review queue ────────────────────────────────────────────────────────────

export type QaSeverity = 'blocker' | 'warning' | 'info';

export interface ReviewQueueItem {
  id: number;
  file_id: number;
  filename: string;
  severity: QaSeverity;
  qa_type: string;
  message: string;
  event_id: number | null;
  line_index: number | null;
  speaker: string | null;
  source_text: string | null;
  translated_text: string | null;
  translation_confidence: number | null;
  is_user_edited: boolean;
  created_at: string;
}

export interface ReviewQueue {
  total: number;
  items: ReviewQueueItem[];
}

// ─── Project stats ────────────────────────────────────────────────────────────

export interface ProjectStats {
  qa_errors: number;
  qa_warnings: number;
}



export type JobStatus = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled';

export interface Job {
  id: number;
  project_id: number;
  file_id: number | null;
  job_type: string;
  status: JobStatus;
  dedupe_key: string;
  priority: number;
  payload_json: string | null;
  result_json: string | null;
  attempt_count: number;
  max_attempts: number;
  error_code: string | null;
  error_message: string | null;
  scheduled_at: string;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  updated_at: string;
  // Real-time progress — in-memory only, delivered via WebSocket
  progress?: number;
  message?: string;
  project_name?: string;
}

// ─── Scheduler ───────────────────────────────────────────────────────────────

export type TriggerType = 'cron' | 'interval';

export interface ScheduledTask {
  id: string;
  name: string;
  job_type: string;
  project_id: number;
  payload: Record<string, unknown>;
  trigger_type: TriggerType;
  trigger_config: Record<string, unknown>;
  enabled: boolean;
  last_triggered_at: string | null;
  created_at: string;
}

// ─── Metadata ─────────────────────────────────────────────────────────────────

export interface SearchResult {
  provider_id: string;
  title: string;
  title_native: string | null;
  year: number | null;
  media_type: string;
  episode_count: number | null;
}

// ─── Import ───────────────────────────────────────────────────────────────────

export interface ImportDirectory {
  name: string;
  file_count: number;
}

// ─── WebSocket Events ─────────────────────────────────────────────────────────

export type WsEvent =
  | { event: 'job_created'; data: WsJobData }
  | { event: 'job_update'; data: WsJobData }
  | { event: 'job_progress'; data: { job_id: number; progress: number; message: string } }
  | { event: 'project_updated'; data: { project_id: number } }
  | { event: 'chunk_progress'; data: { file_id: number; project_id: number; chunks_done: number; chunks_total: number } }
  | { event: 'scheduler_trigger'; data: { schedule_id: string; job_id: number } };

export interface WsJobData {
  job_id: number;
  status?: JobStatus | string;
  project_id?: number;
  file_id?: number | null;
  job_type?: string;
  payload_json?: string | null;
  result_json?: string | null;
  attempt_count?: number;
  error_code?: string | null;
  error_message?: string | null;
  scheduled_at?: string;
  started_at?: string | null;
  finished_at?: string | null;
  updated_at?: string;
}

