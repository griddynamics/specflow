// FE-SPEC-01..03, FE-DS-02 (R5): port of `ChatMessage` in bundle.js. User: right-aligned bubble. Assistant: SpecBot avatar,
// name, tool steps, the answer (children) or the typing dots, and citation chips.
import "./ui-gaps-app.css";
import type { ReactNode } from "react";
import { Avatar } from "./Avatar";
import { Citations } from "./Citations";
import { ToolSteps, type ToolStep } from "./ToolSteps";
import { TypingIndicator } from "./TypingIndicator";
import { cx, nonEmpty, truthy } from "./utils";

export interface ChatMessageProps {
  role: "user" | "assistant";
  name?: string;
  steps?: ToolStep[];
  citations?: { label: string; onClick?: () => void }[];
  typing?: boolean;
  children?: ReactNode;
}

export function ChatMessage({ role, name, steps, citations, typing, children }: Readonly<ChatMessageProps>) {
  const user = role === "user";
  return (
    <div className={cx("gdg-msg", user ? "gdg-msg--user" : "gdg-msg--bot")} data-testid={user ? "chat-user" : "chat-bot"}>
      {user ? null : <Avatar name={nonEmpty(name) ?? "SpecBot"} kind="bot" size="md" />}
      <div className="gdg-msg__body">
        {user ? null : (
          <div className="gdg-msg__name" data-testid="chat-name">
            {nonEmpty(name) ?? "SpecBot"}
          </div>
        )}
        {steps ? <ToolSteps steps={steps} /> : null}
        {truthy(typing) ? (
          <TypingIndicator />
        ) : (
          <div className={user ? "gdg-msg__bubble" : "gdg-msg__text"} data-testid={user ? "chat-bubble" : "chat-text"}>
            {children}
          </div>
        )}
        {truthy(citations?.length) ? <Citations citations={citations} /> : null}
      </div>
    </div>
  );
}
