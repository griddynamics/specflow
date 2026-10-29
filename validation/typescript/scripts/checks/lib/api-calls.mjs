/**
 * Finds the HTTP client call sites of a TypeScript source file, and matches a
 * call's path against a contract path -- one path-matching rule for every
 * caller.
 *
 * Which functions are HTTP calls is configuration: a map from callee name to
 * HTTP method (`DEFAULT_CALLEES` below, or the `callees` object of the
 * contracts file).
 *
 * An endpoint's identity is its HTTP method plus its normalised pathname:
 * query parameters never take part, on either side.
 */

import { calleeName, pathLikeValue, walk, ts } from './ts.mjs';

/** Callee name -> HTTP method, used when the contracts file names none. */
export const DEFAULT_CALLEES = {
  httpGet: 'GET',
  httpPost: 'POST',
  httpPut: 'PUT',
  httpPatch: 'PATCH',
  httpDelete: 'DELETE',
};

/** Every `<callee>(path, ...)` call in a source file, as
 * `{method, path, node}[]`; `path` is the literal string or a template
 * shape with `{}` markers for interpolations (`pathLikeValue`), or `null`
 * when the path is a computed, non-literal expression this checker cannot
 * read statically. */
export function findApiCalls(sourceFile, callees = DEFAULT_CALLEES) {
  const out = [];
  for (const node of walk(sourceFile)) {
    if (!ts.isCallExpression(node)) continue;
    const name = calleeName(node);
    const method = name === null ? undefined : callees[name];
    if (!method || node.arguments.length === 0) continue;
    out.push({ method, path: pathLikeValue(node.arguments[0]), node });
  }
  return out;
}

/** `/orders/{id}/lines` (contract) or `/orders/${id}/lines` (code, already
 * normalised to `{}` by `pathLikeValue`) -> a structural pattern comparable
 * regardless of the placeholder's name: every `{...}` segment becomes a
 * single `*` wildcard token, so `{id}` and `{}` compare equal. */
function structuralPattern(pathText) {
  return pathText
    .split('/')
    .map((segment) => (/^\{.*\}$/.test(segment) || segment === '{}' ? '*' : segment))
    .join('/');
}

/**
 * The pathname of a path-like string: everything before the query. Drops an
 * explicit `?...` suffix and a trailing `{}` interpolation glued to the
 * previous segment -- a dynamically built query suffix (`` `/orders${qs}` ``
 * -> `/orders{}`). A `{}` that is its own segment is never dropped: that is a
 * path parameter (`` `/orders/${id}` `` -> `/orders/{}`).
 */
function pathname(pathText) {
  const q = pathText.indexOf('?');
  const base = q === -1 ? pathText : pathText.slice(0, q);
  return /[^/]\{\}$/.test(base) ? base.slice(0, -2) : base;
}

/** True if a call's path (already through `pathLikeValue`) structurally
 * matches a contract path (`{param}`-style). Endpoint identity is the
 * normalised pathname, so a query string on either side -- declared
 * (`/orders?status=&page=`) or built by the client -- never affects matching. */
export function pathsMatch(callPath, contractPath) {
  if (callPath === null) return false;
  return structuralPattern(pathname(callPath)) === structuralPattern(pathname(contractPath));
}
