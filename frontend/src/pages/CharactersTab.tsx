import { useMemo, useState } from 'react';
import {
  Badge,
  Button,
  Center,
  Collapse,
  Group,
  Loader,
  ScrollArea,
  Select,
  Stack,
  Text,
  TextInput,
  Tooltip,
} from '@mantine/core';
import { notifications } from '@mantine/notifications';
import { ArrowsClockwise, CaretDown, CaretRight, Plus } from '@phosphor-icons/react';
import type { ProjectCharacterWithSpeakers, ProjectSpeaker, SpeakerContentTag } from '../types';
import {
  useCreateCharacter,
  useProjectCharacters,
  useProjectSpeakers,
  useRetranslateAffected,
  useUpdateCharacter,
  useUpdateSpeaker,
} from '../hooks/useCharacterMapping';
import { useStyleGuide, useUpdateCharacterVoice } from '../hooks/useStyleGuide';
import type { CharacterVoice } from '../hooks/useStyleGuide';
import './styleGuide.css';

const GENDER_OPTIONS = [
  { value: 'male', label: 'Male' },
  { value: 'female', label: 'Female' },
  { value: 'non_binary', label: 'Non-binary' },
  { value: 'other', label: 'Other' },
];

const EXTRA_VALUE = '__extra__';
const NONE_VALUE = '__none__';

// Content-tag select values: speakers whose "lines" are actually on-screen
// text / lyrics rather than a character's dialogue.
const TAG_VALUE_PREFIX = '__tag_';
const CONTENT_TAG_OPTIONS: { value: string; tag: SpeakerContentTag; label: string }[] = [
  { value: '__tag_sign__', tag: 'sign', label: 'Sign / typesetting' },
  { value: '__tag_karaoke__', tag: 'karaoke', label: 'Karaoke' },
  { value: '__tag_song__', tag: 'song', label: 'Song / lyrics' },
];
const CONTENT_TAG_COLORS: Record<SpeakerContentTag, string> = {
  sign: 'grape',
  song: 'cyan',
  karaoke: 'teal',
};

function ConfidenceBadge({ speaker }: { speaker: ProjectSpeaker }) {
  if (speaker.match_origin === 'manual') {
    return <Badge size="xs" variant="light" color="blue">manual</Badge>;
  }
  if (speaker.match_confidence === null || speaker.match_origin === null) {
    return <Badge size="xs" variant="light" color="gray">pending</Badge>;
  }
  const pct = Math.round(speaker.match_confidence * 100);
  const color = speaker.match_confidence >= 0.8 ? 'green' : speaker.match_confidence >= 0.5 ? 'yellow' : 'red';
  const label = `${speaker.match_origin} ${pct}%`;
  if (speaker.match_rationale) {
    return (
      <Tooltip label={speaker.match_rationale} withArrow maw={320} multiline>
        <Badge size="xs" variant="light" color={color} style={{ cursor: 'default' }}>{label}</Badge>
      </Tooltip>
    );
  }
  return <Badge size="xs" variant="light" color={color}>{label}</Badge>;
}

function SpeakerName({ speaker }: { speaker: ProjectSpeaker }) {
  const samples = speaker.sample_lines.slice(0, 5).join('\n');
  if (samples) {
    return (
      <Tooltip label={samples} withArrow maw={420} multiline position="top-start">
        <Text size="sm" fw={600} truncate style={{ cursor: 'default' }} title={speaker.name}>
          {speaker.name}
        </Text>
      </Tooltip>
    );
  }
  return <Text size="sm" fw={600} truncate title={speaker.name}>{speaker.name}</Text>;
}

