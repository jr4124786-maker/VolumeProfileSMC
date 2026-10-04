# -*- coding: utf-8 -*-
"""
Bot de sinais Forex/BTC — Volume Profile + Smart Money Concepts (SMC)
----------------------------------------------------------------------
Roda de graça no GitHub Actions (agendado), sem precisar de PC ou VPS.

O QUE ELE FAZ, A CADA EXECUÇÃO:
1. Busca candles de EUR/USD, GBP/USD, USD/JPY e BTC via Twelve Data
   (plano grátis, funciona de qualquer país, uma chave só para os 4).
2. Calcula o Volume Profile da última sessão FECHADA:
   - Forex: janela rolante das 18h de um dia até as 18h do dia seguinte.
   - BTC: janela rolante de 00h até 00h (meia-noite a meia-noite).
   -> Disso saem 3 níveis (POC, VAH, VAL) e onde a sessão fechou.
3. Analisa a estrutura de preço (topos/fundos, BOS/CHOCH) e procura
   order blocks, Fair Value Gaps (FVG) e candles de rejeição/engolfo.
4. Aplica as 3 estratégias clássicas de Volume Profile, cada uma com
   sua condição de validade (igual ao guia):
   - Repique no POC: só vale se a sessão anterior fechou FORA da área
     de valor; confirma com rejeição/order block/FVG no POC.
   - Reversão na Área de Valor: só vale se a sessão anterior fechou
     DENTRO da área de valor; exige candle fechando de volta pra
     dentro (pavio cruzando não conta) + CHOCH.
   - Rompimento: rompeu a área, fez pullback ficando perto dela, e
     confirmou com BOS na direção do rompimento.
   Manda um alerta no Telegram por setup/par/dia (usa state.json para
   não repetir o mesmo aviso a cada 15 minutos).

O QUE VOCÊ PODE AJUSTAR SEM PROGRAMAR (procure "AJUSTE AQUI"):
- PAIRS_CONFIG: pares, fonte de dados e horário da sessão de cada um.
- NUM_BINS / VALUE_AREA_PCT: precisão do Volume Profile.
- SWING_LEFT / SWING_RIGHT: sensibilidade da detecção de topos/fundos.
"""

import os
import json
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

# =========================================================================
# CONFIGURAÇÃO — AJUSTE AQUI
# =========================================================================

PAIRS_CONFIG = {
    "EUR_USD": {"source": "twelvedata", "td_symbol": "EUR/USD", "session_hour": 18, "tz": "America/New_York"},
    "GBP_USD": {"source": "twelvedata", "td_symbol": "GBP/USD", "session_hour": 18, "tz": "America/New_York"},
    "USD_JPY": {"source": "twelvedata", "td_symbol": "USD/JPY", "session_hour": 18, "tz": "America/New_York"},
    "BTC_USD": {"source": "twelvedata", "td_symbol": "BTC/USD", "exchange": "Binance", "session_hour": 0, "tz": "UTC"},
}

# Por que "America/New_York" e não "UTC": o fechamento/abertura do dia no forex
# é tradicionalmente marcado pelas 17h-18h de Nova York (quando a sessão de NY
# fecha e a de Sydney está para abrir). Usando esse fuso (em vez de um UTC fixo),
# a janela do Volume Profile acompanha automaticamente o horário de verão dos
# EUA (troca em março/novembro) sem precisar de nenhum ajuste manual — é
# justamente essa troca de 1h que muda os horários de Londres/Nova York/Sydney
# vistos a partir do Brasil (que não tem mais horário de verão desde 2019).
# BTC continua em UTC porque não fecha nunca; não é afetado por DST de mercado.
INTERVAL_TWELVEDATA = "15min"  # velas de 15 minutos
INTERVAL_BINANCE = "15m"
CANDLE_COUNT = 700          # ~7 dias de velas de 15 min — margem para pular fins de semana/feriados

