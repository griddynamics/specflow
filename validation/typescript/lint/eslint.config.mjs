// Drop-in ESLint flat config for TypeScript code bases, with one preset per surface:
//
//   app      a React + TanStack (Query, Router) + Vitest application (`src`, its unit tests)
//   library  a TypeScript package without React (types, a client, shared code)
//   e2e      Playwright end-to-end specs
//   node     plain Node tooling scripts and configs (.js/.mjs/.cjs)
//
// Run it with
//
//   eslint --config <kit>/eslint.config.mjs --no-inline-config \
//     --max-warnings 0 <paths>
//
// `--no-inline-config` makes inline directives inert; the `no-warning-comments` terms turn every
// leftover suppression comment into a finding.
//
// The default export lints a project rooted at the current working directory with the default
// layout (app + e2e + node). A project with another layout writes its own `eslint.config.mjs`:
//
//   import { defineKitConfig } from './validation/typescript/lint/eslint.config.mjs';
//   export default defineKitConfig({ app: { src: 'web/src' }, library: { files: ['lib/src/**/*.ts'] } });
//
// Every path option is a glob relative to `root`. Checks that need facts of the project (its
// breakpoints, its primitives, its type-only packages, its OpenAPI document) are off until the
// option that supplies the fact is set; `kit.config.example.mjs` shows every option.
//
// Selector groups: each `no-restricted-*` group is its own rule of the `restrict` plugin
// (`./eslint-rules/restrict.mjs` registers ESLint's core rule once per group). Flat config
// replaces a rule's option array on the last matching object, so one rule per group lets every
// group keep its own scope and exemptions without partitioning the files by hand.
//
// `--no-inline-config` makes an inline disable comment inert, and `no-warning-comments` below
// turns one into a finding wherever it is written, together with formatter and coverage ignore
// comments and comments that keep a superseded name alive.

import js from '@eslint/js';
import globals from 'globals';
import tseslint from 'typescript-eslint';
import sonarjs from 'eslint-plugin-sonarjs';
import eslintConfigPrettier from 'eslint-config-prettier';
import react from 'eslint-plugin-react';
import reactHooks from 'eslint-plugin-react-hooks';
import reactRefresh from 'eslint-plugin-react-refresh';
import jsxA11y from 'eslint-plugin-jsx-a11y';
import eslintReact from '@eslint-react/eslint-plugin';
import i18next from 'eslint-plugin-i18next';
import tanstackQuery from '@tanstack/eslint-plugin-query';
import tanstackRouter from '@tanstack/eslint-plugin-router';
import importX from 'eslint-plugin-import-x';
import barrelFiles from 'eslint-plugin-barrel-files';
import boundaries from 'eslint-plugin-boundaries';
import noUnsanitized from 'eslint-plugin-no-unsanitized';
import regexpPlugin from 'eslint-plugin-regexp';
import unicorn from 'eslint-plugin-unicorn';
import vitestPlugin from '@vitest/eslint-plugin';
import testingLibrary from 'eslint-plugin-testing-library';
import jestDom from 'eslint-plugin-jest-dom';
import playwright from 'eslint-plugin-playwright';

import { plugin as localPlugin } from './eslint-rules/index.mjs';
import { defineRestrictPlugin } from './eslint-rules/restrict.mjs';

// ---------------------------------------------------------------------------
// Selector groups that need no project facts.
// ---------------------------------------------------------------------------

/** Every type-checked TypeScript file. */
export const SEL_BASE = [
  {
    selector: "ExportAllDeclaration[exportKind='type']",
    message: 'Export named canonical types; remove the type-star barrel.',
  },
  {
    selector: 'TSEnumDeclaration',
    message: 'No TS enum: use a union of string literals (a discriminated union).',
  },
  {
    selector: "TSModuleDeclaration[kind!='global'][id.type='Identifier']",
    message: 'No non-global TS namespace: use ES modules.',
  },
];

/** Components reach the DOM through refs. */
export const SEL_COMPONENTS = [
  {
    selector:
      "CallExpression[callee.object.name='document'][callee.property.name='getElementById']",
    message: 'No document.getElementById in a component; use a ref.',
  },
  {
    selector: "CallExpression[callee.object.name='document'][callee.property.name='querySelector']",
    message: 'No document.querySelector in a component; use a ref.',
  },
  {
    selector: "AssignmentExpression[left.property.name='innerHTML']",
    message: 'No innerHTML assignment in a component.',
  },
];

/** A TanStack Query key is built in a `*query-keys.ts` module, never inline. */
export const SEL_QUERY_KEY = [
  {
    selector: "Property[key.name='queryKey'] > ArrayExpression",
    message: 'A queryKey array literal belongs in a *query-keys.ts module.',
  },
];

/** Route params and search stay typed: no `strict: false`, no cast of a router hook result. */
const ROUTER_CAST =
  'An `as` cast on a router hook result opts out of typed routing; route params are parsed and validated, never cast.';
export const SEL_ROUTER_STRICT = [
  {
    selector:
      "CallExpression[callee.name='useParams'] ObjectExpression Property[key.name='strict'][value.value=false]",
    message: 'useParams({ strict: false }) opts out of typed params; keep params typed.',
  },
  {
    selector:
      "CallExpression[callee.name='useSearch'] ObjectExpression Property[key.name='strict'][value.value=false]",
    message: 'useSearch({ strict: false }) opts out of typed search; keep search typed.',
  },
  {
    selector:
      'TSAsExpression > CallExpression[callee.name=/^use(Params|Search|Loader|RouteContext|Match)/]',
    message: ROUTER_CAST,
  },
  {
    selector:
      'TSAsExpression > CallExpression[callee.property.name=/^use(Params|Search|Loader|RouteContext|Match)/]',
    message: ROUTER_CAST,
  },
];

// What the user entered is never cleared in the same block as the call that consumes it, before
// that call can have settled: a failed request would discard the entry. A reset inside
// `onSuccess`, or after `await mutateAsync(...)`, is not matched. Setters named for feedback
// state the app produces (an error, a success or progress flag) are not user input.
const INPUT_RESET =
  'CallExpression[callee.name=/^set(?!.*(Error|Success|Warning|Notice|Unavailable|Progress|Loading|Busy|Pending|Saving|Submitting|Status))[A-Z]/][arguments.length=1]' +
  ":matches([arguments.0.raw=\"''\"], [arguments.0.raw='\"\"'], [arguments.0.raw='null'], [arguments.0.raw='false'], " +
  ":has(> ArrayExpression[elements.length=0]), :has(> Identifier[name='undefined']), " +
  ':has(> NewExpression[arguments.length=0][callee.name=/^(Set|Map)$/]))';
const MUTATION_DISPATCH =
  'CallExpression:matches([callee.name=/^mutate(Async)?$/], [callee.property.name=/^mutate(Async)?$/])';
const VALUE_HANDOFF =
  "CallExpression:matches([callee.name=/^on[A-Z]/], [callee.property.name=/^on[A-Z]/])[arguments.length>=1][arguments.0.type!='Literal']";
const KEEP_INPUT =
  'Clear the input in the success path, never before it: move the reset into the mutation onSuccess callback, or await mutateAsync(...) and reset after it resolves; when this component only hands the value on, let the owner report the outcome and reset only on success.';

export const SEL_INPUT_PRESERVATION = [
  {
    selector: `ExpressionStatement:has(${MUTATION_DISPATCH}):not(:has(AwaitExpression)) ~ * ${INPUT_RESET}`,
    message: `This clears what the user entered in the same block as a mutate()/mutateAsync() call that is never awaited, so a failed request discards the entry. ${KEEP_INPUT}`,
  },
  {
    selector: `ExpressionStatement:has(${VALUE_HANDOFF}):not(:has(AwaitExpression)) ~ * ${INPUT_RESET}`,
    message: `This hands a value to an on* callback prop and clears what the user entered in the same block; this component cannot have seen the request succeed. ${KEEP_INPUT}`,
  },
  {
    selector: `ExpressionStatement:has(> ${INPUT_RESET}) ~ ExpressionStatement ${VALUE_HANDOFF}`,
    message: `This clears what the user entered before handing the value to an on* callback prop, so nothing is left to retry with if the request fails. ${KEEP_INPUT}`,
  },
];

