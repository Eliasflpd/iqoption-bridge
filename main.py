from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from iqoptionapi.api import IQOptionAPI
from pydantic import BaseModel
from typing import Optional
import os, time, threading, logging
import requests as req_lib
from datetime import datetime, timedelta, timezone
import pytz
import pandas as pd
import json
import uuid
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


app = FastAPI(title="IQ Option Bridge API v2")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


iq: IQOptionAPI = None
connected = False
connect_lock = threading.Lock()
_iq_disconnected_since: float = 0.0   # timestamp da primeira queda

# ---------------------------------------------------------------------------
# Arquivo de sinais (JSONL local)
# ---------------------------------------------------------------------------

SIGNALS_FILE = Path("signals.jsonl")


def nowiso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Modelos Pydantic
# ---------------------------------------------------------------------------

class SignalLog(BaseModel):
    asset: str
    direction: str               # CALL ou PUT
    expiration_sec: int
    entry_price: float
    killzone: Optional[str] = None
    news_block: bool = False
    atr_ratio: Optional[float] = None
    rsi: Optional[float] = None
    ema9: Optional[float] = None
    ema21: Optional[float] = None
    pattern: Optional[str] = None
    confluence_score: Optional[int] = None
    filters_matched: Optional[dict] = None
    account_type: str = "PRACTICE"
    notes: Optional[str] = None


class SignalResult(BaseModel):
    signal_id: str
    result: str                  # WIN, LOSS ou DOE
    exit_price: float
    pnl_percent: Optional[float] = None


# ---------------------------------------------------------------------------
# Funcoes de persistencia JSONL
# ---------------------------------------------------------------------------

def log_signal(sig: SignalLog) -> dict:
    entry = {
        "id": str(uuid.uuid4()),
        "created_at": nowiso(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=sig.expiration_sec)).isoformat().replace("+00:00", "Z"),
        "result": "PENDING",
        "exit_price": None,
        "pnl_percent": None,
        **sig.dict()
    }
    with SIGNALS_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    logger.info(f"Sinal registrado: {entry['id']} | {entry['asset']} {entry['direction']}")
    return entry


def read_all_signals() -> list:
    if not SIGNALS_FILE.exists():
        return []
    out = []
    with SIGNALS_FILE.open("r") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    return out


