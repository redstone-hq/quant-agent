/* The analyst scorecard's committed example data.
 *
 * WHY THIS EXISTS. The conviction ledger only produces a row when a position
 * closes and is scored, so a freshly deployed desk has an empty scorecard for
 * weeks. A page that renders as a blank grid in that window cannot be reviewed,
 * cannot be tested, and teaches a reader nothing about how to read it. This
 * file is the worked example the page falls back to, and the page states in a
 * banner — every time it is showing — that these rows are invented.
 *
 * HOW IT WAS BUILT. Not by hand. A list of ideas (who backed each one, who
 * objected, how confidently each spoke, what the trade did) was run through the
 * SAME aggregation `src/api/routes_scorecard.py` performs, so every total,
 * running series, peak, monthly step and confidence split below is
 * arithmetically consistent with the endpoint's own output rather than a
 * plausible-looking guess. The properties the generator asserts are reproduced
 * in the test that guards this file.
 *
 * WHAT IT DEMONSTRATES, deliberately:
 *   - `news` gets MORE accurate while LOSING money.
 *   - `technical` gets LESS accurate while MAKING money.
 *     Those two together are the contrast the desk overview exists to show.
 *   - `smart_money` and `earnings` have no record at the earlier date at all,
 *     which is a different thing from a record of zero and must render as such.
 *   - `smart_money` earns most of its money by OBJECTING to trades that lost.
 *   - Several analysts used more than one confidence level, so the
 *     per-confidence split has something in it to read. Credit is raw and
 *     unweighted (owner decision, 2026-08-31): two calls with the same outcome
 *     are worth the same whether they were stated confidently or hedged.
 *   - Two of the trades are bets on a share FALLING, scored exactly like the
 *     rest: the profitable one is a positive number for the analysts that
 *     backed it and a negative one for the analyst that argued against it.
 *
 * This is the fixture only. It is never merged with, defaulted into, or used
 * to fill gaps in a live response — see `chooseView` in scorecardModel.ts.
 */

import type { AnalystScorecardResponse } from "../api/client";

