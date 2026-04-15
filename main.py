from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from iqoptionapi.api import IQOptionAPI
import os, time, threading, logging

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
            logger.info("Conectado - conta PRACTICE")
        else:
            connected = False
        return check, reason
    except Exception as e:
        connected = False
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

def calc_ema(data, p):
    if len(data) < p: return None
    k = 2/(p+1); ema = sum(data[:p])/p
    for v in data[p:]: ema = v*k + ema*(1-k)
    return round(ema, 6)

def calc_rsi(closes, period=14):
    if len(closes) < period+1: return None
    diffs = [closes[i]-closes[i-1] for i in range(1,len(closes))]
    g = sum(d for d in diffs[:period] if d>0)/period
    l = sum(abs(d) for d in diffs[:period] if d<0)/period
    for d in diffs[period:]:
        g = (g*(period-1)+max(0,d))/period
        l = (l*(period-1)+max(0,-d))/period
    return round(100-100/(1+g/l),1) if l else 100.0

def calc_boll(closes, p=20):
    if len(closes)<p: return None
    sma = sum(closes[-p:])/p
    std = (sum((v-sma)**2 for v in closes[-p:])/p)**0.5
    u,l = sma+2*std, sma-2*std
    pct = (closes[-1]-l)/(u-l)*100 if u!=l else 50
    return {"upper":round(u,6),"mid":round(sma,6),"lower":round(l,6),"pct_b":round(pct,1),"squeeze":(u-l)/sma*100<2}

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

@app.get("/")
def root():
    return {"service":"IQ Option Bridge v2","connected":connected,"account":"PRACTICE",
            "endpoints":["/health","/balance","/candles/{asset}/{dur}/{qtd}","/price/{asset}","/payout/{asset}","/analyze/{asset}/{dur}","/assets/open"]}

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
        if not raw: raise HTTPException(404,"Sem dados")
        c = raw[-1]
        return {"asset":asset,"price":c["close"],"open":c["open"],"high":c["max"],"low":c["min"],"time":c["from"]*1000}
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

@app.get("/analyze/{asset}/{duracao_seg}")
def analyze(asset:str, duracao_seg:int):
    chk()
    try:
        raw = iq.get_candles(asset, duracao_seg, 100, time.time())
        if not raw or len(raw)<15: raise HTTPException(404,"Dados insuficientes")
        closes = [c["close"] for c in raw]
        e9=calc_ema(closes,9); e21=calc_ema(closes,21); e50=calc_ema(closes,50)
        lc=closes[-1]
        trend = "alta" if e9 and e21 and e9>e21 and lc>e9 else "baixa" if e9 and e21 and e9<e21 and lc<e9 else "lateral"
        last=raw[-1]
        return {"asset":asset,"duracao_seg":duracao_seg,"preco_atual":lc,
                "vela_atual":{"open":last["open"],"high":last["max"],"low":last["min"],"close":last["close"]},
                "rsi14":calc_rsi(closes),"ema9":e9,"ema21":e21,"ema50":e50,
                "bollinger":calc_boll(closes),"tendencia":trend,
                "padrao_vela":detect_pattern(raw),"num_velas":len(raw),"timestamp_ms":int(time.time()*1000)}
    except HTTPException: raise
    except Exception as e: raise HTTPException(500,str(e))
