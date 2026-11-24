# -*- coding: utf-8 -*-
"""
A股尾盘选股推送脚本：只做信号提醒，不自动下单。
数据源：新浪（行情/板块/日线/分钟线），避开东财海外IP限流。

用法：
  python bot.py                正常运行（扫描+推送+信号留档）
  DRY_RUN=1 python bot.py      只打印不推送
  python bot.py --backtest     用历史日线回放规则，验证信号胜率
  python bot.py --backtest 601689,600519   只回测指定股票（6位代码即可）
  python bot.py --score        回填 signals.csv 里到期信号的实际收益，输出战绩
"""
import os
import sys
import csv
import time
import datetime
import akshare as ak
import pandas as pd
import requests

# ========== 参数区（想改规则只改这里） ==========
MA_SHORT = 5                  # 短均线
MA_MID = 10                   # 中均线
MA_LONG = 120                 # 半年线
VOL_RATIO = 1.5               # 今日成交量 / 近5日均量
TURNOVER_MIN = 3.0            # 换手率下限 %
TURNOVER_MAX = 12.0           # 换手率上限 %
PRICE_MIN = 3.0               # 最低股价
MIN_AMOUNT = 5e8              # 单只最低成交额（5亿）
MAX_DAY_PCT = 9.0             # 当日涨幅上限（防追高）
MIN_DAY_PCT = 1.5             # 当日涨幅下限（要红盘）
HIGH_20D_PCT = 30.0           # 近20日涨幅上限（防高位）
MARKET_AMOUNT_MIN = 8000e8    # 两市成交额门槛（8000亿）
TAIL_MINUTES = 15             # 尾盘急拉观察窗口（分钟）
TAIL_SURGE_PCT = 1.5          # 尾盘拉升幅度阈值 %
TAIL_SHARE = 0.6              # 尾盘拉升占当日涨幅比例阈值
MIN_LIST_DAYS = 60            # 上市不足60个交易日算次新
MAX_CANDIDATE = 400           # 进入日线复核的候选上限（防接口被打爆）

# ---- 回测（验证规则靠不靠谱，别让任何人替你拍脑袋） ----
BACKTEST_DAYS = 120           # 回放最近 N 个交易日
BACKTEST_POOL = 300           # 回测池：板块成分股按成交额取 top N（未指定股票时）
BACKTEST_SELL = (1, 3, 5)     # 信号后持有 N 个交易日的收益统计

# ---- 信号留档（自己的实盘战绩 + 任何人荐股都能记账） ----
# 手工跟踪外部荐股：往 signals.csv 里加一行，source 写来源（如某大V名字），
# 到日子 python bot.py --score 自动回填收益，数据说话，别被资本骗了。
SIGNAL_LOG = os.environ.get("SIGNAL_LOG", "signals.csv")
# ===============================================


def log(msg):
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def now_cn():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))


# ---------- 推送 ----------
def push(title, content):
    content = content[:3500]
    if os.environ.get("DRY_RUN"):
        log("DRY_RUN 模式，只打印不发送：\n" + title + "\n" + content)
        return True
    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    if token:
        try:
            r = requests.post("https://www.pushplus.plus/send",
                              json={"token": token, "title": title, "content": content, "template": "txt"},
                              timeout=15)
            log(f"pushplus 返回: {r.status_code} {r.text[:120]}")
            return r.status_code == 200
        except Exception as e:
            log(f"pushplus 失败: {e}")
            return False
    key = os.environ.get("SENDKEY", "").strip()
    if key:
        try:
            r = requests.post(f"https://sctapi.ftqq.com/{key}.send",
                              data={"title": title, "desp": content}, timeout=15)
            log(f"serverchan 返回: {r.status_code}")
            return r.status_code == 200
        except Exception as e:
            log(f"serverchan 失败: {e}")
            return False
    log("未配置 PUSHPLUS_TOKEN 或 SENDKEY，无法推送")
    return False


def push_fail(reason):
    push("【脚本告警】行情获取失败",
         f"时间：{now_cn():%Y-%m-%d %H:%M}\n原因：{reason}\n"
         "今日未产生信号（不是没有符合条件的股票，是数据没拿到）。请检查数据源/网络。")