/** A runtime duration is a configuration value with a default, never a literal at a call site. */
export const SEL_TIMER_LITERAL = [
  {
    selector:
      "CallExpression[callee.name=/^(setTimeout|setInterval)$/][arguments.1.type='Literal'], CallExpression[callee.property.name=/^(setTimeout|setInterval)$/][arguments.1.type='Literal']",
    message:
      'A literal timer delay: a runtime duration is a configuration value with a default, never a literal at the call site.',
  },
  {
    selector:
      "CallExpression[callee.name=/^use[A-Za-z]*Debounce[A-Za-z]*$/][arguments.1.type='Literal']",
    message:
      'A literal debounce interval: read the interval from configuration in the debounce hook instead of passing a number at the call site.',
  },
];

/** Numeric input is parsed with the locale's decimal separator, outside the locale modules. */
export const SEL_LOCALE_PARSE = [
  {
    selector: "CallExpression[callee.name='parseFloat']",
    message: 'No locale-unaware parseFloat outside the locale module; use the locale-aware parser.',
  },
  {
    selector:
      "CallExpression[callee.name='Number'][arguments.0.type='MemberExpression'][arguments.0.property.name='value']",
    message:
      'No locale-unaware Number(*.target.value) outside the locale module; use the locale-aware parser.',
  },
];

/** A status-like field is a union of literals. */
export const SEL_STATUS_LIKE = [
  {
    selector:
      'TSPropertySignature[key.name=/^(status|kind|stage|mode|state|band|level|tier)$/] TSStringKeyword',
    message: 'A status-like field must be a union of literals, not string (discriminated union).',
  },
];

/** The focus ring is styled, never removed. */
export const SEL_OUTLINE = [
  {
    selector:
      "Property[key.name='outline'][value.value='none'], Property[key.name='outline'][value.value=0]",
    message:
      'outline: none/0 removes the focus ring; style the focus state instead of removing it.',
  },
];

// Production code carries no test-mode branch and no guard or optional call for an interface every
// supported browser has; the test setup stubs what the test environment lacks.
const BROWSER_INTERFACES =
  '/^(window|document|navigator|HTMLElement|Element|ResizeObserver|IntersectionObserver|MutationObserver|matchMedia|requestAnimationFrame|localStorage|sessionStorage)$/';
const BROWSER_METHODS =
  '/^(scrollIntoView|scrollTo|scrollBy|showPopover|hidePopover|togglePopover|matchMedia|requestAnimationFrame)$/';
const NO_GUARD = 'No guard for an interface every supported browser has; the test setup stubs it.';
export const SEL_TEST_PROBES = [
  {
    selector:
      "MemberExpression:matches([property.name='MODE'], [property.value='MODE'])[object.property.name='env'][object.object.type='MetaProperty']",
    message:
      'No branch on the build mode in production code: a test substitutes at a boundary in its setup.',
  },
  {
    selector: "MemberExpression[object.name='process'][property.name='env']",
    message: 'No `process.env` in the browser application: it probes the test runner.',
  },
  {
    selector: `UnaryExpression[operator='typeof'][argument.name=${BROWSER_INTERFACES}]`,
    message: NO_GUARD,
  },
  {
    selector: `UnaryExpression[operator='typeof'][argument.type='MemberExpression'][argument.object.name=${BROWSER_INTERFACES}]`,
    message: NO_GUARD,
  },
  {
    selector: `BinaryExpression[operator='in'][left.value=${BROWSER_METHODS}]`,
    message: NO_GUARD,
  },
  {
    selector: `CallExpression[optional=true][callee.property.name=${BROWSER_METHODS}]`,
    message:
      'No optional call of an interface every supported browser has; the test setup stubs it.',
  },
];

/** Unit tests assert behaviour, mock at the network boundary and keep real modules. */
export const SEL_TESTS = [
  {
    selector: 'CallExpression[callee.property.name=/^toMatchSnapshot$|^toMatchInlineSnapshot$/]',
    message: 'No snapshot tests of whole trees: assert the specific behaviour instead.',
  },
  {
    selector:
      "CallExpression[callee.name='stubGlobal'][arguments.0.value='fetch'], CallExpression[callee.name='stubGlobal'][arguments.0.value='XMLHttpRequest'], CallExpression[callee.name='stubGlobal'][arguments.0.value='WebSocket']",
    message: 'No vi.stubGlobal of the network surface: mock at the network boundary (MSW).',
  },
  {
    selector:
      "CallExpression[callee.name='mock'][arguments.0.value=/^(\\.|@\\/).*\\/(api|context|hooks|components|pages)\\//]",
    message:
      'No vi.mock of a relative or @/ api/context/hooks/components/pages module: mock at the network boundary or use the real module.',
  },
];

// End-to-end specs own the inputs they send and wait for conditions, never for durations.
const POSITION_MESSAGE =
  'A row is selected by its position in a response. Select it by a value the test itself wrote or names.';
export const SEL_E2E = [
  {
    selector:
      "MemberExpression[computed=true][property.type='Literal'][object.type='AwaitExpression'][object.argument.type='CallExpression'][object.argument.callee.property.name='json']",
    message: POSITION_MESSAGE,
  },
  {
    selector:
      "MemberExpression[computed=true][property.type='Literal'][object.type='TSAsExpression'][object.expression.type='AwaitExpression'][object.expression.argument.callee.property.name='json']",
    message: POSITION_MESSAGE,
  },
  {
    selector: "NewExpression[callee.name='Promise'] CallExpression[callee.name='setTimeout']",
    message:
      'A test never waits for a duration; wait for the condition with a web-first assertion or expect.poll.',
  },
];
/** Every spec takes `test`/`expect` from the shared fixtures module, not from the library. */
export const SEL_E2E_FIXTURE_IMPORT = [
  {
    selector:
      "ImportDeclaration[source.value='@playwright/test'][specifiers.0.imported.name=/^(test|expect)$/]",
    message:
      'Import test/expect from the shared fixtures module, not directly from @playwright/test.',
  },
];

/** The wheel belongs to the page: no handler traps vertical wheel or trackpad scrolling. */
export const SEL_WHEEL = [
  {
    selector: 'JSXAttribute[name.name=/^onWheel/]',
    message:
      'No wheel handler on an element: a vertical wheel or trackpad gesture must scroll the page, and a handler traps it. A horizontal scroller states `overflow-y: hidden` and `overscroll-behavior-x: contain` in CSS instead.',
  },
  {
    selector: "CallExpression[callee.property.name='addEventListener'][arguments.0.value='wheel']",
    message:
      'No `wheel` listener: it traps vertical wheel and trackpad scrolling. A horizontal scroller states `overflow-y: hidden` and `overscroll-behavior-x: contain` in CSS instead.',
  },
];

// Comment terms that are a finding anywhere: inline suppressions, formatter and coverage ignore
// comments, and comments that keep a superseded name alive.
export const BANNED_COMMENT_TERMS = [
  'prettier-ignore',
  'istanbul ignore',
  'c8 ignore',
  'v8 ignore',
  'eslint-disable',
  'backward compat',
  'backward compatibility',
  'backwards compatibility',
  'compatibility shim',
  'legacy alias',
  'legacy shim',
  'legacy export',
  'kept for compatibility',
  'kept for old callers',
  'kept for legacy callers',
  'deprecated alias',
  'deprecated shim',
  'deprecated export',
];

