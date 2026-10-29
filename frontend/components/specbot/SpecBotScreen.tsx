"use client";
// FE-BOT-01, FE-BOT-02, FE-BOT-03, FE-DEMO-09, FE-DS-08, FE-REV-01.AC4 (PLAN a "SpecBot"; TECH-SPECS §1.5, §2.11; MOCKS §7).
// Starts empty every time it opens (local state only): greeting from the assistant port, the composer centred with three
// suggested prompts. After the first message the composer pins to the bottom. Leaving, Reset demo and a new question abort the
// running `ask()`, so a late answer cannot appear. The text from "Ask SpecBot" in Files is read once and only pre-fills the composer.
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Avatar } from "@/components/ui/Avatar";
import { ChatComposer } from "@/components/ui/ChatComposer";
import { ChatMessage } from "@/components/ui/ChatMessage";
import { GlassSurface } from "@/components/ui/GlassSurface";
import { Icon } from "@/components/ui/Icon";
import { MarkdownProse } from "@/components/ui/MarkdownProse";
import { Spinner } from "@/components/ui/ProgressBar";
import { focusedElement, focusIsLost } from "@/components/ui/use-outside";
import { PageTitle } from "@/components/ui/TopBar";
import type { AssistantIntro } from "@/lib/domain/assistant";
import { docRefLink } from "@/lib/markdown/links";
import { decisionLink } from "@/components/decisions/decision-links";
import { applyEvent, failExchange, nearBottom, newExchange, plainText, type Exchange } from "./specbot-logic";
import "./bot-gaps.css";
import { useSpecBotEnv } from "./use-specbot-env";
import { truthy } from "@/components/ui/utils";
import { COPY } from "@/lib/copy";

const USER_MESSAGE = { role: "user" } as const;
const ASSISTANT_MESSAGE = { role: "assistant" } as const;

/** The empty state: the greeting and the composer, the "not available" note, or the loading spinner. */
function Hello({ intro, failed, composer }: Readonly<{ intro: AssistantIntro | null; failed: boolean; composer: ReactNode }>) {
  if (intro) {
    return (
      <>
        <GlassSurface variant="strong" className="bot__hello">
          <Avatar name="SpecBot" kind="bot" size="lg" />
          <PageTitle className="title-1 bot__title" data-testid="specbot-greeting">
            {intro.greeting}
          </PageTitle>
          <p className="body-lg bot__sub">{intro.subline}</p>
        </GlassSurface>
        {composer}
      </>
    );
  }
  if (failed) {
    return (
      <GlassSurface variant="strong" className="bot__hello" role="alert">
        <PageTitle className="title-1 bot__title">SpecBot</PageTitle>
        <p className="body-lg bot__sub">SpecBot is not available right now. Please reload the page.</p>
      </GlassSurface>
    );
  }
  return (
    <GlassSurface variant="strong" className="bot__hello" data-testid="specbot-loading" role="status" aria-label={COPY.loadingCaption}>
      <Spinner size={24} />
    </GlassSurface>
  );
}

