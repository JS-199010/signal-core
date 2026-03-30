"""
Signal Core — GitHub Actions 自動分析腳本
每4小時執行：Binance K線 → 技術指標 + SMC → Claude 分析 → Gmail 寄信
新增：信號一致性過濾（連續兩次方向相同才寄信）
"""

import os, json, math, requests, smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone

# ── 設定 ───────────────────────────────────────────────
CLAUDE_KEY    = os.environ['CLAUDE_API_KEY']
GMAIL_USER    = os.environ['GMAIL_USER']
GMAIL_APP_PWD = os.environ['GMAIL_APP_PASSWORD']
EMAIL_TO      = os.environ['EMAIL_TO']

SYMBOLS   = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'PAXGUSDT']
TIMEFRAME = '4h'
TF2       = '1d'
LIMIT     = 300

# ── Binance ────────────────────────────────────────────
import time as _time

# CoinGecko coin ID mapping
COINGECKO_ID = {
    'BTCUSDT':  'bitcoin',
    'ETHUSDT':  'ethereum',
    'SOLUSDT':  'solana',
    'PAXGUSDT': 'pax-gold',
}
# CoinGecko OHLC granularity: days=1→30min, days=7→4h, days=max→4h(>90d)
CG_DAYS = {'15m': 1, '1h': 7, '4h': 'max', '1d': 'max'}

_last_cg_call = 0

def _get(url, timeout=20, retries=3):
    global _last_cg_call
    # Rate limit: wait at least 2s between CoinGecko calls
    elapsed = _time.time() - _last_cg_call
    if elapsed < 2.5:
        _time.sleep(2.5 - elapsed)
    for attempt in range(retries):
        try:
            res = requests.get(url, timeout=timeout,
                headers={'User-Agent': 'SignalCore/1.0'})
            _last_cg_call = _time.time()
            if not res.text.strip():
                raise Exception(f"空白回應 HTTP {res.status_code}")
            data = res.json()
            if isinstance(data, dict) and data.get('status', {}).get('error_code') == 429:
                raise Exception("Rate limit 429，等待重試")
            return data
        except Exception as e:
            print(f"  [retry {attempt+1}/{retries}] {e}")
            if attempt < retries - 1:
                _time.sleep(10 * (attempt + 1))
    raise Exception(f"連線失敗已重試 {retries} 次")

def fetch_klines(symbol, interval, limit=300):
    cg_id = COINGECKO_ID.get(symbol)
    if not cg_id:
        raise Exception(f"未支援的幣種: {symbol}")
    days = CG_DAYS.get(interval, 'max')
    url = f'https://api.coingecko.com/api/v3/coins/{cg_id}/ohlc?vs_currency=usd&days={days}'
    data = _get(url)
    if not isinstance(data, list) or len(data) == 0:
        raise Exception(f"CoinGecko 回傳異常: {str(data)[:100]}")
    data = data[-limit:]
    return [{'t': int(k[0]), 'o': float(k[1]), 'h': float(k[2]),
             'l': float(k[3]), 'c': float(k[4]), 'v': 0.0} for k in data]

def fetch_price(symbol):
    cg_id = COINGECKO_ID.get(symbol)
    if not cg_id:
        raise Exception(f"未支援的幣種: {symbol}")
    url = f'https://api.coingecko.com/api/v3/simple/price?ids={cg_id}&vs_currencies=usd'
    data = _get(url, timeout=10)
    return float(data[cg_id]['usd'])

# ── 傳統指標 ───────────────────────────────────────────
def calc_sma(closes, p):
    return [sum(closes[i-p+1:i+1])/p if i>=p-1 else None for i in range(len(closes))]

def calc_ema(closes, p):
    k=2/(p+1); r=[]
    for i,v in enumerate(closes): r.append(v if i==0 else v*k+r[-1]*(1-k))
    return r

