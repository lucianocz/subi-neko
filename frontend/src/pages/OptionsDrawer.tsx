import { useState } from 'react';
import {
  Center,
  Divider,
  Drawer,
  Loader,
  NumberInput,
  PasswordInput,
  Select,
  Stack,
  Switch,
  Text,
  Textarea,
  TextInput,
  Title,
  Anchor,
} from '@mantine/core';
import type { OptionsMap } from '../hooks/useOptions';
import { useOptions, useSaveOptions } from '../hooks/useOptions';

// ─── Language list ────────────────────────────────────────────────────────────

const LANGUAGES = [
  { code: 'ar', name: 'Arabic' },
  { code: 'ca', name: 'Catalan' },
  { code: 'zh', name: 'Chinese' },
  { code: 'hr', name: 'Croatian' },
  { code: 'cs', name: 'Czech' },
  { code: 'da', name: 'Danish' },
  { code: 'nl', name: 'Dutch' },
  { code: 'en', name: 'English' },
  { code: 'fi', name: 'Finnish' },
  { code: 'fr', name: 'French' },
  { code: 'de', name: 'German' },
  { code: 'el', name: 'Greek' },
  { code: 'hu', name: 'Hungarian' },
  { code: 'id', name: 'Indonesian' },
  { code: 'it', name: 'Italian' },
  { code: 'ja', name: 'Japanese' },
  { code: 'ko', name: 'Korean' },
  { code: 'no', name: 'Norwegian' },
  { code: 'pl', name: 'Polish' },
  { code: 'pt', name: 'Portuguese' },
  { code: 'ro', name: 'Romanian' },
  { code: 'ru', name: 'Russian' },
  { code: 'sr', name: 'Serbian' },
  { code: 'sk', name: 'Slovak' },
  { code: 'sl', name: 'Slovenian' },
  { code: 'es', name: 'Spanish' },
  { code: 'sv', name: 'Swedish' },
  { code: 'tr', name: 'Turkish' },
  { code: 'uk', name: 'Ukrainian' },
  { code: 'vi', name: 'Vietnamese' },
];

// ─── Field components (save on blur / change) ─────────────────────────────────

function SaveOnBlurText({
  optionKey,
  label,
  description,
  defaultValue,
  placeholder,
}: {
  optionKey: string;
  label: string;
  description: string;
  defaultValue: string | null;
  placeholder?: string;
}) {
  const { mutate } = useSaveOptions();
  return (
    <TextInput
      label={label}
      description={description}
      defaultValue={defaultValue ?? ''}
      placeholder={placeholder}
      onBlur={(e) => mutate({ [optionKey]: e.currentTarget.value || null })}
    />
  );
}

function SaveOnBlurPassword({
  optionKey,
  label,
  description,
  defaultValue,
}: {
  optionKey: string;
  label: string;
  description: string;
  defaultValue: string | null;
}) {
  const { mutate } = useSaveOptions();
  return (
    <PasswordInput
      label={label}
      description={description}
      defaultValue={defaultValue ?? ''}
      onBlur={(e) => mutate({ [optionKey]: e.currentTarget.value || null })}
    />
  );
}

function SaveOnBlurNumber({
  optionKey,
  label,
  description,
  defaultValue,
  min,
  max,
}: {
  optionKey: string;
  label: string;
  description: string;
  defaultValue: string | null;
  min?: number;
  max?: number;
}) {
  const { mutate } = useSaveOptions();
  const [value, setValue] = useState<number | string>(
    defaultValue != null ? Number(defaultValue) : ''
  );
  return (
    <NumberInput
      label={label}
      description={description}
      value={value}
      onChange={setValue}
      onBlur={() => typeof value === 'number' && mutate({ [optionKey]: String(value) })}
      min={min}
      max={max}
    />
  );
}