def update_signal_result(res: SignalResult) -> dict:
    signals = read_all_signals()
    updated = None
    for s in signals:
        if s["id"] == res.signal_id:
            s["result"] = res.result
            s["exit_price"] = res.exit_price
            s["pnl_percent"] = res.pnl_percent
            s["closed_at"] = nowiso()
            updated = s
            break
    if not updated:
        raise HTTPException(404, f"Signal {res.signal_id} nao encontrado")
    with SIGNALS_FILE.open("w") as f:
        for s in signals:
            f.write(json.dumps(s) + "\n")
    logger.info(f"Resultado atualizado: {res.signal_id} -> {res.result}")
    return updated


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(text: str, parse_mode: str = "HTML") -> bool:
    """Envia mensagem via Telegram. Fail-open se env vars ausentes."""
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        logger.warning("Telegram nao configurado (faltam TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }

    # Retry com backoff: 15s, 20s, 30s
    timeouts = [15, 20, 30]
    for attempt, timeout in enumerate(timeouts, 1):
        try:
            resp = req_lib.post(url, json=payload, timeout=timeout)
            if resp.status_code == 200:
                if attempt > 1:
                    logger.info(f"Telegram OK na tentativa {attempt}")
                return True
            else:
                logger.warning(f"Telegram retornou {resp.status_code} (tentativa {attempt}): {resp.text[:200]}")
        except req_lib.exceptions.Timeout:
            logger.warning(f"Telegram timeout ({timeout}s) na tentativa {attempt}/{len(timeouts)}")
        except Exception as e:
            logger.error(f"Erro Telegram tentativa {attempt}: {e}")
            break  # Erros nao-timeout: nao tenta de novo

    logger.error("Telegram falhou apos todas as tentativas")
    return False


def alert_once(key: str, message: str, cooldown_sec: int = 3600) -> bool:
    now = time.time()
    if key in _alert_cooldown and (now - _alert_cooldown[key]) < cooldown_sec:
        return False
    ok = send_telegram(message)
    if ok:
        _alert_cooldown[key] = now
    return ok


# ---------------------------------------------------------------------------
# IQ Option — conexao
# ---------------------------------------------------------------------------

def do_connect():
    global iq, connected, _iq_disconnected_since
    email    = os.getenv("IQOPTION_EMAIL", "")
    password = os.getenv("IQOPTION_PASSWORD", "")
    if not email or not password:
        logger.error("Credenciais nao configuradas")
        connected = False
        return False, "missing_credentials"
    try:
        iq = IQOptionAPI("iqoption.com", email, password)
        check, reason = iq.connect()
        if check:
            iq.change_balance("PRACTICE")
            connected = True
            _iq_disconnected_since = 0.0
            logger.info("IQ Option conectado (PRACTICE)")
            return True, "ok"
        else:
            if connected:
                _iq_disconnected_since = time.time()
            connected = False
            logger.error(f"Falha na conexao: {reason}")
            return False, str(reason)
    except Exception as e:
        if connected:
            _iq_disconnected_since = time.time()
        connected = False
        logger.error(f"Excecao na conexao: {e}")
        return False, str(e)


@app.on_event("startup")
def startup():
    threading.Thread(target=do_connect, daemon=True).start()


def keepalive():
    while True:
        time.sleep(90)
        global connected, _iq_disconnected_since
        if iq:
            try:
                if not iq.check_connect():
                    if connected:
                        _iq_disconnected_since = time.time()
                    connected = False
                    do_connect()
            except:
                pass

threading.Thread(target=keepalive, daemon=True).start()


def chk():
    if not connected:
        with connect_lock:
            if not connected: do_connect()
    if not connected:
        raise HTTPException(503, "Nao conectado ao IQ Option")


# ---------------------------------------------------------------------------
# Indicadores tecnicos
# ---------------------------------------------------------------------------

def calc_ema(data, p):
    if len(data) < p: return None
    k = 2/(p+1); ema = sum(data[:p])/p
    for v in data[p:]: ema = v*k + ema*(1-k)
    return round(ema, 6)

def calc_rsi(closes, period=14):
    if len(closes) < period+1: return None
    diffs = [closes[i]-closes[i-1] for i in range(1,len(closes))]
    g = sum(d for d in diffs[:period] if d>0)/period
    l = sum(-d for d in diffs[:period] if d<0)/period
    for d in diffs[period:]:
        g = (g*(period-1) + max(d,0))/period
        l = (l*(period-1) + max(-d,0))/period
    return round(100 - 100/(1 + g/l) if l else 100, 2)

def calc_boll(closes, p=20):
    if len(closes) < p: return None
    sl = closes[-p:]
    m = sum(sl)/p
    sd = (sum((x-m)**2 for x in sl)/p)**0.5
    return {"upper":round(m+2*sd,6),"middle":round(m,6),"lower":round(m-2*sd,6)}

def detect_pattern(raw):
    if len(raw)<3: return "Sem dados"
    last,prev = raw[-1],raw[-2]
    body = abs(last["close"]-last["open"])
    lw = min(last["open"],last["close"])-last["min"]
    uw = last["max"]-max(last["open"],last["close"])
    if body<(last["max"]-last["min"])*0.1: return "Doji"
    if lw>body*2 and uw<body*0.5 and prev["close"]<prev["open"]: return "Martelo (alta)"
    if uw>body*2 and lw<body*0.5 and prev["close"]>prev["open"]: return "Shooting Star (baixa)"
    if prev["close"]<prev["open"] and last["close"]>last["open"] and last["open"]<prev["close"] and last["close"]>prev["open"]: return "Engolfo de Alta"
    if prev["close"]>prev["open"] and last["close"]<last["open"] and last["open"]>prev["close"] and last["close"]<prev["open"]: return "Engolfo de Baixa"
    if lw>body*2.5: return "Pin Bar Alta"
    if uw>body*2.5: return "Pin Bar Baixa"
    return "Sem padrao claro"


# ---------------------------------------------------------------------------
# Filtro #1 — Killzone ICT (horario de NY)
# ---------------------------------------------------------------------------

def is_killzone_active() -> tuple:
    """Retorna (ativo: bool, nome_da_zona: str)"""
    ny_tz = pytz.timezone('America/New_York')
    now_ny = datetime.now(ny_tz)
    total_min = now_ny.hour * 60 + now_ny.minute
    if 11*60+30 <= total_min < 13*60:
        return False, "NY Lunch (DEAD ZONE)"
    zones = {
        "London Open":   (2*60, 5*60),
        "NY Overlap":    (7*60, 10*60),
        "Silver Bullet": (10*60, 11*60),
    }
    for name, (start, end) in zones.items():
        if start <= total_min < end:
            return True, name
    return False, "Fora de Killzone"


# ---------------------------------------------------------------------------
# Filtro #2 — News de alto impacto (Finnhub) com cache 15 min
# ---------------------------------------------------------------------------

_news_cache = {"data": None, "fetched_at": 0}

def fetch_news():
    now = time.time()
    if _news_cache["data"] is not None and (now - _news_cache["fetched_at"] < 900):
        return _news_cache["data"]
    try:
        key = os.getenv("FINNHUB_API_KEY", "")
        if not key:
            logger.warning("FINNHUB_API_KEY nao configurada — filtro de news desativado")
            return []
        today    = datetime.utcnow().strftime("%Y-%m-%d")
        tomorrow = (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%d")
        url = f"https://finnhub.io/api/v1/calendar/economic?from={today}&to={tomorrow}&token={key}"
        resp = req_lib.get(url, timeout=5).json()
        events = resp.get("economicCalendar", [])
        _news_cache["data"] = events
        _news_cache["fetched_at"] = now
        return events
    except Exception as e:
        logger.error(f"Erro ao buscar news: {e}")
        return _news_cache.get("data") or []

def has_high_impact_news(before_min: int = 30, after_min: int = 15) -> tuple:
    events = fetch_news()
    now = datetime.utcnow()
    window_start = now - timedelta(minutes=after_min)
    window_end   = now + timedelta(minutes=before_min)
    for ev in events:
        if ev.get("impact", "").lower() != "high":
            continue
        if ev.get("country") not in ("GB", "US"):
            continue
        try:
            ev_time = datetime.fromisoformat(ev["time"].replace("Z", ""))
        except Exception:
            continue
        if window_start <= ev_time <= window_end:
            desc = f"{ev.get('event','?')} ({ev.get('country')}) @ {ev_time.strftime('%H:%M')} UTC"
            return True, desc
    return False, ""


# ---------------------------------------------------------------------------
# Filtro #3 — Volatilidade real via ATR
# ---------------------------------------------------------------------------

def volatility_ok(raw: list, multiplier: float = 1.2, period: int = 14) -> tuple:
    if len(raw) < period + 1:
        return False, 0.0
    highs  = [c["max"]   for c in raw]
    lows   = [c["min"]   for c in raw]
    closes = [c["close"] for c in raw]
    trs = []
    for i in range(1, len(raw)):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i-1])
        lc = abs(lows[i]  - closes[i-1])
        trs.append(max(hl, hc, lc))
    if len(trs) < period:
        return False, 0.0
    avg_atr    = sum(trs[-period:]) / period
    current_tr = trs[-1]
    if avg_atr == 0:
        return False, 0.0
    ratio = current_tr / avg_atr
    return ratio >= multiplier, round(ratio, 4)


