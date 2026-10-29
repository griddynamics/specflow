declare function fetchJson(path: string): unknown;
declare function sendJson(path: string): unknown;

// Documented with a query but sent without one, and sent with a dynamic one.
export const plain = () => fetchJson('/orders');
export const filtered = (query: string) => fetchJson(`/orders${query}`);
export const literal = (status: string) => fetchJson(`/orders?status=${status}`);
// A trailing interpolation that is its own segment is a path parameter.
export const one = (id: string) => fetchJson(`/orders/${id}`);
export const cancel = (id: string) => sendJson(`/orders/${id}/cancel`);
