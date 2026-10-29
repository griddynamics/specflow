declare const order: { orderId: string; total: number };
declare const item: { headline: string; note: string };
declare const headline: string;

// ruleid: ds-no-h1-outside-page-header
const title = <h1>Orders</h1>;
// ok: ds-no-h1-outside-page-header
const subtitle = <h2>Orders</h2>;

// ruleid: ds-no-hardcoded-heading-font-size
const sized = <h2 style={{ fontSize: 18 }}>Orders</h2>;
// ok: ds-no-hardcoded-heading-font-size
const spaced = <h2 style={{ marginTop: 0 }}>Orders</h2>;

// ruleid: ds-no-hardcoded-header-style-font-size
const sectionTitleStyle = { fontWeight: 600, fontSize: 18 };
// ok: ds-no-hardcoded-header-style-font-size
const cardStyle = { fontWeight: 600, fontSize: 14 };

// ruleid: ds-no-duplicate-header-component
function OrdersPageHeader() {
  return null;
}
// ok: ds-no-duplicate-header-component
function OrdersSummary() {
  return null;
}

// ruleid: ds-fluid-type-at-call-site
const heroStyle = { fontSize: 'clamp(1.5rem, 4vw, 2rem)' };
// ok: ds-fluid-type-at-call-site
const classedHeroStyle = { fontSize: 'var(--type-display)' };

// ruleid: ds-type-role-font-size-at-call-site
const sizedHeadline = <p style={{ fontSize: 18 }}>{item.headline}</p>;
// ruleid: ds-type-role-font-size-at-call-site
const bareHeadline = <p style={{ font: 'inherit' }}>{headline}</p>;
// ok: ds-type-role-font-size-at-call-site
const classedHeadline = <p className="headline">{item.headline}</p>;
// ok: ds-type-role-font-size-at-call-site
const sizedNote = <p style={{ fontSize: 18 }}>{item.note}</p>;

// ruleid: ds-monospace-outside-the-mono-token
const codeStyle = { fontFamily: 'ui-monospace, monospace' };
// ok: ds-monospace-outside-the-mono-token
const bodyStyle = { fontFamily: 'var(--font-mono)' };

// ruleid: ds-identifier-rendered-without-monospace
const plainId = <span>{order.orderId}</span>;
// ok: ds-identifier-rendered-without-monospace
const monoId = <span className="mono">{order.orderId}</span>;
// ok: ds-identifier-rendered-without-monospace
const codeId = <code>{order.orderId}</code>;
// ok: ds-identifier-rendered-without-monospace
const total = <span>{order.total}</span>;

export {
  title,
  subtitle,
  sized,
  spaced,
  sectionTitleStyle,
  cardStyle,
  OrdersPageHeader,
  OrdersSummary,
  heroStyle,
  classedHeroStyle,
  sizedHeadline,
  bareHeadline,
  classedHeadline,
  sizedNote,
  codeStyle,
  bodyStyle,
  plainId,
  monoId,
  codeId,
  total,
};
