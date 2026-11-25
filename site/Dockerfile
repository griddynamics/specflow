# Public landing page image. The last stage (runtime) is the image; Infra and the local stack build it.
# Build context = the repository root (npm workspace: package.json, package-lock.json, packages/ui, frontend and site manifests);
# `docker build -f site/Dockerfile .`. The context is filtered by site/Dockerfile.dockerignore, which only BuildKit reads:
# build with BuildKit, from the repository root, with `-f site/Dockerfile`.
# The base tag is a literal in every FROM (no ARG), so --build-arg cannot change it.
# runtime = the static export (site/out) served by BusyBox httpd on port 8080 as an unprivileged user.
# httpd writes nothing, so the image runs with a read-only root filesystem (docker run --read-only).

# deps: the locked dependencies.
FROM node:24.21.0-alpine AS deps
ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /app
RUN chown node:node /app
USER node
# Every workspace manifest goes in before `npm ci` (the lock names them all), so the install layer is cached until a manifest changes.
COPY --chown=node:node package.json package-lock.json ./
COPY --chown=node:node frontend/package.json frontend/package.json
COPY --chown=node:node packages/ui/package.json packages/ui/package.json
COPY --chown=node:node site/package.json site/package.json
RUN npm ci

# build: `next build` with output: "export" writes site/out. No runtime env is needed.
FROM deps AS build
COPY --chown=node:node packages/ui packages/ui
COPY --chown=node:node site site
RUN npm run build --workspace=site

# runtime: the static files and a static server, nothing else.
FROM busybox:1.37.0 AS runtime
COPY --from=build /app/site/out /srv
COPY site/httpd.conf /etc/httpd.conf
# A numeric user: Kubernetes checks runAsNonRoot against a number.
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=5s --start-period=5s --retries=3 \
  CMD wget -qO /dev/null http://127.0.0.1:8080/ || exit 1
# -f stays in the foreground (PID 1 gets SIGTERM); -c maps the 404 error page (a path relative to -h).
CMD ["httpd", "-f", "-p", "8080", "-h", "/srv", "-c", "/etc/httpd.conf"]
