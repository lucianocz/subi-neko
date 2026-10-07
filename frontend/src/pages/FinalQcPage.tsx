import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { Alert, Badge, Button, Center, Group, Loader, Stack, Switch, Text, Title } from '@mantine/core';
import { ArrowLeft, ArrowClockwise, Plus, Sparkle } from '@phosphor-icons/react';
import { QcBrandingDialog } from '../components/qc/QcBrandingDialog';
import { QcEventDetail } from '../components/qc/QcEventDetail';
import { QcEventList } from '../components/qc/QcEventList';
import { QcVideoPlayer } from '../components/qc/QcVideoPlayer';
import { useSplit } from '../components/qc/useSplit';
import { useActiveSubtitleEvents } from '../hooks/useActiveSubtitleEvents';
import { useJassub } from '../hooks/useJassub';
import { qcErrorDetail, qcErrorKind, useQcEventDetail, useQcEvents } from '../hooks/useQcEvents';
import { useQcBranding } from '../hooks/useQcBranding';
import { useQcEditing } from '../hooks/useQcEditing';
import { useQcFonts } from '../hooks/useQcFonts';
import { useQcPreview } from '../hooks/useQcPreview';
import type { QcBrandingSave, QcEvent, QcEventList as QcEventListData } from '../types/qc';
import { cpsLimitsFrom } from '../utils/cps';
import { brandingIndicator, brandingNeedsFontRefresh } from '../utils/qcBranding';
import { spaceBelongsToTarget } from '../utils/qcKeyboard';
import '../components/qc/qc.css';

const SEEK_NUDGE_MS = 50;
const NO_FONTS: readonly Uint8Array[] = [];

function parseId(value: string | undefined): number | null {
  const n = Number(value);
  return Number.isInteger(n) && n > 0 ? n : null;
}

function StateScreen({
  title, children, projectId, onRetry,
}: { title: string; children?: React.ReactNode; projectId: number | null; onRetry?: () => void }) {
  const navigate = useNavigate();
  return (
    <Center h="100dvh" p="md">
      <Stack align="center" gap="sm" maw={520}>
        <Title order={3}>{title}</Title>
        {children && <Text c="dimmed" ta="center">{children}</Text>}
        <Group>
          {onRetry && <Button variant="light" onClick={onRetry}>Retry</Button>}
          <Button
            variant="default"
            leftSection={<ArrowLeft size={14} />}
            onClick={() => navigate(projectId ? `/?project=${projectId}` : '/')}
          >
            Back to project
          </Button>
        </Group>
      </Stack>
    </Center>
  );
}

export function FinalQcPage() {
  const params = useParams();
  const projectId = parseId(params.projectId);
  const fileId = parseId(params.fileId);
  if (projectId === null || fileId === null) {
    return <StateScreen title="File not found" projectId={projectId}>This Final QC address is not valid.</StateScreen>;
  }
  // Keyed so switching file resets all page state (selection, follow, renderer).
  return <FinalQc key={`${projectId}:${fileId}`} projectId={projectId} fileId={fileId} />;
}

function FinalQc({ projectId, fileId }: { projectId: number; fileId: number }) {
  const [showHidden, setShowHidden] = useState(false);

  const eventsQuery = useQcEvents(projectId, fileId, showHidden);
  // Started in parallel with the (large) event list; the workspace only renders —
  // and so the <video>/renderer only start — once the events say QC is available.
  const preview = useQcPreview(projectId, fileId);
  const fonts = useQcFonts(projectId, fileId);

  const list = eventsQuery.data;
  if (eventsQuery.isError && !list) {
    const kind = qcErrorKind(eventsQuery.error);
    if (kind === 'not_found') {
      return <StateScreen title="File not found" projectId={projectId}>This project or file does not exist.</StateScreen>;
    }
    if (kind === 'unavailable') {
      return (
        <StateScreen title="Final QC is not available" projectId={projectId}>
          {qcErrorDetail(eventsQuery.error) ?? 'This file still has chunks that have not finished the pipeline.'}
        </StateScreen>
      );
    }
    return (
      <StateScreen title="Could not load Final QC" projectId={projectId} onRetry={() => eventsQuery.refetch()}>
        {qcErrorDetail(eventsQuery.error)}
      </StateScreen>
    );
  }
  if (!list) {
    return <Center h="100dvh"><Loader /></Center>;
  }
  return (
    <FinalQcWorkspace
      projectId={projectId}
      fileId={fileId}
      list={list}
      showHidden={showHidden}
      setShowHidden={setShowHidden}
      preview={preview}
      fonts={fonts}
    />
  );
}