// ---------------------------------------------------------------------------
// Selector groups built from project facts (options).
// ---------------------------------------------------------------------------

const alternation = (names) => names.map((name) => String(name)).join('|');

/** Packages consumed as types only (generated API types, a contracts package). */
export function selTypeOnlyImports(packages) {
  return packages.map((name) => ({
    selector: `ImportDeclaration[source.value='${name}'][importKind!='type'] ImportDefaultSpecifier, ImportDeclaration[source.value='${name}'][importKind!='type'] ImportSpecifier[importKind!='type']`,
    message: `Import from '${name}' with \`import type\` only: its exports are consumed as types, never as runtime values.`,
  }));
}

/** Pages are code-split: the router imports a page only through lazy(), except the eager ones. */
export function selLazyPages({ importPattern, eager = [] }) {
  const eagerArm =
    eager.length > 0 ? `[source.value!=/${importPattern}(${alternation(eager)})$/]` : '';
  return [
    {
      selector: `ImportDeclaration[source.value=/${importPattern}/]${eagerArm}[importKind!='type']`,
      message: `A page is imported in the router module only through lazy()${eager.length > 0 ? `, except ${eager.join(', ')}` : ''}.`,
    },
  ];
}

/** Breakpoints are a stylesheet fact: no viewport measurement, width literal or viewport hook. */
export function selViewport({ widths = [] }) {
  const groups = [
    {
      selector:
        'MemberExpression[property.name=/^(client|offset|inner|outer)(Width|Height)$/][object.property.name=/^(documentElement|body)$/]',
      message:
        'No viewport measurement in JS: the breakpoints are owned by CSS media queries, and a script that measures the viewport is a second owner of them that drifts on resize.',
    },
    {
      selector:
        'CallExpression[callee.name=/^use(IsDesktop|IsMobile|IsTablet|IsWide|IsNarrow|IsSmallScreen|IsLargeScreen|Breakpoints|MediaQuery|ViewportWidth|ViewportSize|WindowSize|WindowWidth|ScreenSize)$/]',
      message:
        'Do not consult a viewport hook to decide what to render or how to style it: a breakpoint is a stylesheet fact. Use a CSS media query; the one sanctioned width read is the breakpoint hook, for a behavioural branch CSS cannot express.',
    },
  ];
  if (widths.length > 0) {
    const sides = widths.flatMap((width) => [`[left.value=${width}]`, `[right.value=${width}]`]);
    groups.push({
      selector: `BinaryExpression[operator=/^[<>]=?$/]:matches(${sides.join(',')})`,
      message: `${widths.join(', ')} are the breakpoint widths. They belong in a CSS media query, not in a JS comparison.`,
    });
  }
  return groups;
}

const TWO_COLUMNS =
  '/^\\s*(?:1fr|minmax\\(\\s*0\\s*,\\s*1fr\\s*\\))\\s+(?:1fr|minmax\\(\\s*0\\s*,\\s*1fr\\s*\\))\\s*$|^\\s*repeat\\(\\s*2\\s*,\\s*(?:1fr|minmax\\(\\s*0\\s*,\\s*1fr\\s*\\))\\s*\\)\\s*$/';

/** The responsive form grid is one class; a two-column template is not written inline. */
export function selFormGrid(formGridClass) {
  const message = `A two-column grid template is declared inline: it is two columns at every width and forks the responsive form grid. Use the \`${formGridClass}\` class. A grid that is not a form grid (a card gallery, a table row template) is not matched.`;
  return [
    {
      selector: `Property[key.name='gridTemplateColumns'] > Literal[value=${TWO_COLUMNS}]`,
      message,
    },
    {
      selector: `Property[key.name='gridTemplateColumns'] > TemplateLiteral > TemplateElement[value.raw=${TWO_COLUMNS}]`,
      message,
    },
  ];
}

const LOOK_PROPERTIES =
  'background|backgroundColor|backgroundImage|color|border|borderColor|borderWidth|borderRadius|boxShadow|opacity|fontSize|fontWeight|textTransform';

/** A component with a variant API is not re-skinned inline. */
export function selVariantReskin(components) {
  const names = alternation(components);
  const look = `JSXAttribute[name.name='style']:has(Property[key.name=/^(?:${LOOK_PROPERTIES})$/])`;
  return [
    {
      selector: `JSXOpeningElement[name.name=/^(?:${names})$/] > ${look}, JSXOpeningElement[name.property.name=/^(?:${names})$/] > ${look}`,
      message:
        'This re-skins a component that has a variant API: an inline background, border, radius, colour, font size, weight or opacity restates a variant by hand, per call site, off the token set. Pick the variant. Geometry and layout (width, height, padding, display, gap, margin) are not matched.',
    },
  ];
}

/** A field says it is required through the `required` prop of its primitive. */
export const SEL_REQUIRED_MARKER = [
  {
    selector: "JSXAttribute[name.name='aria-required']",
    message:
      'A raw `aria-required` marks a control required without the visible marker. Pass `required` to the field primitive: it draws the marker and sets `aria-required` together.',
  },
  {
    selector: "JSXElement[openingElement.name.name='label'] JSXText[value=/\\*/]",
    message:
      'A required marker is written into label text by hand: a bare `*` has no accessible explanation and does not mark the control required. Use the `required` prop of the field primitive.',
  },
  {
    selector: 'JSXText[value=/^\\s*\\*\\s*$/]',
    message:
      'A hand-drawn required asterisk marks nothing for assistive technology. Use the `required` prop of the field primitive.',
  },
  {
    selector: 'JSXElement > JSXExpressionContainer > Literal[value=/^\\s*\\*\\s*$/]',
    message:
      'A hand-drawn required asterisk marks nothing for assistive technology. Use the `required` prop of the field primitive.',
  },
  {
    selector:
      'JSXText[value=/\\S\\s\\*\\s*$/], JSXExpressionContainer TemplateElement[value.raw=/\\s\\*\\s*$/], JSXExpressionContainer Literal[value=/\\S\\s\\*\\s*$/], JSXAttribute[name.name=/^(label|ariaLabel|title|placeholder)$/] Literal[value=/\\s\\*\\s*$/]',
    message:
      'A label that ends in a typed asterisk draws the required marker by hand. Pass `required` to the field primitive.',
  },
  {
    selector: "JSXSpreadAttribute Property[key.value='aria-required']",
    message:
      'A spread `aria-required` marks a control required without the visible marker. Pass `required` to the field primitive instead.',
  },
];

/** The page content region is the only vertical scroller: no inline vertical scroller. */
export const SEL_SINGLE_SCROLLER = [
  {
    selector:
      "JSXAttribute[name.name='style'] Property:matches([key.name='maxHeight'],[key.value='maxHeight'])",
    message:
      "An inline `maxHeight` clips a panel and gives it its own vertical scroller; the page content region is the page's only vertical scroller. A pinned or floating region is declared in the sanctioned stylesheet with its reason.",
  },
  {
    selector:
      "JSXAttribute[name.name='style'] Property:matches([key.name=/^overflowY?$/],[key.value=/^overflowY?$/]) > Literal[value=/^\\s*(?:auto|scroll)(?:\\s|$)/]",
    message:
      'An inline `overflow`/`overflowY` of auto or scroll nests a second vertical scroller in the page; let the panel grow with its content. `overflowX` is not matched.',
  },
];

/** A page owns no look for a button. */
export const SEL_PAGE_BUTTON_CLASS = [
  {
    selector:
      "JSXAttribute[name.name='className'] Literal[value=/-buttons?\\b|filters?-button/], JSXAttribute[name.name='className'] TemplateElement[value.raw=/-buttons?\\b|filters?-button/]",
    message:
      'A page owns no look for a button: a `*-button` class on a page is a hand-written button style at a call site. Use the button variant, or a primitive that owns the look.',
  },
];

