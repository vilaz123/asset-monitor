# 资产低水位监控器

盯 22 个资产（A股宽基/行业ETF/港美股/金银油/BTC/ETH），当出现"低水位"信号时提醒你，
专治"回头一看才发现当初是个坑底"。**本工具只做提醒，不做投资建议。**

## 信号规则（2/4 触发）

| 条件 | 含义 |
|---|---|
| S1 深度回撤 | 现价距 250 日最高收盘回撤 ≥ 阈值（指数20%/黄金12%/白银25%/油30%/BTC35%/ETH40%） |
| S2 RSI 超卖 | 14 日 Wilder RSI < 30（加密 25） |
| S3 跌破 MA200 | 现价低于 200 日均线 ≥ 阈值幅度 |
| S4 3年低百分位 | 现价处于近 3 年（750 交易日）收盘价最低 25% 区间 |

四个条件中 **≥2 个满足即触发"低水位"**。盘中 S1/S3 用实时价，S2/S4 只用已完成日K。
单条件接近但未触发 → 仅 Mac 弹"接近"提醒（不打微信）。

## 技术分析（看板卡片内点开"技术分析"）

每个信号资产附带一套完整技术面（只用已完成日K，与主图口径一致）：

| 维度 | 内容 |
|---|---|
| 均线 | MA20/60/200 及排列（多头/空头/纠缠；不足200根降级为 MA20上下方） |
| MACD | DIF/DEA/柱（国内口径柱=2×(DIF−DEA)），近5日金叉/死叉、红绿柱扩大/收窄 |
| KDJ | K/D/J + 超买超卖区、近3日金叉死叉 |
| BOLL | 20日±2σ 上下轨、%B 位置、带宽及其近半年分位（带宽历史低位=变盘窗口） |
| 动量 | 20日/60日涨跌幅 |
| 52周 | 区间位置%、距52周高点%、高于低点% |
| 波动 | ATR14 日波幅% 及年化估计 |
| 量能 | 5日/20日量比（放量≥1.5 / 缩量≤0.7） |

微信低水位提醒与每日日报同样带一行技术面摘要（如"空头排列 · MACD金叉2日 · KDJ超卖"）。

## 技术面结论（小白友好，一句话看懂）

技术面之上，每张卡片再给一句**纯技术分析的综合状态结论**——只描述技术面组合出什么状态，
**不含任何买卖/仓位建议**，由固定规则推导：

| 结论标签 | 技术面状态 |
|---|---|
| **低位·趋势向好** | 已进低水位区 + 均线多头排列（「便宜+走强」） |
| **低位·现止跌迹象** | 已进低水位区 + MACD绿柱收窄/KDJ金叉（「便宜+企稳迹象」） |
| **低位·跌势未止** | 已进低水位区但均线仍偏空（「便宜但短期仍弱」） |
| **接近低位** | 接近低水位门槛，处于临界状态 |
| **趋势强·不在低位** | 均线多头排列但价格不便宜（「强势但偏贵」） |
| **不在低位** | 各项指标均未进入低水位区 |

微信低水位提醒末尾带 **技术面结论【标签】**，日报每个资产带「标签」。

## 看板卡片怎么读

- **水位条**：条越短=价格越便宜（低水位），越长=越贵；竖刻度线=25% 低水位区门槛；
  标签如"水位 3% · 近3年分位"。上市不足3年的资产自动降级用 52周位置。
- **S1~S4 圆点**：实心=该条件满足，空心=不满足；≥2 实心即触发低水位。
- **技术分析**：点开 `<details>` 看均线/MACD/KDJ/BOLL/动量/52周/ATR/量比明细。

## 提醒逻辑

- **微信（主通道）**：Server酱，每轮运行所有触发合并成 1 条；每日上限 4 条（免费额度5条留余量）
- **Mac 通知**：低水位每资产 1 条（上限3+1合并）、接近合并 1 条
- **重报规则**：信号持续时不会每 7 天轰炸——冷却 7 天到期 **且价格比上次提醒又低 ≥5%** 才重报
- **信号消失**：连续 2 个新交易日不满足 → 状态复位，下次触发重新首报
- **每日水位日报**：20:00 后第一轮运行推送全部资产水位一览（config 可关）

## 首次使用（必做 2 步）

