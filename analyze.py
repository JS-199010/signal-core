"""
Signal Core — GitHub Actions 自動分析腳本
每4小時執行：Binance K線 → 技術指標 + SMC → Claude 分析 → EmailJS 寄信
"""

import os, json, math, requests
from datetime import datetime, timezone

# ── 設定 ───────────────────────────────────────────────
CLAUDE_KEY          = os.environ['CLAUDE_API_KEY']
EMAIL_TO            = os.environ['EMAIL_TO']
EMAILJS_SERVICE_ID  = os.environ['EMAILJS_SERVICE_ID']
EMAILJS_TEMPLATE_ID = os.environ['EMAILJS_TEMPLATE_ID']
EMAILJS_PUBLIC_KEY  = os.environ['EMAILJS_PUBLIC_KEY']

SYMBOLS    = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']
TIMEFRAME  = '4h'
TF2        = '1d'
LIMIT      = 300

# ── Binance ────────────────────────────────────────────
def fetch_klines(symbol, interval, limit=300):
    url = f'https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}'
    data = requests.get(url, timeout=10).json()
    return [{'t': k[0], 'o': float(k[1]), 'h': float(k[2]),
             'l': float(k[3]), 'c': float(k[4]), 'v': float(k[5])} for k in data]

def fetch_price(symbol):
    url = f'https://api.binance.com/api/v3/ticker/price?symbol={symbol}'
    return float(requests.get(url, timeout=5).json()['price'])

# ── 傳統指標 ───────────────────────────────────────────
def calc_sma(closes, p):
    return [sum(closes[i-p+1:i+1])/p if i >= p-1 else None for i in range(len(closes))]

def calc_ema(closes, p):
    k = 2/(p+1); r = []
    for i, v in enumerate(closes):
        r.append(v if i == 0 else v*k + r[-1]*(1-k))
    return r

def calc_rsi(closes, p=14):
    result = []
    for i in range(len(closes)):
        if i < p:
            result.append(None); continue
        gains = losses = 0
        for j in range(i-p+1, i+1):
            d = closes[j] - closes[j-1]
            if d > 0: gains += d
            else: losses += abs(d)
        rs = gains / (losses or 0.0001)
        result.append(round(100 - 100/(1+rs), 2))
    return result

def calc_macd(closes):
    e12 = calc_ema(closes, 12)
    e26 = calc_ema(closes, 26)
    macd = [round(a-b, 6) for a, b in zip(e12, e26)]
    signal = calc_ema(macd[-26:], 9)
    hist = round(macd[-1] - signal[-1], 6)
    return {'macd': round(macd[-1], 4), 'signal': round(signal[-1], 4), 'histogram': hist}

def calc_bb(closes, p=20):
    last = closes[-p:]
    mean = sum(last)/p
    std = math.sqrt(sum((x-mean)**2 for x in last)/p)
    return {'upper': round(mean+2*std, 2), 'middle': round(mean, 2), 'lower': round(mean-2*std, 2)}

def calc_atr(klines, p=14):
    trs = [max(k['h']-k['l'], abs(k['h']-klines[i]['c']), abs(k['l']-klines[i]['c']))
           for i, k in enumerate(klines[1:], 0)]
    return round(sum(trs[-p:])/p, 4)

def vol_ratio(klines):
    avg = sum(k['v'] for k in klines[-10:])/10
    return round(klines[-1]['v']/avg, 2)

# ── SMC ────────────────────────────────────────────────
def find_swings(klines, lb=5):
    highs, lows = [], []
    for i in range(lb, len(klines)-lb):
        if all(klines[j]['h'] <= klines[i]['h'] for j in range(i-lb,i)) and \
           all(klines[j]['h'] <= klines[i]['h'] for j in range(i+1,i+lb+1)):
            highs.append({'i':i, 'price':klines[i]['h']})
        if all(klines[j]['l'] >= klines[i]['l'] for j in range(i-lb,i)) and \
           all(klines[j]['l'] >= klines[i]['l'] for j in range(i+1,i+lb+1)):
            lows.append({'i':i, 'price':klines[i]['l']})
    return highs, lows

