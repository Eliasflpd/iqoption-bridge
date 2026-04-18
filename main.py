from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from iqoptionapi.api import IQOptionAPI
import os, time, threading, logging
import requests as req_lib
from datetime import datetime, timedelta, timezone
import pytz
import pandas as pd

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


app = FastAPI(title="IQ Option Bridge API v2")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


iq: IQOptionAPI = None
connected = False
connect_lock = threading.Lock()


def do_connect():
    global iq, connected
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
            logger.info("IQ Option conectado (PRACTICE)")
            return True, "ok"
        else:
            connected = False
            logger.error(f"Falha na conexao: {reason}")
            return False, str(reason)
    except Exception as e:
        connected = False
        logger.error(f"Excecao na conexao: {e}")
        return False, str(e)


@app.on_event("startup")
def startup():
    threading.Thread(target=do_connect, daemon=True).start()


def keepalive():
    while True:
        time.sleep(90)
        global connected
        if iq:
            try:
                if not iq.check_connect():
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

    # Dead zone — NY Lunch: bloquear explicitamente
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
    """Busca eventos economicos do Finnhub com cache de 15 min."""
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
    """
    Retorna (True, descricao) se houver news HIGH em GBP ou USD
    dentro da janela: -after_min ... +before_min em relacao ao horario atual (UTC).
    Fail-open: se FINNHUB_API_KEY ausente, retorna (False, '').
    """
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
# Filtro #3 — Volatilidade real via ATR (substitui volume OTC/proxy)
# ---------------------------------------------------------------------------

def volatility_ok(raw: list, multiplier: float = 1.2, period: int = 14) -> tuple:
    """
    Retorna (ok: bool, atr_ratio: float).
    ATR atual > multiplier * ATR_medio(period) = volatilidade suficiente.
    Substitui o filtro de volume (que e proxy/zerado em forex OTC).
    """
    if len(raw) < period + 1:
        return False, 0.0
    highs  = [c["max"]   for c in raw]
    lows   = [c["min"]   for c in raw]
    closes = [c["close"] for c in raw]

    trs = []
    for i in range(1, len(raw)):
        hl  = highs[i]  - lows[i]
        hc  = abs(highs[i]  - closes[i-1])
        lc  = abs(lows[i]   - closes[i-1])
        trs.append(max(hl, hc, lc))

    if len(trs) < period:
        return False, 0.0

    avg_atr = sum(trs[-period:]) / period
    current_tr = trs[-1]

    if avg_atr == 0:
        return False, 0.0

    ratio = current_tr / avg_atr
    return ratio >= multiplier, round(ratio, 4)


# ---------------------------------------------------------------------------
# Endpoints FastAPI
# ---------------------------------------------------------------------------

@app.get("/")
def root():
    return {"service":"IQ Option Bridge v2","connected":connected,"account":"PRACTICE",
            "endpoints":["/health","/balance","/candles/{asset}/{dur}/{qtd}","/price/{asset}","/payout/{asset}","/analyze/{asset}/{dur}","/assets/open","/killzone","/news-check"]}

@app.get("/health")
def health():
    return {"ok":True,"connected":connected,"account":"PRACTICE"}

@app.get("/connect")
def reconnect():
    ok,reason = do_connect()
    return {"connected":ok,"reason":str(reason)}

@app.get("/balance")
def balance():
    chk()
    try: return {"balance":iq.get_balance(),"currency":iq.get_currency(),"account":"PRACTICE"}
    except Exception as e: raise HTTPException(500,str(e))

@app.get("/candles/{asset}/{duracao_seg}/{quantidade}")
def candles(asset:str, duracao_seg:int, quantidade:int=50):
    chk()
    try:
        raw = iq.get_candles(asset, duracao_seg, min(quantidade,500), time.time())
        if not raw: raise HTTPException(404,f"Sem dados para {asset}")
        return {"asset":asset,"duracao_seg":duracao_seg,"total":len(raw),
                "candles":[{"time":c["from"]*1000,"open":c["open"],"high":c["max"],"low":c["min"],"close":c["close"],"volume":c.get("volume",0)} for c in raw]}
    except HTTPException: raise
    except Exception as e: raise HTTPException(500,str(e))

