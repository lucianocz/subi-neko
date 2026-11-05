import { useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Badge, Box, Button, Group, Image, Loader, NumberInput, ScrollArea, Select, Stack, Text, TextInput, Tooltip } from '@mantine/core';
import { ArrowLeft, MagnifyingGlass } from '@phosphor-icons/react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { SearchResult as SourceResult } from '../../types';
import { translateImportDescription, useAppConfig, useTmdbSuggestion } from '../../hooks/useImport';
import { titleQuery } from '../../hooks/useTmdbTitle';
import { webApi, type WebConfig } from '../../hooks/useWebPublishing';
import { apiErrorDetail } from '../../utils/projectArchive';
import { apiMessage, posterUrl, titleConfig } from '../../utils/tmdbShared';
import { DescriptionRequestGuard, refreshDescription, seasonLabel } from '../../utils/webPublishing';
import {
  FIELD_WIDTH, MAX_EXPECTED_EPISODES, applyLatest, autoSeason, buildImportBody, candidateLabel, effectiveExpected,
  episodeMapSummary, expectedBasisText, groupLabel, importActions, mappingErrors, parseExpectedInput,
  shouldApplySuggestion, sourceKey, suggestExpected, suggestionNotice, suggestionTarget,
  withGroup, withOrder, withSeason, withStructure,
  type ImportBody, type ImportIntent, type MappingDraft, type SourceProvider, type SuggestionCandidate,
} from '../../utils/tmdbImport';
import { TitleMetadataFields } from './TitleMetadataFields';
import { TmdbSearchCard } from './TmdbSearchCard';

interface Props {
  provider: SourceProvider;
  result: SourceResult;
  directory: string;
  draft: MappingDraft;
  /** Functional update of the dialog-owned draft (so Back never loses it). */
  onDraft: (update: (d: MappingDraft) => MappingDraft) => void;
  onBack?: () => void;
  locked?: boolean;
  publishMode?: boolean;
  projectId?: number;
  version?: number;
  onBusyChange?: (busy: boolean) => void;
  onSubmit?: (intent: ImportIntent, body: ImportBody) => void;
  pending: boolean;
  pendingIntent: ImportIntent | null;
}

const sameTitle = (c: WebConfig | null, t: { media_type: string; tmdb_id: string }) =>
  !!c && c.media_type === t.media_type && c.tmdb_title_id === t.tmdb_id;

/** A suggested TMDB title, resolved to its name so the user can recognise it. */
function CandidateButton({ media, id, label, disabled, onPick }: {
  media: 'tv' | 'movie'; id: string; label: string; disabled: boolean; onPick: () => void;
}) {
  const detail = useQuery(titleQuery(media, id));
  const m = detail.data?.metadata;
  return <Tooltip label={label} openDelay={300}>
    <Button size="compact-sm" variant="default" disabled={disabled} onClick={onPick}
      leftSection={detail.isFetching ? <Loader size={10} /> : undefined}>
      {m ? `${m.title_english}${m.year ? ` (${m.year})` : ''}` : label}
    </Button>
  </Tooltip>;
}

