---
type: Design
title: "Graph build"
description: "How graph.build turns a corpus into the graph and keeps it current"
status: stable
tags: [graph]
---
# build.md: how graph.build turns a corpus into the graph and keeps it current

## Modes

| Mode | When | What is extracted |
|---|---|---|
| `first` | The project has no persisted graph | Everything |
| `update` | Every later build (a delta build) | New, changed and retried fragments, and fragments whose stored result no longer matches the extractor fingerprint; stored results are reused for the rest |
| `rebuild` | Only when the host asks | Everything again; no stored extraction result is reused and Resolve and Conform keep no persisted label; only embedding vectors and merge decisions are reused, so identities stay stable. Matched nodes keep their `uuid`, `spec_id` and label (Diff) |

The library picks `first` or `update` from the persisted graph; the host asks for a `rebuild` with a
flag in the build context. An `update` needs the record of the last `first` or `rebuild` build in the
`BuildStore`; when the project has a persisted graph but no such record, it raises
`ConfigurationError("rebuild_required")` and a `rebuild` is needed; when that record's build is not
finished, it raises `ConfigurationError("unfinished_build")`.

`BuildOptions` are frozen by the last `first` or `rebuild` build; an `update` build with different
options raises `ConfigurationError("frozen_options_changed")`, and one with another embedding model
`ConfigurationError("embedding_model_changed")`. Changing options or the embedding model needs a `rebuild`;
adding, removing or renaming ontology types or relations needs a migration (Migration). An `update`
build in those cases raises `ConfigurationError` (`ontology_changed` for types and relations). The
ontology is checked first, so a change of both the ontology and the options raises `ontology_changed`.

## Steps

A build runs these steps in order. Each step stores its output in the `BuildStore` (artifacts) so a
host can run steps separately and resume after an interruption; `run_build` runs them all in one call
([api.md](api.md)), then the follow-up builds the build asks for (Follow-up builds and settlement).

| # | Step | Output |
|---|---|---|
| 1 | Plan | Change set, fragments, dirty fragments, units |
| 2 | Extract nodes | Raw nodes per fragment |
| 3 | Resolve | Canonical nodes with identity keys, names, labels, embeddings |
| 4 | Extract edges | Raw edges per edge window, catalog parts per unit, attribute values |
| 5 | Conform | Target graph: grounded nodes, legal and normalised edges, statements; quarantine |
| 6 | Diff | Changes from the current graph to the target graph, with `spec_id`s and spec changes |
| 7 | Persist | The diff written to Neo4j |
| 8 | Infer | Inferred edges ([analysis.md](analysis.md)) |
| 9 | Verify | Verification run with repairs ([analysis.md](analysis.md)) |

### Plan

1. Read the corpus snapshot. Parser comment lines (page markers, machine annotations) are masked: they
   keep their line numbers but are never part of text, hashes, prompts or quotes.
2. Change set: compare the snapshot with the persisted `Document` nodes. A lineage without an `active`
   document is new; a different `revision_id` is a new revision; an active document missing from the snapshot is
   removed. The host may add lineages to retry: their `skipped` fragments, and their `active`
   fragments that were never extracted, are extracted again. A fragment with an unknown status is
   read as never extracted.
3. Segment each new or revised revision into fragments along its headings, within a token range
   (`fragment_tokens`). A long section is cut at points chosen from its content, so an edit changes
   only the fragment holding it and at most its neighbour. The fragment id is
   `<document_id>#<heading path slug>#<8 hex chars from the first body line>`, with `~2`, `~3` on
   collision; an unchanged section keeps its id across revisions.
4. Match the fragments of a revised document to its persisted fragments: same content is unchanged;
   same heading path with mostly the same lines is changed; the rest are new. Persisted fragments
   without a match, and every fragment of a removed document, become `retired`.
5. Dirty fragments are new, changed and retried ones. Fragments are grouped into units: a unit is
   consecutive fragments of one document, never of several. A document that fits `unit_tokens` is one
   unit; a longer one is cut into balanced parts at fragment boundaries, preferring top-level headings.
   Units and their windows are working data only; they are never stored in the graph.

### Extract nodes

Node extraction runs two passes over the same windows of every unit with dirty fragments (call class
`extraction`; each call one structured gateway call with its own messages and the shared answer schema, both
rendered from the ontology):

1. **Hub pass** (prompt `nodes.hubs.14`): every window first asks for the hubs only (types whose
   ontology role is `anchor` or `group`; in the shipped ontology modules, features, capabilities,
   interfaces and data entities), each with the other names the text uses for it.
2. **Statement pass** (prompt `nodes.statements.15`): after every hub call finished, every window asks
   for the other extracted types only (requirements, rules, questions), with the hub catalog of the
   hub pass as known terms.

Both passes send the same answer schema (both lists), so the system message, the schema and the
unit message form one prefix the provider caches across passes (a schema per pass got no cache hit
across passes in a live probe). Each call parses only its own list; items in the other list are
counted (`stray_pass_items`) and dropped.

Hubs come only from the hub pass, statements only from the statement pass. An ontology without hub
types makes the statement pass only (one without other types the hub pass only). Messages, in this
order: the system message (1), the unit message (2, a user message holding only the fragment
blocks) and the call's own user message (3 to 5):

| # | Part | Content |
|---|---|---|
| 1 | System message | Fixed per ontology, the same for both passes: role, corpus framing and exclusions, the types of each list under its heading with their descriptions, group texts, the hub rules (every hub the text names, body and headings, also one mentioned in passing; never invented; one hub per thing by the name the text uses most consistently, other names as aliases, never two things joined in one name, never a generic noun alone; the other-names rule), the statement rules (rationale, questions: one per question asked, an either-or question is one, an answered one none; commitments), the table rule (a table is read as the sentences it stands for; statements: one per row of a requirements, rules, settings, services, environments, deployment, policies or integrations table, and of any table whose rows each state what the system supports, allows, limits or does (limits or quotas, supported or allowed items, outcomes and codes), never one for the whole table, also when the row names a hub; a row giving a value a sentence elsewhere only refers to is its own statement, never a restatement; a network or connection policy row is a rule the system enforces on its own traffic, never an integration contract; one entity per field table of one object, one per meaningful matrix cell with examples, a cell holding an error, status or response code counted like any other, permission cells as rules of who may do what, same-value cells of a row and a value a whole column shares grouped, none for empty or "n/a" cells, "-" cells only when the legend gives them a meaning, none for a glossary, people or RACI table or a list of hub names; hubs: every row of a list of features, modules, services, components or interfaces is a hub, every row of an out-of-scope, excluded or deferred list too (the type it would have in scope), a row naming several things one hub per thing, a row of another table may name one, a glossary, people or RACI table none unless a row names a part), the out-of-scope, naming and statement verbatim rules, the explicit id families, the known-terms rule, the window rule and the answer rule of both lists |
| 2 | Unit message | A user message of its own, byte-identical for every call over the unit (both passes, every window): every fragment of the unit as an untrusted block `fragment 1..n`: a breadcrumb of the document title and heading path (prompt-only), a blank line, the fragment text |
| 3 | Known terms | Starts the second user message: an untrusted block, when the unit has any: one line per term, `name (Label; also: a, b)` |
| 4 | Pass line | `Pass: answer only the "hubs" list: ...` or the same for `statements`: the one list the call answers, ending `Leave "statements" empty ([]).` (or `"hubs"`) |
| 5 | Window line | Last: `Window: extract only entities described in fragments a to b.` (`fragment a` for one) |

Every node call of a build shares the system message, and every call over a unit, of either pass,
shares the unit message, so a provider's prompt cache can serve that prefix to both passes; the
gateway records the cached input tokens per call class and prompt ([api.md](api.md)), so usage
shows hub-pass and statement-pass cost apart.

Prompt caching: the provider (a live probe, TEST.2b) reuses a cached prefix only up to where an
earlier request ended, never up to the middle of a message: calls over one unit that differ only in
their last message still read just the system message from the cache. So, per pass, a unit with two
or more windows first sends one warm-up request (`Gateway.warm_up`, prompt `nodes.warmup.1`, the
same answer schema): exactly the system message and the unit message, its answer cut at a few
tokens and discarded. The unit's windows then start together and read both messages from the cache
(about 99% of their input in the probe). A unit with one window sends no warm-up; a truncated
answer that halves a window warms its unit first (once). The warm-up runs per pass because a cache
entry lives only minutes and the statement pass may start much later; a failed warm-up is logged
(`graph.extract.warm_up_failed`) and costs only the cache. Units run concurrently. Every warm-up of a
pass starts before the pass's first window call (while its known terms are ranked); a unit's
windows wait only for its own warm-up. A unit's statement-pass warm-up starts as soon as its own hub
windows answered, so it overlaps the hub windows of other units. The statement pass starts when
every hub call has finished. When a pass fails, the warm-ups still running are cancelled.

The answer has one list: `hubs` (hub pass) or `statements` (statement pass). An item is `{name, label,
quote, fragments}`, a hub item also `aliases`: `label` is an enum of the pass's labels; `quote` is the
one sentence, table row or list item that states the entity (a statement) or the sentence, row, item
or heading that best names or describes it (a hub), verbatim, `""` when the text has none;
`fragments` are the numbers (the block labels) of the window's fragments that state, name or
describe it; `aliases` are the other names the text itself uses for the hub (a defined
abbreviation, a stated equivalent, a container, code or table name), never a generic reference or
the name of a different thing. Every item becomes one raw node with its quote (and aliases); nothing
is collapsed within a call (Resolve merges equal nodes). A name given two or more labels in one call
keeps one raw node per label and is counted (`label_collapses`).

**Hub catalog.** The hub pass items of the build (answered, and in an `update` build reused) are
merged into the hub catalog, deterministically (same items, same catalog):

| # | Merge rule |
|---|---|
| 1 | Names and aliases get folded whitespace; an item with an empty name or a label that is no hub label is left out, and so is an alias with the item's own hub surface |
| 2 | A weak alias is dropped (counted): at most 2 characters ("DB", "UI"), or only stop words and generic words ("the service", "the system") |
| 3 | Items group by label and hub surface (Resolve's hub form) |
| 4 | An alias naming a group of another label is dropped (counted) |
| 5 | An alias naming another group of the same label links the two groups into one hub, unless the hub would hold more than 4 groups (the link is refused and counted, and the alias dropped as another hub's name) |
| 6 | An alias claimed by two or more hubs is dropped from each (counted) |
| 7 | A hub's name is the variant given most often, then the one named in most documents, then the lowest; but a linked full name (a longer name of a group whose items give the hub's other groups as aliases, the shortest of them: "Image QA Service" for "IQS") wins over a shorter name unless that name was given more than twice as often as the full name's items and links |
| 8 | The hub's other names are its aliases: the best variant of each other group and the kept aliases (the lowest text per surface), sorted; the documents of its items are kept with it |

The catalog (`NodesArtifact.hub_catalog`, the catalog part only, without persisted hubs) and its
counts are stored on the extract-nodes artifact, for the known terms and the metrics; Resolve does
not read it. There is no separate catalog call; the hub pass runs and its catalog is stored whatever
the `hub_catalog` option says.

**Known terms** make the same thing named the same way in every document. A known term is reused when
the text refers to the same thing by its known name, a nearly identical one or one of its other names;
when the text clearly uses a different name (a rename), the text's name is used, and Diff carries the
node over (same `spec_id`, new name, the old key kept as an identity alias); an "also" name is not a
rename. In the hub pass a known term is extracted only when the window names or describes it; in the
statement pass known terms are for naming only, never extracted as statements.

| Pass | Build | Known terms |
|---|---|---|
| Hub | `first`, `rebuild` | None |
| Hub | `update` | The persisted hub nodes (with their `surface` identity aliases) |
| Statement | `first`, `rebuild` | The hub catalog (`hub_catalog` option; off: none) |
| Statement | `update` | The persisted hubs merged with the hub catalog (option off: the persisted hubs only): a catalog hub that meets exactly one persisted hub of its label by a name or alias adds its names to that hub as aliases (the persisted name wins); one meeting two or more is left out (counted); one meeting none is added |

The statement pass's source is recorded as `known_terms_source`: `hub_pass`, `persisted_and_catalog`,
`persisted` or `none`.

Each pass selects the terms of each window from its hub list, one term per hub surface and label
(the first by name, with the aliases of the others), from the window's own fragments (a unit can
name hundreds of hubs; a window names far fewer); the block stays after the unit's fragments, so the
cached prefix is unchanged:

