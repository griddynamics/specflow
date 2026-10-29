// Stand-in for a formatter module at a non-default path: the format-module options of
// `no-unformatted-number` and `no-display-list-join` recognise a formatter by the file its
// declaration lives in.
export declare function renderNumber(value: number, locale: string): string;
export declare function renderList(values: readonly string[], locale: string): string;
export declare const LIST_SEPARATOR: string;