/** Stacked blocks are spaced by the parent's gap, never by an outer stack-gap margin. */
export function selPageMargin(values) {
  const value = `(?:${alternation(values)})(?:px)?`;
  return [
    {
      selector: `JSXAttribute[name.name='style'] Property:matches([key.name=/^margin(?:Top|Bottom)$/],[key.value=/^margin(?:Top|Bottom)$/]) > Literal[raw=/^["']?${value}["']?$/]`,
      message:
        "A stack-gap margin on a page block doubles or replaces the parent's `gap`. Put the space on the parent container. A smaller micro-nudge is allowed.",
    },
  ];
}

const SEPARATOR = '(?:^|\\s)[·→]\\s|\\s[·→](?:\\s|$)';

/** One module formats what a screen displays: separators and `Intl` date/list formatting. */
export const SEL_DISPLAY_FORMAT = [
  {
    selector: `:matches(Literal)[value=/${SEPARATOR}/], TemplateElement[value.raw=/${SEPARATOR}/], JSXText[value=/[·→]/]`,
    message:
      'A separator is written by hand. Join a metadata line, a chain or a list with the formatter module, so the separator has one owner.',
  },
  {
    selector:
      "NewExpression[callee.object.name='Intl'][callee.property.name=/^(?:DateTimeFormat|ListFormat)$/], CallExpression[callee.object.name='Intl'][callee.property.name=/^(?:DateTimeFormat|ListFormat)$/]",
    message:
      'A date or a list is formatted with `Intl` outside the formatter module. Use the formatter module, so every screen reads a value the same way.',
  },
];

/** Raw elements and library components that a primitive wraps. */
export function selWrapped(elements, imports) {
  return [
    ...elements.map(({ element, use }) => ({
      selector: `JSXOpeningElement[name.name='${element}']`,
      message: `A raw \`<${element}>\` is a second implementation beside \`${use}\`. Use \`${use}\`.`,
    })),
    ...imports.map(({ source, names, use }) => ({
      selector: `ImportDeclaration[source.value='${source}'] ImportSpecifier[imported.name=/^(?:${alternation(names)})$/]`,
      message: `${names.join(', ')} from '${source}' are imported only by the primitives that wrap them. Use ${use}.`,
    })),
  ];
}

/** One caps-label class: no inline uppercase transform. */
export function selCapsLabel(capsLabelClass) {
  return [
    {
      selector:
        "Property:matches([key.name='textTransform'],[key.value='textTransform'])[value.value='uppercase']",
      message: `No inline \`textTransform: uppercase\`: the caps label is one class, \`${capsLabelClass}\`. Use it.`,
    },
  ];
}

/** One containment unit: no inline `content-visibility` or intrinsic size. */
export function selContainment(containmentClass) {
  return [
    {
      selector: 'Property[key.name=/^(contentVisibility|containIntrinsic\\w*)$/]',
      message: `No inline \`content-visibility\` or intrinsic size: \`${containmentClass}\` is the one containment unit.`,
    },
  ];
}

// ---------------------------------------------------------------------------
// The restrict plugin: one core rule per group.
// ---------------------------------------------------------------------------

export const RESTRICT_GROUPS = [
  'syntax-base',
  'syntax-type-only-imports',
  'syntax-components',
  'syntax-query-key',
  'syntax-router-strict',
  'syntax-router-lazy',
  'syntax-input-preservation',
  'syntax-timer-literal',
  'syntax-locale-parse',
  'syntax-status-like',
  'syntax-outline',
  'syntax-test-probes',
  'syntax-tests',
  'syntax-e2e',
  'syntax-e2e-fixture-import',
  'syntax-viewport',
  'syntax-form-grid',
  'syntax-variant-reskin',
  'syntax-required-marker',
  'syntax-single-scroller',
  'syntax-wheel',
  'syntax-page-button-class',
  'syntax-page-margin',
  'syntax-display-format',
  'syntax-wrapped',
  'syntax-caps-label',
  'syntax-containment',
  'globals-network',
  'globals-storage',
  'globals-viewport',
  'properties-storage',
  'properties-viewport',
  'imports-query-hooks',
  'imports-test-code',
  'imports-e2e-black-box',
];

const restrictPlugin = defineRestrictPlugin(RESTRICT_GROUPS);

const BAN_TS_COMMENT = [
  'error',
  {
    'ts-expect-error': true,
    'ts-ignore': true,
    'ts-nocheck': true,
    'ts-check': false,
    minimumDescriptionLength: Infinity,
  },
];

const IMPORT_RESOLVER = {
  'import-x/resolver': { node: { extensions: ['.js', '.jsx', '.ts', '.tsx'] } },
};

const DEFAULT_LAYERS = (src) => [
  {
    type: 'pages',
    pattern: `${src}/pages/*`,
    allow: ['pages', 'components', 'hooks', 'api', 'shared'],
  },
  {
    type: 'components',
    pattern: `${src}/components/*`,
    allow: ['components', 'hooks', 'api', 'shared'],
  },
  { type: 'hooks', pattern: `${src}/hooks`, allow: ['hooks', 'api', 'shared'] },
  { type: 'api', pattern: `${src}/api`, allow: ['api', 'shared'] },
  { type: 'shared', pattern: `${src}/{utils,styles,locales,context}`, allow: ['shared'] },
];

// ---------------------------------------------------------------------------
// Shared blocks.
// ---------------------------------------------------------------------------

/** strictTypeChecked + stylisticTypeChecked, sonarjs and the types rules. */
function typedBase(root, files) {
  return {
    basePath: root,
    files,
    extends: [...tseslint.configs.strictTypeChecked, ...tseslint.configs.stylisticTypeChecked],
    languageOptions: { parserOptions: { projectService: true, tsconfigRootDir: root } },
    plugins: { sonarjs },
    rules: {
      ...js.configs.recommended.rules,
      // The compiler owns undeclared identifiers; the typed extension owns unused bindings.
      'no-undef': 'off',
      'no-unused-vars': 'off',
      ...sonarjs.configs.recommended.rules,
      // Core `no-useless-catch` (from the JS preset) already owns this.
      'sonarjs/no-useless-catch': 'off',
      'sonarjs/no-duplicate-string': ['error', { threshold: 5 }],
      '@typescript-eslint/prefer-readonly': 'error',
      '@typescript-eslint/strict-boolean-expressions': 'error',
      '@typescript-eslint/switch-exhaustiveness-check': [
        'error',
        {
          considerDefaultExhaustiveForUnions: false,
          requireDefaultForNonUnion: true,
          allowDefaultCaseForExhaustiveSwitch: false,
        },
      ],
      '@typescript-eslint/consistent-type-assertions': [
        'error',
        { assertionStyle: 'as', objectLiteralTypeAssertions: 'never' },
      ],
      '@typescript-eslint/no-non-null-assertion': 'error',
      '@typescript-eslint/no-explicit-any': 'error',
      '@typescript-eslint/consistent-type-imports': 'error',
      '@typescript-eslint/consistent-type-exports': 'error',
      '@typescript-eslint/ban-ts-comment': BAN_TS_COMMENT,
      '@typescript-eslint/naming-convention': [
        'error',
        { selector: 'typeLike', format: ['PascalCase'] },
        {
          selector: 'variableLike',
          format: ['camelCase', 'PascalCase', 'UPPER_CASE'],
          leadingUnderscore: 'allow',
        },
      ],
    },
  };
}

