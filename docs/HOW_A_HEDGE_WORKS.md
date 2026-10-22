# How the hedging engine works — one BTC hedge, step by step

This walks one real hedge through the engine from the first price it reads to
the P&L it reports after a 10 % market drop. Every number below was produced
by the engine itself, on the committed recording of a live Delta Exchange +
Hyperliquid session (22 October 2026, 12:35 UTC). Nothing is illustrative.

> **Paper trading.** Orders fill against the venues' *real* order books inside
> the engine; no order reaches an exchange. The last section shows how to
> reproduce every figure here.

---

## The situation

A desk holds **5,000 BTCUSD perpetual contracts long on Delta Exchange** and
wants the price risk gone — without closing the position. The engine's job:
hold the exactly offsetting position on **Hyperliquid**, so that whatever BTC
does, the two legs cancel.

```
      Delta Exchange                                   Hyperliquid
  ┌──────────────────────┐                        ┌──────────────────────┐
  │  LONG 5,000 BTCUSD   │   BTC up  →  gains     │   SHORT  ?  BTC      │   BTC up  →  losses
  │  contracts           │   BTC down →  losses   │                      │   BTC down →  gains
  └──────────────────────┘                        └──────────────────────┘
                     └──────────────  sum ≈ 0  ──────────────┘
```

The question the engine answers is the **?** — and it is harder than it looks.

---

## Step 1 — Read the market

The engine takes one synchronised snapshot of both venues and the FX rate
between them:

| | Delta Exchange · BTCUSD | Hyperliquid · BTC |
|---|---|---|
| Bid / ask | 81,653.0 / 81,653.5 | 81,685 / 81,686 |
| Mid | 81,653.25 USD | 81,685.5 USDC |
| Spread | 0.06 bps | 0.12 bps |
| Funding (as published) | −0.0035807 **% per 8 h** | +0.00125 **% per hour** (published as 0.0000125) |
| Quote currency | USD | USDC |

And from Kraken: **USDC/USD = 0.99985**.

Three things are already different: the venues quote in different dollars,
publish funding in different units over different periods, and price BTC
about $32 apart. The engine normalises all three here, once, at the edge —
nothing downstream ever compares a raw venue field.

---

## Step 2 — Translate the units

The venues don't count positions the same way:

```
   Delta Exchange                     Hyperliquid
   1 contract  =  0.001 BTC           1 unit  =  1 BTC   (traded in steps of 0.00001)

   5,000 contracts × 0.001  =  5 BTC  ─────────▶  hedge is measured in BTC, not in contracts
```

So the position is **5 BTC long**. Matching the raw number — "sell 5,000" —
would have been a **1,000× over-hedge**. The engine always converts through the
underlying coin, never by caching a ratio between symbols.

---

## Step 3 — Measure the exposure

| | Value |
|---|---|
| Position | 5 BTC long |
| Notional | 5 × 81,653.25 = **$408,266.25** |
| Price sensitivity (delta) | 5 — every $1 move in BTC is $5 of P&L |
| One Hyperliquid unit | 1 BTC = 81,685.5 USDC = **$81,673.25** at 0.99985 |

---

## Step 4 — Decide what "neutral" means

"Equal and opposite" has more than one precise meaning. The engine offers nine,
and here is what each one says for this exact position:

