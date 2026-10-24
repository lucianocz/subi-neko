import { useEffect, useRef, useState } from 'react';
import { Alert, Autocomplete, Badge, Box, Button, Checkbox, Divider, Grid, Group, Image, Loader, Modal, NumberInput, Paper, Progress, ScrollArea, Select, Stack, Stepper, Table, Text, Textarea, TextInput } from '@mantine/core';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import client from '../../api/client';
import { usePublishWeb, useSaveWeb, useWebState, webApi, type Catalog, type SearchResult, type TitleDetails, type WebConfig, type WebState } from '../../hooks/useWebPublishing';
import { DescriptionRequestGuard, refreshDescription, outcomeLabel, seasonLabel, suggestMappings, type WebMapping } from '../../utils/webPublishing';
import { apiErrorDetail } from '../../utils/projectArchive';

function message(error: unknown) { return apiErrorDetail(error).message ?? 'Request failed. Please retry.'; }
const posterUrl = (path: string) => `https://image.tmdb.org/t/p/w342${path}`;

function titleQuery(media: string, id: string) {
  return { queryKey: ['web-title', media, id], queryFn: () => webApi.details(media, id), staleTime: 60_000, retry: false as const };
}

function titleConfig(detail: TitleDetails): WebConfig {
  return { tmdb_title_id: detail.tmdb_title_id, media_type: detail.media_type, episode_structure: detail.episode_structure,
    order_kind: detail.order_kind, group_id: detail.group_id, season: detail.season, metadata: detail.metadata, poster_path: detail.poster_path };
}

function SearchCard({ result, busy, selected, onSelect }: {
  result: SearchResult; busy: boolean; selected: boolean; onSelect: () => void;
}) {
  const detail = useQuery(titleQuery(result.media_type, result.id));
  const names = detail.data?.romaji_titles ?? (result.title_romaji ? [result.title_romaji] : []);
  const seasons = detail.data?.number_of_seasons ?? result.number_of_seasons;
  return <Paper component="button" type="button" p="sm" withBorder disabled={busy} onClick={onSelect}
    aria-pressed={selected} style={{ width: '100%', textAlign: 'left', cursor: busy ? 'wait' : 'pointer',
      color: 'inherit', borderColor: selected ? 'var(--mantine-primary-color-filled)' : undefined }}>
    <Group wrap="nowrap">{result.poster_path && <Image src={posterUrl(result.poster_path)} w={48} h={72} fit="cover" alt="" />}
      <Stack gap={3} style={{ flex: 1, minWidth: 0 }}><Text fw={600} lineClamp={2}>{result.title}</Text>
        {names.length > 0 && <Text size="sm" c="dimmed" lineClamp={2}>{names.join(' / ')}</Text>}
        <Text size="xs" c="dimmed">{[result.year, result.media_type === 'tv' ? 'TV' : 'Movie',
          seasons != null ? `${seasons} season${seasons === 1 ? '' : 's'}` : null].filter(Boolean).join(' · ')}</Text>
      </Stack>{busy && selected && <Loader size="xs" />}</Group>
  </Paper>;
}