def detect_structure(klines, highs, lows):
    price = klines[-1]['c']
    bias = 'NEUTRAL'; last_bos = None; last_choch = None
    rh = highs[-4:]; rl = lows[-4:]

    for i in range(1, len(rh)):
        if rh[i]['price'] > rh[i-1]['price']:
            bias = 'BULLISH'
            last_bos = f"BOS向上 @ ${rh[i]['price']:.2f}（{len(klines)-rh[i]['i']-1}根前）"
    for i in range(1, len(rl)):
        if rl[i]['price'] < rl[i-1]['price']:
            bias = 'BEARISH'
            last_bos = f"BOS向下 @ ${rl[i]['price']:.2f}（{len(klines)-rl[i]['i']-1}根前）"

    if bias == 'BULLISH' and rl:
        last = rl[-1]
        if price < last['price']:
            last_choch = f"CHoCH看空 @ ${last['price']:.2f}"
    if bias == 'BEARISH' and rh:
        last = rh[-1]
        if price > last['price']:
            last_choch = f"CHoCH看多 @ ${last['price']:.2f}"

    return bias, last_bos, last_choch

def find_obs(klines, max_obs=3):
    price = klines[-1]['c']
    bull_obs, bear_obs = [], []

    for i in range(5, len(klines)-3):
        c = klines[i]
        if c['c'] < c['o']:  # bearish candle → bullish OB
            imp = klines[i+1:i+4]
            if all(k['c']>k['o'] for k in imp) and imp[-1]['h'] > c['h']*1.002 and c['l'] < price:
                bull_obs.append(f"${c['l']:.2f}-${c['h']:.2f}（{len(klines)-i-1}根前）")
        if c['c'] > c['o']:  # bullish candle → bearish OB
            imp = klines[i+1:i+4]
            if all(k['c']<k['o'] for k in imp) and imp[-1]['l'] < c['l']*0.998 and c['h'] > price:
                bear_obs.append(f"${c['l']:.2f}-${c['h']:.2f}（{len(klines)-i-1}根前）")

    return bull_obs[-max_obs:], bear_obs[-max_obs:]

def find_fvgs(klines, max_fvgs=3):
    price = klines[-1]['c']
    bull, bear = [], []
    for i in range(1, len(klines)-1):
        p, n = klines[i-1], klines[i+1]
        if n['l'] > p['h'] and price > p['h']:
            bull.append(f"${p['h']:.2f}-${n['l']:.2f}（{len(klines)-i-1}根前）")
        if n['h'] < p['l'] and price < p['l']:
            bear.append(f"${n['h']:.2f}-${p['l']:.2f}（{len(klines)-i-1}根前）")
    return bull[-max_fvgs:], bear[-max_fvgs:]

def calc_pd(klines, highs, lows):
    if not highs or not lows: return ''
    hi, lo = highs[-1]['price'], lows[-1]['price']
    eq = (hi+lo)/2
    price = klines[-1]['c']
    zone = 'PREMIUM（偏貴）' if price > eq else 'DISCOUNT（偏便宜）'
    return f"EQ=${eq:.2f} | 現價{zone}"

def find_liquidity(highs, lows):
    tol = 0.002
    bsl = set(); ssl = set()
    for i in range(len(highs)-1):
        for j in range(i+1, len(highs)):
            if abs(highs[i]['price']-highs[j]['price'])/highs[i]['price'] < tol:
                bsl.add(round((highs[i]['price']+highs[j]['price'])/2, 2))
    for i in range(len(lows)-1):
        for j in range(i+1, len(lows)):
            if abs(lows[i]['price']-lows[j]['price'])/lows[i]['price'] < tol:
                ssl.add(round((lows[i]['price']+lows[j]['price'])/2, 2))
    return list(bsl)[-2:], list(ssl)[-2:]

