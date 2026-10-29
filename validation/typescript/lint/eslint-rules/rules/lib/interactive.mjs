/**
 * Is this JSX element a control, and where does its accessible name come from?
 * (WCAG 2.2, WAI-ARIA 1.2.) A control is recognised
 * by structure (intrinsic button, link, summary, button-like input, a checkbox or radio input with
 * an `onChange`), by `role`, by any function-valued `on[A-Z]...` prop it passes (activation) except
 * the reviewed field and focus events in FIELD_EVENTS, by `href` / `to`, or, for a component, by
 * its props type declaring `onClick`, `href` or `to` (the checker decides: declared anywhere but
 * the global DOM typings, so a primitive extending a library's props counts and one inheriting
 * only `HTMLAttributes` does not). No component name is consulted.
 */
import ts from 'typescript';

const CONTROL_ROLES = new Set(['button', 'link', 'menuitem', 'tab', 'option', 'switch']);
const BUTTON_INPUT_TYPES = new Set(['button', 'submit', 'reset']);
const TOGGLE_INPUT_TYPES = new Set(['checkbox', 'radio']);
const ACTIVATION_PROPS = ['onClick', 'href', 'to'];
// Props that never carry a name: React's key, inline style, and a link's navigation payload.
const NOT_NAMING = new Set([
  'key',
  'style',
  'className',
  'id',
  'href',
  'to',
  'params',
  'search',
  'hash',
]);
const NAME_ATTRIBUTES = new Set([
  'aria-label',
  'ariaLabel',
  'aria-labelledby',
  'aria-describedby',
  'title',
  // The visible text a component control (an icon action, a chip) takes as a prop.
  'label',
]);

function attributeNamed(opening, name) {
  return opening.attributes.find(
    (a) => a.type === 'JSXAttribute' && a.name.type === 'JSXIdentifier' && a.name.name === name,
  );
}

function stringAttribute(opening, name) {
  const value = attributeNamed(opening, name)?.value;
  if (value?.type === 'Literal' && typeof value.value === 'string') return value.value;
  if (
    value?.type === 'JSXExpressionContainer' &&
    value.expression.type === 'Literal' &&
    typeof value.expression.value === 'string'
  ) {
    return value.expression.value;
  }
  return undefined;
}

function intrinsicTag(opening) {
  const { name } = opening;
  return name.type === 'JSXIdentifier' && /^[a-z]/u.test(name.name) ? name.name : undefined;
}

function isIntrinsicControl(opening, tag) {
  if (tag === 'button' || tag === 'summary') return true;
  if (tag === 'a') return attributeNamed(opening, 'href') !== undefined;
  if (tag === 'input') {
    const type = stringAttribute(opening, 'type') ?? '';
    // A checkbox or radio toggled by a handler acts per row like a button does.
    return (
      BUTTON_INPUT_TYPES.has(type) ||
      (TOGGLE_INPUT_TYPES.has(type) && attributeNamed(opening, 'onChange') !== undefined)
    );
  }
  return false;
}

/**
 * Reviewed denylist of `on[A-Z]...` props that are not activation: a field's value, focus, scroll,
 * submit, hover and key events, and the lifecycle callbacks a container reports upward. Any other function-valued `on[A-Z]...` prop activates the element.
 */
const FIELD_EVENTS = new Set([
  'onChange',
  'onInput',
  'onBeforeInput',
  'onInvalid',
  'onReset',
  'onSubmit',
  'onBlur',
  'onFocus',
  'onFocusCapture',
  'onBlurCapture',
  'onScroll',
  'onWheel',
  'onKeyUp',
  'onKeyDown',
  'onKeyPress',
  'onMouseEnter',
  'onMouseLeave',
  'onMouseMove',
  'onMouseOver',
  'onMouseOut',
  'onPointerEnter',
  'onPointerLeave',
  'onPointerMove',
  'onLoad',
  'onError',
  // What a container reports upward (a dialog, a list state, a footer): the control inside it is
  // named on its own, and the container is not itself the thing a person activates.
  'onClose',
  'onDismiss',
  'onRetry',
  'onConfirm',
  'onCancel',
  // Delegated to the rows a list component renders inside itself, which name their own targets.
  'onNavigate',
  'onShow',
]);

function isFieldEvent(name) {
  // `onValueChange`, `onOpenChange`: a value change of a component, like `onChange`.
  return FIELD_EVENTS.has(name) || /Change$/u.test(name);
}

function isEventName(name) {
  return /^on[A-Z]/u.test(name);
}

const FALSE_VALUES = new Set(['false']);

/** `aria-hidden` removes the element from the accessibility tree unless its value is "false". */
export function isAriaHidden(opening) {
  const attribute = attributeNamed(opening, 'aria-hidden');
  if (!attribute) return false;
  const value = attribute.value;
  if (value?.type === 'Literal') return !FALSE_VALUES.has(String(value.value));
  if (value?.type === 'JSXExpressionContainer' && value.expression.type === 'Literal') {
    return !FALSE_VALUES.has(String(value.expression.value));
  }
  return true;
}