NUM_BINS = 50               # nº de "fatias" de preço no Volume Profile
VALUE_AREA_PCT = 0.70       # 70% do volume define a área de valor (VAH/VAL)

# Fuso horário só para exibir o horário do sinal na mensagem do Telegram
# (não afeta o cálculo do Volume Profile). Troque se não estiver no Brasil.
DISPLAY_TZ = ZoneInfo("America/Sao_Paulo")

SWING_LEFT = 2              # velas à esquerda para confirmar um topo/fundo
SWING_RIGHT = 2             # velas à direita para confirmar um topo/fundo

# Tamanho do pip por par — usado pelo filtro de ruído de fim de semana
# (candle_tem_negociacao). BTC não tem pip, usa um limite relativo ao preço.
PIP_SIZE = {
    "EUR_USD": 0.0001,
    "GBP_USD": 0.0001,
    "USD_JPY": 0.01,
}

STATE_FILE = "state.json"

# =========================================================================
# BUSCA DE DADOS
# =========================================================================

def fetch_twelvedata_candles(symbol, exchange=None):
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
    if exchange:
        params["exchange"] = exchange
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


def fetch_com_retry(fetch_fn, *args, tentativas=2, espera_segundos=75, **kwargs):
    """Tenta buscar os candles até `tentativas` vezes, com uma pausa entre
    elas, pra pegar o candle de 15 min recém-fechado o mais rápido possível.
    Cobre dois motivos comuns do candle mais novo ainda não estar disponível
    na hora exata em que o job dispara: a Twelve Data leva alguns segundos
    pra publicar o candle, e o agendamento do GitHub Actions às vezes atrasa
    um pouco (comportamento documentado deles, não é bug)."""
    candles = fetch_fn(*args, **kwargs)
    for _ in range(tentativas - 1):
        agora = datetime.now(timezone.utc)
        if candles and (agora - candles[-1]["time"]) <= timedelta(minutes=16):
            break  # candle mais recente já veio, não precisa esperar mais
        time.sleep(espera_segundos)
        candles = fetch_fn(*args, **kwargs)
    return candles


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


def candle_tem_negociacao(c, pair_name=None):
    """Com o mercado fechado (fim de semana/feriado), várias fontes de dado forex
    continuam emitindo candles de 15 em 15 min com uma cotação quase parada —
    às vezes perfeitamente flat (abertura = máxima = mínima = fechamento), às
    vezes com um ruído mínimo de menos de 1 pip. Por isso não basta checar
    "teve alguma variação": exige-se uma amplitude mínima (2 pips no par, ou
    ~0.01% do preço quando não há pip definido, como no BTC) para considerar
    que o candle teve negociação real."""
    pip = PIP_SIZE.get(pair_name)
    limite = pip * 2 if pip else c["close"] * 0.0001
    return (c["high"] - c["low"]) >= limite


def dentro_do_fechamento_semanal(t, session_hour):
    """True se o horário (UTC) cai dentro do fechamento semanal do forex:
    de sexta até domingo, no session_hour de Nova York — regra fixa da
    semana, vale todo fim de semana, sem depender de feriado nenhum."""
    ny_time = t.astimezone(ZoneInfo("America/New_York"))
    wd = ny_time.weekday()
    if wd == 5:  # sábado inteiro
        return True
    if wd == 4 and ny_time.hour >= session_hour:  # sexta, depois do fechamento
        return True
    if wd == 6 and ny_time.hour < session_hour:  # domingo, antes da reabertura
        return True
    return False


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

