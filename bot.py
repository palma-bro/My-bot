import os
import asyncio
import aiohttp
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("BYBIT_BOT")

# Конфигурация из переменных окружения
TOKEN = os.environ.get("TOKEN")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
BYBIT_URL = "https://api.bybit.com"

# Глобальное состояние бота
state = {
    "running": False,
    "chat_id": None,
    "min_score": 75,
    "interval": 15,
    "notified": {},
    "symbols": [],
}

async def bybit_get(endpoint, params={}):
    """Выполнение GET-запроса к API Bybit с имитацией браузера."""
    url = f"{BYBIT_URL}{endpoint}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Content-Type": "application/json"
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get(url, params=params, headers=headers) as r:
                if r.status != 200:
                    error_text = await r.text()
                    logger.error(f"Bybit API error {r.status}: {error_text[:100]}")
                    return {}
                return await r.json()
    except Exception as e:
        logger.error(f"Network error in bybit_get: {e}")
        return {}

async def get_all_symbols():
    """Получение списка всех торгуемых USDT пар."""
    data = await bybit_get("/v5/market/instruments-info", {"category": "linear"})
    symbols = []
    if data.get("result", {}).get("list"):
        for item in data["result"]["list"]:
            if item.get("status") == "Trading" and item.get("symbol", "").endswith("USDT"):
                symbols.append(item["symbol"])
    return symbols

async def get_klines(symbol, interval="15", limit=100):
    """Получение исторических данных (свечей)."""
    data = await bybit_get("/v5/market/kline", {
        "category": "linear", "symbol": symbol,
        "interval": interval, "limit": limit
    })
    if data.get("result", {}).get("list"):
        return data["result"]["list"]
    return []

async def get_ticker(symbol):
    """Получение текущей цены и данных тикера."""
    data = await bybit_get("/v5/market/tickers", {"category": "linear", "symbol": symbol})
    if data.get("result", {}).get("list"):
        return data["result"]["list"][0]
    return {}

# --- Математические функции (Индикаторы) ---

