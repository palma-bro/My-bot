import os
import asyncio
import aiohttp
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

logging.basicConfig(level=logging.INFO, format=’%(asctime)s [%(levelname)s] %(message)s’)
logger = logging.getLogger(“BYBIT_BOT”)

TOKEN = os.environ.get(“TOKEN”)
OWNER_ID = int(os.environ.get(“OWNER_ID”, “0”))
BYBIT_URL = “https://api.bybit.com”

# ─── Состояние ───────────────────────────────────────────────

state = {
“running”: False,
“chat_id”: None,
“min_score”: 75,        # минимальный % уверенности для сигнала
“interval”: 15,         # минуты между проверками
“notified”: {},         # {symbol: last_signal} чтобы не спамить
“symbols”: [],          # список всех фьючерсов
}

# ─── Bybit API ───────────────────────────────────────────────

async def bybit_get(endpoint: str, params: dict = {}) -> dict:
url = f”{BYBIT_URL}{endpoint}”
try:
async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
async with s.get(url, params=params) as r:
return await r.json()
except Exception as e:
logger.error(f”Bybit API ошибка: {e}”)
return {}

async def get_all_symbols() -> list:
data = await bybit_get(”/v5/market/instruments-info”, {“category”: “linear”})
symbols = []
if data.get(“result”, {}).get(“list”):
for item in data[“result”][“list”]:
if item.get(“status”) == “Trading” and item.get(“symbol”, “”).endswith(“USDT”):
symbols.append(item[“symbol”])
return symbols

async def get_klines(symbol: str, interval: str = “15”, limit: int = 100) -> list:
“”“Получить свечи. interval: 1,3,5,15,30,60,240,D”””
data = await bybit_get(”/v5/market/kline”, {
“category”: “linear”,
“symbol”: symbol,
“interval”: interval,
“limit”: limit
})
if data.get(“result”, {}).get(“list”):
# Формат: [timestamp, open, high, low, close, volume, turnover]
return data[“result”][“list”]
return []

async def get_ticker(symbol: str) -> dict:
data = await bybit_get(”/v5/market/tickers”, {“category”: “linear”, “symbol”: symbol})
if data.get(“result”, {}).get(“list”):
return data[“result”][“list”][0]
return {}

# ─── Технический анализ ──────────────────────────────────────

def calc_ema(closes: list, period: int) -> list:
if len(closes) < period:
return []
ema = [sum(closes[:period]) / period]
k = 2 / (period + 1)
for price in closes[period:]:
ema.append(price * k + ema[-1] * (1 - k))
return ema

def calc_rsi(closes: list, period: int = 14) -> float:
if len(closes) < period + 1:
return 50.0
gains, losses = [], []
for i in range(1, len(closes)):
diff = closes[i] - closes[i-1]
gains.append(max(diff, 0))
losses.append(max(-diff, 0))
avg_gain = sum(gains[-period:]) / period
avg_loss = sum(losses[-period:]) / period
if avg_loss == 0:
return 100.0
rs = avg_gain / avg_loss
return 100 - (100 / (1 + rs))

def calc_macd(closes: list):
ema12 = calc_ema(closes, 12)
ema26 = calc_ema(closes, 26)
if not ema12 or not ema26:
return None, None, None
min_len = min(len(ema12), len(ema26))
macd_line = [ema12[-min_len+i] - ema26[-min_len+i] for i in range(min_len)]
signal_line = calc_ema(macd_line, 9)
if not signal_line:
return None, None, None
histogram = macd_line[-1] - signal_line[-1]
return macd_line[-1], signal_line[-1], histogram

def calc_bollinger(closes: list, period: int = 20):
if len(closes) < period:
return None, None, None
recent = closes[-period:]
sma = sum(recent) / period
std = (sum((x - sma) ** 2 for x in recent) / period) ** 0.5
return sma + 2 * std, sma, sma - 2 * std  # upper, middle, lower

def calc_volume_signal(volumes: list) -> float:
“”“Объём последней свечи vs среднее за 20 свечей”””
if len(volumes) < 21:
return 1.0
avg = sum(volumes[-21:-1]) / 20
if avg == 0:
return 1.0
return volumes[-1] / avg

def analyze(klines: list, ticker: dict) -> dict:
“””
Полный технический анализ.
Возвращает: signal (LONG/SHORT/НЕЙТРАЛЬНО), score (0-100), reasons
“””
if len(klines) < 50:
return {“signal”: “НЕЙТРАЛЬНО”, “score”: 0, “reasons”: []}

