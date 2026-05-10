# Three Masters Bot — Claude Context

## Vad är det här?

En automatiserad swing trading-bot för US-aktier som kombinerar tre investeringsfilosofier i en sekventiell pipeline. Körs dagligen på **docker-nuc** som systemd user service.

- **Plats:** `/home/habil/three-masters-bot/`
- **Service:** `systemctl --user {start|stop|restart|status} three-masters-bot`
- **Paper account:** Alpaca paper trading (inte riktiga pengar)
- **Nästa scan:** varje dag 22:30 CEST (efter US-börsen stänger)

## De tre lagren

### Lager 1 — Jim Simons (screener.py)
Kvantitativ screening av 500+ S&P 500-aktier med Minervinis Trend Template:
- Pris över MA20/MA50/MA150/MA200 (bull stack)
- MA200 trending uppåt ≥20 dagar
- Inom 25% av 52-veckors high
- RS-rating ≥70 vs SPY, RSI ≤75
- Earnings ≥7 dagar bort
- Veckodiagram: pris över MA10w + MA40w
- Veckodiagram: senaste 4 veckors H-L < 15% av pris (tight base)
- Månadsdiagram: pris över MA10m + MA40m (Stage 2 på tre tidshorisonter)
- RS-line (aktie/SPY) på 52-veckors high → högsta prioritet
- Weinstein Stage-klassificering: Stage 2 = bonus, Stage 3/4 = avdrag
- Earnings acceleration: EPS-tillväxt accelererar Q-för-Q (Minervini SEPA-kärna)
- Institutionellt ägande ≥ 5% (skydd mot pump-stocks utan smart money)
- Market cap ≥ $500M | Pris ≥ $10 | Daglig dollar-volym ≥ $5M

### Lager 2 — Mark Minervini (vcp_analyzer.py)
VCP-mönsteranalys via Claude AI i tre nivåer:
- **Tier 0 (kvant):** segmentbaserad kontraktionskontroll, volymuttorkning (5d avg < 60% av 100d avg)
- **Multi-handle detektion:** räknar distinkta swing-toppar — 3+ handles = +1 quality score bonus
- **Tier 1 (Haiku):** kort prompt — bekräfta/avvisa VCP
- **Tier 2 (Sonnet):** full 60-bars OHLCV-analys — breakout-nivå, stop, confidence, quality score
- ATR-justerad stop: klämmer pivot-stop till 1–3× ATR

### Lager 3 — Paul Tudor Jones (risk_manager.py + position_monitor.py)
- Riskstorlek: 1,5% per trade (→2% vid hög confidence)
- **VIX-skalning:** VIX<15=100%, 15-20=90%, 20-25=80%, 25-30=65%, >30=50%
- **Beta-justerad storlek:** beta>2.0=70%, beta>1.5=85%
- Consecutive loss factor: 1 förlust=75%, 2=50%, 3+=33%
- Marknadsregim: SPY vs MA200 (bull/neutral/bear) — bear blockerar alla ordrar
- **Bear-regim hedge:** köper SH (invers S&P500) vid bear-signal, 0.5% risk
- **NH/NL hard block:** om NH/NL-ratio <0.25 i icke-bull regim → inga nya positioner
- **Distribution days (IBD):** ≥5 dagar = −1.5 Tudor-poäng, ≥3 = −0.5
- **Tvånivå-sektor-skip:** bottom-3 + neg momentum = alltid skip; bottom-half + neg = skip
- Max 8 positioner | Max 2 per sektor | Max 60% i samma super-sektor
- Portfolio heat-tak: 8%
- Daglig förlustgräns: −4% | Drawdown-halt: −12%
- Korrelationsguard: skippa om r≥0,80 med befintlig position

## Exitregler (position_monitor.py — var 15:e minut)

| Steg | Trigger | Åtgärd |
|------|---------|--------|
| A | Position syns första gången | ATR-dynamisk trailing stop (2×ATR, 4–12%) |
| B1 | Vinst ≥ +10% (elite: +15%) | Sälj 33% |
| P | Vinst 12–20%, dag 3+, över MA20 | **Pyramid: köp 30% extra** |
| B2 | Vinst ≥ +20% (el. measured move) | Sälj 33% till, strama trailing till 5% |
| C | Vinst ≥ +8% | Flytta stop till breakeven |
| D | Hållen ≥15 handelsdagar OCH vinst <+2% | Time stop — stäng trög position |

## Skyddsfilter mot spekulationsaktier

Screener blockar aktivt följande typer (i ordning):
1. Pris < $10
2. Volym < 500k/dag eller dollar-volym < $5M/dag
3. Marknadsregim bear (SPY >8% under MA200)
4. Market cap < $500M
5. EPS sjunkande >10%
6. **Institutionellt ägande < 5%** ← stoppar pump-stocks, pre-revenue biotech m.fl.
7. Balance sheet-problem (negativt FCF, hög skuldsättning)
8. 4-veckors bas för bred (>15% H-L range)
9. Månadsdiagram under MA10m
10. VCP-mönster saknas (Tier 0→1→2 måste alla godkännas)

