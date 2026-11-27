// FE-DATA-01, FE-BP-02, FE-BP-08, SPL-DEMO-01: the production port bundle. http serves what the backend has (`people.me`, `projects`,
// `createProject.create`, the Ready gate `missionControl`, `sources`, `knowledge`, `plan`, `work`, `gaps`, `members`, `jira`, `slack`, `github`); `unavailable` answers empty
// for everything else (connectors, assistant, files, comments, diagrams, the people directory and mentions). No fixture is read.
import type { HttpPorts } from "./http";
import type { DataPorts } from "./ports";
import { unavailable, unavailableIntro } from "./unavailable";

export function composeProductionPorts(http: HttpPorts): DataPorts {
  return {
    ...unavailable,
    people: {
      me: () => http.people.me(), // GET /me (FE-PPL-01.AC3, FE-BP-02.AC8)
      directory: () => Promise.resolve([]), // no users endpoint: no suggestions
      mentionables: () => Promise.resolve([]),
    },
    projects: http.projects,
    createProject: {
      create: (input) => http.createProject.create(input), // POST /projects: the name, and the collaborator emails when given
    },
    missionControl: http.missionControl, // the Ready gate only (>= 1 processed document)
    sources: http.sources, // Drive folders, Local files; no GitHub
    knowledge: http.knowledge,
    plan: http.plan, // the plan API (/projects/{pid}/plan*)
    work: http.work, // findings and claims (/projects/{pid}/findings*, /plan/claims*)
    gaps: http.gaps, // the Gaps screen and the Plan gap links (/projects/{pid}/gaps*, /projects/{pid}/gap/runs/latest), FR-GWEB-04, FR-GWEB-05
    members: http.members, // members, invites, leave and suggestions (/projects/{pid}/members*, /invites*, /leave, /users/suggestions)
    jira: http.jira, // the Jira target, access check and sync status (/projects/{pid}/jira*)
    githubProjects: http.githubProjects, // the GitHub Projects destination (/projects/{pid}/github-projects*)
    notionPlan: http.notionPlan, // the Notion plan destination (/projects/{pid}/notion-plan*)
    notionConnection: http.notionConnection, // the caller's Notion connection (/integrations/notion)
    jiraGaps: http.jiraGaps, // the Jira gap channel (/projects/{pid}/jira-gaps*)
    slack: http.slack, // the Slack channel of a project (/projects/{pid}/slack)
    github: http.github, // the caller's GitHub connection and the project repositories (/integrations/github*, /projects/{pid}/repositories*)
    box: http.box, // the caller's Box connection (/integrations/box*)
    dropbox: http.dropbox, // the caller's Dropbox connection (/integrations/dropbox*)
    integrations: http.integrations, // who each app is connected by for a project (/projects/{pid}/integrations)
    linear: http.linear, // LINEAR S1: the caller's Linear connection (/integrations/linear*)
    linearPlan: http.linearPlan, // LINEAR S3: the Linear plan destination (/projects/{pid}/linear-plan*)
    linearSource: http.linearSource, // LINEAR S4: a Linear project or issue as a source (/projects/{pid}/sources/linear*)
    linearGaps: http.linearGaps, // LINEAR S5: the Linear gap channel (/projects/{pid}/linear-gaps*)
    assistant: {
      ...unavailable.assistant,
      // The SpecBot greeting names the signed-in user (GET /me). If /me fails, the greeting has no name.
      async intro() {
        try {
          return unavailableIntro(firstName((await http.people.me()).name));
        } catch {
          return unavailableIntro("");
        }
      },
    },
  };
}

const firstName = (name: string) => name.trim().split(/\s+/)[0] ?? "";
