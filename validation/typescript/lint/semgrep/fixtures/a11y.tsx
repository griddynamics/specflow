declare const go: () => void;
declare const steps: string[];
declare const Item: (props: { children?: unknown }) => null;

// ruleid: a11y-control-inside-interactive-role
const nestedRole = <div role="button">Pick <button>Edit</button></div>;
// ok: a11y-control-inside-interactive-role
const groupRole = <div role="group"><button>Edit</button><button>Delete</button></div>;

// ruleid: a11y-control-inside-native-control
const nestedNative = <button><span onClick={go}>x</span></button>;
// ok: a11y-control-inside-native-control
const siblings = <div><button>Save</button><span onClick={go}>x</span></div>;

// ruleid: a11y-step-semantics-outside-list
const stepSpan = <span aria-current="step">Shipping</span>;
// ok: a11y-step-semantics-outside-list
const stepItem = <li aria-current="step">Shipping</li>;

// ruleid: a11y-process-indicator-not-a-list
const flatSteps = steps.map((step) => <span key={step}>{step}</span>);
// ok: a11y-process-indicator-not-a-list
const listSteps = <ol>{steps.map((step) => <li key={step}>{step}</li>)}</ol>;

// ruleid: a11y-decorative-separator-not-hidden
const separator = <span>›</span>;
// ok: a11y-decorative-separator-not-hidden
const hiddenSeparator = <span aria-hidden="true">›</span>;

const tones = {
  // ruleid: a11y-status-mapped-to-colour-alone
  ACTIVE: 'var(--success)',
  // ok: a11y-status-mapped-to-colour-alone
  active: 'var(--success)',
};
const labelled = {
  // ok: a11y-status-mapped-to-colour-alone
  ACTIVE: { colour: 'var(--success)', label: 'Active' },
};

export { nestedRole, groupRole, nestedNative, siblings, stepSpan, stepItem, flatSteps, listSteps };
export { separator, hiddenSeparator, tones, labelled, Item };