## Re-entry cooldown

| Situation | Cooldown |
|-----------|----------|
| Vanlig stop-out | 5 handelsdagar |
| Stop-out på pyramiderad position | **2 handelsdagar** (shakeout ≠ botten-misslyckande) |
| Pivotmisslyckande (dubbel failure) | 45 handelsdagar |

## Viktiga filer

| Fil | Syfte |
|-----|-------|
| `main.py` | Orkestratör — regime, sektor, korrelation, sizing, ordrar, VWAP-check |
| `screener.py` | Simons: Trend Template + RS-line + weekly/monthly Stage 2 + inst.ägande |
| `vcp_analyzer.py` | Minervini: VCP Tier 0→1→2, multi-handle, ATR-stop, vol dry-up |
| `risk_manager.py` | Tudor Jones: sizing, circuit breakers, cooldown (5d/2d/45d) |
| `position_monitor.py` | Tudor Jones: ATR trailing stop, partial exit, pyramid, time stop |
| `position_sync.py` | Synk Alpaca↔bot-state (SyncError blockerar all handel) |
| `broker.py` | Alpaca-ordrar (buy-stop, sell-stop, market buy/sell) |
| `backtest.py` | Backtesting: `--symbols`, `--years`, `--min-score`, `--out` |
| `telegram_commands.py` | Tvåvägs-Telegram: /status /orders /positions /cancel /help |
| `order_stream.py` | WebSocket fill-notifieringar |
| `dashboard.py` | Flask-dashboard på port 5002 |
| `config.py` | Alla inställningar (läses från .env) |

## Loggar

| Fil | Innehåll |
|-----|---------|
| `logs/stdout.log` | Huvudlogg (stdout + stderr) |
| `logs/risk_state.json` | Live: heat, daglig P&L, consecutive losses |
| `logs/monitor_state.json` | Per position: entry_date, stop order-ID, partial/pyramid exit |
| `logs/trade_journal.jsonl` | Avslutade trades med R-multiples |
| `logs/sync_audit.jsonl` | Varje sync-körning (audit trail) |
| `logs/weekly_backtest.json` | Senaste automatiska veckobacktest (skrivs varje söndag) |
| `logs/feedback_state.json` | Win rate, expectancy, score-bucket-analys |
| `logs/signal_accuracy.json` | Vilka screener-signaler korrelerar med vinst |

## Automatiska jobb

| Tid | Jobb |
|-----|------|
| 22:30 CEST dagligen | Daglig scan (Simons→Minervini→Tudor Jones) |
| 15:15 CEST dagligen | Morning briefing (premarket-volym, PM12-lista) |
| Var 15:e min (marknadstid) | Position monitor (trailing stop, partial, pyramid) |
| 16:00 CEST (10:00 ET) | Opening range check (VWAP, volym, SPY/QQQ) |
| Söndag 07:00 UTC (09:00 CEST) | **Auto backtest** (1 år, min-score 6.5) → Telegram |
| Fredag kväll | Veckorapport (trades, stats, backtest-summary) till Telegram |

## Telegram-kommandon

`/status` `/orders` `/positions` `/cancel SYMBOL` `/help`

## Infrastruktur

- **Watchdog:** `three-masters-watchdog.timer` — startar om boten om heartbeat är >15 min gammal
- **Dashboard:** http://docker-nuc:5002
- **Git-repo:** https://github.com/Akinci-82/three-masters-bot

## Vanliga åtgärder

```bash
# Starta om boten
systemctl --user restart three-masters-bot

# Visa live-loggar
journalctl --user -u three-masters-bot -f

# Kör scan manuellt (test)
cd /home/habil/three-masters-bot && venv/bin/python main.py --run-now

# Kör backtest manuellt
venv/bin/python backtest.py --years 1 --min-score 6.5

# Visa risk-state
cat logs/risk_state.json

# Visa senaste trades
tail -20 logs/trade_journal.jsonl | python3 -m json.tool
```

## Ändringshistorik (senaste patches)

### patch-19 — Robusthet och dataintegritet (2026-05-10)

**main.py**
- `_tg()`: delar upp meddelanden >4000 tecken rekursivt (Telegram 4096-gräns)
- `_gtc_order_age_bdays()` + GTC zombie-rensning i `_smart_order_management()`: avbryter buy-stop ordrar äldre än 3 handelsdagar
- `_archive_old_jsonl()`: flyttad till slutet av `_run_daily_impl()` (undviker race med position monitor under aktiv marknad)
- Skickar `tick_fn=_heartbeat` till `batch_analyze()` — heartbeat var 5:e VCP-kandidat

**vcp_analyzer.py**
- `batch_analyze()`: `tick_fn` callback-parameter — anropas var 5:e kandidat för att hålla watchdog-heartbeat vid liv under långa API-batchar
- `_VCP_CACHE_LOCK`: threading.Lock() runt all cache-I/O (atomisk skrivning via tempfil)
- `anthropic.Anthropic(timeout=20.0)` — förhindrar att hängande Claude-anrop blockerar scan i minuter
- `_call_haiku/sonnet/opus()`: separerar `anthropic.APIError` (nätverksfel → sentinel `{"_api_error": True}`) från JSON-parsningsfel (→ VCP avvisas)
- `analyze()`: `confidence` klämmt till [0,1], `quality` till [1,5]; validerar `breakout > stop_loss > 0`

