// @vitest-environment jsdom
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AnalystScorecard } from "./AnalystScorecard";
import { api, type AnalystScorecardResponse } from "../../api/client";

// No global setup file configures auto-cleanup (see AgentPromptViewer.test.tsx),
// and the endpoint spy is shared across this file, so its call count has to be
// reset too or the last test reads every earlier render's fetches.
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

// The three chart libraries are stubbed, not exercised. lightweight-charts
// needs a real canvas, and @xyflow/react needs layout measurement, neither of
// which jsdom provides — mocking them keeps this file testing what it can
// actually assert (the copy, the structure, and the live/example switch)
// instead of testing the libraries' own rendering.
vi.mock("lightweight-charts", () => ({
  createChart: () => ({
    addBaselineSeries: () => ({
      setData: vi.fn(),
      setMarkers: vi.fn(),
      createPriceLine: vi.fn(),
    }),
    timeScale: () => ({ fitContent: vi.fn() }),
    applyOptions: vi.fn(),
    remove: vi.fn(),
  }),
  LineStyle: { Dotted: 1, Dashed: 2 },
}));

vi.mock("@xyflow/react", () => ({
  ReactFlow: ({ nodes }: { nodes: Array<{ id: string }> }) => (
    <div data-testid="react-flow">{nodes.map((n) => n.id).join(",")}</div>
  ),
  Background: () => null,
  BackgroundVariant: { Dots: "dots" },
  Handle: () => null,
  Position: { Left: "left", Right: "right" },
}));

vi.mock("../../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/client")>();
  return { ...actual, api: { ...actual.api, analystScorecard: vi.fn() } };
});

const scorecard = api.analystScorecard as unknown as ReturnType<typeof vi.fn>;

function emptyResponse(overrides: Partial<AnalystScorecardResponse> = {}): AnalystScorecardResponse {
  return {
    as_of: "2026-08-31T09:00:00+00:00",
    state: "empty",
    read_error: null,
    risk_dollars_per_call: 100,
    resolved_calls_total: 0,
    months: [],
    analysts: [],
    ideas: [],
    ...overrides,
  };
}

const ONE_LIVE_ANALYST: AnalystScorecardResponse = emptyResponse({
  state: "populated",
  resolved_calls_total: 1,
  months: ["2026-08"],
  analysts: [
    {
      analyst: "macro",
      resolved_calls: 1,
      calls_right: 0,
      hit_rate_pct: 0,
      avg_win: null,
      avg_loss: -0.9,
      cumulative_credit: -0.9,
      peak: 0,
      below_best: 0.9,
      below_best_since: "2026-08-04 20:00:00",
      calls_since_peak: 1,
      cumulative: [
        { resolved_at: "2026-08-04 20:00:00", cumulative: -0.9, peak: 0, below_best: 0.9 },
      ],
      monthly: [
        {
          month: "2026-08", credit: -0.9, cumulative: -0.9,
          resolved_calls: 1, calls_right: 0, hit_rate_pct: 0,
        },
      ],
      by_confidence: [
        {
          conviction: "high", resolved_calls: 1, calls_right: 0, hit_rate_pct: 0,
          avg_win: null, avg_loss: -0.9, cumulative_credit: -0.9,
        },
      ],
    },
  ],
  ideas: [
    {
      symbol: "XOM",
      direction: "long",
      position_id: "position-xom",
      decision_id: "decision-xom",
      resolved_at: "2026-08-04 20:00:00",
      r_multiple: -0.9,
      supported: [
        {
          analyst: "macro", side: "supported", stance: "buy", conviction: "high",
          credit: -0.9, nominated: true, reason: "Crude inventories were drawing.",
        },
      ],
      opposed: [],
    },
  ],
});

