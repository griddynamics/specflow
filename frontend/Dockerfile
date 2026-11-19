# Frontend image. The last stage (runtime) is the image; Infra and the local stack build it.
# The base tag is a literal in every FROM (no ARG), so --build-arg cannot change it.
# runtime = the standalone Next server, started only after scripts/check-env.mjs passes.

# deps: the locked dependencies.
FROM node:24.21.0-alpine AS deps
ENV NEXT_TELEMETRY_DISABLED=1 \
    NODE_EXTRA_CA_CERTS=/etc/ssl/build-ca.pem
WORKDIR /app
# The extra CAs of npm (and of the build stage, which starts from this one): the image's own bundle plus, when the
# build gets the secret extra_ca, the CA of a proxy that inspects TLS (`make up EXTRA_CA=<file>`,
# docs/configuration.md "Behind a TLS-inspecting proxy"). The runtime stage starts from the base image again, so the
# image has neither this bundle nor the variable.
RUN --mount=type=secret,id=extra_ca \
    cat /etc/ssl/certs/ca-certificates.crt > /etc/ssl/build-ca.pem \
    && if [ -s /run/secrets/extra_ca ]; then cat /run/secrets/extra_ca >> /etc/ssl/build-ca.pem; fi
RUN chown node:node /app
USER node
COPY --chown=node:node package.json package-lock.json ./
RUN npm ci

# build: `next build` with output: "standalone". No runtime env is needed here.
FROM deps AS build
COPY --chown=node:node . .
RUN npm run build

# runtime: standalone server + static assets + public, no node_modules install, no dev tooling.
FROM node:24.21.0-alpine AS runtime
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000
WORKDIR /app
COPY --from=build --chown=node:node /app/.next/standalone ./
COPY --from=build --chown=node:node /app/.next/static ./.next/static
COPY --from=build --chown=node:node /app/public ./public
# The startup gate and the rules it shares with the app.
COPY --from=build --chown=node:node /app/scripts/check-env.mjs ./scripts/check-env.mjs
COPY --from=build --chown=node:node /app/lib/env-rules.mjs ./lib/env-rules.mjs
USER node
EXPOSE 3000
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=3 \
  CMD wget -qO- http://127.0.0.1:3000/api/health >/dev/null || exit 1
# A bad configuration exits non-zero before the server starts (AC-6.4, AC-14.3).
# HOSTNAME is set for the server process itself: Kubernetes injects HOSTNAME=<pod name>,
# which server.js would bind to. `exec` keeps SIGTERM going to node.
CMD ["sh", "-c", "node scripts/check-env.mjs && HOSTNAME=0.0.0.0 exec node server.js"]
