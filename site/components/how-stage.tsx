import type { CSSProperties, JSX } from "react";
import { EDGES, FLAGGED, METHODS, NODES, SOURCES, node, type NodeId } from "../lib/how-graph";
import { AGENTS, BLOCKED_BY_QUESTION, type PlanState } from "../lib/plan-state";

export type StageStep = 1 | 2 | 3;

const FLAG_OFFSET = 13;

function Flag({ id }: Readonly<{ id: NodeId }>): JSX.Element {
  const { x, y } = node(id);
  return (
    <g className="gflag">
      <circle cx={x + FLAG_OFFSET} cy={y - FLAG_OFFSET} r={9} />
      <text x={x + FLAG_OFFSET} y={y - 9.5} textAnchor="middle">
        !
      </text>
    </g>
  );
}

/** Step 1: sources feed section strips, strips feed the graph. */
function Sources(): JSX.Element {
  return (
    <g className="layer1">
      {SOURCES.map(({ label, feeds }, i) => {
        const y = 40 + i * 80;
        const target = node(feeds);
        return (
          <g key={`${label}-${feeds}`}>
            <g className="src-box">
              <rect x={14} y={y} width={112} height={40} rx={8} />
              <text x={70} y={y + 24} textAnchor="middle">
                {label}
              </text>
            </g>
            <path className="feed" d={`M126 ${y + 20} C 150 ${y + 20}, 150 ${y + 20}, 160 ${y + 20}`} />
            {[0, 1, 2].map((j) => (
              <rect key={j} className="strip" x={162} y={y + 6 + j * 10} width={30} height={7} rx={2} />
            ))}
            <path className="feed" d={`M192 ${y + 20} C 210 ${y + 20}, ${target.x - 40} ${target.y}, ${target.x - 15} ${target.y}`} />
          </g>
        );
      })}
    </g>
  );
}

/** Step 2: Gap's methods scan the graph, problems get a flag. */
function GapLayer({ lit }: Readonly<{ lit: number }>): JSX.Element {
  return (
    <g className="layer2">
      <ellipse className="scan" cx={400} cy={170} rx={210} ry={150} />
      {FLAGGED.map((id) => (
        <Flag key={id} id={id} />
      ))}
      {METHODS.map((method, i) => {
        const x = 160 + i * 120;
        return (
          <g key={method} className={i === lit ? "m-chip lit" : "m-chip"}>
            <rect x={x} y={352} width={112} height={34} rx={17} />
            <text x={x + 56} y={373} textAnchor="middle">
              {method}
            </text>
          </g>
        );
      })}
    </g>
  );
}

/** Step 3: agents claim ready work. */
function AgentLayer({ plan }: Readonly<{ plan: PlanState }>): JSX.Element {
  return (
    <g className="layer3">
      {AGENTS.map((agent) => {
        const id = NODES.find((n) => plan.holder[n.id] === agent)?.id;
        if (!id) return null;
        const { x, y } = node(id);
        const w = agent.length * 6.4 + 12;
        return (
          <g key={agent} className="agent-tag">
            <rect x={x - w / 2} y={y - 40} width={w} height={17} rx={4} />
            <text x={x} y={y - 28} textAnchor="middle">
              {agent}
            </text>
          </g>
        );
      })}
      {plan.status[BLOCKED_BY_QUESTION] === "block" ? <Flag id={BLOCKED_BY_QUESTION} /> : null}
    </g>
  );
}

export interface HowStageProps {
  step: StageStep;
  /** Changes whenever step 1 is entered, so the items pop in again. */
  replay: number;
  plan: PlanState;
  lit: number;
  titleId: string;
}

/** The figure of "how it works": one graph, three jobs. */
export function HowStage({ step, replay, plan, lit, titleId }: Readonly<HowStageProps>): JSX.Element {
  return (
    <svg className={`stage-svg s${step}`} viewBox="0 0 640 410" aria-labelledby={titleId} role="img">
      <defs>
        <pattern id="hatch" width={6} height={6} patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <rect width={6} height={6} fill="var(--gx-paper)" />
          <line className="hatch-a" x1={0} y1={0} x2={0} y2={6} />
        </pattern>
      </defs>
      <Sources />
      <g>
        {EDGES.map(([a, b]) => (
          <line key={`${a}-${b}`} className="glink" x1={node(a).x} y1={node(a).y} x2={node(b).x} y2={node(b).y} />
        ))}
      </g>
      <g>
        {NODES.map(({ id, x, y, label }, i) => (
          <g
            key={`${id}-${replay}`}
            className={`gnode pop ${plan.status[id]}`}
            style={{ "--d": `${(0.15 + i * 0.08).toFixed(2)}s` } as CSSProperties}
          >
            <circle cx={x} cy={y} r={14} />
            <text x={x} y={y + 30} textAnchor="middle">
              {label}
            </text>
          </g>
        ))}
      </g>
      <GapLayer lit={lit} />
      <AgentLayer plan={plan} />
    </svg>
  );
}
