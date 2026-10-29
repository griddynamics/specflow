"use client";
// FE-CON-01..03, FE-DEMO-02.AC1 (PLAN a.10, MOCKS §9, TECH-SPECS §1.1): the Connections screen (organization route, no project in
// the path). The path, the export name, no props and the `screen-connections` hook of the screen are bound by the page file and by the
// shell navigation. Content comes only through `ports.connectors`.
// No connector action is on the demo path: every card action shows "Not available in demo"; "+ New connection" shows "Coming soon".
// Layout: in the shell's header row (ShellHeader), the category tab bar, then the tools (search, "+ New connection" at the end),
// which join the tabs on wide screens and wrap under them otherwise. "+ New connection" is a glass button: the top bar's "New" is
// the view's one yellow action (DS rule 4), and on wide screens the two sit side by side. The count line stays above the grid. The h1 is visually
// hidden: the sidebar names the page.
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ShellHeader } from "@/components/shell/ShellHeader";
import { useNotAvailable } from "@/components/shell/use-not-available";
import { Button } from "@/components/ui/Button";
import { ConnectorCard } from "@/components/ui/ConnectorCard";
import { EmptyState } from "@/components/ui/EmptyState";
import { Icon } from "@/components/ui/Icon";
import { SearchField } from "@/components/ui/SearchField";
import { SegmentedControl } from "@/components/ui/SegmentedControl";
import { useToast } from "@/components/ui/Toast";
import { usePorts } from "@/lib/data/provider";
import type { Connector } from "@/lib/domain/connectors";
import { ALL_CATEGORY, actionLabel, filterConnectors } from "./con-logic";
import { logoSrc } from "./logos";
import "./con-gaps.css";
import "@/components/ui/ui-gaps-app.css";
import { COPY } from "@/lib/copy";
import { PageTitle } from "@/components/ui/TopBar";

function screenState(failed: boolean, items: Connector[] | null): "error" | "ready" | "loading" {
  if (failed) return "error";
  return items === null ? "loading" : "ready";
}

export function ConnectionsScreen() {
  const { connectors } = usePorts();
  const toast = useToast();
  const notAvailable = useNotAvailable();
  const [items, setItems] = useState<Connector[] | null>(null);
  const [categories, setCategories] = useState<string[]>([ALL_CATEGORY]);
  const [failed, setFailed] = useState(false);
  const [category, setCategory] = useState(ALL_CATEGORY);
  const [query, setQuery] = useState("");

  useEffect(() => {
    let live = true;
    Promise.all([connectors.list(), connectors.categories()]).then(
      ([list, cats]) => {
        if (!live) return;
        setItems(list);
        setCategories(cats);
      },
      () => {
        if (live) setFailed(true);
      },
    );
    return () => {
      live = false;
    };
  }, [connectors]);

  const shown = useMemo(() => (items ? filterConnectors(items, category, query) : []), [items, category, query]);
  const options = useMemo(() => categories.map((c) => ({ value: c, label: c })), [categories]);
  const tools = useRef<HTMLDivElement>(null); // in the header row (a portal), not inside the screen's <section>
  const clear = () => {
    setCategory(ALL_CATEGORY);
    setQuery("");
    // the empty state unmounts with the filters: keep the focus in the page (the search field)
    tools.current?.querySelector<HTMLInputElement>(".gdg-search input")?.focus();
  };

  const state = screenState(failed, items);

  let body: ReactNode;
  if (failed) {
    body = (
      <EmptyState icon="warning-circle" title="Connections could not be loaded">
        Reload the page to try again.
      </EmptyState>
    );
  } else if (items !== null && shown.length === 0) {
    body = (
      <div className="con-empty" data-testid="con-empty">
        <EmptyState
          icon="magnifying-glass"
          title="No connectors found"
          actions={
            <Button variant="glass" size="sm" onClick={clear}>
              Clear filters
            </Button>
          }
        >
          Try another name or category.
        </EmptyState>
      </div>
    );
  } else {
    body = (
      <ul className="con-grid" aria-label="Connectors" data-testid="con-grid">
        {shown.map((c) => (
          <li key={c.id} data-testid="con-card" data-connector={c.id} data-status={c.status}>
            <ConnectorCard
              name={c.name}
              category={c.category}
              description={c.description}
              status={c.status}
              logoSrc={logoSrc(c.logoSlug)}
              actionLabel={actionLabel(c.status)}
              onAction={notAvailable}
            />
          </li>
        ))}
      </ul>
    );
  }

  return (
    <section className="con-screen" data-testid="screen-connections" data-state={state}>
      <PageTitle className="shell-sr">Connections</PageTitle>
      <ShellHeader
        tabs={<SegmentedControl label="Connector category" options={options} value={category} onChange={setCategory} />}
        tools={
          <div ref={tools} className="con-tools">
            <SearchField value={query} onChange={setQuery} placeholder={COPY.searchConnectors} />
            <Button
              variant="glass"
              className="con-new"
              onClick={() => {
                toast({ title: "Coming soon", tone: "info" });
              }}
            >
              <Icon name="plus" size={16} /> New connection
            </Button>
          </div>
        }
      />
      {items ? (
        <p className="con-count body-sm" role="status" data-testid="con-count">
          {shown.length} of {items.length} connectors
        </p>
      ) : null}
      {body}
    </section>
  );
}