/** Imports, barrels and security hygiene for type-checked files. */
function hygiene(root, files) {
  return [
    {
      basePath: root,
      files,
      plugins: { 'import-x': importX, 'barrel-files': barrelFiles },
      settings: IMPORT_RESOLVER,
      rules: {
        'import-x/no-cycle': 'error',
        'import-x/no-duplicates': 'error',
        'import-x/no-relative-packages': 'error',
        'import-x/no-self-import': 'error',
        'barrel-files/avoid-barrel-files': 'error',
        'barrel-files/avoid-re-export-all': 'error',
      },
    },
    {
      basePath: root,
      files,
      plugins: {
        'no-unsanitized': noUnsanitized.configs.recommended.plugins['no-unsanitized'],
        regexp: regexpPlugin,
        unicorn,
      },
      rules: {
        ...noUnsanitized.configs.recommended.rules,
        'no-eval': 'error',
        'no-new-func': 'error',
        'no-implied-eval': 'error',
        'no-console': 'error',
        'no-alert': 'error',
        'no-debugger': 'error',
        'use-isnan': 'error',
        ...regexpPlugin.configs['flat/recommended'].rules,
        'regexp/no-super-linear-backtracking': 'error',
        'unicorn/prefer-node-protocol': 'error',
        'unicorn/prefer-number-properties': 'error',
        'unicorn/throw-new-error': 'error',
        'unicorn/prefer-type-error': 'error',
        'unicorn/filename-case': ['error', { case: 'kebabCase' }],
        'unicorn/no-array-sort': 'error',
        'unicorn/no-array-reverse': 'error',
      },
    },
    {
      basePath: root,
      files,
      rules: { 'local/no-unsafe-model-assertion': 'error' },
    },
  ];
}

/** Rules that need the list of type-only packages (a generated API types package). */
function packageRules(root, files, { typeOnlyPackages, reexportPackages, wireTypes }) {
  const blocks = [];
  if (typeOnlyPackages.length > 0) {
    blocks.push({
      basePath: root,
      files,
      rules: {
        'restrict/syntax-type-only-imports': ['error', ...selTypeOnlyImports(typeOnlyPackages)],
      },
    });
  }
  if (reexportPackages.length > 0) {
    blocks.push({
      basePath: root,
      files,
      rules: { 'local/no-reexport-package': ['error', { packages: reexportPackages }] },
    });
  }
  if (wireTypes !== null) {
    blocks.push({
      basePath: root,
      files,
      rules: { 'local/no-hand-written-wire-types': ['error', { root, ...wireTypes }] },
    });
  }
  return blocks;
}

// ---------------------------------------------------------------------------
// Presets.
// ---------------------------------------------------------------------------

/**
 * Plain Node tooling scripts and configs: JS recommended, Node globals, no type information.
 *
 * @param {string} root
 * @param {{ files?: string[], cjs?: string[] }} [options]
 */
export function nodePreset(root, options = {}) {
  const files = options.files ?? ['**/*.{js,mjs}'];
  const cjs = options.cjs ?? ['**/*.cjs'];
  return [
    {
      basePath: root,
      files,
      languageOptions: { ecmaVersion: 'latest', sourceType: 'module', globals: globals.node },
      plugins: { '@eslint/js': js },
      rules: { ...js.configs.recommended.rules },
    },
    {
      basePath: root,
      files: cjs,
      languageOptions: { ecmaVersion: 'latest', sourceType: 'commonjs', globals: globals.node },
      plugins: { '@eslint/js': js },
      rules: { ...js.configs.recommended.rules },
    },
  ];
}

/**
 * A TypeScript package without React.
 *
 * @param {string} root
 * @param {object} options
 * @param {string[]} [options.files] the package's type-checked sources.
 * @param {string[]} [options.publicEntry] its public entry modules (the one sanctioned barrel).
 * @param {string[]} [options.typeOnlyPackages] packages imported with `import type` only.
 * @param {string[]} [options.reexportPackages] packages whose exports are never re-exported.
 * @param {object|null} [options.wireTypes] see `app.wireTypes`.
 */
export function libraryPreset(root, options = {}) {
  const files = options.files ?? ['lib/src/**/*.ts'];
  const publicEntry = options.publicEntry ?? [];
  return [
    typedBase(root, files),
    ...hygiene(root, files),
    { basePath: root, files, rules: { 'restrict/syntax-base': ['error', ...SEL_BASE] } },
    // A library's production code imports no development dependency.
    {
      basePath: root,
      files,
      plugins: { 'import-x': importX },
      settings: IMPORT_RESOLVER,
      rules: { 'import-x/no-extraneous-dependencies': ['error', { devDependencies: false }] },
    },
    ...(publicEntry.length > 0
      ? [
          {
            basePath: root,
            files: publicEntry,
            plugins: { 'barrel-files': barrelFiles },
            rules: { 'barrel-files/avoid-barrel-files': 'off' },
          },
        ]
      : []),
    ...packageRules(root, files, {
      typeOnlyPackages: options.typeOnlyPackages ?? [],
      reexportPackages: options.reexportPackages ?? [],
      wireTypes: options.wireTypes ?? null,
    }),
  ];
}

/**
 * Playwright end-to-end specs.
 *
 * @param {string} root
 * @param {object} [options]
 * @param {string[]} [options.files] the specs and their helpers.
 * @param {string} [options.fixtures] the one module that re-exports `test`/`expect`.
 * @param {string[]} [options.appSourceImports] import patterns (gitignore-style) of application
 *   source a black-box spec never imports; `[]` turns the ban off.
 * @param {string} [options.searchWords] regular expression source (case-insensitive) of the words
 *   that name a search, filter or sort control in the UI languages of the application; the default
 *   is English (`search|filter|find|sort`).
 */
export function e2ePreset(root, options = {}) {
  const files = options.files ?? ['e2e/**/*.ts'];
  const fixtures = options.fixtures ?? 'e2e/fixtures.ts';
  const appSourceImports = options.appSourceImports ?? ['**/src/**'];
  return [
    ...(appSourceImports.length > 0
      ? [
          {
            basePath: root,
            files,
            rules: {
              'restrict/imports-e2e-black-box': [
                'error',
                {
                  patterns: [
                    {
                      group: appSourceImports,
                      message:
                        'End-to-end specs drive the application from outside: no import of application source.',
                    },
                  ],
                },
              ],
            },
          },
        ]
      : []),
    typedBase(root, files),
    ...hygiene(root, files),
    {
      basePath: root,
      files,
      plugins: { 'import-x': importX },
      settings: IMPORT_RESOLVER,
      rules: { 'import-x/no-extraneous-dependencies': ['error', { devDependencies: true }] },
    },
    {
      basePath: root,
      files,
      plugins: { playwright: playwright.configs['flat/recommended'].plugins.playwright },
      rules: {
        ...playwright.configs['flat/recommended'].rules,
        'playwright/no-wait-for-timeout': 'error',
        'playwright/no-wait-for-selector': 'error',
        'playwright/no-networkidle': 'error',
        'playwright/no-element-handle': 'error',
        'playwright/no-eval': 'error',
        'playwright/no-page-pause': 'error',
        'playwright/no-force-option': 'error',
        'playwright/prefer-web-first-assertions': 'error',
        'playwright/missing-playwright-await': 'error',
        'playwright/no-focused-test': 'error',
        'playwright/no-skipped-test': 'error',
        'playwright/prefer-native-locators': 'error',
        'playwright/no-raw-locators': 'error',
        'playwright/require-tags': 'error',
        'local/settled-before-action': [
          'error',
          ...(options.searchWords == null ? [] : [{ searchWords: options.searchWords }]),
        ],
      },
    },
    {
      basePath: root,
      files,
      rules: {
        'restrict/syntax-base': ['error', ...SEL_BASE],
        'restrict/syntax-e2e': ['error', ...SEL_E2E],
      },
    },
    {
      basePath: root,
      files,
      ignores: [fixtures],
      rules: { 'restrict/syntax-e2e-fixture-import': ['error', ...SEL_E2E_FIXTURE_IMPORT] },
    },
  ];
}

function escapeRegex(text) {
  return text.replaceAll(/[.*+?^${}()|[\]\\]/gu, String.raw`\$&`);
}

