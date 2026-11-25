import { seeded } from "./rng";

export interface Spot {
  x: number;
  y: number;
}

export interface Fact extends Spot {
  k: string;
  name: string;
  tag: string;
  src: string;
  /** Label side: 1 right, -1 left, 0 centred. */
  side: number;
  dy?: number;
  /** Label place on a narrow screen. */
  narrow?: { side: number; dy: number };
  /** What a search returns for this fact (when found / when missed). */
  hit?: string;
  miss?: string;
  /** How the agent reaches the fact in the graph, and the MCP tool that returns it. */
  how: string;
  tool: string;
}

/** The task sits in the middle; position stands for text similarity to it: the closer a dot, the more alike it reads. */
export const TASK: Spot = { x: 0.5, y: 0.5 };

export const NEED: Fact[] = [
  {
    k: "F",
    x: 0.535,
    y: 0.48,
    name: "Feature 2",
    tag: "Feature 2 · Doc 2",
    src: "Document 2, section 3",
    side: 1,
    hit: "Found: similar wording",
    how: "The start: the task's brief",
    tool: "get_story_brief",
  },
  {
    k: "R1",
    x: 0.64,
    y: 0.64,
    name: "Its business rule",
    tag: "Rule · Doc 1",
    src: "Document 1, section 3",
    side: 1,
    hit: "Found: similar wording",
    miss: "Missed: pushed out by lookalikes",
    how: "1 hop: a rule of Feature 2",
    tool: "get_story_brief",
  },
  {
    k: "R7",
    x: 0.88,
    y: 0.2,
    name: "The same rule, other limit",
    tag: "Same rule · Doc 7",
    src: "Document 7, section 4",
    side: -1,
    narrow: { side: -1, dy: 20 },
    miss: "Missed: worded differently, other document",
    how: "Reads like the rule but has no link. Contradiction flagged",
    tool: "similar",
  },
  {
    k: "I",
    x: 0.12,
    y: 0.22,
    name: "The integration it uses",
    tag: "Integration · Doc 4",
    src: "Document 4, section 2",
    side: 1,
    dy: -16,
    miss: "Missed: does not mention the task",
    how: "2 hops: Feature 2 calls an interface that the integration provides",
    tool: "trace",
  },
  {
    k: "Q",
    x: 0.8,
    y: 0.85,
    name: "An open question",
    tag: "Open question · Doc 3",
    src: "Document 3, section 5",
    side: 1,
    miss: "Missed: does not look like the task",
    how: "2 hops: a question on its rule, still open, asked to its owner",
    tool: "list_gaps",
  },
];

/** Items the walk passes through on the way to a needed fact. */
export const HOPS: Record<string, Spot & { tag: string }> = { IF: { x: 0.36, y: 0.3, tag: "interface" } };

/** [from, to, relation, bend, reads-alike link with no edge] */
export type Link = [string, string, string, number, boolean?];
export const LINKS: Link[] = [
  ["T", "F", "builds", 0],
  ["F", "R1", "has rule", -0.12],
  ["R1", "Q", "open question", 0.12],
  ["F", "IF", "calls", 0.12],
  ["IF", "I", "provided by", 0.1],
  ["R1", "R7", "reads alike, no link", 0.22, true],
];

export const SIZES = ["20 documents", "200 documents", "2,000 documents"];

export const at = (k: string): Spot => (k === "T" ? TASK : (HOPS[k] ?? (NEED.find((n) => n.k === k) as Fact)));

export interface Passage extends Spot {
  /** Smallest project size (0..2) this passage belongs to. */
  tier: number;
}

interface Tier {
  n: number;
  cl: number;
  min: number;
  near?: number;
  r0?: number;
  r1?: number;
}

/** Bigger project, more lookalikes next to the task: small projects keep a clear zone around it (min); the 2,000-document tier adds a ring of near passages. */
const TIERS: Tier[] = [
  { n: 70, cl: 5, min: 0.09 },
  { n: 190, cl: 28, min: 0.09 },
  { n: 640, cl: 110, min: 0.045, near: 40, r0: 0.045, r1: 0.09 },
];
const WHOLE_PROJECT_RADIUS = 0.46;

function keepClear(x: number, y: number, tier: number, ring: boolean, min: number): boolean {
  if (x < 0.02 || x > 0.98 || y < 0.03 || y > 0.97) return false;
  /* A small project stays inside the ring that a whole-project context covers. */
  if (tier === 0 && Math.hypot(x - TASK.x, y - TASK.y) > WHOLE_PROJECT_RADIUS) return false;
  /* Keep the task square and its label clear. */
  if (Math.abs(x - TASK.x) < 0.03 && y - TASK.y > -0.03 && y - TASK.y < 0.07) return false;
  if (!ring && Math.hypot(x - TASK.x, y - TASK.y) < min) return false;
  if (NEED.some((n) => Math.hypot(n.x - x, n.y - y) < 0.02)) return false;
  /* and the Feature 2 label beside the ring */
  return !(x > 0.54 && x < 0.67 && Math.abs(y - 0.48) < 0.022);
}

/** The seeded field of passages: every viewer sees the same one. */
export function buildPassages(): Passage[] {
  const random = seeded(7);
  const gauss = () => {
    let u = 0;
    let v = 0;
    while (!u) u = random();
    while (!v) v = random();
    return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
  };
  const out: Passage[] = [];
  TIERS.forEach((spec, tier) => {
    let made = 0;
    let clustered = 0;
    let nearMade = 0;
    while (made < spec.n) {
      const ring = nearMade < (spec.near ?? 0);
      let x: number;
      let y: number;
      if (ring) {
        const a = random() * Math.PI * 2;
        const d = (spec.r0 ?? 0) + random() * ((spec.r1 ?? 0) - (spec.r0 ?? 0));
        x = TASK.x + Math.cos(a) * d;
        y = TASK.y + Math.sin(a) * d;
      } else if (clustered < spec.cl) {
        x = TASK.x + gauss() * 0.14;
        y = TASK.y + gauss() * 0.14;
      } else {
        x = 0.03 + random() * 0.94;
        y = 0.04 + random() * 0.92;
      }
      if (!keepClear(x, y, tier, ring, spec.min)) continue;
      out.push({ x, y, tier });
      made++;
      if (ring) nearMade++;
      else if (clustered < spec.cl) clustered++;
    }
  });
  return out;
}
