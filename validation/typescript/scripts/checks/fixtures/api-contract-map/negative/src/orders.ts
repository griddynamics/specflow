declare function httpGet(path: string): unknown;

export const missing = () => httpGet('/missing');
export const ambiguous = (id: string) => httpGet(`/orders/${id}`);
