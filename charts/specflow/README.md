# Specflow Helm chart

This chart deploys exactly one Specflow component. Install the same chart as
five independent Helm releases to run the complete application:

| Release | `component` | Public Service |
| --- | --- | --- |
| `specflow-api` | `api` | Optional, port 8000 |
| `specflow-frontend` | `frontend` | Optional, port 3000 |
| `specflow-mcp` | `mcp` | Optional, port 8002 |
| `specflow-worker-agent` | `worker-agent` | No |
| `specflow-worker-parse` | `worker-parse` | No |

The chart has no subcharts and no Argo CD or Flux assumptions. PostgreSQL,
graph storage, Temporal, object storage, OIDC, External Secrets Operator, and
Secrets Store CSI Driver are external dependencies.

## Prerequisites

- Kubernetes 1.25 or newer
- Helm 3
- The external services required by your Specflow configuration
- Gateway API CRDs and a Gateway only when `routing.type=httpRoute`
- Any optional secret or ingress controllers referenced by your values

The component profiles under `examples/components` refer to one existing
Secret per component. Create those Secrets manually or through a secret
controller before installing. The examples never contain secret values.

## Install all components

Use the same chart version for all releases. First install the API and wait for
its startup migration and readiness checks:

```shell
helm upgrade --install specflow-api ./charts/specflow \
  --namespace specflow \
  --create-namespace \
  --values ./charts/specflow/examples/components/api.yaml \
  --wait
```

Then install the remaining components. They may be installed in parallel after
the API is ready:

```shell
helm upgrade --install specflow-frontend ./charts/specflow \
  --namespace specflow \
  --values ./charts/specflow/examples/components/frontend.yaml

helm upgrade --install specflow-mcp ./charts/specflow \
  --namespace specflow \
  --values ./charts/specflow/examples/components/mcp.yaml

helm upgrade --install specflow-worker-agent ./charts/specflow \
  --namespace specflow \
  --values ./charts/specflow/examples/components/worker-agent.yaml

helm upgrade --install specflow-worker-parse ./charts/specflow \
  --namespace specflow \
  --values ./charts/specflow/examples/components/worker-parse.yaml
```

Helm does not order separate releases. Plain Helm, Argo CD, Flux, and other
deployment systems should enforce the API-first sequence in their own workflow.
The chart does not add controller-specific ordering annotations.

Environment overrides are normal Helm layers:

```shell
helm upgrade --install specflow-api ./charts/specflow \
  --namespace specflow \
  --values ./charts/specflow/examples/components/api.yaml \
  --values ./environments/production/api.yaml \
  --wait
```

Helm replaces lists instead of merging their entries. An environment overlay
that changes `env`, `envFrom`, `volumes`, or `volumeMounts` must include the
complete desired list.

## Application configuration

Use the Kubernetes-native `env` and `envFrom` values. The component profiles
list every variable explicitly and read each value from the component Secret:

```yaml
env:
  - name: DATABASE_PASSWORD
    valueFrom:
      secretKeyRef:
        name: specflow-api
        key: DATABASE_PASSWORD
```

A literal `env.value` must be a quoted string, and every entry must contain
exactly one of `value` or `valueFrom`. The chart rejects duplicate names and
settings owned by the images or Kubernetes. `envFrom` remains available for
custom profiles, but the shipped component profiles use explicit
`secretKeyRef` entries so their required keys remain visible.

The primary settings for each component are:

