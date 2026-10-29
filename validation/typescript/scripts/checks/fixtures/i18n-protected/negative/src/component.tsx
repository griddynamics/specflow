type Kind = 'NEW' | 'SHIPPED';
type Band = 'HIGH' | 'MEDIUM';
type Level = 'HIGH' | 'LOW';

function t(key: string): string {
  return key;
}

export function Widget({ kind, band, level }: { kind: Kind; band: Band; level: Level }): string {
  return `${t(`orderState.${kind}`)} ${t(`status.band_${band}`)} ${t(`priority.${level}`)}`;
}
