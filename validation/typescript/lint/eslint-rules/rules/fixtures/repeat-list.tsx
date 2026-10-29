// Fixed on-disk consumer for the cross-file case of row-action-names-row and for the lib/ tests:
// `PairRow` (repeat-row.tsx) is rendered once per pair here.
import { PairRow } from './repeat-row';

declare const pairs: { id: string; label: string }[];

export const PairList = () => (
  <ul>
    {pairs.map((pair) => (
      <PairRow key={pair.id} pair={pair} />
    ))}
  </ul>
);