export function TmdbMappingEditor({ provider, result, directory, draft, onDraft, onBack, onSubmit, pending, pendingIntent, locked = false, publishMode = false, projectId, version = 0, onBusyChange }: Props) {
  const qc = useQueryClient();
  const key = publishMode ? draft.sourceKey : sourceKey(provider, result.provider_id);
  const config = draft.config;
  const appConfig = useAppConfig();
  const tmdbOff = appConfig.data?.tmdb_available === false;

  const [query, setQuery] = useState(draft.searchQuery);
  const [search, setSearch] = useState(draft.searchQuery);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [translating, setTranslating] = useState(false);
  useEffect(() => { onBusyChange?.(busy || translating); }, [busy, translating, onBusyChange]);
  // Latest-request guards: a title switch, edit, Back or unmount drops late responses.
  const selectGuard = useRef(new DescriptionRequestGuard());
  const translateGuard = useRef(new DescriptionRequestGuard());
  const suggestionStarted = useRef(false);
  useEffect(() => {
    const select = selectGuard.current;
    const translate = translateGuard.current;
    return () => { select.invalidate(); translate.invalidate(); };
  }, []);

  const suggestion = useTmdbSuggestion(provider, result.provider_id, !publishMode && !draft.skipped);
  const target = suggestionTarget(suggestion.data);
  const results = useQuery({ queryKey: ['web-search', search], enabled: !!search && !tmdbOff && !draft.skipped,
    queryFn: () => webApi.search(search), staleTime: 60_000, retry: false });
  const searchOff = tmdbOff || apiErrorDetail(results.error).code === 'tmdb_unconfigured';

  const tv = config?.media_type === 'tv';
  const baseKey = (title: string, group: string | null, season: number | undefined) => ['web-catalog', title, group, season];
  const base = useQuery({ queryKey: baseKey(config?.tmdb_title_id ?? '', null, undefined), enabled: !!config && tv && !draft.skipped,
    queryFn: () => webApi.catalog({ ...config!, group_id: null, season: null }), staleTime: 60_000, retry: false });
  const scopedWanted = !!config && tv && (!!config.group_id || !!config.season);
  const scoped = useQuery({ queryKey: baseKey(config?.tmdb_title_id ?? '', config?.group_id ?? null, config?.season?.season_number),
    enabled: scopedWanted && !draft.skipped, queryFn: () => webApi.catalog(config!), staleTime: 60_000, retry: false });
  const catalog = scopedWanted ? scoped : base;
  const detail = useQuery({ ...titleQuery(config?.media_type ?? 'tv', config?.tmdb_title_id ?? ''), enabled: !!config && !draft.skipped });

  const seasons = useMemo(
    () => (config?.order_kind === 'episode_group' ? scoped.data?.seasons : base.data?.seasons) ?? [],
    [config?.order_kind, scoped.data?.seasons, base.data?.seasons],
  );
  const groups = base.data?.groups ?? [];
  const episodes = config?.season ? catalog.data?.episodes_by_season[config.season.external_id] ?? [] : [];

  // The AniBridge candidate that describes the selected title (standard order only).
  const candidatesForTitle: SuggestionCandidate[] = config && config.order_kind === 'standard'
    ? (suggestion.data?.candidates ?? []).filter(c => sameTitle(config, c)) : [];
  const chosenCandidate = candidatesForTitle.find(c => c.season_number === (config?.season?.season_number ?? null)) ?? null;
  const expectedSuggestion = config
    ? suggestExpected({ config, candidate: chosenCandidate, seasonEpisodeCount: config.season ? episodes.length || null : null,
      sourceEpisodeCount: result.episode_count })
    : { value: null, basis: null };
  const expected = effectiveExpected(draft, expectedSuggestion);
  const errors = mappingErrors(config, expected);
  const suggestedSeasonNumber = config && target && sameTitle(config, target) ? target.season_number : null;

  // Pick the season once the catalog is known: the AniBridge one, or the only one.
  useEffect(() => {
    if (!config || draft.skipped || locked) return;
    const picked = autoSeason(config, seasons, suggestedSeasonNumber);
    if (!picked) return;
    onDraft(d => d.config && !d.config.season && d.config.tmdb_title_id === config.tmdb_title_id
      && d.config.group_id === config.group_id ? { ...d, config: withSeason(d.config, picked) } : d);
  }, [config, seasons, suggestedSeasonNumber, draft.skipped, onDraft, locked]);

  async function selectTitle(media: 'tv' | 'movie', id: string) {
    selectGuard.current.invalidate();          // supersede any title still loading
    translateGuard.current.invalidate();       // …and any translation of the old title
    onDraft(d => ({ ...d, suggestionHandled: true }));
    setBusy(true); setError('');
    await applyLatest(selectGuard.current, () => qc.fetchQuery(titleQuery(media, id)), {
      onValue: detail => onDraft(d => d.sourceKey === key ? { ...d, config: titleConfig(detail), suggestionHandled: true } : d),
      onError: e => setError(apiMessage(e)),
      onSettled: () => setBusy(false),
    });
  }

  // Apply the automatic suggestion once — only if it is still relevant when it arrives.
  // Deferred (and cancelled by the cleanup) so a manual pick made in the meantime
  // — which flips `suggestionHandled` and re-runs this effect — always wins.
  useEffect(() => {
    if (!target || tmdbOff || locked || suggestionStarted.current) return;
    if (!shouldApplySuggestion({ suggestionKey: key, currentKey: key, draft })) return;
    const timer = setTimeout(() => {
      if (suggestionStarted.current) return;
      suggestionStarted.current = true;
      void selectTitle(target.media_type, target.tmdb_id);
    }, 0);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- selectTitle/draft are read at fire time; deps cover what matters
  }, [target?.media_type, target?.tmdb_id, tmdbOff, draft.suggestionHandled, draft.config, draft.skipped, locked]);

  function commitSearch() {
    const next = query.trim();
    if (!next) return;
    onDraft(d => ({ ...d, searchQuery: next }));
    if (next === search) void results.refetch(); else setSearch(next);
  }

  function setConfig(update: (c: WebConfig) => WebConfig) {
    setBusy(false);
    selectGuard.current.invalidate();
    translateGuard.current.invalidate();
    onDraft(d => d.config ? { ...d, config: update(d.config) } : d);
  }

  function field<K extends keyof WebConfig['metadata']>(name: K, value: WebConfig['metadata'][K]) {
    setConfig(c => ({ ...c, metadata: { ...c.metadata, [name]: value } }));
  }

  async function translate() {
    const source = config?.metadata.original_description;
    if (!config || !source?.trim() || translating) return;
    translateGuard.current.invalidate();
    const titleId = config.tmdb_title_id;
    setTranslating(true); setError('');
    // Dropped if the user edits, switches title, goes Back or closes meanwhile.
    await applyLatest(translateGuard.current, () => projectId ? webApi.translate(projectId, version, config) : translateImportDescription(source), {
      onValue: proposal => onDraft(d => d.config?.tmdb_title_id === titleId
        ? { ...d, config: { ...d.config, metadata: { ...d.config.metadata, description: proposal.description } } } : d),
      onError: e => setError(apiMessage(e)),
    });
    setTranslating(false);
  }

  async function refreshMetadata() {
    if (!config || busy) return;
    selectGuard.current.invalidate();
    translateGuard.current.invalidate();
    setBusy(true); setError('');
    const before = config;
    await applyLatest(selectGuard.current, () => webApi.details(before.media_type, before.tmdb_title_id, true), {
      onValue: fresh => onDraft(d => d.config?.tmdb_title_id === before.tmdb_title_id ? {
        ...d, config: { ...d.config, metadata: refreshDescription(before.metadata, fresh.metadata), poster_path: fresh.poster_path },
      } : d),
      onError: e => setError(apiMessage(e)),
    });
    setBusy(false);
  }

  const actions = importActions({ draft, expected, pending, busy: busy || translating });
  function submit(intent: ImportIntent) {
    const body = buildImportBody({ directory, provider, result, draft, expected, intent });
    if (body) onSubmit?.(intent, body);
  }

  const notice = suggestionNotice(suggestion.data, !!config && !!target && sameTitle(config, target));
  const titleCandidates = [...new Map((suggestion.data?.candidates ?? []).map(c => [`${c.media_type}:${c.tmdb_id}`, c])).values()];
  const showTitleChoices = !config && titleCandidates.length > 0
    && (suggestion.data?.status === 'ambiguous' || suggestion.data?.status === 'conflict');
  const seasonOptions = seasons.map(s => ({
    value: s.external_id,
    label: `${seasonLabel(s)}${candidatesForTitle.some(c => c.season_number === s.season_number) ? ' · AniBridge' : ''}`,
  }));
  const mapSummary = chosenCandidate ? episodeMapSummary(chosenCandidate) : null;
  const loadingEpisodes = !!config?.season && catalog.isFetching;

  return <Stack gap="sm" data-testid="tmdb-step">
    {!publishMode && <Group gap="xs" wrap="nowrap" align="center" style={{ minWidth: 0 }}>
      <Badge variant="light" size="sm" style={{ flexShrink: 0 }}>{provider === 'anidb' ? 'AniDB' : 'AniList'}</Badge>
      <Text size="sm" fw={600} truncate style={{ minWidth: 0 }}>{result.title}</Text>
      <Text size="xs" c="dimmed" truncate style={{ minWidth: 0, flexShrink: 1 }}>
        {[result.year, result.title_english && result.title_english !== result.title ? result.title_english : null, directory]
          .filter(Boolean).join(' · ')}
      </Text>
    </Group>}

    {tmdbOff && <Alert color="yellow" p="xs">TMDB is not configured on the server (<code>TMDB_API_TOKEN</code>). Return to Source to import without publishing.</Alert>}
    {notice && !draft.skipped && !tmdbOff && <Alert color={notice.color} variant="light" p="xs" py={6}>{notice.text}</Alert>}
    {error && <Alert color="red" p="xs" withCloseButton onClose={() => setError('')}>{error}</Alert>}

    {draft.skipped ? <Alert color="gray" p="sm" title="Importing without a TMDB mapping">
      <Group justify="space-between" wrap="nowrap" gap="sm">
        <Text size="sm">The project is imported as before. Publish &amp; Import is unavailable without a mapping.</Text>
        {!tmdbOff && <Button size="compact-sm" variant="default" onClick={() => onDraft(d => ({ ...d, skipped: false }))}>Add TMDB mapping</Button>}
      </Group>
    </Alert> : <>
      <Group align="flex-start" gap="md" wrap="wrap" style={{ alignItems: 'flex-start' }}>
        {/* Left: search + candidates */}
        <Stack gap="xs" style={{ flex: '1 1 330px', minWidth: 0 }}>
          {!searchOff && <Group gap="xs" align="flex-end" wrap="nowrap">
            <TextInput label="Search TMDB" disabled={locked} style={{ flex: 1 }} value={query} data-autofocus
              onChange={e => setQuery(e.currentTarget.value)} onKeyDown={e => { if (e.key === 'Enter') commitSearch(); }} />
            <Button variant="default" leftSection={<MagnifyingGlass size={14} />} disabled={locked || !query.trim()}
              loading={results.isFetching} onClick={commitSearch}>Search</Button>
          </Group>}
          {showTitleChoices && <Stack gap={4}>
            <Text size="xs" c="dimmed">AniBridge candidates</Text>
            <Group gap={6}>{titleCandidates.map(c => <CandidateButton key={`${c.media_type}:${c.tmdb_id}`}
              media={c.media_type} id={c.tmdb_id} label={candidateLabel(c)} disabled={busy || locked}
              onPick={() => void selectTitle(c.media_type, c.tmdb_id)} />)}</Group>
          </Stack>}
          {results.error && !searchOff && <Alert color="red" p="xs">{apiMessage(results.error)}</Alert>}
          {!searchOff && <ScrollArea.Autosize mah={config ? 230 : 300} scrollbars="y" offsetScrollbars={false}>
            <Stack gap="xs">{results.data?.map(r => <TmdbSearchCard key={`${r.media_type}:${r.id}`} result={r} busy={busy || locked}
              selected={config?.media_type === r.media_type && config.tmdb_title_id === r.id}
              onSelect={() => void selectTitle(r.media_type, r.id)} />)}</Stack>
            {!results.data?.length && !results.isFetching && !results.error
              && <Text size="sm" c="dimmed">{search ? 'No matching TMDB titles found.' : 'Search TMDB to choose a title.'}</Text>}
          </ScrollArea.Autosize>}

        </Stack>

        {/* Right: the chosen title */}
        <Box style={{ flex: '2 1 440px', minWidth: 0, borderLeft: '1px solid var(--mantine-color-default-border)', paddingLeft: 16 }}>
          <Stack gap="sm">
          {config ? <Group align="flex-start" wrap="nowrap" gap="md">
            <Stack gap={6} w={104} style={{ flexShrink: 0 }}>
              {config.poster_path
                ? <Image src={posterUrl(config.poster_path)} alt="TMDB poster" radius="sm" />
                : <Box h={150} style={{ border: '1px dashed var(--mantine-color-dark-4)', borderRadius: 4 }} />}
              <Badge variant="light" size="sm" fullWidth>{config.media_type === 'tv' ? 'TV' : 'Movie'}{config.metadata.year ? ` · ${config.metadata.year}` : ''}</Badge>
              <Text size="xs" c="dimmed" ta="center">TMDB {config.tmdb_title_id}</Text>
              <Button size="compact-xs" variant="subtle" h="auto" py={4} styles={{ label: { whiteSpace: 'normal', lineHeight: 1.3 } }} loading={busy} onClick={() => void refreshMetadata()}>Refresh metadata</Button>
              {locked && <Text size="xs" c="dimmed">Identity locked</Text>}
            </Stack>
            <TitleMetadataFields compact config={config} names={detail.data?.romaji_titles ?? []} onField={field}
              onTranslate={() => void translate()} translating={translating} busy={busy}
              errors={{ title: errors.title, language: errors.language }} />
          </Group> : <Box p="md" style={{ border: '1px dashed var(--mantine-color-dark-4)', borderRadius: 4 }}>
            <Text size="sm" c="dimmed">{busy ? 'Loading TMDB title…' : 'Select a TMDB title to review its metadata and episode placement.'}</Text>
          </Box>}
      {config && <Stack gap={4}>
        <Group gap="sm" align="flex-start" wrap="wrap">
          {tv ? <>
            <Select disabled={locked} label="Episode order" w={FIELD_WIDTH.order} allowDeselect={false} value={config.order_kind}
              data={[{ value: 'standard', label: 'Standard' }, { value: 'episode_group', label: 'Alternative group' }]}
              onChange={v => v && setConfig(c => withOrder(c, v as WebConfig['order_kind']))} />
            {config.order_kind === 'episode_group' && <Select disabled={locked} label="Episode group" w={FIELD_WIDTH.group} searchable
              placeholder={base.isFetching ? 'Loading…' : groups.length ? 'Choose a group' : 'No groups on TMDB'} nothingFoundMessage="No groups"
              comboboxProps={{ width: 'max-content', position: 'bottom-start' }} error={errors.group}
              value={config.group_id} data={groups.map(g => ({ value: g.id, label: groupLabel(g) }))}
              onChange={v => setConfig(c => withGroup(c, v))} />}
            <Select disabled={locked} label="Structure" w={FIELD_WIDTH.structure} allowDeselect={false} value={config.episode_structure}
              data={[{ value: 'SEASONED', label: 'Seasoned' }, { value: 'FLAT', label: 'Flat' }]}
              onChange={v => v && setConfig(c => withStructure(c, v as WebConfig['episode_structure']))} />
            {config.episode_structure === 'SEASONED' && <Select disabled={locked} label="Season / subgroup" w={FIELD_WIDTH.season} searchable
              placeholder={catalog.isFetching && !seasons.length ? 'Loading…' : 'Choose a season'} nothingFoundMessage="No seasons"
              comboboxProps={{ width: 'max-content', position: 'bottom-start' }} error={errors.season}
              value={config.season?.external_id ?? null} data={seasonOptions}
              onChange={v => setConfig(c => withSeason(c, seasons.find(s => s.external_id === v) ?? null))} />}
          </> : <Stack gap={4} justify="flex-end" h={56}><Badge variant="light" color="gray" size="lg">Movie · Flat · Standard order</Badge></Stack>}
          <NumberInput label="Expected episodes" w={FIELD_WIDTH.expected} min={1} max={MAX_EXPECTED_EPISODES} allowDecimal={false}
            allowNegative={false} clampBehavior="none" value={expected ?? ''} error={errors.expected}
            onChange={v => onDraft(d => ({ ...d, expected: parseExpectedInput(v), expectedDirty: true }))} />
        </Group>
        <Group gap={6} wrap="nowrap" style={{ minHeight: 18 }}>
          {loadingEpisodes && <Loader size={12} />}
          <Text size="xs" c="dimmed">
            {[
              config.episode_structure === 'FLAT' && tv ? 'Flat episode placement.' : null,
              config.season && !loadingEpisodes && !catalog.error
                ? `${episodes.length} TMDB episode${episodes.length === 1 ? '' : 's'} loaded${episodes.length ? ` (E${episodes[0].episode_number}–E${episodes[episodes.length - 1].episode_number})` : ''}.` : null,
              mapSummary ? `AniBridge: ${mapSummary}.` : null,
              draft.expectedDirty ? null : expectedBasisText(expectedSuggestion),
            ].filter(Boolean).join(' ')}
          </Text>
          {catalog.error && <Text size="xs" c="yellow">Could not load TMDB episodes ({apiMessage(catalog.error)}). They are re-checked when importing.
            {' '}<Button component="span" size="compact-xs" variant="subtle" onClick={() => void catalog.refetch()}>Retry</Button></Text>}
        </Group>
      </Stack>}
          </Stack>
        </Box>
      </Group>


    </>}

    {onSubmit && <Group justify="space-between" mt="xs" pt="sm" style={{ borderTop: '1px solid var(--mantine-color-dark-4)' }}>
      <Button variant="subtle" leftSection={<ArrowLeft size={14} />} disabled={pending} onClick={onBack}>Back</Button>
      <Group gap="sm">
        <Tooltip label={actions.publishAndImport.reason} disabled={actions.publishAndImport.enabled} withinPortal>
          <span><Button color="pink" disabled={!actions.publishAndImport.enabled} loading={pendingIntent === 'publish_and_import'}
            onClick={() => submit('publish_and_import')}>Save &amp; Publish</Button></span>
        </Tooltip>
      </Group>
    </Group>}
  </Stack>;
}