# ---------------------------------------------------------------------------
# Confluencia 4/8 — inferencia de direcao + score
# ---------------------------------------------------------------------------

_CALL_PATTERNS = {"Martelo (alta)", "Engolfo de Alta", "Pin Bar Alta"}
_PUT_PATTERNS  = {"Shooting Star (baixa)", "Engolfo de Baixa", "Pin Bar Baixa"}

def infer_direction(pattern: str) -> Optional[str]:
    if pattern in _CALL_PATTERNS: return "CALL"
    if pattern in _PUT_PATTERNS:  return "PUT"
    return None

def calc_confluence(pattern: str, vol_ok: bool, atr_ratio: float,
                    rsi: Optional[float], ema9: Optional[float],
                    ema21: Optional[float], lc: float,
                    boll: Optional[dict], trend: str,
                    kz_zone: str) -> dict:
    """
    Retorna dict com direction, filters_matched, score, valid.
    4/8 = pattern(obrigatorio) + atr(obrigatorio) + 2 de 6 opcionais.
    """
    direction = infer_direction(pattern)
    if direction is None:
        return {"valid": False, "direction": None, "score": 0, "filters_matched": {}}

    fm = {}
    # Obrigatorios
    fm["pattern"] = pattern not in ("Sem padrao claro", "Sem dados", "Doji") and direction is not None
    fm["atr"]     = vol_ok

    # Opcionais (precisa >= 2)
    fm["rsi_extremo"] = rsi is not None and (rsi < 30 or rsi > 70)

    if ema9 and ema21:
        if direction == "CALL":
            fm["ema_aligned"] = lc > ema9 > ema21
        else:
            fm["ema_aligned"] = lc < ema9 < ema21
    else:
        fm["ema_aligned"] = False

    if direction == "CALL":
        fm["trend_match"] = trend == "alta"
    else:
        fm["trend_match"] = trend == "baixa"

    if boll:
        rng = boll["upper"] - boll["lower"]
        if rng > 0:
            pct_b = (lc - boll["lower"]) / rng * 100
            if direction == "CALL":
                fm["bollinger_breakout"] = pct_b < 5
            else:
                fm["bollinger_breakout"] = pct_b > 95
        else:
            fm["bollinger_breakout"] = False
    else:
        fm["bollinger_breakout"] = False

    fm["killzone_strong"] = kz_zone in ("NY Overlap", "Silver Bullet")
    fm["squeeze_break"]   = False   # placeholder — requer historico de bandwidth

    optional_keys = ["rsi_extremo","ema_aligned","trend_match","bollinger_breakout","killzone_strong","squeeze_break"]
    optional_hits = sum(1 for k in optional_keys if fm.get(k))
    score = 2 + optional_hits   # 2 obrigatorios + N opcionais

    valid = fm["pattern"] and fm["atr"] and optional_hits >= 2

    return {"valid": valid, "direction": direction, "score": score, "filters_matched": fm}


