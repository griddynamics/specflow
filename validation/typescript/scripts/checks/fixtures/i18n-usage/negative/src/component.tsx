type Band = 'HIGH' | 'MEDIUM' | 'LOW';

function t(key: string): string {
  return key;
}

export function Widget({ band }: { band: Band }): string {
  // "app.missing" does not exist in the catalogue; "status.band_LOW" (a
  // real Band member) has no catalogue key either. "app.unused" (in the
  // catalogue but never referenced) is the only genuinely unused key.
  return `${t('app.title')} ${t('app.missing')} ${t(`status.band_${band}`)}`;
}
