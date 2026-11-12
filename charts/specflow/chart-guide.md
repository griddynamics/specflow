# Deploying Specflow with the Helm chart

Deploy the chart five times—once per component. Use the recommended release
names because the frontend profile expects the API Service at
`http://specflow-api:8000`.

## 1. Prepare the namespace

```shell
NAMESPACE=specflow
CHART=./charts/specflow

kubectl create namespace "$NAMESPACE" \
  --dry-run=client \
  --output yaml |
kubectl apply -f -
```

Before deployment, PostgreSQL, Temporal, object storage, graph storage, and the
OIDC provider must already be available.

## 2. Prepare component Secrets

Each component expects one Kubernetes Secret:

| Component | Secret |
| --- | --- |
| API | `specflow-api` |
| Frontend | `specflow-frontend` |
| MCP | `specflow-mcp` |
| Agent worker | `specflow-worker-agent` |
| Parse worker | `specflow-worker-parse` |

You can print the required keys for a component with:

```shell
sed -n '/^env:/,/^envFrom:/p' \
  "$CHART/examples/components/api.yaml" |
sed -n 's/^  - name: //p'
```

Replace `api.yaml` with the other component filename as needed.

Prepare environment files in a secure directory outside the repository. Set
the directory explicitly in your shell; the guard exits instead of silently
using a repository-local path:

```shell
: "${SECRET_INPUT_DIR:?set SECRET_INPUT_DIR to a secure directory outside this repository}"
```

The expected files are:

```text
$SECRET_INPUT_DIR/api.env
$SECRET_INPUT_DIR/frontend.env
$SECRET_INPUT_DIR/mcp.env
$SECRET_INPUT_DIR/worker-agent.env
$SECRET_INPUT_DIR/worker-parse.env
```

Their format is:

```dotenv
APP_ENV=prod
API_PORT=8000
GRAPH_STATE_DIR=/var/lib/specflow/graph
DATABASE_HOST=postgres.example.com
DATABASE_NAME=specflow
DATABASE_USER=specflow
DATABASE_PASSWORD=<database-password>
```

Include every key referenced by the corresponding component profile. The format above is the API's; `mcp.env`
holds only the keys of `mcp.yaml` (`APP_ENV`, the OIDC and MCP settings; no `DATABASE_*`, `GRAPH_STORE_*`, Temporal or
storage keys: MCP calls the API at `API_INTERNAL_URL`). When MCP is deployed, `api.env` also carries
`MCP_RESOURCE_URL`, `MCP_AUDIENCE` and `MCP_CLIENT_IDS` with the same values as `mcp.env`, and the same
`AUTH_*` claim profile: the API serves the agent routes MCP reads through only with them (unset, they answer
`503 agent_unavailable` and every MCP tool fails with `internal_error`), and it verifies the MCP token itself. Remove
unused optional entries from your copy of the profile. `PGSSLROOTCERT` is
optional and only needed when mounting a PostgreSQL CA.

Create or update the Secrets:

```shell
kubectl --namespace "$NAMESPACE" create secret generic specflow-api \
  --from-env-file="$SECRET_INPUT_DIR/api.env" \
  --dry-run=client \
  --output yaml |
kubectl apply -f -

kubectl --namespace "$NAMESPACE" create secret generic specflow-frontend \
  --from-env-file="$SECRET_INPUT_DIR/frontend.env" \
  --dry-run=client \
  --output yaml |
kubectl apply -f -

kubectl --namespace "$NAMESPACE" create secret generic specflow-mcp \
  --from-env-file="$SECRET_INPUT_DIR/mcp.env" \
  --dry-run=client \
  --output yaml |
kubectl apply -f -

kubectl --namespace "$NAMESPACE" create secret generic specflow-worker-agent \
  --from-env-file="$SECRET_INPUT_DIR/worker-agent.env" \
  --dry-run=client \
  --output yaml |
kubectl apply -f -

kubectl --namespace "$NAMESPACE" create secret generic specflow-worker-parse \
  --from-env-file="$SECRET_INPUT_DIR/worker-parse.env" \
  --dry-run=client \
  --output yaml |
kubectl apply -f -
```

Do not commit the environment files.

ESO or CSI can create these Secrets instead. The chart provides corresponding
examples under `examples/integrations/`.

## 3. Deploy the API first

The API runs database migrations during startup, so install it first and wait
until it is ready:

```shell
helm upgrade --install specflow-api "$CHART" \
  --namespace "$NAMESPACE" \
  --values "$CHART/examples/components/api.yaml" \
  --wait \
  --timeout 10m
```

The API profile:

