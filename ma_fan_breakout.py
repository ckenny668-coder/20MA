#!/usr/bin/env python3
"""均線發散突破 · 台股掃描器

每個交易日收盤後掃描上市＋上櫃普通股，三個條件同一天成立才算訊號：
  1. 20MA > 60MA > 120MA
  2. 收盤價 > 前 10 日最高收盤價
  3. 5、10、20MA 都在上揚，且 5MA 上升最快（預設 3 日斜率，並要求 5MA > 10MA > 20MA）

用法：
  pip install yfinance pandas numpy requests lxml
  python ma_fan_breakout.py              # 抓真實資料，輸出 docs/index.html
  python ma_fan_breakout.py --demo       # 模擬資料，只看畫面
  python ma_fan_breakout.py --compare    # 另外印出不同參數與原作數字的比對
  python ma_fan_breakout.py --fresh      # 忽略 frames.pkl 快取，重新下載
"""


import sys
import json, gzip, base64, io, re, datetime as dt
import numpy as np
import pandas as pd

# ========= 設定 =========
DEMO = "--demo" in sys.argv   # 命令列加 --demo 就用模擬資料
START = "2026-01-02"      # 訊號掃描起日
FETCH_FROM = "2025-05-01" # 資料抓取起日（要早於 START 至少 130 個交易日，才算得出 120MA）
LOOKBACK = 10             # 「突破前 N 日最高收盤」與「首次」共用的 N
MIN_BARS = 130            # 股票至少要有幾根日 K 才納入股票池
OUT = "docs/index.html"
SLOPE_DAYS = 3            # 5/10/20MA「上揚、誰上升最快」用幾日斜率比較（1 = 與前一天比）；3 為與原作數字最接近
SHORT_ORDER = True        # True = 另外要求 5MA > 10MA > 20MA；與原作數字最接近
EMBED = "gzip"            # "gzip"（檔案小，需較新瀏覽器）或 "plain"（最相容、檔案較大）

# ========= 核心：指標與訊號 =========
MA_N = (5, 10, 20, 60, 120)

def detect(df, lookback=LOOKBACK, slope_days=None, short_order=None):
    """df: index=日期, 欄位 Open/High/Low/Close/Volume（還原權值）。回傳加上 MA 與 sig/first/streak 的 DataFrame。"""
    sd = SLOPE_DAYS if slope_days is None else slope_days
    so = SHORT_ORDER if short_order is None else short_order
    d = df.copy()
    c = d["Close"]
    for n in MA_N:
        d[f"ma{n}"] = c.rolling(n).mean()
    cond1 = (d.ma20 > d.ma60) & (d.ma60 > d.ma120)
    cond2 = c > c.shift(1).rolling(lookback).max()
    g5, g10, g20 = [d[f"ma{n}"] / d[f"ma{n}"].shift(sd) - 1 for n in (5, 10, 20)]
    cond3 = (g5 > 0) & (g10 > 0) & (g20 > 0) & (g5 > g10) & (g5 > g20)
    if so:
        cond3 = cond3 & (d.ma5 > d.ma10) & (d.ma10 > d.ma20)
    d["sig"] = (cond1 & cond2 & cond3).fillna(False).astype(bool)
    prev = d["sig"].astype(int).shift(1).rolling(lookback, min_periods=1).sum().fillna(0)
    d["first"] = d["sig"] & (prev == 0)
    grp = (~d["sig"]).cumsum()
    d["streak"] = d["sig"].astype(int).groupby(grp).cumsum()
    return d

# ========= 自我測試（確認邏輯正確；失敗會直接報錯）=========
def _mk(closes):
    c = np.asarray(closes, float)
    idx = pd.bdate_range("2025-01-01", periods=len(c))
    return pd.DataFrame({"Open": c, "High": c * 1.004, "Low": c * 0.996, "Close": c, "Volume": 1e6}, index=idx)

# 1) 緩步墊高 → 小幅整理 → 放量跳漲：跳漲當天應為「首次」訊號
base = np.r_[np.linspace(50, 70, 170), 69.5 - 0.02 * np.arange(30) + 0.25 * np.sin(np.arange(30))]
up = np.r_[base, [base[-1] * 1.04, base[-1] * 1.06, base[-1] * 1.08]]
r = detect(_mk(up), slope_days=1, short_order=False)
jump = len(base)
assert r["sig"].iloc[jump] and r["first"].iloc[jump] and r["streak"].iloc[jump] == 1, "跳漲日應為首次訊號"
assert r["sig"].iloc[:jump].sum() == 0, "整理期不該有訊號"
if r["sig"].iloc[jump + 1]:
    assert r["streak"].iloc[jump + 1] == 2 and not r["first"].iloc[jump + 1], "隔天若仍符合應為連續第 2 天"

# 2) 空頭排列（一路下跌）→ 不該有訊號
assert detect(_mk(np.linspace(100, 40, 250)))["sig"].sum() == 0

