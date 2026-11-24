# Frontend image. The last stage (runtime) is the image; Infra and the local stack build it.
# Build context = the repository root (npm workspace: package.json, package-lock.json, packages/ui, frontend/);
# `docker build -f frontend/Dockerfile .`. The context is filtered by frontend/Dockerfile.dockerignore.
# The base tag is a literal in every FROM (no ARG), so --build-arg cannot change it.
# runtime = the standalone Next server, started only after scripts/check-env.mjs passes.

# deps: the locked dependencies.
FROM node:24.21.0-alpine AS deps
ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /app
RUN chown node:node /app
USER node
# Every workspace manifest goes in before `npm ci`, so the install layer is cached until a manifest changes.
COPY --chown=node:node package.json package-lock.json ./
COPY --chown=node:node frontend/package.json frontend/package.json
COPY --chown=node:node packages/ui/package.json packages/ui/package.json
RUN npm ci

# build: `next build` with output: "standalone". No runtime env is needed here.
FROM deps AS build
COPY --chown=node:node . .
RUN npm run build --workspace=frontend

# runtime: standalone server + static assets + public, no node_modules install, no dev tooling.
FROM node:24.21.0-alpine AS runtime
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000
WORKDIR /app
# The standalone tree is rooted at the workspace root (outputFileTracingRoot), so the server is frontend/server.js.
COPY --from=build --chown=node:node /app/frontend/.next/standalone ./
COPY --from=build --chown=node:node /app/frontend/.next/static ./frontend/.next/static
COPY --from=build --chown=node:node /app/frontend/public ./frontend/public
# The startup gate and the rules it shares with the app.
COPY --from=build --chown=node:node /app/frontend/scripts/check-env.mjs ./frontend/scripts/check-env.mjs
COPY --from=build --chown=node:node /app/frontend/lib/env-rules.mjs ./frontend/lib/env-rules.mjs
WORKDIR /app/frontend
USER node
EXPOSE 3000
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=3 \
  CMD wget -qO- http://127.0.0.1:3000/api/health >/dev/null || exit 1
# A bad configuration exits non-zero before the server starts (AC-6.4, AC-14.3).
# HOSTNAME is set for the server process itself: Kubernetes injects HOSTNAME=<pod name>,
# which server.js would bind to. `exec` keeps SIGTERM going to node.
CMD ["sh", "-c", "node scripts/check-env.mjs && HOSTNAME=0.0.0.0 exec node server.js"]
