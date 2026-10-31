<div align="center">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="public/specflow-logo-dark.png"/>
  <img src="public/specflow-logo.png" alt="SpecFlow logo" width="200"/>
</picture>

### One Shared Knowledge Base for Agentic Delivery

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![Next.js 16](https://img.shields.io/badge/Next.js-16-black.svg)](https://nextjs.org/)
[![Quick start](https://img.shields.io/badge/quick%20start-5%20min-brightgreen.svg)](QUICKSTART.md)

</div>

---
Agents solved coding. Planning became the critical path.
SpecFlow turns your scattered specs into one shared knowledge graph.
People and coding agents build from the same, complete context.

<!-- TODO: product GIF (add a source -> build -> explore the graph) -->

## The problem

With AI coding agents, implementation takes a fraction of the time it used to.
Planning did not get faster. **Even with AI, writing a solid spec still takes weeks**,
and agents build exactly what an unclear spec says.

<p align="center">
  <img src="public/specflow-problem.png" width="860"
       alt="AI SDLC: agents solved coding, planning became the critical path. Before agents: plan, a long implementation, review. With agents: implementation is short and planning is the longest part. Problem: even with AI, writing a solid spec takes weeks.">
</p>

A real spec is dozens of documents, decks and Drive folders, written by different
people at different times. Requirements repeat and contradict each other, and key
decisions are missing. Teams find out while they build, which is the most expensive
moment.

## How SpecFlow helps

<p align="center">
  <img src="public/specflow-how-it-works.png" width="860"
       alt="SpecFlow: one shared knowledge base for the team. Sources (codebase, documents, presentations) feed four steps: parse sources, generate the knowledge graph, raise decisions for owners, generate the implementation plan. The delivery team adds docs, answers decisions and triggers the plan. Plan steps go to the Rosetta harness, which drives coding agents that read the graph and work in your repos. Goal: deterministic context for higher quality.">
</p>

The graph stays current: when a document changes, the next build updates only what
that change affects. Its vocabulary comes from an ontology file, so you can fit it to
your domain without code changes.

## What changes for your team

<p align="center">
  <img src="public/specflow-for-developers.png" width="860"
       alt="What changes for your team: three developers with Claude Code, Cursor and Codex claim and update features in the SpecFlow knowledge graph through MCP. Keep your tools; no collisions, because the graph hands out features that don't share code paths; same daily flow: claim, code, open a PR, and the status updates itself. Impact: unlock team productivity.">
</p>

## Try it

```shell
make env-local          # writes local secrets and asks for your LLM gateway settings
make up WITH=temporal   # starts the whole stack in Docker
```

Open http://127.0.0.1:3001. Prerequisites, the architecture and the development
commands are in [**QUICKSTART.md**](QUICKSTART.md).

## Learn more

- [QUICKSTART.md](QUICKSTART.md): run it, how it fits together, develop
- [docs/configuration.md](docs/configuration.md): every setting
- [docs/graph/](docs/graph/INDEX.md): how the knowledge graph is built
- [Rosetta](https://github.com/griddynamics/rosetta): the harness that turns plan steps into reviewed code

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](.github/CONTRIBUTING.md) and our
[Code of Conduct](.github/CODE_OF_CONDUCT.md). To report a vulnerability, follow
[SECURITY.md](.github/SECURITY.md).
