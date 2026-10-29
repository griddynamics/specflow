import type { ReactNode } from 'react';

declare const active: boolean;
declare const tags: string[];
declare const location: { pathname: string };

// ruleid: ds-no-hand-written-breadcrumb-nav
const crumbs = <nav aria-label="Breadcrumb">Home</nav>;
// ok: ds-no-hand-written-breadcrumb-nav
const mainNav = <nav aria-label="Main">Home</nav>;

// ruleid: ds-no-hand-written-breadcrumb-trail
const current = <span aria-current="page">Orders</span>;
// ok: ds-no-hand-written-breadcrumb-trail
const link = <a aria-current={active ? 'page' : undefined}>Orders</a>;

// ruleid: ds-no-breadcrumb-separator-chain
const separator = <li>/</li>;
// ok: ds-no-breadcrumb-separator-chain
const item = <li>Orders</li>;

interface HeaderProps {
  // ruleid: ds-no-breadcrumb-label-prop
  breadcrumb: string;
  // ok: ds-no-breadcrumb-label-prop
  breadcrumbSlot: ReactNode;
}

// ruleid: ds-no-pathname-label-sniffing
const onOrders = location.pathname.includes('/orders');
// ok: ds-no-pathname-label-sniffing
const tagged = tags.includes('new');

export type { HeaderProps };
export { crumbs, mainNav, current, link, separator, item, onOrders, tagged };
