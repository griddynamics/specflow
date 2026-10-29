# INDEX.md: table of contents of the graph library specification

The graph library (`graph`) builds a typed knowledge graph of a software specification in Neo4j from a
project's documents and keeps it current. Shared core `graph.core`, build submodule `graph.build`;
`graph.gap` and `graph.plan` come later.

# overview.md: purpose, scope, goals, non-goals, actors, boundaries, constraints, key terms
- [overview.md](overview.md)

# how-it-works.md: explainer: why this ontology, how a build creates the graph, dedup, verification
- [how-it-works.md](how-it-works.md)

# architecture.md: packages, components, import rules, write ownership, ports, settings, errors
- [architecture.md](architecture.md)

# data-model.md: Neo4j model, identity, statements, provenance, constraints and indexes, invariants
- [data-model.md](data-model.md)

# ontology.md: ontology format, loading and validation, submodule files, the shipped ontology and what may change in it
- [ontology.md](ontology.md), [spec-ontology.yaml](spec-ontology.yaml) (initial template)

# build.md: modes, build steps, reuse in update builds, failures, build result, options
- [build.md](build.md)

# analysis.md: light inference, verification and repair
- [analysis.md](analysis.md)

# api.md: public API: setup, owned writes, read and search, LLM gateway, judges, build, ports
- [api.md](api.md)

# requirements.md: requirement units FR-CORE, FR-ONT, FR-READ, FR-LLM, FR-BLD, FR-INF, FR-VER, NFR
- [requirements.md](requirements.md)

# later.md, questions.md: deferred features, open questions
- [later.md](later.md), [questions.md](questions.md)

# implementation/: implementation plan and code-level contracts, traced to requirement ids
- [implementation/plan.md](implementation/plan.md): milestones, steps, gates, requirement coverage
- [implementation/tech-specs.md](implementation/tech-specs.md): modules, signatures, Graphiti mapping, test design, files affected
