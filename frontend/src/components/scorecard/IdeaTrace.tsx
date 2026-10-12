/* Section (d) — one idea traced end to end.
 *
 * Who proposed it, who agreed, who objected, what the desk did, what happened,
 * and what each analyst was credited or charged as a result. Drawn with
 * `@xyflow/react` through the cockpit's existing `AgentFlowGraph` mount (the
 * same fixed, non-interactive posture the Decision Room graph uses), with its
 * own node vocabulary rather than the decision-chain one.
 *
 * READING WITHOUT COLOUR. Every node states its role in words — PROPOSED,
 * AGREED, OBJECTED, THE TRADE, WHAT HAPPENED — and every credit is a signed
 * dollar figure with a ▲/▼ in front of it. Nodes for analysts who were paid
 * are solid-bordered; nodes for analysts who were charged are dashed. Colour
 * repeats those two facts and states nothing by itself.
 */

import { useMemo } from "react";
import { Handle, Position, type Edge, type Node } from "@xyflow/react";
import type { ScorecardIdea, ScorecardIdeaAnalyst } from "../../api/client";
import { AgentFlowGraph } from "../agentflow/AgentFlowGraph";
import { dayLabel, signedMoney, trendGlyph } from "./scorecardModel";

const INVISIBLE_HANDLE = "opacity-0 pointer-events-none w-1 h-1";

interface AnalystNodeData extends Record<string, unknown> {
  analyst: string;
  role: "PROPOSED" | "AGREED" | "OBJECTED";
  confidence: string;
  reason: string;
  credit: number;
}

function AnalystNode({ data }: { data: AnalystNodeData }) {
  const paid = data.credit >= 0;
  return (
    <div
      className={`w-[214px] rounded-lg border bg-panel px-2.5 py-2 ${
        paid ? "border-solid border-pos/60" : "border-dashed border-neg/70"
      }`}
    >
      <Handle type="target" position={Position.Left} className={INVISIBLE_HANDLE} />
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-[length:var(--fs-body)] font-semibold text-ink">{data.analyst}</span>
        <span className="text-[length:var(--fs-micro)] font-semibold uppercase tracking-wide text-agent">
          {data.role}
        </span>
      </div>
      <div className="mt-0.5 text-[length:var(--fs-micro)] text-dim">
        spoke with {data.confidence} confidence
      </div>
      {data.reason && (
        <p className="m-0 mt-1 line-clamp-3 text-[length:var(--fs-micro)] leading-snug text-dim">
          &ldquo;{data.reason}&rdquo;
        </p>
      )}
      <div className="mt-1.5 border-t border-border pt-1 font-mono text-[length:var(--fs-meta)]">
        <span aria-hidden="true">{trendGlyph(data.credit)}</span>{" "}
        <span className={paid ? "text-pos" : "text-neg"}>{signedMoney(data.credit)}</span>{" "}
        <span className="text-faint">{paid ? "credited" : "charged"}</span>
      </div>
      <Handle type="source" position={Position.Right} className={INVISIBLE_HANDLE} />
    </div>
  );
}

interface FactNodeData extends Record<string, unknown> {
  heading: string;
  headline: string;
  detail: string;
  outline?: boolean;
}

function FactNode({ data }: { data: FactNodeData }) {
  return (
    <div
      className={`w-[196px] rounded-lg border bg-panel-alt px-2.5 py-2 ${
        data.outline ? "border-dashed border-border-strong" : "border-solid border-accent/60"
      }`}
    >
      <Handle type="target" position={Position.Left} className={INVISIBLE_HANDLE} />
      <div className="text-[length:var(--fs-micro)] font-semibold uppercase tracking-wide text-dim">
        {data.heading}
      </div>
      <div className="mt-0.5 font-mono text-[length:var(--fs-subhead)] font-semibold text-ink">
        {data.headline}
      </div>
      <div className="mt-0.5 text-[length:var(--fs-micro)] leading-snug text-dim">{data.detail}</div>
      <Handle type="source" position={Position.Right} className={INVISIBLE_HANDLE} />
    </div>
  );
}

const IDEA_NODE_TYPES = { analyst: AnalystNode, fact: FactNode };

function analystNode(
  participant: ScorecardIdeaAnalyst,
  dollarsPerCall: number,
  x: number,
  y: number,
): Node {
  return {
    id: `${participant.side}-${participant.analyst}`,
    type: "analyst",
    position: { x, y },
    data: {
      analyst: participant.analyst,
      role: participant.nominated ? "PROPOSED" : participant.side === "supported" ? "AGREED" : "OBJECTED",
      confidence: participant.conviction || "unstated",
      reason: participant.reason,
      credit: participant.credit * dollarsPerCall,
    } satisfies AnalystNodeData,
  };
}

