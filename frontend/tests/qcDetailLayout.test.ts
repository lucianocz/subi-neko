import assert from 'node:assert/strict';
import { test } from 'node:test';
import { GENDER_COLORS, NON_BINARY_BADGE_STYLE } from '../src/utils/gender.ts';
import type { QcEventDetail } from '../src/types/qc.ts';

test('shared gender styling keeps the subtitle-editor palette', () => {
  assert.deepEqual(GENDER_COLORS, { female: 'pink', male: 'blue', non_binary: 'gray', other: 'gray' });
  assert.equal(NON_BINARY_BADGE_STYLE.color, '#b7791f');
});

test('QC detail DTO type carries character / gender fields', () => {
  const keys: (keyof QcEventDetail)[] = ['character_name', 'character_gender', 'speaker_gender', 'speaker'];
  assert.equal(keys.length, 4);
});