export function SpecBotScreen({ projectId }: Readonly<{ projectId: string }>) {
  const { assistant, handoff, routes } = useSpecBotEnv();
  const router = useRouter();
  const [intro, setIntro] = useState<AssistantIntro | null>(null);
  const [introFailed, setIntroFailed] = useState(false);
  const [exchanges, setExchanges] = useState<Exchange[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [prefilled, setPrefilled] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const counter = useRef(0);
  const prefillTaken = useRef(false);
  const root = useRef<HTMLElement | null>(null);
  const endRef = useRef<HTMLDivElement | null>(null);

  const focusComposer = useCallback((caretAtEnd: boolean) => {
    const box = root.current?.querySelector<HTMLTextAreaElement>(".gdg-composer textarea");
    if (!box) return;
    box.focus();
    if (caretAtEnd) box.setSelectionRange(box.value.length, box.value.length);
  }, []);

  // The intro (greeting with the first name and the project name).
  useEffect(() => {
    let live = true;
    assistant.intro(projectId).then(
      (i) => {
        if (live) setIntro(i);
      },
      () => {
        if (live) setIntroFailed(true);
      },
    );
    return () => {
      live = false;
    };
  }, [assistant, projectId]);

  // "Ask SpecBot" hand-over (§1.5): read once (the ref survives a Strict Mode re-run), only for this project, never sent.
  // The page remounts this screen for every [projectId] (the shell gate), so one read per mount is one read per project.
  // A slot left for ANOTHER project is dropped: the port only clears a slot it returns, so an empty text for this project is
  // written and taken, which clears the slot (a stale selection must not pre-fill a later visit).
  useEffect(() => {
    if (prefillTaken.current) return;
    prefillTaken.current = true;
    let text = handoff.takeSpecBotPrefill(projectId);
    if (text === null) {
      handoff.setSpecBotPrefill(projectId, "");
      handoff.takeSpecBotPrefill(projectId);
    }
    if (text === "") text = null;
    if (text !== null)
      void Promise.resolve().then(() => {
        setDraft(text);
        setPrefilled(true);
      });
  }, [handoff, projectId]);

  // Focus the composer once it exists; with a pre-fill the caret goes to the end.
  const focused = useRef(false);
  useEffect(() => {
    if (!intro || !prefilled || focused.current) return;
    focused.current = true;
    requestAnimationFrame(() => {
      focusComposer(true);
    });
  }, [intro, prefilled, focusComposer]);

  // Leaving the screen aborts the running ask (its timers are cleared by the port).
  useEffect(() => () => abortRef.current?.abort(), []);

  // Follow the conversation only while the user is already near the bottom (a user who scrolled up is not pulled back).
  const following = useRef(true);
  // P15 (QA M2): the conversation scrolls inside its own region (the screen fits the shell), so the scroll position is the region's.
  const scroller = useRef<HTMLDivElement>(null);
  const hasLog = exchanges.length > 0;
  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const onScroll = () => {
      following.current = nearBottom(el.scrollHeight, el.scrollTop, el.clientHeight);
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      el.removeEventListener("scroll", onScroll);
    };
  }, [hasLog]);
  useEffect(() => {
    if (exchanges.length && following.current) endRef.current?.scrollIntoView({ block: "end" });
  }, [exchanges]);

  const patch = (id: number, fn: (x: Exchange) => Exchange) => {
    setExchanges((xs) => xs.map((x) => (x.id === id ? fn(x) : x)));
  };

  async function send(text: string) {
    const question = text.trim();
    if (!question || busy) return;
    abortRef.current?.abort();
    const ac = new AbortController();
    abortRef.current = ac;
    const id = ++counter.current;
    setExchanges((xs) => [...xs, newExchange(id, question)]);
    setDraft("");
    setBusy(true);
    try {
      for await (const event of assistant.ask(projectId, question, ac.signal)) {
        if (ac.signal.aborted) return;
        patch(id, (x) => applyEvent(x, event));
      }
    } catch {
      if (!ac.signal.aborted) patch(id, failExchange);
    } finally {
      if (!ac.signal.aborted) {
        setBusy(false);
        // Refocus only when the focus was lost with the disabled box (body) or is still in the composer.
        requestAnimationFrame(() => {
          if (focusIsLost() || truthy(root.current?.querySelector(".gdg-composer")?.contains(focusedElement()))) focusComposer(false);
        });
      }
    }
  }

  const open = (href: string) => (e: React.MouseEvent) => {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return; // new tab keeps working
    e.preventDefault();
    router.push(href);
  };

  const started = exchanges.length > 0;
  const composer = (
    <ChatComposer
      value={draft}
      onChange={setDraft}
      onSend={(text) => {
        void send(text);
      }}
      placeholder={intro?.placeholder}
      suggestions={started || !truthy(intro?.suggestions.length) ? undefined : intro.suggestions}
      onSuggestion={(s) => void send(s)}
      disabled={busy}
      label="Ask SpecBot"
    />
  );

  return (
    <section
      ref={root}
      className="bot gdg-scene"
      data-testid="screen-specbot"
      data-project={projectId}
      data-state={started ? "chat" : "empty"}
    >
      {!started ? (
        <div className="bot__empty" data-testid="specbot-empty">
          <Hello intro={intro} failed={introFailed} composer={composer} />
        </div>
      ) : (
        <>
          <PageTitle className="bot__sr">SpecBot</PageTitle>
          <div className="bot__scroll" ref={scroller} data-testid="specbot-scroll">
            <div className="bot__log" role="region" aria-label="Conversation" data-testid="specbot-log">
              {exchanges.map((x) => (
                <div key={x.id} className="bot__exchange" data-testid="specbot-exchange">
                  <ChatMessage {...USER_MESSAGE} name="You">
                    {x.question}
                  </ChatMessage>
                  <div
                    className={`bot__msg ${x.answer === null && !x.typing ? "bot__msg--working" : ""}`}
                    data-testid="specbot-answer"
                    data-done={x.done}
                  >
                    <ChatMessage
                      {...ASSISTANT_MESSAGE}
                      steps={x.steps.length ? x.steps.map((s) => ({ label: s.label, status: s.status })) : undefined}
                      typing={x.typing}
                      citations={x.citations.map((c) => ({
                        label: c.label,
                        onClick: () => {
                          router.push(docRefLink(routes, projectId, c));
                        },
                      }))}
                    >
                      {x.answer === null ? null : <MarkdownProse source={x.answer} />}
                    </ChatMessage>
                    {x.decisionLink ? (
                      <a
                        className="bot__decision body-sm"
                        data-testid="specbot-decision-link"
                        href={decisionLink(routes, projectId, x.decisionLink.decisionId)}
                        onClick={open(decisionLink(routes, projectId, x.decisionLink.decisionId))}
                      >
                        <Icon name="arrow-square-out" size={14} />
                        {x.decisionLink.label}
                      </a>
                    ) : null}
                  </div>
                </div>
              ))}
              <div ref={endRef} className="bot__end" />
              <div className="bot__sr" role="status" data-testid="specbot-announce">
                {truthy(exchanges.at(-1)?.done) ? plainText(exchanges.at(-1)?.answer ?? "") : ""}
              </div>
            </div>
          </div>
          <div className="bot__pin" data-testid="specbot-pin">
            {composer}
          </div>
        </>
      )}
    </section>
  );
}
