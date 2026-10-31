"use client";
// Mission Control sources (plans/mission-control-sources/DESIGN.md D9, D11; owner redesign 2026-10-15; sources-ux SPECS §7.4). Rendered by
// MissionControlScreen with the production http adapter (GDRIVE plan 07). The page is the source cards; the header row, left of the
// project dropdown, holds the build button and "Add source" (HeaderActions). No knowledge bar, no item list, no README / Business / Architecture / Files views. A document link (Decisions,
// SpecBot: `?view=files&file=`) opens that one document with a Back link.
//
// A card opens its details modal (FR-SRC-0005); Remove source opens a confirmation over it (FR-SRC-0014/0015) and the details stay mounted
// under it. Files are uploaded only from Add source -> Local files; each pick is one new batch (FR-SRC-0020).
//
// Build knowledge (plans/connect-build-kg-button): a click opens the confirm dialog with the reasoning-level slider; its confirm starts the
// build. A failed build turns the button into Error, which opens the error details (closed again when the failure clears).
//
// Data: one status read, read again on every change the port reports (`knowledge.subscribe`). A failed read is an error, never
// "empty" (D9). Leaving the screen stops only the listener: sync and build are server work.
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { FilesView } from "@/components/files/FilesView";
import { ShellHeader } from "@/components/shell/ShellHeader";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/ProgressBar";
import { PageTitle } from "@/components/ui/TopBar";
import { useToast } from "@/components/ui/Toast";
import type { KnowledgePort, ProjectSourcesStatus, Source, SourcesPort } from "@/lib/domain/sources";
import { parseMissionControlQuery } from "@/lib/routes";
import { HeaderActions } from "./HeaderActions";
import { buildButtonView } from "./kb-logic";
import { ScreenDialogs, type Dialog } from "./ScreenDialogs";
import { goneToast, removedToast } from "./source-logic";
import { SourcesSection } from "./SourcesSection";
import { isGone } from "./source-files-reader";
import { useBuild, useFailToast, useKnowledgeBuiltToast, useUpload } from "./use-sources-actions";
import { useDriveEmail, useMemberId, useSourcesRoutes, useSourcesStatus } from "./use-sources-data";
import "./mc-sources.css";
import { truthy } from "@/components/ui/utils";
import { COPY } from "@/lib/copy";

/** The page: the error, the spinner, one document with its Back link, or the source cards. */
function SourcesBody({
  status,
  failed,
  doc,
  section,
  projectId,
  github,
  onOpen,
  onBack,
}: Readonly<{
  status: ProjectSourcesStatus | null;
  failed: boolean;
  doc: string | null;
  section: ReturnType<typeof parseMissionControlQuery>["section"];
  projectId: string;
  github: boolean;
  onOpen: (sid: string) => void;
  onBack: () => void;
}>) {
  if (!status) {
    if (failed) {
      return (
        <p className="body mcs-error" role="alert" data-testid="mcs-error">
          Mission Control could not be loaded. Reload the page to try again.
        </p>
      );
    }
    return (
      <div className="mc-scene" data-testid="mc-loading">
        <Spinner label={COPY.loadingMissionControl} />
      </div>
    );
  }
  if (truthy(doc)) {
    return (
      <div className="mcs-doc" data-testid="mcs-doc">
        <Button variant="plain" size="sm" className="mcs-doc__back" onClick={onBack} data-testid="mcs-doc-back">
          Back to sources
        </Button>
        <FilesView projectId={projectId} file={doc} section={section} treeless />
      </div>
    );
  }
  return <SourcesSection status={status} onOpen={onOpen} github={github} />;
}

