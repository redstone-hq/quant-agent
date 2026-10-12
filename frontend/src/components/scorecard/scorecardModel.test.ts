import { describe, expect, it } from "vitest";
import type { AnalystScorecardItem, AnalystScorecardResponse } from "../../api/client";
import { ANALYST_SCORECARD_EXAMPLE } from "../../fixtures/analystScorecard";
import {
  belowBestSeries,
  buildDeskSlopes,
  callCounts,
  chooseView,
  dayLabel,
  defaultIdea,
  hitRateText,
  isLive,
  longestSpellBelowBest,
  monthLabel,
  profitSeries,
  signedMoney,
  stampToEpochSeconds,
  toDollars,
  trendGlyph,
  waterfall,
  zeroCrossings,
} from "./scorecardModel";

function analyst(overrides: Partial<AnalystScorecardItem> = {}): AnalystScorecardItem {
  return {
    analyst: "technical",
    resolved_calls: 0,
    calls_right: 0,
    hit_rate_pct: null,
    avg_win: null,
    avg_loss: null,
    cumulative_credit: 0,
    peak: 0,
    below_best: 0,
    below_best_since: null,
    calls_since_peak: 0,
    cumulative: [],
    monthly: [],
    by_confidence: [],
    ...overrides,
  };
}

function response(overrides: Partial<AnalystScorecardResponse> = {}): AnalystScorecardResponse {
  return {
    as_of: "2026-08-31T00:00:00+00:00",
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

describe("plain-language formatting", () => {
  it("always signs money and never emits an R unit", () => {
    expect(signedMoney(210)).toBe("+$210");
    expect(signedMoney(-60)).toBe("−$60");
    expect(signedMoney(0)).toBe("$0");
    expect(signedMoney(null)).toBe("—");
    expect(signedMoney(1234.6)).toBe("+$1,235");
  });

  it("pairs every direction with a glyph so colour is never the only cue", () => {
    expect(trendGlyph(1)).toBe("▲");
    expect(trendGlyph(-1)).toBe("▼");
    expect(trendGlyph(0)).toBe("·");
    expect(trendGlyph(null)).toBe("·");
  });

  it("converts the backend's R into the page's worked-example dollars", () => {
    expect(toDollars(2.4, 100)).toBe(240);
    expect(toDollars(null, 100)).toBeNull();
  });

  it("shows raw counts next to any percentage — spec §9.5 item 8", () => {
    const item = analyst({ resolved_calls: 3, calls_right: 3, hit_rate_pct: 100 });
    expect(callCounts(item)).toBe("3 of 3");
    expect(hitRateText(item)).toBe("100%");
    expect(hitRateText(analyst())).toBe("no settled calls yet");
  });

  it("names months and days without letting a timezone shift them", () => {
    expect(monthLabel("2026-08")).toBe("August 2026");
    expect(monthLabel("")).toBe("—");
    expect(dayLabel("2026-06-10 15:00:00")).toBe("10 June 2026");
    expect(dayLabel(null)).toBe("—");
  });
});

describe("the running-profit series", () => {
  it("parses the ledger's stored stamps as UTC and refuses to guess", () => {
    expect(stampToEpochSeconds("2026-06-10 15:00:00")).toBe(Date.UTC(2026, 5, 10, 15, 0, 0) / 1000);
    expect(stampToEpochSeconds("2026-06-10")).toBe(Date.UTC(2026, 5, 10) / 1000);
    expect(stampToEpochSeconds("not a date")).toBeNull();
  });

  it("forces strictly increasing times — two calls can settle in the same second", () => {
    const item = analyst({
      cumulative: [
        { resolved_at: "2026-06-10 15:00:00", cumulative: 1, peak: 1, below_best: 0 },
        { resolved_at: "2026-06-10 15:00:00", cumulative: 2, peak: 2, below_best: 0 },
        { resolved_at: "unparseable", cumulative: 1.5, peak: 2, below_best: 0.5 },
      ],
    });
    const series = profitSeries(item, 100);
    expect(series.map((p) => p.dollars)).toEqual([100, 200, 150]);
    expect(series[1].time).toBe(series[0].time + 1);
    expect(series[2].time).toBe(series[1].time + 1);
  });

  it("marks every crossing of zero, in words", () => {
    const series = [
      { time: 1, dollars: -50 },
      { time: 2, dollars: 20 },
      { time: 3, dollars: -10 },
    ];
    expect(zeroCrossings(series)).toEqual([
      { time: 2, direction: "into profit" },
      { time: 3, direction: "into loss" },
    ]);
  });
});

describe("how far below its own best, and for how long", () => {
  const item = analyst({
    cumulative: [
      { resolved_at: "2026-06-01 15:00:00", cumulative: 2, peak: 2, below_best: 0 },
      { resolved_at: "2026-06-02 15:00:00", cumulative: 1, peak: 2, below_best: 1 },
      { resolved_at: "2026-06-03 15:00:00", cumulative: 0.5, peak: 2, below_best: 1.5 },
      { resolved_at: "2026-06-04 15:00:00", cumulative: 3, peak: 3, below_best: 0 },
    ],
  });

  it("hangs downward from zero so depth is a position, not a hue", () => {
    expect(belowBestSeries(item, 100).map((p) => p.belowBest)).toEqual([0, -100, -150, 0]);
  });

  it("counts the longest unbroken spell below a previous best", () => {
    expect(longestSpellBelowBest(item)).toBe(2);
    expect(longestSpellBelowBest(analyst())).toBe(0);
  });
});

describe("the month-by-month waterfall", () => {
  it("floats each bar from last month's total to this month's", () => {
    const item = analyst({
      monthly: [
        { month: "2026-06", credit: 2, cumulative: 2, resolved_calls: 2, calls_right: 2, hit_rate_pct: 100 },
        { month: "2026-07", credit: -3, cumulative: -1, resolved_calls: 3, calls_right: 1, hit_rate_pct: 60 },
      ],
    });
    expect(waterfall(item, 100).map((s) => [s.label, s.change, s.span])).toEqual([
      ["June 2026", 200, [0, 200]],
      ["July 2026", -300, [200, -100]],
    ]);
  });
});

describe("the desk overview's two dates", () => {
  it("finds the analysts getting more accurate while losing money", () => {
    const rising = analyst({
      analyst: "news",
      monthly: [
        { month: "2026-05", credit: -1, cumulative: -1, resolved_calls: 2, calls_right: 1, hit_rate_pct: 50 },
        { month: "2026-08", credit: -3, cumulative: -4, resolved_calls: 8, calls_right: 5, hit_rate_pct: 60 },
      ],
    });
    const falling = analyst({
      analyst: "macro",
      monthly: [
        { month: "2026-05", credit: 1, cumulative: 1, resolved_calls: 1, calls_right: 1, hit_rate_pct: 100 },
        { month: "2026-08", credit: -2, cumulative: -1, resolved_calls: 4, calls_right: 2, hit_rate_pct: 50 },
      ],
    });
    const slopes = buildDeskSlopes([rising, falling], ["2026-05", "2026-08"], 100);
    expect(slopes?.moreAccurateButLosing).toEqual(["news"]);
    expect(slopes?.money.find((r) => r.analyst === "news")?.to).toBe(-400);
  });

  it("treats 'no record yet at the earlier date' as absent, never as zero", () => {
    const latecomer = analyst({
      analyst: "earnings",
      monthly: [
        { month: "2026-08", credit: 2, cumulative: 2, resolved_calls: 2, calls_right: 2, hit_rate_pct: 100 },
      ],
    });
    const slopes = buildDeskSlopes([latecomer], ["2026-05", "2026-08"], 100);
    const row = slopes?.accuracy[0];
    expect(row?.hasFrom).toBe(false);
    expect(row?.firstMonth).toBe("2026-08");
    // A late starter must never be counted into the headline contrast, which
    // compares two real dates it does not have both of.
    expect(slopes?.moreAccurateButLosing).toEqual([]);
  });

  it("returns nothing rather than a fabricated comparison with no data", () => {
    expect(buildDeskSlopes([], [], 100)).toBeNull();
    expect(buildDeskSlopes([analyst()], [], 100)).toBeNull();
  });
});

describe("the live / example switch", () => {
  it("is live only when the endpoint actually returned scored calls", () => {
    expect(isLive(null)).toBe(false);
    expect(isLive(response({ state: "empty" }))).toBe(false);
    expect(isLive(response({ state: "error", read_error: "unavailable" }))).toBe(false);
    expect(isLive(response({ state: "populated", resolved_calls_total: 0 }))).toBe(false);
    expect(
      isLive(response({ state: "populated", resolved_calls_total: 2, analysts: [analyst()] })),
    ).toBe(true);
  });

  it("falls back to the example and says why, in the reader's language", () => {
    expect(chooseView(response({ state: "empty" }), ANALYST_SCORECARD_EXAMPLE, null)).toMatchObject({
      source: "example",
      exampleReason: expect.stringContaining("No trade has been closed and scored yet"),
    });
    expect(
      chooseView(response({ state: "error", read_error: "x" }), ANALYST_SCORECARD_EXAMPLE, null),
    ).toMatchObject({ source: "example", exampleReason: expect.stringContaining("could not be read") });
    expect(chooseView(null, ANALYST_SCORECARD_EXAMPLE, "HTTP 500")).toMatchObject({
      source: "example",
      exampleReason: expect.stringContaining("could not be loaded"),
    });
  });

  it("never blends the example into a live response", () => {
    const liveData = response({
      state: "populated",
      resolved_calls_total: 1,
      analysts: [analyst({ analyst: "macro", resolved_calls: 1 })],
    });
    const view = chooseView(liveData, ANALYST_SCORECARD_EXAMPLE, null);
    expect(view.source).toBe("live");
    expect(view.exampleReason).toBeNull();
    expect(view.data.analysts).toHaveLength(1);
  });
});

describe("the committed example", () => {
  it("carries the contrast the desk overview exists to show", () => {
    const slopes = buildDeskSlopes(
      ANALYST_SCORECARD_EXAMPLE.analysts,
      ANALYST_SCORECARD_EXAMPLE.months,
      ANALYST_SCORECARD_EXAMPLE.risk_dollars_per_call,
    );
    // `news` gets more accurate while losing money — the single most
    // important thing the page shows, so the example must contain it.
    expect(slopes?.moreAccurateButLosing).toContain("news");
    const newsMoney = slopes?.money.find((r) => r.analyst === "news");
    expect(newsMoney?.change).toBeLessThan(0);
    // And the mirror case: `technical` gets less accurate while making money.
    const techAccuracy = slopes?.accuracy.find((r) => r.analyst === "technical");
    const techMoney = slopes?.money.find((r) => r.analyst === "technical");
    expect(techAccuracy?.change).toBeLessThan(0);
    expect(techMoney?.change).toBeGreaterThan(0);
  });

  it("is internally consistent: each analyst's monthly steps reach its total", () => {
    for (const item of ANALYST_SCORECARD_EXAMPLE.analysts) {
      const steps = waterfall(item, 100);
      expect(steps[steps.length - 1].total).toBeCloseTo(item.cumulative_credit * 100, 6);
      expect(item.resolved_calls).toBe(item.cumulative.length);
      expect(item.calls_right).toBe(item.cumulative.filter((_, i) => {
        const previous = i === 0 ? 0 : item.cumulative[i - 1].cumulative;
        return item.cumulative[i].cumulative > previous;
      }).length);
    }
  });

  it("splits every analyst by declared confidence, summing back to its total", () => {
    for (const item of ANALYST_SCORECARD_EXAMPLE.analysts) {
      expect(item.by_confidence.length).toBeGreaterThan(0);
      const calls = item.by_confidence.reduce((sum, b) => sum + b.resolved_calls, 0);
      const right = item.by_confidence.reduce((sum, b) => sum + b.calls_right, 0);
      const total = item.by_confidence.reduce((sum, b) => sum + b.cumulative_credit, 0);
      expect(calls).toBe(item.resolved_calls);
      expect(right).toBe(item.calls_right);
      expect(total).toBeCloseTo(item.cumulative_credit, 6);
    }
    // At least one analyst used more than one level, or the split shows nothing.
    expect(
      ANALYST_SCORECARD_EXAMPLE.analysts.some((a) => a.by_confidence.length > 1),
    ).toBe(true);
  });

  it("scores a bet on a fall exactly like any other trade", () => {
    const falling = ANALYST_SCORECARD_EXAMPLE.ideas.filter((i) => i.direction === "short");
    expect(falling.length).toBeGreaterThan(0);
    const winner = falling.find((i) => i.r_multiple > 0);
    expect(winner).toBeDefined();
    // Backers of a bet that made money are on the positive side of zero and
    // objectors on the negative one — the same convention a rising bet uses.
    for (const p of winner!.supported) expect(p.credit).toBeGreaterThan(0);
    for (const p of winner!.opposed) expect(p.credit).toBeLessThan(0);
    const loser = falling.find((i) => i.r_multiple < 0);
    if (loser) {
      for (const p of loser.supported) expect(p.credit).toBeLessThan(0);
      for (const p of loser.opposed) expect(p.credit).toBeGreaterThan(0);
    }
  });

  it("credits the same amount however confidently the analyst spoke", () => {
    // Every participant on one idea is credited the trade's own result, signed
    // by the side it took — never scaled by what it declared.
    for (const idea of ANALYST_SCORECARD_EXAMPLE.ideas) {
      for (const p of idea.supported) expect(p.credit).toBeCloseTo(idea.r_multiple, 6);
      for (const p of idea.opposed) expect(p.credit).toBeCloseTo(-idea.r_multiple, 6);
    }
  });

  it("opens on an idea that actually had someone on both sides", () => {
    const idea = defaultIdea(ANALYST_SCORECARD_EXAMPLE.ideas);
    expect(idea).not.toBeNull();
    expect(idea!.supported.length).toBeGreaterThan(0);
    expect(idea!.opposed.length).toBeGreaterThan(0);
    expect(defaultIdea([])).toBeNull();
  });
});
