// FE-SPEC-02, FE-REV-03, FE-DS-02 (R5): port of `TypingIndicator` in bundle.js: three pulsing dots.
export function TypingIndicator() {
  return (
    <span className="gdg-typing" role="status" aria-label="Typing">
      <i />
      <i />
      <i />
    </span>
  );
}