function TitleStep({ config, setConfig, saved, locked, projectName, onTranslate, translating, invalidateTranslation }: {
  config: WebConfig | null; setConfig: (value: WebConfig) => void; saved: boolean; locked: boolean; projectName: string;
  onTranslate: () => void; translating: boolean;
  invalidateTranslation: () => void;
}) {
  const [query, setQuery] = useState(projectName);
  const [search, setSearch] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const refreshGuard = useRef(new DescriptionRequestGuard());
  useEffect(() => {
    const guard = refreshGuard.current;
    return () => guard.invalidate();
  }, []);
  const qc = useQueryClient();
  const detail = useQuery({ ...titleQuery(config?.media_type ?? 'tv', config?.tmdb_title_id ?? ''), enabled: !!config });
  const names = detail.data?.romaji_titles ?? [];
  const results = useQuery({ queryKey: ['web-search', search], enabled: !!search,
    queryFn: () => webApi.search(search), staleTime: 60_000 });
  function searchAgain() {
    const next = query.trim();
    if (next && next === search) void results.refetch();
    else setSearch(next);
  }
  async function select(media: string, id: string) {
    invalidateTranslation();
    refreshGuard.current.invalidate();
    setBusy(true); setError('');
    try {
      const detail = titleConfig(await qc.fetchQuery(titleQuery(media, id)));
      const remote = await webApi.lookup(detail);
      if (remote) {
        detail.episode_structure = remote.episode_structure;
        detail.order_kind = remote.episode_order.kind;
        detail.group_id = remote.episode_order.group_id;
      }
      setConfig(detail);
    } catch (e) { setError(message(e)); } finally { setBusy(false); }
  }
  async function refresh() {
    if (!config) return;
    invalidateTranslation();
    const revision = refreshGuard.current.capture();
    setBusy(true); setError('');
    try {
      const refreshed = await webApi.details(config.media_type, config.tmdb_title_id, true);
      if (!refreshGuard.current.isCurrent(revision)) return;
      qc.setQueryData(titleQuery(config.media_type, config.tmdb_title_id).queryKey, refreshed);
      setConfig({ ...config, metadata: refreshDescription(config.metadata, refreshed.metadata), poster_path: refreshed.poster_path ?? config.poster_path });
    }
    catch (e) { setError(message(e)); } finally { setBusy(false); }
  }
  function field<K extends keyof WebConfig['metadata']>(key: K, value: WebConfig['metadata'][K]) {
    refreshGuard.current.invalidate();
    if (config) setConfig({ ...config, metadata: { ...config.metadata, [key]: value } });
  }
  return <Stack>
    {!saved && !locked && <>
      <Group align="flex-end"><TextInput label="Search TMDB" value={query} onChange={e => setQuery(e.currentTarget.value)} style={{ flex: 1 }} onKeyDown={e => { if (e.key === 'Enter') searchAgain(); }} />
        <Button onClick={searchAgain} disabled={!query.trim()} loading={results.isFetching}>Search</Button></Group>
      {results.error && <Alert color="red">{message(results.error)}</Alert>}
      <Paper p="sm" withBorder><Stack gap="xs"><Text size="sm" fw={600}>Search results</Text>
        <ScrollArea.Autosize mah={250}><Stack gap="xs">{results.data?.map(r => <SearchCard key={`${r.media_type}:${r.id}`}
          result={r} busy={busy} selected={config?.media_type === r.media_type && config.tmdb_title_id === r.id}
          onSelect={() => void select(r.media_type, r.id)} />)}</Stack></ScrollArea.Autosize>
        {!results.data?.length && !results.isFetching && <Text size="sm" c="dimmed">{search ? 'No matching titles found.' : 'Search TMDB to choose a title.'}</Text>}
      </Stack></Paper>
    </>}
    {error && <Alert color="red">{error}</Alert>}
    {config && <><Divider label="Title details" labelPosition="left" size="sm" /><Paper withBorder p="md"><Group align="flex-start" wrap="wrap">
      <Stack w={150}>{config.poster_path && <Image src={posterUrl(config.poster_path)} alt="Approved TMDB poster" radius="sm" />}
        <Badge>{config.media_type} · TMDB {config.tmdb_title_id}</Badge>
        <Button size="xs" variant="light" onClick={() => void refresh()} loading={busy}>Refresh metadata</Button>
        {locked && <Text size="xs" c="dimmed">Title identity locked</Text>}</Stack>
      <Stack style={{ flex: 1, minWidth: 240 }} gap="xs">
        <Grid columns={10} gap="xs">
          <Grid.Col span={8}><TextInput label="English title" required maxLength={255} value={config.metadata.title_english} onChange={e => field('title_english', e.currentTarget.value)} /></Grid.Col>
          <Grid.Col span={2}><NumberInput label="Year" min={1} max={9999} allowDecimal={false} value={config.metadata.year ?? ''} onChange={v => field('year', typeof v === 'number' ? v : null)} /></Grid.Col>
          <Grid.Col span={8}>{names.length > 1
            ? <Autocomplete label="Romaji title" placeholder="Choose a name or enter your own" data={names} maxLength={255} value={config.metadata.title_romaji ?? ''} onChange={v => field('title_romaji', v || null)} />
            : <TextInput label="Romaji title" maxLength={255} value={config.metadata.title_romaji ?? ''} onChange={e => field('title_romaji', e.currentTarget.value || null)} />}</Grid.Col>
          <Grid.Col span={2}><TextInput label="Language" required maxLength={3} value={config.metadata.original_language} onChange={e => field('original_language', e.currentTarget.value)} /></Grid.Col>
        </Grid>
        <Textarea label="Description" rows={5} maxLength={10000} value={config.metadata.description ?? ''} onChange={e => field('description', e.currentTarget.value || null)} />
        <Group justify="flex-end"><Button size="compact-xs" variant="subtle" loading={translating}
          disabled={busy || !config.metadata.original_description?.trim()} onClick={onTranslate}>Translate with AI</Button></Group>
      </Stack>
    </Group></Paper></>}
  </Stack>;
}

