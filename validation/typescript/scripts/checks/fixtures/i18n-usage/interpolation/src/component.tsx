function t(key: string, options?: object): string {
  return key + String(options);
}

interface Named {
  readonly label: string;
}

export function Widget({ named, loose }: { named: Named; loose: Record<string, string> }): string {
  const asVariable = { other: 'x' };
  return [
    // Passes its placeholder: no finding.
    t('merge.confirm', { label: 'Item' }),
    t('merge.unescaped', { name: 'Item' }),
    t('merge.formatted', { value: 3 }),
    t('merge.items', { count: 2 }),
    t('merge.plain'),
    // The checker reads a spread or a reference by its type: no finding.
    t('merge.confirm', { ...named }),
    t('merge.confirm', named),
    // An index signature cannot be judged: no finding.
    t('merge.confirm', loose),
    // Each of these leaves a placeholder unresolved: three findings.
    t('merge.confirm'),
    t('merge.items', { label: 'x' }),
    t('merge.unescaped', asVariable),
  ].join(' ');
}