# 3) 下跌趨勢中的急拉（搶反彈）：收盤雖突破前 10 日高點，但 20MA < 60MA → 不該有訊號
bounce = np.r_[np.linspace(100, 60, 200), 60 * 1.06]
assert detect(_mk(bounce))["sig"].sum() == 0

# 4) 緊密整理後突破：20>60>120 成立、5MA 漲最快 → 應有訊號（符合規格）
flat = np.r_[np.full(200, 50.0), 52.0]
assert detect(_mk(flat), slope_days=1, short_order=False)["sig"].iloc[-1]
print("✅ 訊號邏輯測試通過")

# ========= 股票池（證交所 ISIN 公開清單：上市 + 上櫃，CFICode=ESVUFR 為普通股）=========
def fetch_universe():
    import requests
    out = []
    for mode, mkt, suf in ((2, "上市", ".TW"), (4, "上櫃", ".TWO")):
        r = requests.get(f"https://isin.twse.com.tw/isin/C_public.jsp?strMode={mode}", timeout=30)
        html = r.content.decode("ms950", errors="ignore")
        df = pd.read_html(io.StringIO(html))[0]
        df.columns = df.iloc[0]
        df = df.iloc[1:]
        df = df[df["CFICode"] == "ESVUFR"]
        for _, row in df.iterrows():
            parts = str(row["有價證券代號及名稱"]).split("\u3000")
            if len(parts) < 2 or not re.fullmatch(r"\d{4}", parts[0].strip()):
                continue
            out.append({"code": parts[0].strip(), "name": parts[1].strip(), "mkt": mkt,
                        "ind": str(row.get("產業別", "")).replace("nan", ""), "ticker": parts[0].strip() + suf})
    return pd.DataFrame(out).drop_duplicates("ticker").reset_index(drop=True)


def download_all(uni, start, chunk=100):
    import yfinance as yf
    frames = {}
    for i in range(0, len(uni), chunk):
        tk = list(uni.ticker.iloc[i:i + chunk])
        raw = yf.download(tk, start=start, auto_adjust=True, group_by="ticker", progress=False, threads=True)
        for t in tk:
            try:
                d = raw[t].dropna(subset=["Close"])
            except KeyError:
                continue
            if len(d):
                frames[t] = d[["Open", "High", "Low", "Close", "Volume"]].copy()
        print(f"  已下載 {min(i + chunk, len(uni))}/{len(uni)}", end="\r")
    print()
    return frames

# ========= 模擬資料（DEMO=True 時使用；名稱與價格皆為隨機，非真實行情）=========
def make_demo(n=160, seed=7):
    rng = np.random.default_rng(seed)
    cal = pd.bdate_range(FETCH_FROM, "2026-10-22")
    inds = ["半導體", "電子零組件", "電腦及週邊", "通信網路", "生技醫療", "機械", "其他電子"]
    rows, frames = [], {}
    for k in range(n):
        code_ = f"{1101 + k * 7:04d}"
        mkt, suf = ("上市", ".TW") if k % 3 else ("上櫃", ".TWO")
        t = code_ + suf
        rows.append({"code": code_, "name": f"示範{k + 1:03d}", "mkt": mkt, "ind": inds[k % len(inds)], "ticker": t})
        r = np.empty(len(cal)); pos = 0
        while pos < len(cal):
            L = int(rng.integers(30, 90)); drift = rng.choice([-0.002, 0.0, 0.0015, 0.004, 0.007])
            r[pos:pos + L] = rng.normal(drift, 0.017, min(L, len(cal) - pos)); pos += L
        c = float(rng.uniform(15, 220)) * np.exp(np.cumsum(r))
        o = np.r_[c[0], c[:-1]] * (1 + rng.normal(0, .004, len(c)))
        h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, .006, len(c))))
        l = np.minimum(o, c) * (1 - np.abs(rng.normal(0, .006, len(c))))
        v = rng.lognormal(15, .5, len(c)) * (1 + 2 * np.abs(r) * 20)
        frames[t] = pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c, "Volume": v}, index=cal)
    return pd.DataFrame(rows), frames

