import { useState } from 'react';
import {
  ActionIcon,
  Badge,
  Button,
  Center,
  Group,
  Loader,
  Modal,
  ScrollArea,
  Select,
  Stack,
  Tabs,
  Text,
  Textarea,
  TextInput,
  Tooltip,
} from '@mantine/core';
import { notifications } from '@mantine/notifications';
import { BookOpen, Plus, Trash } from '@phosphor-icons/react';
import {
  GLOSSARY_CATEGORIES,
  useCreateGlossaryTerm,
  useDeleteGlossaryTerm,
  useDeleteTmEntry,
  useGlossaryTerms,
  useStyleGuide,
  useTranslationMemory,
  useUpdateAddressPair,
  useUpdateGlossaryTerm,
  useUpdateStyleBible,
  useUpdateTmEntry,
} from '../hooks/useStyleGuide';
import type { GlossaryTerm, TmEntry } from '../hooks/useStyleGuide';
import { CharactersTab } from './CharactersTab';
import './styleGuide.css';

interface StyleGuideDialogProps {
  projectId: number;
  opened: boolean;
  onClose: () => void;
  initialTab?: 'glossary' | 'characters' | 'bible' | 'tm';
}

const MODE_OPTIONS = [
  { value: 'tykani', label: 'tykání' },
  { value: 'vykani', label: 'vykání' },
  { value: 'mixed', label: 'mixed' },
];

const CATEGORY_OPTIONS = GLOSSARY_CATEGORIES.map((c) => ({ value: c, label: c }));

function BibleTab({ projectId }: { projectId: number }) {
  const { data, isLoading } = useStyleGuide(projectId, true);
  const updateBible = useUpdateStyleBible(projectId);
  const updatePair = useUpdateAddressPair(projectId);

  if (isLoading || !data) return <Center h={160}><Loader size="sm" /></Center>;

  const saveField = (field: 'tone_summary' | 'register_notes' | 'honorific_policy') =>
    (e: React.FocusEvent<HTMLTextAreaElement>) => {
      const value = e.currentTarget.value;
      if ((data[field] ?? '') === value) return;
      updateBible.mutate({ [field]: value });
    };

  return (
    <ScrollArea.Autosize mah="60vh" offsetScrollbars scrollbars="y" mt="sm">
      <Stack gap="md">
        {data.version === null && (
          <Text size="xs" c="dimmed">
            No style bible yet — it is generated automatically before the first file translates.
            Fields saved here are kept and never overwritten by the generator.
          </Text>
        )}
        <Textarea
          label="Tone"
          description="Overall tone of the series and how the translation should read."
          defaultValue={data.tone_summary ?? ''}
          onBlur={saveField('tone_summary')}
          autosize minRows={2} maxRows={8}
        />
        <Textarea
          label="Register"
          description="Formality/slang/profanity rules for this series."
          defaultValue={data.register_notes ?? ''}
          onBlur={saveField('register_notes')}
          autosize minRows={2} maxRows={8}
        />
        <Textarea
          label="Honorifics"
          description="How Japanese honorifics are handled."
          defaultValue={data.honorific_policy ?? ''}
          onBlur={saveField('honorific_policy')}
          autosize minRows={2} maxRows={6}
        />

        {data.address_pairs.length > 0 && (
          <Stack gap="xs">
            <Text size="sm" fw={700}>Address pairs (T–V)</Text>
            <div className="sgd-zebra">
              {data.address_pairs.map((pair) => (
                <div key={pair.id} className="sgd-row sgd-pair">
                  <Text size="sm" className="sgd-wrap" title={`${pair.speaker_name} → ${pair.addressee_name}`}>
                    {pair.speaker_name} → {pair.addressee_name}
                  </Text>
                  <div>
                    {pair.origin === 'manual' && <Badge size="xs" variant="light">manual</Badge>}
                  </div>
                  <Select
                    size="xs"
                    data={MODE_OPTIONS}
                    value={pair.mode}
                    onChange={(mode) => {
                      if (mode && mode !== pair.mode) {
                        updatePair.mutate({ pairId: pair.id, mode: mode as typeof pair.mode });
                      }
                    }}
                  />
                </div>
              ))}
            </div>
          </Stack>
        )}
      </Stack>
    </ScrollArea.Autosize>
  );
}

