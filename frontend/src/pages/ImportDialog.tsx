import { useCallback, useRef, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Card,
  Center,
  Group,
  Loader,
  Modal,
  ScrollArea,
  SegmentedControl,
  SimpleGrid,
  Stack,
  Stepper,
  Text,
  TextInput,
  Tooltip,
  UnstyledButton,
} from '@mantine/core';
import { ArrowRight, MagnifyingGlass } from '@phosphor-icons/react';
import { useQueryClient } from '@tanstack/react-query';
import { notifications } from '@mantine/notifications';
import type { SearchResult } from '../types';
import { animeCardRows } from '../utils/animeResultTitles';
import { apiErrorDetail } from '../utils/projectArchive';
import {
  IMPORT_MODAL_WIDTH,
  canProceedToMapping,
  defaultSearchQuery,
  draftForSource,
  importOutcomeMessage,
  sourceKey,
  newImportRequestId,
  buildImportBody,
  emptyDraft,
  type ImportBody,
  type ImportIntent,
  type ImportStep,
  type MappingDraft,
  type SourceProvider,
} from '../utils/tmdbImport';
import {
  useImportDirectories,
  useImportProject,
  useMetadataSearch,
  type ImportResult,
} from '../hooks/useImport';
import client from '../api/client';
import { useWebState } from '../hooks/useWebPublishing';
import { PublicationProgress } from '../components/Project/PublicationProgress';
import { TmdbMappingStep } from '../components/Import/TmdbMappingStep';

function normalizeDirName(name: string): string {
  return name.replace(/[-_.]/g, ' ').replace(/\s+/g, ' ').trim();
}

// Same three rows for every provider: romaji, English (or Japanese), year.
// Titles clamp at two lines; the tooltip carries the full text.
function ResultCardBody({ result }: { result: SearchResult }) {
  const rows = animeCardRows(result);
  return (
    <Box style={{ minWidth: 0 }}>
      <Tooltip label={rows.primary} multiline maw={420} openDelay={300} withinPortal>
        <Text size="sm" fw={500} lineClamp={2}>
          {rows.primary}
        </Text>
      </Tooltip>
      {rows.secondary && (
        <Tooltip label={rows.secondary} multiline maw={420} openDelay={300} withinPortal>
          <Text size="xs" c="dimmed" lineClamp={2}>
            {rows.secondary}
          </Text>
        </Tooltip>
      )}
      {rows.year != null && (
        <Text size="xs" c="dimmed">
          {rows.year}
        </Text>
      )}
    </Box>
  );
}

// Step 1 uses the wide modal: the directory list keeps a fixed column and the
// search side takes the rest, with results flowing into two columns when the
// results area itself is wide enough (container query, not viewport).
const SOURCE_PANE_HEIGHT = 400;
const DIRECTORY_COLUMN_WIDTH = 320;
const RESULT_GRID_COLUMNS = { base: 1, '520px': 2 } as const;

interface ImportDialogProps {
  opened: boolean;
  onClose: () => void;
}