| Component | Required or conditional settings |
| --- | --- |
| API | `APP_ENV`; `DATABASE_HOST`, `DATABASE_NAME`, `DATABASE_USER`, `DATABASE_PASSWORD`; `OIDC_ISSUER`, `AUTH_ALLOWED_EMAIL_DOMAINS`; `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE`; storage settings; graph-store settings; `AGENT_MODEL`; `API_PORT=8000`; `GRAPH_STATE_DIR=/var/lib/specflow/graph` |
| Frontend | `APP_ENV`, `PUBLIC_URL`, `API_INTERNAL_URL`, `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `SESSION_SECRET` |
| MCP | `APP_ENV`; database and graph-store settings; `OIDC_ISSUER`, `AUTH_ALLOWED_EMAIL_DOMAINS`; `MCP_RESOURCE_URL`; `MCP_PORT=8002` |
| Worker agent | `APP_ENV`; database, Temporal, storage, graph-store, agent-model, and LLM gateway settings; `WORKER_QUEUES=agents`; `WORKER_HEALTH_PORT=8001` |
| Worker parse | `APP_ENV`; database, Temporal, and storage settings; `WORKER_QUEUES=parse`; `WORKER_HEALTH_PORT=8001` |

`DRIVE_IDENTITY_EMAIL` is required by the API and parse worker when Google
Drive operations are enabled. Graph building also requires
`GRAPH_LLM_MODEL`, `GRAPH_SMALL_MODEL`, `GRAPH_EMBEDDING_MODEL`,
`LLM_GATEWAY_URL`, and `LLM_GATEWAY_API_KEY`. Set `GRAPH_EMBEDDING_DIM` to the
selected embedding model's real dimension.

Common optional pass-through settings are grouped below. They are ordinary
`env` entries; the chart does not reinterpret them:

- Database and TLS: `DATABASE_PORT`, `DATABASE_AGENT_USER`,
  `DATABASE_AGENT_PASSWORD`, `PGSSLMODE`, `PGSSLROOTCERT`.
- Storage and cloud access: `STORAGE_BACKEND`, `STORAGE_BUCKET`,
  `STORAGE_PREFIX`, `SPECFLOW_CONFIG_FILE`, `GOOGLE_CLOUD_PROJECT`,
  `AWS_ENDPOINT_URL_S3`, `AWS_REGION`, `STORAGE_S3_ADDRESSING`.
- Temporal: `TEMPORAL_API_KEY`, `TEMPORAL_TLS_CERT`, `TEMPORAL_TLS_KEY`,
  `TEMPORAL_QUEUE_PARSE`, `TEMPORAL_QUEUE_AGENTS`.
- Graph store: `GRAPH_STORE`, `GRAPH_STORE_NAME`, `GRAPH_STORE_ADMIN_URI`,
  `GRAPH_STORE_POOL_SIZE`.
- Authentication: `AUTH_JWKS_URL`, `AUTH_API_AUDIENCE`,
  `AUTH_API_CLIENT_IDS`, `AUTH_SUBJECT_CLAIM`, `AUTH_CLIENT_ID_CLAIM`,
  `AUTH_EMAIL_CLAIM`, `AUTH_EMAIL_VERIFIED_CLAIM`, `AUTH_TOKEN_TYPE`,
  `AUTH_TOKEN_TYPE_CLAIM`, `AUTH_TOKEN_TYPE_VALUE`,
  `AUTH_IGNORED_AUDIENCES`.
- MCP and frontend: `MCP_AUDIENCE`, `MCP_CLIENT_IDS`, `MCP_ALLOWED_HOSTS`,
  `MCP_ALLOWED_ORIGINS`, `OIDC_INTERNAL_URL`, `MCP_PUBLIC_URL`,
  `MCP_CLIENT_ID`, `MCP_SERVER_NAME`.
- Models and tracing: `GRAPH_EMBEDDING_DIM`, `GRAPH_LLM_CONCURRENCY`,
  `LANGFUSE_BASE_URL`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and the
  compatibility fallback `LANGFUSE_HOST`.
- Diagnostics: `LOGS_LEVEL`, `LOGS_STDOUT_LEVEL`, `LOGS_RAISE_EXCEPTIONS`,
  `AGENT_TRACE_SENSITIVE_DATA`, `WORKER_DIAG_STATE`,
  `OPENAI_AGENTS_DISABLE_TRACING`, `OMP_NUM_THREADS`.
- Provider credential chains when workload identity is unavailable:
  `GOOGLE_APPLICATION_CREDENTIALS`, `AWS_ACCESS_KEY_ID`,
  `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_DEFAULT_REGION`.

`DATABASE_PORT` defaults to `5432`, and `DATABASE_AGENT_USER` defaults to
`specflow_agent` in the application.

See the
[Specflow configuration reference](https://github.com/griddynamics/specflow/blob/main/docs/configuration.md)
for defaults and optional authentication, storage, graph, model, tracing, and
provider settings. Use `GRAPH_STORE_*`; the transitional graph-backend aliases
are not part of this chart's public contract.

## Images and component contracts

`image.tag` defaults to the chart `appVersion`. Set either a tag or a
`sha256:<64 hexadecimal characters>` digest. Component image defaults are
provided by the five profiles, not selected secretly in a template.

The chart enforces contracts that prevent known unsafe combinations:

- API runs one replica with `Recreate`, without an HPA, because the image runs
  an unlocked database migration before serving traffic.
- API always mounts `/var/lib/specflow/graph`. The default chart uses an
  ephemeral `emptyDir`; the API component profile creates a retained PVC.
- MCP must run `command: [specflow, mcp]`, bypassing the API image startup
  command and its migration.
- Workers have no Service or route, expose health only inside the Pod, and use
  at least 150 seconds for graceful termination.
- The two worker profiles use distinct images and health port 8001.

## Persistence

The API component profile creates a PVC for graph-build state:

```yaml
persistence:
  enabled: true
  existingClaim: ""
  name: graph-state
  mountPath: /var/lib/specflow/graph
  size: 5Gi
  accessModes: [ReadWriteOnce]
  storageClass: ""
  retain: true
  annotations: {}
```

Set `existingClaim` instead of `enabled` to use a pre-created PVC. The two
options are mutually exclusive. `retain: true` adds Helm's `keep` policy, so
uninstalling the release leaves chart-created data behind; delete such PVCs
explicitly when they are no longer needed. A non-empty `mountPath` with neither
option uses an `emptyDir`, which is suitable for rendering and evaluation but
not durable production API state.

## Routing

`routing.type` is one of `none`, `ingress`, or `httpRoute` and requires an
enabled Service. Paths are explicit and use `Prefix` or `Exact` semantics.

Ingress-nginx and Traefik use the standard Ingress template:

```shell
helm template specflow-frontend ./charts/specflow \
  --values ./charts/specflow/examples/components/frontend.yaml \
  --values ./charts/specflow/examples/integrations/ingress-nginx.yaml