function SpeakerRow({
  speaker,
  projectId,
  characterOptions,
}: {
  speaker: ProjectSpeaker;
  projectId: number;
  characterOptions: { value: string; label: string }[];
}) {
  const updateSpeaker = useUpdateSpeaker();
  const retranslate = useRetranslateAffected();

  const selectValue = speaker.content_tag !== null
    ? `${TAG_VALUE_PREFIX}${speaker.content_tag}__`
    : speaker.is_extra
      ? EXTRA_VALUE
      : speaker.character_id !== null
        ? String(speaker.character_id)
        : NONE_VALUE;

  async function handleCharacterChange(value: string | null) {
    if (value === null || value === selectValue) return;
    const tagOption = CONTENT_TAG_OPTIONS.find((o) => o.value === value);
    const payload = tagOption
      ? { projectId, speakerId: speaker.id, content_tag: tagOption.tag }
      : value === EXTRA_VALUE
        ? { projectId, speakerId: speaker.id, is_extra: true }
        : value === NONE_VALUE
          ? { projectId, speakerId: speaker.id, character_id: null, is_extra: false, content_tag: null }
          : { projectId, speakerId: speaker.id, character_id: Number(value), is_extra: false };
    try {
      const result = await updateSpeaker.mutateAsync(payload);
      if (tagOption) {
        notifications.show({
          color: 'blue',
          title: 'Speaker tagged',
          message: `Lines of "${speaker.name}" will be treated as ${tagOption.tag} `
            + 'when a file\'s chunks are next planned. Already-planned files keep their chunks.',
        });
      }
      if (result.affected_chunk_count > 0 && !tagOption) {
        notifications.show({
          color: 'yellow',
          title: 'Mapping corrected',
          message: `${result.affected_chunk_count} already-translated chunk(s) used the old identity. `
            + 'Use the retranslate button to redo them.',
        });
      }
    } catch {
      notifications.show({ color: 'red', message: 'Could not update the speaker.' });
    }
  }

  async function handleRetranslate() {
    try {
      const result = await retranslate.mutateAsync({ projectId, speakerId: speaker.id });
      notifications.show({
        color: 'green',
        message: result.affected_chunk_count > 0
          ? `${result.affected_chunk_count} chunk(s) queued for retranslation.`
          : 'No affected chunks to retranslate.',
      });
    } catch {
      notifications.show({ color: 'red', message: 'Retranslation request failed.' });
    }
  }

  return (
    <div className="sgd-row sgd-speaker" style={{ opacity: speaker.is_extra ? 0.55 : 1 }}>
      <div className="sgd-full">
        <SpeakerName speaker={speaker} />
      </div>
      <Text size="xs" c="dimmed" className="sgd-num">
        {speaker.line_count}
      </Text>
      <div>
        {speaker.content_tag ? (
          <Badge size="xs" variant="light" color={CONTENT_TAG_COLORS[speaker.content_tag]}>
            {speaker.content_tag}
          </Badge>
        ) : (
          <ConfidenceBadge speaker={speaker} />
        )}
      </div>
      <Tooltip label="Overrides the character's gender in translation prompts" withArrow>
        <Select
          size="xs"
          placeholder="inherit"
          clearable
          data={GENDER_OPTIONS}
          value={speaker.gender}
          onChange={(v) => {
            if (v !== speaker.gender) {
              void updateSpeaker.mutateAsync({ projectId, speakerId: speaker.id, gender: v });
            }
          }}
          disabled={updateSpeaker.isPending}
        />
      </Tooltip>
      <Select
        size="xs"
        data={[
          { value: NONE_VALUE, label: '— unmapped —' },
          { value: EXTRA_VALUE, label: 'Extra / non-character' },
          {
            group: 'Non-dialogue content',
            items: CONTENT_TAG_OPTIONS.map(({ value, label }) => ({ value, label })),
          },
          ...(characterOptions.length
            ? [{ group: 'Characters', items: characterOptions }]
            : []),
        ]}
        value={selectValue}
        onChange={(v) => void handleCharacterChange(v)}
        searchable
        disabled={updateSpeaker.isPending}
      />
      <Tooltip label="Retranslate chunks containing this speaker's lines" withArrow>
        <Button
          size="compact-xs"
          variant="subtle"
          color="orange"
          leftSection={<ArrowsClockwise size={12} />}
          loading={retranslate.isPending}
          onClick={() => void handleRetranslate()}
        >
          Retranslate
        </Button>
      </Tooltip>
    </div>
  );
}

function ExtraRow({
  speaker,
  projectId,
  characterOptions,
}: {
  speaker: ProjectSpeaker;
  projectId: number;
  characterOptions: { value: string; label: string }[];
}) {
  const updateSpeaker = useUpdateSpeaker();

  async function handleCharacterChange(value: string | null) {
    if (value === null || value === EXTRA_VALUE) return;
    const payload = value === NONE_VALUE
      ? { projectId, speakerId: speaker.id, character_id: null, is_extra: false }
      : { projectId, speakerId: speaker.id, character_id: Number(value), is_extra: false };
    try {
      await updateSpeaker.mutateAsync(payload);
    } catch {
      notifications.show({ color: 'red', message: 'Could not update the speaker.' });
    }
  }

  return (
    <div className="sgd-row sgd-extra" style={{ opacity: 0.55 }}>
      <div>
        <SpeakerName speaker={speaker} />
      </div>
      <Text size="xs" c="dimmed" className="sgd-num">
        {speaker.line_count}
      </Text>
      <Select
        size="xs"
        data={[
          { value: NONE_VALUE, label: '— unmapped —' },
          { value: EXTRA_VALUE, label: 'Extra / non-character' },
          ...characterOptions,
        ]}
        value={EXTRA_VALUE}
        onChange={(v) => void handleCharacterChange(v)}
        searchable
        disabled={updateSpeaker.isPending}
      />
    </div>
  );
}