def calc_rsi(closes, p=14):
    result=[]
    for i in range(len(closes)):
        if i<p: result.append(None); continue
        g=l=0
        for j in range(i-p+1,i+1):
            d=closes[j]-closes[j-1]
            if d>0: g+=d
            else: l+=abs(d)
        result.append(round(100-100/(1+g/(l or 0.0001)),2))
    return result

def calc_macd(closes):
    e12=calc_ema(closes,12); e26=calc_ema(closes,26)
    macd=[round(a-b,6) for a,b in zip(e12,e26)]
    sig=calc_ema(macd[-26:],9)
    return {'macd':round(macd[-1],4),'signal':round(sig[-1],4),'histogram':round(macd[-1]-sig[-1],6)}

def calc_bb(closes, p=20):
    last=closes[-p:]; mean=sum(last)/p
    std=math.sqrt(sum((x-mean)**2 for x in last)/p)
    return {'upper':round(mean+2*std,2),'middle':round(mean,2),'lower':round(mean-2*std,2)}

def calc_atr(klines, p=14):
    trs=[max(k['h']-k['l'],abs(k['h']-klines[i]['c']),abs(k['l']-klines[i]['c'])) for i,k in enumerate(klines[1:],0)]
    return round(sum(trs[-p:])/p,4)

def vol_ratio(klines):
    avg=sum(k['v'] for k in klines[-10:])/10
    if avg == 0: return 1.0  # CoinGecko OHLC doesn't include volume
    return round(klines[-1]['v']/avg,2)

# ── SMC ────────────────────────────────────────────────
def find_swings(klines, lb=5):
    highs,lows=[],[]
    for i in range(lb,len(klines)-lb):
        if all(klines[j]['h']<=klines[i]['h'] for j in range(i-lb,i)) and all(klines[j]['h']<=klines[i]['h'] for j in range(i+1,i+lb+1)):
            highs.append({'i':i,'price':klines[i]['h']})
        if all(klines[j]['l']>=klines[i]['l'] for j in range(i-lb,i)) and all(klines[j]['l']>=klines[i]['l'] for j in range(i+1,i+lb+1)):
            lows.append({'i':i,'price':klines[i]['l']})
    return highs,lows

def detect_structure(klines, highs, lows):
    price=klines[-1]['c']; bias='NEUTRAL'; bos=None; choch=None
    rh=highs[-4:]; rl=lows[-4:]
    for i in range(1,len(rh)):
        if rh[i]['price']>rh[i-1]['price']:
            bias='BULLISH'; bos=f"BOS向上 @ ${rh[i]['price']:.2f}（{len(klines)-rh[i]['i']-1}根前）"
    for i in range(1,len(rl)):
        if rl[i]['price']<rl[i-1]['price']:
            bias='BEARISH'; bos=f"BOS向下 @ ${rl[i]['price']:.2f}（{len(klines)-rl[i]['i']-1}根前）"
    if bias=='BULLISH' and rl and price<rl[-1]['price']:
        choch=f"CHoCH看空 @ ${rl[-1]['price']:.2f}"
    if bias=='BEARISH' and rh and price>rh[-1]['price']:
        choch=f"CHoCH看多 @ ${rh[-1]['price']:.2f}"
    return bias,bos,choch

def find_obs(klines, n=3):
    price=klines[-1]['c']; bull,bear=[],[]
    for i in range(5,len(klines)-3):
        c=klines[i]; imp=klines[i+1:i+4]
        if c['c']<c['o'] and all(k['c']>k['o'] for k in imp) and imp[-1]['h']>c['h']*1.002 and c['l']<price:
            bull.append(f"${c['l']:.2f}-${c['h']:.2f}（{len(klines)-i-1}根前）")
        if c['c']>c['o'] and all(k['c']<k['o'] for k in imp) and imp[-1]['l']<c['l']*0.998 and c['h']>price:
            bear.append(f"${c['l']:.2f}-${c['h']:.2f}（{len(klines)-i-1}根前）")
    return bull[-n:],bear[-n:]