```

The same command works with `ingress-traefik.yaml`. Controller-specific
behavior belongs in annotations or separately owned `extraObjects`.

Envoy Gateway, Traefik Gateway, and other Gateway API implementations use the
standard HTTPRoute template:

```shell
helm template specflow-frontend ./charts/specflow \
  --values ./charts/specflow/examples/components/frontend.yaml \
  --values ./charts/specflow/examples/integrations/gateway-api.yaml
```

The referenced Gateway and its listeners are not created by this chart. The MCP
profile exposes only `/mcp` and `/.well-known/oauth-protected-resource` when a
route overlay is enabled.

## External Secrets Operator

The chart does not install ESO or a secret store. The
`examples/integrations/external-secrets.yaml` overlay shows a component-owned
`ExternalSecret` rendered through `extraObjects` and consumed by the profile's
explicit `secretKeyRef` entries:

```shell
helm template specflow-api ./charts/specflow \
  --values ./charts/specflow/examples/components/api.yaml \
  --values ./charts/specflow/examples/integrations/external-secrets.yaml
```

Install compatible ESO CRDs and create the referenced `SecretStore` or
`ClusterSecretStore` first. The pattern works with any backend supported by
ESO; only the external store configuration changes.

## Secrets Store CSI Driver and mounted files

The `secrets-store-csi.yaml` overlay demonstrates a `SecretProviderClass`, a
CSI volume and mount, and a synced Kubernetes Secret consumed by explicit
`secretKeyRef` entries. Install the driver and provider first. Its `parameters`
block is provider-specific; replace the AWS example for another backend.

The `mounted-ca.yaml` overlay shows the same generic volumes interface for a
PostgreSQL CA stored in a Kubernetes Secret. Set `PGSSLROOTCERT` in the
`specflow-api` Secret to the mounted path. The interface also supports a
mounted `SPECFLOW_CONFIG_FILE`.

The chart never installs a CSI driver or provider and never creates raw Secret
values.

## Extra objects

`extraObjects` accepts YAML objects or trusted templated strings. It is useful
for component-specific `ExternalSecret`, `SecretProviderClass`, `Certificate`,
`ServiceMonitor`, `NetworkPolicy`, or controller middleware resources:

```yaml
extraObjects:
  - apiVersion: monitoring.coreos.com/v1
    kind: ServiceMonitor
    metadata:
      name: '{{ include "specflow.fullname" . }}'
    spec:
      selector:
        matchLabels:
          app.kubernetes.io/instance: '{{ .Release.Name }}'
```

Treat `extraObjects` as trusted Helm input because it is evaluated with `tpl`.
An object must have exactly one owner: do not repeat a shared object in several
Specflow releases. Install cluster-wide controllers, stores, shared
configuration objects, and shared policies separately.

## Scaling and availability

Set `autoscaling.enabled=true` to render an `autoscaling/v2` HPA. At least one
CPU or memory target is required. API autoscaling is rejected until migrations
gain a distributed lock or move into a reliable migration Job.

Set `podDisruptionBudget.enabled=true` and exactly one of `minAvailable` or
`maxUnavailable`; set the other to `null`. Do not configure a PDB that prevents
all voluntary disruptions for a single-replica component.

## Service accounts and workload identity

The chart creates a ServiceAccount by default with token automount disabled.
Use annotations for your cluster's workload identity mechanism, or reference an
existing account:

```yaml
serviceAccount:
  create: false
  name: specflow-api
  annotations: {}
  automountServiceAccountToken: false
```

Enable token automount only when the workload actually calls the Kubernetes
API.

## Validate a change

From this chart directory:

```shell
jq empty values.schema.json
helm lint --strict .

for component in api frontend mcp worker-agent worker-parse; do
  helm template "specflow-${component}" . \
    --namespace specflow \
    --values "examples/components/${component}.yaml" >/dev/null
done

helm unittest .
helm package . --destination /tmp
```

Render integration overlays together with the matching component profile.
Validate built-in and Gateway API resources with `kubeconform` when it is
available, then test the five releases against disposable external services in
a real cluster before publishing.

## Versioning

The chart `version` follows SemVer for its values and Kubernetes-resource
contract. Breaking changes increment the major version, additive features the
minor version, and fixes the patch version. `appVersion` identifies the default
compatible Specflow image release; image tags may be overridden independently
for controlled rollouts.

The chart intentionally excludes databases, Temporal, object storage,
Langfuse, OIDC providers, secret controllers, CSI drivers, ingress controllers,
KEDA, oauth2-proxy, StatefulSets, Argo CD/Flux resources, and cloud-specific
gateway objects.
