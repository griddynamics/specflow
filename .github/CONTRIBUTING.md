# Contributing

Thank you for helping with SpecFlow Graph. This page explains how to report a
problem, suggest a change and send a pull request.

By taking part, you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Report a bug or ask for a feature

Open a GitHub issue and pick the matching template. Say what you did, what you
expected and what happened. For a bug, add the steps to reproduce it and the
versions you use.

Do not report security problems in a public issue. Follow [`SECURITY.md`](SECURITY.md).

## Set up

You need Docker with the Compose plugin, `make`, uv, Python 3.12 and Node.js 20.9
or newer. The [README](../README.md) shows how to start the stack. All settings are in
[`docs/configuration.md`](../docs/configuration.md).

```shell
make env-local     # writes .env.local with random local secrets
make up            # starts the local stack
```

Never commit `.env.local` or any real key, token or password.

## Make a change

1. Fork the repository and create a branch from `main`.
2. Keep the change small and focused. One pull request should do one thing.
3. Add or update tests for the change.
4. Follow the rules of the area you touch: [`backend/AGENTS.md`](../backend/AGENTS.md)
   for Python code and [`frontend/AGENTS.md`](../frontend/AGENTS.md) for the web app.
5. Run the checks below and fix what they report.
6. Open a pull request and fill in the template.

## Run the checks

Run these from the repo root. CI runs the same checks.

| Command | What it checks |
|---|---|
| `make check` | Backend lint and the offline backend tests |
| `make validate` | All static validation tracks (Python, TypeScript, SQL, Cypher) |
| `make frontend-lint frontend-build` | Frontend lint and build |
| `make e2e-production` | Offline browser tests against the built frontend (run `make frontend-build` first) |

You can run one validation track with `make validate TRACKS="python-lint"`. See
[`validation/README.md`](../validation/README.md).

## Pull request rules

- The title says what changes, in plain words.
- The description says why, and how you tested it.
- CI must pass.
- A maintainer reviews every pull request. Answer review comments by pushing new
  commits or by explaining your view.

## License

This project is licensed under the [Apache License 2.0](../LICENSE). By submitting a
contribution, you agree that it is licensed under the same terms, as described in
section 5 of the license. There is no CLA and no sign-off requirement.