# ========= 掃描：每檔算訊號、報酬，整理成網頁資料 =========
def scan(uni, frames, start=START):
    # 共同交易日曆：該日至少有一半股票有資料，濾掉雜訊日
    cnt = pd.Series(dtype=int)
    for d in frames.values():
        cnt = cnt.add(pd.Series(1, index=d.index), fill_value=0)
    cal = cnt[cnt >= 0.5 * len(frames)].index.sort_values()
    cpos = {d: i for i, d in enumerate(cal)}
    start_ts = pd.Timestamp(start)
    meta_by_t = uni.set_index("ticker")

    stocks, sigs, pool = [], [], 0
    for t, d in frames.items():
        d = d[d.index.isin(cal)]
        if len(d) < MIN_BARS:
            continue
        d = d.reindex(cal[cal >= d.index[0]])
        d[["Open", "High", "Low", "Close"]] = d[["Open", "High", "Low", "Close"]].ffill()
        d["Volume"] = d["Volume"].fillna(0)
        pool += 1
        x = detect(d)
        m = (x.index >= start_ts) & x["sig"]
        if not m.any():
            continue
        c, hi, lo = x["Close"].to_numpy(), x["High"].to_numpy(), x["Low"].to_numpy()
        n = len(x); pos_all = np.flatnonzero(m.to_numpy())
        first_bar = max(0, pos_all[0] - 125)           # 為了畫 120MA，往前多存 125 根
        s_idx = cpos[x.index[first_bar]]
        si = len(stocks)
        info = meta_by_t.loc[t]
        stocks.append({
            "c": info["code"], "n": info["name"], "m": info["mkt"], "i": info["ind"], "s": s_idx,
            "o": np.round(x["Open"].to_numpy()[first_bar:], 2).tolist(),
            "h": np.round(hi[first_bar:], 2).tolist(),
            "l": np.round(lo[first_bar:], 2).tolist(),
            "cl": np.round(c[first_bar:], 2).tolist(),
            "v": np.round(x["Volume"].to_numpy()[first_bar:] / 1000).astype(int).tolist(),
        })
        for p in pos_all:
            def fwd(k): return round(float(c[p + k] / c[p] - 1), 4) if p + k < n else None
            last = p == n - 1
            sigs.append([si, cpos[x.index[p]], round(float(c[p]), 2),
                         round(float(c[p] * x["Volume"].iloc[p] / 1e8), 2),
                         int(x["first"].iloc[p]), int(x["streak"].iloc[p]),
                         fwd(5), fwd(10), fwd(20),
                         None if last else round(float(c[-1] / c[p] - 1), 4),
                         None if last else round(float(hi[p + 1:].max() / c[p] - 1), 4),
                         None if last else round(float(lo[p + 1:].min() / c[p] - 1), 4)])
    last_di = len(cal) - 1
    last_rows = [s for s in sigs if s[1] == last_di]
    meta = {
        "last_date": str(cal[-1].date()), "start_date": start,
        "n_signals": len(sigs), "n_first": int(sum(s[4] for s in sigs)),
        "n_stocks": len(stocks), "pool": pool,
        "last_n": len(last_rows), "last_first": int(sum(s[4] for s in last_rows)),
        "generated": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M"),
    }
    return {"dates": [str(d.date()) for d in cal], "stocks": stocks, "sigs": sigs, "meta": meta}