# ---------------------------------------------------------------------------
# Workers de background
# ---------------------------------------------------------------------------

def monitor_worker():
    """Roda a cada 60s: verifica losses, pendentes expirados e conexao."""
    while True:
        time.sleep(60)
        try:
            # 1. Conexao perdida por mais de 2 min
            global _iq_disconnected_since
            if not connected and _iq_disconnected_since > 0:
                down_sec = time.time() - _iq_disconnected_since
                if down_sec > 120:
                    alert_once("disconnected",
                               "🚨 <b>IQ Option desconectado</b>\nBot tentando reconectar.",
                               cooldown_sec=1800)

            # 2. Losses seguidos
            signals = read_all_signals()
            recent = [s for s in signals if s["result"] in ("WIN","LOSS")][-10:]
            loss_streak = 0
            for s in reversed(recent):
                if s["result"] == "LOSS":
                    loss_streak += 1
                else:
                    break
            if loss_streak >= 5:
                alert_once("losses_5",
                           f"🚨🚨 <b>{loss_streak} LOSSES SEGUIDOS</b>\nPARE AGORA e investigue o que mudou.")
            elif loss_streak >= 3:
                alert_once("losses_3",
                           f"⚠️ <b>{loss_streak} losses seguidos</b>\nConsidere pausar e revisar.")

            # 3. Pendentes expirados ha +5min → marca DOE automaticamente
            now_dt = datetime.now(timezone.utc)
            cutoff  = now_dt - timedelta(minutes=5)
            updated = False
            for s in signals:
                if s["result"] == "PENDING":
                    exp = datetime.fromisoformat(s["expires_at"].replace("Z","+00:00"))
                    if exp < cutoff:
                        s["result"]   = "DOE"
                        s["closed_at"] = nowiso()
                        s["notes"]    = "Auto-marked DOE (expired without result update)"
                        updated = True
            if updated:
                with SIGNALS_FILE.open("w") as f:
                    for s in signals:
                        f.write(json.dumps(s) + "\n")
                logger.warning("Sinais pendentes expirados marcados como DOE")

        except Exception as e:
            logger.error(f"Erro monitor_worker: {e}")