@app.get("/price/{asset}")
def price(asset:str):
    chk()
    try:
        raw = iq.get_candles(asset,60,1,time.time())
        if not raw: raise HTTPException(404,f"Sem dados para {asset}")
        return {"asset":asset,"price":raw[-1]["close"],"time":raw[-1]["from"]*1000}
    except HTTPException: raise
    except Exception as e: raise HTTPException(500,str(e))

@app.get("/payout/{asset}")
def payout(asset:str):
    chk()
    try:
        a = iq.get_all_open_time().get("binary",{}).get(asset)
        if not a: raise HTTPException(404,f"{asset} nao encontrado")
        p = a.get("profit",{})
        return {"asset":asset,"open":a.get("open",False),"payout_1min":p.get("1min",{}).get("value",0),"payout_5min":p.get("5min",{}).get("value",0)}
    except HTTPException: raise
    except Exception as e: raise HTTPException(500,str(e))

@app.get("/assets/open")
def open_assets():
    chk()
    try:
        all_a = iq.get_all_open_time()
        return {"binary":sorted(k for k,v in all_a.get("binary",{}).items() if v.get("open")),
                "digital":sorted(k for k,v in all_a.get("digital",{}).items() if v.get("open"))}
    except Exception as e: raise HTTPException(500,str(e))

@app.get("/killzone")
def killzone_status():
    """Retorna o status atual da killzone ICT."""
    active, zone = is_killzone_active()
    return {"active": active, "zone": zone}

@app.get("/news-check")
def news_check():
    """Verifica se ha news de alto impacto em GBP/USD proximas."""
    blocked, desc = has_high_impact_news()
    return {"blocked": blocked, "news": desc}

@app.get("/analyze/{asset}/{duracao_seg}")
def analyze(asset:str, duracao_seg:int):
    # --- Filtro #1: Killzone ICT ---
    kz_active, kz_zone = is_killzone_active()
    if not kz_active:
        logger.info(f"Bloqueado ({kz_zone}) — aguardando killzone")
        return {
            "blocked": True,
            "reason": f"Fora de killzone: {kz_zone}",
            "killzone": kz_zone
        }
    logger.info(f"Killzone ativa: {kz_zone}")

    # --- Filtro #2: News de alto impacto ---
    has_news, news_desc = has_high_impact_news()
    if has_news:
        logger.warning(f"NEWS BLOCK: {news_desc}")
        return {
            "blocked": True,
            "reason": f"News de alto impacto: {news_desc}",
            "killzone": kz_zone
        }

    chk()
    try:
        raw = iq.get_candles(asset, duracao_seg, 100, time.time())
        if not raw or len(raw)<15: raise HTTPException(404,"Dados insuficientes")
        closes = [c["close"] for c in raw]
        e9=calc_ema(closes,9); e21=calc_ema(closes,21); e50=calc_ema(closes,50)
        lc=closes[-1]
        trend = "alta" if e9 and e21 and e9>e21 and lc>e9 else "baixa" if e9 and e21 and e9<e21 and lc<e9 else "lateral"
        last=raw[-1]

        # --- Filtro #3: Volatilidade ATR (substitui volume OTC/proxy) ---
        # --- Confluencia 4-de-8: padrao + volatilidade(ATR) obrigatorios + 2 de 6 opcionais ---
        vol_ok, atr_ratio = volatility_ok(raw)
        if not vol_ok:
            logger.info(f"Volatilidade insuficiente para {asset} (ATR ratio={atr_ratio})")

        return {"asset":asset,"duracao_seg":duracao_seg,"preco_atual":lc,
                "killzone": kz_zone,
                "vela_atual":{"open":last["open"],"high":last["max"],"low":last["min"],"close":last["close"]},
                "rsi14":calc_rsi(closes),"ema9":e9,"ema21":e21,"ema50":e50,
                "bollinger":calc_boll(closes),"tendencia":trend,
                "padrao_vela":detect_pattern(raw),
                "volatilidade_atr":{"ok":vol_ok,"ratio":atr_ratio},
                "blocked": False}
    except HTTPException: raise
    except Exception as e: raise HTTPException(500,str(e))
