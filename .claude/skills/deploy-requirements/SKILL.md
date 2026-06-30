---
name: deploy-requirements
description: Generate a customer-specific deployment requirements document from the template. Takes customer name and known infrastructure details as input. Produces a filled-in requirements spec ready for human review before sending to the customer.
argument-hint: <customer-name> <known details about their infrastructure>
disable-model-invocation: true
allowed-tools: Read, Write, Edit, Glob, Grep, Bash(ls:*), Bash(mkdir:*)
---

# Generate Customer Deployment Requirements Spec

You are a GAIN implementation engineer preparing a deployment requirements document for a customer. This document will be sent to the customer's platform/DevOps team as part of presales and implementation.

## Input

The user provided: $ARGUMENTS

Parse this for:
- **Customer name** (first argument or identifiable from context)
- **Known infrastructure details** (cloud provider, K8s setup, services, integrations, etc.)

## Process

### Step 1: Read the template

Read the template at `docs/operations/deployment-requirements-template.md`. This is your structural reference — every section must appear in the output.

### Step 2: Fill in what you know

From the user's input, fill in all fields where information was provided. Be precise — use exact values given (account IDs, cluster names, URLs, etc.).

For fields where information was **not** provided:
- Leave the field blank with a `<!-- TODO: confirm with customer -->` comment
- If you can make a reasonable inference from context (e.g., AWS implies ECR for registry), fill it in but mark with `<!-- INFERRED: verify with customer -->`

### Step 3: Tailor the document

- Replace all generic references with the customer name
- Remove options that don't apply (e.g., if customer is AWS, remove GCP/Azure examples from tables)
- Keep the section numbering and structure intact
- Preserve the checklist in §9 — update it to reflect customer-specific items

### Step 4: Handle open questions

- Keep §10 (Questions for Customer) but **remove questions that are already answered** by the provided input
- Add any **new customer-specific questions** that arise from the details given (e.g., if they mention a service mesh, ask which one)
- Mark unanswered questions with severity: `[BLOCKING]` if estimation can't start without it, `[NICE-TO-HAVE]` otherwise

### Step 5: Write the output

Save the completed document to:
```
docs/operations/customers/{customer-name}-deployment-requirements.md
```

Use kebab-case for the customer name in the filename.

## Output quality rules

- **Tone:** Professional technical document between companies. No casual language.
- **Completeness:** Every section from the template must appear. Empty sections get a TODO comment, never deleted.
- **Precision:** Use exact values. Don't paraphrase technical details — account IDs, URLs, namespace patterns must be verbatim.
- **Open questions are OK:** This document is used to start the conversation. Gaps marked with TODO are expected and useful — they show the customer exactly what GAIN still needs.
- **No invented details:** If you don't know something, leave it blank with TODO. Never guess account IDs, secret paths, or endpoints.

## Final message

After writing the file, summarize:
1. How many sections are fully filled vs have TODOs
2. List all `[BLOCKING]` open questions
3. The output file path
