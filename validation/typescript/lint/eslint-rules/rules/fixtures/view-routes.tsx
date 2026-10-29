// Stand-in for `src/router.tsx` in typed RuleTester cases of `no-view-state-in-route`: the rule
// resolves each route's `component` through the checker. `view-page.tsx` is a route component
// that renders `Child` unconditionally and `Overlay` behind a condition; `view-lazy.tsx` is a
// lazily loaded route component; `view-orphan.tsx` is on no route. Each case replaces the source
// of one of them.
import { Page } from './view-page';

declare function createRoute(options: { component: unknown }): unknown;
declare function lazy<T>(load: () => Promise<{ default: T }>): T;

const Loaded = lazy(() => import('./view-lazy').then((m) => ({ default: m.LazyPage })));

export const direct = createRoute({ component: Page });
export const loaded = createRoute({ component: Loaded });
