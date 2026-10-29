// A `console.error` / `console.warn` during a test fails that test instead of scrolling past. A
// test that expects one asserts it explicitly: `vi.spyOn(console, 'error').mockImplementation(...)`.
import failOnConsole from 'vitest-fail-on-console';

failOnConsole();
