// FE-FILES-02, FE-DS-02 (R5): port of `Prose` in bundle.js. Styles for rendered Markdown (`paper` = ground-alt sheet).
// The content is React children: document text goes through MarkdownProse, and the gallery writes its samples as elements
// (bundle.js's `html` string prop is not ported: no markup is injected as raw HTML).
import type { ReactNode, Ref } from "react";
import { cx, truthy } from "./utils";

export interface ProseProps {
  children?: ReactNode;
  paper?: boolean;
  className?: string;
  innerRef?: Ref<HTMLElement>;
}

export function Prose({ children, paper, className, innerRef }: Readonly<ProseProps>) {
  const cls = cx("gdg-prose", truthy(paper) && "gdg-paper", className);
  return (
    <article className={cls} ref={innerRef}>
      {children}
    </article>
  );
}
