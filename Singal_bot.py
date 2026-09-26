# -*- coding: utf-8 -*-
"""
Bot de sinais Forex/BTC — Volume Profile + Smart Money Concepts (SMC)
----------------------------------------------------------------------
Roda de graça no GitHub Actions (agendado), sem precisar de PC ou VPS.

O QUE ELE FAZ, A CADA EXECUÇÃO:
1. Busca candles de EUR/USD, GBP/USD, USD/JPY (via Twelve Data, plano grátis,
   funciona de qualquer país) e de BTC (via Binance, API pública sem chave).
2. Calcula o Volume Profile da última sessão FECHADA:
   - Forex: janela rolante das 18h de um dia até as 18h do dia seguinte.
   - BTC: janela rolante de 00h até 00h (meia-noite a meia-noite).
   -> Disso saem 3 níveis: POC, VAH e VAL.
3. Analisa a estrutura de preço (topos/fundos, BOS/CHOCH) e procura
   order blocks e Fair Value Gaps (FVG) recentes.
4. Se o preço atual encaixa em um dos 3 setups clássicos (Repique no POC,
   Reversão na Área de Valor, Rompimento) E há confluência com SMC,
   manda um alerta no Telegram — só uma vez por setup/dia (usa state.json
   para não repetir o mesmo aviso a cada 15 minutos).

O QUE VOCÊ PODE AJUSTAR SEM PROGRAMAR (procure "AJUSTE AQUI"):
- PAIRS_CONFIG: pares, fonte de dados e horário da sessão de cada um.
- NUM_BINS / VALUE_AREA_PCT: precisão do Volume Profile.
- SWING_LEFT / SWING_RIGHT: sensibilidade da detecção de topos/fundos.
"""

import os
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

# =========================================================================
# CONFIGURAÇÃO — AJUSTE AQUI
# =========================================================================

PAIRS_CONFIG = {
    "EUR_USD": {"source": "twelvedata", "td_symbol": "EUR/USD", "session_hour": 18, "tz": "UTC"},
    "GBP_USD": {"source": "twelvedata", "td_symbol": "GBP/USD", "session_hour": 18, "tz": "UTC"},
    "USD_JPY": {"source": "twelvedata", "td_symbol": "USD/JPY", "session_hour": 18, "tz": "UTC"},
    "BTC_USD": {"source": "binance", "binance_symbol": "BTCUSDT", "session_hour": 0, "tz": "UTC"},
}

# Se os horários das velas do seu MT5 (Exness/Pepperstone) estiverem
# 1 a 3 horas diferentes do esperado, troque "tz" acima pelo fuso do
# servidor do seu broker, ex: "Etc/GMT-2" ou "Etc/GMT-3".

INTERVAL_TWELVEDATA = "15min"  # velas de 15 minutos
INTERVAL_BINANCE = "15m"
CANDLE_COUNT = 300          # ~3 dias de velas de 15 min, suficiente p/ 2 sessões

NUM_BINS = 50               # nº de "fatias" de preço no Volume Profile
VALUE_AREA_PCT = 0.70       # 70% do volume define a área de valor (VAH/VAL)

SWING_LEFT = 2              # velas à esquerda para confirmar um topo/fundo
SWING_RIGHT = 2             # velas à direita para confirmar um topo/fundo

STATE_FILE = "state.json"

# =========================================================================
# BUSCA DE DADOS
# =========================================================================

def fetch_twelvedata_candles(symbol):
    api_key = os.environ["TWELVEDATA_API_KEY"]
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": INTERVAL_TWELVEDATA,
        "outputsize": CANDLE_COUNT,
        "timezone": "UTC",
        "order": "ASC",
        "apikey": api_key,
    }
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") == "error":
        raise RuntimeError(data.get("message", "erro desconhecido na Twelve Data"))

    candles = []
    for v in data.get("values", []):
        dt_str = v["datetime"].replace(" ", "T")
        candles.append({
            "time": datetime.fromisoformat(dt_str).replace(tzinfo=timezone.utc),
            "open": float(v["open"]),
            "high": float(v["high"]),
            "low": float(v["low"]),
            "close": float(v["close"]),
            "volume": float(v.get("volume") or 0),
        })
    return ensure_volume_proxy(candles)


def ensure_volume_proxy(candles):
    """Forex é OTC — muitas fontes grátis retornam volume 0 ou não trazem o campo.
    Nesse caso, usa a amplitude da vela (high - low) como proxy de "atividade"
    para o Volume Profile continuar fazendo sentido."""
    if not candles:
        return candles
    if sum(c["volume"] for c in candles) <= 0:
        for c in candles:
            c["volume"] = max(c["high"] - c["low"], 1e-9)
    return candles


