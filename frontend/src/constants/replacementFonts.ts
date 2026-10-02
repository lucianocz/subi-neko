/**
 * Curated suggestions for the style editor's replacement-font field.
 * The field itself accepts any family name; these are only shortcuts.
 * No availability checking is done — see ProjectStylesDialog.
 */
export const COMMON_FONTS: readonly string[] = [
  'Arial',
  'Arial Narrow',
  'Calibri',
  'Segoe UI',
  'Tahoma',
  'Trebuchet MS',
  'Verdana',
  'Georgia',
  'Times New Roman',
  'Courier New',
  'Liberation Sans',
  'DejaVu Sans',
  'Noto Sans',
  'Open Sans',
  'Roboto',
];

export const EXTENDED_FONTS: readonly string[] = [
  'Arial Black',
  'Candara',
  'Cambria',
  'Consolas',
  'Constantia',
  'Corbel',
  'Franklin Gothic Medium',
  'Impact',
  'Lucida Sans Unicode',
  'Microsoft Sans Serif',
  'Palatino Linotype',
  'Book Antiqua',
  'Liberation Serif',
  'DejaVu Serif',
  'Noto Serif',
  'Ubuntu',
  'Lato',
  'Montserrat',
  'Raleway',
  'Oswald',
  'PT Sans',
  'PT Serif',
  'Source Sans 3',
  'Merriweather',
  'Roboto Condensed',
  'Roboto Mono',
];

export const REPLACEMENT_FONT_GROUPS = [
  { group: 'Common', items: [...COMMON_FONTS] },
  { group: 'Extended', items: EXTENDED_FONTS.filter((f) => !COMMON_FONTS.includes(f)) },
];

export const FONT_PREVIEW_LINES: readonly string[] = [
  'Příliš žluťoučký kůň úpěl ďábelské ódy.',
  'Ahoj, Leon! Co to sakra děláš?',
];