export const ANALYST_SCORECARD_EXAMPLE: AnalystScorecardResponse =
{
  "as_of": "2026-08-26T20:00:00+00:00",
  "state": "populated",
  "read_error": null,
  "risk_dollars_per_call": 100.0,
  "resolved_calls_total": 34,
  "months": [
    "2026-05",
    "2026-06",
    "2026-07",
    "2026-08"
  ],
  "analysts": [
    {
      "analyst": "technical",
      "resolved_calls": 10,
      "calls_right": 7,
      "hit_rate_pct": 70.0,
      "avg_win": 1.7143,
      "avg_loss": -0.8,
      "cumulative_credit": 9.6,
      "peak": 10.1,
      "below_best": 0.5,
      "below_best_since": "2026-08-04 20:00:00",
      "calls_since_peak": 2,
      "cumulative": [
        {
          "resolved_at": "2026-05-06 20:00:00",
          "cumulative": 1.0,
          "peak": 1.0,
          "below_best": 0.0
        },
        {
          "resolved_at": "2026-05-12 20:00:00",
          "cumulative": 2.2,
          "peak": 2.2,
          "below_best": 0.0
        },
        {
          "resolved_at": "2026-05-19 20:00:00",
          "cumulative": 4.2,
          "peak": 4.2,
          "below_best": 0.0
        },
        {
          "resolved_at": "2026-06-03 20:00:00",
          "cumulative": 6.7,
          "peak": 6.7,
          "below_best": 0.0
        },
        {
          "resolved_at": "2026-06-11 20:00:00",
          "cumulative": 5.9,
          "peak": 6.7,
          "below_best": 0.8
        },
        {
          "resolved_at": "2026-07-09 20:00:00",
          "cumulative": 8.4,
          "peak": 8.4,
          "below_best": 0.0
        },
        {
          "resolved_at": "2026-07-16 20:00:00",
          "cumulative": 7.6,
          "peak": 8.4,
          "below_best": 0.8
        },
        {
          "resolved_at": "2026-08-04 20:00:00",
          "cumulative": 10.1,
          "peak": 10.1,
          "below_best": 0.0
        },
        {
          "resolved_at": "2026-08-11 20:00:00",
          "cumulative": 9.3,
          "peak": 10.1,
          "below_best": 0.8
        },
        {
          "resolved_at": "2026-08-25 20:00:00",
          "cumulative": 9.6,
          "peak": 10.1,
          "below_best": 0.5
        }
      ],
      "monthly": [
        {
          "month": "2026-05",
          "credit": 4.2,
          "cumulative": 4.2,
          "resolved_calls": 3,
          "calls_right": 3,
          "hit_rate_pct": 100.0
        },
        {
          "month": "2026-06",
          "credit": 1.7,
          "cumulative": 5.9,
          "resolved_calls": 2,
          "calls_right": 1,
          "hit_rate_pct": 80.0
        },
        {
          "month": "2026-07",
          "credit": 1.7,
          "cumulative": 7.6,
          "resolved_calls": 2,
          "calls_right": 1,
          "hit_rate_pct": 71.43
        },
        {
          "month": "2026-08",
          "credit": 2.0,
          "cumulative": 9.6,
          "resolved_calls": 3,
          "calls_right": 2,
          "hit_rate_pct": 70.0
        }
      ],
      "by_confidence": [
        {
          "conviction": "high",
          "resolved_calls": 5,
          "calls_right": 5,
          "hit_rate_pct": 100.0,
          "avg_win": 1.94,
          "avg_loss": null,
          "cumulative_credit": 9.7
        },
        {
          "conviction": "medium",
          "resolved_calls": 4,
          "calls_right": 2,
          "hit_rate_pct": 50.0,
          "avg_win": 1.15,
          "avg_loss": -0.8,
          "cumulative_credit": 0.7
        },
        {
          "conviction": "low",
          "resolved_calls": 1,
          "calls_right": 0,
          "hit_rate_pct": 0.0,
          "avg_win": null,
          "avg_loss": -0.8,
          "cumulative_credit": -0.8
        }
      ]
    },
    {
      "analyst": "smart_money",
      "resolved_calls": 4,
      "calls_right": 4,
      "hit_rate_pct": 100.0,
      "avg_win": 0.775,
      "avg_loss": null,
      "cumulative_credit": 3.1,
      "peak": 3.1,
      "below_best": 0.0,
      "below_best_since": null,
      "calls_since_peak": 0,
      "cumulative": [
        {
          "resolved_at": "2026-07-02 20:00:00",
          "cumulative": 1.2,
          "peak": 1.2,
          "below_best": 0.0
        },
        {
          "resolved_at": "2026-07-16 20:00:00",
          "cumulative": 2.0,
          "peak": 2.0,
          "below_best": 0.0
        },
        {
          "resolved_at": "2026-08-11 20:00:00",
          "cumulative": 2.8,
          "peak": 2.8,
          "below_best": 0.0
        },
        {
          "resolved_at": "2026-08-25 20:00:00",
          "cumulative": 3.1,
          "peak": 3.1,
          "below_best": 0.0
        }
      ],
      "monthly": [
        {
          "month": "2026-07",
          "credit": 2.0,
          "cumulative": 2.0,
          "resolved_calls": 2,
          "calls_right": 2,
          "hit_rate_pct": 100.0
        },
        {
          "month": "2026-08",
          "credit": 1.1,
          "cumulative": 3.1,
          "resolved_calls": 2,
          "calls_right": 2,
          "hit_rate_pct": 100.0
        }
      ],
      "by_confidence": [
        {
          "conviction": "high",
          "resolved_calls": 3,
          "calls_right": 3,
          "hit_rate_pct": 100.0,
          "avg_win": 0.7667,
          "avg_loss": null,
          "cumulative_credit": 2.3
        },
        {
          "conviction": "medium",
          "resolved_calls": 1,
          "calls_right": 1,
          "hit_rate_pct": 100.0,
          "avg_win": 0.8,
          "avg_loss": null,
          "cumulative_credit": 0.8
        }
      ]
    },
    {
      "analyst": "earnings",
      "resolved_calls": 4,
      "calls_right": 2,
      "hit_rate_pct": 50.0,
      "avg_win": 1.65,
      "avg_loss": -0.55,
      "cumulative_credit": 2.2,
      "peak": 3.3,
      "below_best": 1.1,
      "below_best_since": "2026-06-11 20:00:00",
      "calls_since_peak": 2,
      "cumulative": [
        {
          "resolved_at": "2026-06-03 20:00:00",
          "cumulative": 2.5,
          "peak": 2.5,
          "below_best": 0.0
        },
        {
          "resolved_at": "2026-06-11 20:00:00",
          "cumulative": 3.3,
          "peak": 3.3,
          "below_best": 0.0
        },
        {
          "resolved_at": "2026-07-23 20:00:00",
          "cumulative": 3.0,
          "peak": 3.3,
          "below_best": 0.3
        },
        {
          "resolved_at": "2026-08-11 20:00:00",
          "cumulative": 2.2,
          "peak": 3.3,
          "below_best": 1.1
        }
      ],
      "monthly": [
        {
          "month": "2026-06",
          "credit": 3.3,
          "cumulative": 3.3,
          "resolved_calls": 2,
          "calls_right": 2,
          "hit_rate_pct": 100.0
        },
        {
          "month": "2026-07",
          "credit": -0.3,
          "cumulative": 3.0,
          "resolved_calls": 1,
          "calls_right": 0,
          "hit_rate_pct": 66.67
        },
        {
          "month": "2026-08",
          "credit": -0.8,
          "cumulative": 2.2,
          "resolved_calls": 1,
          "calls_right": 0,
          "hit_rate_pct": 50.0
        }
      ],
      "by_confidence": [
        {
          "conviction": "high",
          "resolved_calls": 1,
          "calls_right": 1,
          "hit_rate_pct": 100.0,
          "avg_win": 0.8,
          "avg_loss": null,
          "cumulative_credit": 0.8
        },
        {
          "conviction": "medium",
          "resolved_calls": 2,
          "calls_right": 1,
          "hit_rate_pct": 50.0,
          "avg_win": 2.5,
          "avg_loss": -0.3,
          "cumulative_credit": 2.2
        },
        {
          "conviction": "low",
          "resolved_calls": 1,
          "calls_right": 0,
          "hit_rate_pct": 0.0,
          "avg_win": null,
          "avg_loss": -0.8,
          "cumulative_credit": -0.8
        }
      ]
    },
    {
      "analyst": "macro",
      "resolved_calls": 7,
      "calls_right": 2,
      "hit_rate_pct": 28.57,
      "avg_win": 0.95,
      "avg_loss": -1.34,
      "cumulative_credit": -4.8,
      "peak": 0.9,
      "below_best": 5.7,
      "below_best_since": "2026-06-24 20:00:00",
      "calls_since_peak": 4,
      "cumulative": [
        {
          "resolved_at": "2026-05-06 20:00:00",
          "cumulative": -1.0,
          "peak": 0.0,
          "below_best": 1.0
        },
        {
          "resolved_at": "2026-05-21 20:00:00",
          "cumulative": 0.5,
          "peak": 0.5,
          "below_best": 0.0
        },
        {
          "resolved_at": "2026-06-24 20:00:00",
          "cumulative": 0.9,
          "peak": 0.9,
          "below_best": 0.0
        },
        {
          "resolved_at": "2026-07-09 20:00:00",
          "cumulative": -1.6,
          "peak": 0.9,
          "below_best": 2.5
        },
        {
          "resolved_at": "2026-08-04 20:00:00",
          "cumulative": -4.1,
          "peak": 0.9,
          "below_best": 5.0
        },
        {
          "resolved_at": "2026-08-18 20:00:00",
          "cumulative": -4.5,
          "peak": 0.9,
          "below_best": 5.4
        },
        {
          "resolved_at": "2026-08-25 20:00:00",
          "cumulative": -4.8,
          "peak": 0.9,
          "below_best": 5.7
        }
      ],
      "monthly": [
        {
          "month": "2026-05",
          "credit": 0.5,
          "cumulative": 0.5,
          "resolved_calls": 2,
          "calls_right": 1,
          "hit_rate_pct": 50.0
        },
        {
          "month": "2026-06",
          "credit": 0.4,
          "cumulative": 0.9,
          "resolved_calls": 1,
          "calls_right": 1,
          "hit_rate_pct": 66.67
        },
        {
          "month": "2026-07",
          "credit": -2.5,
          "cumulative": -1.6,
          "resolved_calls": 1,
          "calls_right": 0,
          "hit_rate_pct": 50.0
        },
        {
          "month": "2026-08",
          "credit": -3.2,
          "cumulative": -4.8,
          "resolved_calls": 3,
          "calls_right": 0,
          "hit_rate_pct": 28.57
        }
      ],
      "by_confidence": [
        {
          "conviction": "high",
          "resolved_calls": 3,
          "calls_right": 1,
          "hit_rate_pct": 33.33,
          "avg_win": 0.4,
          "avg_loss": -1.45,
          "cumulative_credit": -2.5
        },
        {
          "conviction": "medium",
          "resolved_calls": 1,
          "calls_right": 0,
          "hit_rate_pct": 0.0,
          "avg_win": null,
          "avg_loss": -1.0,
          "cumulative_credit": -1.0
        },
        {
          "conviction": "low",
          "resolved_calls": 3,
          "calls_right": 1,
          "hit_rate_pct": 33.33,
          "avg_win": 1.5,
          "avg_loss": -1.4,
          "cumulative_credit": -1.3
        }
      ]
    },
    {
      "analyst": "news",
      "resolved_calls": 9,
      "calls_right": 5,
      "hit_rate_pct": 55.56,
      "avg_win": 0.34,
      "avg_loss": -1.8,
      "cumulative_credit": -5.5,
      "peak": 0.0,
      "below_best": 5.5,
      "below_best_since": null,
      "calls_since_peak": 9,
      "cumulative": [
        {
          "resolved_at": "2026-05-19 20:00:00",
          "cumulative": -2.0,
          "peak": 0.0,
          "below_best": 2.0
        },
        {
          "resolved_at": "2026-05-21 20:00:00",
          "cumulative": -3.5,
          "peak": 0.0,
          "below_best": 3.5
        },
        {
          "resolved_at": "2026-05-26 20:00:00",
          "cumulative": -3.2,
          "peak": 0.0,
          "below_best": 3.2
        },
        {
          "resolved_at": "2026-06-03 20:00:00",
          "cumulative": -5.7,
          "peak": 0.0,
          "below_best": 5.7
        },
        {
          "resolved_at": "2026-06-24 20:00:00",
          "cumulative": -5.3,
          "peak": 0.0,
          "below_best": 5.3
        },
        {
          "resolved_at": "2026-07-02 20:00:00",
          "cumulative": -6.5,
          "peak": 0.0,
          "below_best": 6.5
        },
        {
          "resolved_at": "2026-07-23 20:00:00",
          "cumulative": -6.2,
          "peak": 0.0,
          "below_best": 6.2
        },
        {
          "resolved_at": "2026-08-18 20:00:00",
          "cumulative": -5.8,
          "peak": 0.0,
          "below_best": 5.8
        },
        {
          "resolved_at": "2026-08-25 20:00:00",
          "cumulative": -5.5,
          "peak": 0.0,
          "below_best": 5.5
        }
      ],
      "monthly": [
        {
          "month": "2026-05",
          "credit": -3.2,
          "cumulative": -3.2,
          "resolved_calls": 3,
          "calls_right": 1,
          "hit_rate_pct": 33.33
        },
        {
          "month": "2026-06",
          "credit": -2.1,
          "cumulative": -5.3,
          "resolved_calls": 2,
          "calls_right": 1,
          "hit_rate_pct": 40.0
        },
        {
          "month": "2026-07",
          "credit": -0.9,
          "cumulative": -6.2,
          "resolved_calls": 2,
          "calls_right": 1,
          "hit_rate_pct": 42.86
        },
        {
          "month": "2026-08",
          "credit": 0.7,
          "cumulative": -5.5,
          "resolved_calls": 2,
          "calls_right": 2,
          "hit_rate_pct": 55.56
        }
      ],
      "by_confidence": [
        {
          "conviction": "high",
          "resolved_calls": 3,
          "calls_right": 0,
          "hit_rate_pct": 0.0,
          "avg_win": null,
          "avg_loss": -1.5667,
          "cumulative_credit": -4.7
        },
        {
          "conviction": "medium",
          "resolved_calls": 3,
          "calls_right": 3,
          "hit_rate_pct": 100.0,
          "avg_win": 0.3667,
          "avg_loss": null,
          "cumulative_credit": 1.1
        },
        {
          "conviction": "low",
          "resolved_calls": 3,
          "calls_right": 2,
          "hit_rate_pct": 66.67,
          "avg_win": 0.3,
          "avg_loss": -2.5,
          "cumulative_credit": -1.9
        }
      ]
    }
  ],
  "ideas": [
    {
      "symbol": "NKE",
      "direction": "short",
      "position_id": "pos-16",
      "decision_id": "dec-16",
      "resolved_at": "2026-08-25 20:00:00",
      "r_multiple": 0.3,
      "supported": [
        {
          "analyst": "smart_money",
          "side": "supported",
          "stance": "bearish",
          "conviction": "high",
          "credit": 0.3,
          "nominated": true,
          "reason": "Officers sold every rally for two quarters."
        },
        {
          "analyst": "technical",
          "side": "supported",
          "stance": "sell",
          "conviction": "medium",
          "credit": 0.3,
          "nominated": false,
          "reason": "Lower highs all summer, and the last one failed fast."
        },
        {
          "analyst": "news",
          "side": "supported",
          "stance": "bearish",
          "conviction": "low",
          "credit": 0.3,
          "nominated": false,
          "reason": "Back-to-school commentary read soft."
        }
      ],
      "opposed": [
        {
          "analyst": "macro",
          "side": "opposed",
          "stance": "overweight",
          "conviction": "low",
          "credit": -0.3,
          "nominated": false,
          "reason": "Discretionary spend was recovering, not rolling over."
        }
      ]
    },
    {
      "symbol": "HD",
      "direction": "long",
      "position_id": "pos-15",
      "decision_id": "dec-15",
      "resolved_at": "2026-08-18 20:00:00",
      "r_multiple": 0.4,
      "supported": [
        {
          "analyst": "news",
          "side": "supported",
          "stance": "positive",
          "conviction": "medium",
          "credit": 0.4,
          "nominated": true,
          "reason": "Housing turnover picked up for the second month."
        }
      ],
      "opposed": [
        {
          "analyst": "macro",
          "side": "opposed",
          "stance": "underweight",
          "conviction": "high",
          "credit": -0.4,
          "nominated": false,
          "reason": "Mortgage rates had not moved enough to matter."
        }
      ]
    },
    {
      "symbol": "DIS",
      "direction": "long",
      "position_id": "pos-14",
      "decision_id": "dec-14",
      "resolved_at": "2026-08-11 20:00:00",
      "r_multiple": -0.8,
      "supported": [
        {
          "analyst": "technical",
          "side": "supported",
          "stance": "buy",
          "conviction": "medium",
          "credit": -0.8,
          "nominated": true,
          "reason": "Reclaimed its average after four weeks underneath it."
        },
        {
          "analyst": "earnings",
          "side": "supported",
          "stance": "positive",
          "conviction": "low",
          "credit": -0.8,
          "nominated": false,
          "reason": "Streaming losses narrowed again."
        }
      ],
      "opposed": [
        {
          "analyst": "smart_money",
          "side": "opposed",
          "stance": "bearish",
          "conviction": "high",
          "credit": 0.8,
          "nominated": false,
          "reason": "Two officers sold the week the desk bought."
        }
      ]
    },
    {
      "symbol": "WMT",
      "direction": "long",
      "position_id": "pos-13",
      "decision_id": "dec-13",
      "resolved_at": "2026-08-04 20:00:00",
      "r_multiple": 2.5,
      "supported": [
        {
          "analyst": "technical",
          "side": "supported",
          "stance": "buy",
          "conviction": "high",
          "credit": 2.5,
          "nominated": true,
          "reason": "A third higher low, and the break came on the widest bar of the month."
        }
      ],
      "opposed": [
        {
          "analyst": "macro",
          "side": "opposed",
          "stance": "underweight",
          "conviction": "high",
          "credit": -2.5,
          "nominated": false,
          "reason": "Household savings were still falling."
        }
      ]
    },
    {
      "symbol": "CVX",
      "direction": "long",
      "position_id": "pos-12",
      "decision_id": "dec-12",
      "resolved_at": "2026-07-23 20:00:00",
      "r_multiple": 0.3,
      "supported": [
        {
          "analyst": "news",
          "side": "supported",
          "stance": "positive",
          "conviction": "medium",
          "credit": 0.3,
          "nominated": false,
          "reason": "Refining margins held through the maintenance window."
        }
      ],
      "opposed": [
        {
          "analyst": "earnings",
          "side": "opposed",
          "stance": "bearish",
          "conviction": "medium",
          "credit": -0.3,
          "nominated": false,
          "reason": "The last two quarters were flattered by one-off items."
        }
      ]
    },
    {
      "symbol": "MRNA",
      "direction": "short",
      "position_id": "pos-11",
      "decision_id": "dec-11",
      "resolved_at": "2026-07-16 20:00:00",
      "r_multiple": -0.8,
      "supported": [
        {
          "analyst": "technical",
          "side": "supported",
          "stance": "sell",
          "conviction": "low",
          "credit": -0.8,
          "nominated": true,
          "reason": "Failing rallies, taken small because the base was still intact."
        }
      ],
      "opposed": [
        {
          "analyst": "smart_money",
          "side": "opposed",
          "stance": "positive",
          "conviction": "medium",
          "credit": 0.8,
          "nominated": false,
          "reason": "Two officers had been buying the whole way down."
        }
      ]
    },
    {
      "symbol": "KO",
      "direction": "long",
      "position_id": "pos-10",
      "decision_id": "dec-10",
      "resolved_at": "2026-07-09 20:00:00",
      "r_multiple": 2.5,
      "supported": [
        {
          "analyst": "technical",
          "side": "supported",
          "stance": "buy",
          "conviction": "high",
          "credit": 2.5,
          "nominated": false,
          "reason": "A quiet base, then a clean break with volume behind it."
        }
      ],
      "opposed": [
        {
          "analyst": "macro",
          "side": "opposed",
          "stance": "underweight",
          "conviction": "low",
          "credit": -2.5,
          "nominated": false,
          "reason": "Defensives should lag a recovering tape."
        }
      ]
    },
    {
      "symbol": "BA",
      "direction": "long",
      "position_id": "pos-09",
      "decision_id": "dec-09",
      "resolved_at": "2026-07-02 20:00:00",
      "r_multiple": -1.2,
      "supported": [
        {
          "analyst": "news",
          "side": "supported",
          "stance": "positive",
          "conviction": "high",
          "credit": -1.2,
          "nominated": true,
          "reason": "The order book headlines looked like a genuine turn."
        }
      ],
      "opposed": [
        {
          "analyst": "smart_money",
          "side": "opposed",
          "stance": "bearish",
          "conviction": "high",
          "credit": 1.2,
          "nominated": false,
          "reason": "Insiders were selling into every one of those headlines."
        }
      ]
    },
    {
      "symbol": "META",
      "direction": "long",
      "position_id": "pos-08",
      "decision_id": "dec-08",
      "resolved_at": "2026-06-24 20:00:00",
      "r_multiple": 0.4,
      "supported": [
        {
          "analyst": "news",
          "side": "supported",
          "stance": "positive",
          "conviction": "medium",
          "credit": 0.4,
          "nominated": true,
          "reason": "Advertising checks came back better than the quarter before."
        },
        {
          "analyst": "macro",
          "side": "supported",
          "stance": "overweight",
          "conviction": "high",
          "credit": 0.4,
          "nominated": false,
          "reason": "Advertising spend turns first when rates ease."
        }
      ],
      "opposed": []
    },
    {
      "symbol": "PFE",
      "direction": "long",
      "position_id": "pos-07",
      "decision_id": "dec-07",
      "resolved_at": "2026-06-11 20:00:00",
      "r_multiple": -0.8,
      "supported": [
        {
          "analyst": "technical",
          "side": "supported",
          "stance": "buy",
          "conviction": "medium",
          "credit": -0.8,
          "nominated": true,
          "reason": "Reclaimed the level it lost in April."
        }
      ],
      "opposed": [
        {
          "analyst": "earnings",
          "side": "opposed",
          "stance": "bearish",
          "conviction": "high",
          "credit": 0.8,
          "nominated": false,
          "reason": "Guidance was cut twice and never restored."
        }
      ]
    },
    {
      "symbol": "TSLA",
      "direction": "short",
      "position_id": "pos-06",
      "decision_id": "dec-06",
      "resolved_at": "2026-06-03 20:00:00",
      "r_multiple": 2.5,
      "supported": [
        {
          "analyst": "technical",
          "side": "supported",
          "stance": "sell",
          "conviction": "high",
          "credit": 2.5,
          "nominated": true,
          "reason": "Lower highs into a failing average, with delivery week ahead."
        },
        {
          "analyst": "earnings",
          "side": "supported",
          "stance": "bearish",
          "conviction": "medium",
          "credit": 2.5,
          "nominated": false,
          "reason": "Margins had compressed for three quarters straight."
        }
      ],
      "opposed": [
        {
          "analyst": "news",
          "side": "opposed",
          "stance": "positive",
          "conviction": "low",
          "credit": -2.5,
          "nominated": false,
          "reason": "Delivery chatter had been improving all month."
        }
      ]
    },
    {
      "symbol": "GS",
      "direction": "long",
      "position_id": "pos-05",
      "decision_id": "dec-05",
      "resolved_at": "2026-05-26 20:00:00",
      "r_multiple": 0.3,
      "supported": [
        {
          "analyst": "news",
          "side": "supported",
          "stance": "positive",
          "conviction": "low",
          "credit": 0.3,
          "nominated": false,
          "reason": "Deal pipeline commentary picked up, but only slightly."
        }
      ],
      "opposed": []
    },
    {
      "symbol": "T",
      "direction": "long",
      "position_id": "pos-04",
      "decision_id": "dec-04",
      "resolved_at": "2026-05-21 20:00:00",
      "r_multiple": -1.5,
      "supported": [
        {
          "analyst": "news",
          "side": "supported",
          "stance": "positive",
          "conviction": "high",
          "credit": -1.5,
          "nominated": true,
          "reason": "The dividend headlines read as a floor under the price."
        }
      ],
      "opposed": [
        {
          "analyst": "macro",
          "side": "opposed",
          "stance": "underweight",
          "conviction": "low",
          "credit": 1.5,
          "nominated": false,
          "reason": "Refinancing at these rates eats the dividend it is bought for."
        }
      ]
    },
    {
      "symbol": "XOM",
      "direction": "long",
      "position_id": "pos-03",
      "decision_id": "dec-03",
      "resolved_at": "2026-05-19 20:00:00",
      "r_multiple": -2.0,
      "supported": [
        {
          "analyst": "news",
          "side": "supported",
          "stance": "positive",
          "conviction": "high",
          "credit": -2.0,
          "nominated": true,
          "reason": "Crude inventories were drawing three weeks running."
        }
      ],
      "opposed": [
        {
          "analyst": "technical",
          "side": "opposed",
          "stance": "sell",
          "conviction": "medium",
          "credit": 2.0,
          "nominated": false,
          "reason": "No base under it \u2014 this was extended before it was bought."
        }
      ]
    },
    {
      "symbol": "MSFT",
      "direction": "long",
      "position_id": "pos-02",
      "decision_id": "dec-02",
      "resolved_at": "2026-05-12 20:00:00",
      "r_multiple": 1.2,
      "supported": [
        {
          "analyst": "technical",
          "side": "supported",
          "stance": "buy",
          "conviction": "high",
          "credit": 1.2,
          "nominated": true,
          "reason": "Held its rising average through three separate tests."
        }
      ],
      "opposed": []
    },
    {
      "symbol": "AAPL",
      "direction": "long",
      "position_id": "pos-01",
      "decision_id": "dec-01",
      "resolved_at": "2026-05-06 20:00:00",
      "r_multiple": 1.0,
      "supported": [
        {
          "analyst": "technical",
          "side": "supported",
          "stance": "buy",
          "conviction": "high",
          "credit": 1.0,
          "nominated": true,
          "reason": "Broke out of a six-week base on heavy volume."
        }
      ],
      "opposed": [
        {
          "analyst": "macro",
          "side": "opposed",
          "stance": "underweight",
          "conviction": "medium",
          "credit": -1.0,
          "nominated": false,
          "reason": "The rate path still points against consumer hardware."
        }
      ]
    }
  ]
};
