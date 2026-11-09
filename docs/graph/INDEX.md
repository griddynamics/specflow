# INDEX.md: table of contents of the graph library specification

The graph library (`graph`) builds a typed knowledge graph of a software specification in a graph database (Neo4j, ArcadeDB or PostgreSQL) from a
project's documents and keeps it current. Shared core `graph.core`, build submodule `graph.build`,
plan submodule `graph.plan` (its own specification in [plan/](plan/INDEX.md)); `graph.gap` comes later.

# overview.md: purpose, scope, goals, non-goals, actors, boundaries, constraints, key terms
- [overview.md](overview.md)

# how-it-works.md: explainer: why this ontology, how a build creates the graph, dedup, verification
- [how-it-works.md](how-it-works.md)

# architecture.md: packages, components, import rules, write ownership, ports, settings, errors
- [architecture.md](architecture.md)

# data-model.md: Neo4j model (canonical), per-store mapping, identity, statements, provenance, constraints and indexes, invariants
- [data-model.md](data-model.md)

# ontology.md: ontology format, loading and validation, submodule files, the shipped ontology and what may change in it
- [ontology.md](ontology.md), [spec-ontology.yaml](spec-ontology.yaml) (initial template)

# build.md: modes, build steps, normalisation and quarantine, reuse in update builds, failures, build result, migration, options
- [build.md](build.md)

# analysis.md: shared validator, light inference, verification and repair, consolidated metrics
- [analysis.md](analysis.md)

# api.md: public API: setup, owned writes, read and search, LLM gateway, judges, build, ports
- [api.md](api.md)

# requirements.md: requirement units FR-CORE, FR-ONT, FR-READ, FR-LLM, FR-BLD, FR-INF, FR-VER, NFR
- [requirements.md](requirements.md)

# later.md, questions.md: deferred features, open questions
- [later.md](later.md), [questions.md](questions.md)

# plan/: graph.plan, implementation plans for coding agents on the spec graph
- [plan/INDEX.md](plan/INDEX.md)

