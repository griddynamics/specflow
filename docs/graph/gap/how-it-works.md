---
type: Explainer
title: "How the graph gap module works"
description: "One gap from detection to the re-planned story, keeping noise out, re-uploads, and gaps found by coding agents"
status: stable
tags: [graph, gap]
---
# how-it-works.md: how graph.gap finds gaps, asks once, and changes only what an answer touches

## Why a gap module

A spec is never complete. Authors leave questions open, two documents disagree, a requirement says "fast"
and nothing more, an interface has no provider. A coding agent that builds from such a spec has to guess,
and a guess becomes code nobody asked for. The build already turns the documents into a typed graph, so the
holes can be found as patterns in that graph and in the text behind it, before anybody builds on them.

The gap module does three things:
1. finds the holes and shows how far each one reaches;
2. asks the right person one clear question with a recommended answer, or answers a simple one itself;
3. brings the answer back so that the spec, the graph and the plan change only where the answer touches.

## The loop

```
build ──> gap run ──> plan run                      (after every build)
            │
            ├─ scope        what changed in this build
            ├─ close        answered gaps fixed by this build?
            ├─ relevance    spec, tracking or mixed sections
            ├─ detect       detectors, no model: seeds with witnesses
            ├─ sites        seeds merged and ranked: entry points
            ├─ investigate  one agent per site: real? about what? options
            ├─ check        quotes, ids, options, duplicates
            ├─ route        AI or a person
            └─ record       Gap nodes and relations; options in the relational store

answer ──> Clarification ──> amendment or clarifications document
       ──> update build ──> change report ──> gap run (close) ──> plan update (only the touched stories)
```

## One gap, end to end

The shop spec says, under "Place order": "Can a customer cancel a paid order?". Nothing answers it.

1. **Build.** The build extracts the line as an `OpenQuestion` node, `QST::CANCEL_PAID_ORDER`, and relates it
   to `REQ::FR::PLACE_ORDER`. Its answered-elsewhere check finds no answer, so the node stays.
2. **Detect.** The detector OQ-STATED fires on the node. Its witness is the question line.
3. **Site.** The seed's site ranks high: trace from the requirement reaches two stories in the plan.
4. **Investigate.** An agent reads the evidence, follows `RELATES_TO` and trace, and searches the text for
   "cancel" and "refund". It finds a refund interface but no cancellation rule. It reports an open question
   of severity major, about the question and the requirement, impacting the refund interface, with three
   options: allow within 30 minutes with a full refund (recommended), not allowed after payment, allowed
   until shipment minus a fee.
5. **Check and route.** Code finds every quote in its fragment, resolves every id and confirms one
   recommended option. AI never answers an open question, so the route is `ask`.
6. **Record.** One `Gap` node, `GAP::7C2A91E04B3D`, with `ABOUT`, `IMPACTS` and `CITES`. The three options
   are rows in the relational store. The task that implements the requirement now shows the gap in its brief.
7. **Answer.** The product owner picks option A in the Gaps inbox. A `Clarification` node `ANSWERS` the gap
   and is `PLACED_IN` orders-spec.md. Its edit, "replace the question line with the answer", is stored as an
   amendment.
8. **Apply.** After a quiet period (or "Apply now") the host requests one update build. The corpus reader
   applies the amendment, so the build sees a new version of the "Place order" section and re-extracts only
   that fragment: the question node is removed and `REQ::FR::CANCEL_PAID_ORDER` is added.
9. **Close.** The gap run after the build finds OQ-STATED silent on that line and the answer grounded in the
   new fragment: the gap is resolved, and the clarification gets `GROUNDED_IN`.
10. **Re-plan.** The plan run receives the change report and plans again only the planning part that holds the
    "Place order" story; the "Place order" story gets one new task. Every other part is reused.

Months later anyone can walk from `REQ::FR::CANCEL_PAID_ORDER` to its fragment, to the clarification, to the
gap, and read who decided and why.

## Six kinds of gap

| Kind | Example | A good answer |
|---|---|---|
| Missing information | Payment capture is specified, a failed capture is not | Inserts the missing statement |
| Ambiguity | "The admin panel should be fast." | Replaces it with a measurable statement |
| Contradiction | One document holds only orders over 1,000 EUR for manual review, another holds every order over 500 EUR | Replaces the losing line, often in another document than the winning one |
| Inconsistency | "Customer" in Orders and "Client" in Billing, same fields | Aligns names or separates the two, often in several documents |
| Open question | "Can a customer cancel a paid order?" | Replaces the question with its answer |
| Unconfirmed assumption | "We assume every customer has an email address." | Confirms it as a plain statement, or replaces it |

## Keeping noise out

Real corpora mix spec with project tracking: release plans, sprint scopes, ticket references, roadmaps. The
build extracts some of that as requirements or features that have no owner and no links. A naive "isolated
node" check would turn each into a question.

So structural checks only look at sections judged to be spec. Cheap rules (tracking titles, ticket keys,
version and quarter patterns, sections that are mostly isolated nodes) classify most sections; one batched
model call settles the rest. An isolated or ownerless node is only a weak signal: it needs a second sign,
such as "must" wording, a named module, or a planned task depending on it, before an agent spends budget.
Checks for contradictions and stated questions still read everything, because a real rule written inside a
release plan is still a rule. Each run lists what it treated as tracking in a noise report, and a person can
mark a section "not spec" for good.

## Who answers

Code routes, not a model. Blockers and major gaps always go to a person; a major gap shows the
recommendation preselected. A minor gap goes to AI only when its kind allows it (missing information,
ambiguity, inconsistency; never a contradiction, an open question or an assumption) and every gate holds:
the answer follows from quoted text, it touches one work area and at most five nodes, no stated line says
otherwise, and a second, separate judge call agrees with confidence of at least 0.85. AI answers use the
same Clarification and the same corpus path, are listed for review and can be reverted.

## When the source changes again

Specflow never writes the customer's files: it keeps each answer as an amendment beside its document. When
the owner uploads a new version, reconcile checks every amendment against it:

| The new version | Result |
|---|---|
| Still has the original line (an old file synced again) | The amendment is applied again |
| Has the line slightly edited under the same heading | The amendment is re-anchored and applied |
| Contains the answer, word for word or with the same meaning | Adopted: the amendment retires; the owner's text is the spec now |
| Says something else in that place | Conflict: the build reads the upload, and a follow-up gap asks whether to keep it, restore the answer, or write a new one |
| No longer has the section | Lost: a follow-up gap asks where the answer should go |

"Download with answers" gives owners the text the build reads, so they can adopt answers into their files
and let the amendments retire.

## Gaps found while coding

A coding agent that hits a hole while it builds a story reports a finding over MCP. That finding is a gap
with origin `development`: same node, same inbox, same answer path. It is investigated at once rather than
at the next build. If the spec already answers it, the agent gets the answering lines and its story is
released. Otherwise the gap goes to the top of the inbox, the answer is applied without waiting, and the
story's tasks return to pending once the build has the answer in the spec. The agent then reads a brief built from
the updated spec, not from a note.

## What stays the same

The build ontology and the plan ontology do not change. The build never knows that a text came from an
answer; the plan never imports the gap module. Gap writes only its own two node types and their relations,
and the host's relational store keeps everything nobody traverses.
