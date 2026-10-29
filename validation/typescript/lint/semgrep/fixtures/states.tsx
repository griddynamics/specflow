declare const Outlet: () => null;
declare const ErrorBanner: () => null;
declare const Spinner: () => null;
declare const Icon: (props: { name: string }) => null;
declare function useOrders(): { isError: boolean; isLoading: boolean; error: Error };

function OrdersLayout() {
  const { isError } = useOrders();
  // ruleid: states-route-layout-blanks-on-error
  if (isError) { return <ErrorBanner />; }
  return <div><Outlet /></div>;
}

function SafeLayout() {
  const { isError } = useOrders();
  // ok: states-route-layout-blanks-on-error
  return <div>{isError && <ErrorBanner />}<Outlet /></div>;
}

function OrderError() {
  const { error } = useOrders();
  // ruleid: states-error-text-without-alert-role
  const plain = <p>{error.message}</p>;
  // ok: states-error-text-without-alert-role
  const announced = <p role="alert">{error.message}</p>;
  return <>{plain}{announced}</>;
}

// ruleid: states-raw-status-glyph
const warningIcon = '⚠';
// ok: states-raw-status-glyph
const warningLabel = <span><Icon name="warning" /> Failed</span>;

function OrderList() {
  const { isLoading } = useOrders();
  // ruleid: states-loading-branch-missing-aria-busy
  const waiting = isLoading && <div>…</div>;
  // ok: states-loading-branch-missing-aria-busy
  const busy = isLoading && <div aria-busy="true">…</div>;
  // ok: states-loading-branch-missing-aria-busy
  const spinner = isLoading && <Spinner />;
  return <>{waiting}{busy}{spinner}</>;
}

function OrderPanel() {
  const { isLoading } = useOrders();
  // ruleid: states-loading-return-missing-aria-busy
  if (isLoading) { return <section>…</section>; }
  return <section>done</section>;
}

function OrderPanelBusy() {
  const { isLoading } = useOrders();
  // ok: states-loading-return-missing-aria-busy
  if (isLoading) { return <section aria-busy="true">…</section>; }
  return <section>done</section>;
}

export {
  OrdersLayout,
  SafeLayout,
  OrderError,
  warningIcon,
  warningLabel,
  OrderList,
  OrderPanel,
  OrderPanelBusy,
};
