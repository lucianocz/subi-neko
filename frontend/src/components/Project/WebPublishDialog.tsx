import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, Checkbox, Group, Loader, Modal, ScrollArea, Select, Stack, Stepper, Table, Text, Textarea, TextInput } from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import client from '../../api/client';
import { usePublishWeb, useSaveWeb, useWebState, type WebState } from '../../hooks/useWebPublishing';
import { apiMessage } from '../../utils/tmdbShared';
import { emptyDraft, IMPORT_MODAL_WIDTH, mappingErrors, type MappingDraft } from '../../utils/tmdbImport';
import { assignFile, episodeKey, prepareAssignments, uploadFileIds, type Assignments, type PublishEpisode } from '../../utils/publicationWorkflow';
import { TmdbMappingEditor } from '../Tmdb/TmdbMappingEditor';
import { PublicationProgress } from './PublicationProgress';

function Episodes({ episodes, state, assignments, onAssignments, disabled }: {
  episodes: PublishEpisode[]; state: WebState; assignments: Assignments; onAssignments: (a: Assignments) => void;
  disabled: boolean;
}) {
  return <ScrollArea.Autosize mah={330} offsetScrollbars><Table style={{ minWidth: 640 }}>
    <Table.Thead><Table.Tr><Table.Th>Episode</Table.Th><Table.Th>Project file</Table.Th><Table.Th>Upload ASS</Table.Th></Table.Tr></Table.Thead>
    <Table.Tbody>{episodes.map(episode => {
      const key = episodeKey(episode);
      const assignment = assignments[key];
      const file = state.files.find(f => f.id === assignment?.fileId);
      const eligible = file?.status === 'accepted' && !file.render_error;
      const used = new Set(Object.entries(assignments).filter(([k]) => k !== key).map(([, a]) => a.fileId));
      return <Table.Tr key={key}><Table.Td><Text size="sm">{String(episode.episode_number).padStart(2, '0')} · {episode.name_english ?? 'Untitled'}</Text></Table.Td>
        <Table.Td><Select disabled={disabled} aria-label={`File for episode ${episode.episode_number}`} placeholder="No file" clearable searchable
          value={file ? String(file.id) : null} data={state.files.filter(f => !used.has(f.id)).map(f => ({ value: String(f.id), label: `${f.filename} · ${f.status}` }))}
          onChange={v => onAssignments(assignFile(assignments, key, state.files.find(f => String(f.id) === v) ?? null))} />
          {file?.render_error && <Text size="xs" c="red">{file.render_error}</Text>}</Table.Td>
        <Table.Td><Checkbox aria-label={`Upload episode ${episode.episode_number}`} disabled={disabled || !eligible} checked={!!eligible && !!assignment?.upload}
          onChange={e => onAssignments({ ...assignments, [key]: { fileId: file?.id ?? null, upload: e.currentTarget.checked } })} /></Table.Td>
      </Table.Tr>;
    })}</Table.Tbody>
  </Table></ScrollArea.Autosize>;
}

