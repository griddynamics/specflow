// Stub of the Jira epic source (docs/sources/importers.md "Jira epic", docs/mission-control/integrations-api.md): what a key or link is worth
// to the check and the import. Nothing calls Atlassian. The bound site is the one of `stub-confluence.mjs`. Reserved keys:
//   NWO-12  an epic "Checkout redesign" with 18 issues     NWO-13  an issue that is not an epic (422 jira_epic_not_an_epic)
//   NWO-404 409 jira_epic_not_found                         NWO-403 409 jira_epic_forbidden       NWO-250 an epic of 250 issues (over the cap)
//   a link of another host: 422 atlassian_site_mismatch; anything else that is not a key or a link: 422 jira_epic_link_invalid
import { SITE_HOST } from "./stub-confluence.mjs";

const KEY = /^([A-Z][A-Z0-9]+-\d+)$/;
const EPICS = { "NWO-12": ["Checkout redesign", 18], "NWO-250": ["Everything", 250] };

/** @returns {{status: number, code: string} | {status: 200, body: object}} */
export function checkJiraEpic(keyOrLink) {
  let key = KEY.exec(keyOrLink.trim().toUpperCase())?.[1];
  if (key === undefined) {
    let url;
    try {
      url = new URL(keyOrLink);
    } catch {
      return { status: 422, code: "jira_epic_link_invalid" };
    }
    if (url.protocol !== "https:" || url.username !== "") return { status: 422, code: "jira_epic_link_invalid" };
    if (url.hostname !== SITE_HOST) return { status: 422, code: "atlassian_site_mismatch" };
    key = /\/browse\/([A-Z][A-Z0-9]+-\d+)/.exec(url.pathname)?.[1] ?? url.searchParams.get("selectedIssue") ?? undefined;
    if (key === undefined || !KEY.test(key)) return { status: 422, code: "jira_epic_link_invalid" };
  }
  if (key === "NWO-404") return { status: 409, code: "jira_epic_not_found" };
  if (key === "NWO-403") return { status: 409, code: "jira_epic_forbidden" };
  const epic = EPICS[key];
  if (epic === undefined) return { status: 422, code: "jira_epic_not_an_epic" };
  return { status: 200, body: { key, summary: epic[0], issues: epic[1] } };
}
