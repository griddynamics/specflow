export const CARD_STYLE = {
  background: 'var(--background)',
  // `--success-muted` is declared nowhere: this renders transparent.
  borderColor: 'var(--success-muted)',
  // A fallback is an author's visible decision, not a silent nothing.
  gridTemplateColumns: 'repeat(var(--control-columns, 1), minmax(0, 1fr))',
};
