export function severityOf(rules, id) {
  const entry = rules[id];
  if (entry === undefined) return undefined;
  const level = Array.isArray(entry) ? entry[0] : entry;
  return level === 2 || level === 'error'
    ? 'error'
    : level === 1 || level === 'warn'
      ? 'warn'
      : 'off';
}

export function optionsOf(rules, id) {
  const entry = rules[id];
  return Array.isArray(entry) ? entry.slice(1) : [];
}

export function selectorsOf(rules, id) {
  return optionsOf(rules, id).map((o) => o.selector);
}
