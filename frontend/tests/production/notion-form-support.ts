// Not a test: what the Notion source form tests share (the form side of stream D2; `canvas-form-support.ts` stays as it is): the stub API served
// for one file, a project with a given Notion binding, and the words the form shows for a link, read through the real http port and the real
// check text, so a test sees what a member would.
import { createElement } from "react";
import { NotionForm } from "@/components/v2/canvas/NotionForm";
import { notionCheckText, notionFailureText } from "@/components/v2/canvas/notion-logic";
import { createHttpProjects } from "@/lib/data/http/projects";
import { createHttpSources } from "@/lib/data/http/sources";
import { serveStub } from "../support/stub-fetch";
import { formProps, html } from "./canvas-form-support";

export const PAGE = "https://www.notion.so/Engineering-Wiki-423ed33606458137b924dd97aa16e0a9";
export const CHILD_PAGE = "https://www.notion.so/423ed33606458143b422d74e5eb9926c";
export const DATABASE = "https://www.notion.so/Requirements-423ed3360645819b8a30d73a847000db";
export const BIG_DATABASE = "https://www.notion.so/Big-423ed3360645819b8a30d73a84700b16";
export const NOT_SHARED = "https://www.notion.so/Gone-423ed33606458137b924dd97aa160404";

export type Binding = "active" | "reconnect_required" | "none";

/** Serves the stub API on `port` for the file; returns what the tests call. */
export function notionFormRig(port: number) {
  const ctx = serveStub(port, () => undefined);
  const sources = createHttpSources({ changed: () => undefined });
  const { createProject } = createHttpProjects();
  return {
    ctx,
    sources,
    /** A new project whose Notion binding is in this state ("none": not bound). */
    async project(binding: Binding = "active"): Promise<string> {
      const p = await createProject.create({ name: "P" });
      if (binding !== "none") ctx.api.integrations.seed({ pid: p.id, notion: { health: binding } });
      return p.id;
    },
    /** The line the form shows after Check link for this link: the passed check's text, or the refusal's words. */
    async checkSays(pid: string, link: string): Promise<string> {
      return sources.checkNotion(pid, link).then(notionCheckText, notionFailureText);
    },
    /** The line the form shows when Add source is refused for this link; "added" when it was not. */
    async addSays(pid: string, link: string): Promise<string> {
      return sources.addNotion(pid, link).then(() => "added", notionFailureText);
    },
  };
}

/** The form before any answer, server-rendered. */
export const emptyForm = (): string => html(createElement(NotionForm, formProps("graph", "notion")));