def build_smc_text(klines):
    highs, lows = find_swings(klines)
    bias, bos, choch = detect_structure(klines, highs, lows)
    bull_obs, bear_obs = find_obs(klines)
    bull_fvg, bear_fvg = find_fvgs(klines)
    pd = calc_pd(klines, highs, lows)
    bsl, ssl = find_liquidity(highs, lows)

    lines = [f"市場結構: {bias}"]
    if bos:   lines.append(f"最後BOS: {bos}")
    if choch: lines.append(f"CHoCH: {choch}")
    if bull_obs: lines.append(f"多方OB: {', '.join(bull_obs)}")
    if bear_obs: lines.append(f"空方OB: {', '.join(bear_obs)}")
    if bull_fvg: lines.append(f"看多FVG: {', '.join(bull_fvg)}")
    if bear_fvg: lines.append(f"看空FVG: {', '.join(bear_fvg)}")
    if pd: lines.append(f"P/D Zone: {pd}")
    if bsl: lines.append(f"BSL流動性: ${', $'.join(map(str,bsl))}")
    if ssl: lines.append(f"SSL流動性: ${', $'.join(map(str,ssl))}")
    return '\n'.join(lines)

# ── 建立指標文字 ────────────────────────────────────────
def build_indicator_text(klines, tf):
    closes = [k['c'] for k in klines]
    rsi = calc_rsi(closes)
    macd = calc_macd(closes)
    bb = calc_bb(closes)
    sma20 = calc_sma(closes, 20)[-1]
    ema50 = calc_ema(closes, 50)[-1]
    ema200 = calc_ema(closes, 200)[-1]
    atr = calc_atr(klines)
    vr = vol_ratio(klines)
    smc = build_smc_text(klines)
    recent = klines[-5:]

    ind = (f"RSI(14)={rsi[-1]} | SMA20={sma20:.2f} | EMA50={ema50:.2f} | EMA200={ema200:.2f}\n"
           f"MACD={macd['macd']} Signal={macd['signal']} Hist={macd['histogram']}\n"
           f"BB 上={bb['upper']} 中={bb['middle']} 下={bb['lower']}\n"
           f"ATR={atr} | 量比={vr}x")

    candles = ' | '.join(f"O:{k['o']} H:{k['h']} L:{k['l']} C:{k['c']}" for k in recent)

    tf_name = {'4h':'4小時','1d':'日線','1h':'1小時','15m':'15分鐘'}.get(tf, tf)
    return f"【{tf_name} 指標】\n{ind}\n近5根K棒：{candles}\n\n【{tf_name} SMC結構】\n{smc}"

# ── Claude 分析 ─────────────────────────────────────────
def analyze_with_claude(symbol, price, tf1_text, tf2_text):
    prompt = f"""你是一位專業的加密貨幣合約交易分析師，精通 SMC（Smart Money Concepts）與多時間框架共振分析。

幣種：{symbol}｜當前價格：${price}

━━━ 短週期（入場時機）━━━
{tf1_text}

━━━ 長週期（趨勢方向）━━━
{tf2_text}

━━━ 分析框架 ━━━
SMC 優先邏輯：
1. 長週期結構偏向決定方向
2. Discount 區多方OB/看多FVG做多；Premium 區空方OB/看空FVG做空
3. 止損放 OB 外側，TP1 瞄準流動性（BSL/SSL），TP2 對向OB
4. 兩週期 SMC 共振才給高信心，分歧給 NEUTRAL
5. 傳統指標（RSI/MACD）輔助確認

只回傳 JSON，不要任何其他文字：
{{"signal":"LONG|SHORT|NEUTRAL","confidence":0-100,"entry":數字,"stopLoss":數字,"takeProfit1":數字,"takeProfit2":數字,"leverage":1-20,"tfAlignment":"ALIGNED|DIVERGED","smcBias":"看多OB回測|看空OB反壓|FVG填補|流動性獵殺|結構突破|觀望","summary":"70字內中文摘要","risk":"LOW|MEDIUM|HIGH"}}"""

    res = requests.post(
        'https://api.anthropic.com/v1/messages',
        headers={
            'x-api-key': CLAUDE_KEY,
            'anthropic-version': '2023-06-01',
            'Content-Type': 'application/json'
        },
        json={
            'model': 'claude-sonnet-4-20250514',
            'max_tokens': 700,
            'messages': [{'role': 'user', 'content': prompt}]
        },
        timeout=30
    )
    res.raise_for_status()
    text = res.json()['content'][0]['text'].strip()
    text = text.replace('```json','').replace('```','').strip()
    return json.loads(text)

