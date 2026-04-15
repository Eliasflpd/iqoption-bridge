from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from iqoptionapi.stable_api import IQ_Option
import os, time, threading, asyncio, logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="IQ Option Bridge API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Singleton de conexão ──
iq: IQ_Option = None
connected = False
balance_type = "PRACTICE"   # SEMPRE demo por padrão
connect_lock = threading.Lock()

def do_connect():
    global iq, connected
    email    = os.getenv("IQOPTION_EMAIL", "")
    password = os.getenv("IQOPTION_PASSWORD", "")
    if not email or not password:
        logger.error("Credenciais não configuradas nas env vars")
        return False, "missing_credentials"
    try:
        iq = IQ_Option(email, password)
        iq.set_max_reconnect(-1)
        check, reason = iq.connect()
        if check:
            iq.change_balance(balance_type)
            connected = True
            logger.info(f"✅ Conectado IQ Option — conta: {balance_type}")
        else:
            connected = False
            logger.error(f"❌ Falha na conexão: {reason}")
        return check, reason
    except Exception as e:
        connected = False
        logger.exception("Erro ao conectar")
        return False, str(e)

def ensure_connected():
    global connected
    if not connected:
        with connect_lock:
            if not connected:
                do_connect()
    return connected

# ── Startup ──
@app.on_event("startup")
def startup_event():
    threading.Thread(target=do_connect, daemon=True).start()

# ── Reconexão automática em background ──
def keepalive():
    while True:
        time.sleep(60)
        global connected
        if iq:
            try:
                if not iq.check_connect():
                    logger.warning("Conexão perdida — reconectando…")
                    connected = False
                    do_connect()
            except:
                pass

threading.Thread(target=keepalive, daemon=True).start()


# ══════════════════════════════════════
#  ENDPOINTS
# ══════════════════════════════════════

@app.get("/")
def root():
    return {
        "service": "IQ Option Bridge",
        "connected": connected,
        "account": balance_type,
        "endpoints": ["/health", "/candles/{asset}/{duration_sec}/{count}", "/price/{asset}", "/assets/binary", "/assets/digital"]
    }

@app.get("/health")
def health():
    return {"ok": True, "connected": connected, "account": balance_type}

@app.get("/connect")
def reconnect():
    ok, reason = do_connect()
    return {"connected": ok, "reason": str(reason)}

