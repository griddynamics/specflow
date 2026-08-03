# SpecFlow oracles

The deterministic half of the refinement loop.

Orchestration is prose — skills spawning subagents, sequencing rounds, deciding
when to ask the user. Everything in this directory is code, because an oracle's
whole value is that it is **not** a language model. "Check the state table is
complete" as an instruction is advisory; a script that exits non-zero on an empty
cell is a forcing function.

## Entry point

One dispatcher, invoked by path from a skill:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/lib/specflow_cli.py" <command> [options]
```

`specflow_cli.py` sits outside the `specflow/` package deliberately: the package
stays a pure library, and the script owns the one fragile thing in a plugin — the
path bootstrap. It works from any working directory.

| Command | Job |
|---|---|
| `new-round` | allocate the next round directory |
| `validate` | schema conformance + totality, nothing else |
| `round` | validate, merge, rank, decide whether to stop — the workhorse |
| `resolve` | record a decision so later rounds stop asking |
| `status` | current state, for reporting |
| `contracts` | model contradictions, and emitted SQL/API cross-checks |
| `mutate` | inject a known defect and verify it gets caught (internal QA) |

Exit codes: `0` success, `1` checks failed, `2` bad usage. The non-zero on
failure is the point — a skill cannot quietly proceed past a gate that did not
pass.

## Modules

| Module | Responsibility |
|---|---|
| `jsonschema_mini.py` | JSON Schema validation, stdlib only |
| `artifacts.py` | on-disk layout and IO |
| `totality.py` | the forcing function — see below |
| `contracts.py` | structural contradictions in the model, and emitted-artifact checks |
| `concordance.py` | cross-lens agreement and located divergence |
| `rank.py` | cost-asymmetry ordering and ask/assume/note disposition |
| `saturation.py` | the stop rule |
| `mutate.py` | ambiguity injection and verification |
| `schema/` | the artifact contracts — source of truth |

## Two policies worth knowing

**Stdlib only.** This ships inside a marketplace plugin and runs on whatever
Python the user has. A `pip install` step turns a working skill into a support
ticket, so there is a hand-written JSON Schema validator instead of the
`jsonschema` package, and API contracts are validated as JSON rather than YAML.

The validator's supported keyword set is closed: anything used in `schema/*.json`
is implemented, and anything unimplemented **raises** rather than passing
silently. A constraint that is quietly ignored is worse than no constraint.

**Nothing here calls a model or the network.** Every count, ranking and verdict is
reproducible from the artifacts on disk. That is what lets the output be treated
as evidence rather than opinion, and it is what makes "nothing leaves the
machine" true of the measurement path and not just the storage.

## Why totality matters most

A real build compels decisions — you cannot run code past a point the spec left
undefined. Simulation has no such compulsion, so an agent asked "what would block
you?" produces a plausible list, not an exhaustive one: it finds the legible gaps
and skips the awkward ones.

`totality.py` restores the compulsion structurally:

1. Every dimension carries a real value, not an evasion (`TBD`, `unknown`,
   `varies` are rejected).
2. Every state × event pair in a lifecycle has an outcome.
3. Every reference resolves to something that exists.
4. **Every escape hatch is paid for with a blocker.**

(4) is the one that closes the loophole. An agent can always write
`inferred: true`, or `outcome: "undefined_in_spec"`, or
`spec_says: "nothing"` — those are legitimate answers, but only if the gap is
also *raised*. Without this check they become a silent way past the hard cells,
which is precisely the failure mode simulation is prone to.

## Testing

```bash
python3 plugins/specflow/lib/tests/test_oracles.py
```

Stdlib `unittest`, no pytest — same zero-dependency policy as the library.

Each test corresponds to a defect the loop must keep catching, so a failure means
the product has become less able to find real specification gaps. The ones worth
knowing about:

- the totality gate rejects a partial state matrix, an evasion value, an
  unresolvable operation entity or foreign key, an unraised `inferred` anchor, and
  a recommendation that is not one of the offered options;
- it *accepts* an admitted gap when a blocker was raised for it — the escape
  hatch is legitimate, only skipping it silently is not;
- `contracts` catches a required-and-derived contradiction, a
  mutual-required-reference cycle, an unguarded mutation, a missing table, and a
  dangling `$ref`;
- `concordance` turns two lenses disagreeing on a Part A dimension into a ranked
  blocker, and merges the same blocker found by two lenses into one with both
  attributed;
- `rank` asks about blocking and irreversible decisions, assumes reversible ones,
  and only notes a lone cosmetic finding;
- `saturation` converges on a dry round and treats a resolved blocker as seen;
- `mutate.verify` fails on detection without localization — a loop that
  complained about everything must not pass;
- the schema validator raises on an unimplemented keyword rather than ignoring
  it.