function EpisodeStep({ config, setConfig, state, mappings, setMappings }: {
  config: WebConfig; setConfig: (c: WebConfig) => void; state: WebState;
  mappings: Record<number, WebMapping>; setMappings: (m: Record<number, WebMapping>) => void;
}) {
  const [error, setError] = useState('');
  const qc = useQueryClient();
  const catalog = useQuery({ queryKey: ['web-catalog', config.tmdb_title_id, config.group_id, config.season?.season_number],
    enabled: config.media_type === 'tv', queryFn: () => webApi.catalog(config), staleTime: 60_000 });
  const data = catalog.data;
  const episodes = data?.episodes_by_season[config.season?.external_id ?? ''] ?? [];
  const samePlacement = state.config?.tmdb_title_id === config.tmdb_title_id && state.config?.order_kind === config.order_kind
    && state.config?.group_id === config.group_id && state.config?.season?.external_id === config.season?.external_id
    && state.config?.episode_structure === config.episode_structure;
  const assigned = new Set(Object.values(mappings).map(m => m.tmdb_episode_id));
  useEffect(() => {
    if (config.media_type === 'tv' && config.episode_structure === 'SEASONED' && !config.season
      && !state.season_locked && (config.order_kind === 'standard' || config.group_id) && data?.seasons.length === 1) {
      setConfig({ ...config, season: data.seasons[0] });
    }
  }, [config, data, state.season_locked, setConfig]);
  useEffect(() => {
    const choices = data?.episodes_by_season[config.season?.external_id ?? ''] ?? [];
    const restored = state.files.map(f => ({ ...f, mapping: mappings[f.id] ?? (samePlacement ? f.mapping : null) }));
    const suggested = suggestMappings(restored, choices);
    if (config.episode_structure === 'FLAT') {
      state.files.forEach((f, index) => {
        if (!suggested[f.id]) suggested[f.id] = { tmdb_episode_id: null, episode_number: index + 1,
          name_english: state.files.length === 1 ? config.metadata.title_english : `${config.metadata.title_english} · ${index + 1}` };
      });
    }
    const next = { ...suggested, ...mappings };
    if (JSON.stringify(next) !== JSON.stringify(mappings)) setMappings(next);
  }, [data, config.season, config.episode_structure, config.metadata.title_english, state.files, samePlacement, mappings, setMappings]);
  const placementLocked = state.season_locked;
  async function refresh() {
    try {
      const value = await webApi.catalog(config, true);
      qc.setQueryData<Catalog>(['web-catalog', config.tmdb_title_id, config.group_id, config.season?.season_number], value);
    } catch (e) { setError(message(e)); }
  }
  function suggest() {
    const files = state.files.map(f => ({ ...f, mapping: mappings[f.id] ?? f.mapping }));
    setMappings({ ...suggestMappings(files, episodes), ...mappings });
  }
  return <Stack>
    {config.media_type === 'tv' && <>
      <Group grow wrap="nowrap" align="flex-end"><Select label="Episode order" disabled={state.locked} value={config.order_kind}
        data={[{ value: 'standard', label: 'Standard' }, { value: 'episode_group', label: 'Alternative episode group' }]}
        onChange={v => { setConfig({ ...config, order_kind: v as WebConfig['order_kind'], group_id: null, season: null }); setMappings({}); }} />
        <Select label="Structure" disabled={state.locked} value={config.episode_structure} data={['SEASONED', 'FLAT']}
          onChange={v => { setConfig({ ...config, episode_structure: v as WebConfig['episode_structure'], season: null }); setMappings({}); }} />
      {config.order_kind === 'episode_group' && <Select label="Episode group" disabled={state.locked} value={config.group_id} searchable
        data={(data?.groups.length ? data.groups : config.group_id ? [{ id: config.group_id, name: config.group_id }] : []).map(g => ({ value: g.id, label: g.name }))}
        onChange={v => { setConfig({ ...config, group_id: v, season: null }); setMappings({}); }} />}
      {config.episode_structure === 'SEASONED' && <Select label="Project season / subgroup" disabled={placementLocked} searchable value={config.season?.external_id ?? null}
        data={[...(data?.seasons ?? []), ...(config.season && !data?.seasons.some(s => s.external_id === config.season?.external_id) ? [config.season] : [])]
          .map(s => ({ value: s.external_id, label: `${s.season_number} · ${s.name_english ?? s.external_id}` }))}
        onChange={v => { setConfig({ ...config, season: data?.seasons.find(s => s.external_id === v) ?? null }); setMappings({}); }} />}</Group>
      <Group><Button size="xs" variant="light" loading={catalog.isFetching} onClick={() => void refresh()}>Refresh episodes</Button>
        <Button size="xs" variant="light" disabled={!episodes.length} onClick={suggest}>Suggest from stored episode numbers</Button></Group>
      {(catalog.error || error) && <Alert color="yellow">{error || message(catalog.error)} Saved mappings remain available.</Alert>}
    </>}
    {config.media_type === 'movie' && <Text size="sm">Movie · FLAT · Standard order. Each file maps to one distinct content item.</Text>}
    <ScrollArea><Table style={{ minWidth: 680 }}><Table.Thead><Table.Tr><Table.Th>Project file</Table.Th><Table.Th>Website episode / content item</Table.Th></Table.Tr></Table.Thead>
      <Table.Tbody>{state.files.map(f => {
        const current = mappings[f.id] ?? (samePlacement ? f.mapping : null);
        const missing = current?.tmdb_episode_id && !episodes.some(e => e.tmdb_episode_id === current.tmdb_episode_id);
        return <Table.Tr key={f.id}><Table.Td w="45%"><Text size="sm" style={{ overflowWrap: 'anywhere' }}>{f.filename}</Text>
          <Text size="xs" c="dimmed">Episode {f.episode_number ?? '?'} · {f.status}{f.locked ? ' · Locked' : ''}</Text></Table.Td>
          <Table.Td>{config.episode_structure === 'FLAT' ? <Group wrap="nowrap" align="flex-end">
            <NumberInput label="Number" w={90} min={0} max={2147483647} allowDecimal={false} disabled={f.locked} value={current?.episode_number ?? f.episode_number ?? 1}
              onChange={v => { if (typeof v === 'number') setMappings({ ...mappings, [f.id]: { tmdb_episode_id: current?.tmdb_episode_id ?? null, name_english: current?.name_english ?? config.metadata.title_english, episode_number: v } }); }} />
            <TextInput label="English name" style={{ flex: 1 }} maxLength={255} disabled={f.locked} value={current?.name_english ?? ''}
              onChange={e => setMappings({ ...mappings, [f.id]: { tmdb_episode_id: current?.tmdb_episode_id ?? null, episode_number: current?.episode_number ?? f.episode_number ?? 1, name_english: e.currentTarget.value || null } })} />
          </Group> : <Select placeholder="Confirm an episode" searchable disabled={f.locked} value={current?.tmdb_episode_id ?? null}
            data={[...episodes.filter(e => !assigned.has(e.tmdb_episode_id) || e.tmdb_episode_id === current?.tmdb_episode_id),
              ...(missing && current ? [{ ...current, tmdb_episode_id: current.tmdb_episode_id! }] : [])]
              .map(e => ({ value: e.tmdb_episode_id, label: `${e.episode_number} · ${e.name_english ?? e.tmdb_episode_id}` }))}
            onChange={v => { const episode = episodes.find(e => e.tmdb_episode_id === v); if (episode) setMappings({ ...mappings, [f.id]: episode }); }} />}
            {current && <Text size="xs" c="dimmed" style={{ overflowWrap: 'anywhere' }}>Approved: {current.episode_number} · {current.name_english ?? 'No English name'}</Text>}
            {missing && current && <Text size="xs" c="yellow">Saved identity retained; not present in current TMDB results.</Text>}
            {!f.locked && current?.tmdb_episode_id && episodes.some(e => e.tmdb_episode_id === current.tmdb_episode_id && e.name_english !== current.name_english)
              && <Button size="compact-xs" variant="subtle" onClick={() => {
                const updated = episodes.find(e => e.tmdb_episode_id === current.tmdb_episode_id);
                if (updated) setMappings({ ...mappings, [f.id]: updated });
              }}>Use refreshed episode metadata</Button>}
            {!current && <Text size="xs" c="yellow">Mapping needs confirmation</Text>}
          </Table.Td></Table.Tr>;
      })}</Table.Tbody></Table></ScrollArea>
  </Stack>;
}