@app.get("/candles/{asset}/{duration_sec}/{count}")
def get_candles(asset: str, duration_sec: int, count: int = 50):
    """
    asset: ex. 'EURUSD', 'BTCUSD', 'GBPUSD'
    duration_sec: 60=M1, 300=M5, 900=M15, 3600=H1, 14400=H4, 86400=D1
    count: número de velas (max 1000)
    """
    if not ensure_connected():
        raise HTTPException(503, "Não conectado ao IQ Option")
    try:
        candles = iq.get_candles(asset, duration_sec, min(count, 500), time.time())
        if not candles:
            raise HTTPException(404, f"Sem dados para {asset}")
        result = []
        for c in candles:
            result.append({
                "time":  c["from"] * 1000,
                "open":  c["open"],
                "high":  c["max"],
                "low":   c["min"],
                "close": c["close"],
                "volume": c.get("volume", 0)
            })
        return {"asset": asset, "duration": duration_sec, "candles": result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/price/{asset}")
def get_price(asset: str):
    """Preço atual do ativo"""
    if not ensure_connected():
        raise HTTPException(503, "Não conectado")
    try:
        candles = iq.get_candles(asset, 60, 1, time.time())
        if candles:
            last = candles[-1]
            return {
                "asset": asset,
                "price": last["close"],
                "open":  last["open"],
                "high":  last["max"],
                "low":   last["min"],
                "time":  last["from"] * 1000
            }
        raise HTTPException(404, "Sem dados")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/assets/binary")
def binary_assets():
    """Lista ativos disponíveis para opções binárias"""
    if not ensure_connected():
        raise HTTPException(503, "Não conectado")
    try:
        all_assets = iq.get_all_open_time()
        binary = all_assets.get("binary", {})
        open_assets = {k: v for k, v in binary.items() if v.get("open")}
        return {"count": len(open_assets), "assets": open_assets}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/assets/digital")
def digital_assets():
    """Lista ativos disponíveis para opções digitais"""
    if not ensure_connected():
        raise HTTPException(503, "Não conectado")
    try:
        all_assets = iq.get_all_open_time()
        digital = all_assets.get("digital", {})
        open_assets = {k: v for k, v in digital.items() if v.get("open")}
        return {"count": len(open_assets), "assets": open_assets}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/payout/{asset}")
def get_payout(asset: str):
    """Payout atual do ativo em %"""
    if not ensure_connected():
        raise HTTPException(503, "Não conectado")
    try:
        all_assets = iq.get_all_open_time()
        binary = all_assets.get("binary", {})
        if asset in binary:
            profit = binary[asset].get("profit", {})
            return {
                "asset": asset,
                "payout_1min": profit.get("1min", {}).get("value", 0),
                "payout_5min": profit.get("5min", {}).get("value", 0),
                "payout_15min": profit.get("15min", {}).get("value", 0),
                "open": binary[asset].get("open", False)
            }
        raise HTTPException(404, f"Ativo {asset} não encontrado")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/balance")
def get_balance():
    """Saldo da conta demo"""
    if not ensure_connected():
        raise HTTPException(503, "Não conectado")
    try:
        balance = iq.get_balance()
        currency = iq.get_currency()
        return {"balance": balance, "currency": currency, "account": balance_type}
    except Exception as e:
        raise HTTPException(500, str(e))

# ── Análise técnica server-side ──
@app.get("/analyze/{asset}/{duration_sec}")
def analyze(asset: str, duration_sec: int):
    """Análise técnica completa — retorna snapshot para a IA"""
    if not ensure_connected():
        raise HTTPException(503, "Não conectado")
    try:
        candles = iq.get_candles(asset, duration_sec, 100, time.time())
        if not candles or len(candles) < 15:
            raise HTTPException(404, "Dados insuficientes")

        closes = [c["close"] for c in candles]
        highs  = [c["max"]   for c in candles]
        lows   = [c["min"]   for c in candles]

        # RSI
        def calc_rsi(closes, period=14):
            if len(closes) < period + 1: return None
            diffs = [closes[i] - closes[i-1] for i in range(1, len(closes))]
            g = sum(d for d in diffs[:period] if d > 0) / period
            l = sum(abs(d) for d in diffs[:period] if d < 0) / period
            for d in diffs[period:]:
                g = (g * (period - 1) + max(0, d)) / period
                l = (l * (period - 1) + max(0, -d)) / period
            return round(100 - 100 / (1 + g / l), 2) if l else 100

        # EMA
        def calc_ema(data, period):
            if len(data) < period: return None
            k = 2 / (period + 1)
            ema = sum(data[:period]) / period
            for v in data[period:]: ema = v * k + ema * (1 - k)
            return round(ema, 8)

        # Bollinger
        def calc_boll(closes, period=20):
            if len(closes) < period: return None
            sma = sum(closes[-period:]) / period
            std = (sum((v - sma) ** 2 for v in closes[-period:]) / period) ** 0.5
            upper = sma + 2 * std
            lower = sma - 2 * std
            pct_b = (closes[-1] - lower) / (upper - lower) * 100 if upper != lower else 50
            return {"upper": round(upper, 8), "mid": round(sma, 8), "lower": round(lower, 8), "pct_b": round(pct_b, 1), "squeeze": (upper - lower) / sma * 100 < 2}

        # Pattern
        last, prev = candles[-1], candles[-2]
        body = abs(last["close"] - last["open"])
        lw = min(last["open"], last["close"]) - last["min"]
        uw = last["max"] - max(last["open"], last["close"])
        pattern = "Sem padrão"
        if body < (last["max"] - last["min"]) * 0.1: pattern = "Doji"
        elif lw > body * 2 and uw < body * 0.5 and prev["close"] < prev["open"]: pattern = "Martelo (alta)"
        elif uw > body * 2 and lw < body * 0.5 and prev["close"] > prev["open"]: pattern = "Shooting Star (baixa)"
        elif prev["close"] < prev["open"] and last["close"] > last["open"] and last["open"] < prev["close"] and last["close"] > prev["open"]: pattern = "Engolfo de Alta"
        elif prev["close"] > prev["open"] and last["close"] < last["open"] and last["open"] > prev["close"] and last["close"] < prev["open"]: pattern = "Engolfo de Baixa"
        elif lw > body * 2.5: pattern = "Pin Bar Alta"
        elif uw > body * 2.5: pattern = "Pin Bar Baixa"

        e9  = calc_ema(closes, 9)
        e21 = calc_ema(closes, 21)
        trend = "alta" if e9 and e21 and e9 > e21 and closes[-1] > e9 else "baixa" if e9 and e21 and e9 < e21 and closes[-1] < e9 else "lateral"

        return {
            "asset": asset,
            "duration_sec": duration_sec,
            "preco_atual": closes[-1],
            "vela_atual": {"open": last["open"], "high": last["max"], "low": last["min"], "close": last["close"]},
            "rsi14": calc_rsi(closes),
            "ema9": e9,
            "ema21": e21,
            "bollinger": calc_boll(closes),
            "tendencia_ema": trend,
            "padrao_vela": pattern,
            "num_velas": len(candles),
            "timestamp": int(time.time() * 1000)
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
