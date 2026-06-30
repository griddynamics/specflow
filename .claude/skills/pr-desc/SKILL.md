---
name: pr-desc
description: Update the current PR description based on commits vs main. Writes a concise summary, entrypoint, and details section.
argument-hint: "[optional context or focus area]"
---

# PR Description Updater

1. Run `git log main..HEAD --oneline` to see commits on this branch.
2. Run `git diff main...HEAD --stat` to see which files changed.
3. Run `gh pr view --json number,title,body` to get the current PR (if one exists).
4. Look at existing PR description as it might have already partly correct information.
5. Generate new description based on commits and real state of repo
6. Update the PR description using `gh pr edit --body "..."`.

## Description Format
Separate using newlines only.

```
## Summary
<1-2 sentences: what this PR does and why>

## Entrypoint
<1-2 sentences: where a reviewer should start — the main hook-up point, the top-level call, the route/handler/config that ties it all together>

## Diagram
<If any flow or structure was changed, put the diagram here or reference a file with diagrams if that is used to track it by design.>

## Details
<Only non-obvious things: surprising design choices, edge cases, workarounds, things that look wrong but aren't. Skip plumbing, boilerplate, and straightforward wiring.>

```

## Rules

- **Summary section is always concise prose: no code snippets, no backticks, and no semicolons.** Keep it to 1-2 plain sentences a non-author can understand.
- Keep every section to 1-2 sentences max. Be ruthless about brevity.
- **Entrypoint**: name the specific file/function/line where the new behavior is wired in — not a vague description.
- **Details**: if there are no gotchas, write "Nothing surprising." Don't invent content.
- Do NOT summarize every commit — synthesize the overall change.
- Do NOT use bullet lists inside sections — prose only.
- After updating, print the final body so the user can confirm.