# ── EmailJS 寄信 ────────────────────────────────────────
def send_email(subject, body):
    res = requests.post(
        'https://api.emailjs.com/api/v1.0/email/send',
        json={
            'service_id':  EMAILJS_SERVICE_ID,
            'template_id': EMAILJS_TEMPLATE_ID,
            'user_id':     EMAILJS_PUBLIC_KEY,
            'template_params': {
                'to_email': EMAIL_TO,
                'subject':  subject,
                'message':  body
            }
        },
        timeout=10
    )
    return res.status_code == 200

# ── 主程式 ─────────────────────────────────────────────
def main():
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    print(f"\n{'='*50}")
    print(f"Signal Core 分析開始：{now}")
    print(f"{'='*50}")

    results = []
    has_strong = False

    for symbol in SYMBOLS:
        short = symbol.replace('USDT','')
        print(f"\n[{short}] 抓取 K線...")
        try:
            klines1 = fetch_klines(symbol, TIMEFRAME, LIMIT)
            klines2 = fetch_klines(symbol, TF2, LIMIT)
            price   = fetch_price(symbol)
            print(f"[{short}] 價格: ${price:,.2f}，計算指標...")

            tf1_text = build_indicator_text(klines1, TIMEFRAME)
            tf2_text = build_indicator_text(klines2, TF2)

            print(f"[{short}] 呼叫 Claude...")
            analysis = analyze_with_claude(symbol, price, tf1_text, tf2_text)

            sig   = analysis['signal']
            conf  = analysis['confidence']
            align = analysis.get('tfAlignment','?')
            bias  = analysis.get('smcBias','')
            print(f"[{short}] → {sig} (信心:{conf}%) | {align} | {bias}")

            if conf >= 70 and sig != 'NEUTRAL':
                has_strong = True

            results.append({
                'symbol': short, 'price': price, 'analysis': analysis
            })

        except Exception as e:
            print(f"[{short}] 錯誤: {e}")
            results.append({'symbol': short, 'price': 0, 'error': str(e)})

    # 組合信件內容
    lines = [f"Signal Core AI 分析報告", f"時間：{now}", "="*40]
    for r in results:
        sym = r['symbol']
        if 'error' in r:
            lines.append(f"\n[{sym}] ❌ 分析失敗：{r['error']}")
            continue
        a = r['analysis']
        fmt = lambda n: f"{n:,.2f}" if n > 10 else f"{n:.4f}"
        lines += [
            f"\n[{sym}] ${r['price']:,.2f}",
            f"  信號：{a['signal']} | 信心：{a['confidence']}% | 風險：{a['risk']}",
            f"  SMC：{a.get('smcBias','')} | 週期：{a.get('tfAlignment','')}",
            f"  Entry：${fmt(a['entry'])} | SL：${fmt(a['stopLoss'])}",
            f"  TP1：${fmt(a['takeProfit1'])} | TP2：${fmt(a['takeProfit2'])}",
            f"  槓桿：{a['leverage']}x",
            f"  📊 {a['summary']}",
        ]

    body = '\n'.join(lines)
    subject = (f"🔥 [Signal Core] 強烈信號！{now}" if has_strong
               else f"📊 [Signal Core] 市場分析 {now}")

    print(f"\n寄送信件：{subject}")
    ok = send_email(subject, body)
    print(f"Email {'✓ 成功' if ok else '✗ 失敗'}")
    print(f"\n{'='*50}\n分析完成\n{'='*50}")

if __name__ == '__main__':
    main()