```
# Парсим данные (klines отсортированы от новых к старым на Bybit)
klines = list(reversed(klines))
closes  = [float(k[4]) for k in klines]
highs   = [float(k[2]) for k in klines]
lows    = [float(k[3]) for k in klines]
volumes = [float(k[5]) for k in klines]

price = closes[-1]

# ── Индикаторы ───────────────────────────────────────────
rsi = calc_rsi(closes)
macd_val, macd_signal, macd_hist = calc_macd(closes)
ema9  = calc_ema(closes, 9)
ema21 = calc_ema(closes, 21)
ema50 = calc_ema(closes, 50)
bb_upper, bb_mid, bb_lower = calc_bollinger(closes)
vol_ratio = calc_volume_signal(volumes)

# Данные из тикера
funding = float(ticker.get("fundingRate", 0))
open_interest_change = float(ticker.get("openInterestValue", 0))
price_24h_change = float(ticker.get("price24hPcnt", 0)) * 100

long_score = 0
short_score = 0
reasons_long = []
reasons_short = []

# ── RSI ──────────────────────────────────────────────────
if rsi < 30:
    long_score += 20
    reasons_long.append(f"RSI={rsi:.1f} (перепродан)")
elif rsi < 45:
    long_score += 10
    reasons_long.append(f"RSI={rsi:.1f} (зона покупки)")
elif rsi > 70:
    short_score += 20
    reasons_short.append(f"RSI={rsi:.1f} (перекуплен)")
elif rsi > 55:
    short_score += 10
    reasons_short.append(f"RSI={rsi:.1f} (зона продажи)")

# ── MACD ─────────────────────────────────────────────────
if macd_hist is not None:
    if macd_val > macd_signal and macd_hist > 0:
        long_score += 20
        reasons_long.append(f"MACD бычий ({macd_hist:.4f})")
    elif macd_val < macd_signal and macd_hist < 0:
        short_score += 20
        reasons_short.append(f"MACD медвежий ({macd_hist:.4f})")

# ── EMA тренд ────────────────────────────────────────────
if ema9 and ema21 and ema50:
    e9, e21, e50 = ema9[-1], ema21[-1], ema50[-1]
    if e9 > e21 > e50 and price > e9:
        long_score += 25
        reasons_long.append(f"EMA9>EMA21>EMA50 (восходящий тренд)")
    elif e9 < e21 < e50 and price < e9:
        short_score += 25
        reasons_short.append(f"EMA9<EMA21<EMA50 (нисходящий тренд)")
    elif e9 > e21:
        long_score += 10
        reasons_long.append("EMA9 выше EMA21")
    elif e9 < e21:
        short_score += 10
        reasons_short.append("EMA9 ниже EMA21")

# ── Bollinger Bands ──────────────────────────────────────
if bb_upper and bb_lower:
    if price < bb_lower:
        long_score += 15
        reasons_long.append(f"Цена ниже нижней BB (отскок вверх)")
    elif price > bb_upper:
        short_score += 15
        reasons_short.append(f"Цена выше верхней BB (отскок вниз)")

# ── Объём ────────────────────────────────────────────────
if vol_ratio > 2.0:
    # Высокий объём подтверждает направление
    if long_score > short_score:
        long_score += 15
        reasons_long.append(f"Объём x{vol_ratio:.1f} от среднего (подтверждение)")
    else:
        short_score += 15
        reasons_short.append(f"Объём x{vol_ratio:.1f} от среднего (подтверждение)")

# ── Funding Rate ─────────────────────────────────────────
if funding < -0.0005:
    long_score += 10
    reasons_long.append(f"Funding отрицательный ({funding*100:.4f}%) — шорты платят")
elif funding > 0.001:
    short_score += 10
    reasons_short.append(f"Funding высокий ({funding*100:.4f}%) — лонги перегреты")

# ── Итог ─────────────────────────────────────────────────
total = long_score + short_score
if total == 0:
    return {"signal": "НЕЙТРАЛЬНО", "score": 0, "reasons": [], "price": price, "rsi": rsi}

if long_score > short_score:
    score = int(long_score / (long_score + short_score) * 100)
    return {
        "signal": "LONG 🟢",
        "score": score,
        "reasons": reasons_long,
        "price": price,
        "rsi": rsi,
        "macd_hist": macd_hist,
        "vol_ratio": vol_ratio,
        "funding": funding,
        "change_24h": price_24h_change,
    }
else:
    score = int(short_score / (long_score + short_score) * 100)
    return {
        "signal": "SHORT 🔴",
        "score": score,
        "reasons": reasons_short,
        "price": price,
        "rsi": rsi,
        "macd_hist": macd_hist,
        "vol_ratio": vol_ratio,
        "funding": funding,
        "change_24h": price_24h_change,
    }
```

