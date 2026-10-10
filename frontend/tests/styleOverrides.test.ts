import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  STYLE_GRID_COLUMNS,
  STYLE_GRID_TEMPLATE,
  cycleTriState,
  draftFromStyle,
  draftToUpdate,
  formatDecimal,
  hasAnyOverride,
  isDraftDirty,
  isDraftValid,
  parseDecimalInput,
  previewCss,
  resolveEffective,
  sourceRowCells,
  styleToUpdate,
  triStateLabel,
} from '../src/utils/styleOverrides.ts';
import type { StyleSource } from '../src/utils/styleOverrides.ts';

const style = (over: Partial<StyleSource> = {}): StyleSource => ({
  font_name: 'Arial', font_size: 42, bold: true, italic: false, outline: 2.5, shadow: 1.25,
  primary_colour: '&H80112233&', outline_colour: '&H00445566&', back_colour: '&H7F778899&',
  replacement_font_name: null, replacement_font_size: null, replacement_bold: null,
  replacement_italic: null, replacement_outline: null, replacement_shadow: null,
  replacement_primary_colour: null, replacement_outline_colour: null, replacement_back_colour: null,
  ...over,
});

test('bold/italic cycle inherit → on → off → inherit', () => {
  assert.equal(cycleTriState(null), true);
  assert.equal(cycleTriState(true), false);
  assert.equal(cycleTriState(false), null);
});

test('tri-state tooltips tell inherited, on and off apart', () => {
  const labels = [null, true, false].map((v) => triStateLabel('Bold', v, true));
  assert.equal(new Set(labels).size, 3);
  assert.match(labels[0], /inherited/);
  assert.match(labels[1], /forced on/);
  assert.match(labels[2], /forced off/);
});

test('decimals are never rounded and show no float noise', () => {
  for (const v of [0.5, 1.25, 2.75, 0.1]) assert.equal(parseDecimalInput(v), v);
  assert.equal(parseDecimalInput('1.'), 1);
  assert.equal(parseDecimalInput('0.50'), 0.5);
  assert.equal(parseDecimalInput('2.75'), 2.75);
  assert.equal(formatDecimal(0.1 + 0.2), '0.3');
  assert.equal(formatDecimal(2.5), '2.5');
  assert.equal(formatDecimal(3), '3');
  assert.equal(formatDecimal(1.2345), '1.2345');
});

test('empty input inherits, explicit zero stays zero', () => {
  assert.equal(parseDecimalInput(''), null);
  assert.equal(parseDecimalInput('  '), null);
  assert.equal(parseDecimalInput(0), 0);
  const d = { ...draftFromStyle(style()), outline: 0, shadow: '' as number | string };
  const u = draftToUpdate(d);
  assert.equal(u.replacement_outline, 0);
  assert.equal(u.replacement_shadow, null);
});

test('explicit false is distinct from inherit in the payload', () => {
  const u = draftToUpdate({ ...draftFromStyle(style()), bold: false, italic: null });
  assert.equal(u.replacement_bold, false);
  assert.equal(u.replacement_italic, null);
});

test('draft round-trips the stored overrides and is clean until edited', () => {
  const s = style({ replacement_bold: false, replacement_outline: 0, replacement_primary_colour: '&H40102030&' });
  const d = draftFromStyle(s);
  assert.equal(isDraftDirty(d, s), false);
  assert.equal(isDraftDirty({ ...d, outline: 0.25 }, s), true);
  assert.equal(isDraftDirty({ ...d, outline: '0' }, s), false); // "0" typed == stored 0
  assert.deepEqual(draftToUpdate(d), styleToUpdate(s));
});

test('resetting one property inherits the source, it does not copy it', () => {
  const s = style({
    replacement_bold: false, replacement_outline: 0, replacement_primary_colour: '&HFF000000&',
  });
  let d = draftFromStyle(s);
  assert.equal(resolveEffective(s, d).bold, false);
  d = { ...d, bold: null };
  assert.equal(draftToUpdate(d).replacement_bold, null); // not `true`
  assert.equal(resolveEffective(s, d).bold, true);
  d = { ...d, outline: '' };
  assert.equal(draftToUpdate(d).replacement_outline, null);
  assert.equal(resolveEffective(s, d).outline, 2.5);
  d = { ...d, primary: null };
  assert.equal(draftToUpdate(d).replacement_primary_colour, null);
  assert.equal(resolveEffective(s, d).primary, '&H80112233&'); // original alpha restored
  assert.equal(hasAnyOverride(d), false);
});