**risk_manager.py**
- `_RISK_LOCK = threading.Lock()`: skyddar risk_state.json mot race condition mellan scan-tråd och monitor-tråd
- `_save()`: atomisk skrivning via tempfil + `Path.replace()` (POSIX-atomisk)

**position_monitor.py**
- `_save_state()`: atomisk skrivning via `os.replace()`
- `_sync_fail_count`: modul-level räknare; Telegram-larm vid 2+ konsekutiva sync-misslyckanden, nollställs vid framgång

**Alla 4 filer (89 ställen)**
- Tysta `except: pass` → `_log.debug("[%s] suppressed", __name__, exc_info=True)` — buggen `%%s` fixad

---

### patch-18 — Kodkvalitet och optimering (2026-05-10)

**requirements.txt**
- Skapad (saknad sedan start) — venv reproducerbar utan manuell trial-and-error

**config.py**
- `SECTOR_ETF_MAP` centraliserad hit (tidigare duplicerad i main/screener/position_monitor)
- `exdiv_min_days_away: 3` tillagd i TREND_TEMPLATE
- `stop_distance_tiers` tillagd i RISK-dict (hårdkodade trösklar borttagna från risk_manager.py)

**screener.py**
- Batch-download av daily bars: `_bulk_download_daily()` hämtar 500+ symbols i 1–4 anrop (var ~500 individuella) — scan 2–3 min snabbare
- `_check_symbol()` tar emot `prefetched_df` — faller tillbaka till per-symbol vid behov
- Ex-dividend guard: skippar om ex-datum ≤3 dagar bort (config: `exdiv_min_days_away`)
- `_days_to_exdividend()` tillagd
- Lokal sektor-ETF-dict och `import yfinance as _yf_ind` borttagna → använder config.SECTOR_ETF_MAP + global `yf`

**main.py**
- 21 separata `import yfinance as _yf*` → ett enda `import yfinance as yf` i toppen
- Lokal `_SECTOR_ETF_MAP` borttagen → importeras från config
- `_archive_old_jsonl()` tillagd: arkiverar poster >180 dagar från trade_journal.jsonl och sync_audit.jsonl till logs/archive/ (körs vid varje daglig scan)

**position_monitor.py**
- `import yfinance as yf` i toppen (18 lokala importer borttagna)
- Lokal `_SECTOR_ETF_PM` borttagen → importeras från config
- Stock split-detektion: varnar + justerar SL automatiskt om qty-ratio avviker >80%/40% från förväntat
- Stale metadata-fix: `_meta_loaded` flagga + `_meta_date` — laddar om metadata dagligen (inte en gång per positions-livstid)

**risk_manager.py**
- `_stop_distance_factor()` läser trösklar från `RISK["stop_distance_tiers"]` i config
- 89 tysta `except: pass` i hela kodbasen → `_log.debug("[%s] suppressed", __name__, exc_info=True)`

### patch-17 — Strategi-förbättringar session 2 (2026-05-09)
**screener.py**
- Institutionellt ägande ≥ 5% som hårt filter (blockerar pump-stocks)

**vcp_analyzer.py**
- Multi-handle detektion: räknar swing-toppar i basen
- 3+ handles → quality_score +1 (capped vid 5)
- Handle-kontext skickas till Sonnet-prompten

**broker.py**
- `place_market_buy()` tillagd (används för pyramidering)

**position_monitor.py**
- Steg P (Pyramid): köper 30% extra vid +12–20% vinst, dag 3+, över MA20
- `_place_market_buy()` REST-hjälpfunktion tillagd
- Kortare re-entry cooldown (2 dagar) efter pyramid stop-out

**risk_manager.py**
- `record_stop_out()` får `cooldown_days`-parameter (default 5, pyramid=2)
- `check_reentry_cooldown()` läser dynamiskt cooldown_days från state

**Crontab**
- Veckovis backtest varje söndag 07:00 UTC

**main.py**
- Fredagsrapporten inkluderar senaste backtest-summary från `logs/weekly_backtest.json`

### patch-16 — Strategi-förbättringar session 1 (tidigare)
- ATR-dynamisk trailing stop (2×ATR, 4–12%) i position_monitor.py
- Beta-justerad positionsstorlek (`_beta_size_factor()`) i main.py
- Bear-regim SH inverse ETF hedge i main.py
- Hard NH/NL-block vid ratio <0.25 i main.py
- Tvånivå-sektor-skip i main.py
- Weinstein Stage 2-klassificering i screener.py + scoring i main.py
- Premarket-volymtagg i morning briefing (main.py)
- Förbättrad veckorapport: best/worst trades + avg hold (main.py)
- VWAP-bekräftelse i opening range check (main.py)