1. **绑微信**：手机微信扫码 [sct.ftqq.com](https://sct.ftqq.com) → 复制 SendKey →
   填入 `config.json` 第 10 行 `"key": "SCTxxxxx"` 并把 `"enabled"` 改为 `true` →
   验证：`/usr/bin/python3 monitor.py notify --test --wechat`
2. **Mac 通知权限**：首次弹系统通知时若询问，点"允许"（终端 App 通知权限）

不绑微信也能用：所有提醒会走 Mac 通知兜底。

## 日常使用

```bash
# 看板（双击打开或）：每30分钟自动重新生成
open ~/Projects/asset-monitor/dashboard.html

# 逐资产体检（价格/接口全绿检查）
/usr/bin/python3 monitor.py doctor

# 手动跑一轮
/usr/bin/python3 monitor.py run

# 5年回测（验证阈值、看历史触发点后续收益）
/usr/bin/python3 monitor.py backtest --report bt.md

# 阈值网格搜索（调参用，如黄金档回撤 8/10/12/15 对比）
/usr/bin/python3 monitor.py backtest --sweep "dd=8,10,12,15"
```

后台由 launchd 驱动（`com.vz.asset-monitor`，每 30 分钟 + 开机即跑）：

```bash
launchctl list | grep asset-monitor          # 查看状态
tail -50 ~/Library/Logs/asset-monitor.log    # 看运行日志
launchctl kickstart gui/$(id -u)/com.vz.asset-monitor   # 立即触发一轮
launchctl bootout gui/$(id -u)/com.vz.asset-monitor     # 停用
```

## 自定义（都在 config.json，可在手机端网页改）

- **加自选股/基金**：手机 edit.html 一键添加（见上节），或在 `assets` 数组加一行，如
  `{"id": "sh600519", "name": "贵州茅台", "group": "自选", "source": "tencent", "class": "stock", "tz": "Asia/Shanghai", "expected_range": [1000, 2500]}`
- **删资产**：手机 edit.html 点删除，或从 `assets` 删对应行
- **调灵敏度**：`thresholds_class` 各档阈值（调前先跑 backtest --sweep 看触发密度）
- **日报开关**：`daily_digest: false`
- `expected_range` 是防呆护栏：接口异常返回离谱价格时自动报"数据异常"而不是发假提醒

## 已知口径与坑（读到就是赚到）

- **纳指100 用 QQQ ETF 代理**（腾讯对美股指数只给1根K线）；标普500 放"观察位"仅展示实时价
- **LOF/QDII**（白酒161725/白银161226/南方原油501018）场内价含折溢价，信号可能比净值口径早/晚几天
- **前复权(qfq)历史会在除权日整体改写**，程序每轮全量替换缓存，不做增量追加（防错序）
- **eastmoney 观察位行情**：主域 `push2` 偶发整体失效，自动切 `push2delay`（延迟行情，观察位够用）；
  f43 缩放倍率随主机漂移，程序用 expected_range 自动纠偏
- **Mac 睡眠**：定时器停、唤醒后 launchd 补跑一轮。低水位是日线级慢信号，睡几小时无实际影响
- **网络**：全部数据源（腾讯/东财/gate.io）走直连，不依赖系统代理；Clash 开不开都不影响
- **Server酱特例**：本机 Clash TUN 把 DNS 劫持成 fake-ip（198.18.x），sctapi 流量进 Clash 后
  反而出不去（直连/走代理都超时）→ 程序把 `sctapi.ftqq.com` 钉到真实 IP（代码内置
  82.157.177.201，config `"dns_pin"` 可覆盖），TLS 证书校验不受影响；若腾讯换 IP 导致
  发送失败，nslookup sctapi.ftqq.com 223.5.5.5 查新 IP 填进 dns_pin 即可
- **微信额度**：Server酱免费 5 条/天，本工具限 4 条。要更多可换 pushplus / 企业微信机器人（config 里预留）
- **微信排版**：Server酱会把单个换行折叠成空格 → 消息里段落间用双换行、
  行级内容用 markdown 列表项（`- `），日报按 低水位/接近/其余 分组

## 文件

| 文件 | 作用 |
|---|---|
| `monitor.py` | 全部逻辑（数据源/指标/信号/通知/看板/回测） |
| `config.json` | 资产表+阈值+渠道密钥（一切可调项） |
| `state.json` | 运行状态（冷却/快照/提醒历史），含 .bak 双保险 |
| `dashboard.html` | 水位看板，纯静态，亮/暗自动适配 |
| `backtest-report.md` | 最近一次回测报告 |
| `com.vz.asset-monitor.plist` | launchd 定义（已装至 ~/Library/LaunchAgents/） |

## 手机一键增删资产（已上线）

看板页头点 **⚙️ 管理资产**，或直接打开
**https://vilaz123.github.io/asset-monitor/edit.html**

- 选中资产点删除 / 填代码点添加（支持快捷模板、代码格式校验）
- 提交 → 写入 GitHub 仓库的 `config.json` → Mac 下一轮运行自动 `git pull` 生效（≤30分钟）
- 引擎侧双重防护：远端 config 结构校验失败则拒绝合并不影响监控；代码填错的资产只会在看板显示"数据异常"，删掉即可

**首次使用需配一次 GitHub 令牌**（浏览器存本机，只授权这一个仓库）：
github.com/settings/personal-access-tokens/new → Repository access 选 *Only select repositories → asset-monitor* → Permissions → Contents → *Read and write* → 生成后粘贴进页面。

## 云端看板（GitHub Pages，已上线）

每轮运行后自动把 `dashboard.html` 推到 [vilaz123/asset-monitor](https://github.com/vilaz123/asset-monitor)
的 `gh-pages` 分支，手机可打开：**https://vilaz123.github.io/asset-monitor/**

- 仓库 main 分支 = 引擎 + 公开 config.json + edit.html；**`secrets.json`（含 SendKey）和 `state.json` 被 gitignore，永远不出本机**（load_config 自动叠加 secrets 的渠道配置）
- 提交身份用 GitHub noreply 邮箱（仓库级 git config），不暴露真实邮箱
- 公开仓库 + gh-pages 分支 = Pages 自动激活，无需 API/gh 登录；推送走本机 SSH key
- 内容无变化时跳过提交；推送失败只记日志，不影响本地监控与通知
- 微信日报末尾的看板链接即此 URL
- 关闭云端发布：config `"publish": {"enabled": false}`

## v2 云端化（预留）

指标与信号层是纯函数、无 Mac 依赖；config/state 均为可序列化 JSON。后续可平移到
Vercel/CF Workers 定时任务 + PWA，分享链接给他人自助增删资产（各自绑自己的 Server酱/Bark）。