function CharacterBlock({
  character,
  speakers,
  voice,
  projectId,
  characterOptions,
}: {
  character: ProjectCharacterWithSpeakers;
  speakers: ProjectSpeaker[];
  voice: CharacterVoice | undefined;
  projectId: number;
  characterOptions: { value: string; label: string }[];
}) {
  const updateCharacter = useUpdateCharacter();
  const updateVoice = useUpdateCharacterVoice(projectId);

  const totalLines = speakers.reduce((s, sp) => s + sp.line_count, 0);

  return (
    <div className="sgd-block">
      <div className="sgd-row sgd-char">
        <Text size="sm" fw={600} truncate title={character.name}>
          {character.name}
        </Text>
        <Select
          size="xs"
          placeholder="Gender"
          clearable
          data={GENDER_OPTIONS}
          value={character.gender}
          onChange={(v) => {
            if (v !== character.gender) {
              void updateCharacter.mutateAsync({ projectId, characterId: character.id, gender: v });
            }
          }}
          disabled={updateCharacter.isPending}
        />
        {voice ? (
          <>
            <TextInput
              size="xs"
              defaultValue={voice.voice_note ?? ''}
              placeholder="Voice note"
              onBlur={(e) => {
                if ((voice.voice_note ?? '') !== e.currentTarget.value) {
                  updateVoice.mutate({ styleId: voice.id, voice_note: e.currentTarget.value });
                }
              }}
            />
            <TextInput
              size="xs"
              defaultValue={voice.register ?? ''}
              placeholder="Register"
              onBlur={(e) => {
                if ((voice.register ?? '') !== e.currentTarget.value) {
                  updateVoice.mutate({ styleId: voice.id, register: e.currentTarget.value });
                }
              }}
            />
          </>
        ) : (
          <>
            <div />
            <div />
          </>
        )}
        <Text size="xs" c="dimmed" className="sgd-num">
          {totalLines > 0 ? `${totalLines} lines` : ''}
        </Text>
      </div>
      {speakers.length > 0 && (
        <div className="sgd-speakers">
          {speakers.map((speaker) => (
            <SpeakerRow
              key={speaker.id}
              speaker={speaker}
              projectId={projectId}
              characterOptions={characterOptions}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// Least confident first — that's what deserves human eyes.
function confidenceRank(s: ProjectSpeaker): number {
  return s.match_origin === 'manual' || s.is_extra ? 2 : s.match_confidence === null ? 0 : s.match_confidence;
}

function bySuspicion(a: ProjectSpeaker, b: ProjectSpeaker): number {
  const diff = confidenceRank(a) - confidenceRank(b);
  return diff !== 0 ? diff : b.line_count - a.line_count;
}

function byLineCountThenName(a: ProjectSpeaker, b: ProjectSpeaker): number {
  const countDiff = b.line_count - a.line_count;
  const nameDiff = a.name.localeCompare(b.name);
  return countDiff !== 0 ? countDiff : nameDiff !== 0 ? nameDiff : a.id - b.id;
}

export function CharactersTab({ projectId }: { projectId: number }) {
  const { data: characters = [], isLoading: charsLoading } = useProjectCharacters(projectId);
  const { data: speakers = [], isLoading: speakersLoading } = useProjectSpeakers(projectId);
  const { data: styleGuide } = useStyleGuide(projectId, true);
  const createCharacter = useCreateCharacter();
  const [newCharName, setNewCharName] = useState('');
  const [newCharGender, setNewCharGender] = useState<string | null>(null);
  const [extrasOpen, setExtrasOpen] = useState(false);

  async function handleAddCharacter() {
    const name = newCharName.trim();
    if (!name) return;
    try {
      await createCharacter.mutateAsync({ projectId, name, gender: newCharGender });
      setNewCharName('');
      setNewCharGender(null);
    } catch {
      notifications.show({ color: 'red', message: 'Character already exists or could not be created.' });
    }
  }

  const characterOptions = useMemo(
    () => [...characters]
      .sort((a, b) => a.name.localeCompare(b.name))
      .map((c) => ({ value: String(c.id), label: c.gender ? `${c.name} (${c.gender})` : c.name })),
    [characters],
  );

  const voiceByCharacter = useMemo(() => {
    const map = new Map<number, CharacterVoice>();
    for (const voice of styleGuide?.character_voices ?? []) {
      map.set(voice.character_id, voice);
    }
    return map;
  }, [styleGuide]);

  const unmapped = useMemo(
    () => speakers.filter((s) => !s.is_extra && s.character_id === null).sort(byLineCountThenName),
    [speakers],
  );
  const extras = useMemo(
    () => speakers.filter((s) => s.is_extra).sort((a, b) => b.line_count - a.line_count),
    [speakers],
  );

  const speakersByCharacter = useMemo(() => {
    const map = new Map<number, ProjectSpeaker[]>();
    for (const speaker of speakers) {
      if (speaker.is_extra || speaker.character_id === null) continue;
      const list = map.get(speaker.character_id) ?? [];
      list.push(speaker);
      map.set(speaker.character_id, list);
    }
    for (const list of map.values()) list.sort(bySuspicion);
    return map;
  }, [speakers]);

  const sortedCharacters = useMemo(() => {
    return [...characters].sort((a, b) => {
      const nameDiff = a.name.localeCompare(b.name);
      return nameDiff !== 0 ? nameDiff : a.id - b.id;
    });
  }, [characters]);

  const isLoading = charsLoading || speakersLoading;

  if (isLoading) return <Center py="xl"><Loader size="sm" /></Center>;

  return (
    <Stack gap="sm" pt="sm">
      <Text size="xs" c="dimmed">
        Speaker→character mapping is inferred automatically; translation never waits for it to be
        reviewed. Corrections apply to future chunks immediately — use Retranslate to redo chunks
        that already used the wrong identity. Hover a speaker for sample lines.
      </Text>
      <Group gap="xs" align="flex-end">
        <TextInput
          size="xs"
          label="Add missing character"
          placeholder="Character name"
          value={newCharName}
          onChange={(e) => setNewCharName(e.currentTarget.value)}
          style={{ width: 240 }}
        />
        <Select
          size="xs"
          placeholder="Gender"
          clearable
          data={GENDER_OPTIONS}
          value={newCharGender}
          onChange={setNewCharGender}
          w={120}
        />
        <Button
          size="xs"
          variant="light"
          leftSection={<Plus size={13} />}
          loading={createCharacter.isPending}
          disabled={!newCharName.trim()}
          onClick={() => void handleAddCharacter()}
        >
          Add
        </Button>
      </Group>
      {speakers.length === 0 && characters.length === 0 ? (
        <Text size="sm" c="dimmed">No speakers discovered in this project's subtitles.</Text>
      ) : (
        <ScrollArea.Autosize mah="60vh" offsetScrollbars scrollbars="y">
          <Stack gap="sm">
            {unmapped.length > 0 && (
              <Stack gap={4}>
                <Text size="sm" fw={700}>Unmapped speakers</Text>
                <div className="sgd-zebra">
                  {unmapped.map((speaker) => (
                    <SpeakerRow
                      key={speaker.id}
                      speaker={speaker}
                      projectId={projectId}
                      characterOptions={characterOptions}
                    />
                  ))}
                </div>
              </Stack>
            )}
            {sortedCharacters.length > 0 && (
              <Stack gap="xs">
                <Text size="sm" fw={700}>Characters</Text>
                <div className="sgd-head sgd-char">
                  <span>Name</span>
                  <span>Gender</span>
                  <span>Voice note</span>
                  <span>Register</span>
                  <span className="sgd-num">Lines</span>
                </div>
                <div className="sgd-zebra">
                  {sortedCharacters.map((character) => (
                    <CharacterBlock
                      key={character.id}
                      character={character}
                      speakers={speakersByCharacter.get(character.id) ?? []}
                      voice={voiceByCharacter.get(character.id)}
                      projectId={projectId}
                      characterOptions={characterOptions}
                    />
                  ))}
                </div>
              </Stack>
            )}
            {extras.length > 0 && (
              <Stack gap={4}>
                <Button
                  variant="subtle"
                  size="compact-xs"
                  color="gray"
                  leftSection={extrasOpen ? <CaretDown size={12} /> : <CaretRight size={12} />}
                  onClick={() => setExtrasOpen((o) => !o)}
                  style={{ alignSelf: 'flex-start' }}
                >
                  Extras ({extras.length})
                </Button>
                <Collapse expanded={extrasOpen}>
                  <div className="sgd-zebra">
                    {extras.map((speaker) => (
                      <ExtraRow
                        key={speaker.id}
                        speaker={speaker}
                        projectId={projectId}
                        characterOptions={characterOptions}
                      />
                    ))}
                  </div>
                </Collapse>
              </Stack>
            )}
          </Stack>
        </ScrollArea.Autosize>
      )}
    </Stack>
  );
}