/** A path glob (`*`, `**`) as the source of a regular expression anchored at a path segment. */
function globToRegexSource(glob) {
  const body = escapeRegex(glob)
    .replaceAll(String.raw`\*\*/`, '(?:.*/)?')
    .replaceAll(String.raw`\*\*`, '.*')
    .replaceAll(String.raw`\*`, '[^/]*');
  return `(?:^|/)${body}$`;
}

/**
 * A React + TanStack + Vitest application.
 *
 * @param {string} root
 * @param {object} [options]
 * @param {string} [options.src] application source directory.
 * @param {string[]} [options.testSupport] unit-test support outside `src` (type-checked, linted as tests).
 * @param {string} [options.apiClient] the one module allowed to call fetch/XHR/WebSocket/EventSource.
 * @param {string} [options.storage] the one module allowed to touch web storage and cookies.
 * @param {string} [options.apiDir] the api modules: the only place TanStack Query hooks are imported.
 * @param {string[]} [options.entryPoints] modules that create the QueryClient besides the api modules.
 * @param {string} [options.router] the module that declares the route tree.
 * @param {string} [options.components] the components directory.
 * @param {string} [options.pages] the pages directory.
 * @param {string[]} [options.localeModules] modules that implement locale-aware parsing.
 * @param {boolean} [options.localisation] the display-formatting rules (`no-display-list-join`,
 *   `no-straight-quote-template`, `no-unformatted-number`). Off unless `true`; they expect a
 *   formatter module, named by `designSystem.formatModule` (default `utils/format.ts`).
 * @param {boolean} [options.literalStrings] i18next/no-literal-string on JSX text.
 * @param {boolean} [options.fastRefresh] react-refresh/only-export-components.
 * @param {string} [options.reactVersion] React version for the React plugins.
 * @param {{type: string, pattern: string, allow: string[]}[]} [options.layers] boundaries layers.
 * @param {string[]} [options.typeOnlyPackages] packages imported with `import type` only.
 * @param {string[]} [options.reexportPackages] packages whose exports are never re-exported (default: typeOnlyPackages).
 * @param {{openapi?: string, generatedDir?: string, importFrom?: string}|null} [options.wireTypes] the API description whose schema names may not be redeclared.
 * @param {{metaKey?: string, keys?: string[]}|null} [options.mutationFeedback] every useMutation declares meta[metaKey].
 * @param {{namePattern?: string, use?: string}|null} [options.rawMetric] a number named like namePattern is never rendered raw.
 * @param {{fieldPattern?: string, allowEqualityPairs?: string[][]}|null} [options.servedVerdict] no threshold over a field whose type carries a boolean verdict.
 * @param {object} [options.designSystem] design-system facts; see kit.config.example.mjs.
 */