interface Warning {
  key: string;
  color: string;
  title: string;
  text: string;
  retry?: () => void;
}

function FinalQcWorkspace({
  projectId, fileId, list, showHidden, setShowHidden, preview, fonts,
}: {
  projectId: number;
  fileId: number;
  list: QcEventListData;
  showHidden: boolean;
  setShowHidden: (v: boolean) => void;
  preview: ReturnType<typeof useQcPreview>;
  fonts: ReturnType<typeof useQcFonts>;
}) {
  const navigate = useNavigate();

  const [follow, setFollow] = useState(true);
  const [video, setVideo] = useState<HTMLVideoElement | null>(null);
  const [dismissed, setDismissed] = useState<Set<string>>(() => new Set());
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const leftRef = useRef<HTMLDivElement>(null);

  const events = list.events;
  const hardCps = list.cps_limit;
  const softCps = list.soft_cps_limit;
  const cpsLimits = useMemo(
    () => cpsLimitsFrom({ cps_limit: hardCps, soft_cps_limit: softCps }),
    [hardCps, softCps],
  );

  // The renderer is built once from the first preview + fonts; every later change
  // goes through setTrack/addFonts on the live instance (see useQcEditing).
  const renderer = useJassub(
    video,
    preview.data?.text ?? null,
    fonts.ready ? (fonts.fontData?.bytes ?? NO_FONTS) : null,
  );
  const editing = useQcEditing({
    projectId, fileId, showHidden, list,
    initialRevision: preview.data?.revision ?? null,
    renderer, fonts, videoRef,
  });
  const { selected } = editing;
  const branding = useQcBranding(projectId, fileId);
  const [brandingOpen, setBrandingOpen] = useState(false);
  const { brandingSaved } = editing;
  const savedBranding = branding.config.data;
  const saveBranding = useCallback(async (body: QcBrandingSave) => {
    let saved;
    try {
      saved = await branding.save(body);
    } catch (e) {
      throw new Error(qcErrorDetail(e) ?? 'Could not save branding.');
    }
    const changed = !savedBranding || savedBranding.enabled !== saved.enabled
      || savedBranding.template_filename !== saved.template_filename
      || savedBranding.start_offset_ms !== saved.start_offset_ms
      || savedBranding.scale_to_script_playres !== saved.scale_to_script_playres;
    // A save that changed nothing leaves the preview as it is.
    if (changed) {
      void brandingSaved(
        saved.output_revision,
        !savedBranding || brandingNeedsFontRefresh(savedBranding, saved),
      );
    }
  }, [branding, savedBranding, brandingSaved]);
  const detail = useQcEventDetail(projectId, fileId, selected?.id ?? null);
  const active = useActiveSubtitleEvents(video, events);

  const hSplit = useSplit(bodyRef, { key: 'h', defaultFraction: 0.62, minFirst: 420, minSecond: 340 });
  const vSplit = useSplit(leftRef, { key: 'v', defaultFraction: 0.58, minFirst: 180, minSecond: 140 });

  const onVideoElement = useCallback((el: HTMLVideoElement | null) => {
    videoRef.current = el;
    setVideo(el);
  }, []);

  const { select } = editing;
  // Single click: select only (no seek, playback untouched). A dirty edit is
  // saved first; if that fails the selection stays where it was.
  const handleSelect = useCallback((event: QcEvent) => { void select(event); }, [select]);
  // Double click: select + seek. Assigning currentTime keeps the paused/playing
  // state as it was; nothing here calls play() or pause().
  const handleSeek = useCallback(async (event: QcEvent) => {
    if (!(await select(event))) return;
    const el = videoRef.current;
    // Seeking to the exact start lands on the frame *before* it whenever the start
    // falls between frame timestamps, so the line would not be on screen yet. Aim
    // a few ms in (never past the event's midpoint) to land on the first frame
    // that shows it.
    if (el) el.currentTime = (event.start_ms + Math.min(SEEK_NUDGE_MS, (event.end_ms - event.start_ms) / 2)) / 1000;
  }, [select]);
  const handleUserScroll = useCallback(() => setFollow(false), []);

  // Space = play/pause, except where Space already means something (text entry,
  // buttons, the native video controls).
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.code !== 'Space' || e.repeat || e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return;
      if (e.defaultPrevented || spaceBelongsToTarget(e.target as HTMLElement | null)) return;
      const el = videoRef.current;
      if (!el) return;
      e.preventDefault();
      if (el.paused) el.play().catch(() => { /* blocked or unsupported: the player shows why */ });
      else el.pause();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);

  const toggleShowHidden = async (value: boolean) => {
    if (!(await editing.flushSelected())) return;
    if (!value) editing.dropHiddenSelection();
    setShowHidden(value);
  };
  const leave = async () => {
    if (await editing.flushSelected()) navigate(`/?project=${projectId}`);
  };

  const mediaSrc = `/api/projects/${projectId}/files/${fileId}/media`;
  const manifest = fonts.manifest;
  const warnings: Warning[] = [];
  if (preview.isError) {
    warnings.push({ key: 'preview', color: 'red', title: 'Subtitle preview unavailable',
      text: `${qcErrorDetail(preview.error) ?? 'The translated ASS could not be loaded.'} The video plays without subtitles.` });
  }
  if (editing.preview.state === 'error') {
    warnings.push({ key: `preview-refresh:${editing.preview.message}`, color: 'red', title: 'Subtitle preview not refreshed',
      text: `${editing.preview.message} Your edit is saved; the overlay may be out of date.`, retry: editing.retryPreview });
  }
  if (editing.fontError) {
    warnings.push({ key: `font-refresh:${editing.fontError}`, color: 'yellow', title: 'Could not refresh fonts',
      text: `${editing.fontError} New fonts may be missing from the preview.`, retry: editing.retryPreview });
  }
  if (fonts.manifestError) {
    warnings.push({ key: 'manifest', color: 'yellow', title: 'Font manifest unavailable',
      text: `${qcErrorDetail(fonts.manifestError) ?? 'Could not load fonts.'} Subtitles render with fallback fonts.` });
  }
  if (manifest?.attachment_error) {
    warnings.push({ key: 'attachments', color: 'yellow', title: 'MKV font attachments unavailable',
      text: manifest.attachment_error });
  }
  if (manifest && manifest.missing_families.length > 0) {
    warnings.push({ key: `missing:${manifest.missing_families.join(',')}`, color: 'yellow', title: 'Missing fonts',
      text: `No font file for: ${manifest.missing_families.join(', ')}. Those lines render with a fallback font.` });
  }
  if (fonts.failed.length > 0) {
    warnings.push({ key: `failed:${fonts.failed.join(',')}`, color: 'yellow', title: 'Some fonts failed to download',
      text: fonts.failed.join(', ') });
  }
  if (renderer.status === 'error') {
    warnings.push({ key: 'jassub', color: 'red', title: 'Subtitle renderer failed to start',
      text: `${renderer.error ?? 'Unknown error'}. The video plays without subtitles.` });
  }
  const visibleWarnings = warnings.filter((w) => !dismissed.has(w.key));
  const saveMessage = editing.save.state === 'error' ? editing.save.message : null;
  const actionMessage = editing.action.state === 'error' ? editing.action.message : null;

  return (
    <div className="qc-root" data-dragging={hSplit.dragging || vSplit.dragging}>
      <div className="qc-header">
        <Button
          size="xs"
          variant="default"
          leftSection={<ArrowLeft size={14} />}
          onClick={leave}
        >
          Project
        </Button>
        <Text fw={600} truncate style={{ minWidth: 0 }} title={list.filename}>{list.filename}</Text>
        <Text size="xs" c="dimmed" data-testid="qc-counts">
          {events.length.toLocaleString()} events
          {list.hidden_count > 0 && ` · ${list.hidden_count} hidden`}
        </Text>
        {renderer.status === 'loading' && preview.data && <Badge size="xs" variant="light">loading subtitles…</Badge>}
        {editing.preview.state === 'busy' && (
          <Badge size="xs" variant="light" color="gray" data-testid="qc-preview-busy">updating subtitles…</Badge>
        )}
        <div style={{ flex: 1 }} />
        {editing.stale && (
          <Group gap={6} wrap="nowrap">
            <Badge color="yellow" variant="light" data-testid="qc-stale">Data changed</Badge>
            <Button
              size="compact-xs" variant="light" color="yellow" leftSection={<ArrowClockwise size={12} />}
              loading={editing.reloading} onClick={editing.reload}
            >
              Reload
            </Button>
          </Group>
        )}
        <Button
          size="compact-xs"
          variant="light"
          leftSection={<Plus size={12} />}
          data-testid="qc-add-event"
          disabled={editing.newDraft !== null}
          onClick={editing.startNewEvent}
        >
          Add event
        </Button>
        <Switch
          size="xs"
          label="Show hidden"
          checked={showHidden}
          onChange={(e) => { void toggleShowHidden(e.currentTarget.checked); }}
        />
        <Switch
          size="xs"
          label="Follow playback"
          checked={follow}
          onChange={(e) => setFollow(e.currentTarget.checked)}
        />
        <Button
          size="compact-xs"
          variant="subtle"
          color="gray"
          leftSection={<Sparkle size={12} />}
          rightSection={brandingIndicator(savedBranding) === 'on'
            ? <Badge size="xs" variant="filled" data-testid="qc-branding-on">On</Badge> : undefined}
          disabled={!savedBranding}
          onClick={() => { void branding.templates.refetch(); setBrandingOpen(true); }}
          data-testid="qc-branding-button"
        >
          Branding
        </Button>
      </div>
      {savedBranding && (
        <QcBrandingDialog
          opened={brandingOpen}
          onClose={() => setBrandingOpen(false)}
          config={savedBranding}
          templates={branding.templates.data}
          templatesError={branding.templates.isError}
          getVideoTimeMs={editing.getVideoTimeMs}
          onSave={saveBranding}
        />
      )}

      <div className="qc-body" ref={bodyRef}>
        <div className="qc-left" ref={leftRef} style={{ flex: `0 0 ${hSplit.fraction * 100}%` }}>
          <div className="qc-video-pane" style={{ flex: `0 0 ${vSplit.fraction * 100}%` }}>
            {visibleWarnings.length > 0 && (
              <Stack gap={2} p={4} style={{ maxHeight: '40%', overflow: 'auto' }}>
                {visibleWarnings.map((w) => (
                  <Alert
                    key={w.key}
                    color={w.color}
                    title={w.title}
                    p="xs"
                    withCloseButton
                    onClose={() => setDismissed((s) => new Set(s).add(w.key))}
                  >
                    <Group gap="xs" wrap="nowrap" justify="space-between">
                      <Text size="xs">{w.text}</Text>
                      {w.retry && <Button size="compact-xs" variant="light" onClick={w.retry}>Retry</Button>}
                    </Group>
                  </Alert>
                ))}
              </Stack>
            )}
            <QcVideoPlayer src={mediaSrc} onVideoElement={onVideoElement} onFullscreenChange={renderer.resize} />
          </div>
          <div
            className="qc-splitter qc-splitter-v"
            data-dragging={vSplit.dragging}
            aria-label="Resize video / detail"
            {...vSplit.handleProps}
          />
          <div className="qc-detail-pane">
            <QcEventDetail
              event={selected}
              detail={detail.data}
              loading={detail.isFetching}
              error={detail.isError}
              cpsLimits={cpsLimits}
              draft={editing.draft}
              onDraftChange={editing.changeDraft}
              dirty={editing.dirty}
              saving={editing.save.state === 'busy'}
              saveError={saveMessage}
              onFlush={() => { void editing.flushSelected(); }}
              onToggleHidden={() => { void editing.toggleHidden(); }}
              onResolveIssue={(id) => { void editing.resolveIssue(id); }}
              resolvingIssueId={editing.resolvingIssueId}
              onRestoreAi={() => { void editing.restoreAi(); }}
              actionBusy={editing.action.state === 'busy'}
              getVideoTimeMs={editing.getVideoTimeMs}
              newDraft={editing.newDraft}
              newError={editing.newError}
              styles={list.styles}
              onNewDraftChange={editing.setNewDraft}
              onCreate={() => { void editing.createEvent(); }}
              onCancelNew={editing.cancelNewEvent}
            />
            {actionMessage && <Alert color="red" p="xs" mt="xs" data-testid="qc-action-error">{actionMessage}</Alert>}
          </div>
        </div>

        <div
          className="qc-splitter qc-splitter-h"
          data-dragging={hSplit.dragging}
          aria-label="Resize workspace / event list"
          {...hSplit.handleProps}
        />

        <div className="qc-right">
          {events.length === 0 ? (
            <Center h="100%" p="md">
              <Text c="dimmed" ta="center">
                {list.total_count > 0
                  ? 'All events of this file are hidden. Turn on “Show hidden” to see them.'
                  : 'This file has no subtitle events.'}
              </Text>
            </Center>
          ) : (
            <QcEventList
              events={events}
              cpsLimits={cpsLimits}
              active={active}
              selectedId={editing.selectedId}
              follow={follow}
              reveal={editing.reveal}
              onSelect={handleSelect}
              onSeek={handleSeek}
              onUserScroll={handleUserScroll}
            />
          )}
        </div>
      </div>
    </div>
  );
}