# ─── Сканер ──────────────────────────────────────────────────

async def scan_loop(bot):
# Топ монеты по объёму для начала
priority = [
“BTCUSDT”,“ETHUSDT”,“SOLUSDT”,“BNBUSDT”,“XRPUSDT”,
“DOGEUSDT”,“ADAUSDT”,“AVAXUSDT”,“DOTUSDT”,“LINKUSDT”,
“MATICUSDT”,“LTCUSDT”,“UNIUSDT”,“ATOMUSDT”,“NEARUSDT”,
“APTUSDT”,“ARBUSDT”,“OPUSDT”,“SUIUSDT”,“SEIUSDT”,
]

```
logger.info("Сканер запущен")
while state["running"]:
    try:
        symbols = state["symbols"] or priority
        checked = 0

        for symbol in symbols:
            if not state["running"]:
                break

            try:
                klines = await get_klines(symbol, interval="15", limit=100)
                ticker = await get_ticker(symbol)
                if not klines or not ticker:
                    continue

                result = analyze(klines, ticker)

                if result["score"] >= state["min_score"] and result["signal"] != "НЕЙТРАЛЬНО":
                    # Проверяем не слали ли уже этот сигнал
                    last = state["notified"].get(symbol, {})
                    if last.get("signal") == result["signal"]:
                        continue  # уже уведомляли

                    state["notified"][symbol] = result

                    price = result['price']
                    is_long = "LONG" in result['signal']
                    score = result['score']

                    # Take Profit уровни
                    if is_long:
                        tp1 = round(price * 1.03, 4)
                        tp2 = round(price * 1.06, 4)
                        tp3 = round(price * 1.10, 4)
                        sl  = round(price * 0.96, 4)
                    else:
                        tp1 = round(price * 0.97, 4)
                        tp2 = round(price * 0.94, 4)
                        tp3 = round(price * 0.90, 4)
                        sl  = round(price * 1.04, 4)

                    pair = symbol.replace("USDT", "")
                    signal_icon = "📊 LONG" if is_long else "📊 SHORT"

                    msg = (
                        f"PAIR ${pair}/USDT\n\n"
                        f"{signal_icon}\n"
                        f"Cross (10-50x)\n\n"
                        f"✅ Точность прогноза: {score}%\n\n"
                        f"✔️ Entry Target:\n"
                        f"💡 {price:.5f}\n\n"
                        f"☑️ Take Profits:\n"
                        f"1️⃣ {tp1:.5f}\n\n"
                        f"2️⃣ {tp2:.5f}\n\n"
                        f"3️⃣ {tp3:.5f}\n\n"
                        f"❌ STOP LOSS: {sl:.5f}\n\n"
                        f"📉 RSI: {result['rsi']:.1f} | "
                        f"Объём: x{result.get('vol_ratio',1):.1f} | "
                        f"Funding: {result.get('funding',0)*100:.4f}%"
                    )
                    await bot.send_message(
                        chat_id=state["chat_id"],
                        text=msg
                    )
                    logger.info(f"Сигнал: {symbol} {result['signal']} {result['score']}%")

                checked += 1
                await asyncio.sleep(0.3)

            except Exception as e:
                logger.error(f"Ошибка {symbol}: {e}")
                continue

        logger.info(f"Проверено {checked} монет. Жду {state['interval']} мин...")
        await asyncio.sleep(state["interval"] * 60)

    except Exception as e:
        logger.error(f"Ошибка сканера: {e}")
        await asyncio.sleep(60)
```

# ─── Telegram хендлеры ───────────────────────────────────────

def is_owner(update: Update) -> bool:
return OWNER_ID == 0 or update.effective_user.id == OWNER_ID