def calc_ema(closes, period):
    if len(closes) < period: return []
    ema = [sum(closes[:period]) / period]
    k = 2 / (period + 1)
    for price in closes[period:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return ema

def calc_rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calc_macd(closes):
    ema12 = calc_ema(closes, 12)
    ema26 = calc_ema(closes, 26)
    if not ema12 or not ema26: return None, None, None
    min_len = min(len(ema12), len(ema26))
    macd_line = [ema12[-min_len+i] - ema26[-min_len+i] for i in range(min_len)]
    signal_line = calc_ema(macd_line, 9)
    if not signal_line: return None, None, None
    return macd_line[-1], signal_line[-1], macd_line[-1] - signal_line[-1]

def calc_bollinger(closes, period=20):
    if len(closes) < period: return None, None, None
    recent = closes[-period:]
    sma = sum(recent) / period
    std = (sum((x - sma) ** 2 for x in recent) / period) ** 0.5
    return sma + 2 * std, sma, sma - 2 * std

def calc_volume_signal(volumes):
    if len(volumes) < 21: return 1.0
    avg = sum(volumes[-21:-1]) / 20
    return volumes[-1] / avg if avg > 0 else 1.0

def analyze(klines, ticker):
    """Технический анализ монеты."""
    if len(klines) < 50:
        return {"signal": "NEUTRAL", "score": 0}
    
    klines = list(reversed(klines))
    closes = [float(k[4]) for k in klines]
    volumes = [float(k[5]) for k in klines]
    price = closes[-1]
    
    rsi = calc_rsi(closes)
    macd_val, macd_sig, macd_hist = calc_macd(closes)
    ema9, ema21, ema50 = calc_ema(closes, 9), calc_ema(closes, 21), calc_ema(closes, 50)
    bb_upper, _, bb_lower = calc_bollinger(closes)
    vol_ratio = calc_volume_signal(volumes)
    funding = float(ticker.get("fundingRate", 0))
    price_24h = float(ticker.get("price24hPcnt", 0)) * 100
    
    ls, ss = 0, 0
    # RSI Logic
    if rsi < 30: ls += 20
    elif rsi < 45: ls += 10
    elif rsi > 70: ss += 20
    elif rsi > 55: ss += 10
    
    # MACD Logic
    if macd_hist is not None:
        if macd_val > macd_sig and macd_hist > 0: ls += 20
        elif macd_val < macd_sig and macd_hist < 0: ss += 20
        
    # EMA Logic
    if ema9 and ema21 and ema50:
        e9, e21, e50 = ema9[-1], ema21[-1], ema50[-1]
        if e9 > e21 > e50 and price > e9: ls += 25
        elif e9 < e21 < e50 and price < e9: ss += 25
        
    # Bollinger Logic
    if bb_upper and bb_lower:
        if price < bb_lower: ls += 15
        elif price > bb_upper: ss += 15
        
    # Volume/Funding
    if vol_ratio > 2.0:
        if ls > ss: ls += 15
        else: ss += 15
    if funding < -0.0005: ls += 10
    elif funding > 0.001: ss += 10
    
    total = ls + ss
    if total == 0:
        return {"signal": "NEUTRAL", "score": 0, "price": price, "rsi": rsi, "vol_ratio": vol_ratio, "funding": funding}
    
    sig = "LONG" if ls > ss else "SHORT"
    score = int((max(ls, ss) / total) * 100) if total > 0 else 0
    
    return {"signal": sig, "score": score, "price": price, "rsi": rsi, "vol_ratio": vol_ratio, "funding": funding, "change_24h": price_24h}

async def scan_loop(bot):
    """Основной цикл сканирования рынка."""
    priority = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"]
    logger.info("Scanner started")
    while state["running"]:
        try:
            symbols = state["symbols"] if state["symbols"] else priority
            for symbol in symbols:
                if not state["running"]: break
                try:
                    klines = await get_klines(symbol)
                    ticker = await get_ticker(symbol)
                    if not klines or not ticker: continue
                    
                    result = analyze(klines, ticker)
                    if result["score"] >= state["min_score"] and result["signal"] != "NEUTRAL":
                        last = state["notified"].get(symbol, {})
                        if last.get("signal") == result["signal"]: continue
                        
                        state["notified"][symbol] = result
                        price = result["price"]
                        is_long = result["signal"] == "LONG"
                        
                        # Расчет TP/SL
                        if is_long:
                            tp = [round(price * 1.03, 5), round(price * 1.06, 5), round(price * 1.10, 5)]
                            sl = round(price * 0.96, 5)
                            icon = "LONG 🟢"
                        else:
                            tp = [round(price * 0.97, 5), round(price * 0.94, 5), round(price * 0.90, 5)]
                            sl = round(price * 1.04, 5)
                            icon = "SHORT 🔴"
                            
                        msg = (
                            f"PAIR ${symbol}\n"
                            f"📊 {icon} (Score: {result['score']}%)\n\n"
                            f"Entry: {price:.5f}\n"
                            f"TP: {tp[0]:.5f} | {tp[1]:.5f} | {tp[2]:.5f}\n"
                            f"SL: {sl:.5f}\n\n"
                            f"RSI: {result['rsi']:.1f} | Vol: x{result['vol_ratio']:.1f}"
                        )
                        await bot.send_message(chat_id=state["chat_id"], text=msg)
                        logger.info(f"Signal sent: {symbol}")
                    
                    await asyncio.sleep(0.5) # Пауза между парами для обхода лимитов
                except Exception as e:
                    logger.error(f"Error analyzing {symbol}: {e}")
            
            logger.info(f"Scan cycle done. Waiting {state['interval']}m...")
            await asyncio.sleep(state["interval"] * 60)
        except Exception as e:
            logger.error(f"Scanner loop error: {e}")
            await asyncio.sleep(60)

# --- Интерфейс Telegram ---

def is_owner(update):
    return OWNER_ID == 0 or update.effective_user.id == OWNER_ID

def main_menu():
    run_txt = "Stop scanner" if state["running"] else "Start scanner"
    kb = [
        [InlineKeyboardButton(run_txt, callback_data="toggle")],
        [InlineKeyboardButton(f"Threshold: {state['min_score']}%", callback_data="threshold"),
         InlineKeyboardButton(f"Interval: {state['interval']}m", callback_data="interval")],
        [InlineKeyboardButton("Status", callback_data="status")]
    ]
    return InlineKeyboardMarkup(kb)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update): return
    state["chat_id"] = update.effective_chat.id
    await update.message.reply_text("Bybit Futures Signal Bot Ready.", reply_markup=main_menu())

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == "toggle":
        state["running"] = not state["running"]
        if state["running"]:
            state["chat_id"] = query.message.chat_id
            asyncio.create_task(load_and_scan(context.bot))
        await query.message.edit_text(f"Scanner {'Started' if state['running'] else 'Stopped'}", reply_markup=main_menu())
    
    elif data == "threshold":
        kb = [[InlineKeyboardButton(f"{x}%", callback_data=f"thr_{x}") for x in [60, 70, 75, 80, 90]], [InlineKeyboardButton("Back", callback_data="back")]]
        await query.message.edit_text("Select accuracy threshold:", reply_markup=InlineKeyboardMarkup(kb))
    
    elif data.startswith("thr_"):
        state["min_score"] = int(data.split("_")[1])
        await query.message.edit_text(f"Threshold: {state['min_score']}%", reply_markup=main_menu())

    elif data == "interval":
        kb = [[InlineKeyboardButton(f"{x}m", callback_data=f"int_{x}") for x in [5, 15, 30, 60]], [InlineKeyboardButton("Back", callback_data="back")]]
        await query.message.edit_text("Select scan interval:", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("int_"):
        state["interval"] = int(data.split("_")[1])
        await query.message.edit_text(f"Interval: {state['interval']}m", reply_markup=main_menu())

    elif data == "status":
        await query.message.edit_text(f"Status: {'Running' if state['running'] else 'Idle'}\nPairs: {len(state['symbols'])}\nMin Score: {state['min_score']}%", reply_markup=main_menu())
        
    elif data == "back":
        await query.message.edit_text("Menu:", reply_markup=main_menu())

async def load_and_scan(bot):
    symbols = await get_all_symbols()
    if symbols: state["symbols"] = symbols
    await scan_loop(bot)

def main():
    if not TOKEN:
        print("Set TOKEN in environment variables!")
        return
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(button_handler))
    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
