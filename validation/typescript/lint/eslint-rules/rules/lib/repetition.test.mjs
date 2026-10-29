import { ESLintUtils } from '@typescript-eslint/utils';
import { createRepetition } from './repetition.mjs';
import { CROSS_FILE, typed, withPrelude } from './probe-setup.mjs';

/** Reports `repeated|once:<varies>` for each element carrying a `probe={expr}` attribute. */
const probe = {
  meta: { type: 'problem', schema: [], messages: {} },
  create(context) {
    const services = ESLintUtils.getParserServices(context);
    const repetition = createRepetition(services, context.sourceCode);
    return {
      JSXElement(node) {
        const attribute = node.openingElement.attributes.find(
          (a) => a.type === 'JSXAttribute' && a.name.name === 'probe',
        );
        if (!attribute) return;
        const row = repetition.rowContext(node);
        const varies = row ? repetition.isRowVarying(attribute.value, row) : false;
        context.report({ node, message: `${row ? 'repeated' : 'once'}:${String(varies)}` });
      },
    };
  },
};

const expects = (code, message, filename) => ({
  ...withPrelude(code, filename),
  errors: [{ message }],
});

typed.run('lib/repetition', probe, {
  valid: [],
  invalid: [
    expects('const el = <i probe={fixed} />;', 'once:false'),
    // Iteration methods are recognised by the receiver's type, not the method name.
    expects('rows.map((row) => <i probe={row.name} />);', 'repeated:true'),
    expects('rows.flatMap((row) => [<i probe={row.name} />]);', 'repeated:true'),
    expects(
      'tags.forEach((tag) => { run(<i probe={tag.name} />); });\ndeclare function run(x: unknown): void;',
      'repeated:true',
    ),
    expects('Array.from({ length: 2 }, (_, index) => <i probe={index} />);', 'repeated:true'),
    expects(
      'for (const row of rows) { run(<i probe={row.id} />); }\ndeclare function run(x: unknown): void;',
      'repeated:true',
    ),
    expects(
      'for (let index = 0; index < 2; index += 1) { run(<i probe={index} />); }\ndeclare function run(x: unknown): void;',
      'repeated:true',
    ),
    // A callback of a non-collection call is not an iteration.
    expects("'a'.replace(/a/u, () => <i probe={fixed} />);", 'once:false'),
    expects(
      'declare function later(cb: () => unknown): void;\nlater(() => <i probe={fixed} />);',
      'once:false',
    ),
    // Data flow: a value derived from the row varies; a constant does not.
    expects(
      'rows.map((row) => { const { name } = row; const label = `${name}!`; return <i probe={label} />; });',
      'repeated:true',
    ),
    expects('rows.map((row) => <i probe={fixed} key={row.id} />);', 'repeated:false'),
    expects(
      'rows.map((row) => { const label = fixed; return <i probe={label} />; });',
      'repeated:false',
    ),
    // A component rendered in an iteration is repeated, and so is what it renders.
    expects(
      'const Inner = (props: { id: string }) => <i probe={props.id} />;\nconst Item = () => <Inner id="a" />;\nrows.map(() => <Item />);',
      'repeated:true',
    ),
    // A bare while loop has no per-item value: nothing is repeated.
    expects(
      'while (fixed.length < 3) { run(<i probe={fixed} />); }\ndeclare function run(x: unknown): void;',
      'once:false',
    ),
    // A function argument is not an iteration: `then`'s handlers carry a `length` too.
    expects(
      'declare const job: Promise<string>;\njob.then(() => undefined, (e) => <i probe={fixed} />);',
      'once:false',
    ),
    // A component mounted behind component state is a dialog: it shows for one item.
    expects(
      'import { useState } from \'react\';\nconst Dialog = (props: { id: string }) => <i probe={props.id} />;\nconst Cell = () => { const [shown] = useState(false); const open = shown; return <>{open && <Dialog id="a" />}</>; };\nrows.map(() => <Cell />);',
      'once:false',
    ),
    // A condition on props, permissions, query data or loading does not exempt.
    expects(
      'const Edit = (props: { id: string }) => <i probe={props.id} />;\nconst Cell = (props: { allowed: boolean }) => <>{props.allowed && <Edit id="a" />}</>;\nrows.map(() => <Cell allowed />);',
      'repeated:true',
    ),
    expects(
      'declare const loading: boolean;\nconst Edit = (props: { id: string }) => <i probe={props.id} />;\nconst Cell = () => <>{loading ? null : <Edit id="a" />}</>;\nrows.map(() => <Cell />);',
      'repeated:true',
    ),
    // useId() differs for every instance of a repeated component.
    expects(
      "import { useId } from 'react';\nconst Item = () => { const labelId = useId(); return <i probe={labelId} />; };\nrows.map(() => <Item />);",
      'repeated:true',
    ),
    // A component nobody repeats renders once.
    expects(
      'const Lonely = (props: { id: string }) => <i probe={props.id} />;\nconst el = <Lonely id="a" />;',
      'once:false',
    ),
    // Cross-file: repeat-list.tsx renders PairRow inside pairs.map.
    expects(
      'export const PairRow = ({ pair }: { pair: { label: string } }) => <i probe={pair.label} />;',
      'repeated:true',
      CROSS_FILE,
    ),
  ],
});
