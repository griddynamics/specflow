/**
 * Whether a blur inside an open menu means the focus left it. A press inside the list is not leaving: browsers that do not focus a button on
 * click (Safari, Firefox on macOS) blur the list with no related target just before the click lands.
 */
export function focusLeftMenu(pressing: boolean, relatedTargetInside: boolean): boolean {
  return !pressing && !relatedTargetInside;
}