| Part | Rule |
|---|---|
| Mentioned | Every term the window's fragments name, by its name or an alias, whole words in hub form (as `MENTIONED_IN` counts; a one-word alias only as the same word, case-exact); uncapped, except a safety cap of 100 per window (the most mentioned), which is logged and counted per window and pass (`known_terms_capped`) |
| Nearby | Then at most `known_terms_limit` others whose name vector is closest to one of the window's fragment vectors, at cosine 0.40 or above, best first, ties by name; a term without a name vector is found by mention only |
| Shown | One line per term, mentioned first: `name (Label; also: a, b)`, at most 4 aliases (those the window mentions first, then the shortest, then the lowest) |

The same inputs give the same terms. Fragments are embedded for this ranking only; their vectors are
reused by content and never written to the graph. Known terms are computed only when some unit is
extracted. The artifact stores, per unit (its windows joined; a name mentioned in one window counts as
mentioned), the names shown to the statement pass (`known_terms`) and to the hub pass
(`hub_known_terms`), each split into mentioned and nearby, and the persisted hubs among
them (`persisted_hubs`), for the metrics.

A call extracts only from its window: a run of the unit's fragments, while the whole unit stays
visible as context. A unit too large for one call is split into several windows (at most
`max_node_windows`). In an `update` build the windows of both passes cover only the dirty fragments;
the other fragments reuse their stored nodes of each pass.

| # | Attribution of an item |
|---|---|
| 1 | Its fragment numbers, kept only inside the window, tie it to those fragments (`model`) |
| 2 | Without such numbers (none given, out of the window, not fragment numbers): every window fragment whose text holds the quote's words as a contiguous run, whole words only, case and punctuation ignored (`quote`, counted as `quote_attributions`) |
| 3 | Else, for a quote of 3 or more words, or 2 or more that are not stop words: the one window fragment with the best Conform snap score, at least 0.85 (`quote`, counted likewise); a tie gives none |
| 4 | Else rules 2 and 3 over every fragment of the unit: an item quoting a fragment outside its window is tied to that fragment (`quote`, also counted as `quote_outside_window`) |
| 5 | Else every fragment of its window (`window_fallback`, counted as `window_fallbacks`) |

A truncated answer halves its window and tries again, up to 4 times; a failed window of either pass
skips its dirty fragments.

Node results are stored per pass and fragment (`ExtractionStore` kind `nodes`, key
`<project_id>|nodes|<pass>|<document_id>|<content hash>|<heading path hash>`), under one extractor
fingerprint for both passes, and only for the fragments that a window of every pass answered: a
fragment one pass skipped stores nothing new, so a later build never mixes one pass's new nodes with
another pass's old ones. A fragment counts as stored (Plan does not extract it again) only when the
nodes of every pass are stored for its content.

The prompt's corpus rules say that an out-of-scope, non-goal or "not supported" item is extracted as
the type it would have, with `in_scope: no`, and that a question or TBD the text states is an
`OpenQuestion`, never a requirement; rhetorical and answered questions are not extracted.

Tables are handled by the prompt and the ontology text only (the requirement group description
repeats the table rule); no code rewrites a table, so quotes stay verbatim and snap to its lines. A
row or cell statement quotes its row.

