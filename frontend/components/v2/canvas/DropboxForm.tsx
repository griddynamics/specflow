"use client";
// Dropbox link: one folder, file or shared link from Dropbox (or the address of a folder or file in it), checked, its files unchecked, then added as a
// source, in the shared file link form (`FileLinkForm`, the Box form's). The acting account is the project's Dropbox integration. A shared link is never
// stored (docs/dropbox/source.md).
import type { FormProps } from "../connector-types";
import { useV2Ports } from "../use-v2-data";
import { CANVAS_COPY } from "./canvas-copy";
import { FileLinkForm } from "./FileLinkForm";
import { dropboxFailureText } from "./dropbox-logic";

const COPY = { link: CANVAS_COPY.dropboxLink, linkHint: CANVAS_COPY.dropboxLinkHint, note: CANVAS_COPY.boxNote };

export function DropboxForm(props: Readonly<FormProps>) {
  const { sources } = useV2Ports();
  const app = {
    copy: COPY,
    check: (pid: string, link: string) => sources.checkDropbox(pid, link),
    add: (pid: string, link: string, exclude: readonly string[]) => sources.addDropbox(pid, link, exclude),
    failureText: dropboxFailureText,
  };
  return <FileLinkForm {...props} app={app} />;
}
