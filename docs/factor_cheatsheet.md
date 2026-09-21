# 量化因子 & 策略 速查表

## 一、有效因子（通过IC + 回测验证，进入生产）

| 符号 | 中文名 | 方向 | 含义 | 一句话 |
|------|--------|------|------|--------|
| `amount_log` | 对数成交额 | -1 | ln(日均成交额, 20日) | 越小越好 = 小盘冷门股 |
| `fa_cfp` | 现金流市值比 | +1 | 每股经营现金流 / 股价 | 越大越好 = 现金流便宜 |
| `fa_bp` | 账面市值比 | +1 | 每股净资产 / 股价 | 越大越好 = 低市净率 |
| `fa_sp` | 营收市值比 | +1 | 每股营收 / 股价 | 越大越好 = 营收便宜 |
| `fa_ep` | 盈利市值比 | +1 | 每股收益 / 股价 | 越大越好 = 低市盈率 |
| `vol_expansion` | 量能变化 | -1 | SMA(量,5) / SMA(量,20) | 越小越好 = 缩量整理 |
| `mf_big_ratio` | 大单买入占比 | -1 | 大单买入额 / 总成交额 | 越小越好 = 避开散户追涨股 |

## 二、策略组合（AND 交集法）

| 符号 | 组成 | 适用 | 收益 | 逻辑 |
|------|------|------|------|------|
| `dual_al_cfp` | amount_log ∩ fa_cfp | risk-on | +2.73%/20日 | 小票 + 现金流好 |
| `triple_and` | amount_log ∩ fa_cfp ∩ fa_bp | neutral | +3.27%/20日 | 小票 + 现金流 + 低估 |
| `mf_al_ve` | mf_big_ratio ∩ amount_log ∩ vol_expansion | risk-off | +3.04%/20日 | 冷门 + 小票 + 缩量 |
| `dual_mf_al` | mf_big_ratio ∩ amount_log | 通用 | +2.42%/20日 | 冷门小票（最简二因子） |
| `dual_al_cfp` | amount_log ∩ fa_cfp | 通用 | — | 同上，别名 |

## 三、宏观状态

| 状态 | 含义 | 触发条件 | 当前策略 |
|------|------|---------|---------|
| risk-on | 风险偏好 | 上证20日涨 >1% | dual_al_cfp |
| neutral | 中性震荡 | -1% ~ +1% | triple_and |
| risk-off | 风险规避 | 上证20日跌 >1% | mf_al_ve |

## 四、已测无效的因子（不用记，知道它们不行就行）

| 大类 | 因子 | 结论 |
|------|------|------|
| 动量 | mom_20d, mom_60d, reversal_5d, rsi_14 | A股动量=反指，全灭 |
| 波动率 | vol_20d, vol_60d, downside_vol | A股低波=毒药，全灭 |
| 流动性 | amihud_20d, turnover_cv | 无效 |
| 资金流 | mf_net_5d, mf_net_20d | 噪声 |
| 技术 | breakout_20d, ma_alignment, range_position | 全灭 |
| 风险 | max_dd_60d, up_day_ratio | 全灭 |
| 质量 | fa_roe, 毛利率, 净利率等 | A股高ROE=韭菜 |
| 成长 | 营收/利润/权益增长率 | A股高增长=反指 |

## 五、常用缩写

| 缩写 | 全称 |
|------|------|
| AND | 多因子交集（所有条件同时满足） |
| IC | Information Coefficient（因子值与未来收益的秩相关） |
| IC_IR | IC / std(IC)，衡量信号稳定性 |
| t值 | 利差 / 标准误，>2=显著，>3=强 |
| PIT | Point-in-Time（回测时不使用未来数据） |
| 利差 | Top组收益 - Bottom组收益 |
| lag | 财报公告滞后天数（A股≈60天） |
| ∩ | AND交集（数学符号，等价于"且"） |