The extract-nodes artifact counts, besides the attribution counts above: `windows`, `calls` (both
passes), `hub_calls` (the hub pass's, halves included), `failed_calls`, `truncation_splits`,
`label_collapses`, `fragments_reused`, `fragments_supported`, `known_terms` (distinct terms shown),
`known_terms_capped` and the catalog counts (`catalog_hubs`, `catalog_aliases`,
`catalog_links_refused`, `catalog_aliases_dropped`, `catalog_ambiguous`; [analysis.md](analysis.md)).

### Resolve

1. Each raw node gets an identity key ([data-model.md](data-model.md)): its explicit id, else its
   normalised name; a requirement without an id gets a provisional key until Conform finds its
   defining line. The name of a hub node (role `anchor` or `group`) is keyed in its hub form, the
   form in which hub names are compared everywhere (Conform's hub grounding, `MENTIONED_IN`, known
   terms, catalog alias pairs below); a text that names a hub is read into the same word form:

   | # | Hub name form |
   |---|---|
   | 1 | `-`, `_` and a `.` between two letters separate words (`order.paid` is `order paid`; `v1.2` keeps its dot); a leading article (the, a, an) is dropped |
   | 2 | Upper-case words joined by `_` fold as lower-case words (`ORDER_ITEMS` is `order item`); a part of at most 4 characters stays an acronym (`CORS_POLICY` is `cors policy`, `ACCESS_LOGS` keeps its "s") |
   | 3 | A lone upper-case word is an acronym: only case-folded (`CORS`); its plural with a lower-case `s` drops it (`APIs`) |
   | 4 | A word with an upper-case letter after its first character folds its plural like a lower-case word (`WebSockets`), unless it ends in an upper-case letter or in `ops` (`macOS`, `SaaS`, `DevOps`): then it is only case-folded. camelCase is never split (`PayPal`, `GitHub`) |
   | 5 | Any other word is case-folded and its plural folded: `-ies` to `-y` (more than 4 letters), `-sses` to `-ss`, `-uses`, `-xes`, `-shes` and `-ches` drop `es`, any other final `-s` drops unless the word ends in `-ss`, `-us` or `-is`; words of up to 3 letters, or with a non-letter, stay |
   | 6 | Exceptions to 5 (English word lists, no ontology names): a word singular in `-s` stays (news, series, alias, canvas, lens, analytics, logistics, goods, ...) and its plural drops `es` (aliases); `-uses` after a vowel drops `s` (houses, causes); `-ches` after two vowels drops `es` (approaches); a listed `-ie` singular drops `s` (cookies, movies); a few Latin and Greek plurals map to their singular (indices, matrices, analyses, crises, hypotheses) |

   So "The Orders service", "Order Service" and "order-service" share `surface|order service`. A change of
   these rules changes some hub keys: an `update` build continues such a node by Diff carry-over
   (same `spec_id`, `carried_over`); decisions stored under the old key are not found.
2. Exact merges: raw nodes with the same key in the same identity scope merge; so do nodes with the
   same explicit id and hub nodes of one label with the same hub form. A name shared by hubs
   of different labels never merges them by itself: a feature and its module are different things.
3. Hub consolidation (judge prompt `consolidate_hubs.4`, call class `judge`) replaces pairwise
   similarity merging. Every hub cluster left after step 2 is one item, keyed for decisions by its
   identity key; every build consolidates every hub item (reused raw nodes too), and stored decisions
   make the unchanged ones free.

   | Part | Rule |
   |---|---|
   | Item line | One JSON line per item in the untrusted `hubs` block, with a short id `n1`, `n2`, ... by position: `label`, `name`; `quote`: the first quote of its raw nodes in plan order, ties by text (at most 200 characters); `heading`: the heading path of its main fragment joined by ` > ` (at most 120); `requirements`: up to 3 distinct names of the raw statement nodes of the main fragment, those naming the hub first, each part in name order, so stored raw nodes reused in another order show the same line (at most 100 each); `evidence`: up to 2 distinct lines of its fragments in plan order that name it, leaving out lines its quote holds (at most 160 each). The main fragment is the first of its fragments in plan order with a line naming the hub (a name of it as whole words, plurals folded), else the first. Every text is made prompt-safe |
   | Label parts | An item's part is its label when its type has `merge_across_labels: false` (interfaces, data entities: a part each), else the one part all other hub labels share. Items of different parts never share a block or a batch, and their pairs get no stored decision |
   | Blocks | Each part, sorted by decision key, is ordered by a greedy nearest-neighbour chain over `name_embedding` and cut into blocks of at most 120 items that overlap by 10; one judge call per block, all concurrent |
   | Compound names | A heading or name joining two things (X and Y, X & Y, X/Y) names two things: their nodes stay apart even when one heading names both, and two compounds sharing one part (X and Y, X and Z) are different; a compound groups with the node of one of its parts only when the lines show it names that one thing (its second part a piece or detail of it) |
   | Answer | Groups of ids that are one part of the system or one grouping, each with the label the merged node keeps (any hub label) and a confidence: `high`, `medium`, or `low` when the judge suspects but cannot confirm. An unknown id, or one already grouped in the answer, is dropped (`unknown_ids`); a group of fewer than two ids is ignored. A `low` group is unsure (`unsure_groups`): not merged, not stored, not taken as different, so it is judged again next build. A failed call merges and stores nothing |
   | Vetoes | A group is split into its label parts (`label_splits`); each part merges member by member in decision-key order from its first member. A member stays out when two different explicit ids would meet, the node would hold more than 12 exact-merged groups, a stored `different` decision exists with a member already accepted, or one side holds a hub named from a heading and the other one named from a heading nested under it in the same document (a section and its subsection: a raw node whose quote is a `#` heading line of its fragment, with or without the markup, outside code fences; such a pair is never judged or stored either, and as a candidate it is `vetoed`; TEST.4b: the chapter hub Data Migration joined the hubs of two of its subsections, and across the TEST.1–4 builds the rule meets wrongly merged pairs only). The merged label: the part's own label when its type does not merge across labels, else the proposed hub label, else the majority of the raw labels |

   **Candidate pairs.** A block shows only neighbours on the name chain, so two more kinds of pairs
   are found per label part (`pair_candidates_vector`, `pair_candidates_alias`):
   - vector pairs: each item with its 5 nearest items by candidate-vector cosine, at least 0.52. The
     candidate vector embeds `"<name>. <heading>. <quote>"` of the item line (no label, so a
     duplicate under another label of the part is found); it comes from the vector store and is never
     written to Neo4j. `name_embedding` stays the vector of the chain and the decisions. The floor is
     the highest cosine that still holds 90% of the links of TEST.2's merged hub clusters (45 of 48);
   - alias pairs: two items whose hub forms meet the name or an alias of one hub catalog entry
     (`NodesArtifact.hub_catalog`, [Extract nodes](#extract-nodes)); no floor.

   A candidate pair is judged, and its decision stored, in every block that holds both items.

   **Pair pass.** After the blocks, a candidate already in one node or in one block is settled; one
   with a valid stored `different` decision is dropped, one with a valid stored `same` decision is
   applied as stored. The rest are queued, alias pairs first, then by falling candidate cosine, and
   packed first-fit into batches of at most 16 items of one part. At most 60 batches are sent per
   build (TEST.3b specflow needed 43); the pairs left over (`pair_over_cap`) are neither judged nor stored and queue again in the
   next build. In an update, a candidate of two items settled before the build (below) that no stored
   decision covers is kept apart and not sent (`kept_apart`), unless an earlier build left it
   undecided: a candidate ending `over_cap` or `unsure` is recorded as queued and stays open while
   both items keep their evidence (TEST.4b R2: a candidate there only because a neighbouring hub
   vanished merged *Dynamic Client Registration* into *CIMD*, two hubs the first build kept apart). A batch is a call of the same prompt with the same item lines, vetoes and decision
   memory; its judged pairs are its placed candidates and its close pairs; its items get no node
   entry (`pair_batches`, `pair_calls`, `pair_merges`, `alias_merges`).

   **Cross-label twins.** After the pair pass, two items of one part with different labels that share
   a hub surface (one name, two labels) and are not yet one node are asked as an explicit pair
   (prompt `hub_twins.3`, call class `judge`; `ConsolidationParams.twin_pass`, on): one JSON line per
   pair in the untrusted `hubs` block, with id `p1`, `p2`, ... and the item line of each side (`a`,
   `b`); the answer per pair is `same`, the merged `label` (empty when not same) and a confidence.
   One name in two labels of a merge-across part is one thing by default: `same` false only when the
   lines state a different scope (one a piece or step of the other), owner (another party's product
   and the system's own part) or meaning. The judge sees only the descriptions of the labels it can
   choose (the merge-across hub labels); they pick the label, never sameness (TEST.3b: `hub_twins.2`
   refused 17 of 18 such twins). Twins are asked outside the vector floor and the pair-pass cap, at most 8 pairs per call, all
   calls concurrent. A `same` answer is a proposal like any other (vetoes, label choice); `same`
   false is `different`; a `low` answer, a missing one or a failed call is unsure and stores
   nothing. Decisions are stored under their own pass `hubs.twin` (key
   `<project_id>|hubs.twin|<key a>|<key b>`, fingerprint `hub_twins.3|<judge model>`, with the pair
   evidence hash); a valid stored one is applied without a call. Merges are recorded with source
   `pair` (`stored` when applied from memory); a twin candidate pair's outcome is the twin answer's.
   The block judge kept such twins apart in every TEST.3 run, so they get this question of their own.

   **Decision memory** (`ExtractionStore` kind `decisions`, fingerprint `<prompt version>|<judge
   model>`, so a prompt bump drops every stored decision):

   | Entry | Key | Payload |
   |---|---|---|
   | Pair | `<project_id>\|hubs\|<key a>\|<key b>`, keys sorted | `same`; `label` (merged); `confidence`: the judge's `high` or `medium`, `implicit` for a judged pair it did not group; `evidence` |
   | Node | `<project_id>\|hubs.node\|<key>` | The keys it was stored `same` with; `unsure` when it was in a `low` group; `evidence` |
   | Twin | `<project_id>\|hubs.twin\|<key a>\|<key b>`, keys sorted, fingerprint `hub_twins.3\|<judge model>` | As a pair (`confidence` the judge's); no node entry |
   | Queued | `<project_id>\|hubs.queued\|<key a>\|<key b>`, keys sorted | `queued`: a candidate a build left `over_cap` or `unsure`; `evidence` (the pair hash) |

   - A block's judged pairs are its pairs at `name_embedding` cosine 0.85 or more and its candidate
     pairs. After a judged block every pair inside a joined subset is stored `same` (a pair joined in
     either of two overlapping blocks is `same`), every other judged pair `different`, except the
     unsure ones; every judged item gets a node entry (it was seen).
   - Evidence hash: an item's is the sha256 of its label, its distinct names and its item line (what
     the judge is shown of it, its `name` aside: the names hold it); a pair's is the sha256 of both keys with their items' hashes. A stored
     entry counts only while its hash matches the current one; one that no longer matches has expired
     (`decisions_expired`) and its items are judged again; one without a hash (written before hashes)
     is a miss. An edit to a hub's fragment that leaves its item line as it was keeps its decisions:
     the judge would be asked the same question (TEST.4 R2: the edited fragment named 22 hubs, the
     item lines of 6 changed; with fragment content hashes in the hash all 22 expired, 151 entries).
   - A valid stored decision overrides the model: a stored `same` pair is proposed even when the
     answer did not group it (still under the vetoes), a stored `different` pair is a veto inside a
     proposed group (`decision_overrides`). A block whose items were all seen (valid node entries,
     not unsure) and whose judged pairs all have a valid decision is not sent (`blocks_skipped`); its
     stored `same` pairs are applied. So an edit to a hub's evidence re-sends only its blocks and pairs.
     A stored `same` pair that an answer grouped is applied again after the answer, so a veto on
     another member of that group never undoes it.
   - Settled items keep their grouping. An item with a valid node entry that is not unsure is settled.
     A block sent again for a changed or new item gets an answer about all its items; a pair of two
     settled items that the answer groups although the block does not judge it (no close pair, no
     candidate pair) and no stored decision covers it is kept apart (`kept_apart`, nothing stored):
     the group is cut into the parts its members form without such pairs, so settled items joined
     through a changed item stay joined. A group cut this way also loses its pairs with a stored
     `different` decision, so a settled member stored `different` from both never bridges two settled
     items (TEST.5 R2: *Connector Sync* and the chapter *Source Connections and Integrations*, never
     judged together, were joined through a hub stored `different` from both, and the node then took
     the Module twin of *Connector Sync*). Only a changed or new item, or a judged pair still undecided
     (unsure, over the cap, a failed call), changes how settled items group. The pair pass keeps the
     items settled before the build apart by the same rule, and sends no candidate of two of them
     that is neither stored nor queued. Trade-off: two settled hubs no build judged together stay
     apart until one changes. (TEST.4 R2: blocks re-sent for an edited hub joined `atlassian` and
     `atlassian cloud`, two nodes of the first build, and a decision expired by a content hash split
     `member` from `membership and bio`; every window naming them changed its window catalog.)
   - Whole and part in an update. Two items that continue two different persisted nodes (their keys
     name them, `persisted`) are never joined, judged or stored when one's name is the other's
     followed by more words whose first is not a preposition, conjunction or article (`user` and
     `user provisioning and profile`, `atlassian` and `atlassian cloud`: the shorter name is a
     modifier, so the longer names a part or aspect of it; `todo item with question json` or
     `dcr for mcp client` still name the shorter one and are judged). It is a cluster veto, so a new
     item never chains the two either; a candidate is `vetoed`. A new hub joins a persisted one as
     before, and a first or rebuild build judges such pairs freely (TEST.5 R2: an edit gave the
     persisted *Users* page the lines of the provisioning requirement, and the pair pass merged it into
     *User Provisioning and Profile*; across the stored update builds the rule meets only that merge
     and TEST.4's `atlassian` into `atlassian cloud`).

   **Records.** The resolve artifact keeps every merge (`merges`: anchor and member key, source
   `block`, `pair` or `stored`, confidence) and every candidate pair (`pair_candidates`: keys, part,
   name and candidate cosines, source `vector`, `alias` or `both`, outcome `same`, `different`,
   `unsure` (a `low` group or a failed call), `vetoed` (refused by a veto, or a section and its
   subsection), `over_cap`, `block` (settled by the blocks) or `kept_apart` (two settled items of an
   update, not sent)). Identity keys stay in the artifact.
4. Resolve never merges requirements by similarity, with or without an explicit id: wording that looks
   alike may state different commitments ("within 5 s", "within 10 s"). Conform deduplicates
   near-identical restatements of those without an id under guards (Conform 2).
5. Each canonical node gets a name (its clearest member's surface), a label (explicit id family first,
   then the extracted label; existing nodes keep theirs, except in a `rebuild`, and a node of a type
   with `merge_across_labels: false` never takes another label's persisted node) and a
   `name_embedding`. In an `update` build a node holding a member keyed by a persisted node's
   identity key keeps that anchor: its name comes from those members and its identity key is theirs,
   so a node whose members stay keeps its key although another member's name became more frequent.
   It keeps its members' source quotes: each distinct quote once, with the union
   of the fragments of the members that gave it, in plan order (`CanonicalNode.quotes`).

### Extract edges

Edges are extracted per edge window (prompt `edges.8`, call class `extraction`): each unit is cut into
windows, and each window's call sees the whole unit and one catalog of the nodes the unit may link,
drawn from the whole project after Resolve, but states only the facts of its own fragments.

**Edge windows.** Cuts are content-defined, so an edit moves only nearby cuts. From the start of the
unit, each window ends before the fragment that, among the cuts leaving the window between 0.5 and
1.5 times the target size and at least 0.5 times the target after it, has the lowest heading-depth
penalty (more heading levels shared with the fragment before cost more), then the lowest content
hash, then comes first. When the rest of the unit is at most 1.5 times the target, it is the last
window; when no cut qualifies, the window ends before the fragment that would take it over 1.5 times
the target, so an oversized fragment stands alone. A unit of one fragment is one window. A unit gets
at most 8 windows: past that, the target is raised to the smallest step target × 1.25^k (k = 1, 2, ...)
whose windows fit. The steps are quantised, not the unit's tokens / 8, so an edit that keeps the unit's
step moves only the cuts near it; an edit that changes the step (the window count crosses the cap)
may move every cut of the unit, whose windows are then called again (`not_stored`) or fall back by
identity. The target is internal, not a build option: 3,000 tokens, set by TEST.4 R0 (the cheapest
size keeping edge precision within .03 of one call per unit and `PART_OF` precision at least as high on
both test corpora; 6,000 lost `PART_OF` precision on one). Units are unchanged; the estimate cuts the
same windows (Cost estimate).

**Catalog.** One catalog per unit, the same in every window's call (so it is cached), in parts:

| Part | Nodes |
|---|---|
| `local` | Every node grounded in the unit |
| `mentioned` | Hub nodes from elsewhere that a body line of the unit's text names (name or `surface` alias); a markdown heading line outside code fences is not a body line |
| `heading` | Hub nodes from elsewhere named only in headings: an element of a fragment's heading path, or a heading line of its text, which repeats its section's heading (a section headed "Payments API" offers that interface) |
| `near` | Hub nodes from elsewhere whose name vector is close to a fragment of the unit: per fragment the 20 closest by cosine, at 0.40 or above and at least 0.8 of the fragment's best cosine to any hub from elsewhere, so unrelated hubs never enter; ties by `spec_id` or identity key. In an `update` build a unit with no changed fragment keeps the hubs it took by nearness last time (while they exist) and takes new hubs only when its text names them |
| `referenced` | Requirements of fragments in other units that the unit references (References below), the hubs of a referenced section that holds no requirement, and nodes of other units it names by explicit id; at most 30 per unit |

`mentioned`, `heading` and `near` together hold at most `catalog_limit` hubs, in that order;
`referenced` comes on top. So a requirement can link to a module, capability or feature that another
document defines, or to a requirement another document's section states. The parts of each unit are
stored (`EdgesArtifact.catalog_parts`) so Conform can count what each entry path yields (Conform,
Foreign edges).

Internal parameters, set by TEST.4 R0 and recorded in the extractor fingerprint: nearness `off` (the
default: the `near` part stays empty; nearness hubs were under 2-4% of kept edges, and Cross-document
links in [analysis.md](analysis.md) link those documents), `name` (the hub's `name_embedding`) or `rich`
(the vector of its candidate text in Resolve's form, `"<name>. <heading>. <quote>"`, from the heading path
of its first fragment and its first source quote), `name` and `rich` for replays; section hints off (the
local entries of the ownership line's owner labels, features and modules, carry the full heading path
of their first fragment in the unit, joined by ` > `; none without a heading; they gained no `PART_OF`
precision for 3-4% more input); references on.

Each window also has a **window catalog**, used only to decide reuse, never shown: the identity keys
of the nodes grounded in the window, of the hubs named in its text or heading path, and of the
referenced entries its fragments reference. Identity keys, not `spec_id` or uuid: Diff gives those
only after this step, so a node created or carried over in one build would change every window
catalog naming it in the next (a no-change update after a hub rename called 3 windows); a hub that
continues a persisted node keeps its key through its anchor (Resolve 5), and a regrouping of
settled hubs is kept out by the decision memory (Resolve 3). Known gap: a node new elsewhere in the
unit is not linked from a clean
window until that window is dirty or its window catalog changes.

**References** are found in each fragment's text, sentence by sentence (a break after `.`, `!`, `?`
or `;` and a space, or a line break, so `4.2` stays whole), by fixed English patterns, without model
calls; the same text gives the same references. Text in inline code or double quotes is an
illustration or a literal value, never a reference (rules 2, 4 and 5), and a fragment under a heading
with the word "example" has no references:

| # | Pattern | Resolves to |
|---|---|---|
| 1 | Another document's title, all its words, in hub form | That document: it scopes the section and chapter numbers of the same sentence; alone it adds nothing |
| 2 | `section 4.2`, or the section sign `§` with the number (any depth); the plural lists and ranges `Sections 7.1 and 7.2`, `Sections 7.1 to 7.3`, `§§` with `7.1-7.3` (each section; a range over the last number, at most 20 apart, else its two ends) | A section: the fragments of other units whose heading path has an element starting with that number (`4.2 Payments`, `4.2. Payments`; never `4.2.1 ...` or `4.21 ...`). A document without one, but with a heading line of that number in a fragment's text (a subsection merged into its parent fragment: a markdown heading, or a numbered line such as `4.2.1 Retries`, outside code fences), offers that fragment |
| 3 | "see", "described in", "defined in" the `<Title>` section (or document, specification) | A section: the fragments of other units with a heading path element (or a merged heading line) equal to `<Title>` in hub form, the element's leading number left out; a document scopes the sentence as rule 1 does |
| 4 | An explicit id of a node grounded in another unit and not in this one | That node |
| 5 | `Chapter 11`, `Chapters 7, 9 and 11` | The fragments under the top-level heading with that number (the first numbered element of the heading path) in another document, never the same one |

A document named right next to a section or chapter number scopes it: "section 4.3 of the checkout
spec", "Checkout specification, section 4.3" (the words before a document noun such as
specification, spec, document or guide, or a file name). When every content word of the name is a
word of a corpus document's title, it scopes to those documents; a file name (`billing.md`) scopes to
the corpus documents of that file name, the citing document only when it is its own; otherwise it
names a document outside the corpus and the number resolves to nothing.

Rules 2, 3 and 5 match whole sections (the fragments of one document under one heading path prefix):
in a document the sentence names first, then in other documents, then (rules 2 and 3) in other units
of the same document; the first of these with any match decides, and only a unique section counts.
A found section adds the requirements grounded in its fragments, best match first: by the summed
weight of the content words (hub form, stop words and bare numbers left out) a node's name and quotes
share with the referring sentence, a word weighing more the fewer nodes hold it; ties in plan order. So a
big section's requirement the sentence is about gets in before the cap. A node grounded in the unit
itself is never added. A matched section that adds no requirement adds the hubs grounded in its
fragments instead (a module's description, an API chapter's opening): the hub labels a relation of the
reference line pairs, outside an owner pair, with a requirement label or with the same hub label (with
the shipped ontology: modules, interfaces and data entities by `RELATES_TO`, features by `DEPENDS_ON`).
At most 30 distinct entries per unit (`REFERENCED_CAP`), ranked before the cap
so one long table cannot starve the rest: section and title references, then explicit ids, then any
reference in a definition section (a heading with the word glossary, definitions, terminology,
abbreviations or acronyms), then chapters, then the hubs of sections without a requirement. Within a rank the unit's fragments take turns in fragment
order, and within a fragment its references take turns in text order. The window catalog keeps each
fragment's entries uncapped, in text order. The rest are left out (counted per unit,
`UnitReferences.capped`; not reported). No entry is added by similarity.

Edge call messages, in this order: the system message, fixed per ontology (role, framing, the
extracted relations with their descriptions, source → target type pairs and a symmetric note, the
relation rule, one line per `stated_only` relation: such an edge only when the text states it, the
ownership line: `PART_OF` a feature or module only when the fact is in its section or names it, the
list line: an item of a list or table of tools already in use (or that existing tools may provide) and a
scope, goal or summary bullet naming an area own nothing near them, and two items listed side by side
get no `INTEGRATES_WITH` for that (owner relations from the ownership line, integration relations: those
with a `derive_via` rule; a GOOD/BAD example in generic words), the provider line: each interface the
window names gets `PROVIDED_BY` when the text, its section heading or a Provider, Owner or Service column
names the module, and a row of a table or list of endpoints, paths, operations, tools, routes or queues
gets the module a column of the row names as serving or running it, else the module the table's caption,
its section or chapter heading or that section's or chapter's opening text names as offering the API,
catalog or base path (that text may be in a context fragment; the row is the fact), and a queue's module
is the worker or deployment its row names as serving it, never the module that only sends work to it,
the reference line: an entity with
`"referenced_in"` is stated in another document; of a referenced section only an entity whose statement
is what the referring sentence is about is linked, never each entity of the section, and when the
window holds that sentence the entity it states is linked to it as the sentence says, `RELATES_TO` by default, `DEPENDS_ON` only when
the sentence says one needs, requires or waits for the other, never `PART_OF` from a reference alone
(relation names from the ontology: the `id_references` relation, the `stated_only` one with cues and
the owner relation of requirements), the entities rule, the window rule and the answer
rule); the unit message, byte-identical to the node calls'; the catalog, a user message of its own: an
untrusted `entities` block, one JSON line `{"name", "type"}` per node (with section hints, `"section"`
too; a `referenced` entry also `"statement"`, its first quote, and `"referenced_in"`, the unit's first
sentence that refers to it, each at most 200 characters, so every window of the unit shows the same
catalog; a name shared by several nodes gets ` (<Label>)`, then ` #2`, ` #3`); last, the window line, a
user message of its own: `Window: extract only facts stated in fragments a to b.` (`fragment a` for
one). The edge prefix is not shared with node calls (other system message and schema).

A unit with two or more windows to call first sends one warm-up (prompt `edges.warmup.4`, the same
answer schema): the system message, the unit message and the catalog, its answer cut at a few tokens
and discarded; then its windows start together and read all three from the cache. The warm-ups of
all units start before the first window call, and a unit's windows wait only for its own warm-up (as
in the node passes). A unit with one window to call sends none. A truncated answer halves its window
and tries again, up to 4 times; a unit that sent no warm-up warms first (once). Any other failed
call fails the window.

The answer is `{edges: [{source, relation, target, fact, fragments}]}`: `source` and `target` are
catalog names, `relation` an enum of the extracted relations, `fact` the one sentence, table row or
list item that states the fact, verbatim. A fact between names outside the catalog
(`unknown_name_edges`), with a relation the ontology does not extract (`unknown_relation_edges`) or
from an entity to itself (`self_loops`) is dropped; a fact without fragment numbers in the window is
tied to the window's dirty fragments, else all its fragments (`attribution_fallbacks`). Trade-off:
when its numbers name only fragments outside the window (`outside_window_edges`), the fact is about
another window's text, yet it is kept on this window's fragments and stored and reused under this
window's content, so it survives an edit that removes the text it came from until this window is
called again. It is kept, not dropped, because the model's numbers are often off by a fragment while
the fact itself is right; the counter shows how often it happens.

**Store and reuse per window** (`ExtractionStore` kind `edges`):

| Key | Holds | Read |
|---|---|---|
| Edge key (`edge_key`): `<project_id>\|edges\|<window hash>\|<sha256 of the sorted window catalog keys>`; the window hash is the sha256 of its fragments' content hashes | The window's raw edges, ends by identity key | A window whose content and window catalog are unchanged reuses its edges; ends are mapped to this build's nodes by identity key or identity alias |
| Last key (`last_edge_key`): `<project_id>\|edges_last\|<window hash>` | The window's last answered edges (any window catalog), the unit catalog's nearness hubs and the edge key they were stored under | A clean unit keeps its nearness part; the first fallback of a failed window |
| Window key (`window_edge_key`): `<project_id>\|edges_window\|<document_id>\|<first fragment id>` | The window's last answered edges, whatever its content | The second fallback only |

An answered window is stored under all three keys (a reused one under the last two again). In an
`update` build every unit is considered: a window whose edge key is stored reuses its edges and gives
no edge change; every other window is called, with its cause: `dirty` (it holds a dirty fragment),
`catalog_changed` (clean, its content stored under another window catalog) or `not_stored` (clean,
nothing stored for its content, as in the first `update` after a fingerprint change); every window of a
`first` or `rebuild` build is `first`. A failed window of an `update` build falls back to the edges
under its last key (the same content: complete), else under its window key (its first fragment's id,
or the id that fragment replaces; `windows_fallback`, `units_fallback`). The window key holds the
answer of a window that may have been cut differently, so its edges keep only their fragments inside
this window (by id, or the id a fragment replaces; an edge with none left is dropped, never spread
over the unit), and this window's fragments the stored window did not hold (each answer is stored
with its window's fragment ids; an answer stored without them holds none) are
`edge_failed_fragments`. A failed window without fallback keeps the edges of its answered halves,
skips its dirty fragments, and only its own fragments are `edge_failed_fragments`. Diff keeps the
current edges of every `edge_failed_fragments` fragment. The artifact records the windows called
with their cause (`EdgesArtifact.called_windows`) and counts `windows_called`, `windows_reused`,
`windows_fallback`, `catalogs_kept` and `warm_ups`.

A small-model pass (prompt `attributes.7`) fills the attributes of new and changed nodes in
batches. It never asks for the `quote`: that comes from the node call. `acceptance_criteria`,
`release` and `in_scope` are taken only from the text and its heading path, never guessed; a reference attribute names a part the
text names, never a heading (a heading path is context for scope and release only). A node's line
lists at most 3 heading paths of its fragments not shown, those sharing the most leading headings with
a shown one. A hub node is shown, and its stored attributes keyed
by, only the fragments that name it (as `MENTIONED_IN` counts), or all of them if none does.

### Conform

1. Ground every node per source quote (Resolve 5) and every edge's fact; the snapped lines become
   the evidence lines (`REFERENCED_BY`, `source_fragment_ids`). A line span snaps when it holds at
   least 85% of the quote's tokens.

   | # | Node grounding |
   |---|---|
   | 1 | Each source quote is snapped in its own fragments: each of them whose best span matches gives a span |
   | 2 | A quote that matches none of them: the single best span in the other fragments of their units |
   | 3 | A quote that matches nowhere is counted (`unsnapped_source_quotes`). Such a quote's fragments, and attributed fragments no quote came from, are searched with the node's other quotes; for a node that is no hub, the first line there holding one of its explicit ids is then secondary evidence. Of two spans on the same lines the better-ranked one stays |
   | 4 | The node's evidence is the union of the spans; an attributed fragment without one is dropped (`attributed_fragments_dropped`) |
   | 5 | The best-ranked quote span is the primary span (for a hub, a span whose lines name it, by its name or a `surface` alias, ranks before any that does not): its quote feeds the final key, the dedup guards and `id_references`. The persisted `quote` is the run of whole sentences of its lines (verbatim; no sentence ends after an abbreviation such as "No." or "e.g.") that holds at least 85% of the quote's tokens: for a hub one naming it first, then the fewest sentences; else the lines. In an `update` build a quote span whose key a persisted node holds is preferred, so a node's key stays stable |
   | 6 | A node without any span is grounded in its attributed fragments, with each fragment's full line range, and keeps no `quote` |

   So a node stated in several documents keeps evidence and statements from each of them, and a
   change in any of its source documents updates the node (Diff). A hub node is grounded in every
   fragment it was attributed to: on the lines where a quote snaps, else the first line naming it,
   else the whole fragment. An edge's fact snaps in its attributed fragments, then its unit; an edge
   whose fact matches no line keeps its attributed fragments as evidence and no `fact`. An atomic
   node without an explicit id (a requirement or an open question) gets its final identity key here
   from its span value: the sentence(s) of its defining lines (the primary span) that contain its
   quote ([data-model.md](data-model.md)). Nodes with equal final keys merge; on a span key only
   nodes of the same label with alike names merge, and the others get name-qualified keys.
2. Deduplicate: merge near-identical restatements of atomic nodes without an explicit id
   (requirements, open questions), of the same label and identity scope, across documents.
   - Candidates: each node's 10 nearest by `name_embedding` cosine, at least 0.85 (name
     candidates), and its 3 nearest by candidate-vector cosine, at least 0.80. The candidate vector
     embeds `<Label>: <name>. <stmt>. <quote>` (the quote at most 300 characters; a part equal to an
     earlier one left out), comes from the vector store and is never stored on a node. A pair only the
     candidate vectors give is counted (`dedup_candidates_rich_only`). The 3 and 0.80 were calibrated
     by replay at TEST.3 R0 (merge precision .93 specflow, 1.00 stitch).
   - Guards refuse a merge when: both sides state a number of the same kind (a unit kind, or `plain`
     for every unitless number) and neither side's values contain the other's, each side by its own
     numbers (of a kind its name states, the name's values only; the quote counts only for kinds the
     name does not state, so a statement split from a sentence holding its siblings' numbers is
     compared by its own; a record row of the quote, a table row holding an id and an ISO date as
     decision logs and ADR tables have, gives no date: the date is when the record was made); a
     negation is present on one side only; one side says must or shall and the other should or may;
     they reference different explicit ids (the ids of a quote's record rows count, any id family, so
     two different decision records stay apart: TEST.4 R004, a decision row restating an ADR row, was
     refused only by the two record dates); both rest on one source sentence (their primary spans
     overlap in one fragment, or their quotes are equal) and they are not near-identical (name cosine
     ≥ 0.95 and name-token overlap ≥ 0.7; `same_sentence`); the cluster would grow beyond 12 nodes.
   - A pair a pairwise guard (numbers, negation, modality, ids, same sentence) refuses is listed on the
     conform artifact (`dedup_refused`: guard, label, name and candidate cosines, its ends by the keys of
     the nodes they end in after dedup). A pair whose ends merged into one node anyway keeps its own
     keys and is marked `collapsed`; a pair with an end normalisation drops leaves the list; of pairs left with the same ends and guard, the one of
     highest name cosine stays. Cluster refusals (size, mixed modality) are counted only. The build
     result lists the pairs by `spec_id` (Build result).
   - A name candidate at cosine ≥ 0.95 and name-token overlap ≥ 0.7 merges directly; any other
     candidate, and every candidate-vector-only pair, merges only when the small model
     (`dedup_adjudicate.3`; names and quotes in an untrusted block) judges it the same commitment,
     possibly with more detail, and not conflicting, with `high` or `medium` confidence. A `low`
     verdict is unsure (`dedup_unsure`): no merge, not stored. A failed call merges nothing.
   - Verdicts are stored per project (`ExtractionStore` kind `decisions`, key
     `<project_id>|dedup|<key a>|<key b>` with the keys sorted, fingerprint `<prompt version>|<small
     model>`) with their confidence and an evidence hash: the sha256 of both keys with each node's
     hash of label, name, `stmt` and distinct source-quote texts. A stored verdict whose hash matches
     replaces the call (`dedup_verdicts_reused`); one whose hash differs has expired
     (`dedup_verdicts_expired`) and the pair is asked again; one without a hash is a miss.
   - The lowest identity key survives; the other keys become its identity aliases; evidence and
     statements are joined. Attribute values of the members: the first wins (merged members that
     disagree on `in_scope` are counted as `in_scope_disagreements`). For an attribute the spine
     `exclude` reads (`in_scope`), in any merged node (equal keys or dedup) of an `update` build the
     first member grounded in a new or revised document of this build wins over members that are
     not (`in_scope_dirty_wins`); when no such member gives a value, the persisted node's value stays
     if a member gives it, so it does not flip back in a later build. `first` and `rebuild` builds
     keep the first value. Members are not ordered by revision date.
   - After dedup, open questions answered elsewhere in the corpus (another document, or another part of their own) are dropped (Answered elsewhere, below).
3. Add `implied` edges from `ref` attributes the ontology `derive`s. A reference resolves to a node of
   the rule's target labels by normalised name or explicit id; when none matches, to the one hub node
   of those labels whose name starts with the reference's words, else the one whose name holds them
   as whole words (a reference "Stripe" reaches the interface "Stripe API"), a leading article
   dropped; with several such nodes it resolves to none, and it is not tried when the reference is
   the exact name of a hub of any label. Each of the two tiers (starts, then holds) first keeps only
   the hubs named (their name or a `surface` alias, as `MENTIONED_IN` counts) in one of the source
   node's evidence fragments or in their heading path (a heading that is a part's own name counts);
   an emptied tier passes to the next, so "API" never reaches the module "API client" from a fragment
   that does not name it, and reaches a named "SpecFlow API" instead. Resolutions this way are counted
   (`derive_fallback_matches`). Hub names are counted over the corpus once, here, and reused in 7.
4. Legalise each edge: keep a legal relation; flip a reversed one; use the pair's default relation
   (when a label pair has several, the one whose `where` the ends' attributes meet; else the first
   declared, and normalisation then applies its `where`); honour `cue_required`; quarantine the rest
   (`unknown_relation`, `illegal_pair`, `cue_missing`), counted per relation as `dropped_edges`.
   Self-loops are dropped and counted. A `symmetric` edge is put in canonical order. Edges with the
   same edge key then merge: their evidence fragments are joined and the strongest basis wins
   (`stated`, then `implied`).

   Stated edges own first: legalisation runs in two phases, the `stated` edges, then the `implied`
   edges of `derive`. An implied edge of relation R from node N is dropped when it would join the
   conflict group of a kept stated R edge of N (any `owner` pair of R; the same `exclusive` pair) to
   another target (counted `derive_suppressed_by_edge`), so a reference attribute is only a fallback: a requirement stated
   `PART_OF` a feature takes no derived `PART_OF` its module, an interface stated `PROVIDED_BY` a
   module takes no derived provider. Implied edges on other pairs stay.
5. Keep a cross-unit edge only when both endpoints are grounded (`locality: grounded`), or only edges
   inside one unit (`locality: strict`); the others are quarantined (`locality`).
6. Normalise (below).
7. Build statements; a hub node's come only from evidence fragments that name it (as `MENTIONED_IN`
   counts), its quote lines there, else the first line naming it. Set `MENTIONED_IN` where a hub
   node's name occurs in a fragment it is not grounded in (counted on word tokens, longest name
   first). Compute each node's search `embedding` from its label, name and `stmt`.
