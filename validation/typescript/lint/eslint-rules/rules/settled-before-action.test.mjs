/**
 * RuleTester cases for `settled-before-action` (act on a settled screen), run under
 * `node --test`.
 *
 * Two behaviours: an action on a list member directly after a search, filter or sort step that
 * re-renders that list is refused; an action after a plain form field is filled or selected is
 * not, so a form needs no echo assertion of the value it was just given.
 */
import { describe, it } from 'node:test';
import { RuleTester } from 'eslint';
import tseslint from 'typescript-eslint';
import { rule } from './settled-before-action.mjs';

RuleTester.describe = describe;
RuleTester.it = it;
RuleTester.itOnly = it.only;

const tester = new RuleTester({ languageOptions: { ecmaVersion: 'latest', sourceType: 'module' } });
const typedTester = new RuleTester({ languageOptions: { parser: tseslint.parser } });

/** One async function body made of `lines`. */
const body = (...lines) => `async function journey(page, list) {\n${lines.join('\n')}\n}`;
const unsettled = (action, change) => ({ messageId: 'unsettled', data: { action, change } });

tester.run('settled-before-action: a plain form field re-renders no list', rule, {
  valid: [
    // A form is filled field by field: no field is a member of a list.
    body(
      "await page.getByLabel('User name').fill('admin');",
      "await page.getByLabel('Password').fill('secret');",
      "await page.getByRole('button', { name: 'Sign in' }).click();",
    ),
    // A positional field after another field: the fill re-renders nothing, so no echo is needed.
    body(
      "await page.getByLabel('Minimum').fill('5');",
      "await page.getByLabel('Maximum').first().fill('6');",
    ),
    // A radio of a form after a field of the same form.
    body(
      "await page.getByLabel('Label').fill('Widget');",
      "await page.getByRole('radio', { name: 'Express shipping' }).check();",
    ),
    // A row action after a selection in a form select that filters nothing.
    body(
      "await page.getByLabel('Comparison').selectOption('GTE');",
      "await page.getByRole('row', { name: 'Widget' }).click();",
    ),
    // A key press that leaves a plain field re-renders no list.
    body(
      "await page.getByLabel('Minimum').press('Tab');",
      "await page.getByRole('cell', { name: '5' }).click();",
    ),
    // A plain button after a search is no list member.
    body(
      "await page.getByRole('searchbox').fill('widget');",
      "await page.getByRole('button', { name: 'Add source' }).click();",
    ),
  ],
  invalid: [],
});

tester.run('settled-before-action: a search, filter or sort step re-renders its list', rule, {
  valid: [
    // A web-first assertion of the settled list stands between the search and the click.
    body(
      "await page.getByRole('searchbox').fill('widget');",
      "await expect(page.getByRole('row').filter({ hasText: 'widget' })).toHaveCount(1);",
      "await page.getByRole('row').filter({ hasText: 'widget' }).click();",
    ),
    // `expect.poll` and `expect.soft` are assertions too.
    body(
      "await page.getByLabel('Status filter').selectOption('open');",
      "await expect.poll(() => page.url()).toContain('status=open');",
      "await page.getByRole('option', { name: 'Open' }).click();",
    ),
    body(
      "await page.getByPlaceholder('Search sources').press('Enter');",
      "await expect.soft(page.getByRole('row')).toHaveCount(2);",
      'await page.getByRole("row").first().click();',
    ),
    // A click with no fill, selection or key press before it.
    body("await page.getByRole('row').first().click();"),
    // A nested block is read on its own: the click is not the next statement of the fill's list.
    body(
      "await page.getByRole('searchbox').fill('widget');",
      "if (list) { await page.getByRole('option').first().click(); }",
    ),
  ],
  invalid: [
    // A click on a row taken by position, straight after the search that re-renders the list.
    {
      code: body(
        "await page.getByRole('searchbox').fill('widget');",
        "await page.getByRole('row').first().click();",
      ),
      errors: [unsettled('click', 'fill')],
    },
    // A filtered row, after a selection in a control named as a filter.
    {
      code: body(
        "await page.getByLabel(/status filter/i).selectOption('open');",
        "await page.getByRole('row').filter({ hasText: 'widget' }).check();",
      ),
      errors: [unsettled('check', 'selectOption')],
    },
    // A sort selected by role name re-renders the rows.
    {
      code: body(
        "await page.getByRole('combobox', { name: 'Sort by' }).selectOption('updated');",
        "await page.getByRole('row').nth(1).click();",
      ),
      errors: [unsettled('click', 'selectOption')],
    },
    // A search submitted by Enter in a field found by its placeholder.
    {
      code: body(
        "await page.getByPlaceholder('Search sources').press('Enter');",
        "await page.getByRole('row', { name: 'Widget manual' }).click();",
      ),
      errors: [unsettled('click', 'press')],
    },
    // A search field named in another UI language, passed through the `searchWords` option.
    {
      code: body(
        "await page.getByRole('region', { name: 'Items' }).getByLabel('Recherche').fill('stylo');",
        "await page.getByRole('listitem').last().click();",
      ),
      options: [{ searchWords: 'recherch|trier' }],
      errors: [unsettled('click', 'fill')],
    },
    // A picker's options are the results of the text typed into it, whatever its field is named.
    {
      code: body(
        "await page.getByLabel('Owner').fill('widget');",
        "await page.getByRole('option', { name: 'Widget' }).click();",
      ),
      errors: [unsettled('click', 'fill')],
    },
    // An option after a key press; a statement that asserts nothing does not settle.
    {
      code: body(
        "await page.keyboard.press('ArrowDown');",
        'const count = 1;',
        "await page.getByRole('option', { name: 'Widget' }).click();",
      ),
      errors: [unsettled('click', 'press')],
    },
    // A list member and a search field held in a `const` are read from their declarations.
    {
      code: body(
        "const result = page.getByRole('option', { name: 'Widget' }).last();",
        "const search = page.getByRole('combobox');",
        "await search.fill('widget');",
        'await result.click();',
      ),
      errors: [unsettled('click', 'fill')],
    },
    // A fill of a list member counts as an action too.
    {
      code: body(
        "await page.getByRole('combobox').fill('widget');",
        "await page.getByLabel('Label').nth(1).fill('Widget');",
      ),
      errors: [unsettled('fill', 'fill')],
    },
    // A plain field filled after the search leaves the search unsettled.
    {
      code: body(
        "await page.getByRole('searchbox').fill('widget');",
        "await page.getByLabel('Note').fill('checked');",
        "await page.getByRole('row').first().click();",
      ),
      errors: [unsettled('click', 'fill')],
    },
  ],
});

typedTester.run('settled-before-action: a typed locator is read through its cast', rule, {
  valid: [
    body(
      "await page.getByLabel('Minimum').fill('5');",
      "await (page.getByRole('row').first() as typeof list).click();",
    ),
  ],
  invalid: [
    {
      code: body(
        "await (page.getByRole('searchbox') as typeof list).fill('widget');",
        "await (page.getByRole('row').first() as typeof list).click();",
      ),
      errors: [unsettled('click', 'fill')],
    },
  ],
});
