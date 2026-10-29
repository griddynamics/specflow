declare function httpGet(path: string): unknown;
declare function httpPost(path: string, body?: unknown): unknown;

export const listOrders = () => httpGet('/orders');
export const getOrder = (id: string) => httpGet(`/orders/${id}`);
export const createOrder = () => httpPost('/orders', {});
// A computed path cannot be matched statically: skipped, never guessed.
export const byPath = (path: string) => httpGet(path);