function GlossaryRow({
  term,
  projectId,
}: {
  term: GlossaryTerm;
  projectId: number;
}) {
  const updateTerm = useUpdateGlossaryTerm(projectId);
  const deleteTerm = useDeleteGlossaryTerm(projectId);

  const blurUpdate = (field: 'target_term' | 'vocative' | 'note') =>
    (e: React.FocusEvent<HTMLInputElement>) => {
      const value = e.currentTarget.value;
      if ((term[field] ?? '') === value) return;
      updateTerm.mutate({ termId: term.id, [field]: value });
    };

  return (
    <div className="sgd-row sgd-glossary" style={{ opacity: term.is_active ? 1 : 0.45 }}>
      <Text size="sm" className="sgd-wrap sgd-cell sgd-full" data-label="Source" title={term.source_term}>
        {term.source_term}
      </Text>
      <div className="sgd-cell" data-label="Translation">
        <TextInput size="xs" defaultValue={term.target_term} onBlur={blurUpdate('target_term')} />
      </div>
      <div className="sgd-cell" data-label="Category">
        <Select
          size="xs"
          data={CATEGORY_OPTIONS}
          value={term.category}
          onChange={(category) => {
            if (category && category !== term.category) {
              updateTerm.mutate({ termId: term.id, category });
            }
          }}
        />
      </div>
      <div className="sgd-cell" data-label="Vocative">
        <TextInput size="xs" defaultValue={term.vocative ?? ''} placeholder="—" onBlur={blurUpdate('vocative')} />
      </div>
      <div className="sgd-cell" data-label="Note">
        <TextInput size="xs" defaultValue={term.note ?? ''} placeholder="—" onBlur={blurUpdate('note')} />
      </div>
      <div className="sgd-cell" data-label="Origin">
        <Badge size="xs" variant="light" color={term.origin === 'manual' ? 'blue' : term.origin === 'metadata' ? 'grape' : 'gray'}>
          {term.origin}
        </Badge>
      </div>
      <div>
        <Tooltip label="Delete term" withArrow>
          <ActionIcon
            size="sm" variant="subtle" color="red"
            loading={deleteTerm.isPending}
            onClick={() => deleteTerm.mutate(term.id)}
          >
            <Trash size={14} />
          </ActionIcon>
        </Tooltip>
      </div>
    </div>
  );
}

function GlossaryTab({ projectId }: { projectId: number }) {
  const { data: terms, isLoading } = useGlossaryTerms(projectId, true);
  const createTerm = useCreateGlossaryTerm(projectId);
  const [source, setSource] = useState('');
  const [target, setTarget] = useState('');
  const [category, setCategory] = useState<string>('other');
  const [vocative, setVocative] = useState('');
  const [note, setNote] = useState('');

  async function handleAdd() {
    if (!source.trim() || !target.trim()) return;
    try {
      await createTerm.mutateAsync({
        source_term: source.trim(),
        target_term: target.trim(),
        category,
        vocative: vocative.trim() || null,
        note: note.trim() || null,
      });
      setSource('');
      setTarget('');
      setVocative('');
      setNote('');
    } catch {
      notifications.show({ color: 'red', message: 'Term already exists in this category.' });
    }
  }

  if (isLoading || !terms) return <Center h={160}><Loader size="sm" /></Center>;

  return (
    <Stack gap="sm" pt="sm">
      <div className="sgd-row sgd-glossary sgd-add">
        <TextInput
          size="xs" label="English term" value={source} className="sgd-full"
          onChange={(e) => setSource(e.currentTarget.value)}
        />
        <TextInput
          size="xs" label="Translation" value={target}
          onChange={(e) => setTarget(e.currentTarget.value)}
        />
        <Select size="xs" label="Category" data={CATEGORY_OPTIONS} value={category}
          onChange={(v) => v && setCategory(v)} />
        <TextInput
          size="xs" label="Vocative" value={vocative}
          onChange={(e) => setVocative(e.currentTarget.value)}
        />
        <TextInput
          size="xs" label="Note" value={note}
          onChange={(e) => setNote(e.currentTarget.value)}
        />
        <Button
          size="xs" leftSection={<Plus size={14} />} className="sgd-add-button"
          loading={createTerm.isPending}
          disabled={!source.trim() || !target.trim()}
          onClick={() => void handleAdd()}
        >
          Add
        </Button>
      </div>

      {terms.length === 0 ? (
        <Text size="xs" c="dimmed">
          No glossary terms yet — the style bible generator and script analysis fill this in automatically.
        </Text>
      ) : (
        <>
          <div className="sgd-head sgd-glossary">
            <span>Source</span>
            <span>Translation</span>
            <span>Category</span>
            <span>Vocative</span>
            <span>Note</span>
            <span>Origin</span>
            <span />
          </div>
          <ScrollArea.Autosize mah={420} scrollbars="y">
            <div className="sgd-zebra">
              {terms.map((term) => (
                <GlossaryRow key={term.id} term={term} projectId={projectId} />
              ))}
            </div>
          </ScrollArea.Autosize>
        </>
      )}
    </Stack>
  );
}

