"""
Pump.fun Trading Bot - Optimierte Version v2
- Korrekte Transaktions-Signierung
- Realistische Werte-Filterung
- Partial Take-Profit Strategie
"""

import asyncio
import json
import time
import logging
import os
import base64
import aiohttp

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
RPC_URL = os.environ.get("RPC_URL")
PRIVATE_KEY = os.environ.get("PRIVATE_KEY")
TRADE_AMOUNT_SOL = float(os.environ.get("TRADE_AMOUNT_SOL", "0.1"))
TAKE_PROFIT_1 = 0.50
TAKE_PROFIT_2 = 1.00
STOP_LOSS = 0.40
MAX_CONCURRENT_TRADES = int(os.environ.get("MAX_CONCURRENT_TRADES", "3"))
MAX_MARKETCAP_USD = 40000
MAX_INITIAL_BUY_SOL = 1000

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
log = logging.getLogger(__name__)

active_trades = {}
bot_running = False
total_pnl = 0.0
total_trades = 0
winning_trades = 0
manual_sell_requests = set()


async def send_telegram(message: str, reply_markup=None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
            if reply_markup:
                payload["reply_markup"] = json.dumps(reply_markup)
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    log.error(f"Telegram Fehler: {resp.status}")
    except Exception as e:
        log.error(f"Telegram Fehler: {e}")


async def get_sol_price_usd() -> float:
    try:
        async with aiohttp.ClientSession() as session:
            url = "https://price.jup.ag/v6/price?ids=So11111111111111111111111111111111111111112"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data.get("data", {}).get("So11111111111111111111111111111111111111112", {}).get("price", 150))
    except:
        pass
    return 150.0


async def get_token_price(token_mint: str) -> float:
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://price.jup.ag/v6/price?ids={token_mint}&vsToken=So11111111111111111111111111111111111111112"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data.get("data", {}).get(token_mint, {}).get("price", 0))
    except:
        pass
    return 0.0