export function SourcesMissionControl({
  projectId,
  sources,
  knowledge,
}: Readonly<{
  projectId: string;
  sources: SourcesPort;
  knowledge: KnowledgePort;
}>) {
  const routes = useSourcesRoutes();
  const query = useSearchParams();
  const toast = useToast();
  const { status, failed, reload } = useSourcesStatus(knowledge, projectId);
  const [dialog, setDialog] = useState<Dialog>(null);
  // From Confirm until the next status after the POST settled: "posting" while it is in flight, then the status shown when it
  // settled (the next, different status ends it). The button is busy meanwhile, so it never looks clickable after Confirm.
  const [pending, setPending] = useState<"posting" | ProjectSourcesStatus | null>(null);
  const latest = useRef<ProjectSourcesStatus | null>(null);
  const driveEmail = useDriveEmail(sources);
  const me = useMemberId();
  const screen = useRef<HTMLElement>(null);
  const title = useRef<HTMLHeadingElement>(null);
  const picker = useRef<HTMLInputElement>(null);
  const addSourceButton = useRef<HTMLButtonElement>(null);
  // Sources this member is removing, or whose removal was announced already: their disappearing is not "removed by someone else".
  const settled = useRef(new Set<string>());
  const known = useRef(new Map<string, Source>());

  // The control the focus goes back to (NFR-USE-0003.AC2, R23).
  const focusAddSource = useCallback(() => {
    requestAnimationFrame(() => {
      addSourceButton.current?.focus();
    });
  }, []);
  const focusCard = (sid: string) => {
    requestAnimationFrame(() => {
      screen.current
        ?.querySelector<HTMLElement>(`[data-testid=source-card][data-id=${JSON.stringify(sid)}] [data-testid=source-open]`)
        ?.focus();
    });
  };

  const fail = useFailToast();
  useKnowledgeBuiltToast(status);
  const build = useBuild(knowledge, projectId);
  const upload = useUpload(sources, projectId, fail, reload);
  const pickFiles = () => picker.current?.click();

  // FR-SRC-0018/0019: success = the answer, or "already gone" (another member removed it first).
  const removeSource = async (src: Source) => {
    settled.current.add(src.id);
    try {
      await sources.remove(projectId, src.id);
    } catch (e) {
      if (!isGone(e)) {
        settled.current.delete(src.id); // the dialog says so and stays open; the source is still there
        throw e;
      }
    }
    setDialog(null);
    toast(removedToast(src));
    reload.current();
    focusAddSource(); // the card is gone: the focus would fall to <body>
  };

  // FR-SRC-0025.AC3: the open details of a source that someone else removed.
  const removedElsewhere = useCallback(
    (src: Source) => {
      if (settled.current.has(src.id)) return;
      settled.current.add(src.id);
      setDialog((d) => (d && (d.kind === "details" || d.kind === "remove") && d.sid === src.id ? null : d));
      toast(goneToast(src));
      reload.current();
      focusAddSource();
    },
    [toast, reload, focusAddSource],
  );

  async function addLink(url: string) {
    const r = await sources.add(projectId, { kind: "github", url });
    if (!r.created)
      toast({ title: "Already connected", message: `${r.connection.label} is already a source of this project.`, tone: "info" });
  }

  const q = useMemo(() => new URLSearchParams(query.toString()), [query]);
  const parsed = useMemo(() => parseMissionControlQuery(q), [q]);
  const doc = parsed.view === "files" && truthy(parsed.file) ? parsed.file : null; // a document link (Decisions, SpecBot citations)
  // The live region says the state without the ellipsis ("Building…" -> "Building"); the details are in the button's description.
  const buttonLabel = status ? buildButtonView(status, new Date(), pending !== null).label : "";
  const label = buttonLabel.endsWith("…") ? buttonLabel.slice(0, -1) : buttonLabel;

  // The error details show only while a failure is on the status: a poll that clears it closes them (the focus returns to the button).
  if (dialog?.kind === "buildError" && status && !status.buildFailure) setDialog(null);

  const detailsSid = dialog?.kind === "details" || dialog?.kind === "remove" ? dialog.sid : null;
  const detailsSource = truthy(detailsSid) ? status?.sources.find((x) => x.id === detailsSid) : undefined;
  useEffect(() => {
    latest.current = status;
  }, [status]);
  if (pending !== null && pending !== "posting" && status !== pending) setPending(null);
  useEffect(() => {
    for (const src of status?.sources ?? []) known.current.set(src.id, src);
  }, [status]);
  // The status read can notice the removal before the modal's own read does.
  useEffect(() => {
    if (!status || !truthy(detailsSid) || status.sources.some((x) => x.id === detailsSid)) return;
    const last = known.current.get(detailsSid);
    if (last) removedElsewhere(last);
  }, [status, detailsSid, removedElsewhere]);

  return (
    <section
      ref={screen}
      className="mc-screen mcs-screen"
      data-testid="screen-mission-control"
      data-project={projectId}
      data-mode="sources"
      data-phase={status?.phase ?? (failed ? "error" : "loading")}
      data-ready={status?.ready ?? false}
    >
      <PageTitle ref={title} className="title-1 mc-sr mc-title" tabIndex={-1}>
        Mission Control
      </PageTitle>
      {/* Polite: says the button's state when it changes. */}
      <p className="mc-sr" role="status" data-testid="mcs-announce">
        {label ? `Knowledge: ${label}.` : ""}
      </p>
      {status ? (
        <ShellHeader
          tabs={
            <HeaderActions
              status={status}
              onOpenBuild={(mode) => {
                setDialog({ kind: "build", mode });
              }}
              pending={pending !== null}
              onError={() => {
                setDialog({ kind: "buildError" });
              }}
              onAdd={(source) => {
                setDialog(source === "gdrive" ? { kind: "drive" } : { kind: "add" });
              }}
              onPickFiles={pickFiles}
              github={sources.githubEnabled}
              pdfOnly={sources.pdfOnly}
              addSourceRef={addSourceButton}
            />
          }
        />
      ) : null}
      <input
        ref={picker}
        type="file"
        multiple
        hidden
        accept={sources.pdfOnly ? "application/pdf,.pdf" : undefined}
        data-testid="mcs-upload-input"
        aria-label="Upload local files"
        onChange={(e) => {
          const files = Array.from(e.target.files ?? []);
          e.target.value = ""; // the same file can be picked again
          if (files.length) void upload(files);
        }}
      />
      <SourcesBody
        status={status}
        failed={failed}
        doc={doc}
        section={parsed.section}
        projectId={projectId}
        github={sources.githubEnabled}
        onOpen={(sid) => {
          setDialog({ kind: "details", sid });
        }}
        onBack={() => {
          window.history.pushState(null, "", routes.missionControl(projectId));
          title.current?.focus(); // the clicked control disappears with the document
        }}
      />
      <ScreenDialogs
        dialog={dialog}
        onDialog={setDialog}
        projectId={projectId}
        sources={sources}
        status={status}
        detailsSource={detailsSource}
        driveEmail={driveEmail}
        me={me}
        toast={toast}
        onAddLink={addLink}
        onRemove={removeSource}
        onRemovedElsewhere={removedElsewhere}
        onCloseDetails={focusCard}
        onFail={fail}
        onReload={() => {
          reload.current();
        }}
        onBuild={(level) => {
          setPending("posting");
          void build(level).then(() => {
            setPending(latest.current); // a rejection shows as the Error button in the next status
          });
        }}
      />
    </section>
  );
}
