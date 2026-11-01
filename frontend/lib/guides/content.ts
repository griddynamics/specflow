// MCP-M10: every word of the Guides pages (hub, SpecFlow 101, Best practices, MCP server). One module, no JSX, so the copy is easy to
// review and to test. Snippet text is built in ./snippets.ts. Sentence case; UPPERCASE comes from the components' styles.
import type { IconName } from "@/components/ui/icon-map";
import type { GuideTopic } from "@/lib/routes";

export interface TopicCard {
  topic: GuideTopic;
  eyebrow: string;
  title: string;
  icon: IconName;
  summary: string;
  tag: string;
}

export const HUB = {
  title: "Guides", // the visually hidden h1; the visible header is the breadcrumb
  lead: "New to SpecFlow? Start with SpecFlow 101. Ready to build? Connect your coding agent.",
};

export const TOPIC_CARDS: readonly TopicCard[] = [
  {
    topic: "specflow-101",
    eyebrow: "Start here",
    title: "SpecFlow 101",
    icon: "flow-arrow",
    summary: "How SpecFlow turns your documents into a specification, a plan and code.",
    tag: "5 steps",
  },
  {
    topic: "best-practices",
    eyebrow: "Tips",
    title: "Best practices",
    icon: "check-circle",
    summary: "Habits that keep the specification clean and save clean-up work later.",
    tag: "9 tips",
  },
  {
    topic: "mcp-server",
    eyebrow: "Setup",
    title: "MCP server",
    icon: "plug",
    summary: "Connect Claude Code to SpecFlow and bind your repository to a project.",
    tag: "4 steps for Claude Code",
  },
];

export const GUIDES_CRUMB = "Guides";
export const OPEN_LABEL = "Open";

/** The title of a topic, for the breadcrumb and the page. */
export const topicTitle = (topic: GuideTopic): string => TOPIC_CARDS.find((c) => c.topic === topic)?.title ?? topic;

// ---------------------------------------------------------------- SpecFlow 101

export const FLOW_101 = {
  title: "From documents to code in five steps",
  lead: "SpecFlow reads what your team already wrote, turns it into one connected specification, and hands it to your coding agent. Choose a step to see what happens in it.",
  listLabel: "The SpecFlow flow",
  whereLabel: "Where in SpecFlow",
  getLabel: "What you get",
  nextLabel: "Next step",
  connectLabel: "Connect your coding agent",
};

export interface FlowStep {
  icon: IconName;
  name: string;
  detail: string;
  where: string;
  get: string;
}

export const FLOW_STEPS: readonly FlowStep[] = [
  {
    icon: "cloud-arrow-up",
    name: "Connect sources",
    detail:
      "Connect a Google Drive folder or upload files: requirements, specifications, meeting notes. SpecFlow converts each one to Markdown and keeps every version.",
    where: "Mission Control › Sources",
    get: "A clean, versioned set of source documents.",
  },
  {
    icon: "tree-structure",
    name: "Build the graph",
    detail:
      "SpecFlow reads every document and builds the specification graph: modules, features, capabilities and requirements, each linked to the lines it came from. Build it again after your sources change.",
    where: "Mission Control",
    get: "One connected map of the product, traceable to its sources.",
  },
  {
    icon: "list-checks",
    name: "Decide",
    detail:
      "Where documents disagree or leave something out, SpecFlow raises a decision with the evidence. Answer it with your team, assign it to the right person, or ask SpecBot to help.",
    where: "Decisions",
    get: "Agreed answers that the plan builds on.",
  },
  {
    icon: "kanban",
    name: "Plan",
    detail: "SpecFlow builds an implementation plan from the graph and your decisions. Each task links to the requirements it covers.",
    where: "Plan",
    get: "Tasks your team and your agents can pick up.",
  },
  {
    icon: "robot",
    name: "Code",
    detail:
      "Connect Claude Code to the SpecFlow MCP server and bind your repository to the project once. From then on, the agent reads the project from SpecFlow while it works in your repository.",
    where: "Guides › MCP server, then your repository",
    get: "Code that traces back to agreed requirements.",
  },
];

// ---------------------------------------------------------------- Best practices

export const BEST_PRACTICES = {
  title: "Get better results, faster",
  lead: "Small habits that keep the specification clean and save your team clean-up work later.",
  groups: [
    {
      eyebrow: "Step 1",
      title: "Sources",
      tips: [
        {
          advice: "Add only documents about this product",
          reason: "Unrelated files slow every build and add features and decisions you have to clean up afterwards.",
        },
        {
          advice: "Keep one current version of each document",
          reason: "Old drafts next to the final version produce conflicting requirements, which come back to you as decisions.",
        },
        {
          advice: "Prefer a connected Drive folder to one-off uploads",
          reason: "New versions of your documents reach SpecFlow without uploading them again.",
        },
        {
          advice: "Write requirements in words, not only in images",
          reason: "SpecFlow reads text. A requirement that exists only in a screenshot is easy to miss.",
        },
      ],
    },
    {
      eyebrow: "Steps 3 and 4",
      title: "Decisions and the plan",
      tips: [
        {
          advice: "Answer open decisions before you create the plan",
          reason: "The plan builds on your answers; open decisions leave gaps in its tasks.",
        },
        {
          advice: "Use one SpecFlow project per change",
          reason: "A large refactor and a new page are separate projects, even when they land in the same repository.",
        },
      ],
    },
    {
      eyebrow: "Step 5",
      title: "Working with your coding agent",
      tips: [
        {
          advice: "Bind each repository once and commit .specflow/config.json",
          reason: "Your team's agents then work on the same project without asking which one.",
        },
        {
          advice: "Let the agent read SpecFlow instead of pasting specs into the chat",
          reason: "Pasted copies go stale; through MCP the agent always reads the current version.",
        },
        {
          advice: "Review what the agent writes",
          reason: "SpecFlow gives the agent the right context. Your review still decides what ships.",
        },
      ],
    },
  ],
} as const;