def main_menu():
running = state[“running”]
keyboard = [
[InlineKeyboardButton(
“⛔ Остановить сканер” if running else “🚀 Запустить сканер”,
callback_data=“toggle”
)],
[InlineKeyboardButton(f”🎯 Порог: {state[‘min_score’]}%”, callback_data=“threshold”),
InlineKeyboardButton(f”⏱ Интервал: {state[‘interval’]}мин”, callback_data=“interval”)],
[InlineKeyboardButton(“📊 Статус”, callback_data=“status”)],
]
return InlineKeyboardMarkup(keyboard)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not is_owner(update):
return
state[“chat_id”] = update.effective_chat.id
await update.message.reply_text(
“📡 *Bybit Futures Signal Bot*\n\n”
“Анализирую фьючерсы по:\n”
“• RSI (перекупленность/перепроданность)\n”
“• MACD (тренд)\n”
“• EMA 9/21/50 (направление)\n”
“• Bollinger Bands (экстремумы)\n”
“• Объём (подтверждение)\n”
“• Funding Rate (настроение рынка)\n\n”
“Нажми *Запустить сканер* для начала!”,
parse_mode=“Markdown”,
reply_markup=main_menu()
)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
if not is_owner(update):
return
query = update.callback_query
await query.answer()
data = query.data

```
if data == "toggle":
    if state["running"]:
        state["running"] = False
        await query.message.edit_text("⛔ Сканер остановлен.", reply_markup=main_menu())
    else:
        if not state["chat_id"]:
            state["chat_id"] = query.message.chat_id
        state["running"] = True
        # Загружаем все символы в фоне
        asyncio.create_task(load_symbols_and_scan(query.get_bot()))
        await query.message.edit_text(
            f"🚀 Сканер запущен!\n"
            f"Загружаю список монет...\n"
            f"Порог уверенности: {state['min_score']}%\n"
            f"Интервал: {state['interval']} мин",
            reply_markup=main_menu()
        )

elif data == "threshold":
    keyboard = [
        [InlineKeyboardButton("60%", callback_data="thr_60"),
         InlineKeyboardButton("70%", callback_data="thr_70")],
        [InlineKeyboardButton("75%", callback_data="thr_75"),
         InlineKeyboardButton("80%", callback_data="thr_80")],
        [InlineKeyboardButton("90%", callback_data="thr_90"),
         InlineKeyboardButton("◀️ Назад", callback_data="back")],
    ]
    await query.message.edit_text(
        f"🎯 Порог уверенности для сигнала\n\nСейчас: *{state['min_score']}%*\n\n"
        f"Чем выше — тем меньше сигналов, но точнее.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

elif data.startswith("thr_"):
    state["min_score"] = int(data.split("_")[1])
    await query.message.edit_text(
        f"✅ Порог установлен: {state['min_score']}%",
        reply_markup=main_menu()
    )

elif data == "interval":
    keyboard = [
        [InlineKeyboardButton("5 мин", callback_data="int_5"),
         InlineKeyboardButton("15 мин", callback_data="int_15")],
        [InlineKeyboardButton("30 мин", callback_data="int_30"),
         InlineKeyboardButton("1 час", callback_data="int_60")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back")],
    ]
    await query.message.edit_text(
        f"⏱ Интервал между сканированиями\n\nСейчас: *{state['interval']} мин*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

elif data.startswith("int_"):
    state["interval"] = int(data.split("_")[1])
    await query.message.edit_text(
        f"✅ Интервал: {state['interval']} мин",
        reply_markup=main_menu()
    )

elif data == "status":
    total_signals = len(state["notified"])
    long_count = sum(1 for v in state["notified"].values() if "LONG" in v.get("signal",""))
    short_count = sum(1 for v in state["notified"].values() if "SHORT" in v.get("signal",""))
    text = (
        f"📊 *Статус сканера*\n\n"
        f"{'🟢 Работает' if state['running'] else '🔴 Остановлен'}\n"
        f"🎯 Порог: {state['min_score']}%\n"
        f"⏱ Интервал: {state['interval']} мин\n"
        f"📦 Монет в базе: {len(state['symbols'])}\n\n"
        f"*Сигналов за сессию:*\n"
        f"📈 LONG: {long_count}\n"
        f"📉 SHORT: {short_count}"
    )
    await query.message.edit_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="back")]])
    )

elif data == "back":
    await query.message.edit_text(
        "📡 *Bybit Futures Signal Bot*",
        parse_mode="Markdown",
        reply_markup=main_menu()
    )
```

async def load_symbols_and_scan(bot):
logger.info(“Загружаю список всех символов Bybit…”)
symbols = await get_all_symbols()
if symbols:
state[“symbols”] = symbols
logger.info(f”Загружено {len(symbols)} символов”)
await scan_loop(bot)

def main():
app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(CommandHandler(“start”, cmd_start))
app.add_handler(CallbackQueryHandler(button_handler))
logger.info(“Бот запущен!”)
app.run_polling()

if **name** == “**main**”:
main()
