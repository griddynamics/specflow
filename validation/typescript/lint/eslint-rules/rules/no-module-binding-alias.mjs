/** Reject module-level alternate names for callable runtime definitions. */
export const rule = {
  meta: {
    type: 'problem',
    docs: { description: 'Use canonical class/function bindings.' },
    schema: [],
    messages: { alias: "Use '{{name}}' directly instead of a module-level class/function alias." },
  },
  create(context) {
    const sourceCode = context.sourceCode;
    return {
      VariableDeclarator(node) {
        if (node.id.type !== 'Identifier' || node.init?.type !== 'Identifier') return;
        const declaration = node.parent;
        const owner = declaration.parent;
        if (
          owner.type !== 'Program' &&
          !(owner.type === 'ExportNamedDeclaration' && owner.parent.type === 'Program')
        ) {
          return;
        }
        const services = sourceCode.parserServices;
        const checker = services.program?.getTypeChecker();
        const typescriptNode = services.esTreeNodeToTSNodeMap?.get(node.init);
        if (checker && typescriptNode) {
          const type = checker.getTypeAtLocation(typescriptNode);
          if (type.getCallSignatures().length || type.getConstructSignatures().length) {
            context.report({ node, messageId: 'alias', data: { name: node.init.name } });
          }
          return;
        }
        const scope = sourceCode.getScope(node);
        const variable = scope.references.find(
          (reference) => reference.identifier === node.init,
        )?.resolved;
        const callable = variable?.defs.some(
          (definition) =>
            definition.type === 'FunctionName' ||
            definition.type === 'ClassName' ||
            (definition.type === 'Variable' &&
              ['ArrowFunctionExpression', 'FunctionExpression', 'ClassExpression'].includes(
                definition.node.init?.type,
              )),
        );
        if (callable) {
          context.report({ node, messageId: 'alias', data: { name: node.init.name } });
        }
      },
    };
  },
};