def find_fvgs(klines, n=3):
    price=klines[-1]['c']; bull,bear=[],[]
    for i in range(1,len(klines)-1):
        p,nx=klines[i-1],klines[i+1]
        if nx['l']>p['h'] and price>p['h']: bull.append(f"${p['h']:.2f}-${nx['l']:.2f}（{len(klines)-i-1}根前）")
        if nx['h']<p['l'] and price<p['l']: bear.append(f"${nx['h']:.2f}-${p['l']:.2f}（{len(klines)-i-1}根前）")
    return bull[-n:],bear[-n:]

def calc_pd(klines, highs, lows):
    if not highs or not lows: return ''
    eq=(highs[-1]['price']+lows[-1]['price'])/2; price=klines[-1]['c']
    return f"EQ=${eq:.2f} | 現價{'PREMIUM（偏貴）' if price>eq else 'DISCOUNT（偏便宜）'}"

def find_liq(highs, lows):
    tol=0.002; bsl=set(); ssl=set()
    for i in range(len(highs)-1):
        for j in range(i+1,len(highs)):
            if abs(highs[i]['price']-highs[j]['price'])/highs[i]['price']<tol:
                bsl.add(round((highs[i]['price']+highs[j]['price'])/2,2))
    for i in range(len(lows)-1):
        for j in range(i+1,len(lows)):
            if abs(lows[i]['price']-lows[j]['price'])/lows[i]['price']<tol:
                ssl.add(round((lows[i]['price']+lows[j]['price'])/2,2))
    return list(bsl)[-2:],list(ssl)[-2:]

def build_smc_text(klines):
    highs,lows=find_swings(klines)
    bias,bos,choch=detect_structure(klines,highs,lows)
    bull_obs,bear_obs=find_obs(klines)
    bull_fvg,bear_fvg=find_fvgs(klines)
    pd=calc_pd(klines,highs,lows)
    bsl,ssl=find_liq(highs,lows)
    lines=[f"市場結構: {bias}"]
    if bos: lines.append(f"最後BOS: {bos}")
    if choch: lines.append(f"CHoCH: {choch}")
    if bull_obs: lines.append(f"多方OB: {', '.join(bull_obs)}")
    if bear_obs: lines.append(f"空方OB: {', '.join(bear_obs)}")
    if bull_fvg: lines.append(f"看多FVG: {', '.join(bull_fvg)}")
    if bear_fvg: lines.append(f"看空FVG: {', '.join(bear_fvg)}")
    if pd: lines.append(f"P/D Zone: {pd}")
    if bsl: lines.append(f"BSL: ${', $'.join(map(str,bsl))}")
    if ssl: lines.append(f"SSL: ${', $'.join(map(str,ssl))}")
    return '\n'.join(lines)

def build_tf_text(klines, tf):
    closes=[k['c'] for k in klines]
    rsi=calc_rsi(closes); macd=calc_macd(closes); bb=calc_bb(closes)
    sma20=calc_sma(closes,20)[-1]; ema50=calc_ema(closes,50)[-1]; ema200=calc_ema(closes,200)[-1]
    ind=(f"RSI(14)={rsi[-1]} | SMA20={sma20:.2f} | EMA50={ema50:.2f} | EMA200={ema200:.2f}\n"
         f"MACD={macd['macd']} Signal={macd['signal']} Hist={macd['histogram']}\n"
         f"BB 上={bb['upper']} 中={bb['middle']} 下={bb['lower']}\n"
         f"ATR={calc_atr(klines)} | 量比={vol_ratio(klines)}x")
    candles=' | '.join(f"O:{k['o']} H:{k['h']} L:{k['l']} C:{k['c']}" for k in klines[-5:])
    tf_name={'4h':'4小時','1d':'日線','1h':'1小時','15m':'15分鐘'}.get(tf,tf)
    return f"【{tf_name} 指標】\n{ind}\n近5根K棒：{candles}\n\n【{tf_name} SMC】\n{build_smc_text(klines)}"

