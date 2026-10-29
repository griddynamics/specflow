"use client";
// FE-SPEC-01, FE-SPEC-04, FE-DS-02 (R5): port of `ChatComposer` in bundle.js. Glass capsule with a textarea and a yellow send
// button; Enter sends (Shift+Enter is a new line); suggestion chips above call `onSuggestion`, or `onSend` when that is not set.
import { Icon } from "./Icon";
import { GlassSurface } from "./GlassSurface";
import { useControlled } from "./use-controlled";
import { nonEmpty, truthy } from "./utils";

export interface ChatComposerProps {
  onSend?: (text: string) => void;
  placeholder?: string;
  suggestions?: string[];
  onSuggestion?: (s: string) => void;
  value?: string;
  defaultValue?: string;
  onChange?: (v: string) => void;
  disabled?: boolean;
  label?: string;
}

export function ChatComposer({
  onSend,
  placeholder,
  suggestions,
  onSuggestion,
  value,
  defaultValue,
  onChange,
  disabled,
  label,
}: Readonly<ChatComposerProps>) {
  const [v, set] = useControlled<string>(value, defaultValue ?? "", onChange);
  function send() {
    if (v.trim() && onSend) {
      onSend(v.trim());
      set("");
    }
  }
  return (
    <div className="gdg-composer-wrap">
      {truthy(suggestions?.length) ? (
        <div className="gdg-suggest">
          {suggestions.map((s, i) => (
            <button
              key={i}
              type="button"
              data-testid="chat-suggestion"
              className="gdg-suggest__chip"
              disabled={disabled}
              onClick={() => {
                if (onSuggestion) onSuggestion(s);
                else onSend?.(s);
              }}
            >
              <Icon name="sparkle" size={14} />
              {s}
            </button>
          ))}
        </div>
      ) : null}
      <GlassSurface variant="strong" floating className="gdg-composer" data-testid="chat-composer">
        <textarea
          rows={1}
          value={v}
          placeholder={nonEmpty(placeholder) ?? "Ask me"}
          aria-label={nonEmpty(label) ?? "Message"}
          disabled={disabled}
          onChange={(e) => {
            set(e.target.value);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              send();
            }
          }}
        />
        <button type="button" className="gdg-composer__send" aria-label="Send" disabled={!v.trim() || disabled} onClick={send}>
          <Icon name="paper-plane-right" size={18} />
        </button>
      </GlassSurface>
    </div>
  );
}
