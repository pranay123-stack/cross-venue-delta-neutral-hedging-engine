import { useQuery } from "@/hooks/useApi";
import type { MarketState, SystemStatus, Ticker } from "@/lib/api";
import { datetime } from "@/lib/format";
import { ConnectionDot } from "./ui";

/**
 * The first thing a visitor sees: where the numbers come from and what the
 * system is allowed to do, before any number at all.
 *
 * It keeps five things that look alike on a price table visibly apart:
 * live public market data, a replayed recording, paper execution, hypothetical
 * stress overlays -- and real exchange execution, which does not exist.
 */
export function SystemSummary({
  status,
  tickers,
}: {
  status: SystemStatus;
  tickers: Ticker[] | undefined;
}) {
  const { data: market } = useQuery<MarketState>("/market", 5000);

  const replay = status.market_data_source === "replay";
  const stale = (tickers ?? []).filter((t) => t.is_stale).length;
  const newest = (tickers ?? []).reduce<number>(
    (latest, t) => Math.max(latest, Date.parse(t.timestamp)),
    0,
  );
  const ageSeconds = newest ? Math.max(0, (Date.now() - newest) / 1000) : null;

  const overlays = market?.overlays ?? status.market_overlays;
  const scenarios = Object.entries(overlays?.scenarios ?? {}).filter(
    ([, scenario]) => scenario !== "NORMAL",
  );
  const shocks = Object.entries(overlays?.price_shocks ?? {});
  const fundingOverrides = Object.keys(overlays?.funding_overrides ?? {});
  const stressed = scenarios.length + shocks.length + fundingOverrides.length > 0;

  const connected = status.venues.filter((v) => v.connected).length;
  const breaches = status.hard_limit_breaches?.length ?? 0;
  const watchdog = status.watchdog;

  return (
    <section aria-label="System at a glance" className="summary">
      <div className="grid cols-3" style={{ marginBottom: 16 }}>
        <div className="panel summary-card">
          <div className="label">Execution</div>
          <div className="value">
            <span className="badge level-NORMAL">PAPER</span>
          </div>
          <div className="sub">
            Orders fill on the venues&apos; real order books, in this process only.
            Real exchange execution is <strong>not implemented</strong> —{" "}
            {status.live_adapters_registered} live adapters registered.
          </div>
        </div>

        <div className="panel summary-card">
          <div className="label">Market data</div>
          <div className="value">
            <span className={`badge ${replay ? "level-WARNING" : "level-NORMAL"}`}>
              {replay ? "RECORDED REPLAY" : "LIVE PUBLIC DATA"}
            </span>
            {stale > 0 && (
              <span className="badge level-DANGER" style={{ marginLeft: 6 }}>
                {stale} STALE
              </span>
            )}
          </div>
          <div className="sub">
            {!status.market_clock
              ? "No quote received yet — the first poll is pending or a venue is unreachable."
              : replay
                ? `A recording of a real Delta Exchange + Hyperliquid session, frame ${
                    market?.replay?.position ?? "?"
                  } of ${market?.replay?.frames ?? "?"}, captured ${datetime(status.market_clock)}.`
                : `Delta Exchange (India) and Hyperliquid public APIs; newest quote ${
                    ageSeconds === null ? "—" : `${ageSeconds.toFixed(0)}s`
                  } old.`}
          </div>
        </div>

        <div className="panel summary-card">
          <div className="label">Stress overlays</div>
          <div className="value">
            {stressed ? (
              <span className="badge level-EMERGENCY">HYPOTHETICAL PRICES</span>
            ) : (
              <span className="badge level-NORMAL">NONE</span>
            )}
          </div>
          <div className="sub">
            {stressed ? (
              <>
                Prices shown are the real market{" "}
                <strong>transformed</strong> by:{" "}
                {[
                  ...scenarios.map(([scope, scenario]) => `${scenario} (${scope})`),
                  ...shocks.map(([underlying, move]) => `${underlying} shock ${move}`),
                  ...fundingOverrides.map((key) => `funding override ${key}`),
                ].join(", ")}
                . Clear them on Fault Injection.
              </>
            ) : (
              "Every price shown is the real market, unmodified."
            )}
          </div>
        </div>

        <div className="panel summary-card">
          <div className="label">Venues</div>
          <div className="value">
            {connected} / {status.venues.length} connected
          </div>
          <div className="sub">
            {status.venues.map((venue) => (
              <div key={venue.venue}>
                <ConnectionDot connected={venue.connected} />
                {venue.venue} · {venue.open_positions} open
                {!venue.connected && " · DISCONNECTED"}
              </div>
            ))}
          </div>
        </div>

        <div className="panel summary-card">
          <div className="label">Safety</div>
          <div className="value">
            {status.kill_switch_engaged ? (
              <span className="badge level-KILL_SWITCH">KILL SWITCH ENGAGED</span>
            ) : status.trading_paused ? (
              <span className="badge level-DANGER">TRADING PAUSED</span>
            ) : (
              <span className="badge level-NORMAL">TRADING PERMITTED</span>
            )}
          </div>
          <div className="sub">
            Watchdog:{" "}
            {watchdog?.running
              ? `running, last check ${watchdog.last_check_age_s?.toFixed(0)}s ago`
              : (
                <>
                  not running — it is a separate process: <code>hedgelab watchdog</code>
                </>
              )}
            {" · "}
            hard-limit refusals: {breaches}
          </div>
        </div>

        <div className="panel summary-card">
          <div className="label">Reproduce this</div>
          <div className="sub" style={{ marginTop: 6 }}>
            <code>docker compose exec backend hedgelab demo</code> runs the 25-step
            acceptance run; <code>hedgelab scenario run-all</code> the ten failure
            scenarios; <code>hedgelab backtest run</code> a month of real history.
            Offline: <code>HEDGELAB_MARKET_DATA_SOURCE=replay</code>.
          </div>
        </div>
      </div>
    </section>
  );
}