8. Check the target graph against the invariants D1-D4, D6 and D7 (D6 and D7 with the shared
   validator, [analysis.md](analysis.md)); a violation raises `ConformanceError`.

**Foreign edges.** Each raw edge with an end from another unit's catalog part (`mentioned`, `heading`,
`near`, `referenced`; Extract edges) gets one outcome: `kept`, `owner_conflict` (lost an owner or
exclusive conflict), `quarantined` (counted as `quarantined:<reason>`) or `dropped` (otherwise:
redundant, suppressed, unsupported). Outcomes are counted per entry path (`TargetCounters.foreign_edges`;
`foreign_edges` in [analysis.md](analysis.md)); TEST.4 R0 dropped nearness by these counts.

#### Normalisation

Normalisation reshapes the legal graph by the ontology's flags, deterministically given the judge's
verdicts. Attribute defaults are stored first; then the stages run in this order (stage 1 only when
the ontology declares an `owner` pair or a `conform` section; an ontology with neither keeps its
`exclusive` conflicts counted only):

| # | Stage | Effect |
|---|---|---|
| 1 | Owner and exclusive conflicts | Per source, the targets on its `owner` pairs form one group and the targets of each `exclusive` pair another; `exclusive_conflicts` counts every target past the first. The winner is a target whose pair conditions (`where`, `not_across`) hold when any does (else stage 2 handles the winner), then the lowest `owner_rank`, then `stated` over `implied`, then more evidence fragments, then the lowest identity key (the judge never picks the winner). A loser that is the winner's own owner is dropped (`redundant`). When the winner has no owner, the loser becomes the fallback relation if legal for its ends (a hint). A loser with no legal fallback relation and no default pair for its ends is quarantined `owner_conflict`, without a judge call. A module → module loser becomes its pair's default relation, else is quarantined `owner_conflict`. Any other group gets one judge call: `relates` → the fallback relation; `unrelated` → quarantined `judge_rejected`; a failed call, a verdict below `floor` or a group past the `cap` → the fallback relation |
| 2 | Conditions | An edge whose `where` fails becomes the pair's `otherwise` relation when that is legal for its ends, else is quarantined with the pair's `where_reason`. An edge whose `not_across` fails is quarantined with the pair's `where_reason` (`otherwise` never applies to it) |
| 3 | Containment depth | On a pair with `max_depth`, the deepest edge of a longer chain is quarantined `depth_cut` |
| 4 | Redundant via owner | An edge on a `redundant_via_owner` pair is dropped (`redundant`) when the source's owner has the same relation to the same target, judged on the edges as the stage finds them (along an owner chain every such edge goes, whatever the order). A stated edge of the fallback relation is never pruned |
| 5 | Distinct owner | An edge on a `distinct_owner` pair between two nodes of one owner is dropped (`redundant`) |
| 6 | Cycles | Per cycle of an `acyclic` relation, the weakest edge (`inferred` < `implied` < `stated`, then fewer evidence fragments, then the lowest edge key) becomes the fallback relation when legal for its ends, decided by the judge as in stage 1, else is quarantined `cycle_broken` (no judge call when no fallback is legal, as for features) |
| 7 | Unsupported nodes | A node of a type with `supported_by` and none of those edges is dropped with its edges (counted `unsupported`) |
| 8 | Derived edges | `derive_via` adds `implied` edges consumer module → provider module through a shared hub (merged with a stated edge; stated wins); `id_references` adds `implied` edges, only between nodes with different owners (a node without an owner shares none). Both only on a declared pair whose conditions hold, and under the `locality` option |

