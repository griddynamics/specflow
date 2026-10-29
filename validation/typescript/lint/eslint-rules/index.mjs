/**
 * The kit's local ESLint plugin: custom rules that need scope, AST structure or type
 * information that no published plugin offers. `eslint.config.mjs` registers it as `local`.
 * Rules that need project facts take them as rule options; `eslint.config.mjs` passes them from
 * the preset options.
 */
import { rule as noModuleBindingAlias } from './rules/no-module-binding-alias.mjs';
import { rule as noRenamedExport } from './rules/no-renamed-export.mjs';
import { rule as noQueryDataInState } from './rules/no-query-data-in-state.mjs';
import { rule as noUnsafeModelAssertion } from './rules/no-unsafe-model-assertion.mjs';
import { rule as noDataHookInPresentational } from './rules/no-data-hook-in-presentational.mjs';
import { rule as noDisplayListJoin } from './rules/no-display-list-join.mjs';
import { rule as noStraightQuoteTemplate } from './rules/no-straight-quote-template.mjs';
import { rule as noUnformattedNumber } from './rules/no-unformatted-number.mjs';
import {
  ariaNameNeedsRole,
  noImgRoleWithText,
  imgRequiresDimensions,
} from './rules/a11y-semantics.mjs';
import { rule as rowActionNamesRow } from './rules/row-action-names-row.mjs';
import { rule as decorativeGlyphAriaHidden } from './rules/decorative-glyph-aria-hidden.mjs';
import { rule as noViewStateInRoute } from './rules/no-view-state-in-route.mjs';
import { rule as noPendingLabelSwap } from './rules/no-pending-label-swap.mjs';
import { rule as settledBeforeAction } from './rules/settled-before-action.mjs';
import { rule as mutationDeclaresFeedback } from './rules/mutation-declares-feedback.mjs';
import { rule as classTranslateNo } from './rules/class-translate-no.mjs';
import { rule as noReexportPackage } from './rules/no-reexport-package.mjs';
import { rule as noHandWrittenWireTypes } from './rules/no-hand-written-wire-types.mjs';
import { rule as noRawMetricRender } from './rules/no-raw-metric-render.mjs';
import { rule as noThresholdOverServedVerdict } from './rules/no-threshold-over-served-verdict.mjs';

export const plugin = {
  meta: { name: 'local', version: '1.0.0' },
  rules: {
    'no-module-binding-alias': noModuleBindingAlias,
    'no-renamed-export': noRenamedExport,
    'no-query-data-in-state': noQueryDataInState,
    'no-unsafe-model-assertion': noUnsafeModelAssertion,
    'no-data-hook-in-presentational': noDataHookInPresentational,
    'no-display-list-join': noDisplayListJoin,
    'no-straight-quote-template': noStraightQuoteTemplate,
    'no-unformatted-number': noUnformattedNumber,
    'aria-name-needs-role': ariaNameNeedsRole,
    'no-img-role-with-text': noImgRoleWithText,
    'img-requires-dimensions': imgRequiresDimensions,
    'row-action-names-row': rowActionNamesRow,
    'decorative-glyph-aria-hidden': decorativeGlyphAriaHidden,
    'no-view-state-in-route': noViewStateInRoute,
    'no-pending-label-swap': noPendingLabelSwap,
    'settled-before-action': settledBeforeAction,
    'mutation-declares-feedback': mutationDeclaresFeedback,
    'class-translate-no': classTranslateNo,
    'no-reexport-package': noReexportPackage,
    'no-hand-written-wire-types': noHandWrittenWireTypes,
    'no-raw-metric-render': noRawMetricRender,
    'no-threshold-over-served-verdict': noThresholdOverServedVerdict,
  },
};
