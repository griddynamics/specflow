# Lens: authorization boundaries

Simulate building this system while asking, for **every single operation**: who
may call this, and on whose data?

Specs usually name roles once and then describe features as though the caller is
always entitled. The gap is per-operation, so check them one at a time.

Work through the spec asking:

- For each operation, which actors may invoke it? Not "logged-in users" —
  which ones, and under what condition?
- Which operations act on a record belonging to someone else? What relationship
  must hold between caller and record? An endpoint that takes an id and does not
  check ownership is the most common real vulnerability in generated code.
- Where does one actor act on behalf of another (admin, support agent,
  automation)? Is that impersonation visible in the audit trail, and are its
  limits stated?
- Which reads are as sensitive as writes? Listing and searching leak data even
  when the caller cannot change anything. Does a list endpoint filter to the
  caller's scope?
- What is visible in an error? "Record not found" versus "not permitted" tells
  an attacker whether the record exists.
- Which fields may the caller set, and which are server-controlled? A caller who
  can write `role` or `price` has an authorization bug, not a validation bug.

## What counts as a blocker here

The spec is missing a decision wherever an operation touches data it does not
prove the caller owns. Also wherever roles are named but their permissions are
not enumerated — "admins can manage users" is a role, not a rule.

## Fill particularly carefully

- `operations[].authorization` — required on every mutating operation. Leaving
  it empty is caught mechanically, so state the actual rule or raise a blocker.
- `entities[].fields` — mark server-controlled fields as `derived` so a
  caller-writable field that should not be shows up as a contradiction.
- `blockers` — one per operation whose ownership rule you had to infer. These
  are cheap to fix in the spec and expensive to discover in production.
