/* The analyst scorecard — Mission Control's read of the conviction ledger
 * (docs/QAMC_REMEDIATION_SPEC.md §9.5).
 *
 * Four sections, in the order a reader needs them: the desk at a glance, the
 * ranked table, one analyst opened, one idea traced.
 *
 * PLAIN LANGUAGE IS A REQUIREMENT HERE, NOT POLISH. The reader is not a
 * developer or a trader. Every term is explained on this page before it is
 * used, money is money, and the words "R", "R-multiple", "seat", "payoff
 * ratio", "expectancy", "drawdown" and "conviction-weighted" appear nowhere in
 * what renders. The backend speaks in R; this page converts every figure to
 * the worked-example dollars the explainer defines and never shows the unit.
 *
 * ACCESSIBILITY IS BINDING. In every graphic on this page the meaning is
 * carried by position against a drawn zero line, an explicit + or − sign, a
 * ▲/▼ glyph, or solid-versus-outlined shape. Colour only ever repeats
 * something one of those already said, and a red and a green element are
 * never placed next to each other as the sole distinction between them.
 */

import { useMemo, useState } from "react";
import { api, type AnalystScorecardResponse } from "../../api/client";
import { ANALYST_SCORECARD_EXAMPLE } from "../../fixtures/analystScorecard";
import { usePoll } from "../../lib/usePoll";
import { AnalystDetail } from "./AnalystDetail";
import { AnalystRankedTable } from "./AnalystRankedTable";
import { SlopePanel } from "./DeskSlopes";
import { IdeaTrace } from "./IdeaTrace";
import {
  buildDeskSlopes,
  chooseView,
  defaultIdea,
  monthLabel,
  signedMoney,
} from "./scorecardModel";

function Section({
  number,
  title,
  children,
}: {
  number: string;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-xl border border-border bg-panel p-4">
      <h2 className="m-0 mb-3 flex items-baseline gap-2 border-b border-border pb-2 text-[length:var(--fs-subhead)] font-semibold text-ink">
        <span className="font-mono text-[length:var(--fs-meta)] text-faint">{number}</span>
        {title}
      </h2>
      {children}
    </section>
  );
}

/** Every term this page uses, defined before it is used. Deliberately long:
 * a reader who has to ask what a number means has been failed by the page. */
