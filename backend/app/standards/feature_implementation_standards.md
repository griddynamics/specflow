# Feature Implementation Standards

## Purpose

Every feature mentioned in a specification must be decomposed into all its constituent parts
before code generation. An incomplete feature description produces incomplete code.

**CRITICAL**: A specification that says "users can upload images" without specifying storage backend,
validation rules, size limits, and access control is not a complete feature description — it is a
topic heading. Flag every such underspecified feature as a GAP.

For implementation patterns and detailed sub-task checklists for specific feature types, use the
**Rosetta knowledge-base toolset** provisioned into this workspace's `.claude/` (its Skills and subagents).

---

## Universal Feature Layers

Every feature — regardless of domain — must be specified across all applicable layers:

### 1. Data & Storage
What data does this feature create, read, update, or delete? Where is it stored, in what format,
with what schema? What are retention, cleanup, and consistency requirements?

### 2. API Contract
How is the feature invoked? What are the endpoints/events/messages, their inputs, outputs, and
error responses? What versioning strategy applies?

### 3. Business Logic
What are the rules, constraints, and state transitions? What invariants must always hold?
What happens on edge cases (empty input, duplicate, concurrent modification)?

### 4. Security
Who is allowed to perform this action? How is identity verified? What rate limits apply?
What inputs must be validated or sanitized? What data must not be exposed?

### 5. Error Handling
What can fail, and what is the expected behavior for each failure mode? Are failures retried,
surfaced to the user, or logged silently? What is the recovery path?

### 6. Performance & Scale
What is the expected load? Are there latency SLAs? Does the feature require caching, indexing,
pagination, or async processing?

### 7. UI / UX (if frontend)
What does the user see during loading, success, and error states? What feedback is provided?
Are there accessibility requirements?

### 8. Testing
What must be covered by unit tests? What integration scenarios must be verified? Are there
security test cases (unauthorized access, injection, boundary values)?

### 9. Dependencies
What external services, credentials, or infrastructure must exist before this feature can work?
Flag all prerequisites as USER NOTICE if they require manual provisioning.

---

## Drill-Down Process

When a feature is mentioned but its layers are underspecified, apply this process:

1. **Name the feature** concisely ("profile picture upload", "payment checkout", "real-time chat").
2. **Identify which layers apply** — not all 9 apply to every feature (a read-only dashboard has
   no write business logic; a background job has no UI layer).
3. **For each applicable layer, ask**: "Is the choice explicit in the spec, or would two engineers
   reasonably implement it differently?" If different → it's a GAP.
4. **Propose resolution** with concrete options so the stakeholder can decide, not vague guidance.


---

## Variance Prevention

Two engineers implementing the same feature from the same spec must produce equivalent results.
If that is not true for any feature in the spec, the spec is incomplete. Every ambiguity is a
future rework risk — surface it now, not during code review.
