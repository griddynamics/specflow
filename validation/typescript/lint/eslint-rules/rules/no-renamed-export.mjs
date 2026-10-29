/** Export each binding using its canonical name. */
export const rule = {
  meta: {
    type: 'problem',
    docs: { description: 'Use canonical export names.' },
    schema: [],
    messages: {
      renamed: "Export '{{name}}' using its canonical name; remove the alternate export.",
    },
  },
  create(context) {
    return {
      ExportSpecifier(node) {
        const local = node.local.name ?? node.local.value;
        const exported = node.exported.name ?? node.exported.value;
        if (local !== exported) {
          context.report({ node, messageId: 'renamed', data: { name: local } });
        }
      },
    };
  },
};