# ── Claude ─────────────────────────────────────────────
def analyze(symbol, price, tf1, tf2):
    gold=(" （黃金代幣，走勢與實物黃金相關，槓桿保守最高10x）" if symbol=="PAXGUSDT" else "")
    prompt=f"""你是一位專業的加密貨幣合約交易分析師，精通 SMC 與多時間框架共振分析。

幣種：{symbol}｜當前價格：${price}{gold}

━━━ 短週期（入場時機）━━━
{tf1}

━━━ 長週期（趨勢方向）━━━
{tf2}

SMC邏輯：長週期結構定方向 → Discount區OB/FVG做多，Premium區OB/FVG做空 → SL放OB外側 → TP瞄準流動性
兩週期共振給高信心，分歧給NEUTRAL。傳統指標輔助確認。

只回傳JSON不要其他文字：
{{"signal":"LONG|SHORT|NEUTRAL","confidence":0-100,"entry":數字,"stopLoss":數字,"takeProfit1":數字,"takeProfit2":數字,"leverage":1-20,"tfAlignment":"ALIGNED|DIVERGED","smcBias":"看多OB回測|看空OB反壓|FVG填補|流動性獵殺|結構突破|觀望","summary":"70字內中文摘要","risk":"LOW|MEDIUM|HIGH"}}"""

    res=requests.post('https://api.anthropic.com/v1/messages',
        headers={'x-api-key':CLAUDE_KEY,'anthropic-version':'2023-06-01','Content-Type':'application/json'},
        json={'model':'claude-sonnet-4-20250514','max_tokens':700,'messages':[{'role':'user','content':prompt}]},
        timeout=30)
    res.raise_for_status()
    text=res.json()['content'][0]['text'].strip()
    return json.loads(text.replace('```json','').replace('```','').strip())

# ── 信號一致性 ──────────────────────────────────────────
def load_last_signals():
    raw=os.environ.get('LAST_SIGNALS','')
    try: return json.loads(raw) if raw else {}
    except: return {}

def consistent_symbols(current, last):
    out=[]
    for sym,a in current.items():
        prev=last.get(sym,{})
        if (a['signal']!='NEUTRAL' and
            a['signal']==prev.get('signal') and
            a['confidence']>=60 and prev.get('confidence',0)>=60):
            out.append(sym)
    return out

# ── Gmail SMTP ──────────────────────────────────────────
def send_email(subject, body, consistent):
    msg=MIMEMultipart('alternative')
    msg['Subject']=subject; msg['From']=GMAIL_USER; msg['To']=EMAIL_TO

    consistent_note=''
    if consistent:
        consistent_note=f'<div style="background:#00ff8820;border:1px solid #00ff8840;border-radius:6px;padding:10px 14px;margin-bottom:16px;color:#00ff88">✓ 連續兩次一致信號：{", ".join(consistent)}</div>'

    rows=''
    for line in body.split('\n'):
        if line.startswith('[') and ']' in line:
            rows+=f'<tr><td colspan="2" style="padding:10px 0 4px;font-weight:bold;border-top:1px solid #1a2a3a;color:#00e5ff">{line}</td></tr>'
        elif line.strip():
            rows+=f'<tr><td colspan="2" style="padding:2px 0;font-size:12px">{line}</td></tr>'

    html=f"""<html><body style="background:#080c14;color:#c8dce8;font-family:monospace;padding:20px">
<div style="max-width:600px;margin:0 auto">
<h2 style="color:#00e5ff;letter-spacing:3px;border-bottom:1px solid #1a2a3a;padding-bottom:12px">◈ SIGNAL_CORE</h2>
{consistent_note}
<table style="width:100%;font-size:13px">{rows}</table>
<div style="margin-top:20px;font-size:11px;color:#3a5068">Signal Core — AI Analysis Terminal</div>
</div></body></html>"""

    msg.attach(MIMEText(body,'plain','utf-8'))
    msg.attach(MIMEText(html,'html','utf-8'))
    print(f"SMTP 連線中... user={GMAIL_USER}, to={EMAIL_TO}")
    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
        s.login(GMAIL_USER, GMAIL_APP_PWD)
        s.sendmail(GMAIL_USER, EMAIL_TO, msg.as_string())
    print("SMTP 寄送完成")