function SaveOnChangeSelect({
  optionKey,
  label,
  description,
  defaultValue,
  data,
}: {
  optionKey: string;
  label: string;
  description: string;
  defaultValue: string | null;
  data: { value: string; label: string }[];
}) {
  const { mutate } = useSaveOptions();
  const [value, setValue] = useState<string | null>(defaultValue);
  return (
    <Select
      label={label}
      description={description}
      value={value}
      onChange={(v) => { setValue(v); mutate({ [optionKey]: v }); }}
      data={data}
    />
  );
}

function SaveOnBlurTextarea({
  optionKey,
  label,
  description,
  defaultValue,
  onReset,
}: {
  optionKey: string;
  label: string;
  description: string;
  defaultValue: string | null;
  onReset: () => void;
}) {
  const { mutate } = useSaveOptions();
  return (
    <Textarea
      label={
        <span>
          {label}&nbsp;
          <Anchor size="xs" c="dimmed" onClick={onReset} style={{ fontWeight: 400 }}>
            reset to default
          </Anchor>
        </span>
      }
      description={description}
      defaultValue={defaultValue ?? ''}
      onBlur={(e) => mutate({ [optionKey]: e.currentTarget.value || null })}
      autosize
      minRows={4}
      maxRows={14}
      styles={{ input: { fontFamily: 'monospace', fontSize: 12 } }}
    />
  );
}

function SaveOnChangeSwitch({
  optionKey,
  label,
  description,
  defaultValue,
}: {
  optionKey: string;
  label: string;
  description: string;
  defaultValue: string | null;
}) {
  const { mutate } = useSaveOptions();
  const [checked, setChecked] = useState(
    (defaultValue ?? '0').trim().toLowerCase() === '1'
  );
  return (
    <Switch
      label={label}
      description={description}
      checked={checked}
      onChange={(e) => {
        setChecked(e.currentTarget.checked);
        mutate({ [optionKey]: e.currentTarget.checked ? '1' : '0' });
      }}
    />
  );
}

function LanguageSelect({ defaultCode }: { defaultCode: string | null }) {
  const { mutate } = useSaveOptions();
  const [code, setCode] = useState<string | null>(defaultCode);

  const handleChange = (val: string | null) => {
    setCode(val);
    const lang = LANGUAGES.find((l) => l.code === val);
    mutate({ TARGET_LANG_CODE: val, TARGET_LANG_NAME: lang?.name ?? null });
  };

  return (
    <Select
      label="Target language"
      description="Language subtitles will be translated into. Sets both the grammar provider language code and the display name used in LLM prompts."
      value={code}
      onChange={handleChange}
      data={LANGUAGES.map((l) => ({ value: l.code, label: l.name }))}
      searchable
      clearable
      placeholder="Select a language…"
    />
  );
}

// ─── Section wrapper ──────────────────────────────────────────────────────────

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <Stack gap="sm">
      <Title order={6} c="dimmed" style={{ textTransform: 'uppercase', letterSpacing: '0.06em' }}>
        {title}
      </Title>
      {children}
    </Stack>
  );
}

// ─── Full options form ────────────────────────────────────────────────────────