function Wizard({ projectId, projectName, initial, onClose }: { projectId: number; projectName: string; initial: WebState; onClose: () => void }) {
  const { data = initial, refetch } = useWebState(projectId, true);
  const [draft, setDraft] = useState<MappingDraft>(() => ({ ...emptyDraft(`project:${projectId}`, projectName),
    config: initial.config, expected: initial.expected_episode_count ?? null, expectedDirty: true, suggestionHandled: true }));
  const updateDraft = useCallback((update: (d: MappingDraft) => MappingDraft) => setDraft(update), []);
  const [step, setStep] = useState(initial.state === 'publishing' ? 2 : 0);
  const [assignments, setAssignments] = useState<Assignments>({});
  const [release, setRelease] = useState(initial.job && initial.state === 'publishing' ? initial.job.release_label : initial.release_label);
  const [changelog, setChangelog] = useState(initial.job && initial.state === 'publishing' ? initial.job.changelog ?? '' : '');
  const [editorBusy, setEditorBusy] = useState(false);
  const [error, setError] = useState('');
  const submitting = useRef(false);
  const preparedPlacement = useRef('');
  // A polling response may establish remote locks, but must not authorize an
  // old form to overwrite somebody else's newer approved edits.
  const approvalKey = (state: WebState) => JSON.stringify([state.config, state.expected_episode_count,
    state.files.map(f => [f.id, f.mapping])]);
  const approvedBase = useRef({ key: approvalKey(initial), version: initial.version });
  const saveVersion = () => approvalKey(data) === approvedBase.current.key ? data.version : approvedBase.current.version;
  const save = useSaveWeb(projectId);
  const publish = usePublishWeb(projectId);
  const busy = data.state === 'publishing';
  const activeStep = busy ? 2 : step;
  const pending = save.isPending || publish.isPending;
  // Reconcile first: remote identities may have committed during an ambiguous failure.
  const remote = useQuery({ queryKey: ['web-remote', projectId], retry: false, staleTime: 0,
    queryFn: async () => (await client.get(`/projects/${projectId}/web/remote`)).data });
  useEffect(() => { if (remote.isSuccess) void refetch(); }, [remote.isSuccess, refetch]);
  const episodes = data.episodes ?? [];
  const selected = uploadFileIds(assignments, data.files);
  const hasUploads = selected.length > 0;
  const updateAssignments = (next: Assignments) => {
    setAssignments(next);
    if (!uploadFileIds(next, data.files).length) { setRelease(''); setChangelog(''); }
  };

  async function next() {
    if (!draft.config || submitting.current) return;
    const errors = mappingErrors(draft.config, draft.expected);
    if (Object.keys(errors).length) { setError(Object.values(errors)[0] ?? 'Review the mapping.'); return; }
    submitting.current = true; setError('');
    try {
      const saved = await save.mutateAsync({ version: saveVersion(), config: draft.config, expected_episode_count: draft.expected! });
      approvedBase.current = { key: approvalKey(saved), version: saved.version };
      setDraft(d => ({ ...d, config: saved.config, expected: saved.expected_episode_count ?? d.expected }));
      // Prepare once on entering the episode step; background refresh never replaces manual choices.
      const placement = JSON.stringify([saved.config?.tmdb_title_id, saved.config?.media_type, saved.config?.episode_structure, saved.config?.order_kind, saved.config?.group_id, saved.config?.season?.external_id]);
      const nextAssignments = prepareAssignments(saved.episodes ?? [], saved.files, preparedPlacement.current === placement ? assignments : {});
      preparedPlacement.current = placement;
      updateAssignments(nextAssignments);
      setStep(1);
    } catch (e) { setError(apiMessage(e)); }
    finally { submitting.current = false; }
  }

  async function start() {
    if (!draft.config || submitting.current) return;
    submitting.current = true; setError('');
    try {
      const mappings = episodes.flatMap(episode => {
        const fileId = assignments[episodeKey(episode)]?.fileId;
        return fileId == null ? [] : [{ file_id: fileId, tmdb_episode_id: episode.tmdb_episode_id, episode_number: episode.episode_number, name_english: episode.name_english }];
      });
      const saved = await save.mutateAsync({ version: saveVersion(), config: draft.config,
        expected_episode_count: draft.expected!, mappings });
      approvedBase.current = { key: approvalKey(saved), version: saved.version };
      await publish.mutateAsync({ version: saved.version, file_ids: selected,
        release_label: hasUploads ? release.trim() : '', changelog: hasUploads ? changelog.trim() || null : null });
      setStep(2);
    } catch (e) { setError(apiMessage(e)); }
    finally { submitting.current = false; }
  }

  return <Stack gap="sm">
    <Stepper active={activeStep} size="xs" iconSize={22} allowNextStepsSelect={false}>
      <Stepper.Step label="Title" /><Stepper.Step label="Episodes" /><Stepper.Step label="Publish" />
    </Stepper>
    {error && <Alert color="red" p="xs">{error}</Alert>}
    {remote.error && <Alert color="red" p="xs">{apiMessage(remote.error)} <Button size="compact-xs" onClick={() => void remote.refetch()}>Retry reconciliation</Button></Alert>}
    {activeStep === 0 && <fieldset disabled={pending || remote.isFetching} style={{ border: 0, padding: 0, margin: 0, minWidth: 0 }}>
      <TmdbMappingEditor publishMode projectId={projectId} version={data.version} onBusyChange={setEditorBusy} locked={data.locked || data.season_locked} provider="anidb"
        result={{ provider_id: String(projectId), title: projectName, title_native: null, year: null, media_type: 'tv', episode_count: null }}
        directory="" draft={draft} onDraft={updateDraft} pending={pending} pendingIntent={null} />
    </fieldset>}
    {activeStep === 1 && <Stack gap="sm">
      <Text size="xs" c="dimmed">All {episodes.length} episode metadata records will be synchronized. Checkboxes select accepted ASS uploads only.</Text>
      <Episodes disabled={pending} episodes={episodes} state={data} assignments={assignments} onAssignments={updateAssignments} />
      {!episodes.length && <Text size="sm" c="dimmed">No episode metadata is available. Title metadata can still be published.</Text>}
      <Group align="flex-start" grow><TextInput label="Release label" required={hasUploads} maxLength={200} disabled={!hasUploads || pending} value={hasUploads ? release : ''} onChange={e => setRelease(e.currentTarget.value)} />
        <Textarea label="Changelog" maxLength={4000} disabled={!hasUploads || pending} value={hasUploads ? changelog : ''} onChange={e => setChangelog(e.currentTarget.value)} /></Group>
    </Stack>}
    {activeStep === 2 && <PublicationProgress state={data} />}
    <Group justify="space-between" pt="sm" style={{ borderTop: '1px solid var(--mantine-color-default-border)' }}>
      {activeStep === 0 ? <Button variant="subtle" onClick={onClose} disabled={pending}>Cancel</Button> : activeStep === 1 ? <Button variant="subtle" disabled={pending} onClick={() => setStep(0)}>Back</Button>
        : <Button variant="subtle" disabled={busy || pending} onClick={() => setStep(1)}>Review / Retry</Button>}
      {activeStep === 0 ? <Button onClick={() => void next()} loading={save.isPending} disabled={!draft.config || editorBusy || !!remote.error || remote.isFetching}>Save &amp; Next</Button>
        : activeStep === 1 ? <Button onClick={() => void start()} loading={pending} disabled={!!remote.error || (hasUploads && !release.trim())}>Publish to Web</Button>
        : <Button disabled={busy || pending} onClick={onClose}>Close</Button>}
    </Group>
  </Stack>;
}

export function WebPublishDialog({ projectId, projectName, opened, onClose }: { projectId: number; projectName: string; opened: boolean; onClose: () => void }) {
  const query = useWebState(projectId, opened);
  const { refetch } = query;
  useEffect(() => { if (opened) void refetch(); }, [opened, refetch]);
  return <Modal opened={opened} onClose={onClose} title="Publish to Subi-neko Web" size={IMPORT_MODAL_WIDTH}>
    {query.isLoading || query.isFetching && !query.data ? <Loader /> : query.error ? <Alert color="red">{apiMessage(query.error)}</Alert> : opened && query.data && <Wizard key={projectId} projectId={projectId} projectName={projectName} initial={query.data} onClose={onClose} />}
  </Modal>;
}