TEMPLATE = r"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>均線發散突破</title>
<style>
:root{--bg:#eef1f5;--card:#fafafa;--fg:#111827;--mute:#6b7280;--line:#dfe3e9;--chip:#ffffff;--up:#d62828;--dn:#1b9e4b;--grid:#e6e9ee;--accent:#1769e0;--shadow:0 1px 2px rgba(0,0,0,.05)}
:root[data-theme=dark]{--bg:#0e1217;--card:#161b22;--fg:#e6eaf0;--mute:#8a94a3;--line:#262d37;--chip:#1b222b;--up:#ff5d5d;--dn:#3ddc84;--grid:#232a33;--accent:#5b9dff;--shadow:none}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,"PingFang TC","Noto Sans TC","Microsoft JhengHei",sans-serif;-webkit-text-size-adjust:100%}
.wrap{max-width:980px;margin:0 auto;padding:16px 16px 40px}
.top{display:flex;justify-content:space-between;align-items:center;gap:12px}
.eyebrow{color:var(--mute);font-size:13px;letter-spacing:.02em}
h1{font-size:32px;line-height:1.15;margin:10px 0 14px;font-weight:800}
.theme{border:1px solid var(--line);background:var(--chip);color:var(--fg);border-radius:999px;padding:7px 14px;font-size:13px;cursor:pointer}
.chips{display:flex;flex-wrap:wrap;gap:8px 6px;align-items:center}
.chip{background:var(--chip);border:1px solid var(--line);border-radius:8px;padding:8px 12px;font-size:14px}
.plus{color:var(--mute);font-size:16px}
.meta{margin:16px 0 0;font-size:13.5px;line-height:1.75;color:var(--mute)}
.meta .pill{display:inline-block;color:var(--fg);font-weight:700;background:var(--chip);border:1px solid var(--line);border-radius:999px;padding:3px 12px;margin-right:6px}
.demo{margin:12px 0 0;padding:9px 12px;border-radius:8px;background:#fff4d6;color:#7a5200;font-size:13px;border:1px solid #f0d58a}
hr{border:0;border-top:1px solid var(--line);margin:18px 0}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:12px}
.btn{border:1px solid var(--line);background:var(--chip);color:var(--fg);border-radius:8px;padding:9px 14px;font-size:14px;cursor:pointer;min-height:38px}
.btn:disabled{opacity:.4;cursor:default}
.btn.on{border-color:var(--accent);color:var(--accent);font-weight:700}
.count{font-size:14px;color:var(--mute);margin:0 4px}
.count b{color:var(--fg)}
input[type=date],input[type=text]{border:1px solid var(--line);background:var(--chip);color:var(--fg);border-radius:8px;padding:8px 10px;font-size:14px;min-height:38px;font-family:inherit}
input[type=text]{width:110px}
label.chk{font-size:14px;display:flex;align-items:center;gap:5px;cursor:pointer}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;box-shadow:var(--shadow)}
.head{display:flex;justify-content:space-between;align-items:baseline;gap:10px;flex-wrap:wrap}
.title{font-size:24px;font-weight:800}
.tag{color:var(--mute);font-size:14px}
.stats{margin:10px 0 0;font-size:14.5px;line-height:2}
.stats span{white-space:nowrap;margin-right:14px}
.stats .k{color:var(--mute)}
.stats b{font-weight:700}
.badge{display:inline-block;padding:1px 9px;border-radius:6px;font-size:13px;font-weight:700;background:rgba(23,105,224,.12);color:var(--accent)}
.badge.cont{background:rgba(214,40,40,.12);color:var(--up)}
.up{color:var(--up)}.dn{color:var(--dn)}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.ohlc{margin-top:14px;font-size:13.5px;line-height:1.7}
.mas{font-size:13.5px;line-height:1.7}
.chartbox{position:relative;margin-top:8px}
canvas{width:100%;height:380px;display:block;touch-action:pan-y;cursor:crosshair}
.ranges{display:flex;gap:6px;margin-top:8px}
.hist{margin-top:16px;overflow-x:auto}
.hist h3{font-size:14px;margin:0 0 6px;color:var(--mute);font-weight:600}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:6px 8px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
th:first-child,td:first-child{text-align:left}
th{color:var(--mute);font-weight:600}
tr.cur td{background:rgba(23,105,224,.08)}
.hint{margin-top:10px;color:var(--mute);font-size:12.5px}
.foot{margin-top:22px;font-size:12.5px;color:var(--mute);line-height:1.8}
.empty{padding:40px 0;text-align:center;color:var(--mute)}
.toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%);background:#222;color:#fff;padding:9px 16px;border-radius:8px;font-size:13px;opacity:0;transition:opacity .2s;pointer-events:none}
.toast.show{opacity:.95}
@media(max-width:560px){h1{font-size:28px}.title{font-size:21px}canvas{height:340px}}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div class="eyebrow">台股 · 上市＋上櫃全部普通股</div>
    <button class="theme" id="theme" type="button">夜間</button>
  </div>
  <h1>均線發散突破</h1>
  <div class="chips">__CHIPS__</div>
  <div id="demo" class="demo" hidden>⚠ 這是「模擬資料」的示範頁，股票名稱與價格皆為隨機產生，不是真實行情。用真實資料重跑 notebook 後才會是真實名單。</div>
  <p class="meta" id="meta"></p>
  <hr>
  <div class="bar">
    <button class="btn" id="prev" type="button">‹ 上一檔</button>
    <button class="btn" id="next" type="button">下一檔 ›</button>
    <span class="count">訊號清單 <b id="pos">0</b> / <span id="tot">0</span></span>
    <label class="chk"><input type="checkbox" id="onlyFirst">只看首次</label>
    <input type="date" id="jump" title="跳到日期">
    <input type="text" id="find" placeholder="代號 + Enter" inputmode="numeric">
  </div>
  <div class="card" id="card"></div>
  <div class="hint">鍵盤：↑ ← 上一檔，↓ → 下一檔。手機可在圖上左右滑動查看每根 K 棒的數值。</div>
  <div class="foot">
    條件是機械式的，大盤急跌時，剛突破的股票一樣會被拖下去。名單是「符合條件」，不是「一定會漲」，也有很多突破後回落的例子。<br>
    資料來源：Yahoo Finance（還原權值），偶爾會有缺漏；重要價格請以券商報價為準。這是個人研究工具，不是投資建議。
  </div>
