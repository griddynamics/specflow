// SPL-DEMO-01: the small in-memory UI store of the production tree. It keeps only what screens hand to each other:
// the current project id and the SpecBot prefill. Nothing is fetched and nothing persists (a reload starts clean).
import type { Unsubscribe } from "@/lib/domain/common";
import type { UiHandoff } from "./ports";

export interface UiState {
  currentProjectId: string | null; // written only by AppShell (route param, switcher)
  specbotPrefill: { projectId: string; text: string } | null; // exact selected text
}

export interface UiStore {
  get(): UiState;
  update(fn: (s: UiState) => UiState): void; // immutable update, then notify
  subscribe(listener: () => void): Unsubscribe;
}

export function createUiStore(): UiStore {
  let state: UiState = { currentProjectId: null, specbotPrefill: null };
  const listeners = new Set<() => void>();
  return {
    get: () => state,
    update(fn) {
      state = fn(state);
      listeners.forEach((l) => {
        l();
      });
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}

/** The in-memory hand-over between screens (Files "Ask SpecBot" -> SpecBot composer). */
export function createUiHandoff(store: UiStore): UiHandoff {
  return {
    setSpecBotPrefill(projectId, text) {
      store.update((s) => ({ ...s, specbotPrefill: { projectId, text } }));
    },
    takeSpecBotPrefill(projectId) {
      const slot = store.get().specbotPrefill;
      if (slot?.projectId !== projectId) return null;
      store.update((s) => ({ ...s, specbotPrefill: null }));
      return slot.text;
    },
  };
}