def get_session_window(candles, session_hour, tz_name, min_candles=8, max_lookback_days=7,
                        fecha_no_fim_de_semana=True):
    """Retorna (início, fim) em UTC da última sessão de 24h com negociação
    real. Começa pela janela "de calendário" mais recente (24h terminando
    no session_hour); se ela não tiver candles reais de mercado — ou cair
    no fechamento semanal do forex —, volta mais um dia e tenta de novo,
    até achar a última sessão em que o mercado realmente funcionou.

    fecha_no_fim_de_semana=True (forex): pula direto qualquer janela entre
    sexta 18h e domingo 18h (hora de Nova York) — o forex nunca opera nesse
    intervalo, então nem vale a pena checar os candles ali (evita depender
    de "ruído" de fim de semana de alguma fonte de dado ser maior ou menor
    que o esperado). Use False para mercados 24/7 como o BTC.

    min_candles é baixo de propósito (só ~2h de negociação já basta): o
    objetivo é distinguir "mercado global fechado" (feriados tipo Natal,
    Ano Novo — praticamente zero candles em qualquer fonte) de "só um
    feriado local" (outras praças continuam operando, ainda vem bastante
    candle) — sem precisar de uma lista de datas de feriado."""
    tz = ZoneInfo(tz_name)
    ny = ZoneInfo("America/New_York")
    now = datetime.now(tz)
    today_boundary = now.replace(hour=session_hour, minute=0, second=0, microsecond=0)
    session_end = today_boundary if now >= today_boundary else today_boundary - timedelta(days=1)

    session_start = session_end - timedelta(days=1)
    for _ in range(max_lookback_days):
        session_start = session_end - timedelta(days=1)
        if fecha_no_fim_de_semana and session_start.astimezone(ny).weekday() in (4, 5):
            # início da janela é sexta ou sábado (hora de NY) -> mercado fechado
            # o intervalo inteiro; nem checa candle, já pula pro dia anterior.
            session_end = session_start
            continue
        start_utc = session_start.astimezone(timezone.utc)
        end_utc = session_end.astimezone(timezone.utc)
        candles_na_janela = sum(1 for c in candles if start_utc <= c["time"] < end_utc)
        if candles_na_janela >= min_candles:
            return start_utc, end_utc
        session_end = session_start  # sessão vazia (fim de semana/feriado) — volta mais um dia

    # Não achou nenhuma janela com dado suficiente dentro do limite de busca;
    # devolve a mais antiga tentada mesmo assim (compute_volume_profile trata o caso vazio).
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
    session_close = session_candles[-1]["close"]  # fechamento da sessão anterior (define Estratégia 1 vs 2)
    return {"poc": poc_price, "vah": vah_price, "val": val_price, "session_close": session_close,
            "session_high": price_max, "session_low": price_min}


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


def is_rejection_candle(prev_candle, candle, direction):
    """Padrão de engolfo/rejeição, do jeito que o guia de Volume Profile descreve
    para confirmar o Repique no POC (candlestick de rejeição no nível)."""
    if direction == "alta":
        return (candle["close"] > candle["open"]
                and candle["close"] > prev_candle["open"]
                and candle["open"] <= prev_candle["close"])
    return (candle["close"] < candle["open"]
            and candle["close"] < prev_candle["open"]
            and candle["open"] >= prev_candle["close"])


# =========================================================================
# LÓGICA DE SINAL (confluência Volume Profile + SMC)
# =========================================================================

def candles_desde_ultima_reabertura(candles):
    """Evita falso BOS/CHOCH causado por gap de preço na reabertura do
    mercado (o preço pode "pular" ao voltar do fim de semana/feriado, sem
    ter havido negociação real cruzando os níveis no meio do caminho).
    Acha o último "buraco" de mais de 30 min entre candles consecutivos
    (esperado: 15 em 15 min) e devolve só os candles a partir dali —
    topo/fundo, BOS/CHOCH, order block e FVG passam a usar só dado
    contínuo, sem misturar com o lado de antes do gap."""
    limite = timedelta(minutes=30)
    corte = 0
    for i in range(1, len(candles)):
        if candles[i]["time"] - candles[i - 1]["time"] > limite:
            corte = i
    return candles[corte:]


