type Band = 'HIGH' | 'MEDIUM' | 'LOW';

function t(key: string, options?: { count?: number }): string {
  return key + String(options?.count ?? '');
}

// A key reached only through indirection (a config object, not a literal
// t() argument): must still count as "used", never flagged unused.
const NAV_ITEM = { labelKey: 'app.indirect' };

function NavLabel({ item }: { item: { labelKey: string } }): string {
  return t(item.labelKey);
}

export function Widget({ band }: { band: Band }): string {
  // A plural key is written once; the catalogue holds its `_one` and `_other` forms.
  return `${t('app.title')} ${t(`status.band_${band}`)} ${NavLabel({ item: NAV_ITEM })} ${t('app.items', { count: 2 })}`;
}
