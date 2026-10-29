"use client";
// FE-REV-01..04, FE-DS-02 (R5): port of `CommentThread` in bundle.js. A thread anchored to quoted text: comments (the bot's
// highlighted, "typing" shows dots), a resolve button and a composer with @mentions. The draft clears after sending.
import { useState, type Ref } from "react";
import { Avatar } from "./Avatar";
import { Button } from "./Button";
import { GlassSurface } from "./GlassSurface";
import { Icon } from "./Icon";
import { MentionInput } from "./MentionInput";
import { TypingIndicator } from "./TypingIndicator";
import { splitMentions, type Person } from "./app-logic";
import { cx, nonEmpty, truthy } from "./utils";

export interface CommentItem {
  author: string;
  time?: string;
  text?: string;
  kind?: "bot";
  typing?: boolean;
}

export interface CommentThreadProps {
  quote?: string;
  comments: CommentItem[];
  people?: Person[];
  /** Takes the comment. The draft clears once it returns, or once its promise resolves; a throw or a rejection keeps the draft. */
  onSubmit?: (text: string) => void | Promise<void>;
  onResolve?: () => void;
  resolved?: boolean;
  /** Focus the empty comment box on mount (FE-REV-03.AC1). */
  autoFocus?: boolean;
  /** Ref to the comment box. */
  inputRef?: Ref<HTMLTextAreaElement>;
}

export function CommentThread({
  quote,
  comments,
  people = [],
  onSubmit,
  onResolve,
  resolved,
  autoFocus,
  inputRef,
}: Readonly<CommentThreadProps>) {
  const [draft, setDraft] = useState("");
  const send = async (text: string) => {
    if (!text.trim()) return;
    await onSubmit?.(text);
    setDraft("");
  };
  return (
    <GlassSurface variant="strong" className={cx("gdg-thread", truthy(resolved) && "is-resolved")}>
      <div className="gdg-thread__head">
        {truthy(quote) ? <blockquote className="gdg-thread__quote">{quote}</blockquote> : <span />}
        {onResolve ? (
          <button type="button" className="gdg-iconbtn" aria-label="Resolve thread" title="Resolve" onClick={onResolve}>
            <Icon name="check" size={16} />
          </button>
        ) : null}
      </div>
      <ol className="gdg-thread__list">
        {comments.map((c, i) => (
          <li key={i} className={cx("gdg-comment", c.kind === "bot" && "is-bot")} data-testid="comment">
            <Avatar name={c.author} kind={c.kind} size="sm" />
            <div className="gdg-comment__main">
              <div className="gdg-comment__meta">
                <b data-testid="comment-author">{c.author}</b>
                <span>{c.time}</span>
              </div>
              {truthy(c.typing) ? (
                <TypingIndicator />
              ) : (
                <p className="gdg-comment__text" data-testid="comment-text">
                  {splitMentions(nonEmpty(c.text) ?? "", people).map((part, k) =>
                    part.mention ? (
                      <span key={k} className="gdg-mention">
                        {part.text}
                      </span>
                    ) : (
                      part.text
                    ),
                  )}
                </p>
              )}
            </div>
          </li>
        ))}
      </ol>
      {onSubmit ? (
        <div className="gdg-thread__compose">
          <MentionInput
            people={people}
            autoFocus={autoFocus}
            inputRef={inputRef}
            placeholder={comments.length ? undefined : "Add a comment… type @ to mention"}
            value={draft}
            onChange={setDraft}
            onSubmit={(text) => void send(text)}
          />
          <div className="gdg-thread__actions">
            <small>@ to mention · ⌘↵ to send</small>
            <Button
              variant="primary"
              size="sm"
              disabled={!draft.trim()}
              onClick={() => {
                void send(draft);
              }}
            >
              Comment
            </Button>
          </div>
        </div>
      ) : null}
    </GlassSurface>
  );
}
