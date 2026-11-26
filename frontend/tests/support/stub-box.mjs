// Stub of the Box API of docs/box/oauth.md and docs/box/source.md, in memory: the caller's Box connection (`createBoxModel`) and what a Box link is worth to
// the check and to the import (`checkBox`). A test double next to stub-api.mjs, close enough to drive the http adapter, the BFF and the V2 Integrations page and
// Box form; the real API lives in backend/src/api. It never calls Box. `state.enabled` false = Box is not configured (503 `box_unavailable`). The authorize
// URL carries a `state` with `return_to` like the API's (`nonce.payload.signature`); `callback` accepts such a state unless `state.callbackError` names a code.
// Reserved links (app.box.com):
//   /folder/0                    All Files: 422 box_link_invalid
//   /folder/403, /folder/404     409 box_no_access
//   /folder/9999                 a folder "Big archive", 1000 listed files, 20 skipped, capped, files_capped
//   /folder/<other>              a folder "Project files", 12 files (every third one in "design"), 3 skipped, 2 folders
//   /file/<id>                   a file "Spec.pdf", 1 file (/file/404 is 409 box_no_access)
//   /s/password                  422 box_link_password
//   /s/<token>, /v/<name>        a shared folder "Shared folder", 5 files
//   another host, http, a path   422 box_link_invalid
import { createConnectionModel } from "./stub-oauth.mjs";

export const AUTHORIZE_URL = "https://account.box.com/api/oauth2/authorize";
const MAX_FILES = 1000;

const FILE_TYPES = [
  ["pdf", "pdf"],
  ["docx", "word"],
  ["pptx", "powerpoint"],
  ["xlsx", "excel"],
  ["html", "html"],
  ["md", "markdown"],
  ["csv", "csv"],
  ["txt", "text"],
];

/** The listing body of a check (Box, Dropbox): `count` files, every ninth one the app's own kind (`native`: [extension, category]), every third in "design". */
export const fileListing =
  (idOf, native) =>
  (count, skipped, folders, { capped = false } = {}) => {
    const types = [...FILE_TYPES, native];
    const files = Array.from({ length: count }, (_, i) => ({
      id: idOf(i + 1),
      name: `File ${String(i + 1)}.${types[i % 9][0]}`,
      path: i % 3 === 2 ? "design" : "",
      category: types[i % 9][1],
    }));
    return { files, files_capped: capped, skipped, folders, bytes: count * 102400, capped };
  };
const listing = fileListing((n) => String(1000 + n), ["boxnote", "note"]);

/** @returns {{status: number, code: string} | {status: 200, body: object}} */
export function checkBox(link) {
  let url;
  try {
    url = new URL(link);
  } catch {
    return { status: 422, code: "box_link_invalid" };
  }
  const host = url.hostname;
  const boxHost = host === "app.box.com" || host.endsWith(".app.box.com");
  if (url.protocol !== "https:" || url.username !== "" || url.password !== "" || !boxHost) return { status: 422, code: "box_link_invalid" };
  const folder = /^\/folder\/(\d+)\/?$/.exec(url.pathname)?.[1];
  if (folder !== undefined) {
    if (folder === "0") return { status: 422, code: "box_link_invalid" };
    if (folder === "403" || folder === "404") return { status: 409, code: "box_no_access" };
    const big = folder === "9999";
    return {
      status: 200,
      body: {
        kind: "folder",
        name: big ? "Big archive" : "Project files",
        ...(big ? listing(MAX_FILES, 20, 300, { capped: true }) : listing(12, 3, 2)),
      },
    };
  }
  const file = /^\/file\/(\d+)\/?$/.exec(url.pathname)?.[1];
  if (file !== undefined) {
    if (file === "403" || file === "404") return { status: 409, code: "box_no_access" };
    return { status: 200, body: { kind: "file", name: "Spec.pdf", ...listing(1, 0, 0) } };
  }
  const shared = /^\/(?:s|v)\/([^/]+)\/?$/.exec(url.pathname)?.[1];
  if (shared === "password") return { status: 422, code: "box_link_password" };
  if (shared !== undefined) {
    return { status: 200, body: { kind: "folder", name: "Shared folder", ...listing(5, 0, 1) } };
  }
  return { status: 422, code: "box_link_invalid" };
}

/** The caller's Box connection (see `createConnectionModel`; the seam is `POST /seed-box`). */
export const createBoxModel = () =>
  createConnectionModel({
    app: "box",
    authorizeUrl: AUTHORIZE_URL,
    params: { scope: "root_readwrite" },
    workspaceKey: "enterprise_name",
    workspaceName: "Acme Corp",
  });
