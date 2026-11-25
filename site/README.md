# site: the public landing page

The Graphico landing page, a Next.js static export (`output: "export"`): one page, no server, no API calls, no analytics.
Fonts come from Google Fonts. Styles come from `@graphico/ui` (the `gx` root class on `<body>`); the page's own section
styles are the `app/*.css` files here. Animations are client components; reduced motion is honoured and the theme is set
before first paint (no flash). It never imports from `frontend/`.

From the repository root (the npm workspace, `make frontend-setup` = `npm ci`):

| Command | What it does |
|---|---|
| `make site-lint` | eslint (eslint-config-next) and `tsc --noEmit` |
| `make site-build` | writes the static export to `site/out/` |
| `make site-test` | unit tests and static-export checks (run `make site-build` first for the export checks) |
| `npm --prefix site run dev` | dev server on port 3000 |

## Image

`docker build -f site/Dockerfile .` (context = repository root; `site/Dockerfile.dockerignore` filters it). The image
serves `site/out` with BusyBox `httpd` on **port 8080** as the numeric user `10001`. It writes nothing, so it runs with
`docker run --read-only -p 8080:8080`. Compose service `site` publishes it on `http://localhost:3002` (the stack does not
have to run). Docker Hub repository `griddynamics/specflow-site`; Helm: `charts/specflow/examples/components/site.yaml`.
