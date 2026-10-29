/* Minimal react/jsx-runtime shim for UMD builds that expect window.jsxRuntime (e.g. @xyflow/react). Loads after React. */
(function () {
  var R = window.React;
  function jsx(type, props, key) {
    var p = props || {};
    if (key !== undefined) { var q = {}; for (var k in p) q[k] = p[k]; q.key = key; p = q; }
    return R.createElement(type, p);
  }
  window.jsxRuntime = { jsx: jsx, jsxs: jsx, jsxDEV: jsx, Fragment: R.Fragment };
})();