- Runs one replica using `Recreate`.
- Creates a retained PVC for `/var/lib/specflow/graph`.
- Exposes an internal Service on port `8000`.
- Does not create a public route by default.

If the cluster has no default StorageClass, provide one in an environment
overlay:

```yaml
persistence:
  storageClass: standard
```

Then deploy with both files:

```shell
helm upgrade --install specflow-api "$CHART" \
  --namespace "$NAMESPACE" \
  --values "$CHART/examples/components/api.yaml" \
  --values ./environments/production/api.yaml \
  --wait \
  --timeout 10m
```

## 4. Deploy the frontend

Ensure the frontend Secret contains:

```dotenv
API_INTERNAL_URL=http://specflow-api:8000
```

Then install:

```shell
helm upgrade --install specflow-frontend "$CHART" \
  --namespace "$NAMESPACE" \
  --values "$CHART/examples/components/frontend.yaml" \
  --wait \
  --timeout 10m
```

The base profile exposes only a cluster-internal Service on port `3000`.

For ingress-nginx:

```shell
helm upgrade --install specflow-frontend "$CHART" \
  --namespace "$NAMESPACE" \
  --values "$CHART/examples/components/frontend.yaml" \
  --values "$CHART/examples/integrations/ingress-nginx.yaml" \
  --wait \
  --timeout 10m
```

Copy the integration file and replace `specflow.example.com` and
`specflow-tls` before using it in production. Equivalent examples exist for
Traefik and Gateway API.

## 5. Deploy MCP

Before MCP, check that the API's secret holds `MCP_RESOURCE_URL`, `MCP_AUDIENCE` and `MCP_CLIENT_IDS` (the same
values as MCP's); without them the API's agent routes answer `503 agent_unavailable` and every MCP tool fails.

```shell
helm upgrade --install specflow-mcp "$CHART" \
  --namespace "$NAMESPACE" \
  --values "$CHART/examples/components/mcp.yaml" \
  --wait \
  --timeout 10m
```

MCP has its own image (`griddynamics/specflow-mcp`, the `mcp` extra only, no migration) whose
startup command is `specflow mcp`, so the profile sets `command: []`. The chart refuses the API startup
command for this component. It exposes an internal Service on port `8002`.

For public MCP routing, use an environment overlay that exposes only:

```yaml
routing:
  type: ingress
  hostnames:
    - mcp.example.com
  paths:
    - path: /mcp
      pathType: Prefix
    - path: /.well-known/oauth-protected-resource
      pathType: Prefix
  ingress:
    className: nginx
    annotations: {}
    tls:
      - secretName: specflow-mcp-tls
        hosts:
          - mcp.example.com
  httpRoute:
    parentRefs: []
    annotations: {}
```

Do not expose MCP health endpoints publicly.

## 6. Deploy the agent worker

```shell
helm upgrade --install specflow-worker-agent "$CHART" \
  --namespace "$NAMESPACE" \
  --values "$CHART/examples/components/worker-agent.yaml" \
  --wait \
  --timeout 10m
```

Its Secret must include:

```dotenv
WORKER_QUEUES=agents
WORKER_HEALTH_PORT=8001
```

The worker has no Service or public route.

## 7. Deploy the parse worker

```shell
helm upgrade --install specflow-worker-parse "$CHART" \
  --namespace "$NAMESPACE" \
  --values "$CHART/examples/components/worker-parse.yaml" \
  --wait \
  --timeout 15m
```

Its Secret must include:

```dotenv
WORKER_QUEUES=parse
WORKER_HEALTH_PORT=8001
```

The parse worker has higher CPU and memory requirements and uses a writable
1 GiB `/tmp` volume. It has no Service or public route.

## 8. Verify the deployment

```shell
helm list --namespace "$NAMESPACE"

kubectl get deployments,pods,services,pvc \
  --namespace "$NAMESPACE"

kubectl rollout status deployment/specflow-api \
  --namespace "$NAMESPACE"

kubectl rollout status deployment/specflow-frontend \
  --namespace "$NAMESPACE"

kubectl rollout status deployment/specflow-mcp \
  --namespace "$NAMESPACE"

kubectl rollout status deployment/specflow-worker-agent \
  --namespace "$NAMESPACE"

kubectl rollout status deployment/specflow-worker-parse \
  --namespace "$NAMESPACE"
```

Inspect logs when a component does not become ready:

```shell
kubectl logs deployment/specflow-api \
  --namespace "$NAMESPACE" \
  --all-containers

kubectl describe pod \
  --namespace "$NAMESPACE" \
  --selector app.kubernetes.io/instance=specflow-api
```

A Pod failing with `CreateContainerConfigError` usually means its component
Secret or one of the referenced Secret keys is missing.