export function appPreset(root, options = {}) {
  const src = options.src ?? 'src';
  const source = [`${src}/**/*.{ts,tsx}`];
  const unitTests = [`${src}/**/*.test.{ts,tsx}`];
  const testSupport = options.testSupport ?? ['tests/**/*.{ts,tsx}'];
  const apiDir = options.apiDir ?? `${src}/api`;
  const apiClient = options.apiClient ?? `${apiDir}/client.ts`;
  const storage = options.storage ?? `${src}/utils/storage.ts`;
  const entryPoints = options.entryPoints ?? [`${src}/main.tsx`];
  const router = options.router ?? `${src}/router.tsx`;
  const componentsDir = options.components ?? `${src}/components`;
  const pagesDir = options.pages ?? `${src}/pages`;
  const localeModules = options.localeModules ?? [
    `${src}/utils/**/*.{ts,tsx}`,
    `${src}/i18n*/**/*.{ts,tsx}`,
  ];
  const localisation = options.localisation ?? false;
  const reactVersion = options.reactVersion ?? '18.3.1';
  const layers = options.layers ?? DEFAULT_LAYERS(src);
  const typeOnlyPackages = options.typeOnlyPackages ?? [];
  const reexportPackages = options.reexportPackages ?? typeOnlyPackages;
  const ds = options.designSystem ?? {};
  const primitives = ds.primitives ?? `${componentsDir}/primitives/**/*.{ts,tsx}`;
  const styleModules = ds.styleModules ?? [
    `${src}/styles/**/*.{ts,tsx}`,
    `${src}/theme/**/*.{ts,tsx}`,
  ];
  // Layout facts handed to the local rules that recognise a module by where it lives.
  const formatModuleOptions =
    ds.formatModule == null
      ? {}
      : { formatModule: globToRegexSource(ds.formatModule), moduleName: ds.formatModule };
  const apiSegment = apiDir.split('/').filter(Boolean).at(-1) ?? 'api';
  const apiModulePattern = `(^|/)${escapeRegex(apiSegment)}/[A-Za-z0-9-]+$`;

  const components = [`${componentsDir}/**/*.{ts,tsx}`];
  const pages = [`${pagesDir}/**/*.{ts,tsx}`];
  const queryKeys = `${src}/**/*query-keys.ts`;
  const allTests = [...unitTests, ...testSupport];
  const typed = [...source, ...testSupport];
  const jsx = [`${src}/**/*.tsx`, ...testSupport.filter((glob) => glob.endsWith('tsx}'))];
  const production = { files: source, ignores: unitTests };
  const syntax = (group, list, scope = production) => ({
    basePath: root,
    ...scope,
    rules: { [`restrict/syntax-${group}`]: ['error', ...list] },
  });

  const GLOBAL_NETWORK = ['fetch', 'XMLHttpRequest', 'WebSocket', 'EventSource'].map((name) => ({
    name,
    message: `Use the API client module (${apiClient}), not ${name} directly.`,
  }));
  const GLOBAL_STORAGE = ['localStorage', 'sessionStorage'].map((name) => ({
    name,
    message: `Use the storage module (${storage}).`,
  }));
  const PROP_STORAGE = [
    ['window', 'localStorage'],
    ['window', 'sessionStorage'],
    ['document', 'cookie'],
  ].map(([object, property]) => ({
    object,
    property,
    message: `Use the storage module (${storage}).`,
  }));
  const VIEWPORT_MESSAGE = 'Use a CSS breakpoint, not a JS viewport query.';
  const GLOBAL_VIEWPORT = ['matchMedia', 'innerWidth', 'outerWidth'].map((name) => ({
    name,
    message: VIEWPORT_MESSAGE,
  }));
  const PROP_VIEWPORT = [
    ['window', 'matchMedia'],
    ['window', 'innerWidth'],
    ['window', 'outerWidth'],
    ['screen', 'width'],
  ].map(([object, property]) => ({ object, property, message: VIEWPORT_MESSAGE }));
  const PATTERNS_TEST_CODE = [
    {
      regex: '(^|/)tests/',
      message: 'Test support is not production code: an application module never imports it.',
    },
    {
      regex: '\\.test(\\.[jt]sx?)?$',
      message: 'A unit test is not production code: an application module never imports one.',
    },
  ];

  const blocks = [
    typedBase(root, typed),
    ...hygiene(root, typed),

    // -- JSX: React, hooks, accessibility.
    {
      basePath: root,
      files: jsx,
      plugins: {
        react,
        'react-hooks': reactHooks,
        'react-refresh': reactRefresh,
        'jsx-a11y': jsxA11y,
        '@eslint-react': eslintReact,
        i18next,
      },
      settings: {
        react: { version: reactVersion },
        'react-x': { version: reactVersion },
        'jsx-a11y': { polymorphicPropNames: ['as'] },
      },
      rules: {
        'react/no-unescaped-entities': 'error',
        'jsx-a11y/no-static-element-interactions': 'error',
        'jsx-a11y/no-noninteractive-element-interactions': 'error',
        'jsx-a11y/click-events-have-key-events': 'error',
        'jsx-a11y/interactive-supports-focus': 'error',
        'jsx-a11y/no-noninteractive-tabindex': ['error', { roles: ['tabpanel'] }],
        'jsx-a11y/aria-props': 'error',
        'jsx-a11y/aria-proptypes': 'error',
        'jsx-a11y/aria-role': 'error',
        'jsx-a11y/aria-unsupported-elements': 'error',
        'jsx-a11y/role-has-required-aria-props': 'error',
        'jsx-a11y/role-supports-aria-props': 'error',
        'jsx-a11y/no-aria-hidden-on-focusable': 'error',
        'jsx-a11y/label-has-associated-control': ['error', { assert: 'either', depth: 3 }],
        'react-hooks/rules-of-hooks': 'error',
        'react-hooks/exhaustive-deps': 'error',
        'react-hooks/purity': 'error',
        'react-hooks/refs': 'error',
        'react-hooks/immutability': 'error',
        'react-hooks/globals': 'error',
        'react-hooks/set-state-in-render': 'error',
        'react-hooks/set-state-in-effect': 'error',
        'react-hooks/static-components': 'error',
        'react-hooks/use-memo': 'error',
        'react-hooks/error-boundaries': 'error',
        '@eslint-react/no-leaked-conditional-rendering': 'error',
        '@eslint-react/no-nested-component-definitions': 'error',
        '@eslint-react/dom-no-dangerously-set-innerhtml': 'error',
        '@eslint-react/dom-no-script-url': 'error',
        'react-refresh/only-export-components':
          options.fastRefresh === true ? ['error', { allowConstantExport: true }] : 'off',
        'i18next/no-literal-string':
          options.literalStrings === true ? ['error', { mode: 'jsx-text-only' }] : 'off',
      },
    },

    // -- browser boundaries: one module owns the network, one owns web storage.
    {
      basePath: root,
      files: source,
      ignores: [...unitTests, apiClient],
      rules: { 'restrict/globals-network': ['error', ...GLOBAL_NETWORK] },
    },
    {
      basePath: root,
      files: source,
      ignores: [...unitTests, storage],
      rules: { 'restrict/globals-storage': ['error', ...GLOBAL_STORAGE] },
    },
    {
      basePath: root,
      files: source,
      ignores: [storage],
      rules: { 'restrict/properties-storage': ['error', ...PROP_STORAGE] },
    },

    // -- data layer: TanStack Query, its hooks imported only in the api modules.
    {
      basePath: root,
      files: typed,
      plugins: {
        '@tanstack/query': tanstackQuery.configs['flat/recommended'][0].plugins['@tanstack/query'],
      },
      rules: { ...tanstackQuery.configs['flat/recommended'][0].rules },
    },
    {
      basePath: root,
      files: source,
      ignores: [`${apiDir}/**`, ...entryPoints, ...allTests],
      rules: {
        'restrict/imports-query-hooks': [
          'error',
          {
            paths: [
              {
                name: '@tanstack/react-query',
                message:
                  'Import TanStack Query hooks only in the api modules, never directly in a component, hook or page.',
              },
            ],
          },
        ],
      },
    },
    {
      basePath: root,
      ...production,
      rules: { 'restrict/imports-test-code': ['error', { patterns: PATTERNS_TEST_CODE }] },
    },

    // -- router: TanStack Router.
    {
      basePath: root,
      files: typed,
      plugins: {
        '@tanstack/router':
          tanstackRouter.configs['flat/recommended'][0].plugins['@tanstack/router'],
      },
      rules: { ...tanstackRouter.configs['flat/recommended'][0].rules },
    },
    // A route loader or guard throws `redirect()` / `notFound()`, which are not Error objects.
    { basePath: root, files: [router], rules: { '@typescript-eslint/only-throw-error': 'off' } },

    // -- dependencies and layering.
    {
      basePath: root,
      ...production,
      plugins: { 'import-x': importX },
      settings: IMPORT_RESOLVER,
      rules: { 'import-x/no-extraneous-dependencies': ['error', { devDependencies: false }] },
    },
    {
      basePath: root,
      files: allTests,
      plugins: { 'import-x': importX },
      settings: IMPORT_RESOLVER,
      rules: { 'import-x/no-extraneous-dependencies': ['error', { devDependencies: true }] },
    },
    {
      basePath: root,
      files: source,
      plugins: { boundaries },
      settings: {
        'boundaries/root-path': root,
        'boundaries/include': source,
        'boundaries/elements': layers.map(({ type, pattern }) => ({ type, pattern })),
      },
      rules: {
        'boundaries/dependencies': [
          'error',
          {
            default: 'disallow',
            policies: layers.map(({ type, allow }) => ({
              from: { element: { type } },
              allow: allow.map((to) => ({ to: { element: { type: to } } })),
            })),
          },
        ],
      },
    },

    // -- local custom rules.
    { basePath: root, ...production, rules: { 'local/no-query-data-in-state': 'error' } },
    {
      basePath: root,
      files: components,
      ignores: unitTests,
      rules: { 'local/no-data-hook-in-presentational': ['error', { apiModulePattern }] },
    },
    {
      basePath: root,
      files: jsx,
      ignores: unitTests,
      rules: {
        'local/aria-name-needs-role': 'error',
        'local/no-img-role-with-text': 'error',
        'local/img-requires-dimensions': 'error',
        'local/row-action-names-row': 'error',
        'local/decorative-glyph-aria-hidden': 'error',
        'local/no-pending-label-swap': 'error',
        'local/no-view-state-in-route': ['error', { router: `/${router}` }],
      },
    },
    ...(localisation
      ? [
          {
            basePath: root,
            files: source,
            ignores: [...unitTests, ...testSupport],
            rules: {
              'local/no-display-list-join': ['error', formatModuleOptions],
              'local/no-straight-quote-template': 'error',
              'local/no-unformatted-number': ['error', formatModuleOptions],
            },
          },
        ]
      : []),
    ...packageRules(root, typed, {
      typeOnlyPackages,
      reexportPackages,
      wireTypes: options.wireTypes ?? null,
    }),
    ...(options.mutationFeedback == null
      ? []
      : [
          {
            basePath: root,
            files: source,
            ignores: allTests,
            rules: { 'local/mutation-declares-feedback': ['error', options.mutationFeedback] },
          },
        ]),
    ...(options.rawMetric == null
      ? []
      : [
          {
            basePath: root,
            ...production,
            rules: { 'local/no-raw-metric-render': ['error', options.rawMetric] },
          },
        ]),
    ...(options.servedVerdict == null
      ? []
      : [
          {
            basePath: root,
            ...production,
            rules: { 'local/no-threshold-over-served-verdict': ['error', options.servedVerdict] },
          },
        ]),
    ...(ds.translateNoClass == null
      ? []
      : [
          {
            basePath: root,
            files: source,
            ignores: [...unitTests, ...testSupport],
            rules: { 'local/class-translate-no': ['error', { className: ds.translateNoClass }] },
          },
        ]),

    // -- unit tests: Vitest, Testing Library, jest-dom.
    {
      basePath: root,
      files: allTests,
      plugins: {
        vitest: vitestPlugin,
        'testing-library': testingLibrary,
        'jest-dom': jestDom,
      },
      rules: {
        ...vitestPlugin.configs.recommended.rules,
        'vitest/no-focused-tests': 'error',
        'vitest/no-disabled-tests': 'error',
        'vitest/expect-expect': 'error',
        'vitest/valid-title': 'error',
        'vitest/no-identical-title': 'error',
        'vitest/no-conditional-expect': 'off',
        ...testingLibrary.configs['flat/react'].rules,
        ...jestDom.configs['flat/recommended'].rules,
      },
    },

    // -- selector groups without project facts.
    syntax('base', SEL_BASE, { files: typed }),
    syntax('components', SEL_COMPONENTS, { files: components, ignores: [...allTests, queryKeys] }),
    syntax('query-key', SEL_QUERY_KEY, { files: source, ignores: [...unitTests, queryKeys] }),
    syntax('router-strict', SEL_ROUTER_STRICT),
    syntax('input-preservation', SEL_INPUT_PRESERVATION),
    syntax('timer-literal', SEL_TIMER_LITERAL),
    syntax('locale-parse', SEL_LOCALE_PARSE, {
      files: source,
      ignores: [...unitTests, ...localeModules],
    }),
    syntax('status-like', SEL_STATUS_LIKE),
    syntax('outline', SEL_OUTLINE),
    syntax('test-probes', SEL_TEST_PROBES),
    syntax('tests', SEL_TESTS, { files: allTests }),
  ];

  // -- selector groups built from design-system facts (each off until its option is set).
  if (ds.lazyPages != null) {
    blocks.push(syntax('router-lazy', selLazyPages(ds.lazyPages), { files: [router] }));
  }
  if (ds.breakpoints != null) {
    const hook = ds.breakpoints.hook;
    const hookIgnores = hook == null ? [] : [hook];
    blocks.push(
      syntax('viewport', selViewport(ds.breakpoints), {
        files: source,
        ignores: [...unitTests, ...hookIgnores],
      }),
      {
        basePath: root,
        files: source,
        ignores: [...unitTests, ...hookIgnores],
        rules: { 'restrict/globals-viewport': ['error', ...GLOBAL_VIEWPORT] },
      },
      {
        basePath: root,
        files: source,
        ignores: hookIgnores,
        rules: { 'restrict/properties-viewport': ['error', ...PROP_VIEWPORT] },
      },
    );
    if (hook != null) {
      // The breakpoint hook may ask `matchMedia` for a breakpoint and nothing else.
      blocks.push({
        basePath: root,
        files: [hook],
        rules: {
          'restrict/properties-viewport': [
            'error',
            ...PROP_VIEWPORT.filter((entry) => entry.property !== 'matchMedia'),
          ],
        },
      });
    }
  }
  if (ds.formGridClass != null) {
    blocks.push(
      syntax('form-grid', selFormGrid(ds.formGridClass), {
        files: source,
        ignores: [...unitTests, ...styleModules],
      }),
    );
  }
  if ((ds.variantComponents ?? []).length > 0) {
    blocks.push(
      syntax('variant-reskin', selVariantReskin(ds.variantComponents), {
        files: source,
        ignores: [...unitTests, ...styleModules],
      }),
    );
  }
  if (ds.requiredMarker === true) {
    blocks.push(
      syntax('required-marker', SEL_REQUIRED_MARKER, {
        files: source,
        ignores: [...allTests, primitives],
      }),
    );
  }
  if (ds.singleScroller != null) {
    blocks.push(
      syntax('single-scroller', SEL_SINGLE_SCROLLER, {
        files: source,
        ignores: [...unitTests, ...(ds.singleScroller.exempt ?? [])],
      }),
      syntax('wheel', SEL_WHEEL),
    );
  }
  if (ds.pageButtonClass === true) {
    blocks.push(
      syntax('page-button-class', SEL_PAGE_BUTTON_CLASS, { files: pages, ignores: allTests }),
    );
  }
  if ((ds.pageMargins ?? []).length > 0) {
    blocks.push(
      syntax('page-margin', selPageMargin(ds.pageMargins), { files: pages, ignores: allTests }),
    );
  }
  if (ds.formatModule != null) {
    blocks.push(
      syntax('display-format', SEL_DISPLAY_FORMAT, {
        files: source,
        ignores: [...unitTests, ds.formatModule],
      }),
    );
  }
  const wrapped = selWrapped(ds.wrappedElements ?? [], ds.wrappedImports ?? []);
  if (wrapped.length > 0) {
    blocks.push(syntax('wrapped', wrapped, { files: source, ignores: [...unitTests, primitives] }));
  }
  if (ds.capsLabelClass != null) {
    blocks.push(
      syntax('caps-label', selCapsLabel(ds.capsLabelClass), {
        files: source,
        ignores: [...unitTests, ...styleModules],
      }),
    );
  }
  if (ds.containmentClass != null) {
    blocks.push(syntax('containment', selContainment(ds.containmentClass)));
  }
  return blocks;
}

