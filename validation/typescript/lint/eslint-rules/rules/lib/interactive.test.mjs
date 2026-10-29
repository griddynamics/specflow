import { ESLintUtils } from '@typescript-eslint/utils';
import { createInteractive } from './interactive.mjs';
import { typed, withPrelude } from './probe-setup.mjs';

/** Reports `control:<tag>:<name sources>` for every control, `none` for the outermost non-control. */
const probe = {
  meta: { type: 'problem', schema: [], messages: {} },
  create(context) {
    const interactive = createInteractive(ESLintUtils.getParserServices(context));
    return {
      JSXElement(node) {
        if (node.parent.type === 'JSXElement') return;
        const control = interactive.describe(node);
        context.report({
          node,
          message: control
            ? `control:${control.tag}:${String(control.nameSources.length)}`
            : 'none',
        });
      },
    };
  },
};

const expects = (code, message) => ({ ...withPrelude(code), errors: [{ message }] });

typed.run('lib/interactive', probe, {
  valid: [],
  invalid: [
    expects('const el = <button>x</button>;', 'control:button:1'),
    expects('const el = <a>x</a>;', 'none'),
    expects('const el = <a href="/x">x</a>;', 'control:a:1'),
    expects('const el = <summary>x</summary>;', 'control:summary:1'),
    expects('const el = <input type="submit" />;', 'control:input:0'),
    expects('const el = <input type="text" />;', 'none'),
    expects('const el = <div role="tab">x</div>;', 'control:div:1'),
    expects('const el = <div role="presentation">x</div>;', 'none'),
    // Name sources: aria-label, title, aria-describedby, spread props, text children.
    expects(
      'const el = <button aria-label="a" title="b" aria-describedby="c">x</button>;',
      'control:button:4',
    ),
    expects('const el = <button {...fixed}>x</button>;', 'control:button:2'),
    // An event prop carrying a function makes a control of any element.
    expects('const el = <span onPointerDown={() => undefined}>x</span>;', 'control:span:1'),
    expects(
      'declare const go: () => void;\nconst el = <span onClick={go}>x</span>;',
      'control:span:1',
    ),
    // Any other function-valued on[A-Z] prop activates.
    expects(
      'declare const go: () => void;\nconst el = <span onDragStart={go}>x</span>;',
      'control:span:1',
    ),
    // Field, focus and container-lifecycle events do not.
    expects(
      'declare const go: () => void;\nconst el = <span onBlur={go} onKeyUp={go} onScroll={go} onClose={go}>x</span>;',
      'none',
    ),
    // A checkbox toggled by a handler is a control; a plain one is not.
    expects(
      'declare const go: () => void;\nconst el = <input type="checkbox" onChange={go} />;',
      'control:input:0',
    ),
    expects('const el = <input type="checkbox" />;', 'none'),
    // aria-hidden removes the element, unless its value is "false".
    expects('const el = <button aria-hidden="true">x</button>;', 'none'),
    expects('const el = <button aria-hidden="false">x</button>;', 'control:button:1'),
    // An onX that is not a function is not an event handler.
    expects('const el = <span onClick={fixed}>x</span>;', 'none'),
    // A field's change handler is not activation.
    expects('declare const go: () => void;\nconst el = <span onChange={go}>x</span>;', 'none'),
    // A navigation prop.
    expects('const el = <Plain to="/x">x</Plain>;', 'control:component:1'),
    // A component is a control by what its props type declares.
    expects('const el = <Button>x</Button>;', 'control:component:1'),
    expects('const el = <Plain>x</Plain>;', 'none'),
    // onClick declared by a library base props type counts (a Button extending a design system's).
    expects(
      "import type { LibraryButtonProps } from './library-props';\ndeclare const Lib: (props: LibraryButtonProps) => null;\nconst el = <Lib>x</Lib>;",
      'control:component:1',
    ),
    // onClick that only React's HTMLAttributes bag supplies proves nothing (a Pill, a Skeleton).
    expects(
      "import type { HTMLAttributes } from 'react';\ndeclare const Tag: (props: HTMLAttributes<HTMLSpanElement>) => null;\nconst el = <Tag>x</Tag>;",
      'none',
    ),
  ],
});
