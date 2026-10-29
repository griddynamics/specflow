// Stand-in for `src/utils/format.ts` in typed RuleTester cases: the render-sink analysis
// recognises a formatter by the file its declaration lives in, so these declarations stand in
// for the real ones.
export declare function formatNumber(value: number, locale: string): string;
export declare function formatList(values: readonly string[], locale: string): string;
export declare function formatInline(values: readonly string[]): string;
export declare const LIST_INPUT_SEPARATOR: string;