// ---------------------------------------------------------------- MCP server

export const MCP_GUIDE = {
  title: "Connect your coding agent",
  lead: "Your coding agent reads your SpecFlow projects through this server. It signs in with your company account and sees only the projects you are a member of. It can read, not change.",
  picker: { label: "Agent", tested: "Tested", inProgress: "In progress", local: "Local development" },
  inProgress: {
    title: (agent: string) => `${agent} setup is in progress`,
    text: "We publish an agent's setup after we have tested it end to end with SpecFlow. Until then, use Claude Code.",
  },
  steps: {
    add: {
      eyebrow: "Step 1",
      title: "Add SpecFlow to Claude Code",
      text: "Run this once in a terminal. It adds SpecFlow to Claude Code for every repository on this computer.",
      caption: "Terminal",
      team: {
        summary: "For your whole team: commit a .mcp.json instead",
        text: "Save this file in the root of your repository and commit it. Everyone who opens the repository in Claude Code gets SpecFlow without running the command.",
        caption: "Save as .mcp.json",
      },
    },
    addLocal: {
      eyebrow: "Step 1",
      title: "Add SpecFlow to Claude Code (local)",
      text: "Run this from the root of your SpecFlow checkout. It registers the local server with a development token that renews itself.",
      caption: "Terminal",
      note: "Claude Code runs the token helper only after you have accepted its workspace trust dialog once for this folder. Keep this entry out of the tracked .mcp.json.",
    },
    signIn: {
      eyebrow: "Step 2",
      title: "Sign in",
      text: "In Claude Code, type /mcp, choose specflow, then Authenticate. Your browser opens your company sign-in. After that, Claude Code keeps you signed in.",
      caption: "In Claude Code",
      note: "Working over SSH? Run claude mcp login specflow --no-browser, open the address it prints, and paste back the address your browser ends on. Keep port 8765 free while you sign in.",
    },
    signInLocal: {
      eyebrow: "Step 2",
      title: "Sign in",
      text: "No sign-in in local development: the token comes from your local dev login.",
    },
    bind: {
      eyebrow: "Step 3",
      title: "Bind your repository to a project",
      text: "Open Claude Code in the repository you will work on and say this once. Claude Code finds the project, saves the choice in .specflow/config.json, and every later session uses it. Commit that file so your team works on the same project.",
      caption: "Say this to Claude Code, inside your repository",
      note: "Moved on to a new SpecFlow project in the same repository? Say it again with the new project's name: it replaces the old choice. Plan › Develop shows a ready command for each project.",
    },
    check: {
      eyebrow: "Step 4",
      title: "Check that it works",
      text: "Ask Claude Code. It should list the projects you are a member of.",
      caption: "Say this to Claude Code",
    },
  },
  reference: {
    eyebrow: "Reference",
    title: "What your agent can use",
    columns: ["Name", "Kind", "What it does"],
    toolKind: "Tool, read-only",
    promptKind: "Command",
    note: "Every call checks that you are a member of the project. More tools arrive with implementation plans.",
    tableLabel: "Tools and commands",
  },
  troubleshooting: {
    eyebrow: "Help",
    title: "Troubleshooting",
    items: [
      {
        symptom: "Sign-in works, but every request fails",
        fix: "Your administrator needs to check the SpecFlow sign-in settings for coding agents. Send them the time it happened.",
      },
      {
        symptom: 'The browser shows "invalid redirect_uri"',
        fix: "Another sign-in may hold port 8765. Close it, then run Authenticate again. Do not change the port.",
      },
      {
        symptom: "Access denied after sign-in",
        fix: "Your company email must be verified, and its domain must be allowed on this SpecFlow. Ask your administrator.",
      },
      {
        symptom: "No projects are listed",
        fix: "You are not a member of a project yet, or you signed in with another account. Open SpecFlow in the browser to check.",
      },
      {
        symptom: "The agent asks for the project in every session",
        fix: "Ask Claude Code to connect the repository to your SpecFlow project, and commit .specflow/config.json.",
      },
      {
        symptom: "SpecFlow tools disappeared",
        fix: "Your sign-in ended. Type /mcp, choose specflow, then Authenticate again.",
      },
    ],
  },
} as const;

// ---------------------------------------------------------------- Plan › Develop

export const DEVELOP = {
  bind: {
    eyebrow: "This project",
    title: (projectName: string) => `Use ${projectName} in your repository`,
    text: "Run this in Claude Code, inside the repository you will change. From then on, your agent works on this project in every session.",
    caption: "In Claude Code, inside your repository",
    file: {
      summary: "Or save the binding file yourself",
      caption: "Save as .specflow/config.json in the repository and commit it",
    },
  },
  ask: {
    eyebrow: "Ask your agent",
    title: "Try these in Claude Code",
    overview: "Get an overview",
    start: "Start the work",
  },
  notConnected: "Claude Code not connected yet?",
  guideLink: "Set up your coding agent in Guides",
  nameMissing: "Could not load this project's name. Reload the page to try again.",
} as const;