# ---------- 行情 ----------
def get_sina_sector_map():
    """新浪行业板块：一次拿板块涨跌幅 + 成分股（含换手率）。"""
    s = ak.stock_sector_spot(indicator="新浪行业")
    boards = s.set_index("label")["涨跌幅"].astype(float).to_dict()
    names = s.set_index("label")["板块"].to_dict()
    frames = []
    for lab in s["label"]:
        for attempt in range(2):
            try:
                d = ak.stock_sector_detail(sector=lab)
                d["板块代码"] = lab
                frames.append(d)
                break
            except Exception as e:
                if attempt == 1:
                    log(f"板块 {lab} 跳过: {e}")
                time.sleep(0.5)
        time.sleep(0.15)
    if not frames:
        raise RuntimeError("板块成分股全部获取失败")
    m = pd.concat(frames, ignore_index=True)
    m["板块涨跌幅"] = m["板块代码"].map(boards)
    m["板块名称"] = m["板块代码"].map(names)
    return m


def sina_symbol(code):
    """6位纯代码补新浪市场前缀：6→sh，0/3→sz，8/4/920→bj。"""
    code = str(code).strip()
    if code[:2] in ("sh", "sz", "bj"):
        return code
    if code.startswith("6"):
        return "sh" + code
    if code.startswith(("0", "3")):
        return "sz" + code
    return "bj" + code


def get_daily(symbol, start):
    for attempt in range(2):
        try:
            d = ak.stock_zh_a_hist_sina(symbol=symbol, start_date=start,
                                        end_date=now_cn().strftime("%Y%m%d"), adjust="")
            return d
        except Exception:
            time.sleep(0.4)
    return None


def tail_surge(symbol, day_pct):
    """返回 True 表示尾盘急拉（应剔除）。"""
    try:
        m = ak.stock_zh_a_hist_min_sina(symbol=symbol, period="1")
    except Exception as e:
        log(f"  {symbol} 分钟线失败，跳过急拉校验: {e}")
        return False
    today = now_cn().strftime("%Y-%m-%d")
    m = m[m["day"].astype(str).str.startswith(today)]
    if len(m) < 20:
        return False
    m = m.copy()
    m["t"] = m["day"].astype(str).str[-8:-3]
    base_rows = m[m["t"] <= f"14:{60 - TAIL_MINUTES:02d}"]
    if base_rows.empty:
        return False
    base = float(base_rows["close"].iloc[-1])
    last = float(m["close"].iloc[-1])
    if base <= 0:
        return False
    p = (last - base) / base * 100
    if p > TAIL_SURGE_PCT and p > TAIL_SHARE * day_pct:
        log(f"  {symbol} 尾盘{TAIL_MINUTES}分钟拉升 {p:.2f}%（当日{day_pct:.2f}%），剔除")
        return True
    return False


# ---------- 规则核心（实盘/回测共用同一套，防止两套口径自己骗自己） ----------
def rule_on_history(closes, vols, i):
    """在历史第 i 天（含 i）的切片上跑价格/量能规则，True=信号触发。
    换手率/板块同步/ST 属当日快照过滤，历史无法回放，回测口径见 README。"""
    if i < MA_LONG or i < 21:
        return False
    c = closes[:i + 1]
    v = vols[:i + 1]
    ma_s = sum(c[-MA_SHORT:]) / MA_SHORT
    ma_m = sum(c[-MA_MID:]) / MA_MID
    ma_l = sum(c[-MA_LONG:]) / MA_LONG
    if ma_s <= ma_m:                     # 5日线必须在10日线上方
        return False
    if c[-1] < ma_m or c[-1] < ma_l:     # 站上10日线和半年线
        return False
    avg5 = sum(v[-6:-1]) / 5
    if avg5 <= 0 or v[-1] / avg5 < VOL_RATIO:   # 放量
        return False
    p20 = (c[-1] / c[-21] - 1) * 100
    if p20 > HIGH_20D_PCT:               # 阶段高位
        return False
    day_pct = (c[-1] / c[-2] - 1) * 100
    if not (MIN_DAY_PCT <= day_pct <= MAX_DAY_PCT):   # 要红盘但不追高
        return False
    return True


# ---------- 信号留档 ----------
LOG_FIELDS = (["date", "code", "name", "price", "pct", "turnover", "amount",
              "vol_x", "p20", "board", "source"]
             + [f"ret_{n}d" for n in BACKTEST_SELL] + ["scored"])