# ── 主程式 ─────────────────────────────────────────────
def main():
    now=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    print(f"\n{'='*50}\nSignal Core：{now}\n{'='*50}")

    last=load_last_signals()
    current={}; results=[]

    for symbol in SYMBOLS:
        short=symbol.replace('USDT','')
        print(f"\n[{short}] 抓取K線...")
        try:
            k1=fetch_klines(symbol,TIMEFRAME,LIMIT)
            k2=fetch_klines(symbol,TF2,LIMIT)
            price=fetch_price(symbol)
            print(f"[{short}] ${price:,.4f} → 計算指標...")
            a=analyze(symbol,price,build_tf_text(k1,TIMEFRAME),build_tf_text(k2,TF2))
            print(f"[{short}] {a['signal']} ({a['confidence']}%) | {a.get('tfAlignment')} | {a.get('smcBias')}")
            current[short]={'signal':a['signal'],'confidence':a['confidence']}
            results.append({'symbol':short,'price':price,'analysis':a})
        except Exception as e:
            print(f"[{short}] 錯誤: {e}")
            results.append({'symbol':short,'price':0,'error':str(e)})

    # 儲存當次信號供下次比對
    with open('current_signals.json', 'w') as f:
        json.dump(current, f)
    print(f"當次信號已儲存：{json.dumps(current)}")

    consistent=consistent_symbols(current,last)
    has_strong=any(r.get('analysis',{}).get('confidence',0)>=70 and
                   r.get('analysis',{}).get('signal')!='NEUTRAL'
                   for r in results if 'analysis' in r)

    print(f"一致信號：{consistent or '無'} | 強烈信號：{'是' if has_strong else '否'}")

    # 無上次記錄（第一次）或有一致/強烈信號才寄信
    should_send = not last or bool(consistent) or has_strong
    if not should_send:
        print("⚠ 信號與上次不一致且無強烈信號，跳過寄信")
        return

    lines=[f"Signal Core AI 分析報告",f"時間：{now}","="*40]
    if consistent: lines+=[f"✓ 連續一致信號：{', '.join(consistent)}",""]
    for r in results:
        sym=r['symbol']
        if 'error' in r: lines.append(f"\n[{sym}] ❌ {r['error']}"); continue
        a=r['analysis']; fmt=lambda n: f"{n:,.2f}" if n>10 else f"{n:.4f}"
        lines+=[f"\n[{sym}] ${r['price']:,.4f}",
                f"  信號：{a['signal']} | 信心：{a['confidence']}% | 風險：{a['risk']}",
                f"  SMC：{a.get('smcBias','')} | 週期：{a.get('tfAlignment','')}",
                f"  Entry：${fmt(a['entry'])} | SL：${fmt(a['stopLoss'])}",
                f"  TP1：${fmt(a['takeProfit1'])} | TP2：${fmt(a['takeProfit2'])}",
                f"  槓桿：{a['leverage']}x",
                f"  📊 {a['summary']}"]

    subject=(f"🔥 [Signal Core] 強烈信號！{now}" if has_strong
             else f"📊 [Signal Core] 一致信號確認 {now}")
    try:
        send_email(subject,'\n'.join(lines),consistent)
        print("Email ✓ 成功")
    except Exception as e:
        import traceback
        print(f"Email ✗ 失敗：{e}")
        traceback.print_exc()

if __name__=='__main__':
    main()