function HowToRead({ dollarsPerCall }: { dollarsPerCall: number }) {
  const money = `$${Math.round(dollarsPerCall)}`;
  return (
    <section className="rounded-xl border border-accent/30 bg-panel-alt p-4" data-testid="how-to-read">
      <h2 className="m-0 mb-2 text-[length:var(--fs-subhead)] font-semibold text-ink">How to read this page</h2>
      <dl className="m-0 grid gap-x-8 gap-y-3 lg:grid-cols-2">
        <div>
          <dt className="font-semibold text-ink">An analyst</dt>
          <dd className="m-0 text-[length:var(--fs-meta)] leading-relaxed text-dim">
            One of the AI specialists on this desk — the technical analyst, the news analyst, the macro
            analyst, the earnings analyst, the insider-activity analyst. Each one looks at a different kind
            of evidence and gives an opinion on whether a share is worth buying.
          </dd>
        </div>
        <div>
          <dt className="font-semibold text-ink">A call</dt>
          <dd className="m-0 text-[length:var(--fs-meta)] leading-relaxed text-dim">
            One analyst taking one side on one idea: for it, or against it. An analyst with no view on an
            idea takes no side and is not scored on it either way — neither rewarded nor penalised.
          </dd>
        </div>
        <div>
          <dt className="font-semibold text-ink">A settled call</dt>
          <dd className="m-0 text-[length:var(--fs-meta)] leading-relaxed text-dim">
            A call on a trade the desk has since closed, so there is a real result to judge it against.
            Trades still open are not counted anywhere on this page. Every figure here is accompanied by
            the number of settled calls behind it — read that count first.
          </dd>
        </div>
        <div>
          <dt className="font-semibold text-ink">The money</dt>
          <dd className="m-0 text-[length:var(--fs-meta)] leading-relaxed text-dim">
            A worked example, not the desk&rsquo;s actual profit and loss. Every call is treated as if the
            same <strong className="text-ink">{money}</strong> had been put at risk on it. If a trade made
            twice what it risked, that is {signedMoney(2 * dollarsPerCall)} on the idea: everyone who
            backed it is credited that, and everyone who argued against it is charged it. When a trade
            loses, it runs the other way — an analyst who argued against a losing trade is{" "}
            <em>paid</em> for having been right to object. Nothing scales those amounts up or down: the
            figure is simply what the trade did, against the analyst&rsquo;s side of it.
          </dd>
        </div>
        <div>
          <dt className="font-semibold text-ink">How confidently the analyst spoke</dt>
          <dd className="m-0 text-[length:var(--fs-meta)] leading-relaxed text-dim">
            An analyst states how strongly it holds each view, and that does{" "}
            <strong className="text-ink">not</strong> change what the call is worth. A confident call and a
            hedged one are credited or charged exactly the same amount. Scaling the score by an
            analyst&rsquo;s own confidence would assume the very thing this page exists to find out — and a
            confident view already gets a bigger position, so it already earns or loses more. Instead, each
            analyst&rsquo;s record is split by what it said, further down the page, so you can see whether
            its confident calls really are worth more.
          </dd>
        </div>
        <div>
          <dt className="font-semibold text-ink">Betting on a share going down</dt>
          <dd className="m-0 text-[length:var(--fs-meta)] leading-relaxed text-dim">
            The desk can bet on a share falling as well as rising. It counts exactly the same way. If the
            bet made money it is a win and a positive amount; if it lost money it is a loss and a negative
            amount. An analyst who argued for a bet that made money is credited, and one who argued against
            it is charged — the same as for any other trade. Nothing on this page is reversed, inverted or
            written differently because a trade was a bet on a fall.
          </dd>
        </div>
        <div>
          <dt className="font-semibold text-ink">Nothing compounds, nothing expires</dt>
          <dd className="m-0 text-[length:var(--fs-meta)] leading-relaxed text-dim">
            Every call is measured against the same {money}, so profits are never reinvested and a good run
            does not make later calls count for more. A call from months ago counts exactly as much as one
            from last week, and no recent-months window is applied — nothing is trimmed off the record.
          </dd>
        </div>
        <div>
          <dt className="font-semibold text-ink">This page changes nothing</dt>
          <dd className="m-0 text-[length:var(--fs-meta)] leading-relaxed text-dim">
            No score on this page changes how much money any trade gets. An analyst that scores well is not
            given more to work with, and one that scores badly is not given less. This is a record, not a
            control.
          </dd>
        </div>
        <div>
          <dt className="font-semibold text-ink">Nothing is hidden</dt>
          <dd className="m-0 text-[length:var(--fs-meta)] leading-relaxed text-dim">
            There is no minimum number of calls an analyst has to reach before its record is shown. An
            analyst with three settled calls appears with three settled calls, and you can decide for
            yourself how much that is worth.
          </dd>
        </div>
      </dl>
      <p className="m-0 mt-4 text-[length:var(--fs-micro)] text-faint">
        Running-total charts drawn with lightweight-charts by TradingView.
      </p>
    </section>
  );
}

function ExampleBanner({ reason }: { reason: string }) {
  return (
    <div
      data-testid="example-data-banner"
      data-source="example"
      className="flex flex-wrap items-baseline gap-x-3 gap-y-1 rounded-xl border-2 border-dashed border-warn bg-warn/10 px-4 py-3"
      role="status"
    >
      <span className="rounded-full border border-warn px-2 py-0.5 text-[length:var(--fs-micro)] font-bold uppercase tracking-wide text-warn">
        Example data — not real
      </span>
      <span className="text-[length:var(--fs-meta)] leading-snug text-ink">
        {reason} Every name, number and date below is invented. Nothing here describes a trade this desk
        has actually made.
      </span>
    </div>
  );
}