threading.Thread(target=monitor_worker, daemon=True).start()


def daily_summary_worker():
    """Envia resumo diario as 22h horario de Brasilia."""
    sent_today = None
    while True:
        time.sleep(60)
        try:
            br = pytz.timezone('America/Sao_Paulo')
            now_br = datetime.now(br)
            if now_br.hour == 22 and now_br.minute < 5:
                today_key = now_br.strftime("%Y-%m-%d")
                if sent_today == today_key:
                    continue
                stats = api_stats(days=1)
                if stats["total_closed"] == 0:
                    sent_today = today_key   # nao repete no mesmo dia mesmo sem trades
                    continue
                zones_text = "\n".join([
                    f"• {z}: {d['wins']}W/{d['losses']}L ({d['win_rate']}%)"
                    for z, d in stats["by_killzone"].items()
                ]) or "Nenhuma zona registrada"
                wr_icon = "✅" if stats["above_break_even"] else "⚠️"
                msg = (
                    f"📊 <b>RESUMO DIÁRIO — {now_br.strftime('%d/%m')}</b>\n"
                    f"Total: {stats['total_closed']} trades\n"
                    f"🟢 Wins: {stats['wins']}\n"
                    f"🔴 Losses: {stats['losses']}\n"
                    f"🟡 DOE: {stats['does']}\n"
                    f"📈 Win rate: {stats['win_rate']}%\n"
                    f"{wr_icon} {'Acima' if stats['above_break_even'] else 'Abaixo'} break-even (55.5%)\n\n"
                    f"<b>Por killzone:</b>\n{zones_text}\n\n"
                    f"<b>Maior streak de loss:</b> {stats['max_loss_streak']}"
                )
                if send_telegram(msg):
                    sent_today = today_key
        except Exception as e:
            logger.error(f"Erro daily_summary_worker: {e}")

threading.Thread(target=daily_summary_worker, daemon=True).start()



# ---------------------------------------------------------------------------
# Endpoints FastAPI
# ---------------------------------------------------------------------------

@app.get("/")
def root():
    tg_configured = bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))
    return {
        "service": "IQ Option Bridge API v2",
        "connected": connected,
        "account": "PRACTICE",
        "telegram_configured": tg_configured,
        "endpoints": [
            "/health", "/balance",
            "/candles/{asset}/{dur}/{qtd}",
            "/price/{asset}", "/payout/{asset}",
            "/analyze/{asset}/{dur}?auto_log=true",
            "/assets/open",
            "/killzone", "/news-check",
            "/telegram/test",
            "/signal/log", "/signal/result",
            "/signals/recent", "/signals/export",
            "/stats"
        ]
    }

@app.get("/health")
def health():
    return {"ok": True, "connected": connected, "account": "PRACTICE"}

@app.get("/connect")
def reconnect():
    ok, reason = do_connect()
    return {"connected": ok, "reason": str(reason)}

@app.get("/balance")
def balance():
    chk()
    try: return {"balance": iq.get_balance(), "currency": iq.get_currency(), "account": "PRACTICE"}
    except Exception as e: raise HTTPException(500, str(e))

