type Kind = 'NEW' | 'SHIPPED';
type Band = 'HIGH' | 'MEDIUM';

function t(key: string): string {
  return key;
}

export function Widget({ kind, band }: { kind: Kind; band: Band }): string {
  return `${t(`orderState.${kind}`)} ${t(`status.band_${band}`)}`;
}