Edges normalisation drops are counted per relation and reason in `dropped_edges`: the quarantine
reasons, `redundant`, and `unsupported` for the edges of a dropped node.

The judge is the ontology's `conform.judge_prompt`, run through the core judge runner on the `judge`
class. One call per group: it sees the source, the kept winner (absent for a cycle break) and every loser
with their evidence (at most 8 fragments: one per loser first, then the winner's, then the rest), and answers `relates` (citing a quote found in the evidence) or `unrelated` for
each loser. Quotes are checked for `relates` answers only; an invalid quote turns that answer into the
fallback relation. Verdicts are stored in the
`ExtractionStore` (kind `decisions`, key `<project_id>|conform|<input hash>`, valid for the same
prompt and judge model) and reused without a call; the cap counts fresh calls only. Groups are judged
in a fixed order, so the same input gives the same graph. `owner_conflicts_judged` and
`conform_judge_calls` count the groups sent to the judge (failed calls and verdicts below the floor
included); a stored verdict counts as reused.

#### Answered elsewhere

When the ontology declares `conform.answered_elsewhere` ([ontology.md](ontology.md)), Conform checks
the nodes of its `label` (shipped: `OpenQuestion`) after dedup, before derived edges and
normalisation:

| Part | Rule |
|---|---|
| Candidates | The `candidates` (4) fragments other than the node's evidence fragments, the fragment on each side of each in plan order within the same document, and the fragments of that document under the same heading path as one of them (its own section or table, which restates its own default; TEST.4b) (the question's own context; so a later part of the question's own document or unit can answer it) with the highest cosine between the node's search text (label, name, `stmt`) and the fragment vector, at least `min_cosine` (0.35); vectors come from the vector store. A node without a candidate is not judged. Set from TEST.4 v24 stitch, offline from the stored vectors: the labelled answering fragment of 10 answered questions has cosine .30-.50, inside the range of the other fragments (no floor separates them), but ranks 1-4 for 7 of them; 0.5 with 3 candidates reached none of the 10, 0.35 with 4 reaches 6 (0.40: 5 with 36 of 48 questions called; 0.30: 7 with 47), calling 43 of 48 stitch questions and 10 of 14 specflow ones. A keyword path (shared rare words) was measured and not added: it reached one more question only as a second keyword candidate |
| Call | One small-model call per node (the flag's `prompt`, shipped `judge_answered_elsewhere.3`; the question's name and quote, its `section` (the heading path of its first evidence fragment) and, when its quote is a row of a table there, that table's header row (`columns`), and the candidate fragments in an untrusted block). A default, assumption or fallback that applies while the question is open, or a passage restating it, is no answer. Answer: `answered`, `partly` or `open`, the fragment, a verbatim quote and a confidence |
| Drop | `answered` with confidence at least `floor` (0.8) and the quote found in the cited candidate fragment: the node and its edges are dropped and listed (`TargetGraph.answered_elsewhere`: key, fragment, quote). A node that is a row of a table with a default column (a header naming a default, assumption or fallback, such as "Default if unanswered": the specification lists it as open with what applies meanwhile) is dropped only when the quote names the row's first cell (its id); else it is kept (logged `kept_open`). TEST.4b: the judge took passages restating an open issue's default for its answer (5 of 6 specflow drops), with or without the prompt rule |
| Keep | `partly` (counted `oq_partly_kept`), `open`, a confidence below the floor, an invalid quote, a failed call, or a node past the `cap` (60 fresh calls per build; judged in a later build) |
| Memory | Verdicts are stored (`ExtractionStore` kind `decisions`, key `<project_id>\|answered\|<identity key>`, fingerprint `<prompt version>\|<small model>`) with an evidence hash in the payload over the node's key, name, quote and context (section, table header, row id and default) and the content hashes of its candidate fragments, so an edit to the question or to any candidate expires it. The cited fragment is stored by content hash, so a verdict survives a fragment's new id. A stored verdict whose hash matches replaces the call and does not count against the cap |

Counted: `oq_answer_judged` (fresh and reused verdicts), `oq_answered_dropped`, `oq_partly_kept`. A
dropped node that was persisted is deleted by Diff like any other node without a target.

#### Quarantine

An edge that Conform or inference cannot keep is listed in the build result's `quarantine` and
stored only in the `BuildStore`, never in Neo4j. An entry holds: reason, relation, source and target
label, source and target `spec_id` (never identity keys, which may hold document text), basis and
evidence fragment ids. Conform runs before Diff gives `spec_id`s, so its artifact names the ends by
identity key; the build result gives each end the `spec_id` the build's diff (else the `spec_id`
registry) holds for that key, and none when there is none (the build stopped before Diff).

| Reason | When |
|---|---|
| `unknown_relation` | The extracted relation is not declared |
| `illegal_pair` | No legal, flipped or default relation for the pair |
| `cue_missing` | A `cue_required` pair without a matching cue |
| `locality` | A cross-unit edge the `locality` option does not keep |
| `condition_failed` | A `where` or `not_across` failed and no `otherwise` applies |
| `external_owner` | As `condition_failed`, on a pair whose `where_reason` names it (an owner pair to an external or deployment module) |
| `owner_conflict` | A conflict loser with no legal fallback relation or default |
| `judge_rejected` | The judge answered `unrelated` for a conflict loser |
| `depth_cut` | Deeper than the pair's `max_depth` |
| `cycle_broken` | The weakest edge of a cycle, with no legal fallback |
| `invalid_proposal` | An inference proposal that fails the shared validator ([analysis.md](analysis.md)) |

Counted only, never quarantined: self-loops, redundant edges and unsupported nodes.

### Diff

Match target nodes to current nodes by identity key and identity aliases:

| Case | Result |
|---|---|
| One target, one current node | Update in place |
| Several current nodes, one target | Merge: a node of the target's label survives (so its `spec_id` prefix names the label it keeps), of those the oldest; the others' `spec_id`s become aliases; their foreign relationships move to it |
| One current node, several targets | Split: the node continues as its best match; the others are new |
| Current node without a target, new target in the same scope with a matching name and overlapping evidence lines | Carry-over: the new target takes the node's `uuid`, `spec_id` and relationships; the node's name updates to the target's, the old key stays as an identity alias |
| Current node without a target | Delete |
| Target without a current node | Create, with a new `spec_id` |

Each `modified` node counts why it changed (`DiffArtifact.modified_causes`): each build-owned property
whose value changes, by name (`updated_at` and `last_build_id`, set with any change, left out),
`vectors` (a vector's text changed), `links` (its evidence lines or `MENTIONED_IN` counts changed) and
`merged` (other nodes merged into it); a node counts once under each of its causes. The count only
reports; it changes nothing.

Each domain edge the diff creates, deletes or updates is listed in `DiffArtifact.edge_changes`:
`<source spec_id>|<RELATION>|<target spec_id>`, the change (`added`, `removed`, `changed`), the basis,
the rule of an inferred edge and, for a `changed` one, its `causes` (the edge properties whose value
changed: `basis`, `fact`, `source_fragment_ids`; `ends` when an end moved to another node), for the
change report (Change report). A created or moved edge is named by its ends after the diff (a moved
one, whose end was merged, is `changed` under its new ends), a deleted one by its ends before; the
inferred edges the diff deletes or turns stated are listed too. An update that only rewrites the edge
key (an end rekeyed or carried over) is no change. An artifact written before the list has none;
offline classification then names the ends of its edge operations through the `SpecIdRegistry`.

Edges match by edge key. Inferred edges are outside the diff: they change only when inference replaces
them, when an endpoint is deleted, or when extraction now states the same edge (it becomes `stated` in
place). The diff and the build's spec changes (`added`, `removed`, `modified`,
`rekeyed`, `carried_over`) are stored before anything is written.

A continuing node keeps its persisted label; its vectors are computed from its texts under that label
(embedded by Persist when they differ from the target's), and a created node that Conform relabeled
carries `label_disagreement`, so the next build sees no change.

### Persist

The diff is applied in batches, each its own idempotent transaction, in this order: removed edges,
merges, updates and carry-overs, node deletes, creates, edges and structural relationships, then
documents and fragments. A cursor records the last applied batch, so an interrupted persist resumes
there. While persist runs, readers may see a partly applied diff.

## Reuse in update builds

| Result | Reused when | Stored in |
|---|---|---|
| Nodes of a fragment, per node pass (by the node call's attribution), with their quotes and (hubs) aliases | Same lineage, same fragment content, same heading path (the breadcrumb the prompt shows), same extractor fingerprint; stored only when every pass answered the fragment, and read only when every pass's nodes are stored. A raw node on several fragments is reused once, on its reused fragments. Reused hub-pass nodes join the hub catalog | `ExtractionStore` |
| Edges of an edge window | Same window content (its fragments' content hashes), same window catalog keys, same extractor fingerprint; ends mapped by identity key or alias. A unit with no changed fragment keeps its earlier nearness hubs; a failed window falls back to the stored edges of the same content, else of the same document and first fragment, clipped to the window, the fragments that answer did not hold left to Diff as `edge_failed_fragments` (Extract edges) | `ExtractionStore` |
| Attributes of a node | Same identity key and label, same evidence fragment contents (for a hub, those naming it), same extractor fingerprint; a node supported only by the last extracted version of a skipped fragment is never asked again (its stored values are kept) | `ExtractionStore` |
| Embeddings (node and fragment vectors) | Same text and embedding model | `ExtractionStore`, once per distinct text: kind `vectors`, key `<project_id>\|<embedding model>\|<sha256 of the text>` |
| Embeddings (candidate vectors of hubs and id-less requirements) | As above; used only to find candidate pairs, never written to Neo4j | `ExtractionStore`, kind `vectors`, as above |
| Hub consolidation decisions (merged and not merged pairs, seen items) | Same project, the same identity keys, same prompt version and judge model, and the same evidence hash (an edit that changes the item line of either hub expires it; Resolve 3) | `ExtractionStore`: kind `decisions`, key `<project_id>\|hubs\|<identity key>\|<identity key>` (node entries `<project_id>\|hubs.node\|<identity key>`; twin decisions `<project_id>\|hubs.twin\|<identity key>\|<identity key>`) |
| Dedup verdicts | Same project, the same two identity keys, same prompt version and small model, and the same evidence hash (a changed name, `stmt` or source quote of either side expires it; Conform 2) | `ExtractionStore`: kind `decisions`, key `<project_id>\|dedup\|<identity key>\|<identity key>` |
| Conform judge verdicts | Same project and judge input (hash of the payload), same prompt and judge model | `ExtractionStore`: kind `decisions`, key `<project_id>\|conform\|<input hash>` |
| Answered-elsewhere verdicts | Same project, node key, name and quote, same candidate fragment contents (the evidence hash), same prompt and small model (Conform, Answered elsewhere) | `ExtractionStore`: kind `decisions`, key `<project_id>\|answered\|<identity key>` |
| Inference judgements (and cross-document subjects' members) | Same project, rule, subject and judgement input (payload less node statements and sections, shown nodes by identity key, label, name and quote, an `attach_orphan` subject's candidates as a set, fragments by content hash, the read edges behind each candidate of a rule with `reads` and not reached without them, prompt, judge model; a batched `attach_orphan` subject under its single call's input); fresh subjects are judged under the rule's limits, changed ones first, then the queue, then never-judged ones; in an `update` whose diff changed something an `INF-XDOC-01` subject is judged only when its members changed (created, modified or rekeyed), joined or left since the last build (a never-judged one only when a member joined), then its queue. Judgements and `seen` keys are written every 100 completed calls before the swap, with the build's completed inference input, `members` keys after it ([analysis.md](analysis.md) Limits and backlog) | `ExtractionStore`: kind `decisions`, keys `<project_id>\|<rule id>\|<subject spec_id>\|<input hash>`, `...\|seen` and `...\|members`; the input: `<project_id>\|budget\|<build id>`. The queues: the infer artifact (`InferenceReport.backlog`) in the `BuildStore` |

Every reuse key includes the project id. Build artifacts hold texts or hashes, never vectors.

The extractor fingerprint covers what shapes an extraction call: the ontology parts that reach the
prompt, the build options that shape it, the prompt name with its version (`nodes.hubs.14` and `nodes.statements.15`
for the two node passes, `edges.8`, `attributes.7`), the sha256 of the rendered system message(s) (for a node
pass with its pass line; one node fingerprint covers both passes) and the model. The edge fingerprint
also takes the internal edge window parameters (target size, most windows, nearness, section hints, references). The node fingerprint
takes only the options that shape windows and known terms (`fragment_tokens`, `unit_tokens`,
`max_node_windows`, `hub_catalog`, `known_terms_limit`), the edge fingerprint only `fragment_tokens`,
`unit_tokens` and `catalog_limit`, and the attributes fingerprint none. No third-party library version is part of
it. A `rebuild` extracts everything again,
reuses no stored extraction result, and Resolve and Conform keep no persisted label (a node Diff
matches still keeps its label); it reuses only embedding vectors and decisions (hub consolidation decisions, dedup verdicts, conform
judge verdicts, answered-elsewhere verdicts, inference judgements), so identities stay stable. An update
build's graph may differ slightly from a rebuild of the same corpus; that is accepted.

## Failures

| Failure | Effect |
|---|---|
| An extraction call fails after its retries (malformed output, refusal, overflow) | Its dirty fragments become `skipped` with an error code; a changed fragment keeps its last extracted version as support; the build ends `partially_succeeded`. A failed edge window first falls back to stored edges (Extract edges) |
| An answered-elsewhere call fails | The node is kept; counted; the build continues |
| The LLM endpoint or Neo4j is unreachable, or the endpoint rejects the credentials, the model or the request ([architecture.md](architecture.md) "Errors") | The step raises `InfrastructureUnavailableError` or `LLMFatalError`; nothing is marked skipped; the build is `failed` |
| A write fails | `GraphWriteError`; persist resumes from its cursor |
| A conform judge call fails | The loser gets the fallback relation; counted; the build continues |
| A judge call fails, a verdict is invalid, or a non-fatal model error stops an inference rule | Reported in the inference report; the build still succeeds. Any other inference error fails the build, and the previous inferred edges stay ([analysis.md](analysis.md)). A failed judgement's subject goes to the head of the rule's queue |
| A follow-up build fails | As any build: it is finished `failed` and `run_build` raises; the builds before it stay finished |

Skipped fragments are extracted again when their content changes, the host asks for a retry, or
`run_build` starts a follow-up build (Follow-up builds and settlement).

## Build result

| Field | Meaning |
|---|---|
| `status` | `failed` when a step raised; `partially_succeeded` when fragments were skipped; else `succeeded`. Verification does not change it. |
| `settled` | Persist completed and verification requested no follow-up build; its meaning is unchanged by `settlement` (the API exposes it) |
| `settlement` | Whether the project is complete after the build and its follow-ups: state, reasons, documents, inference backlog, follow-up builds (Follow-up builds and settlement); stored in the `BuildStore` only; none for a failed build |
| `change_report` | The build's changes classified per node and edge (Change report); stored in the `BuildStore` only; none for a failed build |
| `spec_changes` | `added`, `removed`, `modified`, `rekeyed`, `carried_over` spec_ids; `inferred_added` and `inferred_removed` edges as `<source spec_id>\|<RELATION>\|<target spec_id>`, never as edge keys (NFR-SEC-01). Nodes and inferred edges deleted by verification repairs are listed as removed |
| `quarantine` | Edges Conform and inference could not keep, with their reasons (Quarantine); stored in the `BuildStore` only |
| `dedup_refused` | Requirement pairs a pairwise dedup guard kept apart (Conform 2): `first_spec_id`, `second_spec_id` (as for `quarantine`: none when the key has no `spec_id`), `label`, `guard` (`numbers`, `negation`, `modality`, `ids`, `same_sentence`), `name_cosine`, `rich_cosine` (candidate vectors), `collapsed` (both ends ended in one node; both `spec_id`s are that node's); never identity keys; stored in the `BuildStore` only |
| `answered_elsewhere` | Nodes Conform dropped as answered elsewhere in the corpus (Answered elsewhere): `spec_id` (none for a node never persisted), `fragment_id`, `quote`; never identity keys; stored in the `BuildStore` only |
| `verification` | `ok`, problems found and left per check, repairs, repair rounds applied and blocked by the valve, follow-up lineages and the verification metrics ([analysis.md](analysis.md); `owners_by_source` and `cross_document_edges` measured on the persisted graph after inference) |
| `metrics` | Fragments by status, units extracted and reused, LLM calls, input, output and cached input tokens per step (`llm`), node `attribution_fallbacks` (window fallbacks), `attribute_batches_failed`, nodes and edges by type, dropped edges, `exclusive_conflicts`, `spine_unreachable`, `quarantine_by_reason`, `quarantine_by_pair`, `conform_judge_calls` (fresh conform judge calls), `dedup_refused_by_guard` (`dedup_refused` per guard; none when the conform artifact records no list), `judge_calls` and `judge_failed_calls` per judging step (`hub_consolidation` with the twin calls, `requirement_dedup`, `conform_judge`, `answered_check`, `inference`; a host running the steps itself records them the same way, `StepUsage`, so `examples/graph_eval.py` results carry `llm`, `judge_calls` and `judge_failed_calls` too; a step whose failed calls exceed 10% of its calls logs the WARNING `graph.build.judge_failures`, never failing the build), duration per step. The full consolidated metric set, with the identity metrics (hub merges by source, pair pass, unsure, expired and kept-apart decisions, dedup candidates, verdicts and refusals), is in the verification metrics ([analysis.md](analysis.md)) |

Metrics are reported, never used as gates.

## Follow-up builds and settlement

A build can leave the project incomplete: fragments skipped by failed extraction calls, a
verification follow-up (`fragment_state`), an inference backlog or a failed inference rule.

`run_build` starts a follow-up build when a build finishes without raising and has retry lineages:
the verification's `follow_up_lineages` and the lineages with `skipped` fragments (skipped fragments
count too) that hold a retryable code. A skipped fragment's error code is not
retryable when the same content fails the same way again: `llm_refused` (content filter or refusal),
`llm_truncated` (still cut after halving its window four times, or a one-fragment window),
`llm_malformed` (still malformed after the repair request) and `llm_overflow` (the request exceeds the
model's context). `llm_exhausted` (retries spent on rate limits, server errors or timeouts), a
verification follow-up (`fragment_state`) and any other code are retryable. The follow-up is an
`update` with those lineages to retry, `rebuild` off, and the same corpus, options and settings; its
`BuildRecord` names the chain's first build (`follow_up_of`) and its place in the chain
(`follow_up_attempt`, 1 or 2). The chain stops when a follow-up recovered nothing: the same documents
and the same skipped fragments, with the same codes, as the build before it. At most `MAX_FOLLOW_UPS`
(2) follow-ups run per call. A lineage that keeps failing, or fails with a code that is not retryable,
stays in `settlement.documents` with its codes (`document_codes`), so a fragment that fails the same way
in every build costs at most one follow-up per build. An inference backlog alone starts none: every
later build drains it. `run_build` returns the last build's result. Steps run one by one start no
follow-up; a host that runs them finishes each build through `run_build`, which starts them
(`examples/graph_eval.py` does). A finished build that is the project's latest build (its process
stopped after it finished, before its follow-up started) starts the follow-up when `run_build` resumes
it; a resumed follow-up continues its chain. Follow-ups inside `run_build`, not in the host or a runner,
are decided.

`BuildResult.settlement`, a new field (the concept's "not settled" is not a change
of `settled`):

| Field | Meaning |
|---|---|
| `state` | `settled` when no reason holds, else `not_settled` |
| `reasons` | `documents_incomplete` (retry lineages remain), `inference_backlog` (a rule's queue is not empty), `inference_rule_failed` (a rule reported an `error_code`) |
| `documents` | Lineage ids still holding skipped fragments or verification follow-up lineages after the chain's last build |
| `document_codes` | Each of `documents` with its codes, sorted: its skipped fragments' error codes and `fragment_state` for a verification follow-up lineage |
| `inference_backlog` | Queued subjects per rule ([analysis.md](analysis.md) Limits and backlog) |
| `follow_up_builds` | The ids of the follow-up builds `run_build` chained after the first build, in order (at most 2); a resumed follow-up lists the whole chain, read from the latest finished build of the chain (`BuildRecord.follow_up_of`) |

A build can be `settled` (nothing to retry) and `not_settled` in `settlement` (an inference backlog).

## Building status

`await graph.build.status.project_status(builds, project_id) -> ProjectBuildStatus` reads the `BuildStore`
only: `building` (the project's latest build record is running), its `build_id`, `mode`, current
`step`, `started_at` and `follow_up_attempt` (0: no follow-up), and the `settlement` of the latest
finished build. There is a short gap between a build and its follow-up with no running record, so a
reader may see `building` false just before a follow-up starts; a record whose process died stays running, and judging it stale
is the host's call. Blocking readers while a project builds is the host's; the graph side is this
helper only.

## Change report

`BuildResult.change_report` classifies the build's changes from its stored diff, inference and
verification artifacts. It is a pure function of them, so a stored build can be classified offline.
Nodes are named by `spec_id`, edges as `<source spec_id>|<RELATION>|<target spec_id>` (NFR-SEC-01).

| Node class | The node |
|---|---|
| `added` | Was created |
| `removed` | Was deleted, by the diff or a verification repair |
| `merged` | Survived a merge, whether or not its own properties changed; its `previous_spec_ids` are the merged-away `spec_id`s |
| `carried_over` | Was carried over to a new identity key and name (Diff) |
| `rekeyed` | Kept its `spec_id` under a new identity key |
| `changed_meaning` | Changed its `name` or an ontology attribute of its label other than `quote` (attribute names read from the ontology; its statement `stmt` only together with its `quote` or `name`), had its label set by a verification repair, or, as the source of an `owner` pair, gained, lost or changed an edge of that pair (any basis) |
| `changed_links` | Gained, lost or changed another domain edge (any basis), the owner end of an owner-pair edge included |
| `changed_evidence` | Changed only evidence: its `quote`, statements, evidence lines or mention counts, its vectors or another build-owned property, or its `stmt` with the same `quote` and `name` (a re-extraction's paraphrase of the same source text); or is an end of an edge whose `fact` or source fragments only changed |

A node gets every class that holds, with its diff `causes` (as `modified_causes` counts them); an
added or removed node gets no other class, and a node a repair deleted is `removed`, not `added`.
Evidence lines that only move make `changed_evidence`, never `changed_meaning`; nor does a `stmt`
reworded over an unchanged quote. Each edge entry holds
its reference, `change` (`added`, `removed`, `changed`), `basis`, `causes` and, for an inferred edge,
its rule (from `DiffArtifact.edge_changes`, the inference report, with the rule of an accepted judged
item, and the verification repairs); an inferred edge the build added and a repair deleted is not
listed. `counts` gives nodes per class, `edge_counts` edges per change.

Besides the artifacts, `classify` reads the `SpecIdRegistry` rows: a node whose evidence lines alone
changed is named only by its uuid. A node's label is the one its diff operations give, else the label
whose `spec_id` prefix it has. For an artifact without `edge_changes` the edge ends come from the
identity keys of the edge keys and the end uuids, through the registry and the diff's own rows; a
deleted edge then reads `stated` unless the diff lists it as an inferred edge removed, and its rule is
unknown. `examples/graph_report.py` classifies a stored result written before change reports this way.

## Migration

A change of the ontology's labels or relations (a major version, such as 1.3 to 2.0) cannot be
applied by an `update` build: it raises `ConfigurationError("ontology_changed")` (`part`: `labels` or
`relations`) before any model call or write, and the graph stays as it was. The host migrates the
project:

1. Drop the project's graph: delete every node whose `group_id` is the project id, with its
   relationships, in batches until none is left
   (`MATCH (n {group_id: $project}) WITH n LIMIT 2000 DETACH DELETE n`, as
   `examples/graph_eval.py --cleanup` does). This drops the documents, fragments and spec nodes, and
   whatever another owner (gap, plan) wrote for the project.
2. Run a build with the new ontology and without the `rebuild` flag. With no persisted graph the
   library picks `first` and builds everything under the new ontology.

The file stores stay; none needs clearing:

| Store | After the drop |
|---|---|
| `ExtractionStore` | Stored extraction results are not reused: `first` reads none, and their fingerprint changed. Embedding vectors (same text and model), hub consolidation decisions and dedup verdicts (same identity keys and evidence hash), conform judge verdicts (same input), answered-elsewhere verdicts and inference judgements (same evidence or input hash) are reused |
| `BuildStore` | The old build records stay; the new `first` record freezes the new ontology and options for later updates |
| `SpecIdRegistry` | Keeps each identity key's `spec_id`, so a node with the same identity key under the new ontology keeps its `spec_id` |
| Corpus index | Document versions continue from the old ones |

A shop project built under 1.3 and migrated this way gives the graph of a clean 2.0 `first` build of
the same corpus: the same nodes, labels, properties, edges and evidence, no 1.3 node or relation,
and verification `ok` (`tests/test_graph_migration_live.py`).

A `rebuild` is not a migration. Resolve and Conform type every node again, but Diff continues a
persisted node that shares an identity key with a target, and a continuing node keeps its persisted
label (Persist never relabels a node). A hub's identity key includes its label, so a retyped hub is
deleted and created again. Requirements share one identity scope (`requirement|span|...`), so a
requirement the new ontology types differently keeps its old label and `spec_id` and only gets
`label_disagreement`; the build still succeeds and verifies. Diff also reads only the relations of
the new ontology, so edges of a relation the new ontology drops would stay. On the shop corpus no
continuing node changed its label, so a `rebuild` over the 1.3 graph happened to give the clean 2.0
graph (plus the old corpus's retired fragments and removed documents); that holds for no corpus in
general.

## Cost estimate

`estimate_build` runs the Plan step and the reuse lookups without any model call or write, and returns:
the mode, the change set, fragments by change and by status, dirty fragments, units to extract and
units reused, the expected calls and input tokens of eleven steps: hub catalog, node extraction, hub
consolidation, edge extraction, attributes, requirement dedup, conform judge, answered check,
inference, cross-document inference and embeddings, and the subjects per inference rule its limits
would leave queued (`inference_backlog`).

| Aspect | Rule |
|---|---|
| Tokens | Prompts are rendered by the build's own message builders (the node and edge calls' messages are those the build sends) and counted at about 3.6 characters per token; input tokens are totals: the share the provider serves from its prompt cache is not predicted |
| Node extraction | Two calls per window with a dirty fragment (hub pass and statement pass), plus per pass one warm-up call (system and unit message) per unit with two or more such windows, rendered by the build's message builders; the hub catalog step is always 0 calls (the hub pass is the catalog). Known terms are selected as the build selects them, with the vectors the store holds (a text without one is found by mention only): the hub pass of an `update` is shown the persisted hubs; the statement pass the persisted hubs as a stand-in for the merged catalog (`update`, `rebuild`), or in a `first` build the expected hubs of the unit's fragments as mentioned terms, then up to `known_terms_limit` expected hubs of other units as nearby stand-ins, every third with one placeholder alias; with `hub_catalog` off, what the hub pass is shown |
| Edge extraction | Per unit with a dirty fragment, its edge windows cut as the build cuts them (`edge_windows` with `DEFAULT_EDGE_WINDOWS`): every window in a `first` or `rebuild` build, only the windows holding a dirty fragment in an `update`; one call each, plus one warm-up per unit calling two or more windows, rendered by the build's message builders. Every window of the unit has its expected catalog: its expected nodes, then hubs from elsewhere (the persisted hubs, or in a `first` build placeholders), at most `catalog_limit` and 20 per fragment, then with `references` on the referenced entries the build's `ReferenceFinder` finds over the expected nodes (at most 30; explicit ids not predicted, expected nodes carry none), each with its referring sentence and its expected name standing in for its statement; a unit without an expected node of its own, or with fewer than two in all, makes no call |
| Expected nodes | A fragment with a persisted version gives back the persisted nodes grounded in it; any other gives 20 nodes per 1,000 tokens (10% hubs) |
| Hub consolidation | Per label part, the expected hubs in blocks of 120 overlapping by 10; a part whose hubs all have a node entry in the decision memory and are grounded only in unchanged fragments sends nothing (a changed fragment counts as expiring the entry, though only a changed item line does), else at most one block per unseen hub. Each item is priced as the judge shows it: the persisted node's quote and evidence where it has them, else placeholders, with a heading and 3 requirements. Plus the pair pass and the cross-label twin calls: round(0.15 × unseen hubs of the parts cut into more than one block) calls (a part of one block holds every candidate pair in its block), at most 60, each priced as a batch of 8 placeholder items, the mean a real batch shows (specflow's one multi-block part, 251 hubs: TEST.3 R1 28 pair batches and 2 twin calls, 0.12; TEST.3b 43 and 3, 0.18; stitch 0) |
| Reuse | As the build reuses: stored vectors and merge decisions, and in an `update` build stored edges and attributes |
| Dedup | Judged pairs (the name and candidate-vector-only candidates the guards and the direct-merge rule leave to the small model): round(0.04 × new id-less requirements × √(id-less requirements in all, new and persisted, / 1,000)); a larger pool holds more near restatements per requirement (calls grow about as pool^1.5; TEST.3b first builds: specflow 2,828 requirements 190 estimated / 194 real, stitch 443 12 / 11); pairs of two persisted requirements cost nothing |
| Embeddings | The texts each step embeds that the store does not hold, including the candidate texts of the hubs and of the id-less requirements in a dedup group of two or more; a persisted node grounded only in unchanged fragments has its candidate text stored; any other node's is a placeholder, so it counts as new |
| Conform judge | A fixed rate of judged conflict groups per requirement, measured on the test corpus, at most the ontology's `conform.cap`; priced with the rendered owner-conflict judge prompt |
| Answered check | 0.25 calls per new expected node of the `conform.answered_elsewhere` label (shipped `OpenQuestion`; provisional, calibrated at TEST.4), at most the flag's `cap`; none without the flag. Priced as the check sends it: a placeholder question and quote and the flag's `candidates` fragments of the dirty fragments' mean length |
| Inference | Rules other than `cross_document`. Fresh subjects per rule: in `first` and `rebuild`, a rate per expected node of the rule's subject labels (a placeholder counts as its kind's share of them: hub or not), measured on the TEST.4b v24b builds: `attach_orphan` 0.36 × √(nodes / 1,000) per expected requirement (the share grows with the pool; specflow 1,359 for 1,345 measured, stitch 84 for 86) and 2.0 per expected hub (130 for 105, 20 for 25), `endpoints` 0.04, `realization` 0.05; in an `update` with a dirty fragment, the rule's persisted queue (exact: `InferenceReport.backlog` of the latest finished build) plus the subjects judged before whose inputs change: those of the previous report (its subjects less deferred) times 2.1 (requirement subjects) or 7.5 (hub subjects) × the share of the rule's target nodes grounded in or mentioned in a dirty fragment, at most all (a re-extracted target may be grounded anew in the changed fragment, so its quote and shown fragment change, and its new vector re-ranks candidates; TEST.5 v25 updates after a full drain, with the stable input hash and less the subjects two wrong hub merges changed: specflow 147 of 1,321 requirements for 14 of 228 targets, stitch 47 of 90 for 13 of 66; features 32 of 83 and 19 of 26), plus the new subjects among new expected nodes (the `first` rates); without that count, 1.5 per expected subject node of a dirty fragment; hub merges of the build are not predicted; in an `update` without one (its diff is taken to change nothing), the queue plus the previous build's subjects deferred outside its queue (the never-judged ones it drains; an upper bound). The infer step's own limit function cuts them (`cap`; `first_cap` and `first_budget` in `first` and `rebuild`, the budget counted across the rules in ontology order), so the subjects past the limits are the predicted queue (`inference_backlog`). Each call is priced as the judge sends it (`judge_messages` over a placeholder payload of the template's shape: the subject with one fragment and `max_candidates` candidates, `min_targets` targets with every pair, or one member and one candidate; every other node with one fragment; fragments of the plan's mean length, as a queued subject's fragments lie anywhere in the corpus); a rule with `batch` is priced `batch` subjects per call with its batch prompt, as if every subject shared its top fragment and candidates with others (a lower bound of the calls). Within ±30% (`first`) and ±25% (`update`) of the v24b calls under its limits (its prompts were older) and of the TEST.5 v25 calls and input under the shipped limits (full drain; `update`: specflow 135 calls, 632k for 149, 715k; stitch 65, 306k for 74, 334k) |
| Cross-document inference | `cross_document` rules, as above: in `first` and `rebuild`, 1.65 × √(hubs / 1,000) per expected hub of the subject labels (specflow 220 for 212 measured, stitch 14 for 14); in an `update` with a dirty fragment, 0.7 per subject hub grounded in or mentioned in a dirty fragment (the update scope; specflow 18 for 16, stitch 10 for 12), then the queue; without one, the queue and the never-judged subjects as above. Priced as `infer_xdoc.judgement_parts` builds the payload over a placeholder hub and 12.5 × √(hubs / 1,000) member pairs (at least 1, at most `max_pairs`) of two documents, one fragment each, one orphan member with the hub as its owner candidate (specflow 8 pairs, 17.0k per call: 17.2k measured over its 36 largest hubs; stitch 3 pairs, 8.5k: 8.7k over all 14). Measured runs used `judge_cross_document.3`; the shipped `.4` adds about 0.4k per call, so the TEST.5 v25 update is checked on its calls only |
| Not estimated | Inference subjects that reuse a stored judgement (every expected subject counts as fresh), the subjects an inference batch leaves alone (different top fragment or candidates), verification, output tokens, truncation splits (and the warm-ups they send), edge calls of a clean window whose window catalog changed, the reuse of a dirty window whose content and window catalog are stored, references by explicit id, a persisted question's expired answered verdict |

The estimate is of a new build: a context with `build_id` set raises
`ConfigurationError("estimate_build_id_set")`.

## Build options

| Option | Default | Meaning |
|---|---|---|
| `fragment_tokens` | 300-3,000, target 2,000 | Fragment size range |
| `hub_catalog` | on | Show the statement pass the hub catalog of the hub pass as known terms, merged with the persisted hubs in an `update` build (off: the persisted hubs only, none in `first` and `rebuild` builds). The hub pass runs either way |
| `known_terms_limit` | 30 | Known terms shown per unit and pass by nearness; mentioned terms are always shown (up to the safety cap of 100) |
| `catalog_limit` | 150 | Hub nodes from elsewhere in an edge catalog per unit (`mentioned`, `heading` and `near` parts; `referenced` requirements come on top, at most 30) |
| `locality` | `grounded` | Cross-unit edge rule |
| `unit_tokens` | 24,000 | Largest unit; a longer document is cut into parts |
| `max_node_windows` | 8 | Node windows per unit at most (two calls each, one per pass) |

The `known_terms_limit` default was 120, a cap on all known terms of a unit; a project frozen with
it raises `frozen_options_changed` on its next `update` and needs a `rebuild`.
