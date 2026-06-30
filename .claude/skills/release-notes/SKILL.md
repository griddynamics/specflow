---
name: release-notes
description: Generate release notes between two git tags and publish as a GitHub release. Input format: "<new-tag> from <base-tag>" (e.g. "v0.5.2 from v0.5.0").
argument-hint: "<new-tag> from <base-tag>"
---

# Release Notes Generator

## Steps

1. Parse args to extract `NEW_TAG` and `BASE_TAG` (format: `v0.5.2 from v0.5.0`).
2. Run `git log <BASE_TAG>..<NEW_TAG> --oneline --merges` to list merge commits (PRs).
3. For each merge commit, extract the PR number and title using `gh pr view <number> --json number,title,author` — match PR numbers from commit messages like `#123` or `Merge pull request #123`.
4. If no PR number is found in a merge commit, use the commit subject as the entry with the committer as author.
5. Write release notes in the format below.
6. Publish the GitHub release: `gh release create <NEW_TAG> --title "<title>" --notes "<body>"`.
   - If the tag does not yet exist on the remote, add `--target main` (or the default branch).
   - If the release already exists, use `gh release edit <NEW_TAG> --notes "<body>"` instead.
7. Print the release URL from the `gh` output.

## Output Format

```
## <NEW_TAG> — <short label derived from the tag, e.g. "0.5.2">

<2-3 sentence Slack-ready summary. What shipped, why it matters, any breaking changes. Plain prose, no bullet points. No internal jargon.>

## What's Changed

- <PR title> by @<author> in #<number>
- <PR title> by @<author> in #<number>
...

**Full Changelog**: https://github.com/<owner>/<repo>/compare/<BASE_TAG>...<NEW_TAG>
```

## Rules

- Derive `<owner>/<repo>` from `gh repo view --json nameWithOwner -q .nameWithOwner`.
- Release title format: `<NEW_TAG>` (just the tag — GitHub prepends the repo name automatically).
- The Slack summary goes at the **top** of the release body, before "What's Changed".
- List only PRs merged between the two tags — do not include direct commits that have no PR.
- If a commit has no associated PR, skip it silently unless it's the only change, in which case list it as `- <subject> by @<author>`.
- Do NOT include "Merge branch" or "Merge pull request" boilerplate lines.
- The summary must be usable as-is in a Slack message — no markdown headers, no code spans.
- After publishing, print the final release URL so the user can share it.
