"use client";

import { useSyncExternalStore, type JSX } from "react";
import { GD_URL } from "../lib/links";

const SOCIAL = [
  ["LinkedIn", "https://www.linkedin.com/company/grid-dynamics/"],
  ["YouTube", "https://www.youtube.com/@griddynamicsengineeringcom2006"],
  ["Facebook", "https://www.facebook.com/griddynamicsglobal"],
  ["Instagram", "https://www.instagram.com/griddynamics_global/"],
];

const LEGAL = [
  ["Privacy Policy", "https://www.griddynamics.com/privacy"],
  ["Terms of Use", "https://www.griddynamics.com/terms-and-conditions"],
  ["Cookie List", "https://www.griddynamics.com/cookie-list"],
];

/** The year is the visitor's, not the build's: a static page is built once and served for years. */
const noSubscription = () => () => undefined;
const thisYear = () => new Date().getFullYear();

function CurrentYear(): JSX.Element {
  // The server snapshot is the year of the build; the browser swaps in its own after hydration.
  const year = useSyncExternalStore(noSubscription, thisYear, thisYear);
  return <span>{year}</span>;
}

function External({ href, children }: Readonly<{ href: string; children: string }>): JSX.Element {
  return (
    <a href={href} target="_blank" rel="noopener">
      {children}
    </a>
  );
}

export function SiteFooter(): JSX.Element {
  return (
    <footer className="site-footer">
      <div className="wrap">
        <div className="foot-cols">
          <div className="foot-brand">
            <a className="gx-brand" href="#top">
              Graphico
            </a>
            <p>One shared knowledge base for agentic delivery.</p>
            <p>
              Built by <External href={GD_URL}>Grid Dynamics</External>.
            </p>
          </div>
          <nav className="foot-col" aria-label="Grid Dynamics">
            <p className="gx-eyebrow">Grid Dynamics</p>
            <External href={GD_URL}>griddynamics.com</External>
            {SOCIAL.map(([name, href]) => (
              <External key={name} href={href}>
                {name}
              </External>
            ))}
          </nav>
          <div className="foot-col">
            <p className="gx-eyebrow">Contact</p>
            <External href="https://www.griddynamics.com/contact">Contact us</External>
            <a href="tel:+16505235000">+1 (650) 523-5000</a>
            <address>
              6101 Bollinger Canyon Road
              <br />
              Suite 465
              <br />
              San Ramon, CA 94583
            </address>
          </div>
        </div>
        <div className="foot-legal">
          <p>
            Copyright © <CurrentYear /> Grid Dynamics Holdings, Inc. All rights reserved.
          </p>
          <nav aria-label="Legal">
            {LEGAL.map(([name, href]) => (
              <External key={name} href={href}>
                {name}
              </External>
            ))}
          </nav>
        </div>
      </div>
    </footer>
  );
}