export function IdeaTrace({
  idea,
  dollarsPerCall,
}: {
  idea: ScorecardIdea;
  dollarsPerCall: number;
}) {
  const { nodes, edges } = useMemo(() => {
    const everyone = [...idea.supported, ...idea.opposed];
    const nodes: Node[] = everyone.map((participant, index) =>
      analystNode(participant, dollarsPerCall, 0, index * 116),
    );
    const middle = ((everyone.length - 1) * 116) / 2;
    // React Flow's fitView scales to whichever axis binds first. This stack of
    // analyst cards is far taller than the three columns are wide, so fitView
    // was fitting the HEIGHT and letterboxing the graph into the middle ~40%
    // of the canvas with dead space either side. Spreading the columns in
    // proportion to the stack's height keeps the drawn shape near the
    // canvas's own ratio, so it fills the width at any number of analysts.
    const stackHeight = (everyone.length - 1) * 116 + 96;
    const span = Math.max(760, stackHeight * 3.4);
    const tradeX = Math.round(span * 0.42);
    const resultX = Math.round(span * 0.74);
    const outcome = idea.r_multiple * dollarsPerCall;

    nodes.push({
      id: "trade",
      type: "fact",
      position: { x: tradeX, y: middle - 24 },
      data: {
        heading: "The trade",
        headline: idea.symbol,
        // Both directions get the same sentence in the same words — only the
        // verb differs, because only the bet differs. Owner decision,
        // 2026-08-31: nothing about a bet on a fall is worded specially.
        detail: `The desk bet it would ${
          idea.direction === "short" ? "fall" : "rise"
        }, closed ${dayLabel(idea.resolved_at)}.`,
      } satisfies FactNodeData,
    });
    nodes.push({
      id: "result",
      type: "fact",
      position: { x: resultX, y: middle - 24 },
      data: {
        heading: "What happened",
        headline: `${trendGlyph(outcome)} ${signedMoney(outcome)}`,
        detail:
          outcome >= 0
            ? "on the $100 treated as put at risk. Backers were credited that; objectors were charged it."
            : "on the $100 treated as put at risk. Backers were charged that; objectors were credited it.",
        outline: outcome < 0,
      } satisfies FactNodeData,
    });

    const edges: Edge[] = everyone.map((participant) => ({
      id: `edge-${participant.side}-${participant.analyst}`,
      source: `${participant.side}-${participant.analyst}`,
      target: "trade",
      label: participant.side === "supported" ? "for" : "against",
      labelStyle: { fill: "rgb(var(--c-ink-dim))", fontSize: 10 },
      labelBgStyle: { fill: "rgb(var(--c-surface))" },
      style: {
        stroke: participant.side === "supported" ? "rgb(var(--c-green))" : "rgb(var(--c-red))",
        strokeWidth: 1.4,
        strokeDasharray: participant.side === "supported" ? undefined : "5 3",
      },
    }));
    edges.push({
      id: "edge-trade-result",
      source: "trade",
      target: "result",
      style: { stroke: "rgb(var(--c-border-strong))", strokeWidth: 1.4 },
    });

    return { nodes, edges };
  }, [idea, dollarsPerCall]);

  const outcome = idea.r_multiple * dollarsPerCall;

  return (
    <section data-testid="idea-trace">
      <p className="m-0 mb-2 text-[length:var(--fs-meta)] leading-snug text-dim">
        One closed trade, traced back to everyone who took a side on it. {idea.symbol} was proposed by{" "}
        {[...idea.supported, ...idea.opposed].find((p) => p.nominated)?.analyst ?? "an analyst"}, and the
        desk bet it would {idea.direction === "short" ? "fall" : "rise"}. It ended{" "}
        <strong className="font-mono text-ink">{signedMoney(outcome)}</strong> against the $100 treated as
        put at risk, and every analyst below was credited or charged that same amount — the ones that
        backed it one way, the ones that argued against it the other. How confidently each spoke is shown
        beside its name and changes none of the figures.
      </p>
      <AgentFlowGraph nodes={nodes} edges={edges} nodeTypes={IDEA_NODE_TYPES} height={300} />
      <ul className="mt-2 list-none space-y-1 p-0 text-[length:var(--fs-meta)]">
        {[...idea.supported, ...idea.opposed].map((participant) => (
          <li key={`${participant.side}-${participant.analyst}`} className="text-dim">
            <span aria-hidden="true">{trendGlyph(participant.credit)}</span>{" "}
            <strong className="text-ink">{participant.analyst}</strong>{" "}
            {participant.nominated
              ? "proposed it"
              : participant.side === "supported"
              ? "agreed with it"
              : "objected to it"}
            , with {participant.conviction || "unstated"} confidence —{" "}
            <span className="font-mono text-ink">
              {signedMoney(participant.credit * dollarsPerCall)}
            </span>
            .
          </li>
        ))}
      </ul>
    </section>
  );
}