describe("analyst scorecard — the example / live switch", () => {
  it("renders the committed example and says so when nothing has settled yet", async () => {
    scorecard.mockResolvedValue(emptyResponse());
    render(<AnalystScorecard />);

    const banner = await screen.findByTestId("example-data-banner");
    expect(banner.getAttribute("data-source")).toBe("example");
    expect(banner.textContent).toContain("Example data — not real");
    expect(banner.textContent).toContain("No trade has been closed and scored yet");
    expect(banner.textContent).toContain("Every name, number and date below is invented");
    expect(screen.queryByTestId("live-data-banner")).toBeNull();

    // ...and the page is fully populated from the fixture, not blank.
    expect(screen.getByTestId("slope-accuracy")).toBeTruthy();
    expect(screen.getByTestId("slope-money")).toBeTruthy();
    expect(screen.getByTestId("analyst-detail")).toBeTruthy();
    expect(screen.getByTestId("idea-trace")).toBeTruthy();
    expect(screen.getAllByText("news").length).toBeGreaterThan(0);
  });

  it("shows the example, with a different reason, when the read failed", async () => {
    scorecard.mockResolvedValue(emptyResponse({ state: "error", read_error: "unavailable" }));
    render(<AnalystScorecard />);
    const banner = await screen.findByTestId("example-data-banner");
    expect(banner.textContent).toContain("The record could not be read");
  });

  it("shows the example when the endpoint itself is unreachable", async () => {
    scorecard.mockRejectedValue(new Error("HTTP 503"));
    render(<AnalystScorecard />);
    const banner = await screen.findByTestId("example-data-banner");
    expect(banner.textContent).toContain("could not be loaded");
  });

  it("switches to the real record cleanly, with no example row left behind", async () => {
    scorecard.mockResolvedValue(ONE_LIVE_ANALYST);
    render(<AnalystScorecard />);

    const banner = await screen.findByTestId("live-data-banner");
    expect(banner.getAttribute("data-source")).toBe("live");
    expect(banner.textContent).toContain("1 settled call");
    expect(screen.queryByTestId("example-data-banner")).toBeNull();
    // A fixture-only analyst must not survive into a live render.
    expect(screen.queryByText("smart_money")).toBeNull();
    expect(screen.getAllByText("macro").length).toBeGreaterThan(0);
  });
});

describe("analyst scorecard — a live record with almost nothing in it", () => {
  it("renders one analyst, one call, no wins, without breaking", async () => {
    scorecard.mockResolvedValue(ONE_LIVE_ANALYST);
    render(<AnalystScorecard />);

    await screen.findByTestId("live-data-banner");
    // No minimum-sample gate: a single settled call is shown, not hidden.
    expect(screen.getByText("0 of 1")).toBeTruthy();
    // An analyst that has never won says so rather than printing "$NaN".
    expect(screen.getAllByText("no wins yet").length).toBeGreaterThan(0);
    expect(document.body.textContent).not.toContain("NaN");
    expect(document.body.textContent).not.toContain("Infinity");
  });

  it("draws no two-date comparison when the desk has only one month", async () => {
    scorecard.mockResolvedValue(ONE_LIVE_ANALYST);
    render(<AnalystScorecard />);
    await screen.findByTestId("live-data-banner");
    // One month means both ends are the same date — honest, and still drawn,
    // rather than an invented earlier value.
    const panel = screen.getByTestId("slope-money");
    expect(within(panel).getAllByText("August 2026").length).toBe(2);
  });
});