async def execute_trade(action: str, token_mint: str, amount) -> bool:
    """Trade ausführen über PumpPortal mit korrekter Signierung"""
    try:
        import base58
        from solders.keypair import Keypair
        from solders.transaction import VersionedTransaction

        key_bytes = base58.b58decode(PRIVATE_KEY)
        keypair = Keypair.from_bytes(key_bytes)
        public_key = str(keypair.pubkey())

        async with aiohttp.ClientSession() as session:
            payload = {
                "publicKey": public_key,
                "action": action,
                "mint": token_mint,
                "amount": amount,
                "denominatedInSol": "true" if action == "buy" else "false",
                "slippage": 15 if action == "buy" else 20,
                "priorityFee": 0.001,
                "pool": "pump"
            }
            async with session.post(
                "https://pumpportal.fun/api/trade-local",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.error(f"PumpPortal {action} Fehler: {resp.status} | {body}")
                    return False
                tx_bytes = await resp.read()

            # Transaktion signieren
            tx = VersionedTransaction.from_bytes(tx_bytes)
            signed_tx = VersionedTransaction(tx.message, [keypair])

            # Signierte Transaktion senden
            send_payload = {
                "jsonrpc": "2.0", "id": 1,
                "method": "sendTransaction",
                "params": [
                    base64.b64encode(bytes(signed_tx)).decode(),
                    {"encoding": "base64", "skipPreflight": True, "maxRetries": 3}
                ]
            }
            async with session.post(RPC_URL, json=send_payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                result = await resp.json()
                if "result" in result:
                    log.info(f"TX erfolgreich: {result['result']}")
                    return True
                log.error(f"TX Fehler: {result.get('error', 'Unbekannt')}")
                return False

    except Exception as e:
        log.error(f"Trade Fehler: {e}")
        return False


async def analyze_token(data: dict) -> tuple:
    name = data.get("name", "")
    initial_buy_raw = data.get("initialBuy", 0)
    market_cap_sol = data.get("marketCapSol", 0)

    # initialBuy kommt in Lamports (1 SOL = 1.000.000.000 Lamports)
    initial_buy = initial_buy_raw / 1_000_000_000

    sol_price = await get_sol_price_usd()
    market_cap_usd = market_cap_sol * sol_price

    if market_cap_usd > MAX_MARKETCAP_USD:
        return False, f"MarketCap zu hoch: ${market_cap_usd:.0f}"

    if initial_buy < 0.1:
        return False, f"Initialer Kauf zu niedrig: {initial_buy:.2f} SOL"

    score = 0
    reasons = []

    if initial_buy >= 1.0:
        score += 3
        reasons.append(f"✅ Starker Kauf: {initial_buy:.2f} SOL")
    elif initial_buy >= 0.5:
        score += 2
        reasons.append(f"✅ Guter Kauf: {initial_buy:.2f} SOL")
    else:
        score += 1
        reasons.append(f"⚠️ Kauf: {initial_buy:.2f} SOL")

    if market_cap_usd < 10000:
        score += 2
        reasons.append(f"✅ Früher Einstieg: ${market_cap_usd:.0f}")
    elif market_cap_usd < 25000:
        score += 1
        reasons.append(f"✅ MarketCap: ${market_cap_usd:.0f}")

    if score >= 2:
        return True, "\n".join(reasons)
    return False, f"Score zu niedrig: {score}"


async def monitor_positions():
    global total_pnl, total_trades, winning_trades

    while True:
        try:
            for token_mint, trade in list(active_trades.items()):
                if not bot_running:
                    break

                if token_mint in manual_sell_requests:
                    success = await execute_trade("sell", token_mint, "100%")
                    if success:
                        manual_sell_requests.discard(token_mint)
                        del active_trades[token_mint]
                        await send_telegram(f"✅ <b>Manuell verkauft!</b>\nToken: {trade['name']}")
                    continue

                current_price = await get_token_price(token_mint)
                if current_price == 0 or trade["entry_price"] == 0:
                    continue

                pnl_percent = ((current_price - trade["entry_price"]) / trade["entry_price"]) * 100
                pnl_sol = TRADE_AMOUNT_SOL * (pnl_percent / 100)

                if pnl_percent >= TAKE_PROFIT_1 * 100 and not trade.get("tp1_done"):
                    success = await execute_trade("sell", token_mint, "50%")
                    if success:
                        active_trades[token_mint]["tp1_done"] = True
                        await send_telegram(
                            f"📊 <b>TAKE-PROFIT 1 (+50%)</b>\n"
                            f"Token: {trade['name']}\n"
                            f"Hälfte gesichert: +{pnl_sol/2:.3f} SOL",
                            reply_markup={"inline_keyboard": [[{"text": "🔴 Alles verkaufen", "callback_data": f"sell_{token_mint}"}]]}
                        )

                elif pnl_percent >= TAKE_PROFIT_2 * 100 and trade.get("tp1_done"):
                    success = await execute_trade("sell", token_mint, "100%")
                    if success:
                        total_trades += 1
                        winning_trades += 1
                        total_pnl += pnl_sol
                        del active_trades[token_mint]
                        await send_telegram(f"🎯 <b>TAKE-PROFIT 2 (+100%)</b>\nToken: {trade['name']}\nGesamt PnL: {total_pnl:+.3f} SOL")

                elif pnl_percent <= -(STOP_LOSS * 100):
                    success = await execute_trade("sell", token_mint, "100%")
                    if success:
                        total_trades += 1
                        total_pnl += pnl_sol
                        del active_trades[token_mint]
                        await send_telegram(f"🛑 <b>STOP-LOSS (-40%)</b>\nToken: {trade['name']}\nVerlust: {pnl_percent:.1f}%\nGesamt PnL: {total_pnl:+.3f} SOL")

        except Exception as e:
            log.error(f"Monitor Fehler: {e}")

        await asyncio.sleep(5)


async def watch_new_tokens():
    while True:
        if not bot_running:
            await asyncio.sleep(5)
            continue

        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect("wss://pumpportal.fun/api/data", timeout=aiohttp.ClientTimeout(total=30)) as ws:
                    await ws.send_json({"method": "subscribeNewToken"})
                    log.info("✅ Verbunden mit Pump.fun")

                    async for msg in ws:
                        if not bot_running:
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await handle_new_token(json.loads(msg.data))

        except Exception as e:
            log.error(f"WebSocket Fehler: {e}")
            await asyncio.sleep(10)


async def handle_new_token(data: dict):
    token_mint = data.get("mint", "")
    if not token_mint or len(active_trades) >= MAX_CONCURRENT_TRADES or token_mint in active_trades:
        return

    name = data.get("name", "Unbekannt")
    symbol = data.get("symbol", "?")

    should_buy, reason = await analyze_token(data)
    if not should_buy:
        log.info(f"⏭ Übersprungen: {name} | {reason}")
        return

    await send_telegram(f"🆕 <b>Token gefunden!</b>\nName: {name} (${symbol})\n{reason}\n\nKaufe {TRADE_AMOUNT_SOL} SOL...")

    entry_price = await get_token_price(token_mint)
    success = await execute_trade("buy", token_mint, TRADE_AMOUNT_SOL)

    if success:
        active_trades[token_mint] = {
            "entry_price": entry_price, "amount": TRADE_AMOUNT_SOL,
            "entry_time": time.time(), "name": name, "symbol": symbol, "tp1_done": False
        }
        await send_telegram(f"✅ <b>Kauf erfolgreich!</b>\nToken: {name} (${symbol})\nEinsatz: {TRADE_AMOUNT_SOL} SOL")
    else:
        await send_telegram(f"❌ Kauf fehlgeschlagen: {name}")


async def handle_telegram_commands():
    global bot_running
    offset = 0

    await send_telegram("🚀 <b>Trading Bot gestartet!</b>\n\n/start - Bot starten\n/stop - Bot stoppen\n/status - Status\n/trades - Aktive Trades\n/pnl - Gewinn/Verlust\n/sellall - Alles verkaufen")

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={offset}&timeout=30"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=35)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for update in data.get("result", []):
                            offset = update["update_id"] + 1

                            callback = update.get("callback_query", {})
                            if callback:
                                cb_data = callback.get("data", "")
                                if cb_data.startswith("sell_"):
                                    manual_sell_requests.add(cb_data.replace("sell_", ""))
                                    await send_telegram("🔴 Manueller Verkauf wird ausgeführt...")
                                continue

                            text = update.get("message", {}).get("text", "")

                            if text == "/start":
                                bot_running = True
                                await send_telegram(f"▶️ <b>Bot läuft!</b>\nEinsatz: {TRADE_AMOUNT_SOL} SOL\nTP1: +50% | TP2: +100%\nStop-Loss: -40%\nMax MarketCap: ${MAX_MARKETCAP_USD:,}")
                            elif text == "/stop":
                                bot_running = False
                                await send_telegram("⏸ <b>Bot pausiert.</b>")
                            elif text == "/status":
                                status = "▶️ Läuft" if bot_running else "⏸ Pausiert"
                                win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
                                await send_telegram(f"📊 <b>Status</b>\nBot: {status}\nAktive Trades: {len(active_trades)}\nGesamt Trades: {total_trades}\nWin Rate: {win_rate:.1f}%\nGesamt PnL: {total_pnl:+.3f} SOL")
                            elif text == "/trades":
                                if not active_trades:
                                    await send_telegram("📭 Keine aktiven Trades.")
                                else:
                                    msg = "📈 <b>Aktive Trades:</b>\n\n"
                                    for mint, trade in active_trades.items():
                                        duration = (time.time() - trade["entry_time"]) / 60
                                        current = await get_token_price(mint)
                                        if trade["entry_price"] > 0 and current > 0:
                                            pnl = ((current - trade["entry_price"]) / trade["entry_price"]) * 100
                                            msg += f"• {trade['name']}: {pnl:+.1f}% | {duration:.0f} Min\n"
                                        else:
                                            msg += f"• {trade['name']} | {duration:.0f} Min\n"
                                    await send_telegram(msg)
                            elif text == "/pnl":
                                win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
                                await send_telegram(f"💰 <b>Gewinn/Verlust</b>\nGesamt PnL: {total_pnl:+.3f} SOL\nTrades: {total_trades}\nGewonnen: {winning_trades}\nWin Rate: {win_rate:.1f}%")
                            elif text == "/sellall":
                                if not active_trades:
                                    await send_telegram("Keine aktiven Trades.")
                                else:
                                    for mint in list(active_trades.keys()):
                                        manual_sell_requests.add(mint)
                                    await send_telegram(f"🔴 Verkaufe alle {len(active_trades)} Positionen...")

        except Exception as e:
            log.error(f"Telegram Command Fehler: {e}")
            await asyncio.sleep(5)


async def main():
    log.info("🚀 Optimierter Trading Bot v2 startet...")
    await asyncio.gather(
        handle_telegram_commands(),
        watch_new_tokens(),
        monitor_positions(),
    )


if __name__ == "__main__":
    asyncio.run(main())
