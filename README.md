# stock-signal-bot

A股尾盘选股信号推送。**只做信号提醒，不自动下单。**

## 它做什么

交易日尾盘扫一遍全A，把同时满足下面条件的股票列出来，推到微信：

- 5日均线在10日均线上方，股价站上10日线和半年线（120日线）
- 当日成交量 ≥ 近5日均量的 1.5 倍（放量）
- 换手率 3%~12%
- 当日涨幅 1.5%~9%（要红盘，但不追高）
- 单只成交额 ≥ 5亿（有流动性）
- 所属板块同步上涨（不是孤零零一只在唱戏）
- 近20日涨幅 ≤ 30%（不碰已经涨完的高位票）
- 剔除 ST / 退市 / 停牌 / 北交所 / 上市不足60个交易日的次新股
- 剔除尾盘最后15分钟突然拉起来的（防对倒画K线诱多）

大盘沪深两市成交额低于 **8000亿** 时，直接不选股、不推送。

## 新功能：回测与战绩留档

信号好不好，历史数据说了算，别听任何人拍脑袋：

```bash
python bot.py --backtest              # 回放近120个交易日，验证规则胜率（默认取成交额top300池）
python bot.py --backtest 601689,600519  # 只回测指定股票
python bot.py --score                 # 回填 signals.csv 里到期信号的实际收益，输出战绩
```

- 每次推送的信号自动记入 `signals.csv`（同日同票不重复），workflow 每天跑完自动回填收益并提交回仓库。
- **跟踪外部荐股**：任何人（大V、群里"老师"、朋友）推荐的票，手工往 `signals.csv` 加一行，`source` 列写来源名字，到期 `--score` 自动算他推的票真实赚没赚。战绩按来源分组统计。
- 回测口径：只回放价格/量能规则（均线/放量/涨幅/位置），换手率/板块同步/ST 属当日快照过滤无法历史回放；未计手续费印花税。回测是必要非充分验证，实盘前自己掂量。

## 数据源

全部走新浪：`stock_zh_a_spot_sina`（全市场快照/成交额）、`stock_sector_spot` + `stock_sector_detail`（板块涨跌幅+成分股+换手率）、`stock_zh_a_hist_sina`（日线算均线）、`stock_zh_a_hist_min_sina`（1分钟线查尾盘急拉）。

**不用东财接口**：东财会按 IP 限流，GitHub 海外节点经常直接 503，整条链路会静默失败。

## 部署（GitHub Actions）

1. 仓库 Settings → Secrets and variables → Actions，新增：
   - `PUSHPLUS_TOKEN`（推荐，pushplus 微信推送）
   - `SENDKEY`（可选，Server酱 的 SendKey，二者填一个即可）
2. Actions 页面点 `run stock bot` → Run workflow 手动跑一次
3. 定时已配好：工作日 14:40（北京时间）自动跑，GitHub 定时通常有几到十几分钟延迟

## 本地跑

```bash
pip install -r requirements.txt
export PUSHPLUS_TOKEN=你的token
python bot.py
```

只想看结果不推送：`DRY_RUN=1 python bot.py`

## 注意

- 脚本只输出候选池，买不买、买多少要人决定
- 单票仓位 ≤ 总资金7%，单笔亏5%无条件止损，账户一周回撤8%停手
- 主力会画K线骗指标，信号必须人工复核
- 数据源只覆盖新浪行业分类里的约3000只，不是全市场5000+
