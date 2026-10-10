import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  assToCssRgba,
  assToHexa,
  describeAssColour,
  formatAssColour,
  mergePickerValue,
  parseAssColour,
  parseColourInput,
} from '../src/utils/assColour.ts';

test('parses &HAABBGGRR& with inverted alpha and BGR order', () => {
  assert.deepEqual(parseAssColour('&H80112233&'), { a: 0x80, b: 0x11, g: 0x22, r: 0x33 });
  assert.deepEqual(parseAssColour('&h00FFFFFF'), { a: 0, b: 255, g: 255, r: 255 });
  assert.equal(parseAssColour('&H123&'), null);
  assert.equal(parseAssColour(null), null);
});

test('six-digit ASS colour is opaque', () => {
  assert.equal(formatAssColour(parseAssColour('&H0000FF&')!), '&H000000FF&');
});

test('ASS → #rrggbbaa flips alpha and swaps channels', () => {
  assert.equal(assToHexa('&H00112233&'), '#332211ff');
  assert.equal(assToHexa('&HFF112233&'), '#33221100'); // fully transparent
  assert.equal(assToHexa('&H80112233&'), '#3322117f');
});

test('every ASS alpha level survives ASS → hexa → ASS exactly', () => {
  for (let a = 0; a < 256; a++) {
    const ass = formatAssColour({ r: 12, g: 200, b: 99, a });
    assert.equal(parseColourInput(assToHexa(ass)), ass);
  }
});

test('typed colours: #RGB, #RRGGBB (opaque), #RRGGBBAA (CSS alpha)', () => {
  assert.equal(parseColourInput('#f00'), '&H000000FF&');
  assert.equal(parseColourInput('#ff0000'), '&H000000FF&');
  assert.equal(parseColourInput('00ff0080'), '&H7F00FF00&');
  assert.equal(parseColourInput('#12'), null);
  assert.equal(parseColourInput('nope'), null);
});

test('css rgba keeps alpha', () => {
  assert.equal(assToCssRgba('&H00FF0000&'), 'rgba(0, 0, 255, 1)');
  assert.equal(assToCssRgba('&HFF000000&'), 'rgba(0, 0, 0, 0)');
  assert.equal(assToCssRgba(null), 'rgba(255, 255, 255, 1)');
});

test('tooltip shows rgb, opacity and the raw ASS value', () => {
  assert.equal(describeAssColour('&H80112233&'), '#332211 · opacity 50% · &H80112233&');
});

test('picker RGB drag keeps the exact alpha (no 1-level drift)', () => {
  // ASS alpha 0x80 → css 0x7F (0.498); Mantine re-emits alpha rounded to 2 decimals → 0.5 → 0x80
  assert.equal(mergePickerValue('&H80112233&', '#aabbcc80'), '&H80CCBBAA&');
});

test('picker alpha drag keeps the exact RGB despite HSV rounding', () => {
  // blue 0x11 came back as 0x12 from the HSV round trip; only alpha really changed
  assert.equal(mergePickerValue('&H00112233&', '#33221280'), '&H7F112233&');
});

test('picker change of both channels applies both', () => {
  assert.equal(mergePickerValue('&H00112233&', '#ff000080'), '&H7F0000FF&');
});

test('an untouched picker value merges to the identical colour', () => {
  for (const c of ['&H80112233&', '&H00FFFFFF&', '&HFF000000&', '&H7F778899&', '&H91112233&']) {
    assert.equal(mergePickerValue(c, assToHexa(c)), c);
  }
});
