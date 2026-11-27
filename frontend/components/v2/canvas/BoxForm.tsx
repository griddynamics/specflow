"use client";
// Box link: one folder, file or shared link from Box, a check that lists the files an import would take (all checked; unchecking leaves a file out,
// like the Jira epic's issues and the Drive folder's files), then Add source (one source per link, D5; read once, D7). The acting account is the project's
// Box integration, so a project without it is sent to Integrations from the plus dialog ("Set up first"). A shared link is never stored
// (docs/box/source.md). The form is the shared file link form (`FileLinkForm`).
import type { FormProps } from "../connector-types";
import { useV2Ports } from "../use-v2-data";
import { CANVAS_COPY } from "./canvas-copy";
import { FileLinkForm } from "./FileLinkForm";
import { boxFailureText } from "./box-logic";

const COPY = { link: CANVAS_COPY.boxLink, linkHint: CANVAS_COPY.boxLinkHint, note: CANVAS_COPY.boxNote };

export function BoxForm(props: Readonly<FormProps>) {
  const { sources } = useV2Ports();
  const app = {
    copy: COPY,
    check: (pid: string, link: string) => sources.checkBox(pid, link),
    add: (pid: string, link: string, exclude: readonly string[]) => sources.addBox(pid, link, exclude),
    failureText: boxFailureText,
  };
  return <FileLinkForm {...props} app={app} />;
}
