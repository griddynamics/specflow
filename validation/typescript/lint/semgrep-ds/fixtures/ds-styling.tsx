declare const element: HTMLElement;

// ruleid: styling-layout-measurement
const observer = new ResizeObserver(() => undefined);
// ruleid: styling-layout-measurement
const box = element.getBoundingClientRect();
// ok: styling-layout-measurement
const width = element.offsetParent;

export { observer, box, width };