function TmRow({ entry, projectId }: { entry: TmEntry; projectId: number }) {
  const updateEntry = useUpdateTmEntry(projectId);
  const deleteEntry = useDeleteTmEntry(projectId);

  return (
    <div className="sgd-row sgd-tm">
      <Text size="sm" className="sgd-wrap sgd-cell sgd-full" data-label="Source">{entry.source_text}</Text>
      <div className="sgd-cell sgd-full" data-label="Translation">
        <TextInput
          size="xs"
          defaultValue={entry.target_text}
          onBlur={(e) => {
            const value = e.currentTarget.value.trim();
            if (value && value !== entry.target_text) {
              updateEntry.mutate({ entryId: entry.id, targetText: value });
            }
          }}
        />
      </div>
      <div className="sgd-cell" data-label="Origin">
        <Badge size="xs" variant="light" color={entry.origin === 'human' ? 'blue' : 'gray'}>
          {entry.origin}
        </Badge>
      </div>
      <Text size="xs" c="dimmed" className="sgd-cell" data-label="Uses">{entry.use_count}</Text>
      <div>
        <Tooltip label="Delete entry" withArrow>
          <ActionIcon
            size="sm" variant="subtle" color="red"
            loading={deleteEntry.isPending}
            onClick={() => deleteEntry.mutate(entry.id)}
          >
            <Trash size={14} />
          </ActionIcon>
        </Tooltip>
      </div>
    </div>
  );
}

function TranslationMemoryTab({ projectId }: { projectId: number }) {
  const [search, setSearch] = useState('');
  const {
    data, isLoading, fetchNextPage, hasNextPage, isFetchingNextPage,
  } = useTranslationMemory(projectId, true, search);

  const entries = data?.pages.flatMap((p) => p.items) ?? [];
  const total = data?.pages[0]?.total ?? 0;

  return (
    <Stack gap="sm" pt="sm">
      <Text size="xs" c="dimmed">
        Accepted translations, reused across episodes: exact matches of human-origin entries are
        applied automatically; near matches appear as hints in translation prompts. Editing an
        entry promotes it to human origin.
      </Text>
      <TextInput
        size="xs"
        placeholder="Search source or translation…"
        value={search}
        onChange={(e) => setSearch(e.currentTarget.value)}
      />
      {isLoading || !data ? (
        <Center h={160}><Loader size="sm" /></Center>
      ) : entries.length === 0 ? (
        <Text size="xs" c="dimmed">
          {search ? 'No matching entries.' : 'Empty — the memory fills as files are accepted.'}
        </Text>
      ) : (
        <>
          <div className="sgd-head sgd-tm">
            <span>Source</span>
            <span>Translation</span>
            <span>Origin</span>
            <span>Uses</span>
            <span />
          </div>
          <ScrollArea.Autosize mah={420} scrollbars="y">
            <div className="sgd-zebra">
              {entries.map((entry) => (
                <TmRow key={entry.id} entry={entry} projectId={projectId} />
              ))}
            </div>
          </ScrollArea.Autosize>
          <Group justify="space-between">
            <Text size="xs" c="dimmed">{entries.length} of {total} entries</Text>
            {hasNextPage && (
              <Button
                size="compact-xs"
                variant="light"
                loading={isFetchingNextPage}
                onClick={() => fetchNextPage()}
              >
                Load more
              </Button>
            )}
          </Group>
        </>
      )}
    </Stack>
  );
}

export function StyleGuideDialog({ projectId, opened, onClose, initialTab = 'glossary' }: StyleGuideDialogProps) {
  return (
    <Modal
      opened={opened}
      onClose={onClose}
      title={<Group gap="xs"><BookOpen size={18} /><Text fw={700}>Style guide & glossary</Text></Group>}
      size="72rem"
    >
      <Tabs defaultValue={initialTab} key={initialTab}>
        <Tabs.List>
          <Tabs.Tab value="glossary">Glossary</Tabs.Tab>
          <Tabs.Tab value="characters">Characters</Tabs.Tab>
          <Tabs.Tab value="bible">Style bible</Tabs.Tab>
          <Tabs.Tab value="tm">Translation memory</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="glossary">
          <GlossaryTab projectId={projectId} />
        </Tabs.Panel>
        <Tabs.Panel value="characters">
          <CharactersTab projectId={projectId} />
        </Tabs.Panel>
        <Tabs.Panel value="bible">
          <BibleTab projectId={projectId} />
        </Tabs.Panel>
        <Tabs.Panel value="tm">
          <TranslationMemoryTab projectId={projectId} />
        </Tabs.Panel>
      </Tabs>
    </Modal>
  );
}