export function ImportDialog({ opened, onClose }: ImportDialogProps) {
  const queryClient = useQueryClient();

  const [step, setStep] = useState<ImportStep>('source');
  const [selectedDir, setSelectedDir] = useState<string | null>(null);
  const [provider, setProvider] = useState<SourceProvider>('anidb');
  const [searchQuery, setSearchQuery] = useState('');
  const [committedQuery, setCommittedQuery] = useState('');
  const [selectedResult, setSelectedResult] = useState<SearchResult | null>(null);
  // Step 2 form state lives here so Back → Next keeps everything the user entered.
  const [draft, setDraft] = useState<MappingDraft | null>(null);
  const [imported, setImported] = useState<ImportResult | null>(null);
  const [publicationError, setPublicationError] = useState('');
  const [startingPublication, setStartingPublication] = useState(false);
  const submitGuard = useRef(false);
  const request = useRef<{ key: string; id: string } | null>(null);
  const publication = useWebState(imported?.id ?? 0, step === 'progress' && !!imported);
  const [pendingIntent, setPendingIntent] = useState<ImportIntent | null>(null);

  const { data: directories = [], isLoading: dirsLoading } = useImportDirectories(opened);
  const { data: searchResults = [], isLoading: searching } = useMetadataSearch(
    provider,
    committedQuery,
  );
  const importMutation = useImportProject();
  const updateDraft = useCallback(
    (update: (d: MappingDraft) => MappingDraft) => setDraft((d) => (d ? update(d) : d)),
    [],
  );

  function handleSearch() {
    const q = searchQuery.trim();
    if (q) {
      setCommittedQuery(q);
      setSelectedResult(null);
    }
  }

  function handleSelectDir(name: string) {
    if (name === selectedDir) return;
    // Selecting a directory searches for it once (and drops the previous pick).
    const q = normalizeDirName(name);
    setSelectedDir(name);
    setSearchQuery(q);
    setCommittedQuery(q);
    setSelectedResult(null);
  }

  function resetAndClose() {
    setStep('source');
    setImported(null); setPublicationError(''); request.current = null;
    setSelectedDir(null);
    setSearchQuery('');
    setCommittedQuery('');
    setSelectedResult(null);
    setDraft(null);
    onClose();
  }

  // Not while an import request is in flight (its result decides what happens next).
  function handleClose() {
    if (!importMutation.isPending && !startingPublication && publication.data?.state !== 'publishing') resetAndClose();
  }

  function handleNext() {
    if (!selectedDir || !selectedResult || !canProceedToMapping({ directory: selectedDir, result: selectedResult })) return;
    // Same source as before → keep the draft; a different one starts fresh.
    const key = sourceKey(provider, selectedResult.provider_id);
    setDraft((d) => draftForSource(d, key, defaultSearchQuery(selectedResult)));
    setStep('mapping');
  }

  async function startPublication(projectId: number) {
    setStartingPublication(true); setPublicationError('');
    try {
      // Import is already committed. Publication retries operate only on this ID.
      const state = (await client.get<import('../hooks/useWebPublishing').WebState>(`/projects/${projectId}/web`)).data;
      if (state.state !== 'publishing') await client.post(`/projects/${projectId}/web/publish`, {
        version: state.version, file_ids: [], release_label: '', changelog: null,
      });
      await queryClient.invalidateQueries({ queryKey: ['projects', projectId, 'web'] });
    } catch (e) {
      // A lost response may follow a successful claim. Observe the job before offering retry.
      await queryClient.invalidateQueries({ queryKey: ['projects', projectId, 'web'] });
      setPublicationError(apiErrorDetail(e).message ?? 'Publication could not start. The imported project is safe.');
    } finally { setStartingPublication(false); }
  }

  async function handleImport(intent: ImportIntent, body: ImportBody) {
    if (submitGuard.current || imported) return;
    submitGuard.current = true; setPendingIntent(intent);
    const key = `${body.directory_name}:${body.provider}:${body.provider_id}:${intent}`;
    if (request.current?.key !== key) request.current = { key, id: newImportRequestId() };
    try {
      const created = await importMutation.mutateAsync({ ...body, request_id: request.current.id });
      await queryClient.invalidateQueries({ queryKey: ['projects'] });
      await queryClient.invalidateQueries({ queryKey: ['import', 'directories'] });
      if (intent === 'publish_and_import') {
        setImported(created); setStep('progress');
        await startPublication(created.id);
      } else {
        const outcome = importOutcomeMessage(intent, created.name);
        notifications.show({ color: 'teal', title: outcome.title, message: outcome.message });
        resetAndClose();
      }
    } catch (e) {
      notifications.show({ color: 'red', title: 'Import failed',
        message: apiErrorDetail(e).message ?? 'Could not create the project. Retry safely with the same request.' });
    } finally { submitGuard.current = false; setPendingIntent(null); }
  }

  function importSource() {
    if (!selectedDir || !selectedResult) return;
    const body = buildImportBody({ directory: selectedDir, provider, result: selectedResult,
      draft: emptyDraft(sourceKey(provider, selectedResult.provider_id), ''), expected: null, intent: 'import_only' });
    if (body) void handleImport('import_only', body);
  }

  const canProceed = canProceedToMapping({ directory: selectedDir, result: selectedResult });
  const pending = importMutation.isPending || startingPublication || publication.data?.state === 'publishing';
  const sourceActive = step === 'source';

  return (
    <Modal
      opened={opened}
      onClose={handleClose}
      title="Import Project"
      size={IMPORT_MODAL_WIDTH}
      closeOnClickOutside={!pending}
      closeOnEscape={!pending}
      styles={{ body: { padding: 0 } }}
    >
      <Box px="md" pt={4} pb="sm" style={{ borderBottom: '1px solid var(--mantine-color-dark-4)' }}>
        <Stepper
          maw={600}
          size="xs"
          iconSize={22}
          active={sourceActive ? 0 : step === 'mapping' ? 1 : 2}
          allowNextStepsSelect={false}
          onStepClick={(i) => { if (i === 0 && !pending && step !== 'progress') setStep('source'); }}
        >
          <Stepper.Step label="Source" description="AniDB / AniList" />
          <Stepper.Step label="TMDB mapping" description="Title, order, season" />
          <Stepper.Step label="Publish" description="Metadata progress" />
        </Stepper>
      </Box>

      {sourceActive ? (
        <>
          <Group align="stretch" gap={0} wrap="nowrap" style={{ minHeight: 460 }}>
            {/* Left: unimported directories */}
            <Box
              style={{
                width: DIRECTORY_COLUMN_WIDTH,
                maxWidth: '38%',
                borderRight: '1px solid var(--mantine-color-dark-4)',
                flexShrink: 0,
              }}
            >
              <Box p="md">
                <Text size="xs" fw={600} mb="sm" c="dimmed" tt="uppercase" style={{ letterSpacing: '0.05em' }}>
                  Unimported Directories
                </Text>
                {dirsLoading ? (
                  <Center py="xl">
                    <Loader size="sm" />
                  </Center>
                ) : directories.length === 0 ? (
                  <Text size="sm" c="dimmed">
                    No new directories found in import root.
                  </Text>
                ) : (
                  <ScrollArea h={SOURCE_PANE_HEIGHT + 40} offsetScrollbars>
                    <Stack gap="xs">
                      {directories.map((dir) => (
                        <UnstyledButton
                          key={dir.name}
                          w="100%"
                          onClick={() => handleSelectDir(dir.name)}
                        >
                          <Card
                            p="xs"
                            radius="sm"
                            withBorder
                            style={{
                              borderColor:
                                selectedDir === dir.name
                                  ? 'var(--mantine-color-pink-6)'
                                  : undefined,
                              backgroundColor:
                                selectedDir === dir.name
                                  ? 'var(--mantine-color-dark-5)'
                                  : undefined,
                              cursor: 'pointer',
                            }}
                          >
                            <Text size="sm" fw={500} truncate>
                              {dir.name}
                            </Text>
                            <Text size="xs" c="dimmed">
                              {dir.file_count} {dir.file_count === 1 ? 'file' : 'files'}
                            </Text>
                          </Card>
                        </UnstyledButton>
                      ))}
                    </Stack>
                  </ScrollArea>
                )}
              </Box>
            </Box>

            {/* Right: anime search */}
            <Box style={{ flex: 1, minWidth: 0 }}>
              <Box p="md">
                <Text size="xs" fw={600} mb="sm" c="dimmed" tt="uppercase" style={{ letterSpacing: '0.05em' }}>
                  Anime Match
                </Text>
                <Stack gap="sm">
                  <Group gap="sm" align="flex-end" wrap="nowrap">
                    <SegmentedControl
                      size="sm"
                      value={provider}
                      onChange={(v) => {
                        setProvider(v as SourceProvider);
                        setSelectedResult(null);
                      }}
                      data={[
                        { label: 'AniDB', value: 'anidb' },
                        { label: 'AniList', value: 'anilist' },
                      ]}
                    />
                    <TextInput
                      style={{ flex: 1 }}
                      placeholder="Search anime…"
                      value={searchQuery}
                      onChange={(e) => setSearchQuery(e.currentTarget.value)}
                      onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
                    />
                    <Button
                      variant="default"
                      leftSection={<MagnifyingGlass size={14} />}
                      onClick={handleSearch}
                      loading={searching}
                    >
                      Search
                    </Button>
                  </Group>

                  {committedQuery && (
                    <ScrollArea h={SOURCE_PANE_HEIGHT} offsetScrollbars>
                      {searching ? (
                        <Center py="md">
                          <Loader size="sm" />
                        </Center>
                      ) : searchResults.length === 0 ? (
                        <Text size="sm" c="dimmed">
                          No results found.
                        </Text>
                      ) : (
                        <SimpleGrid type="container" cols={RESULT_GRID_COLUMNS} spacing="xs" verticalSpacing="xs">
                          {searchResults.map((result) => (
                            <UnstyledButton
                              key={result.provider_id}
                              w="100%"
                              onClick={() => setSelectedResult(result)}
                            >
                              <Card
                                p="xs"
                                radius="sm"
                                withBorder
                                h="100%"
                                style={{
                                  borderColor:
                                    selectedResult?.provider_id === result.provider_id
                                      ? 'var(--mantine-color-pink-6)'
                                      : undefined,
                                  backgroundColor:
                                    selectedResult?.provider_id === result.provider_id
                                      ? 'var(--mantine-color-dark-5)'
                                      : undefined,
                                  cursor: 'pointer',
                                }}
                              >
                                <ResultCardBody result={result} />
                              </Card>
                            </UnstyledButton>
                          ))}
                        </SimpleGrid>
                      )}
                    </ScrollArea>
                  )}
                </Stack>
              </Box>
            </Box>
          </Group>

          {/* Footer */}
          <Box p="md" style={{ borderTop: '1px solid var(--mantine-color-dark-4)' }}>
            <Group justify="flex-end" gap="sm">
              <Button variant="default" onClick={handleClose}>
                Cancel
              </Button>
              <Button variant="default" disabled={!canProceed || pending} loading={pendingIntent === 'import_only'} onClick={importSource}>Import</Button>
              <Button
                color="pink"
                disabled={!canProceed || pending}
                rightSection={<ArrowRight size={14} />}
                onClick={handleNext}
              >
                Publish settings
              </Button>
            </Group>
          </Box>
        </>
      ) : step === 'progress' && imported ? (
        <Stack p="md" gap="sm">
          <Alert color="teal" p="xs">Project imported: {imported.name}. Discover has started.</Alert>
          {publicationError && publication.data?.state !== 'publishing' && <Alert color="red" p="xs">Publication: {publicationError}</Alert>}
          {publication.error && <Alert color="red" p="xs">{apiErrorDetail(publication.error).message ?? 'Could not load publication status.'}</Alert>}
          {publication.data ? <PublicationProgress state={publication.data} /> : <Text size="sm">{startingPublication ? 'Starting metadata publication...' : 'Loading publication status...'}</Text>}
          <Group justify="flex-end" pt="sm" style={{ borderTop: '1px solid var(--mantine-color-default-border)' }}>
            {(publicationError || ['failed', 'partial'].includes(publication.data?.state ?? '')) && <Button variant="default" disabled={pending} onClick={() => void startPublication(imported.id)}>Retry publication</Button>}
            <Button disabled={pending} onClick={handleClose}>Close</Button>
          </Group>
        </Stack>
      ) : (
        selectedDir && selectedResult && draft && (
          <Box p="md">
            <TmdbMappingStep
              key={sourceKey(provider, selectedResult.provider_id)}
              provider={provider}
              result={selectedResult}
              directory={selectedDir}
              draft={draft}
              onDraft={updateDraft}
              onBack={() => setStep('source')}
              onSubmit={(intent, body) => void handleImport(intent, body)}
              pending={pending}
              pendingIntent={pendingIntent}
            />
          </Box>
        )
      )}
    </Modal>
  );
}