function LiveBanner({ asOf, settled }: { asOf: string; settled: number }) {
  const stamp = (() => {
    const parsed = new Date(asOf);
    return Number.isNaN(parsed.getTime()) ? asOf : parsed.toLocaleString();
  })();
  return (
    <div
      data-testid="live-data-banner"
      data-source="live"
      className="flex flex-wrap items-baseline gap-x-3 gap-y-1 rounded-xl border border-border bg-panel px-4 py-3"
      role="status"
    >
      <span className="rounded-full border border-pos px-2 py-0.5 text-[length:var(--fs-micro)] font-bold uppercase tracking-wide text-pos">
        Real record
      </span>
      <span className="text-[length:var(--fs-meta)] text-ink">
        {settled} settled {settled === 1 ? "call" : "calls"} from this desk&rsquo;s own closed trades. Read
        at {stamp}.
      </span>
    </div>
  );
}

export function AnalystScorecard() {
  const [live, setLive] = useState<AnalystScorecardResponse | null>(null);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [openAnalyst, setOpenAnalyst] = useState<string | null>(null);
  const [openIdea, setOpenIdea] = useState<string | null>(null);

  usePoll(() => {
    api
      .analystScorecard()
      .then((data) => {
        setLive(data);
        setFetchError(null);
      })
      .catch((err: Error) => setFetchError(err.message));
  }, []);

  const view = useMemo(
    () => chooseView(live, ANALYST_SCORECARD_EXAMPLE, fetchError),
    [live, fetchError],
  );
  const { data, source, exampleReason } = view;
  const dollarsPerCall = data.risk_dollars_per_call || 100;

  const slopes = useMemo(
    () => buildDeskSlopes(data.analysts, data.months, dollarsPerCall),
    [data, dollarsPerCall],
  );

  const selectedAnalyst =
    data.analysts.find((a) => a.analyst === openAnalyst) ?? data.analysts[0] ?? null;
  const selectedIdea =
    data.ideas.find((i) => (i.position_id ?? i.symbol) === openIdea) ?? defaultIdea(data.ideas);

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-4 p-4" data-testid="analyst-scorecard">
      <header>
        <h1 className="m-0 text-2xl font-semibold text-ink">Analyst scorecard</h1>
        <p className="m-0 mt-1 max-w-[70ch] text-[length:var(--fs-body)] leading-relaxed text-dim">
          Every idea this desk trades is put forward and argued over by a handful of AI analysts. This page
          keeps score of them: who was right, who was wrong, who was right to object, and what each
          one&rsquo;s opinions have been worth in money.
        </p>
      </header>

      {source === "example" && exampleReason ? (
        <ExampleBanner reason={exampleReason} />
      ) : (
        <LiveBanner asOf={data.as_of} settled={data.resolved_calls_total} />
      )}

      <HowToRead dollarsPerCall={dollarsPerCall} />

      <Section number="1" title="The desk at a glance">
        {slopes ? (
          <>
            <p className="m-0 mb-3 max-w-[75ch] text-[length:var(--fs-meta)] leading-relaxed text-dim">
              Each analyst is drawn twice: where it stood at the end of {monthLabel(slopes.fromMonth)}, and
              where it stands at the end of {monthLabel(slopes.toMonth)}. A line that rises is improving; a
              line that falls is getting worse and is drawn dashed as well as sloping down.{" "}
              <strong className="text-ink">
                The pair matters more than either panel on its own: an analyst climbing on the left while
                falling on the right is getting right more often and still losing money.
              </strong>{" "}
              That happens when its wins are small and its losses are large — being right often is not the
              same as being worth money.
            </p>
            <div className="grid gap-4 lg:grid-cols-2">
              <SlopePanel
                testId="slope-accuracy"
                title="How often right"
                question="Of this analyst's settled calls, what share made money?"
                rows={slopes.accuracy}
                fromMonth={slopes.fromMonth}
                toMonth={slopes.toMonth}
                format={(v) => `${Math.round(v)}%`}
              />
              <SlopePanel
                testId="slope-money"
                title="Money"
                question="Adding up every settled call, where is this analyst in total?"
                rows={slopes.money}
                fromMonth={slopes.fromMonth}
                toMonth={slopes.toMonth}
                format={(v) => signedMoney(v)}
              />
            </div>
            {slopes.moreAccurateButLosing.length > 0 && (
              <p
                data-testid="accurate-but-losing"
                className="m-0 mt-3 rounded-lg border border-warn/50 bg-warn/10 px-3 py-2 text-[length:var(--fs-meta)] leading-relaxed text-ink"
              >
                <strong>Worth looking at:</strong>{" "}
                {slopes.moreAccurateButLosing.join(", ")}{" "}
                {slopes.moreAccurateButLosing.length === 1 ? "is" : "are"} getting right more often than in{" "}
                {monthLabel(slopes.fromMonth)} and still losing money over the same stretch. More accurate,
                worse off.
              </p>
            )}
          </>
        ) : (
          <p className="m-0 text-[length:var(--fs-meta)] text-dim">
            No trade has been closed and scored yet, so there is nothing to compare between two dates.
          </p>
        )}
      </Section>

      <Section number="2" title="Every analyst, ranked by money">
        <p className="m-0 mb-3 max-w-[75ch] text-[length:var(--fs-meta)] leading-relaxed text-dim">
          One row per analyst, best total first. &ldquo;Right how often&rdquo; gives the raw counts before
          the percentage, so a perfect record over two calls never reads like a perfect record over fifty.
          &ldquo;Typical loss&rdquo; is the average of that analyst&rsquo;s losing calls and &ldquo;typical
          win&rdquo; the average of its winning ones: the two bars grow outward from the centre line, loss
          to the left as a hollow outline, win to the right as a solid block. An analyst whose left bar is
          longer than its right needs to be right far more than half the time just to break even. Click any
          row to open it below.
        </p>
        {data.analysts.length > 0 ? (
          <AnalystRankedTable
            analysts={data.analysts}
            dollarsPerCall={dollarsPerCall}
            selected={selectedAnalyst?.analyst ?? null}
            onSelect={setOpenAnalyst}
          />
        ) : (
          <p className="m-0 text-[length:var(--fs-meta)] text-dim">
            No analyst has a settled call yet.
          </p>
        )}
      </Section>

      <Section number="3" title="One analyst, opened up">
        {selectedAnalyst ? (
          <>
            <div className="mb-3 flex flex-wrap gap-1.5">
              {data.analysts.map((item) => {
                const active = item.analyst === selectedAnalyst.analyst;
                return (
                  <button
                    key={item.analyst}
                    type="button"
                    onClick={() => setOpenAnalyst(item.analyst)}
                    aria-pressed={active}
                    className={`min-h-9 rounded-md border px-3 text-[length:var(--fs-meta)] font-semibold ${
                      active
                        ? "border-accent bg-accent/10 text-accent"
                        : "border-border text-dim hover:border-accent hover:text-accent"
                    }`}
                  >
                    {item.analyst}
                  </button>
                );
              })}
            </div>
            <AnalystDetail item={selectedAnalyst} dollarsPerCall={dollarsPerCall} />
          </>
        ) : (
          <p className="m-0 text-[length:var(--fs-meta)] text-dim">
            There is no analyst with a settled call to open yet.
          </p>
        )}
      </Section>

      <Section number="4" title="One idea, traced back">
        {selectedIdea ? (
          <>
            {data.ideas.length > 1 && (
              <div className="mb-3 flex flex-wrap gap-1.5">
                {data.ideas.slice(0, 12).map((idea) => {
                  const key = idea.position_id ?? idea.symbol;
                  const active = key === (selectedIdea.position_id ?? selectedIdea.symbol);
                  return (
                    <button
                      key={key}
                      type="button"
                      onClick={() => setOpenIdea(key)}
                      aria-pressed={active}
                      className={`min-h-9 rounded-md border px-3 font-mono text-[length:var(--fs-meta)] font-semibold ${
                        active
                          ? "border-accent bg-accent/10 text-accent"
                          : "border-border text-dim hover:border-accent hover:text-accent"
                      }`}
                    >
                      {idea.symbol}
                    </button>
                  );
                })}
              </div>
            )}
            <IdeaTrace idea={selectedIdea} dollarsPerCall={dollarsPerCall} />
          </>
        ) : (
          <p className="m-0 text-[length:var(--fs-meta)] text-dim">
            No closed trade has been scored yet, so there is no idea to trace.
          </p>
        )}
      </Section>
    </div>
  );
}