def checar_rompimento_dia_anterior(candles, vp, pair_name, state, today_str):
    """Alerta independente dos outros: rompeu a máxima e/ou a mínima REAL da
    sessão anterior (não é VAH/VAL nem exige SMC) — os dois lados são
    checados separadamente, então pode disparar um, o outro, ou nenhum."""
    eventos = []
    if vp is None:
        return eventos
    price = candles[-1]["close"]
    session_high = vp["session_high"]
    session_low = vp["session_low"]
    par_fmt = pair_name.replace("_", "/")

    if price > session_high:
        key = f"{pair_name}|rompeu_maxima_dia_anterior|{today_str}"
        if not state.get(key):
            state[key] = True
            eventos.append(f"🔺 <b>{par_fmt}</b> rompeu a máxima do dia anterior ({session_high:.5f})\n"
                            f"Preço atual: {price:.5f}")

    if price < session_low:
        key = f"{pair_name}|rompeu_minima_dia_anterior|{today_str}"
        if not state.get(key):
            state[key] = True
            eventos.append(f"🔻 <b>{par_fmt}</b> rompeu a mínima do dia anterior ({session_low:.5f})\n"
                            f"Preço atual: {price:.5f}")

    return eventos


def generate_signal(candles, vp):
    if vp is None or len(candles) < 30:
        return None

    candles_recentes = candles_desde_ultima_reabertura(candles)
    if len(candles_recentes) < 10:
        return None  # muito perto da reabertura; sem histórico contínuo suficiente pra confirmar estrutura

    current = candles_recentes[-1]
    price = current["close"]
    poc, vah, val = vp["poc"], vp["vah"], vp["val"]
    session_close = vp["session_close"]
    fechou_dentro_da_area = val <= session_close <= vah  # onde a sessão anterior terminou

    swings = find_swings(candles_recentes)
    structure_event = detect_structure_event(candles_recentes, swings)

    poc_tolerance = (vah - val) * 0.05 if vah != val else price * 0.0005
    va_width = (vah - val) if vah > val else price * 0.001

    # ------------------------------------------------------------------
    # Estratégia 1 — Repique no POC
    # Só é válida se a SESSÃO ANTERIOR terminou FORA da Área de Valor.
    # ------------------------------------------------------------------
    if not fechou_dentro_da_area and abs(price - poc) <= poc_tolerance:
        direction = "alta" if current["close"] > current["open"] else "baixa"
        rejeicao = is_rejection_candle(candles_recentes[-2], candles_recentes[-1], direction)
        ob = find_order_block(candles_recentes, direction)
        fvgs = find_recent_fvgs(candles_recentes)
        confluencia = rejeicao or (ob and price_in_zone(price, ob)) or any(price_in_zone(price, f) for f in fvgs)
        if confluencia:
            return {"setup": "Repique no POC", "direction": direction,
                    "detalhe": f"Sessão anterior fechou fora da área de valor; preço testando o POC ({poc:.5f}) "
                               f"com confirmação de rejeição/order block/FVG."}

    # ------------------------------------------------------------------
    # Estratégia 2 — Reversão na Área de Valor
    # Só é válida se a SESSÃO ANTERIOR terminou DENTRO da Área de Valor.
    # Exige um candle fechando de volta para dentro (pavio cruzando não conta).
    # ------------------------------------------------------------------
    if fechou_dentro_da_area:
        candles_antes = candles_recentes[-10:-1]
        rompeu_topo = any(c["close"] > vah for c in candles_antes)
        rompeu_fundo = any(c["close"] < val for c in candles_antes)
        fechou_de_volta = val <= price <= vah
        if rompeu_topo and fechou_de_volta and structure_event == "CHOCH_BAIXA":
            return {"setup": "Reversão na Área de Valor", "direction": "baixa",
                    "detalhe": f"Sessão anterior fechou dentro da área de valor; preço saiu acima da VAH "
                               f"({vah:.5f}) e fechou de volta para dentro, com CHOCH de baixa."}
        if rompeu_fundo and fechou_de_volta and structure_event == "CHOCH_ALTA":
            return {"setup": "Reversão na Área de Valor", "direction": "alta",
                    "detalhe": f"Sessão anterior fechou dentro da área de valor; preço saiu abaixo da VAL "
                               f"({val:.5f}) e fechou de volta para dentro, com CHOCH de alta."}

    # ------------------------------------------------------------------
    # Estratégia 3 — Rompimento
    # Vale com a sessão anterior tendo fechado dentro ou fora da área.
    # Rompeu -> pullback ficando perto da área -> BOS na direção do rompimento.
    # ------------------------------------------------------------------
    candles_antes = candles_recentes[-12:-1]
    rompeu_para_cima = any(c["close"] > vah for c in candles_antes)
    rompeu_para_baixo = any(c["close"] < val for c in candles_antes)
    pullback_alta = rompeu_para_cima and vah < price <= vah + va_width * 0.3
    pullback_baixa = rompeu_para_baixo and val - va_width * 0.3 <= price < val
    if pullback_alta and structure_event == "BOS_ALTA":
        return {"setup": "Rompimento de alta", "direction": "alta",
                "detalhe": f"Rompeu a VAH ({vah:.5f}), fez pullback perto da área e confirmou com BOS de alta."}
    if pullback_baixa and structure_event == "BOS_BAIXA":
        return {"setup": "Rompimento de baixa", "direction": "baixa",
                "detalhe": f"Rompeu a VAL ({val:.5f}), fez pullback perto da área e confirmou com BOS de baixa."}

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
            state = json.load(f)
    else:
        state = {}
    return state


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
                candles = fetch_com_retry(fetch_twelvedata_candles, cfg["td_symbol"], cfg.get("exchange"))
            else:
                candles = fetch_binance_candles(cfg["binance_symbol"])

            if len(candles) < 30:
                print(f"{pair_name}: poucos candles retornados, pulando.")
                continue

            candles = [c for c in candles if candle_tem_negociacao(c, pair_name)]
            if len(candles) < 30:
                print(f"{pair_name}: mercado provavelmente fechado (só vieram candles sem negociação real), pulando.")
                continue

            if pair_name != "BTC_USD":
                candles = [c for c in candles if not dentro_do_fechamento_semanal(c["time"], cfg["session_hour"])]
                if len(candles) < 30:
                    print(f"{pair_name}: mercado fechado (fim de semana), pulando.")
                    continue

            session_start, session_end = get_session_window(
                candles, cfg["session_hour"], cfg["tz"],
                fecha_no_fim_de_semana=(pair_name != "BTC_USD"))
            vp = compute_volume_profile(candles, session_start, session_end)
            signal = generate_signal(candles, vp)

            def fmt(dt):
                return dt.astimezone(DISPLAY_TZ).strftime("%d/%m %H:%M")

            print(f"{pair_name}: perfil de volume de {fmt(session_start)} até {fmt(session_end)} "
                  f"| último candle recebido: {fmt(candles[-1]['time'])}")

            for msg_rompimento in checar_rompimento_dia_anterior(candles, vp, pair_name, state, today_str):
                send_telegram_message(msg_rompimento)
                print(f"{pair_name}: alerta de rompimento da máxima/mínima do dia anterior enviado.")

            if signal:
                key = f"{pair_name}|{signal['setup']}|{today_str}"
                if not state.get(key):
                    msg = (f"<b>{pair_name.replace('_', '/')}</b>\n"
                           f"Setup: {signal['setup']}\n"
                           f"{signal['detalhe']}\n"
                           f"Preço atual: {candles[-1]['close']}\n"
                           f"Horário do candle: {fmt(candles[-1]['time'])}\n"
                           f"Perfil de volume: {fmt(session_start)} até {fmt(session_end)}")
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
