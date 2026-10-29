import v8 from "node:v8";
import vm from "node:vm";

// Upload buffers are external memory (Node socket reads). V8 frees them lazily, so
// during a very large streamed upload RSS grows by tens of MiB although nothing is
// retained (measured: arrayBuffers oscillate while the writable side stays at one
// chunk). A cheap collection every few MiB keeps that growth bounded. Best effort:
// without the flag the hint is a no-op.
const EVERY_BYTES = 8 * 1024 * 1024;

let collect: (() => void) | undefined;
try {
  v8.setFlagsFromString("--expose_gc");
  const fn = vm.runInNewContext("gc") as unknown;
  if (typeof fn === "function") collect = fn as () => void;
} catch {
  collect = undefined;
}

/** Returns a function to call with each chunk length of a streamed body. */
export function gcPacer(): (bytes: number) => void {
  let since = 0;
  return (bytes) => {
    since += bytes;
    if (collect && since >= EVERY_BYTES) {
      since = 0;
      collect();
    }
  };
}
