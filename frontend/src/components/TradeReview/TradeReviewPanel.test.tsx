// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TradeReviewPanel } from "./TradeReviewPanel";
import { fmtProfitFactor } from "./tradeReviewModel";
import { api, type TradeReviewResponse, type TradeReviewWindow } from "../../api/client";

afterEach(cleanup);

vi.mock("../../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/client")>();
  return { ...actual, api: { ...actual.api, tradeReview: vi.fn() } };
});
const tradeReview = api.tradeReview as unknown as ReturnType<typeof vi.fn>;

const CAVEAT = "small samples and lopsided results make this unreliable";

const emptyWindow: TradeReviewWindow = {
  n: 0, wins: 0, losses: 0, win_rate_pct: null, total_pnl_usd: 0, profit_factor: "n/a", expectancy_r: null,
  r_unknown: 0, avg_hold_seconds: null, largest_winner_r: null, largest_winner_usd: null, average_winner_r: null,
  average_winner_usd: null, largest_loser_r: null, largest_loser_usd: null, average_loser_r: null, average_loser_usd: null,
};

function payload(over: Partial<TradeReviewResponse> = {}): TradeReviewResponse {
  return {
    state: "ok", read_error: null, caveat: CAVEAT, excluded_pre_clean_record_n: 2, n_closed: 0,
    today: { pnl_usd: 0, n: 0, wins: 0, losses: 0, win_rate_pct: null },
    last_trades: [], r_histogram: [{ label: "0R to 1R", count: 0 }],
    windows: { today: emptyWindow, week: emptyWindow, all: emptyWindow },
    slices: { by_side: [], by_entry_time_et: [], by_hold: [] },
    drawdown_r: 0, longest_losing_streak: 0, cumulative_r: [],
    quality: { n: 0, mean_r: null, stdev_r: null, score: null, p_value: null, caveat: CAVEAT },
    ...over,
  };
}

describe("TradeReviewPanel", () => {
  it("renders empty states without crashing", async () => {
    tradeReview.mockResolvedValue(payload());
    render(<TradeReviewPanel />);
    await waitFor(() => expect(screen.getByTestId("trade-review")).toBeTruthy());
    expect(screen.getByText(/No closed trades on the clean record yet/)).toBeTruthy();
    expect(screen.getAllByText("n/a").length).toBeGreaterThan(0);
    expect(screen.getByTestId("quality").textContent).toContain(CAVEAT);
  });

  it("shows R unknown for a trade without R and the N on quality", async () => {
    tradeReview.mockResolvedValue(
      payload({
        n_closed: 1,
        last_trades: [{ position_id: "p1", symbol: "AAA", side: "long", pnl: 10, r: null, opened_at: "a", closed_at: "b", hold_seconds: 3600 }],
        quality: { n: 3, mean_r: 1, stdev_r: 1, score: 1, p_value: 0.1, caveat: CAVEAT },
      }),
    );
    render(<TradeReviewPanel />);
    await waitFor(() => expect(screen.getAllByText("R unknown")).toBeTruthy());
    expect(screen.getByTestId("quality").textContent).toContain("N = 3");
  });

  it("reports a read error", async () => {
    tradeReview.mockResolvedValue({ state: "error", read_error: "trade history unavailable" });
    render(<TradeReviewPanel />);
    await waitFor(() => expect(screen.getByText(/trade history unavailable/)).toBeTruthy());
  });
});

describe("fmtProfitFactor", () => {
  it("never prints infinity", () => {
    expect(fmtProfitFactor("no losses")).toBe("no losses");
    expect(fmtProfitFactor(2)).toBe("2.00");
  });
});