def fetch_binance_candles(symbol):
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": INTERVAL_BINANCE, "limit": CANDLE_COUNT}
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    candles = []
    for k in data:
        candles.append({
            "time": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        })
    return candles


# =========================================================================
# VOLUME PROFILE
# =========================================================================

def get_session_window(session_hour, tz_name):
    """Retorna (inicio, fim) em UTC da última sessão de 24h já FECHADA."""
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    today_boundary = now.replace(hour=session_hour, minute=0, second=0, microsecond=0)
    session_end = today_boundary if now >= today_boundary else today_boundary - timedelta(days=1)
    session_start = session_end - timedelta(days=1)
    return session_start.astimezone(timezone.utc), session_end.astimezone(timezone.utc)


def compute_volume_profile(candles, session_start, session_end):
    session_candles = [c for c in candles if session_start <= c["time"] < session_end]
    if len(session_candles) < 5:
        return None

    price_max = max(c["high"] for c in session_candles)
    price_min = min(c["low"] for c in session_candles)
    if price_max <= price_min:
        return None

    bin_size = (price_max - price_min) / NUM_BINS
    bins = [0.0] * NUM_BINS
    for c in session_candles:
        typical_price = (c["high"] + c["low"] + c["close"]) / 3
        idx = int((typical_price - price_min) / bin_size)
        idx = min(max(idx, 0), NUM_BINS - 1)
        bins[idx] += c["volume"]

    total_volume = sum(bins)
    if total_volume == 0:
        return None

    poc_idx = bins.index(max(bins))
    poc_price = price_min + (poc_idx + 0.5) * bin_size

    included = {poc_idx}
    included_volume = bins[poc_idx]
    target = total_volume * VALUE_AREA_PCT
    left, right = poc_idx - 1, poc_idx + 1
    while included_volume < target and (left >= 0 or right < NUM_BINS):
        left_vol = bins[left] if left >= 0 else -1
        right_vol = bins[right] if right < NUM_BINS else -1
        if right_vol >= left_vol:
            included.add(right)
            included_volume += right_vol
            right += 1
        else:
            included.add(left)
            included_volume += left_vol
            left -= 1

    val_price = price_min + min(included) * bin_size
    vah_price = price_min + (max(included) + 1) * bin_size
    return {"poc": poc_price, "vah": vah_price, "val": val_price}


# =========================================================================
# SMART MONEY CONCEPTS (SMC)
# =========================================================================

def find_swings(candles):
    swings = []
    n = len(candles)
    for i in range(SWING_LEFT, n - SWING_RIGHT):
        window_high = [candles[j]["high"] for j in range(i - SWING_LEFT, i + SWING_RIGHT + 1)]
        window_low = [candles[j]["low"] for j in range(i - SWING_LEFT, i + SWING_RIGHT + 1)]
        if candles[i]["high"] == max(window_high) and window_high.count(candles[i]["high"]) == 1:
            swings.append({"index": i, "type": "high", "price": candles[i]["high"]})
        if candles[i]["low"] == min(window_low) and window_low.count(candles[i]["low"]) == 1:
            swings.append({"index": i, "type": "low", "price": candles[i]["low"]})
    return swings


def detect_structure_event(candles, swings):
    """Retorna 'BOS_ALTA', 'BOS_BAIXA', 'CHOCH_ALTA', 'CHOCH_BAIXA' ou None."""
    highs = [s for s in swings if s["type"] == "high"]
    lows = [s for s in swings if s["type"] == "low"]
    if not highs or not lows:
        return None

    trend = None
    if len(highs) >= 2 and len(lows) >= 2:
        if highs[-1]["price"] > highs[-2]["price"] and lows[-1]["price"] > lows[-2]["price"]:
            trend = "alta"
        elif highs[-1]["price"] < highs[-2]["price"] and lows[-1]["price"] < lows[-2]["price"]:
            trend = "baixa"

    current_close = candles[-1]["close"]
    if current_close > highs[-1]["price"]:
        return "CHOCH_ALTA" if trend == "baixa" else "BOS_ALTA"
    if current_close < lows[-1]["price"]:
        return "CHOCH_BAIXA" if trend == "alta" else "BOS_BAIXA"
    return None


def find_order_block(candles, direction):
    lookback = candles[-8:-1]
    for c in reversed(lookback):
        if direction == "alta" and c["close"] < c["open"]:
            return {"top": c["high"], "bottom": c["low"]}
        if direction == "baixa" and c["close"] > c["open"]:
            return {"top": c["high"], "bottom": c["low"]}
    return None


