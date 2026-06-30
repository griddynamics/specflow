# JVM Hanging Tests Runbook

## Purpose

Use this runbook when a JVM build or test command appears to "hang" in unattended agent runs.
Applies to any JVM language (Java, Kotlin, Scala) using Gradle or Maven.

## Fast Triage

- `100%` CPU Java worker for many minutes usually means active spin/infinite loop in code/tests.
- near-`0%` CPU Java/Gradle process after work output often means daemon/idle wait behavior.
- no new output for a long time can also indicate deadlock or blocking I/O.

## Required Gradle Command Shape (Unattended Runs)

For one-shot JVM tasks in agent runs, use:

```bash
./gradlew <task> --no-daemon --console=plain
```

Rules:
- always use one-shot tasks that terminate on their own.
- never use `--continuous` or `-t` in unattended runs.
- avoid rich console/progress UI output in agents; prefer plain output.

## JVM Hang Causes

- test code infinite loops or unbounded recursion.
- deadlocks across threads/locks.
- tests waiting on external systems (network, filesystem, subprocess) without timeout.
- unresolved futures/promises/coroutines that keep workers alive.
- daemon/process-lifecycle mismatch where the parent waits for the wrong process.

## Investigation Checklist

1. Confirm command form includes `--no-daemon --console=plain`.
2. Capture process snapshot and CPU:
   - identify Java/Gradle worker CPU level and elapsed runtime.
3. If CPU is high:
   - inspect test for unbounded loops, recursive calls, uncancelled async jobs.
4. If CPU is low:
   - verify no watcher/continuous mode is enabled.
   - verify parent process is not waiting on daemon/idle child.
5. Add explicit test-level timeout/cancellation where reasonable.

## Resolution Direction

- If high CPU and active worker: fix test/app logic first.
- If low CPU and process linger: fix command/process policy first.
- If uncertain: treat as unknown and gather one more process snapshot before retries.

## Language-Specific Guidance

For Kotlin coroutines hang patterns (`runTest`, `advanceUntilIdle`, `viewModelScope`):
- read `./standards/kotlin_coroutines_tests.md`