@app.get("/candles/{asset}/{duracao_seg}/{quantidade}")
def candles(asset: str, duracao_seg: int, quantidade: int = 50):
    chk()
    try:
        raw = iq.get_candles(asset, duracao_seg, min(quantidade, 500), time.time())
        if not raw: raise HTTPException(404, f"Sem dados para {asset}")
        return {"asset": asset, "duracao_seg": duracao_seg, "total": len(raw),
                "candles": [{"time": c["from"]*1000, "open": c["open"], "high": c["max"],
                              "low": c["min"], "close": c["close"], "volume": c.get("volume", 0)} for c in raw]}
    except HTTPException: raise
    except Exception as e: raise HTTPException(500, str(e))

@app.get("/price/{asset}")
def price(asset: str):
    chk()
    try:
        raw = iq.get_candles(asset, 60, 1, time.time())
        if not raw: raise HTTPException(404, f"Sem dados para {asset}")
        return {"asset": asset, "price": raw[-1]["close"], "time": raw[-1]["from"]*1000}
    except HTTPException: raise
    except Exception as e: raise HTTPException(500, str(e))

@app.get("/payout/{asset}")
def payout(asset: str):
    chk()
    try:
        a = iq.get_all_open_time().get("binary", {}).get(asset)
        if not a: raise HTTPException(404, f"{asset} nao encontrado")
        p = a.get("profit", {})
        return {"asset": asset, "open": a.get("open", False),
                "payout_1min": p.get("1min", {}).get("value", 0),
                "payout_5min": p.get("5min", {}).get("value", 0)}
    except HTTPException: raise
    except Exception as e: raise HTTPException(500, str(e))

@app.get("/assets/open")
def open_assets():
    chk()
    try:
        all_a = iq.get_all_open_time()
        return {
            "binary":  sorted(k for k, v in all_a.get("binary",  {}).items() if v.get("open")),
            "digital": sorted(k for k, v in all_a.get("digital", {}).items() if v.get("open"))
        }
    except Exception as e: raise HTTPException(500, str(e))

@app.get("/killzone")
def killzone_status():
    active, zone = is_killzone_active()
    return {"active": active, "zone": zone}

@app.get("/news-check")
def news_check():
    blocked, desc = has_high_impact_news()
    return {"blocked": blocked, "news": desc}

@app.get("/telegram/test")
def telegram_test():
    """Testa conectividade com Telegram API e envia mensagem de teste."""
    import time as _time
    token_set = bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip())
    chat_set  = bool(os.getenv("TELEGRAM_CHAT_ID", "").strip())

    result = {
        "configured": token_set and chat_set,
        "token_set": token_set,
        "chat_id_set": chat_set,
    }

    if not (token_set and chat_set):
        result["sent"] = False
        result["error"] = "Variaveis de ambiente faltando"
        return result

    # Testa conectividade com Telegram API antes de enviar
    try:
        start = _time.time()
        token = os.getenv("TELEGRAM_BOT_TOKEN").strip()
        check_url = f"https://api.telegram.org/bot{token}/getMe"
        resp = req_lib.get(check_url, timeout=15)
        result["api_reachable"] = resp.status_code == 200
        result["api_latency_ms"] = int((_time.time() - start) * 1000)
        if resp.status_code == 200:
            result["bot_info"] = resp.json().get("result", {}).get("username")
    except Exception as e:
        result["api_reachable"] = False
        result["api_error"] = str(e)

    # Tenta enviar
    ok = send_telegram("\u2705 <b>Bridge conectado ao Telegram!</b>\nTeste de notificacao do iqoption-bridge.")
    result["sent"] = ok
    return result

