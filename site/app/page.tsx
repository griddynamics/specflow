import type { JSX } from "react";
import { ButtonLink } from "@graphico/ui";
import { Haystack } from "../components/haystack";
import { HowItWorks } from "../components/how-it-works";
import { SiteFooter } from "../components/site-footer";
import { SiteHeader } from "../components/site-header";
import { Untangle } from "../components/untangle";
import { DEMO_MAILTO } from "../lib/links";

const WAYS = ["Web app", "MCP", "Jira", "Slack"];
const HERO_PROOF = ["Your organization knowledge", "Your coding agents", "Your cloud"];
const BENCH_POINTS = [
  "how many of the facts the task needs reach the agent,",
  "how much unneeded text comes with them,",
  "whether the finished work passes the task's checks.",
];

function Hero(): JSX.Element {
  return (
    <section className="hero">
      <div className="wrap hero-grid">
        <div className="hero-copy">
          <p className="gx-eyebrow">One shared knowledge base for agentic delivery</p>
          <h1 className="display">Spec-driven development at enterprise scale.</h1>
          <p className="lede">
            A large project starts with hundreds of pages: specs, tickets, decks and meeting notes, written by different people at different
            times. Graphico refines them into one verified specification and finds contradictions and missing decisions. Coding agents then
            build from how everything connects, not one page at a time.
          </p>
          <div className="btn-row">
            <ButtonLink href={DEMO_MAILTO}>Book a demo</ButtonLink>
            <ButtonLink variant="ghost" href="#how">
              See how it works
            </ButtonLink>
          </div>
          <div className="hero-proof">
            {HERO_PROOF.map((text) => (
              <span key={text}>{text}</span>
            ))}
          </div>
        </div>
        <Untangle />
      </div>
    </section>
  );
}

function SectionHead({ label, children }: Readonly<{ label: string; children: JSX.Element }>): JSX.Element {
  return (
    <div className="section-head">
      <p className="label">{label}</p>
      <div className="intro">{children}</div>
    </div>
  );
}

export default function Home(): JSX.Element {
  return (
    <>
      <SiteHeader />
      <main id="top">
        <Hero />

        <section className="section" id="how">
          <div className="wrap">
            <SectionHead label="How it works">
              <>
                <h2 className="h2">Everything your project knows, turned into structure agents can follow.</h2>
                <p className="lede">
                  Graph links your documents into one graph. Gap finds what is wrong with them. Plan gives agents the work that is ready.
                </p>
              </>
            </SectionHead>
            <HowItWorks />
          </div>
        </section>

        <section className="section" id="grounding">
          <div className="wrap">
            <SectionHead label="Designed to scale">
              <>
                <h2 className="h2">Agents find what looks similar. Graphico finds what is connected.</h2>
                <p className="lede">
                  In a large product, the facts a coding agent needs for one task are spread over many documents, and most of them do not
                  look like the task at all. Search returns the passages that read most like it. Graphico starts the agent at the task and
                  lets it follow the links in the graph, hop by hop, to the facts the task depends on, each with the line it came from.
                </p>
              </>
            </SectionHead>
            <Haystack />
            <p className="tiny muted mt-24">
              Illustration of how each method works. Not a measurement. Dots are passages from the product&apos;s documents; closer to the
              task means the text looks more similar.
            </p>
          </div>
        </section>

        <section className="section inv team" id="team">
          <div className="wrap">
            <SectionHead label="Built for teams">
              <>
                <h2 className="display">Large projects are never one person&apos;s job.</h2>
                <p className="lede">
                  A large project is delivered by many teams, vendors and stakeholders, often across time zones. Its decisions are made in
                  comments, calls and chat threads that nobody can find a month later. Graphico keeps every answer, who gave it and why, in
                  one shared workspace, so every team and every agent works from what the project has already agreed.
                </p>
              </>
            </SectionHead>
            <div className="ways" aria-label="Where the team works">
              {WAYS.map((way) => (
                <span key={way}>{way}</span>
              ))}
            </div>
          </div>
        </section>

        <section className="section" id="benchmark">
          <div className="wrap">
            <div className="bench">
              <div className="stack-12">
                <p className="gx-eyebrow">Benchmark · in progress</p>
                <h3 className="h3">We are measuring this on real projects.</h3>
                <p className="small muted">Results will appear here.</p>
              </div>
              <div className="stack-12">
                <p>Same documents, same tasks, same coding agent. Once with agent search, once with Graphico. For each task we measure:</p>
                <ul>
                  {BENCH_POINTS.map((point) => (
                    <li key={point}>{point}</li>
                  ))}
                </ul>
              </div>
            </div>
          </div>
        </section>
      </main>
      <SiteFooter />
    </>
  );
}