describe("analyst scorecard — plain language", () => {
  it("defines every term on the page before using it", async () => {
    scorecard.mockResolvedValue(emptyResponse());
    render(<AnalystScorecard />);
    const guide = await screen.findByTestId("how-to-read");
    const text = guide.textContent ?? "";

    expect(text).toContain("An analyst");
    expect(text).toContain("A settled call");
    expect(text).toContain("$100");
    expect(text).toContain("paid");
    // The three statements the owner required verbatim in substance.
    expect(text).toContain("profits are never reinvested");
    expect(text).toContain("no recent-months window is applied");
    expect(text).toContain("No score on this page changes how much money any trade gets");
    expect(text).toContain("There is no minimum number of calls");
  });

  it("says plainly that confidence changes nothing about the amount", async () => {
    scorecard.mockResolvedValue(emptyResponse());
    render(<AnalystScorecard />);
    const guide = await screen.findByTestId("how-to-read");
    const text = guide.textContent ?? "";

    expect(text).toContain("How confidently the analyst spoke");
    expect(text).toContain("does not change what the call is worth");
    expect(text).toContain("credited or charged exactly the same amount");
    // ...and the record is split by it instead, further down the page.
    const split = screen.getAllByTestId("by-confidence");
    expect(split.length).toBeGreaterThan(0);
    expect(document.body.textContent).toContain("When it said it was sure, and when it hedged");
  });

  it("describes a bet on a fall in the same words as any other trade", async () => {
    scorecard.mockResolvedValue(emptyResponse());
    render(<AnalystScorecard />);
    const guide = await screen.findByTestId("how-to-read");
    const text = guide.textContent ?? "";

    expect(text).toContain("Betting on a share going down");
    expect(text).toContain("It counts exactly the same way");
    expect(text).toContain("Nothing on this page is reversed");
    // The traced idea uses one sentence for both directions — only the verb
    // differs. Neither wording says a bet on a fall is scored backwards.
    const trace = screen.getByTestId("idea-trace");
    expect(trace.textContent).toMatch(/the desk bet it would (rise|fall)/);
    expect(trace.textContent).not.toMatch(/invert|reversed|negative of/i);
  });

  it("uses none of the jargon words anywhere in what renders", async () => {
    scorecard.mockResolvedValue(emptyResponse());
    render(<AnalystScorecard />);
    await screen.findByTestId("how-to-read");
    const page = document.body.textContent ?? "";

    for (const banned of [
      "R-multiple",
      "seat",
      "payoff ratio",
      "expectancy",
      "drawdown",
      "conviction-weighted",
    ]) {
      expect(page.toLowerCase()).not.toContain(banned.toLowerCase());
    }
    // "R" as a bare unit: no figure on this page is ever suffixed with it.
    expect(page).not.toMatch(/[-+]?\d+(\.\d+)?\s?R\b/);
  });

  it("names the contrast the desk overview exists to show", async () => {
    scorecard.mockResolvedValue(emptyResponse());
    render(<AnalystScorecard />);
    const callout = await screen.findByTestId("accurate-but-losing");
    expect(callout.textContent).toContain("news");
    expect(callout.textContent).toContain("still losing money");
  });
});

describe("analyst scorecard — readable without colour", () => {
  it("puts an explicit sign and a ▲/▼ on every headline figure", async () => {
    scorecard.mockResolvedValue(emptyResponse());
    render(<AnalystScorecard />);
    await screen.findByTestId("example-data-banner");
    const page = document.body.textContent ?? "";

    expect(page).toContain("▲");
    expect(page).toContain("▼");
    // Money is always signed, never a bare magnitude that needs a hue read.
    expect(page).toMatch(/\+\$\d/);
    expect(page).toMatch(/−\$\d/);
  });

  it("labels the two bars in words as well as position", async () => {
    scorecard.mockResolvedValue(emptyResponse());
    render(<AnalystScorecard />);
    await screen.findByTestId("example-data-banner");
    // Every opposed-bar graphic carries its own text alternative.
    const described = screen.getAllByRole("img", { name: /typical loss .*, typical win/ });
    expect(described.length).toBeGreaterThan(0);
  });

  it("spells out each analyst's role and credit in the traced idea", async () => {
    scorecard.mockResolvedValue(emptyResponse());
    render(<AnalystScorecard />);
    const trace = await screen.findByTestId("idea-trace");
    expect(trace.textContent).toMatch(/proposed it|agreed with it|objected to it/);
    expect(trace.textContent).toContain("confidence");
  });
});

describe("analyst scorecard — it only ever reads", () => {
  it("calls the read-only scorecard endpoint and nothing else", async () => {
    scorecard.mockResolvedValue(emptyResponse());
    render(<AnalystScorecard />);
    await waitFor(() => expect(scorecard).toHaveBeenCalled());
    expect(scorecard).toHaveBeenCalledTimes(1);
  });
});