@app.get("/analyze/{asset}/{duracao_seg}")
def analyze(asset: str, duracao_seg: int, auto_log: bool = Query(False, description="Se True, loga e notifica sinal automaticamente quando confluencia >= 4/8")):
    # --- Filtro #1: Killzone ICT ---
    kz_active, kz_zone = is_killzone_active()
    if not kz_active:
        logger.info(f"Bloqueado ({kz_zone}) — aguardando killzone")
        return {"blocked": True, "reason": f"Fora de killzone: {kz_zone}", "killzone": kz_zone}
    logger.info(f"Killzone ativa: {kz_zone}")

    # --- Filtro #2: News de alto impacto ---
    has_news, news_desc = has_high_impact_news()
    if has_news:
        logger.warning(f"NEWS BLOCK: {news_desc}")
        return {"blocked": True, "reason": f"News de alto impacto: {news_desc}", "killzone": kz_zone}

    chk()
    try:
        raw = iq.get_candles(asset, duracao_seg, 100, time.time())
        if not raw or len(raw) < 15: raise HTTPException(404, "Dados insuficientes")
        closes = [c["close"] for c in raw]
        e9  = calc_ema(closes, 9)
        e21 = calc_ema(closes, 21)
        e50 = calc_ema(closes, 50)
        lc  = closes[-1]
        trend = ("alta"   if e9 and e21 and e9 > e21 and lc > e9 else
                 "baixa"  if e9 and e21 and e9 < e21 and lc < e9 else "lateral")
        last = raw[-1]

        # --- Filtro #3: Volatilidade ATR ---
        vol_ok, atr_ratio = volatility_ok(raw)
        rsi14 = calc_rsi(closes)
        boll  = calc_boll(closes)
        padrao = detect_pattern(raw)

        # --- Confluencia 4/8 ---
        conf = calc_confluence(padrao, vol_ok, atr_ratio, rsi14, e9, e21, lc, boll, trend, kz_zone)

        # Log e notificacao automatica (apenas se auto_log=True e confluencia valida)
        signal_id  = None
        sig_generated = False
        if auto_log and conf["valid"]:
            direction = conf["direction"]
            exp_sec   = duracao_seg * 5   # 5 velas como expiracao padrao
            sig = SignalLog(
                asset=asset,
                direction=direction,
                expiration_sec=exp_sec,
                entry_price=lc,
                killzone=kz_zone,
                atr_ratio=atr_ratio,
                rsi=rsi14,
                ema9=e9,
                ema21=e21,
                pattern=padrao,
                confluence_score=conf["score"],
                filters_matched=conf["filters_matched"],
                account_type="PRACTICE",
            )
            entry = log_signal(sig)
            signal_id     = entry["id"]
            sig_generated = True

            # Notificacao Telegram
            emoji = "🟢" if direction == "CALL" else "🔴"
            tg_msg = (
                f"{emoji} <b>SINAL {asset.upper()}</b>\n"
                f"📍 Direção: <b>{direction}</b>\n"
                f"⏱ Expiração: {exp_sec}s ({exp_sec//60} min)\n"
                f"💰 Entrada: {lc:.5f}\n"
                f"📊 Confluência: {conf['score']}/8\n"
                f"🔥 Killzone: {kz_zone}\n"
                f"📈 Pattern: {padrao}\n"
                f"📉 RSI: {rsi14}\n"
                f"⚡ ATR ratio: {atr_ratio}x\n"
                f"🆔 {signal_id[:8]}..."
            )
            threading.Thread(target=send_telegram, args=(tg_msg,), daemon=True).start()
            logger.info(f"Sinal auto-logado: {signal_id} | {asset} {direction} | score {conf['score']}/8")

        return {
            "blocked": False,
            "asset": asset,
            "duracao_seg": duracao_seg,
            "preco_atual": lc,
            "killzone": kz_zone,
            "vela_atual": {"open": last["open"], "high": last["max"], "low": last["min"], "close": last["close"]},
            "rsi14": rsi14,
            "ema9": e9, "ema21": e21, "ema50": e50,
            "bollinger": boll,
            "tendencia": trend,
            "padrao_vela": padrao,
            "volatilidade_atr": {"ok": vol_ok, "ratio": atr_ratio},
            "confluencia": {
                "valid": conf["valid"],
                "score": conf["score"],
                "direction": conf["direction"],
                "filters_matched": conf["filters_matched"],
            },
            "signal_generated": sig_generated,
            "signal_id": signal_id,
        }
    except HTTPException: raise
    except Exception as e: raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# Endpoints de Logger de Sinais
# ---------------------------------------------------------------------------