/** An activation `onX` carrying a function: a literal function, or an expression whose type is callable. */
function passesEventHandler(attribute, checker, services) {
  if (
    attribute.name.type !== 'JSXIdentifier' ||
    !isEventName(attribute.name.name) ||
    isFieldEvent(attribute.name.name)
  ) {
    return false;
  }
  const expression =
    attribute.value?.type === 'JSXExpressionContainer' ? attribute.value.expression : null;
  if (!expression || expression.type === 'JSXEmptyExpression') return false;
  if (expression.type === 'ArrowFunctionExpression' || expression.type === 'FunctionExpression') {
    return true;
  }
  const type = checker.getTypeAtLocation(services.esTreeNodeToTSNodeMap.get(expression));
  return checker.getNonNullableType(type).getCallSignatures().length > 0;
}

/** The attribute's value is data a name can be made of: text, or an object (not a function, boolean or number). */
function carriesData(attribute, checker, services) {
  const expression =
    attribute.value?.type === 'JSXExpressionContainer' ? attribute.value.expression : null;
  if (!expression || expression.type === 'JSXEmptyExpression') return false;
  const type = checker.getNonNullableType(
    checker.getTypeAtLocation(services.esTreeNodeToTSNodeMap.get(expression)),
  );
  const parts = type.isUnion() ? type.types : [type];
  return parts.some(
    (part) =>
      (part.flags & ts.TypeFlags.StringLike) !== 0 ||
      ((part.flags & ts.TypeFlags.Object) !== 0 && part.getCallSignatures().length === 0),
  );
}

/**
 * The global DOM typings: the default library and React's attribute bag (`HTMLAttributes` and its
 * kin in `@types/react`). Every element-shaped component inherits `onClick` from there without
 * being a control (a `Pill`, a `Skeleton`, a table header), so it proves nothing.
 */
function inDomTypings(program, symbol) {
  const declarations = symbol.declarations ?? [];
  return (
    declarations.length > 0 &&
    declarations.every((d) => {
      const file = d.getSourceFile();
      return (
        program.isSourceFileDefaultLibrary(file) ||
        /[\\/]@types[\\/]react[\\/]/u.test(file.fileName)
      );
    })
  );
}

function propsTypeDeclaresActivation(opening, services) {
  const { program } = services;
  const checker = program.getTypeChecker();
  const type = checker.getTypeAtLocation(services.esTreeNodeToTSNodeMap.get(opening.name));
  for (const signature of type.getCallSignatures()) {
    const parameter = signature.parameters[0];
    if (!parameter?.valueDeclaration) continue;
    const props = checker.getTypeOfSymbolAtLocation(parameter, parameter.valueDeclaration);
    for (const name of ACTIVATION_PROPS) {
      const prop = checker.getPropertyOfType(props, name);
      // Declared anywhere but the global DOM typings: a library's props (a design system's `onClick`) or
      // the project's own.
      if (prop && !inDomTypings(program, prop)) return true;
    }
  }
  return false;
}

/**
 * Binds control detection to a linted file. `describe(element)` returns `null` for a
 * non-control, else `{ tag, nameSources }`: the expressions and nodes the accessible name is made
 * from (`aria-label`, `ariaLabel`, `aria-labelledby`, `aria-describedby`, `title`, `label`, spread props
 * that may carry them, and the children).
 */
export function createInteractive(services) {
  const checker = services.program.getTypeChecker();

  function isControl(element) {
    const opening = element.openingElement;
    // Removed from the accessibility tree (a click-away backdrop): nothing to name.
    if (isAriaHidden(opening)) return false;
    const tag = intrinsicTag(opening);
    if (tag !== undefined && isIntrinsicControl(opening, tag)) return true;
    if (CONTROL_ROLES.has(stringAttribute(opening, 'role') ?? '')) return true;
    const props = opening.attributes.filter((a) => a.type === 'JSXAttribute');
    if (
      props.some(
        (a) =>
          (a.name.type === 'JSXIdentifier' && (a.name.name === 'href' || a.name.name === 'to')) ||
          passesEventHandler(a, checker, services),
      )
    ) {
      return true;
    }
    return tag === undefined && propsTypeDeclaresActivation(opening, services);
  }

  function describe(element) {
    if (!isControl(element)) return null;
    const opening = element.openingElement;
    const nameSources = [];
    for (const attribute of opening.attributes) {
      if (attribute.type === 'JSXSpreadAttribute') nameSources.push(attribute.argument);
      else if (
        attribute.name.type === 'JSXIdentifier' &&
        NAME_ATTRIBUTES.has(attribute.name.name) &&
        attribute.value
      ) {
        nameSources.push(attribute.value);
      } else if (
        intrinsicTag(opening) === undefined &&
        !NOT_NAMING.has(attribute.name.name) &&
        carriesData(attribute, checker, services)
      ) {
        // A component handed text or an object (the row, a wording, a heading) can name itself from it.
        nameSources.push(attribute.value);
      }
    }
    nameSources.push(...element.children);
    // `aria-labelledby` / `aria-describedby` are name sources like the rest: they name the control
    // per row only when their expression is row-varying, so a constant id repeated per row is flagged.
    return { tag: intrinsicTag(opening) ?? 'component', nameSources };
  }

  return { isControl, describe };
}