function Wizard({ projectId, projectName, initial, onClose }: { projectId: number; projectName: string; initial: WebState; onClose: () => void }) {
  const queryClient = useQueryClient();
  const { data = initial } = useWebState(projectId, true);
  const [config, setConfigState] = useState<WebConfig | null>(initial.config);
  const [translating, setTranslating] = useState(false);
  const translationGuard = useRef(new DescriptionRequestGuard());
  const translationActive = useRef(false);
  useEffect(() => {
    const guard = translationGuard.current;
    return () => guard.invalidate();
  }, []);
  function setConfig(value: WebConfig) {
    translationGuard.current.invalidate();
    setConfigState(value);
  }
  const [step, setStep] = useState(initial.state === 'publishing' ? 3 : 0);
  const [mappings, setMappings] = useState<Record<number, WebMapping>>(() => Object.fromEntries(initial.files.filter(f => f.mapping).map(f => [f.id, f.mapping!])));
  const [selected, setSelected] = useState(initial.state === 'publishing' && initial.job ? initial.job.file_ids : initial.files.filter(f => f.status === 'accepted').map(f => f.id));
  const [release, setRelease] = useState(initial.state === 'publishing' && initial.job ? initial.job.release_label : initial.release_label);
  const [changelog, setChangelog] = useState(initial.state === 'publishing' ? initial.job?.changelog ?? '' : '');
  const [error, setError] = useState('');
  const save = useSaveWeb(projectId);
  const publish = usePublishWeb(projectId);
  async function translateDescription() {
    if (!config || translationActive.current) return;
    translationActive.current = true;
    setTranslating(true); setError('');
    const guard = translationGuard.current;
    const revision = guard.capture();
    try {
      const proposal = await webApi.translate(projectId, data.version, config);
      if (!guard.isCurrent(revision)) return;
      // Persist only after confirming the wizard still owns this exact draft.
      // Existing save performs the database version CAS, without an LLM transaction.
      const saved = await save.mutateAsync({ version: data.version,
        config: { ...config, metadata: { ...config.metadata, description: proposal.description } } });
      if (guard.isCurrent(revision)) setConfigState(saved.config);
      void queryClient.invalidateQueries({ queryKey: ['projects', projectId, 'llm-metrics'] });
    } catch (e) { if (guard.isCurrent(revision)) setError(message(e)); }
    finally { translationActive.current = false; setTranslating(false); }
  }
  const busy = data.state === 'publishing';
  const activeStep = busy ? 3 : step;
  const job = useQuery({ queryKey: ['jobs', data.job?.id], enabled: !!data.job?.id && busy,
    queryFn: async () => (await client.get<{ progress: number; message: string }>(`/jobs/${data.job?.id}`)).data,
    refetchInterval: busy ? 1500 : false });
  async function next() {
    if (!config) return;
    setError('');
    if (!config.metadata.title_english.trim()) { setError('Enter an English title.'); return; }
    if (!/^[a-z]{2,3}$/.test(config.metadata.original_language)) { setError('Language must be a two- or three-letter lowercase code.'); return; }
    if (config.order_kind === 'episode_group' && !config.group_id) { setError('Select an episode group.'); return; }
    try {
      const rows = step === 1 ? Object.entries(mappings).map(([id, m]) => ({ file_id: Number(id), ...m })) : undefined;
      await save.mutateAsync({ version: data.version, config, mappings: rows });
      setStep(step + 1);
    } catch (e) { setError(message(e)); }
  }
  async function start() {
    setError('');
    try {
      await publish.mutateAsync({ version: data.version, file_ids: selected, release_label: release, changelog: changelog || null });
      setStep(3);
    }
    catch (e) { setError(message(e)); }
  }
  const result = data.job?.result;
  const remote = useQuery({ queryKey: ['web-remote', projectId, data.version, data.job?.status],
    enabled: !!data.config && !busy, retry: false, staleTime: Infinity, refetchOnMount: 'always',
    queryFn: async () => {
      const response = await client.get(`/projects/${projectId}/web/remote`);
      void queryClient.invalidateQueries({ queryKey: ['projects', projectId, 'web'] });
      return response.data;
    } });
  return <Stack mih={600}>
    <Stepper active={activeStep} size="sm" onStepClick={activeStep === 3 ? undefined : s => { if (s < step) setStep(s); }}>
      <Stepper.Step label="Title" /><Stepper.Step label="Episodes" /><Stepper.Step label="Publish" /><Stepper.Step label="Progress" />
    </Stepper>
    {error && <Alert color="red">{error}</Alert>}
    {remote.error && <Alert color="red">{message(remote.error)}</Alert>}
    <Box style={{ flex: 1 }}>
      {activeStep === 0 && <fieldset disabled={save.isPending} style={{ border: 0, padding: 0, margin: 0, minWidth: 0 }}><TitleStep projectName={projectName} config={config} setConfig={value => { if (value.tmdb_title_id !== config?.tmdb_title_id) setMappings({}); setConfig(value); }} saved={data.locked} locked={data.locked}
        onTranslate={() => void translateDescription()} translating={translating}
        invalidateTranslation={() => translationGuard.current.invalidate()} /></fieldset>}
      {activeStep === 1 && config && <EpisodeStep config={config} setConfig={setConfig} state={data} mappings={mappings} setMappings={setMappings} />}
      {activeStep === 2 && config && <Stack>
        <Paper withBorder p="md"><Stack gap="xs">
        <Group align="flex-start" wrap="nowrap">{config.poster_path && <Image src={posterUrl(config.poster_path)} w={65} alt="Approved poster" />}
          <Stack gap={3}><Text fw={600}>{config.metadata.title_english}{config.metadata.title_romaji && ` (${config.metadata.title_romaji})`}</Text><Text size="sm">{config.media_type} · {config.episode_structure} · {config.order_kind} · {config.season ? seasonLabel(config.season) : 'Content items directly under title'}</Text>
            <Text size="xs" c="dimmed">{config.metadata.year} · {config.metadata.original_language}</Text></Stack></Group>
        {config.metadata.description && <Text size="sm" lineClamp={4}>{config.metadata.description}</Text>}
        </Stack></Paper>
        <Divider label="Files to publish" labelPosition="left" size="sm" />
        <ScrollArea.Autosize mah={230}><Stack gap="xs">{data.files.filter(f => f.status === 'accepted').map(f => <Checkbox key={f.id}
          disabled={busy} checked={selected.includes(f.id)} onChange={e => setSelected(e.currentTarget.checked ? [...selected, f.id] : selected.filter(id => id !== f.id))}
          label={<Box><Text size="sm" style={{ overflowWrap: 'anywhere' }}>{f.filename}</Text><Text size="xs" c={f.mapping && !f.render_error ? 'dimmed' : 'yellow'}>
            {f.render_error ?? (f.mapping ? `${f.mapping.episode_number} · ${f.mapping.name_english ?? ''}` : 'Missing confirmed mapping')}
          </Text></Box>} />)}</Stack></ScrollArea.Autosize>
        <Divider label="Release details" labelPosition="left" size="sm" />
        <TextInput label="Release label" required maxLength={200} disabled={busy} value={release} onChange={e => setRelease(e.currentTarget.value)} />
        <Textarea label="Changelog (optional)" maxLength={4000} disabled={busy} value={changelog} onChange={e => setChangelog(e.currentTarget.value)} />
      </Stack>}
      {activeStep === 3 && <Stack>
        <Text fw={600}>Publication progress</Text>
        {busy && <Stack gap="xs"><Progress value={(job.data?.progress ?? 0) * 100} animated /><Text size="sm">{job.data?.message ?? result?.stage ?? 'Queued'}</Text>
          <Button variant="light" color="orange" size="xs" onClick={() => { void client.post(`/jobs/${data.job?.id}/cancel`).catch(e => setError(message(e))); }}>Request cancellation</Button>
          <Text size="xs" c="dimmed">An in-flight request may finish. The next publication stays blocked until this worker stops.</Text></Stack>}
        {result && <Paper withBorder p="sm"><Stack gap={4}><Text fw={600}>{result.stage}</Text>
          <Text size="sm">Subtitle results: {(['created', 'updated', 'noop', 'failed'] as const)
            .map(outcome => `${outcomeLabel(outcome)}: ${result.files.filter(file => file.outcome === outcome).length}`).join(' · ')}</Text>
          {result.metadata.map(r => <Text size="sm" key={r.stage}>{r.stage}: {outcomeLabel(r.outcome)}</Text>)}
          {result.poster && <Text size="sm" c={result.poster.error ? 'red' : undefined}>Poster: {outcomeLabel(result.poster.outcome)} {result.poster.error}</Text>}
          {result.files.map(r => <Text size="sm" key={r.file_id} c={r.outcome === 'failed' ? 'red' : undefined} style={{ overflowWrap: 'anywhere' }}>{r.filename}: {outcomeLabel(r.outcome)} {r.error}</Text>)}
          {result.error && <Text c="red" size="sm">{result.error.message}</Text>}
        </Stack></Paper>}
      </Stack>}
    </Box>
    {activeStep === 3 ? <Group justify="flex-end"><Button variant="light" onClick={onClose}>Close</Button></Group> : <Group justify="space-between"><Button variant="subtle" disabled={step === 0 || busy} onClick={() => setStep(step - 1)}>Back</Button>
      {step < 2 ? <Button disabled={!config || busy || translating || (step === 1 && config.episode_structure === 'SEASONED' && !config.season)} loading={save.isPending} onClick={() => void next()}>Save &amp; Next</Button>
        : <Button disabled={busy || remote.isFetching || !!remote.error || !release.trim() || !selected.length || selected.some(id => { const f = data.files.find(f => f.id === id); return !f?.mapping || !!f.render_error; })}
          loading={publish.isPending} onClick={() => void start()}>Publish to Web</Button>}
    </Group>}
  </Stack>;
}

export function WebPublishDialog({ projectId, projectName, opened, onClose }: { projectId: number; projectName: string; opened: boolean; onClose: () => void }) {
  const query = useWebState(projectId, opened);
  const { refetch } = query;
  // Refresh local hashes and remote read-only state each time the wizard opens.
  useEffect(() => { if (opened) void refetch(); }, [opened, refetch]);
  return <Modal opened={opened} onClose={onClose} title="Publish to Subi-neko Web" size={1120}>
    <Box mih={600}>{query.isLoading ? <Loader /> : query.error ? <Alert color="red">{message(query.error)}</Alert> : opened && query.data && <Wizard key={projectId} projectId={projectId} projectName={projectName} initial={query.data} onClose={onClose} />}</Box>
  </Modal>;
}