def find_recent_fvgs(candles, lookback=15):
    fvgs = []
    recent = candles[-lookback:]
    for i in range(2, len(recent)):
        c1, c3 = recent[i - 2], recent[i]
        if c1["high"] < c3["low"]:
            fvgs.append({"top": c3["low"], "bottom": c1["high"]})
        elif c1["low"] > c3["high"]:
            fvgs.append({"top": c1["low"], "bottom": c3["high"]})
    return fvgs


def price_in_zone(price, zone):
    top, bottom = zone["top"], zone["bottom"]
    margin = (top - bottom) * 0.15 if top != bottom else price * 0.0005
    return (bottom - margin) <= price <= (top + margin)


# =========================================================================
# LÓGICA DE SINAL (confluência Volume Profile + SMC)
# =========================================================================

def generate_signal(candles, vp):
    if vp is None or len(candles) < 30:
        return None

    current = candles[-1]
    price = current["close"]
    poc, vah, val = vp["poc"], vp["vah"], vp["val"]

    swings = find_swings(candles)
    structure_event = detect_structure_event(candles, swings)

    poc_tolerance = (vah - val) * 0.05 if vah != val else price * 0.0005

    # 1) Repique no POC
    if abs(price - poc) <= poc_tolerance:
        direction = "alta" if current["close"] > current["open"] else "baixa"
        ob = find_order_block(candles, direction)
        fvgs = find_recent_fvgs(candles)
        confluencia = (ob and price_in_zone(price, ob)) or any(price_in_zone(price, f) for f in fvgs)
        if confluencia:
            return {"setup": "Repique no POC",
                    "detalhe": f"Preço testando o POC ({poc:.5f}) com order block/FVG por perto."}

    # 2) Reversão na Área de Valor (varredura de liquidez + CHOCH)
    recent_highs = [c["high"] for c in candles[-5:]]
    recent_lows = [c["low"] for c in candles[-5:]]
    varreu_topo = max(recent_highs) > vah and price < vah
    varreu_fundo = min(recent_lows) < val and price > val
    if varreu_topo and structure_event == "CHOCH_BAIXA":
        return {"setup": "Reversão na Área de Valor",
                "detalhe": f"Varredura de liquidez acima da VAH ({vah:.5f}) seguida de CHOCH de baixa."}
    if varreu_fundo and structure_event == "CHOCH_ALTA":
        return {"setup": "Reversão na Área de Valor",
                "detalhe": f"Varredura de liquidez abaixo da VAL ({val:.5f}) seguida de CHOCH de alta."}

    # 3) Rompimento (continuação com BOS)
    if price > vah and structure_event == "BOS_ALTA":
        return {"setup": "Rompimento de alta",
                "detalhe": f"Fechamento acima da VAH ({vah:.5f}) com BOS de alta."}
    if price < val and structure_event == "BOS_BAIXA":
        return {"setup": "Rompimento de baixa",
                "detalhe": f"Fechamento abaixo da VAL ({val:.5f}) com BOS de baixa."}

    return None


# =========================================================================
# TELEGRAM
# =========================================================================

def send_telegram_message(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=20)
    resp.raise_for_status()


# =========================================================================
# ESTADO (evita repetir o mesmo alerta a cada 15 min)
# =========================================================================

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# =========================================================================
# EXECUÇÃO PRINCIPAL
# =========================================================================

def main():
    state = load_state()
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for pair_name, cfg in PAIRS_CONFIG.items():
        try:
            if cfg["source"] == "twelvedata":
                candles = fetch_twelvedata_candles(cfg["td_symbol"])
            else:
                candles = fetch_binance_candles(cfg["binance_symbol"])

            if len(candles) < 30:
                print(f"{pair_name}: poucos candles retornados, pulando.")
                continue

            session_start, session_end = get_session_window(cfg["session_hour"], cfg["tz"])
            vp = compute_volume_profile(candles, session_start, session_end)
            signal = generate_signal(candles, vp)

            if signal:
                key = f"{pair_name}|{signal['setup']}|{today_str}"
                if not state.get(key):
                    msg = (f"<b>{pair_name.replace('_', '/')}</b>\n"
                           f"Setup: {signal['setup']}\n"
                           f"{signal['detalhe']}\n"
                           f"Preço atual: {candles[-1]['close']}")
                    send_telegram_message(msg)
                    state[key] = True
                    print(f"{pair_name}: alerta enviado ({signal['setup']}).")
                else:
                    print(f"{pair_name}: sinal ativo mas já avisado hoje.")
            else:
                print(f"{pair_name}: sem sinal nesta checagem.")

        except Exception as e:
            print(f"Erro ao processar {pair_name}: {e}")

    save_state(state)


if __name__ == "__main__":
    main()
