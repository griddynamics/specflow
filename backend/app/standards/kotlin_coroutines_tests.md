# Kotlin Coroutines Test Hang Patterns

## Purpose

Applies to Kotlin projects using `kotlinx-coroutines-test` (`runTest`, `StandardTestDispatcher`).
For JVM-generic hang causes and Gradle command rules, read `./standards/jvm_hanging_tests.md`.

## The Core Problem: `runTest` Implicit Drain

`runTest` always calls `advanceUntilIdle()` internally after the test body completes.
If any coroutine scope backed by `testDispatcher` has a live tick loop, the drain spins forever.

Two ways this surfaces:

1. **Explicit** — test body calls `advanceUntilIdle()` directly with a live tick loop in scope.
2. **Hidden** — test body correctly uses `advanceTimeBy(...)` but the ViewModel/class owns a
   `viewModelScope` (or similar long-lived scope) backed by `testDispatcher`. The implicit drain
   at the end of `runTest` hits the tick loop. This is harder to spot because the test body itself
   looks correct.

## Required Pattern: Inject `backgroundScope`

Never let a tick loop run in `viewModelScope` during unit tests. Inject the scope instead.
`backgroundScope` (provided by `TestScope`) is excluded from `advanceUntilIdle()` draining
and is cancelled automatically when `runTest` ends.

```kotlin
// ViewModel: accept an optional external scope; default to viewModelScope in production
class MyViewModel(
    private val externalScope: CoroutineScope? = null,
) : ViewModel() {
    private val scope get() = externalScope ?: viewModelScope
    // tick loop: scope.launch { while (isActive) { delay(1_000); refresh() } }
}

// Test: pass backgroundScope so the tick loop is excluded from runTest's drain
@Test
fun myTest() = runTest(testDispatcher) {
    val vm = MyViewModel(externalScope = backgroundScope)
    advanceTimeBy(1_001L)
    assertEquals(expected, vm.uiState.value.field)
    // backgroundScope and the tick loop are cancelled automatically when runTest ends
}
```

## Anti-Patterns

- `advanceUntilIdle()` with any live infinite loop in scope → hang.
- `advanceTimeBy(...)` in the body but `viewModelScope` backed by `testDispatcher` → hidden hang
  from `runTest`'s implicit drain.
- Shared `StandardTestDispatcher` instance across tests without cancelling ViewModel scopes between
  tests → zombie tick loops accumulate on the scheduler and compound the drain hang.

## Checklist for Kotlin Coroutine Tests

- [ ] ViewModel (or equivalent) accepts an injected `CoroutineScope`.
- [ ] Tests pass `backgroundScope` as the injected scope.
- [ ] No test calls `advanceUntilIdle()` when a tick loop is in scope.
- [ ] `advanceTimeBy(...)` is used for all time-dependent assertions.
- [ ] `Dispatchers.setMain(testDispatcher)` in `@Before`, `Dispatchers.resetMain()` in `@After`.