| Objective | Hedge on Hyperliquid | What it matches |
|---|---|---|
| `BASE_ASSET_NEUTRAL` | −5.00000 BTC | the same number of coins |
| `QUOTE_PNL_NEUTRAL` *(this pair's setting)* | **−5.00000 BTC** | the same P&L per $1 move, each leg in its own quote currency |
| `NOTIONAL_NEUTRAL` | −4.99877 BTC | the same dollar value — Hyperliquid's BTC is ~$20 dearer in USD |
| `ACCOUNT_CCY_PNL_NEUTRAL` | −5.00075 BTC | the same P&L **in USD**, so the USDC leg is scaled up by 1/0.99985 |
| `FUNDING_ADJUSTED` | −4.91362 BTC | full hedge, trimmed where funding makes the hedge costly to hold |
| `COST_ADJUSTED` | −4.87969 BTC | mean-variance: `h* = β − k/(λσ²)`, trading a little risk for lower cost |
| `RISK_WEIGHTED` | −4.89130 BTC | the **measured** β = 0.978 between the venues (ρ = 0.9945, 500 five-minute samples) |
| `CUSTOM_RATIO` · `PARTIAL` | −5 × your ratio | a deliberate fraction |

The spread between them — about 0.13 BTC, or $10,000 of exposure — is the
difference between *questions*, not rounding. Every result carries its
derivation so the choice is visible, not buried.

---

## Step 5 — Fit the venue's rules

Before an order exists, the quantity must be legal on the venue:

| Rule | Hyperliquid BTC | Result |
|---|---|---|
| Size step | 0.00001 BTC | −5.00000 — no rounding needed |
| Rounding direction | toward zero — never trade more than asked | — |
| Minimum order value | $10 | $408k ✔ |
| Price | five significant figures | ✔ |

Rounding *toward zero* matters: an under-hedge leaves a small, known residual
the rebalancer can close; an over-hedge creates exposure in the opposite
direction that nobody asked for. (Going the other way — hedging a Hyperliquid
position with whole Delta contracts — rounding does bite: 0.00037 BTC is not a
whole number of 0.001 BTC contracts. A property test checks both directions.)

---

## Step 6 — Price the hedge before trading it

The calculator prices the hedge *before* a single order is placed:

| | |
|---|---|
| Execution cost | **$186.26** = Hyperliquid taker fee $183.76 (4.5 bps) + half the spread $2.50 |
| Carry at today's funding rates | **+$166.37 per day** (detail in Step 11) |
| Initial margin on the hedge leg | **$10,209** (2.5 % — Hyperliquid's 40× on BTC) |
| Break-even move | **0.046 %** — the adverse BTC move that would have cost the *unhedged* position what this hedge costs to put on |
| Expected P&L, first day | carry $166.37 − execution $186.26 = **−$19.90** |
| 99 % worst case, first day | −$22.20 — the residual risk left after hedging is tiny |

In plain terms: for about $186 the desk removes the risk on $408,000 of BTC,
and at the funding rates of that moment the hedge would pay for itself in a
little over a day. (Funding rates change — see Step 11.)

---

## Step 7 — Check it is safe to trade

Three independent gates stand between the calculation and an order:

1. **The risk gate** — margin on both venues, fresh data (quotes older than 15 s
   block trading), a sane spread (above 50 bps blocks it), no kill switch.
2. **The hard limits** — wrapped around every venue, below any strategy code:
   maximum order and position size, daily loss per venue, orders per minute.
3. **The dry run** — the engine builds the *exact* order each exchange would
   receive and checks it against that exchange's rules. For this hedge:

```json
POST https://api.india.delta.exchange/v2/orders
{"product_id": 27, "product_symbol": "BTCUSD", "size": 5000, "side": "buy",
 "order_type": "market_order", "time_in_force": "ioc",
 "post_only": false, "reduce_only": false, "client_order_id": "cli-0ecc4a0153e741e9"}

POST https://api.hyperliquid.xyz/exchange
{"type": "order", "grouping": "na", "orders": [{
   "a": 0, "b": false, "p": "77602", "s": "5", "r": false,
   "t": {"limit": {"tif": "Ioc"}}, "c": "0x6be26bc1e17322777aee2547e684c5bb"}]}
```

Both pass every rule. Note the Hyperliquid "market" order: the exchange has no
market order type, so it is an immediate-or-cancel sell limited 5 % through the
mid (77,602), on the five-significant-figure price grid — the way the official
SDK does it.

---

## Step 8 — Execute both legs, safely

Two exchanges cannot be traded atomically. The engine runs a supervised cycle
and writes every step to the database **before** doing it:

```
VALIDATED → AWAITING_MARKET_DATA → CALCULATED → RISK_APPROVED
   → LEG_1_SUBMITTED → LEG_1_FILLED        Delta:        BUY  5,000 contracts @ 81,653.5   fee $204.13
   → LEG_2_SUBMITTED → BOTH_FILLED         Hyperliquid:  SELL 5.00000 BTC     @ 81,685     fee $183.76
   → COMPLETED
```

Two details carry the safety:

* **Leg 2 is sized from what leg 1 actually filled**, not from what was asked —
  if only 3,000 contracts had filled, the hedge would be 3 BTC.
* **Write-ahead persistence** means that if the process dies between the legs,
  the restart knows an order was *submitted with unknown outcome* — and asks
  the exchange, instead of guessing or sending it twice.

---

## Step 9 — The result

| | |
|---|---|
| Hedge ratio | **0.99985** |
| Residual exposure | **0.00075 BTC = 1.50 bps** of the position (tolerance 25 bps) |
| Cross-venue basis | −2.45 bps (Delta's BTC slightly cheaper than Hyperliquid's, in USD) |
| Risk level | NORMAL |

The 0.00075 BTC is not an error — it is exactly 5 BTC × (1 − 0.99985): the two
legs settle in different dollars, and the engine reports that residual rather
than hiding it.

---

## Step 10 — Prove it works: BTC falls 10 %

Now the market drops: BTC from about **81,669 to 73,503**. Here is the pair's
P&L, attributed line by line, before and after:

| Component | Just after hedging | After BTC −10 % |
|---|---:|---:|
| Delta leg — price P&L | −$1.25 | **−$40,827.50** |
| Hyperliquid leg — price P&L | −$2.50 | **+$40,840.00** |
| Fees (both legs) | −$387.90 | −$387.90 |
| Spread and slippage | −$7.50 | −$11.25 |
| FX (restating USDC) | $0.00 | −$6.13 |
| **Net** | **−$399.15** | **−$392.77** |

**A $40,800 move on each leg, and the book moved by $6.** The components are
asserted to add up to the net, so nothing can hide in a rounding line.

One thing the table cannot show, and the engine watches for you: the loss and
the gain sit on **different exchanges**. Delta's account equity fell from
$249,796 to $208,968 while Hyperliquid's rose to $290,650. Each venue margins
its own leg, so a large enough move can threaten one side even while the book
as a whole is flat. That is why margin is monitored **per venue** — here both
remain comfortable, at 4,867 % (Delta) and 3,163 % (Hyperliquid) margin level.

---

## Step 11 — Holding the hedge: funding

While the hedge is open, both perpetuals charge **funding** between longs and
shorts — each venue on its own clock. The engine converts both to the same
unit and settles the same span of time on both:

| | Delta (long) | Hyperliquid (short) |
|---|---|---|
| Rate | −0.0035807 % per 8 h | +0.00125 % per hour |
| Who pays | negative → **shorts pay longs** | positive → **longs pay shorts** |
| This position | **receives $43.86 / day** | **receives $122.51 / day** |
| Settlements per day | 3 | 24 |

At that moment the hedge was **paid to hold: +$166.37 a day**. But that is a
snapshot of the current rate, and rates move. The backtest on 30 days of real
settled funding is the honest measure: per BTC hedged, the snapshot above
implies about $1,000 a month; the month actually delivered **$303** — Delta's
funding spent much of it positive, so the long leg paid $261 while the short
leg received $564. Displayed funding is not realised carry, and the engine
reports both.

---

## When something goes wrong

The same position, with Hyperliquid rejecting the hedge order:

```
LEG_1_FILLED → LEG_2_SUBMITTED → RECOVERY_REQUIRED → REBALANCING → COMPLETED
```

The engine sees exposure on the book without a complete hedge, measures the
residual from the venues' own positions, trades the difference, and finishes
at the same 1.50 bps. Partial fills, timeouts, disconnects, stale data, a 15 %
crash on a levered book and a process restart each have their own tested path —
see the [failure scenarios](FAILURE_RECOVERY.md) and the
[engineering case study](ENGINEERING_CASE_STUDY.md). And if the process
itself dies with the hedge open, an independent watchdog flattens both legs
(35 seconds, in a Docker crash drill).

---

## The whole flow on one page

```
 read both venues ─▶ normalise units ─▶ measure exposure ─▶ choose objective ─▶ fit venue rules
   (prices, funding,    (contracts→coins,    (5 BTC, $408k,      (9 ways to be       (step, tick,
    FX)                  %/8h→fraction/h)     delta 5)            "neutral")          $10 minimum)
                                                                                        │
 report & reconcile ◀── execute 2 legs ◀── dry-run payloads ◀── hard limits ◀── risk gate ◀── price the hedge
   (P&L by component,    (write-ahead,       (exact exchange     (size, loss,     (margin, fresh   (cost $186,
    per-venue margin)     leg 2 from fill)     orders, checked)    rate)            data, spread)    carry, margin)
        │
        ▼
 hold: funding on each venue's clock · rebalance beyond tolerance · watch per-venue margin · watchdog
```

---

## Reproduce every number

```bash
make demo                 # or: docker compose exec backend hedgelab demo
cd backend
HEDGELAB_MARKET_DATA_SOURCE=replay \
HEDGELAB_MARKET_REPLAY_PATH=tests/data/live_frames.jsonl.gz \
HEDGELAB_MARKET_HISTORY_PATH=tests/data/history.json.gz \
.venv/bin/hedgelab calculate --source PAPER_DELTA:BTCUSD --hedge PAPER_HYPERLIQUID:BTC \
    --quantity 5000 --objective QUOTE_PNL_NEUTRAL
```

The dashboard's **Hedge Calculator** shows the same derivation for any pair
and objective; **Hedge Pairs** shows the executed position, its residual and
its costs; **Fault Injection** runs the failure scenarios.