function OptionsForm({ options }: { options: OptionsMap }) {
  const { mutate } = useSaveOptions();

  const resetPrompt = (key: string) => mutate({ [key]: null });

  return (
    <Stack gap="xl" pb="xl">
      <Section title="Language">
        <LanguageSelect defaultCode={options['TARGET_LANG_CODE'] ?? null} />
      </Section>

      <Divider />

      <Section title="OpenAI">
        <SaveOnBlurPassword
          optionKey="OPENAI_API_KEY"
          label="API key"
          description="Your OpenAI (or compatible) API key. Never shared outside this app."
          defaultValue={options['OPENAI_API_KEY'] ?? null}
        />
        <SaveOnBlurText
          optionKey="OPENAI_API_BASE"
          label="API base URL"
          description="Override the default OpenAI endpoint. Leave blank for api.openai.com. Useful for Azure OpenAI, local LLMs (Ollama), or proxies."
          defaultValue={options['OPENAI_API_BASE'] ?? null}
          placeholder="https://api.openai.com/v1"
        />
        <SaveOnBlurText
          optionKey="OPENAI_MODEL_CHEAP"
          label="Cheap model"
          description="Used for translation jobs where speed and cost matter."
          defaultValue={options['OPENAI_MODEL_CHEAP'] ?? null}
        />
        <SaveOnBlurText
          optionKey="OPENAI_MODEL_BETTER"
          label="Better model"
          description="Used for repair, polish, and the final read-only QA audit where quality is critical."
          defaultValue={options['OPENAI_MODEL_BETTER'] ?? null}
        />
        <SaveOnChangeSelect
          optionKey="LLM_STRUCTURED_OUTPUTS"
          label="Structured outputs"
          description="How LLM responses are constrained. Auto probes the backend and falls back automatically (json_schema → json_object → text)."
          defaultValue={options['LLM_STRUCTURED_OUTPUTS'] ?? 'auto'}
          data={[
            { value: 'auto', label: 'Auto (recommended)' },
            { value: 'json_schema', label: 'JSON schema (strict)' },
            { value: 'json_object', label: 'JSON object' },
            { value: 'text', label: 'Free text' },
          ]}
        />
        <SaveOnBlurText
          optionKey="LLM_PRICES_JSON"
          label="Model prices (JSON)"
          description={'Per-million-token prices for cost tracking, e.g. {"gpt-5.4-mini": {"in": 0.4, "out": 1.6}}. Leave blank to skip cost accounting.'}
          defaultValue={options['LLM_PRICES_JSON'] ?? null}
          placeholder='{"gpt-5.4-mini": {"in": 0.4, "out": 1.6}}'
        />
      </Section>

      <Divider />

      <Section title="Quality">
        <SaveOnChangeSwitch
          optionKey="TRANSLATE_KARAOKE"
          label="Translate karaoke lines"
          description="Off (recommended): karaoke lines with per-syllable \\k timing are kept in the original language so the timing survives. On: they are translated and the syllable timing is lost."
          defaultValue={options['TRANSLATE_KARAOKE'] ?? '0'}
        />
        <SaveOnChangeSwitch
          optionKey="REPLACE_INCOMPATIBLE_FONTS"
          label="Replace incompatible fonts"
          description="On (default): translated subtitles use each style's replacement font and size from Styles & fonts where set. Off: translated subtitles keep the source font and size; stored replacements are kept and apply again when re-enabled."
          defaultValue={options['REPLACE_INCOMPATIBLE_FONTS'] ?? '1'}
        />
        <SaveOnBlurNumber
          optionKey="CPS_LIMIT"
          label="Reading speed limit (CPS)"
          description="Characters per second above which a line is flagged for condensing."
          defaultValue={options['CPS_LIMIT'] ?? null}
          min={5}
          max={40}
        />
        <SaveOnBlurNumber
          optionKey="MAX_ROW_CHARS"
          label="Max characters per row"
          description="Row length above which a line is flagged for rewrapping or condensing."
          defaultValue={options['MAX_ROW_CHARS'] ?? null}
          min={20}
          max={80}
        />
        <SaveOnChangeSwitch
          optionKey="AUTO_LINE_BREAK"
          label="Auto line breaks"
          description="On (recommended): dialogue rows longer than the row limit are automatically rebalanced onto two lines at a word boundary; only lines that still don't fit are flagged. Off: long rows are flagged for manual rewrapping."
          defaultValue={options['AUTO_LINE_BREAK'] ?? '1'}
        />
      </Section>

      <Divider />

      <Section title="Processing">
        <SaveOnBlurNumber
          optionKey="CHUNK_SIZE"
          label="Chunk size"
          description="Number of subtitle lines grouped into a single translation chunk. Larger chunks give more context but cost more tokens."
          defaultValue={options['CHUNK_SIZE'] ?? null}
          min={10}
          max={500}
        />
        <SaveOnBlurNumber
          optionKey="PREPEND_CONTEXT_SIZE"
          label="Context lines"
          description="Rolling context window: how many preceding subtitle lines (with their finished translations) each chunk sees. Counted within the chunk's own kind of content, so interleaved signs and songs never eat into a dialogue chunk's window. Dialogue translation is serialized per file — a chunk starts only once the previous one is polished — so this context carries the final wording, not a draft."
          defaultValue={options['PREPEND_CONTEXT_SIZE'] ?? null}
          min={0}
          max={50}
        />
        <SaveOnBlurNumber
          optionKey="LOOKAHEAD_CONTEXT_SIZE"
          label="Lookahead lines"
          description="How many lines following a chunk are shown to the translator and polish passes as untranslated English. Without it the last lines of every chunk are translated blind to what comes next. Counted within the chunk's own kind of content. Set to 0 to disable."
          defaultValue={options['LOOKAHEAD_CONTEXT_SIZE'] ?? null}
          min={0}
          max={20}
        />
        <SaveOnBlurNumber
          optionKey="JOB_WORKER_COUNT"
          label="Worker threads"
          description="Number of parallel background job workers. Takes effect immediately without restart."
          defaultValue={options['JOB_WORKER_COUNT'] ?? null}
          min={1}
          max={32}
        />
        <SaveOnBlurNumber
          optionKey="MAPPING_CHARACTER_DESCRIPTION_MAX"
          label="Mapping description per character"
          description="Maximum character-description length used for speaker mapping. Larger values provide richer evidence but consume more LLM input. Set to 0 to omit descriptions."
          defaultValue={options['MAPPING_CHARACTER_DESCRIPTION_MAX'] ?? '400'}
          min={0}
          max={4000}
        />
        <SaveOnBlurNumber
          optionKey="MAPPING_CHARACTER_DESCRIPTION_BUDGET"
          label="Mapping description total budget"
          description="Total character-description text available to speaker mapping across the roster. Set to 0 to omit descriptions."
          defaultValue={options['MAPPING_CHARACTER_DESCRIPTION_BUDGET'] ?? '24000'}
          min={0}
          max={200000}
        />
        <SaveOnBlurNumber
          optionKey="STYLE_BIBLE_CHARACTER_DESCRIPTION_MAX"
          label="Style Bible description per character"
          description="Maximum character-description length used for initial Style Bible generation. Larger values improve characterization but consume more LLM input. Set to 0 to omit descriptions."
          defaultValue={options['STYLE_BIBLE_CHARACTER_DESCRIPTION_MAX'] ?? '600'}
          min={0}
          max={4000}
        />
        <SaveOnBlurNumber
          optionKey="STYLE_BIBLE_CHARACTER_DESCRIPTION_BUDGET"
          label="Style Bible description total budget"
          description="Total character-description text available to initial Style Bible generation across the roster. Set to 0 to omit descriptions."
          defaultValue={options['STYLE_BIBLE_CHARACTER_DESCRIPTION_BUDGET'] ?? '32000'}
          min={0}
          max={200000}
        />
      </Section>

      <Divider />

      <Section title="System">
        <SaveOnChangeSelect
          optionKey="LOG_LEVEL"
          label="Log level"
          description="Minimum severity of log messages written to the server console. Takes effect immediately without restart."
          defaultValue={options['LOG_LEVEL'] ?? 'INFO'}
          data={['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'].map((v) => ({ value: v, label: v }))}
        />
      </Section>

      <Divider />

      <Section title="Prompts">
        <Text size="xs" c="dimmed">
          System prompts sent to the LLM. Use <code style={{ fontFamily: 'monospace' }}>{'{TARGET_LANG_NAME}'}</code> as a placeholder for the target language. Clear a prompt to revert to the built-in default.
        </Text>
        <SaveOnBlurTextarea
          optionKey="TRANSLATION_PROMPT"
          label="Translation prompt"
          description="Instructs the model how to translate subtitle chunks."
          defaultValue={options['TRANSLATION_PROMPT'] ?? null}
          onReset={() => resetPrompt('TRANSLATION_PROMPT')}
        />
        <SaveOnBlurTextarea
          optionKey="REPAIR_PROMPT"
          label="Repair prompt"
          description="Instructs the model how to fix translation errors flagged by validation."
          defaultValue={options['REPAIR_PROMPT'] ?? null}
          onReset={() => resetPrompt('REPAIR_PROMPT')}
        />
        <SaveOnBlurTextarea
          optionKey="POLISH_PROMPT"
          label="Polish prompt"
          description="Instructs the model how to rework draft translations into natural, fluent target-language subtitles."
          defaultValue={options['POLISH_PROMPT'] ?? null}
          onReset={() => resetPrompt('POLISH_PROMPT')}
        />
        <SaveOnBlurTextarea
          optionKey="FINAL_QA_PROMPT"
          label="Final QA audit prompt"
          description="Instructs the read-only post-review auditor to find semantic and target-language defects without changing subtitles."
          defaultValue={options['FINAL_QA_PROMPT'] ?? null}
          onReset={() => resetPrompt('FINAL_QA_PROMPT')}
        />
        <SaveOnBlurTextarea
          optionKey="SIGN_TRANSLATION_PROMPT"
          label="Sign translation prompt"
          description="Used for on-screen text (signs, captions, typesetting) instead of the dialogue prompt."
          defaultValue={options['SIGN_TRANSLATION_PROMPT'] ?? null}
          onReset={() => resetPrompt('SIGN_TRANSLATION_PROMPT')}
        />
        <SaveOnBlurTextarea
          optionKey="SONG_TRANSLATION_PROMPT"
          label="Song translation prompt"
          description="Used for song lyrics (OP/ED/insert songs) instead of the dialogue prompt."
          defaultValue={options['SONG_TRANSLATION_PROMPT'] ?? null}
          onReset={() => resetPrompt('SONG_TRANSLATION_PROMPT')}
        />
        <SaveOnBlurTextarea
          optionKey="ANALYZE_PROMPT"
          label="Script analysis prompt"
          description="Runs once per file before translation: produces the episode synopsis, scene segmentation, tricky-line notes, T–V address pairs and suggested glossary terms."
          defaultValue={options['ANALYZE_PROMPT'] ?? null}
          onReset={() => resetPrompt('ANALYZE_PROMPT')}
        />
        <SaveOnBlurTextarea
          optionKey="MAPPING_PROMPT"
          label="Speaker mapping prompt"
          description="Matches raw subtitle speaker labels to characters from the metadata roster. Language-neutral — it does not use the target language placeholder."
          defaultValue={options['MAPPING_PROMPT'] ?? null}
          onReset={() => resetPrompt('MAPPING_PROMPT')}
        />
        <SaveOnBlurTextarea
          optionKey="STYLE_BIBLE_PROMPT"
          label="Style bible prompt"
          description="Builds the project-level tone, register and honorific guidance, plus the initial glossary, character voices and address pairs."
          defaultValue={options['STYLE_BIBLE_PROMPT'] ?? null}
          onReset={() => resetPrompt('STYLE_BIBLE_PROMPT')}
        />
        <SaveOnBlurTextarea
          optionKey="STYLE_BIBLE_UPDATE_PROMPT"
          label="Style bible update prompt"
          description="Runs after each accepted episode: captures only NEW terms, voices and address-pair changes from the finished translation."
          defaultValue={options['STYLE_BIBLE_UPDATE_PROMPT'] ?? null}
          onReset={() => resetPrompt('STYLE_BIBLE_UPDATE_PROMPT')}
        />
      </Section>
    </Stack>
  );
}

// ─── Drawer ───────────────────────────────────────────────────────────────────

export function OptionsDrawer({
  opened,
  onClose,
}: {
  opened: boolean;
  onClose: () => void;
}) {
  const { data: options, isLoading } = useOptions();

  return (
    <Drawer
      opened={opened}
      onClose={onClose}
      title={<Title order={4}>Settings</Title>}
      position="right"
      size={520}
      scrollAreaComponent={undefined}
    >
      {isLoading ? (
        <Center h={200}><Loader /></Center>
      ) : options ? (
        <OptionsForm key={JSON.stringify(options)} options={options} />
      ) : null}
    </Drawer>
  );
}
