import { useQuery } from "@/hooks/useApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import type {
  Account,
  Instrument,
  PairRisk,
  PnLBreakdown,
  PortfolioRisk,
  SystemStatus,
  Ticker,
} from "@/lib/api";
import { SystemSummary } from "@/components/SystemSummary";
import {
  Bar,
  Empty,
  ErrorBox,
  Loading,
  MoneyStat,
  Notice,
  Panel,
  RiskBadge,
  Stat,
} from "@/components/ui";
import { bps, decimal, marginLevelClass, money, num, pct, ratio, signClass, time } from "@/lib/format";

interface RiskResponse { count: number; worst_level: string; pairs: PairRisk[] }
interface PnLResponse { currency: string; total_net_pnl: string; pairs: PnLBreakdown[] }
interface AccountsResponse {
  currency: string;
  accounts: Account[];
  totals: Record<string, string>;
}
interface MarketResponse { tickers: Ticker[]; clock: string }
interface InstrumentsResponse { instruments: Instrument[] }

export function Overview() {
  const { data: status } = useQuery<SystemStatus>("/system/status", 5000);
  const { data: accounts, error: accountsError } = useQuery<AccountsResponse>("/accounts", 3000);
  const { data: portfolio, error: portfolioError } = useQuery<PortfolioRisk>("/portfolio", 4000);
  const { data: instruments } = useQuery<InstrumentsResponse>("/instruments", 0);
  const { data: risk } = useQuery<RiskResponse>("/risk", 4000);
  const { data: pnl } = useQuery<PnLResponse>("/pnl", 5000);
  const { data: market, error: marketError } = useQuery<MarketResponse>("/market-data", 10000);
  const { frames } = useWebSocket(["prices"]);

  const livePrices = (frames.prices?.data?.tickers ?? []) as Array<{
    key: string; mid: string; spread_bps: string; is_stale: boolean;
  }>;
  const priceByKey = new Map(livePrices.map((t) => [t.key, t]));

  const intervalHours = new Map(
    (instruments?.instruments ?? []).map((i) => [i.key, num(i.funding_interval_hours)]),
  );

  if (!accounts || !portfolio) {
    const failure = accountsError ?? portfolioError;
    if (!failure) return <Loading what="overview" />;
    return (
      <>
        <h2 className="page-title">Overview</h2>
        <ErrorBox
          message={`The dashboard cannot reach the API: ${failure}. Is the backend running? Start it with \`docker compose up\` (API on port 8001).`}
        />
      </>
    );
  }

  const currency = accounts.currency;
  const fundingPerDay = num(portfolio.aggregate_funding_per_day);

  return (
    <>
      <h2 className="page-title">Overview</h2>
      <p className="page-sub">
        A Delta Exchange perpetual hedged with the same coin on Hyperliquid, so price
        moves cancel. Prices, books and funding are the venues&apos; real public market
        data; every order is a paper fill against those books — no order can reach a
        venue.
      </p>

      {status && <SystemSummary status={status} tickers={market?.tickers} />}

      {num(portfolio.gross_notional) === 0 && (
        <Notice>
          The paper book is flat. Open a position and hedge it on <strong>Hedge Pairs</strong>,
          size one on <strong>Hedge Calculator</strong>, or run{" "}
          <code>hedgelab demo</code> to drive a complete hedge cycle with failures and
          recovery.
        </Notice>
      )}

      <div className="grid cols-4" style={{ marginBottom: 16 }}>
        <MoneyStat
          label="Equity"
          value={accounts.totals.equity}
          currency={currency}
          sub={`across ${accounts.accounts.length} venues`}
        />
        <MoneyStat
          label="Portfolio P&L"
          value={portfolio.portfolio_pnl}
          currency={currency}
          sub={`realised ${money(portfolio.realized_pnl, currency)}`}
        />
        <Stat
          label="Gross exposure"
          value={money(portfolio.gross_notional, currency, 0)}
          sub={`net ${money(portfolio.net_exposure, currency)} (${pct(portfolio.net_exposure_pct)})`}
        />
        <Stat
          label="Portfolio risk"
          value={<RiskBadge level={portfolio.level} />}
          sub={
            portfolio.allows_new_trades
              ? "new hedges permitted"
              : "new hedges blocked"
          }
        />
      </div>

      <div className="grid cols-4" style={{ marginBottom: 16 }}>
        <Stat
          label="Margin utilisation"
          value={pct(portfolio.margin_utilization_pct)}
          sub={
            <Bar pct={num(portfolio.margin_utilization_pct)} />
          }
        />
        <Stat
          label="Worst margin level"
          value={
            num(portfolio.worst_margin_level) >= 99_999
              ? "—"
              : pct(portfolio.worst_margin_level)
          }
          tone={marginLevelClass(portfolio.worst_margin_level)}
          sub="lowest across venues"
        />
        <MoneyStat
          label="Carry per day"
          value={portfolio.aggregate_funding_per_day}
          currency={currency}
          sub={fundingPerDay >= 0 ? "the book earns carry" : "the book pays carry"}
        />
        <Stat
          label="Concentration"
          value={pct(portfolio.concentration_pct)}
          sub={portfolio.concentration_underlying || "—"}
        />
      </div>

      <div className="grid cols-2" style={{ marginBottom: 16 }}>
        <Panel title="Venue accounts" flush>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Venue</th>
                  <th className="num">Balance</th>
                  <th className="num">Equity</th>
                  <th className="num">Used</th>
                  <th className="num">Free</th>
                  <th className="num">Margin level</th>
                  <th className="num">Positions</th>
                </tr>
              </thead>
              <tbody>
                {accounts.accounts.map((account) => (
                  <tr key={account.venue}>
                    <td>
                      {account.venue}
                      {!account.is_connected && (
                        <span className="badge level-DANGER" style={{ marginLeft: 6 }}>
                          OFFLINE
                        </span>
                      )}
                    </td>
                    <td className="num">{money(account.balance, account.currency, 0)}</td>
                    <td className="num">{money(account.equity, account.currency, 0)}</td>
                    <td className="num">{money(account.used_margin, account.currency, 0)}</td>
                    <td className="num">{money(account.free_margin, account.currency, 0)}</td>
                    <td className={`num ${marginLevelClass(account.margin_level)}`}>
                      {num(account.margin_level) >= 99_999 ? "—" : pct(account.margin_level)}
                    </td>
                    <td className="num">{account.open_positions}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>

        <Panel title="Hedge ratios and residual exposure" flush>
          {!risk || risk.pairs.length === 0 ? (
            <Empty what="open hedge pairs" />
          ) : (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Pair</th>
                    <th className="num">Ratio</th>
                    <th className="num">Residual</th>
                    <th className="num">Basis</th>
                    <th>Risk</th>
                  </tr>
                </thead>
                <tbody>
                  {risk.pairs.map((pair) => (
                    <tr key={pair.pair}>
                      <td>{pair.pair}</td>
                      <td className="num">{ratio(pair.hedge_ratio)}</td>
                      <td className="num">{bps(pair.residual_bps)}</td>
                      <td className="num">{bps(pair.basis_bps)}</td>
                      <td><RiskBadge level={pair.level} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      </div>

      <div className="grid cols-2">
        <Panel title="P&L attribution" flush>
          {!pnl || pnl.pairs.length === 0 ? (
            <Empty what="P&L" />
          ) : (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Pair</th>
                    <th className="num">Gross</th>
                    <th className="num">Funding</th>
                    <th className="num">Costs</th>
                    <th className="num">Net</th>
                  </tr>
                </thead>
                <tbody>
                  {pnl.pairs.map((row) => (
                    <tr key={row.pair}>
                      <td>{row.pair}</td>
                      <td className={`num ${signClass(row.gross_pnl)}`}>
                        {money(row.gross_pnl, row.currency)}
                      </td>
                      <td className={`num ${signClass(row.net_funding)}`}>
                        {money(row.net_funding, row.currency)}
                      </td>
                      <td className={`num ${signClass(row.total_costs)}`}>
                        {money(row.total_costs, row.currency)}
                      </td>
                      <td className={`num ${signClass(row.net_pnl)}`}>
                        {money(row.net_pnl, row.currency)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        <Panel
          title="Market data"
          flush
          actions={
            <span className="small muted mono">
              {time(market?.clock ?? status?.market_clock)}
            </span>
          }
        >
          {!market ? (
            marketError ? (
              <ErrorBox message={`No market data: ${marketError}`} />
            ) : (
              <Loading what="prices" />
            )
          ) : market.tickers.length === 0 ? (
            <Empty what="quotes from either venue — the first poll is pending" />
          ) : (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Instrument</th>
                    <th className="num">Bid</th>
                    <th className="num">Ask</th>
                    <th className="num">Spread</th>
                    <th className="num" title="Funding per settlement interval, as each venue charges it">
                      Funding / interval
                    </th>
                    <th
                      className="num"
                      title="The per-interval rate scaled to a year, so 8-hourly Delta and hourly Hyperliquid rates are comparable"
                    >
                      Annualised
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {market.tickers.map((ticker) => {
                    const live = priceByKey.get(ticker.key);
                    return (
                      <tr key={ticker.key}>
                        <td>
                          {ticker.key}
                          {(live?.is_stale ?? ticker.is_stale) && (
                            <span className="badge level-DANGER" style={{ marginLeft: 6 }}>
                              STALE
                            </span>
                          )}
                        </td>
                        <td className="num">{decimal(ticker.bid, 2)}</td>
                        <td className="num">{decimal(ticker.ask, 2)}</td>
                        <td className="num">
                          {bps(live?.spread_bps ?? ticker.spread_bps)}
                        </td>
                        <td className="num">
                          {ticker.funding_rate
                            ? `${(num(ticker.funding_rate) * 10000).toFixed(3)} bps / ${
                                intervalHours.get(ticker.key) ?? "?"
                              }h`
                            : "—"}
                        </td>
                        <td className="num">
                          {ticker.funding_rate && intervalHours.get(ticker.key)
                            ? `${(
                                num(ticker.funding_rate) *
                                (24 / (intervalHours.get(ticker.key) as number)) *
                                365 *
                                100
                              ).toFixed(2)}%`
                            : "—"}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      </div>
    </>
  );
}
