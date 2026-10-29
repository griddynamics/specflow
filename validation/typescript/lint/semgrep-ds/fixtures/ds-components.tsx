// Semgrep reads this file as syntax only: the components it renders are not declared.
declare const orders: { id: string }[];

// ruleid: ds-no-local-pill-or-badge
function StatusPill() {
  return null;
}
// ok: ds-no-local-pill-or-badge
const orderStatus = <Pill value="open" />;

// ruleid: ds-no-primitive-name-shadow
const Button = () => null;
// ok: ds-no-primitive-name-shadow
const OrderRow = () => null;

// ruleid: ds-use-data-table-primitive
const table = <table>Rows</table>;
// ruleid: ds-use-data-table-primitive
const grid = <div role="grid">Rows</div>;
// ok: ds-use-data-table-primitive
const list = <ul>Rows</ul>;

// ruleid: ds-no-hand-rolled-empty-state
const emptyText = orders.length === 0 ? <p>No orders.</p> : <OrderList orders={orders} />;
// ok: ds-no-hand-rolled-empty-state
const emptyState = orders.length === 0 ? <EmptyState title="No orders." /> : <OrderList orders={orders} />;

// ruleid: ds-unconfigured-skeleton
const bareSkeleton = <Skeleton />;
// ok: ds-unconfigured-skeleton
const sizedSkeleton = <Skeleton height={120} />;

// ruleid: ds-summary-without-hit-target
const summary = <summary>More</summary>;
// ok: ds-summary-without-hit-target
const classedSummary = <summary className="touch-target">More</summary>;
// ruleid: ds-summary-hit-target-class-missing
const mutedSummary = <summary className="muted">More</summary>;
// ok: ds-summary-hit-target-class-missing
const targetSummary = <summary className="touch-target">More</summary>;

export {
  StatusPill,
  orderStatus,
  Button,
  OrderRow,
  table,
  grid,
  list,
  emptyText,
  emptyState,
  bareSkeleton,
  sizedSkeleton,
  summary,
  classedSummary,
  mutedSummary,
  targetSummary,
};