@app.post("/signal/log")
def api_log_signal(sig: SignalLog):
    """Registra um novo sinal operado no arquivo JSONL local."""
    try:
        return log_signal(sig)
    except Exception as e:
        raise HTTPException(500, str(e))

@app.post("/signal/result")
def api_update_result(res: SignalResult):
    """Atualiza o resultado (WIN/LOSS/DOE) de um sinal apos expiracao."""
    try:
        return update_signal_result(res)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/signals/recent")
def api_recent(limit: int = 20):
    """Retorna os ultimos N sinais registrados (mais recente primeiro)."""
    signals = read_all_signals()
    return {"total": len(signals), "signals": signals[-limit:][::-1]}

@app.get("/signals/export")
def api_export():
    """Retorna conteudo bruto do JSONL para backup."""
    if not SIGNALS_FILE.exists():
        return {"content": "", "lines": 0}
    content = SIGNALS_FILE.read_text()
    return {"content": content, "lines": len([l for l in content.split("\n") if l.strip()])}

@app.get("/stats")
def api_stats(days: int = 30):
    """Win rate e metricas por dimensao (killzone, hora UTC, streaks)."""
    signals = read_all_signals()
    cutoff  = datetime.now(timezone.utc) - timedelta(days=days)

    closed = [
        s for s in signals
        if s["result"] in ("WIN", "LOSS", "DOE")
        and datetime.fromisoformat(s["created_at"].replace("Z", "+00:00")) >= cutoff
    ]

    total  = len(closed)
    wins   = sum(1 for s in closed if s["result"] == "WIN")
    losses = sum(1 for s in closed if s["result"] == "LOSS")
    does   = sum(1 for s in closed if s["result"] == "DOE")
    win_rate = (wins / total * 100) if total else 0.0

    by_zone = {}
    for s in closed:
        zone = s.get("killzone") or "N/A"
        if zone not in by_zone:
            by_zone[zone] = {"total": 0, "wins": 0, "losses": 0}
        by_zone[zone]["total"] += 1
        if s["result"] == "WIN":   by_zone[zone]["wins"]   += 1
        elif s["result"] == "LOSS": by_zone[zone]["losses"] += 1
    for d in by_zone.values():
        d["win_rate"] = round(d["wins"] / d["total"] * 100, 1) if d["total"] else 0

    by_hour = {}
    for s in closed:
        hour = datetime.fromisoformat(s["created_at"].replace("Z", "+00:00")).hour
        if hour not in by_hour:
            by_hour[hour] = {"total": 0, "wins": 0}
        by_hour[hour]["total"] += 1
        if s["result"] == "WIN": by_hour[hour]["wins"] += 1
    for d in by_hour.values():
        d["win_rate"] = round(d["wins"] / d["total"] * 100, 1) if d["total"] else 0

    cur_loss = cur_win = max_win_streak = max_loss_streak = 0
    current_streak = {"type": None, "count": 0}
    for s in closed:
        if s["result"] == "WIN":
            cur_win += 1; cur_loss = 0
            max_win_streak = max(max_win_streak, cur_win)
        elif s["result"] == "LOSS":
            cur_loss += 1; cur_win = 0
            max_loss_streak = max(max_loss_streak, cur_loss)
    if cur_win > 0:   current_streak = {"type": "WIN",  "count": cur_win}
    elif cur_loss > 0: current_streak = {"type": "LOSS", "count": cur_loss}

    now = datetime.now(timezone.utc)
    pending_expired = [
        s for s in signals
        if s["result"] == "PENDING"
        and datetime.fromisoformat(s["expires_at"].replace("Z", "+00:00")) < now
    ]

    return {
        "period_days": days,
        "total_closed": total,
        "wins": wins, "losses": losses, "does": does,
        "win_rate": round(win_rate, 2),
        "break_even_at_80_payout": 55.56,
        "above_break_even": win_rate > 55.56,
        "current_streak": current_streak,
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
        "by_killzone": by_zone,
        "by_hour_utc": by_hour,
        "pending_expired_count": len(pending_expired),
        "total_signals_all_time": len(signals),
    }
