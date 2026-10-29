/**
 * Does a component choose what it renders by one of its props? Used by
 * `local/no-view-state-in-route` to follow a state value into a child
 * (`<Tabs value={tab} onChange={setTab} /><Body tab={tab} />`).
 *
 * The answer comes from the declaration the checker resolves, never from a name. The prop is
 * followed from the parameter (a destructured binding, or `props.x`) and from a `const` derived
 * from it, up to a few hops. It selects content when it is read by the test of a conditional, an
 * `&&`, an `if` or a `switch` that yields at least two renderings between them, or by the index of
 * a lookup that yields an element or the component rendered next. A prop passed on to a further
 * component's prop is followed there, to a bounded depth.
 */
import ts from 'typescript';
import { resolveComponent } from './repetition.mjs';

const MAX_HOPS = 3;
const MAX_DEPTH = 3;

function isJsxNode(node) {
  return ts.isJsxElement(node) || ts.isJsxSelfClosingElement(node) || ts.isJsxFragment(node);
}

function yieldsContent(node) {
  if (node === undefined) return false;
  if (isJsxNode(node)) return true;
  return ts.forEachChild(node, (child) => yieldsContent(child) || undefined) === true;
}

function containsReturn(node) {
  if (node === undefined) return false;
  if (ts.isReturnStatement(node)) return true;
  if (ts.isFunctionLike(node)) return false;
  return ts.forEachChild(node, (child) => containsReturn(child) || undefined) === true;
}

/** The TS nodes (parameter bindings and `props.x` accesses) that carry the prop. */
function propRoots(checker, component, propName) {
  const [parameter] = component.parameters;
  const roots = { symbols: new Set(), propsSymbol: undefined };
  if (parameter === undefined) return roots;
  if (ts.isObjectBindingPattern(parameter.name)) {
    for (const element of parameter.name.elements) {
      const source = element.propertyName ?? element.name;
      if (ts.isIdentifier(source) && source.text === propName && ts.isIdentifier(element.name)) {
        const symbol = checker.getSymbolAtLocation(element.name);
        if (symbol) roots.symbols.add(symbol);
      }
    }
  } else if (ts.isIdentifier(parameter.name)) {
    roots.propsSymbol = checker.getSymbolAtLocation(parameter.name);
  }
  return roots;
}

/**
 * Whether the component renders by `propName`.
 * @param {import('typescript').TypeChecker} checker
 * @param {import('typescript').Node} component a function node
 * @param {string} propName
 */
export function choosesContentByProp(checker, component, propName, depth = 0, seen = new Set()) {
  const key = `${component.pos}:${component.getSourceFile().fileName}:${propName}`;
  if (depth >= MAX_DEPTH || seen.has(key) || component.body === undefined) return false;
  seen.add(key);
  const { symbols, propsSymbol } = propRoots(checker, component, propName);
  if (symbols.size === 0 && propsSymbol === undefined) return false;

  function reads(node, hops = 0) {
    if (ts.isPropertyAccessExpression(node) && propsSymbol !== undefined) {
      if (
        ts.isIdentifier(node.expression) &&
        checker.getSymbolAtLocation(node.expression) === propsSymbol &&
        node.name.text === propName
      ) {
        return true;
      }
    }
    if (ts.isIdentifier(node)) {
      const symbol = checker.getSymbolAtLocation(node);
      if (symbol === undefined) return false;
      if (symbols.has(symbol)) return true;
      const declaration = symbol.valueDeclaration;
      return (
        hops < MAX_HOPS &&
        declaration !== undefined &&
        ts.isVariableDeclaration(declaration) &&
        declaration.initializer !== undefined &&
        reads(declaration.initializer, hops + 1)
      );
    }
    return ts.forEachChild(node, (child) => reads(child, hops) || undefined) === true;
  }

  let alternatives = 0;
  let lookup = false;
  let forwards = false;

  function isElementType(node) {
    const type = checker.getNonNullableType(checker.getTypeAtLocation(node));
    return (type.isUnion() ? type.types : [type]).some((part) => part.symbol?.name === 'Element');
  }

  function visit(node) {
    if (ts.isConditionalExpression(node) && reads(node.condition)) {
      alternatives += [node.whenTrue, node.whenFalse].filter(yieldsContent).length;
    } else if (
      ts.isBinaryExpression(node) &&
      (node.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandToken ||
        node.operatorToken.kind === ts.SyntaxKind.BarBarToken ||
        node.operatorToken.kind === ts.SyntaxKind.QuestionQuestionToken) &&
      reads(node.left) &&
      yieldsContent(node.right)
    ) {
      alternatives += 1;
    } else if (ts.isIfStatement(node) && reads(node.expression)) {
      alternatives += [node.thenStatement, node.elseStatement].filter(yieldsContent).length;
      // `if (x) return <A />; return <B />;`: the rest of the function is the other rendering.
      if (node.elseStatement === undefined && containsReturn(node.thenStatement)) alternatives += 1;
    } else if (ts.isSwitchStatement(node) && reads(node.expression)) {
      alternatives += node.caseBlock.clauses.filter((clause) => yieldsContent(clause)).length;
    } else if (
      ts.isElementAccessExpression(node) &&
      reads(node.argumentExpression) &&
      isElementType(node)
    ) {
      lookup = true;
    } else if (
      (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) &&
      ts.isIdentifier(node.tagName)
    ) {
      // `const Pane = PANES[tab]; <Pane />`: the lookup's index chooses the component.
      const declaration = checker.getSymbolAtLocation(node.tagName)?.valueDeclaration;
      const init = declaration && ts.isVariableDeclaration(declaration) && declaration.initializer;
      if (init && ts.isElementAccessExpression(init) && reads(init.argumentExpression)) {
        lookup = true;
      }
      // A prop handed on to another component's prop is followed there.
      for (const attribute of node.attributes.properties) {
        if (
          !forwards &&
          ts.isJsxAttribute(attribute) &&
          attribute.initializer !== undefined &&
          ts.isJsxExpression(attribute.initializer) &&
          attribute.initializer.expression !== undefined &&
          reads(attribute.initializer.expression)
        ) {
          const child = resolveComponent(checker, node.tagName);
          if (
            child !== undefined &&
            choosesContentByProp(checker, child, attribute.name.getText(), depth + 1, seen)
          ) {
            forwards = true;
          }
        }
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(component.body);
  return forwards || lookup || alternatives >= 2;
}