test('effective values fall back per property; missing source colour is white', () => {
  const s = style({ primary_colour: null });
  const e = resolveEffective(s, { ...draftFromStyle(s), fontSize: 30, shadow: 0 });
  assert.deepEqual(
    [e.fontName, e.fontSize, e.bold, e.italic, e.outline, e.shadow, e.primary],
    ['Arial', 30, true, false, 2.5, 0, '&H00FFFFFF&'],
  );
});

test('validation blocks negative / oversized numbers', () => {
  const d = draftFromStyle(style());
  assert.equal(isDraftValid(d), true);
  assert.equal(isDraftValid({ ...d, outline: -1 }), false);
  assert.equal(isDraftValid({ ...d, shadow: 1001 }), false);
  assert.equal(isDraftValid({ ...d, fontSize: 0 }), false);
  assert.equal(isDraftValid({ ...d, outline: 0, shadow: 0.5 }), true);
});

test('source row reads only imported values, never replacements', () => {
  const plain = sourceRowCells(style());
  const overridden = sourceRowCells(style({
    replacement_font_name: 'Noto Sans', replacement_font_size: 10, replacement_bold: false,
    replacement_outline: 0, replacement_primary_colour: '&H00000000&',
  }));
  assert.deepEqual(overridden, plain);
  assert.deepEqual(plain.outline, { kind: 'text', text: '2.5' });
  assert.deepEqual(plain.shadow, { kind: 'text', text: '1.25' });
  assert.equal(plain.bold.kind === 'flag' && plain.bold.on, true);
  assert.equal(plain.primary.kind === 'colour' && plain.primary.title, '#332211 · opacity 50% · &H80112233&');
});

test('header, source row and replacement row share one column list', () => {
  const ids = STYLE_GRID_COLUMNS.map((c) => c.id);
  assert.equal(new Set(ids).size, ids.length);
  assert.equal(STYLE_GRID_TEMPLATE.split(/\s+(?![^()]*\))/).length, ids.length);
  assert.deepEqual(Object.keys(sourceRowCells(style())), ids.filter((i) => i !== 'label'));
  assert.deepEqual(STYLE_GRID_COLUMNS.slice(-3).map((c) => c.header), ['Text', 'Border', 'Shadow']);
});

test('preview reflects every effective property incl. alpha', () => {
  const s = style();
  const e = resolveEffective(s, {
    ...draftFromStyle(s), fontName: 'Noto Sans', fontSize: 30, bold: false, italic: true,
    outline: 1.5, shadow: 0.75, primary: '&H80112233&', outlineColour: '&H00000000&', backColour: '&HFF0000FF&',
  });
  const css = previewCss(e);
  assert.equal(css.fontFamily, '"Noto Sans", sans-serif');
  assert.equal(css.fontSize, 30);
  assert.equal(css.fontWeight, 400);
  assert.equal(css.fontStyle, 'italic');
  assert.equal(css.color, 'rgba(51, 34, 17, 0.498)');
  assert.equal(css.WebkitTextStroke, '3px rgba(0, 0, 0, 1)');
  assert.equal(css.textShadow, '0.75px 0.75px 0 rgba(255, 0, 0, 0)');
});

test('preview: zero outline/shadow draw nothing; inherited source styling applies', () => {
  const s = style();
  assert.equal(previewCss(resolveEffective(s, draftFromStyle(s))).fontWeight, 700);
  const none = previewCss(resolveEffective(s, { ...draftFromStyle(s), outline: 0, shadow: 0 }));
  assert.equal(none.WebkitTextStroke, '0');
  assert.equal(none.textShadow, 'none');
});

test('preview scales outline/shadow with a clamped font size', () => {
  const s = style({ font_size: 5, outline: 1, shadow: 1 });
  const css = previewCss(resolveEffective(s, draftFromStyle(s)));
  assert.equal(css.fontSize, 10);
  assert.equal(css.WebkitTextStroke.startsWith('4px'), true); // 1 * (10/5) * 2
});
