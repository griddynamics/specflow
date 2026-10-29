# Synthetic shop corpus

Made-up "Acme Shop" documents for the knowledge-graph builder tests. Everything here is synthetic:
no real company, person, URL or e-mail address. Snapshot 1 is in `1/`; snapshots 2 and 3 are full copies in `2/` and `3/` that differ only as listed
below (every other byte is identical, so fragment ids of unchanged sections stay stable).

| File | Exercises |
|---|---|
| `1/overview.md` | Modules, external modules, cross-cutting capabilities realised by several modules, architecture calls (hub nodes, PART_OF, REALIZED_BY, INTEGRATES_WITH) |
| `1/checkout.md` | Features, explicit ids `FR-CHK-001..006` (`FR_CHK_006` tests id normalisation), a quality requirement with a target, a business rule, an integration requirement to Stripe, a page-marker comment, two `### Notes` sections with the same heading path and first line (fragment id `~2`) |
| `1/orders.md` | Requirements without ids ("must", "should", "I want"), a data requirement with a field table, an integration requirement to the ERP, the cross-reference "see FR-CHK-002", a long "Export details" section (over 3000 tokens) that forces a split inside one section |
| `1/admin.md` | Requirements of the "Order history export" feature without naming it (linking by meaning), a TBD line, an assumption |
| `1/notes.md` | Low-content meeting notes, a prompt-injection line, several page-marker comments |

## Snapshot 2 (from 1)

Only `checkout.md` changes; `orders.md`, `overview.md`, `admin.md` and `notes.md` are byte-identical to
snapshot 1.

| Change | File, section | Serves |
|---|---|---|
| Edited | `2/checkout.md`, "Features / Payment flow": FR-CHK-004 reworded, one new requirement sentence without id added | Changed fragment (new content hash) while every other fragment is reused; the reworded requirement keeps its explicit id, the new sentence has none |
| Removed | `2/checkout.md`, "Business rules / Manager approval" (heading "Business rules" removed with it; the `source-page: 3` marker stays) | Retired fragment and deletion of the nodes grounded only in it (the business rule) |
| Added | `2/checkout.md`, "Features / Delivery options" (new feature, two new requirements) | New fragment and new nodes (feature plus two requirements) |
| Added | same section: `FR-CHK-007` | Explicit id added in a revision (node keyed by its id); the second new requirement has no id |

## Snapshot 3 (from 2)

| Change | File, section | Serves |
|---|---|---|
| Document removed | `3/admin.md` is absent | Removed document: its fragments retire and nodes grounded only there are deleted, while nodes shared with `orders.md` survive |
| Renamed | "Orders service" becomes "Order management service" on the same lines in `3/overview.md` (heading "Modules / Orders service" plus 8 mention lines and one mention wrapped across two lines in "Authentication"), `3/orders.md` (title "Orders service Requirements" and 5 mentions) and `3/checkout.md` ("Order confirmation", FR-CHK-005). "Orders database" is deliberately not renamed | Rename: carry-over keeps `spec_id` for the module node and moves the relationships (foreign relationship moved); the similarly named "Orders database" stays a separate node |

Nothing else changes between snapshots 2 and 3.
