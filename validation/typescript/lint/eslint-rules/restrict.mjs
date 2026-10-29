/**
 * The `restrict` plugin: ESLint's own `no-restricted-syntax`, `no-restricted-globals`,
 * `no-restricted-properties` and `no-restricted-imports`, registered once per selector group under
 * a group name (`restrict/syntax-base`, `restrict/globals-network`, ...).
 *
 * Flat config REPLACES a rule's option array on the last matching config object; it never merges
 * two option arrays for one rule key. With one core rule per group, every group keeps its own
 * `files`/`ignores` scope, no two groups contest one rule key, and a finding names its group. The
 * rule implementations are ESLint's, unchanged: no custom code.
 */
import unsupportedApi from 'eslint/use-at-your-own-risk';

const CORE_RULE = {
  syntax: 'no-restricted-syntax',
  globals: 'no-restricted-globals',
  properties: 'no-restricted-properties',
  imports: 'no-restricted-imports',
};

/**
 * @param {string[]} ids group rule names, each `<kind>-<group>` with kind one of
 *   syntax | globals | properties | imports.
 */
export function defineRestrictPlugin(ids) {
  const rules = {};
  for (const id of ids) {
    const kind = id.slice(0, id.indexOf('-'));
    const coreName = CORE_RULE[kind];
    const core = coreName === undefined ? undefined : unsupportedApi.builtinRules.get(coreName);
    if (core === undefined) {
      throw new Error(`restrict plugin: "${id}" does not start with a known kind`);
    }
    rules[id] = core;
  }
  return { meta: { name: 'restrict', version: '1.0.0' }, rules };
}