</div>
<div class="toast" id="toast"></div>
<script id="payload" type="application/json">__PAYLOAD__</script>
<script>
(function(){
"use strict";
var GZ=__GZ__, DEMO=__DEMO__;
var $=function(s){return document.querySelector(s)};
var fmt=function(x,d){return x==null?'—':Number(x).toFixed(d==null?2:d)};
var pct=function(x){return x==null?'—':(x>=0?'+':'')+(x*100).toFixed(1)+'%'};
var cls=function(x){return x==null?'':(x>0?'up':(x<0?'dn':''))};
var D=null, list=[], idx=0, stock=null, hov=null, rangeN=120, bySt={};

/* ---------- theme ---------- */
function setTheme(t){document.documentElement.setAttribute('data-theme',t);$('#theme').textContent=t==='dark'?'日間':'夜間';try{localStorage.setItem('ma-theme',t)}catch(e){}if(stock)draw();}
(function(){var t=null;try{t=localStorage.getItem('ma-theme')}catch(e){}
 if(!t)t=(window.matchMedia&&matchMedia('(prefers-color-scheme: dark)').matches)?'dark':'light';
 setTheme(t);})();
$('#theme').onclick=function(){setTheme(document.documentElement.getAttribute('data-theme')==='dark'?'light':'dark')};

/* ---------- load ---------- */
async function load(){
  var raw=$('#payload').textContent.trim();
  if(!GZ)return JSON.parse(raw);
  if(typeof DecompressionStream==='undefined')throw new Error('此瀏覽器不支援壓縮資料，請更新瀏覽器，或用 EMBED="plain" 重新產生。');
  var bin=atob(raw),u=new Uint8Array(bin.length);
  for(var i=0;i<bin.length;i++)u[i]=bin.charCodeAt(i);
  var s=new Blob([u]).stream().pipeThrough(new DecompressionStream('gzip'));
  return JSON.parse(await new Response(s).text());
}

/* ---------- helpers ---------- */
function sma(a,n){var out=new Array(a.length).fill(null),s=0;for(var i=0;i<a.length;i++){s+=a[i];if(i>=n)s-=a[i-n];if(i>=n-1)out[i]=s/n;}return out;}
function ensureMA(st){if(st._ma)return;st._ma={5:sma(st.cl,5),10:sma(st.cl,10),20:sma(st.cl,20),60:sma(st.cl,60),120:sma(st.cl,120)};}
function niceTicks(lo,hi,n){var span=hi-lo||1,step=Math.pow(10,Math.floor(Math.log10(span/n))),e=span/n/step;
  step*= e>=5?5:(e>=2?2:1);var t=[],v=Math.ceil(lo/step)*step;while(v<=hi+1e-9){t.push(v);v+=step;}return {t:t,step:step};}
function toast(m){var el=$('#toast');el.textContent=m;el.classList.add('show');setTimeout(function(){el.classList.remove('show')},1600);}

/* ---------- list ---------- */
function buildList(keep){
  var only=$('#onlyFirst').checked, prevKey=keep&&list[idx]?list[idx].key:null;
  list=D.sigs.map(function(s,k){return {key:k,si:s[0],di:s[1],close:s[2],turn:s[3],first:s[4],streak:s[5],r5:s[6],r10:s[7],r20:s[8],rn:s[9],hi:s[10],lo:s[11]};});
  if(only)list=list.filter(function(x){return x.first});
  list.sort(function(a,b){return b.di-a.di||b.first-a.first||a.streak-b.streak||b.turn-a.turn;});
  idx=0;
  if(prevKey!=null){for(var i=0;i<list.length;i++)if(list[i].key===prevKey){idx=i;break;}}
  $('#tot').textContent=list.length;
}

/* ---------- render ---------- */
function show(i){
  if(!list.length){$('#card').innerHTML='<div class="empty">沒有符合的訊號</div>';$('#pos').textContent=0;return;}
  idx=Math.max(0,Math.min(list.length-1,i));
  var cur=list[idx];stock=D.stocks[cur.si];ensureMA(stock);hov=null;
  $('#pos').textContent=idx+1;
  $('#prev').disabled=idx===0;$('#next').disabled=idx===list.length-1;
  var badge=cur.first?'<span class="badge">首次訊號</span>':'<span class="badge cont">連續第 '+cur.streak+' 天</span>';
  var mine=bySt[cur.si]||[];
  var rows=mine.slice().sort(function(a,b){return b.di-a.di}).map(function(x){
    return '<tr'+(x.di===cur.di?' class="cur"':'')+'><td>'+D.dates[x.di]+'</td><td>'+(x.first?'首次':'連續'+x.streak)+'</td><td>'+fmt(x.close)+'</td>'+
    ['r5','r10','r20','rn'].map(function(k){return '<td class="'+cls(x[k])+'">'+pct(x[k])+'</td>'}).join('')+'</tr>';}).join('');
  $('#card').innerHTML=
   '<div class="head"><div class="title">'+stock.c+' '+stock.n+'</div><div class="tag">'+stock.m+' · '+stock.i+'</div></div>'+
   '<div class="stats">'+
    '<span><span class="k">訊號日</span> <b>'+D.dates[cur.di]+'</b></span>'+
    '<span><span class="k">收盤</span> <b>'+fmt(cur.close)+'</b></span>'+
    '<span><span class="k">成交額</span> <b>'+fmt(cur.turn,1)+' 億</b></span>'+
    '<span>'+badge+'</span><br>'+
    '<span><span class="k">+5日</span> <b class="'+cls(cur.r5)+'">'+pct(cur.r5)+'</b></span>'+
    '<span><span class="k">+10日</span> <b class="'+cls(cur.r10)+'">'+pct(cur.r10)+'</b></span>'+
    '<span><span class="k">+20日</span> <b class="'+cls(cur.r20)+'">'+pct(cur.r20)+'</b></span>'+
    '<span><span class="k">至今報酬</span> <b class="'+cls(cur.rn)+'">'+pct(cur.rn)+'</b></span>'+
    '<span><span class="k">至今最高</span> <b class="'+cls(cur.hi)+'">'+pct(cur.hi)+'</b></span>'+
    '<span><span class="k">至今最低</span> <b class="'+cls(cur.lo)+'">'+pct(cur.lo)+'</b></span></div>'+
   '<div class="ohlc mono" id="ohlc"></div><div class="mas mono" id="mas"></div>'+
   '<div class="chartbox"><canvas id="cv"></canvas></div>'+
   '<div class="ranges">'+[[60,'60 日'],[120,'120 日'],[250,'250 日'],[9999,'全部']].map(function(r){return '<button type="button" class="btn'+(r[0]===rangeN?' on':'')+'" data-n="'+r[0]+'">'+r[1]+'</button>'}).join('')+'</div>'+
   '<div class="hist"><h3>這檔過去的訊號紀錄（報酬以訊號日收盤為基準）</h3><table><thead><tr><th>訊號日</th><th>類型</th><th>收盤</th><th>+5日</th><th>+10日</th><th>+20日</th><th>至今</th></tr></thead><tbody>'+rows+'</tbody></table></div>';
  var cv=$('#cv');
  cv.addEventListener('pointermove',onMove);cv.addEventListener('pointerdown',onMove);
  cv.addEventListener('pointerleave',function(){hov=null;draw();});
  Array.prototype.forEach.call(document.querySelectorAll('.ranges .btn'),function(b){b.onclick=function(){rangeN=+b.dataset.n;show(idx);}});
  draw();
}

var geo=null;
function onMove(e){
  if(!geo)return;var r=e.currentTarget.getBoundingClientRect(),x=e.clientX-r.left;
  var i=Math.floor((x-geo.L)/geo.bw)+geo.a;i=Math.max(geo.a,Math.min(geo.b,i));
  if(i!==hov){hov=i;draw();}
}
function info(i){
  var st=stock,cur=list[idx],c=st.cl[i],pc=i>0?st.cl[i-1]:null,ch=pc?c/pc-1:null;
  $('#ohlc').innerHTML='<b>'+D.dates[st.s+i]+'</b> &nbsp;開 '+fmt(st.o[i])+' 高 '+fmt(st.h[i])+' 低 '+fmt(st.l[i])+' 收 <b>'+fmt(c)+'</b> <span class="'+cls(ch)+'">'+pct(ch)+'</span> 量 '+Number(st.v[i]).toLocaleString()+' 張';
  var m=st._ma;
  $('#mas').innerHTML='<span style="color:#7e57c2">5MA '+fmt(m[5][i])+'</span> <span style="color:#f08c00">10MA '+fmt(m[10][i])+'</span> <span style="color:#d6247f">20MA '+fmt(m[20][i])+'</span> <span style="color:#2e9e4f">60MA '+fmt(m[60][i])+'</span> <span style="color:#3b5bdb">120MA '+fmt(m[120][i])+'</span>';
}

function draw(){
  var cv=$('#cv');if(!cv||!stock)return;
  var st=stock,cur=list[idx],n=st.cl.length,sbar=cur.di-st.s;
  var b=n-1,a=Math.max(0,b-Math.min(rangeN,n)+1);
  if(sbar<a)a=Math.max(0,sbar-40);
  var dpr=window.devicePixelRatio||1,W=cv.clientWidth,H=cv.clientHeight;
  cv.width=Math.round(W*dpr);cv.height=Math.round(H*dpr);
  var g=cv.getContext('2d');g.setTransform(dpr,0,0,dpr,0,0);g.clearRect(0,0,W,H);
  var cs=getComputedStyle(document.documentElement),UP=cs.getPropertyValue('--up').trim(),DN=cs.getPropertyValue('--dn').trim(),
      GRID=cs.getPropertyValue('--grid').trim(),MUTE=cs.getPropertyValue('--mute').trim(),ACC=cs.getPropertyValue('--accent').trim(),FG=cs.getPropertyValue('--fg').trim(),CARD=cs.getPropertyValue('--card').trim();
  var L=4,R=54,T=8,B=20,plotW=W-L-R,gap=10,totH=H-T-B,pH=Math.round(totH*.76),vH=totH-pH-gap,vTop=T+pH+gap;
  var cnt=b-a+1,bw=plotW/cnt;geo={L:L,bw:bw,a:a,b:b};
  var lo=1e18,hi=-1e18,vmax=0,MAC={5:'#7e57c2',10:'#f08c00',20:'#d6247f',60:'#2e9e4f',120:'#3b5bdb'};
  for(var i=a;i<=b;i++){lo=Math.min(lo,st.l[i]);hi=Math.max(hi,st.h[i]);vmax=Math.max(vmax,st.v[i]);
    [5,10,20,60,120].forEach(function(k){var v=st._ma[k][i];if(v!=null){lo=Math.min(lo,v);hi=Math.max(hi,v);}});}
  var padv=(hi-lo)*.06||1;lo-=padv;hi+=padv;
  var X=function(i){return L+(i-a+.5)*bw},Y=function(p){return T+(hi-p)/(hi-lo)*pH},YV=function(v){return vTop+vH-(vmax?v/vmax*vH:0)};
  g.font='11px ui-monospace,Menlo,Consolas,monospace';g.textBaseline='middle';
  var tk=niceTicks(lo,hi,5),dec=tk.step<1?(tk.step<.1?3:2):(tk.step<5?1:0);
  g.strokeStyle=GRID;g.lineWidth=1;g.fillStyle=MUTE;g.textAlign='left';
  tk.t.forEach(function(v){var y=Math.round(Y(v))+.5;g.beginPath();g.moveTo(L,y);g.lineTo(W-R,y);g.stroke();g.fillText(v.toFixed(Math.max(2,dec)),W-R+6,y);});
  g.beginPath();g.moveTo(L,vTop+vH+.5);g.lineTo(W-R,vTop+vH+.5);g.stroke();
  /* x labels */
  g.textAlign='center';g.textBaseline='top';var step=Math.max(1,Math.round(cnt/5)),lastM='';
  for(var i=a;i<=b;i+=step){var d=D.dates[st.s+i];g.fillText(d.slice(5),Math.min(Math.max(X(i),20),W-R-14),T+totH+4);}
  g.textBaseline='middle';
  /* volume + candles */
  var w=Math.max(1,Math.min(14,bw*.68));
  for(var i=a;i<=b;i++){
    var up=st.cl[i]>=st.o[i],col=up?UP:DN,x=X(i);
    g.fillStyle=col;g.globalAlpha=.55;g.fillRect(x-w/2,YV(st.v[i]),w,vTop+vH-YV(st.v[i]));g.globalAlpha=1;
    g.strokeStyle=col;g.lineWidth=1;g.beginPath();g.moveTo(Math.round(x)+.5,Y(st.h[i]));g.lineTo(Math.round(x)+.5,Y(st.l[i]));g.stroke();
    var y1=Y(st.o[i]),y2=Y(st.cl[i]);g.fillStyle=col;g.fillRect(x-w/2,Math.min(y1,y2),w,Math.max(1,Math.abs(y2-y1)));
  }
  /* MAs */
  [120,60,20,10,5].forEach(function(k){g.strokeStyle=MAC[k];g.lineWidth=1.6;g.beginPath();var on=false;
    for(var i=a;i<=b;i++){var v=st._ma[k][i];if(v==null){on=false;continue;}if(!on){g.moveTo(X(i),Y(v));on=true;}else g.lineTo(X(i),Y(v));}g.stroke();});
  /* last close */
  var lc=st.cl[b],ly=Math.round(Y(lc))+.5;
  g.setLineDash([4,3]);g.strokeStyle=ACC;g.beginPath();g.moveTo(L,ly);g.lineTo(W-R,ly);g.stroke();g.setLineDash([]);
  g.fillStyle=ACC;g.fillRect(W-R+1,ly-9,R-2,18);g.fillStyle='#fff';g.textAlign='left';g.fillText(lc.toFixed(2),W-R+5,ly);
  /* signal markers */
  (bySt[cur.si]||[]).forEach(function(s){var i=s.di-st.s;if(i<a||i>b)return;var x=X(i),y=Y(st.l[i])+6,isCur=s.di===cur.di;
    g.fillStyle=isCur?ACC:'#9aa3af';g.beginPath();
    if(isCur){g.moveTo(x,y);g.lineTo(x-6,y+11);g.lineTo(x-2.2,y+11);g.lineTo(x-2.2,y+20);g.lineTo(x+2.2,y+20);g.lineTo(x+2.2,y+11);g.lineTo(x+6,y+11);g.closePath();g.fill();
      g.font='bold 11px sans-serif';g.textAlign='left';g.fillText('訊號',Math.min(x+8,W-R-30),y+16);g.font='11px ui-monospace,Menlo,Consolas,monospace';}
    else{g.moveTo(x,y);g.lineTo(x-4,y+8);g.lineTo(x+4,y+8);g.closePath();g.fill();}});
  /* crosshair */
  var hi_=hov!=null?hov:Math.min(b,Math.max(a,sbar<=b?sbar:b));
  if(hov!=null){var cx=Math.round(X(hov))+.5;g.strokeStyle=MUTE;g.lineWidth=1;g.setLineDash([3,3]);g.beginPath();g.moveTo(cx,T);g.lineTo(cx,vTop+vH);g.stroke();g.setLineDash([]);}
  info(hi_);
}

/* ---------- controls ---------- */
function go(d){if(!list.length)return;if(idx+d<0||idx+d>=list.length)return;show(idx+d);}
$('#prev').onclick=function(){go(-1)};$('#next').onclick=function(){go(1)};
document.addEventListener('keydown',function(e){
  var t=e.target&&e.target.tagName;if(t==='INPUT'||t==='TEXTAREA')return;
  if(e.key==='ArrowUp'||e.key==='ArrowLeft'){e.preventDefault();go(-1);}
  else if(e.key==='ArrowDown'||e.key==='ArrowRight'){e.preventDefault();go(1);}
});
$('#onlyFirst').onchange=function(){buildList(true);show(idx);};
$('#jump').onchange=function(){
  var v=this.value;if(!v||!list.length)return;
  var di=-1;for(var i=0;i<D.dates.length;i++){if(D.dates[i]<=v)di=i;else break;}
  for(var k=0;k<list.length;k++){if(list[k].di<=di){show(k);if(D.dates[list[k].di]!==v)toast(v+' 無訊號，已跳到 '+D.dates[list[k].di]);return;}}
  toast('該日期之前沒有訊號');
};
$('#find').onkeydown=function(e){
  if(e.key!=='Enter')return;var q=this.value.trim();if(!q)return;
  for(var k=0;k<list.length;k++){var s=D.stocks[list[k].si];if(s.c===q||s.n===q){show(k);return;}}
  toast('清單中找不到 '+q);
};
window.addEventListener('resize',function(){if(stock)draw();});

/* ---------- init ---------- */
load().then(function(data){
  D=data;
  D.sigs.forEach(function(s,k){var o={di:s[1],close:s[2],first:s[4],streak:s[5],r5:s[6],r10:s[7],r20:s[8],rn:s[9]};(bySt[s[0]]=bySt[s[0]]||[]).push(o);});
  var m=D.meta;
  $('#meta').innerHTML='<span class="pill">資料收盤日 '+m.last_date+'</span>'+m.start_date+' 起共 '+m.n_signals.toLocaleString()+' 筆訊號（首次 '+m.n_first.toLocaleString()+' 筆）、'+m.n_stocks.toLocaleString()+' 檔個股；股票池 '+m.pool.toLocaleString()+' 檔。最新一日 '+m.last_n+' 檔符合（首次 '+m.last_first+' 檔）。頁面產生 '+m.generated+'（台北時間）。';
  if(DEMO)$('#demo').hidden=false;
  buildList(false);show(0);
}).catch(function(err){$('#card').innerHTML='<div class="empty">資料載入失敗：'+err.message+'</div>';});
})();
</script>
</body>
</html>
"""

def chips_html():
    cs = ["20MA &gt; 60MA &gt; 120MA",
          f"收盤突破前 {LOOKBACK} 日最高收盤",
          f"5、10、20MA 皆上揚，且 5MA 上升最快（{SLOPE_DAYS} 日斜率）"]
    if SHORT_ORDER:
        cs.append("5MA &gt; 10MA &gt; 20MA")
    return '<span class="plus">＋</span>'.join(f'<span class="chip">{c}</span>' for c in cs)


def render(payload, out=OUT, embed=EMBED, demo=DEMO):
    import os
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    gz = embed == "gzip"
    body = base64.b64encode(gzip.compress(raw.encode("utf-8"), 9)).decode() if gz else raw
    html = (TEMPLATE.replace("__CHIPS__", chips_html())
                    .replace("__GZ__", "true" if gz else "false")
                    .replace("__DEMO__", "true" if demo else "false")
                    .replace("__PAYLOAD__", body))
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✅ 已輸出 {out}（{len(html) / 1e6:.1f} MB）")

if __name__ == "__main__":
    # ========= 執行 =========
    import os, pickle
    if DEMO:
        uni, frames = make_demo()
    elif os.path.exists("frames.pkl") and "--fresh" not in sys.argv:
        uni, frames = pickle.load(open("frames.pkl", "rb"))
        print("使用快取 frames.pkl（刪除此檔可重新下載）")
    else:
        uni = fetch_universe();  print("股票池：", len(uni), "檔")
        frames = download_all(uni, FETCH_FROM)
        pickle.dump((uni, frames), open("frames.pkl", "wb"))
    payload = scan(uni, frames)
    m = payload["meta"]
    print(f"資料收盤日 {m['last_date']}｜{m['start_date']} 起 {m['n_signals']} 筆訊號（首次 {m['n_first']}）｜"
          f"{m['n_stocks']} 檔個股｜股票池 {m['pool']} 檔｜最新一日 {m['last_n']} 檔（首次 {m['last_first']}）")
    render(payload)

if __name__ == "__main__" and "--compare" in sys.argv:
    # ========= （選用）與貼文原作的數字比對 =========
    # 原作在 2026-10-22 的統計：8,817 筆訊號（首次 2,996）、1,233 檔、最新一日 23 檔（首次 5）。
    # 若你的數字差很多，代表第 3 條件的細節和原作不同；下面試幾種寫法，看哪個最接近，再回上面設定 SLOPE_DAYS / SHORT_ORDER。
    TARGET = dict(n_signals=8817, n_first=2996, n_stocks=1233, last_n=23, last_first=5)
    print("原作", TARGET)
    _keep = (SLOPE_DAYS, SHORT_ORDER)
    for sd, so in [(1, False), (1, True), (2, False), (2, True), (3, False), (3, True)]:
        SLOPE_DAYS, SHORT_ORDER = sd, so
        mm = scan(uni, frames)["meta"]
        print(f"SLOPE_DAYS={sd} SHORT_ORDER={so!s:5}｜{mm['n_signals']} 筆（首次 {mm['n_first']}）｜{mm['n_stocks']} 檔｜最新一日 {mm['last_n']} 檔（首次 {mm['last_first']}）")
    SLOPE_DAYS, SHORT_ORDER = _keep
