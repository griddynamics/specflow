# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in SpecFlow, **please report it privately**. Do not open a public GitHub issue.

**Email:** [specflow@griddynamics.com](mailto:specflow@griddynamics.com)
**Subject line:** `[SECURITY] <brief description>`

Please include:

- A description of the vulnerability and its potential impact
- Steps to reproduce or a proof of concept
- Affected component(s) (e.g., `backend`, `mcp_server`, TUI/CLI)
- Your suggested severity (Critical / High / Medium / Low)

### Response Commitment

| Milestone | Target |
|---|---|
| Acknowledgment of report | 3 business days |
| Initial triage and severity assessment | 7 business days |
| Patch or mitigation available | Best effort, dependent on severity |
| Public disclosure (coordinated) | After fix is released, or 90 days from report — whichever comes first |

We follow coordinated disclosure. We ask reporters to give us reasonable time to investigate and remediate before any public disclosure. We will credit reporters in the advisory unless they prefer to remain anonymous.

### Safe Harbor

We consider security research conducted in good faith to be authorized and will not pursue legal action against researchers who comply with this policy.

---

## Supported Versions

Security fixes are applied to the **current release** of the published package. Older releases do not receive backports.

| Component | Package | Supported |
|---|---|---|
| SpecFlow MCP Server + CLI | [`gd-specflow`](https://pypi.org/project/gd-specflow/) | Current release |

---

## Security Architecture

### Design Principles

SpecFlow generates code inside sandboxed, isolated agent workspaces with no credentials and no access to customer systems or production infrastructure. All external dependencies used during generation are mocked; there is no infra provisioning and no deploy step during code generation. If user provided instructions for agentic deployment and integration testing (See [QUICKSTART.md](QUICKSTART.md) ) then it happens without any credentials, based only on Service Account and Workflow Identity Federation. The cloud sandbox given to SpecFlow agents is completely owned by the user and must be provisioned to allow SpecFlow agents in GitHub Actions to perform deployments there.

### Data Boundary

- Generation runs against disposable scratchpad repositories created for the run — never against a repository with history or code you want to keep.
- The MCP server (running locally in your IDE) uploads only your `specs/`, optional `src/`, and planning outputs to the backend for a `run_generation` call; it does not transmit your broader project or credentials.
- Backend agents operate in per-run, isolated workspaces and hold no standing credentials to external systems.

---

## Supply Chain Security

**Risk:** Compromise of the published `gd-specflow` package or its dependencies.

**Mitigations:**
- Packages are published to PyPI via the [`publish-mcp.yml`](.github/workflows/publish-mcp.yml) GitHub Actions workflow with controlled access.
- Secret scanning runs via `gitleaks` (see [`.gitleaks.toml`](.gitleaks.toml)).

**Deployer Responsibility:**
- Pin dependency versions in production deployments.
- Monitor dependencies for known vulnerabilities using tools such as `pip-audit`, Dependabot, or equivalent.
- Review `requirements.txt` / `pyproject.toml` transitive dependencies before deploying in sensitive environments.

---

## AI-Generated Output

SpecFlow's harness produces full-stack codebases via autonomous AI agents over multi-hour runs. Treat all generated output as an untrusted third-party contribution:

- **Mandatory review.** Thoroughly review, test, and validate generated code before it is deployed or used in any production or customer-facing context.
- **Complexity and variance signals are not a security audit.** P10Y/Compass scoring measures code complexity alignment across parallel runs — it does not substitute for a security review.
- **Zero-trust assumption.** Never assume the inherent safety, correctness, or security of AI-generated output.

---

## General Security Recommendations

- Apply least-privilege access controls across all components and integrations.
- Keep dependencies, base images, and infrastructure components up to date.
- Perform a security review before any production deployment of generated code.
- Use separate environments (development, staging, production) with appropriate access controls for each.

---

## Scope and Limitations

This policy covers the SpecFlow open-source project as published at [github.com/griddynamics/specflow](https://github.com/griddynamics/specflow), including the `gd-specflow` PyPI package.

This policy does **not** cover:
- Hosted or managed SpecFlow deployments operated by Grid Dynamics or third parties (these may have their own security policies).
- Third-party integrations, LLM providers, or IDE platforms used alongside SpecFlow.

---

## Disclaimer

SpecFlow is provided under the [MIT License](LICENSE) on an "AS IS" basis, without warranties or conditions of any kind. The threat model and mitigations described in this document represent best-effort guidance. Deployers are responsible for conducting their own security assessments appropriate to their environment, compliance requirements, and risk tolerance. Nothing in this document constitutes legal advice or a guarantee of security.
