// Stub of the Dropbox API of docs/dropbox/oauth.md and docs/dropbox/source.md, in memory: the caller's Dropbox connection (`createDropboxModel`) and what a Dropbox link is worth to
// the check and to the import (`checkDropbox`). A test double next to stub-api.mjs, close enough to drive the http adapter, the BFF and the V2 Integrations page and
// Dropbox form; the real API lives in backend/src/api. It never calls Dropbox. `state.enabled` false = Dropbox is not configured (503 `dropbox_unavailable`). The authorize
// URL carries a `state` with `return_to` like the API's (`nonce.payload.signature`); `callback` accepts such a state unless `state.callbackError` names a code.
// Reserved links (www.dropbox.com or dropbox.com, https only):
//   /home                        the Dropbox root: 422 dropbox_link_invalid
//   /home/<path>/403, .../404    409 dropbox_no_access (last segment)
//   /home/<path>/big             a folder "Big archive", 1000 listed files, 20 skipped, capped, files_capped
//   /home/<other path>           a folder "Project files", 12 files (every third one in "design"), 3 skipped, 2 folders
//   /scl/fi/<id>/<name>          a file "Spec.pdf", 1 file (<id> "noaccess" is 409 dropbox_no_access)
//   /scl/fo/<id>/<name>, /sh/<id>  a shared folder "Shared folder", 5 files (<id> "noaccess" 409; "password" 422 dropbox_link_invalid)
//   /s/<token>                   as /scl/fo (legacy folder link); /s/password is 422
//   another host, http, a path   422 dropbox_link_invalid
import { fileListing } from "./stub-box.mjs";
import { createConnectionModel } from "./stub-oauth.mjs";

export const AUTHORIZE_URL = "https://www.dropbox.com/oauth2/authorize";
const MAX_FILES = 1000;

const listing = fileListing((n) => `id:file${String(n)}`, ["paper", "paper"]);
const invalid = { status: 422, code: "dropbox_link_invalid" };
const noAccess = { status: 409, code: "dropbox_no_access" };
const ok = (kind, name, rest) => ({ status: 200, body: { kind, name, ...rest } });

/** @returns {{status: number, code: string} | {status: 200, body: object}} */
export function checkDropbox(link) {
  let url;
  try {
    url = new URL(link);
  } catch {
    return invalid;
  }
  const host = url.hostname;
  const dropboxHost = host === "www.dropbox.com" || host === "dropbox.com";
  if (url.protocol !== "https:" || url.username !== "" || url.password !== "" || !dropboxHost) return invalid;
  const parts = url.pathname.split("/").filter((p) => p !== "");
  const head = parts[0];
  if (head === "home") {
    if (parts.length === 1) return invalid;
    const last = decodeURIComponent(parts[parts.length - 1]).toLowerCase();
    if (last === "403" || last === "404") return noAccess;
    if (last === "big") return ok("folder", "Big archive", listing(MAX_FILES, 20, 300, { capped: true }));
    return ok("folder", "Project files", listing(12, 3, 2));
  }
  const scl = head === "scl" && (parts[1] === "fi" || parts[1] === "fo");
  const token = scl ? parts[2] : head === "sh" || head === "s" ? parts[1] : undefined;
  if (token === undefined || token === "password") return invalid;
  if (token === "noaccess") return noAccess;
  if (scl && parts[1] === "fi") return ok("file", "Spec.pdf", listing(1, 0, 0));
  return ok("folder", "Shared folder", listing(5, 0, 1));
}

/** The caller's Dropbox connection (see `createConnectionModel`; the seam is `POST /seed-dropbox`). */
export const createDropboxModel = () =>
  createConnectionModel({
    app: "dropbox",
    authorizeUrl: AUTHORIZE_URL,
    params: { token_access_type: "offline", scope: "account_info.read files.metadata.read files.content.read" },
    workspaceKey: "team_name",
    workspaceName: "Acme Team",
  });
