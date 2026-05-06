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
- RS-line (aktie/SPY) på 52-veckors high → högsta prioritet

### Lager 2 — Mark Minervini (vcp_analyzer.py)
VCP-mönsteranalys via Claude AI i tre nivåer:
- **Tier 0 (kvant):** segmentbaserad kontraktionskontroll, volymuttorkning (5d avg < 60% av 100d avg)
- **Tier 1 (Haiku):** kort prompt — bekräfta/avvisa VCP
- **Tier 2 (Sonnet):** full 60-bars OHLCV-analys — breakout-nivå, stop, confidence, quality score
- ATR-justerad stop: klämmer pivot-stop till 1–3× ATR

### Lager 3 — Paul Tudor Jones (risk_manager.py + position_monitor.py)
- Riskstorlek: 1,5% per trade (→2% vid hög confidence)
- Consecutive loss factor: 1 förlust=75%, 2=50%, 3+=33%
- Marknadsregim: SPY vs MA200 (bull/neutral/bear) — bear blockerar alla ordrar
- Max 8 positioner | Max 2 per sektor
- Portfolio heat-tak: 8%
- Daglig förlustgräns: −4% | Drawdown-halt: −12%
- Korrelationsguard: skippa om r≥0,80 med befintlig position

## Exitregler (position_monitor.py — var 15:e minut)

| Steg | Trigger | Åtgärd |
|------|---------|--------|
| A | Position syns första gången | Lägg 7% trailing stop |
| B | Vinst ≥ +15% | Sälj 50%, strama trailing stop till 5% |
| C | Vinst ≥ +8% | Flytta stop till breakeven |
| D | Hållen ≥15 handelsdagar OCH vinst <+2% | Time stop — stäng trög position |

## Viktiga filer

| Fil | Syfte |
|-----|-------|
| `main.py` | Orkestratör — regime, sektor, korrelation, sizing, ordrar |
| `screener.py` | Simons: Trend Template + RS-line + weekly tight base |
| `vcp_analyzer.py` | Minervini: VCP Tier 0→1→2, ATR-stop, vol dry-up |
| `risk_manager.py` | Tudor Jones: sizing, circuit breakers |
| `position_monitor.py` | Tudor Jones: trailing stop, partial exit, time stop |
| `position_sync.py` | Synk Alpaca↔bot-state (SyncError blockerar all handel) |
| `broker.py` | Alpaca-ordrar |
| `telegram_commands.py` | Tvåvägs-Telegram: /status /orders /positions /cancel /help |
| `order_stream.py` | WebSocket fill-notifieringar |
| `dashboard.py` | Flask-dashboard på port 5002 |
| `config.py` | Alla inställningar (läses från .env) |

## Loggar

| Fil | Innehåll |
|-----|---------|
| `logs/three_masters.log` | Huvudlogg |
| `logs/risk_state.json` | Live: heat, daglig P&L, consecutive losses |
| `logs/monitor_state.json` | Per position: entry_date, stop order-ID, partial exit |
| `logs/trade_journal.jsonl` | Avslutade trades med R-multiples |
| `logs/sync_audit.jsonl` | Varje sync-körning (audit trail) |

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

# Visa risk-state
cat logs/risk_state.json

# Visa senaste trades
tail -20 logs/trade_journal.jsonl | python3 -m json.tool
```