def log_signals(final, total_amount):
    """把今日信号追加到 CSV（同日同票不重复），workflow 会把它提交回仓库。"""
    today = now_cn().strftime("%Y-%m-%d")
    old = set()
    if os.path.exists(SIGNAL_LOG):
        with open(SIGNAL_LOG, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                old.add((r["date"], r["code"]))
    new = [r for r in final if (today, r["code"]) not in old]
    if not new:
        log("无新信号需要留档")
        return
    header = not os.path.exists(SIGNAL_LOG)
    with open(SIGNAL_LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if header:
            w.writeheader()
        for r in new:
            w.writerow(dict(date=today, code=r["code"], name=r["name"],
                            price=f"{r['price']:.2f}", pct=f"{r['pct']:.2f}",
                            turnover=f"{r['turnover']:.2f}", amount=f"{r['amount']:.2f}",
                            vol_x=f"{r['vol_x']:.2f}", p20=f"{r['p20']:.2f}",
                            board=r["board"], source="bot",
                            ret_1d="", ret_3d="", ret_5d="", scored=""))
    log(f"已留档 {len(new)} 条信号到 {SIGNAL_LOG}")


def score_log():
    """回填到期信号的实际收益（以信号日收盘为基准），并按来源统计战绩。"""
    if not os.path.exists(SIGNAL_LOG):
        log(f"没有找到 {SIGNAL_LOG}，先跑几天留档再说")
        return
    df = pd.read_csv(SIGNAL_LOG, dtype=str).fillna("")
    if df.empty:
        log("signals.csv 是空的")
        return
    start = (now_cn() - datetime.timedelta(days=400)).strftime("%Y%m%d")
    max_hold = max(BACKTEST_SELL)
    changed = False
    for idx, row in df.iterrows():
        if row["scored"] == "1":
            continue
        d = get_daily(sina_symbol(row["code"]), start)
        if d is None or d.empty:
            continue
        d = d.copy()
        d["date"] = d["date"].astype(str).str[:10]
        d = d[d["date"] > row["date"]]        # 信号日之后的交易日
        if len(d) < max_hold:                  # 还没到期，下次再填
            continue
        closes = d["close"].astype(float).tolist()
        base = float(row["price"])
        if base <= 0:
            continue
        for n in BACKTEST_SELL:
            df.at[idx, f"ret_{n}d"] = f"{(closes[n - 1] / base - 1) * 100:.2f}"
        df.at[idx, "scored"] = "1"
        changed = True
        time.sleep(0.15)
    if changed:
        df.to_csv(SIGNAL_LOG, index=False, encoding="utf-8")
        log(f"已回填并写回 {SIGNAL_LOG}")

    done = df[df["scored"] == "1"]
    if done.empty:
        log("还没有到期的信号，暂无战绩")
        return
    lines = [f"信号战绩  {now_cn():%Y-%m-%d}", ""]
    for src, g in done.groupby("source"):
        lines.append(f"== {src}（{len(g)} 条已到期）==")
        for n in BACKTEST_SELL:
            r = pd.to_numeric(g[f"ret_{n}d"], errors="coerce").dropna()
            if len(r):
                lines.append(f"  持有{n}日: 胜率 {(r > 0).mean() * 100:.0f}%  "
                             f"平均 {r.mean():+.2f}%  最差 {r.min():+.2f}%  最好 {r.max():+.2f}%")
    report = "\n".join(lines)
    log("\n" + report)
    push("信号战绩更新", report)


# ---------- 回测 ----------
def backtest(symbols=None):
    """用历史日线回放规则：信号日收盘买入，持有 N 日收盘卖出，统计胜率。
    连续触发只记第一次（冷却期=最长持有期，近似实盘不重复加仓）。"""
    days_needed = BACKTEST_DAYS + MA_LONG + 30
    start = (now_cn() - datetime.timedelta(days=int(days_needed * 1.6))).strftime("%Y%m%d")
    if symbols:
        pool = [sina_symbol(s) for s in symbols if s.strip()]
    else:
        log("未指定股票，按板块成分股成交额取回测池（要几分钟）...")
        m = get_sina_sector_map()
        m["amount"] = pd.to_numeric(m["amount"], errors="coerce")
        pool = (m.drop_duplicates("code").sort_values("amount", ascending=False)
                .head(BACKTEST_POOL)["code"].tolist())
        pool = [sina_symbol(c) for c in pool]
    log(f"回测池 {len(pool)} 只，回放近 {BACKTEST_DAYS} 个交易日")

    trades = []
    for k, code in enumerate(pool, 1):
        d = get_daily(code, start)
        if d is None or len(d) < MA_LONG + 25:
            continue
        closes = d["close"].astype(float).tolist()
        vols = d["volume"].astype(float).tolist()
        cooldown = 0
        lo = max(MA_LONG, len(closes) - max(BACKTEST_SELL) - BACKTEST_DAYS)
        for i in range(lo, len(closes) - max(BACKTEST_SELL)):
            if cooldown > 0:
                cooldown -= 1
                continue
            if rule_on_history(closes, vols, i):
                rec = {"code": code}
                for n in BACKTEST_SELL:
                    rec[f"ret_{n}d"] = (closes[i + n] / closes[i] - 1) * 100
                trades.append(rec)
                cooldown = max(BACKTEST_SELL)
        if k % 50 == 0:
            log(f"  进度 {k}/{len(pool)}，已记录 {len(trades)} 次信号")
        time.sleep(0.15)

    if not trades:
        log("回测期内没有任何信号触发")
        return
    df = pd.DataFrame(trades)
    lines = [f"回测结果：{df['code'].nunique()} 只股票共 {len(df)} 次信号（近{BACKTEST_DAYS}交易日回放）"]
    for n in BACKTEST_SELL:
        r = df[f"ret_{n}d"]
        lines.append(f"  持有{n}日: 胜率 {(r > 0).mean() * 100:.0f}%  平均 {r.mean():+.2f}%  "
                     f"中位 {r.median():+.2f}%  最差 {r.min():+.2f}%  最好 {r.max():+.2f}%")
    lines.append("口径说明：只回放价格/量能规则（均线/放量/涨幅/位置），"
                 "换手率/板块同步/ST 属当日过滤未回放；未计手续费印花税。")
    report = "\n".join(lines)
    log("\n" + report)
    push("规则回测报告", report)


# ---------- 主流程 ----------
def run():
    log("=== 开始扫描 ===")

    # 1. 全市场快照（用于大盘成交额门槛）
    try:
        spot = ak.stock_zh_a_spot_sina()
    except Exception as e:
        push_fail(f"新浪全市场行情获取失败：{e}")
        return
    total_amount = pd.to_numeric(spot["成交额"], errors="coerce").sum()
    log(f"两市成交额约 {total_amount/1e8:.0f} 亿（门槛 {MARKET_AMOUNT_MIN/1e8:.0f} 亿）")
    if total_amount < MARKET_AMOUNT_MIN:
        log("成交额不足门槛，今日不选股、不推送")
        return

    # 2. 板块+成分股（含换手率）
    try:
        m = get_sina_sector_map()
    except Exception as e:
        push_fail(f"板块数据获取失败：{e}")
        return
    log(f"板块覆盖股票 {m['code'].nunique()} 只")

    for col in ["trade", "changepercent", "amount", "turnoverratio", "volume"]:
        m[col] = pd.to_numeric(m[col], errors="coerce")
    m = m.dropna(subset=["trade", "changepercent", "amount", "turnoverratio"])

    # 3. 初筛（一次搞定大部分硬性过滤）
    cond = (
        m["changepercent"].between(MIN_DAY_PCT, MAX_DAY_PCT)
        & (m["trade"] >= PRICE_MIN)
        & (m["amount"] >= MIN_AMOUNT)
        & m["turnoverratio"].between(TURNOVER_MIN, TURNOVER_MAX)
        & m["板块涨跌幅"].fillna(-1) > 0        # 板块同步上涨
        & ~m["name"].str.contains("ST|退|停", na=False)
        & ~m["symbol"].str.startswith("bj")
    )
    m = m[cond].copy()
    m = m.drop_duplicates(subset=["code"])
    log(f"初筛通过 {len(m)} 只（含板块同步/换手率/涨幅/成交额）")
    if m.empty:
        log("无候选，今日不推送")
        return
    m = m.sort_values("amount", ascending=False).head(MAX_CANDIDATE)

    # 4. 逐只日线复核：均线多头、放量、高位、次新
    start = (now_cn() - datetime.timedelta(days=260)).strftime("%Y%m%d")
    results = []

    def check(row):
        sym = row["symbol"]
        d = get_daily(sym, start)
        if d is None or len(d) < MIN_LIST_DAYS:
            return None
        d = d.copy()
        # 新浪日线当日收盘后才更新，尾盘需把今天的实时价/量补进去
        today_close = float(row["trade"])
        today_vol = float(row["volume"]) if row["volume"] > 0 else float(d["volume"].iloc[-1])
        closes = d["close"].astype(float).tolist() + [today_close]
        vols = d["volume"].astype(float).tolist() + [today_vol]
        if len(closes) < MA_LONG:
            return None

        ma_s = sum(closes[-MA_SHORT:]) / MA_SHORT
        ma_m = sum(closes[-MA_MID:]) / MA_MID
        ma_l = sum(closes[-MA_LONG:]) / MA_LONG
        if ma_s <= ma_m:                                 # 5日线必须在10日线上方
            return None
        if today_close < ma_m or today_close < ma_l:     # 站上10日线和半年线
            return None
        avg5 = sum(vols[-6:-1]) / 5                      # 前5日均量（不含今天）
        if avg5 <= 0 or today_vol / avg5 < VOL_RATIO:    # 不放量
            return None
        p20 = (today_close / closes[-21] - 1) * 100      # 近20个交易日涨幅
        if p20 > HIGH_20D_PCT:                           # 阶段高位
            return None
        turnover = float(row["turnoverratio"])
        return dict(code=row["code"], name=row["name"], price=today_close,
                    pct=float(row["changepercent"]), turnover=turnover,
                    amount=float(row["amount"]) / 1e8, board=row.get("板块名称", ""),
                    board_pct=float(row["板块涨跌幅"]), p20=p20,
                    vol_x=today_vol / avg5, symbol=sym)

    for _, row in m.iterrows():
        r = check(row)
        if r:
            results.append(r)
        time.sleep(0.15)   # 串行+间隔，避免新浪封IP与py_mini_racer并发崩溃
    log(f"日线复核通过 {len(results)} 只")

    # 5. 尾盘急拉过滤（只对通过日线的少量股票做）
    final = []
    for r in results:
        if tail_surge(r["symbol"], r["pct"]):
            continue
        final.append(r)
    log(f"最终符合条件 {len(final)} 只")

    if not final:
        log("今日无信号，不推送")
        return

    # 5.5 信号留档（战绩可查证，谁的话都别盲信）
    log_signals(final, total_amount)

    # 6. 组装推送
    lines = [f"A股尾盘信号  {now_cn():%Y-%m-%d %H:%M}",
             f"两市成交额 {total_amount/1e8:.0f} 亿｜符合条件 {len(final)} 只", ""]
    for i, r in enumerate(final, 1):
        lines.append(
            f"{i}. {r['name']}({r['code']})  {r['price']:.2f}元  涨{r['pct']:.2f}%  "
            f"换手{r['turnover']:.1f}%  额{r['amount']:.1f}亿  量比{r['vol_x']:.1f}x  "
            f"20日涨{r['p20']:.0f}%  板块:{r['board']}({r['board_pct']:+.1f}%)")
    lines += ["",
              "【风控铁律·必须自己执行】",
              "1. 总试水资金上限500元，亏完立刻停用，绝不追加捞本",
              "2. 单票≤总资金7%，单笔亏5%无条件止损，不扛单",
              "3. 账户一周回撤8%，本周停止交易",
              "4. 本信号只是形态筛选，主力会画K线诱多，必须人工复核后再决定",
              "5. 脚本只提醒，从不下单",
              "6. 信号历史战绩见 signals.csv，跑 python bot.py --score 查看"]
    push(f"A股尾盘信号 {len(final)}只", "\n".join(lines))
    log("推送完成")


if __name__ == "__main__":
    args = list(sys.argv[1:])
    try:
        if "--backtest" in args:
            codes = None
            for a in args:
                if a != "--backtest":
                    codes = a.split(",")
            backtest(codes)
        elif "--score" in args:
            score_log()
        else:
            run()
    except Exception as e:
        import traceback
        traceback.print_exc()
        push_fail(f"脚本异常：{type(e).__name__}: {e}")
        sys.exit(1)