// ---------------------------------------------------------------------------
// The whole config.
// ---------------------------------------------------------------------------

/**
 * The kit's flat config: global ignores, the plugins, the enabled presets, the comment net and
 * eslint-config-prettier (last). A preset is enabled unless its option is `false`; `library` is
 * enabled only when given.
 *
 * @param {object} [options]
 * @param {string} [options.root] project root: basePath of every glob and the typed-lint root.
 * @param {string[]} [options.ignores] extra global ignores.
 * @param {object|false} [options.node] see nodePreset.
 * @param {object|false} [options.app] see appPreset.
 * @param {object|false} [options.library] see libraryPreset.
 * @param {object|false} [options.e2e] see e2ePreset.
 */
export function defineKitConfig(options = {}) {
  const root = options.root ?? process.cwd();
  const enabled = (value) => value !== false && value !== undefined;
  const app = options.app === undefined ? {} : options.app;
  const e2e = options.e2e === undefined ? {} : options.e2e;
  const node = options.node === undefined ? {} : options.node;
  const library = options.library ?? false;

  const typedFiles = [
    ...(enabled(app)
      ? [`${app.src ?? 'src'}/**/*.{ts,tsx}`, ...(app.testSupport ?? ['tests/**/*.{ts,tsx}'])]
      : []),
    ...(enabled(e2e) ? (e2e.files ?? ['e2e/**/*.ts']) : []),
    ...(enabled(library) ? (library.files ?? ['lib/src/**/*.ts']) : []),
  ];

  return tseslint.config(
    // -- global ignores: build, dependency and report artefacts, and rule fixtures.
    {
      basePath: root,
      ignores: [
        '**/node_modules/**',
        '**/.venv/**',
        '**/dist/**',
        '**/coverage/**',
        '**/playwright-report/**',
        '**/test-results/**',
        '**/*.tsbuildinfo',
        '**/fixtures/**',
        ...(options.ignores ?? []),
      ],
    },
    // -- the kit's plugins, registered for every file.
    { plugins: { local: localPlugin, restrict: restrictPlugin } },

    ...(enabled(node) ? nodePreset(root, node) : []),
    // -- every other .ts/.tsx (standalone configs, scattered helpers): parsed, not type-checked.
    {
      basePath: root,
      files: ['**/*.{ts,tsx}'],
      ignores: typedFiles,
      languageOptions: {
        parser: tseslint.parser,
        parserOptions: { ecmaFeatures: { jsx: true } },
      },
      plugins: { '@typescript-eslint': tseslint.plugin },
      rules: {
        ...js.configs.recommended.rules,
        'no-undef': 'off',
        'no-unused-vars': 'off',
        '@typescript-eslint/ban-ts-comment': BAN_TS_COMMENT,
      },
    },
    ...(enabled(app) ? appPreset(root, app) : []),
    ...(enabled(library) ? libraryPreset(root, library) : []),
    ...(enabled(e2e) ? e2ePreset(root, e2e) : []),

    // -- canonical names, everywhere.
    {
      basePath: root,
      files: ['**/*.{ts,tsx,js,mjs,cjs}'],
      rules: {
        'local/no-module-binding-alias': 'error',
        'local/no-renamed-export': 'error',
      },
    },
    // -- suppression and superseded-name comments, anywhere.
    {
      basePath: root,
      files: ['**/*.{ts,tsx,js,jsx,mjs,cjs}'],
      rules: {
        'no-warning-comments': ['error', { terms: BANNED_COMMENT_TERMS, location: 'anywhere' }],
      },
    },

    // Formatting rules that would conflict with Prettier: always last (it only turns rules off).
    eslintConfigPrettier,
  );
}

export default defineKitConfig();
