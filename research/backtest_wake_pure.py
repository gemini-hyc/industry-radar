#!/usr/bin/env python3
"""
纯唤醒条件回测 — 方案C
====================================
唤醒逻辑: C3 AND (C1 OR C2 OR C4)
  - C3 涨幅为必要条件
  - C1/C2/C4 至少满足一个
  - 去掉 C0（前20日平均宽度）

唤醒条件：
  C1 沉睡: breadth 20日百分位 < 45%
  C2 跳升: breadth 比前1日跳升 > 20pp (0.20)
  C3 涨幅: avg_pct > 1.5%
  C4 放量: total_amount / 前1日 total_amount > 1.15
"""

import pandas as pd
import numpy as np
from math import sqrt, erf

# ── 配置 ──────────────────────────────────────
WAKE_SLEEP     = 0.45   # C1: breadth 20日百分位阈值
WAKE_DELTA     = 0.20   # C2: breadth 跳升阈值 (20pp)
WAKE_PCT       = 1.5    # C3: 涨幅阈值
WAKE_AMT_RATIO = 1.15   # C4: 放量比值阈值
HOLD_DAYS      = [5, 10, 20]
DATA_PATH      = "/Users/hyc/.hermes/quant/data/industry/industry_daily_full.parquet"
COOLDOWN_DAYS  = 20     # 同一行业两次唤醒最短间隔

# ── 统计工具 ──────────────────────────────────
def ttest_1samp(x, popmean=0):
    x = np.array(x, dtype=float)
    n = len(x)
    if n < 2:
        return 0.0, 1.0
    mean = x.mean()
    var = x.var(ddof=1)
    if var == 0:
        return 0.0, 1.0
    t_val = (mean - popmean) / sqrt(var / n)
    p_val = 2 * (1 - 0.5 * (1 + erf(abs(t_val) / sqrt(2))))
    return t_val, p_val

# ── 加载数据 ──────────────────────────────────
print("加载数据...")
df = pd.read_parquet(DATA_PATH)
df = df.sort_values(["con_code", "date"]).reset_index(drop=True)
print(f"  {df.shape[0]} 行, {df['con_code'].nunique()} 行业, "
      f"日期 {df['date'].min().strftime('%Y-%m-%d')} ~ {df['date'].max().strftime('%Y-%m-%d')}")

# ── 计算每日唤醒条件 ──────────────────────────
print("计算唤醒条件...")

def compute_wake_flags(g):
    g = g.sort_values("date").copy()
    
    # C1: breadth 20日百分位
    g["bw20_pct"] = g["breadth"].rolling(20, min_periods=10).apply(
        lambda x: (x.iloc[-1] > x.iloc[:-1]).mean() * 100, raw=False
    )
    
    # C2: breadth 跳升 (今日 - 昨日)
    g["bw_prev"] = g["breadth"].shift(1)
    g["bw_delta"] = g["breadth"] - g["bw_prev"]
    
    # C4: 放量 (今日 / 昨日)
    g["amt_prev"] = g["total_amount"].shift(1)
    g["amt_ratio"] = g["total_amount"] / g["amt_prev"]
    
    # 标记满足各条件
    g["c1"] = g["bw20_pct"] < (WAKE_SLEEP * 100)
    g["c2"] = g["bw_delta"] > WAKE_DELTA
    g["c3"] = g["avg_pct"] > WAKE_PCT
    g["c4"] = g["amt_ratio"] > WAKE_AMT_RATIO
    
    # 方案C唤醒逻辑: C3 AND (C1 OR C2 OR C4)
    g["wake"] = g["c3"] & (g["c1"] | g["c2"] | g["c4"])
    
    # 记录满足几个辅助条件(用于分析)
    g["aux_count"] = g["c1"].astype(int) + g["c2"].astype(int) + g["c4"].astype(int)
    
    return g

df = df.groupby("con_code", group_keys=False).apply(compute_wake_flags)

# ── 提取唤醒事件 ──────────────────────────────
print("提取唤醒事件...")
wake_events = df[df["wake"]].copy()
print(f"  原始唤醒信号: {len(wake_events)} 次")

# 冷却期：同一行业20天内只取第一次
wake_events = wake_events.sort_values(["con_code", "date"])
filtered = []
for code, grp in wake_events.groupby("con_code"):
    last_date = None
    for _, row in grp.iterrows():
        if last_date is None or (row["date"] - last_date).days >= COOLDOWN_DAYS:
            filtered.append(row)
            last_date = row["date"]

wake_events = pd.DataFrame(filtered).reset_index(drop=True)
print(f"  冷却后唤醒事件: {len(wake_events)} 次")

# ── 计算后续收益 ──────────────────────────────
print("计算后续收益...")

all_dates = sorted(df["date"].unique())
date_idx = {d: i for i, d in enumerate(all_dates)}
daily_market_avg = df.groupby("date")["avg_pct"].mean()

results = []
skipped = 0

for _, evt in wake_events.iterrows():
    code = evt["con_code"]
    wake_date = evt["date"]
    
    if wake_date not in date_idx:
        skipped += 1
        continue
    idx = date_idx[wake_date]
    
    ind_data = df[df["con_code"] == code].set_index("date")["avg_pct"].sort_index()
    
    for h in HOLD_DAYS:
        if idx + h > len(all_dates):
            actual = len(all_dates) - idx
            if actual < 3:
                skipped += 1
                continue
            h_actual = actual
        else:
            h_actual = h
        
        trade_dates = all_dates[idx: idx + h_actual]
        
        ind_cum = sum(ind_data.get(d, 0) for d in trade_dates)
        bench_cum = sum(daily_market_avg.get(d, 0) for d in trade_dates)
        
        valid_days = sum(1 for d in trade_dates if d in ind_data.index)
        if valid_days < 3:
            skipped += 1
            continue
        
        results.append({
            "con_code": code,
            "wake_date": wake_date.strftime("%Y-%m-%d"),
            "c1": bool(evt["c1"]),
            "c2": bool(evt["c2"]),
            "c3": bool(evt["c3"]),
            "c4": bool(evt["c4"]),
            "aux_count": int(evt["aux_count"]),
            "bw20_pct": round(evt["bw20_pct"], 1),
            "bw_delta": round(evt["bw_delta"] * 100, 1),
            "avg_pct": round(evt["avg_pct"], 2),
            "amt_ratio": round(evt["amt_ratio"], 2),
            "horizon": h_actual,
            "horizon_target": h,
            "ind_cum": round(ind_cum, 2),
            "bench_cum": round(bench_cum, 2),
            "excess": round(ind_cum - bench_cum, 2),
        })

df_results = pd.DataFrame(results)
print(f"  有效事件×持有期: {len(df_results)}, 跳过: {skipped}")

# ── 统计输出 ──────────────────────────────────
def stats_block(sub, label):
    rows = []
    for h in HOLD_DAYS:
        hsub = sub[sub["horizon_target"] == h]
        n = len(hsub)
        if n < 3:
            rows.append(f"  {h:2d}日: n={n} (样本不足)")
            continue
        
        ind = hsub["ind_cum"]
        exc = hsub["excess"]
        
        ind_mean = ind.mean()
        ind_win = (ind > 0).mean()
        exc_mean = exc.mean()
        exc_win = (exc > 0).mean()
        
        t_ind, p_ind = ttest_1samp(ind.values, 0)
        t_exc, p_exc = ttest_1samp(exc.values, 0)
        
        sig_ind = "***" if p_ind < 0.001 else "**" if p_ind < 0.01 else "*" if p_ind < 0.05 else ""
        sig_exc = "***" if p_exc < 0.001 else "**" if p_exc < 0.01 else "*" if p_exc < 0.05 else ""
        
        rows.append(
            f"  {h:2d}日 | n={n:3d} | "
            f"绝对涨幅 均值={ind_mean:+6.2f}% 胜率={ind_win:5.1%} t={t_ind:+5.1f}{sig_ind} | "
            f"超额 均值={exc_mean:+6.2f}% 胜率={exc_win:5.1%} t={t_exc:+5.1f}{sig_exc}"
        )
    return rows

print("\n" + "=" * 100)
print("方案C 唤醒条件回测结果")
print("=" * 100)
print(f"条件: C3(avg_pct>{WAKE_PCT}%) AND (C1(bw20_pct<{WAKE_SLEEP*100:.0f}%) OR C2(跳升>{WAKE_DELTA*100:.0f}pp) OR C4(放量>{WAKE_AMT_RATIO}x))")
print(f"数据范围: {df['date'].min().strftime('%Y-%m-%d')} ~ {df['date'].max().strftime('%Y-%m-%d')}")
print(f"唤醒事件数: {len(wake_events)}")
print()

print("── 全样本 ──")
for line in stats_block(df_results, "全样本"):
    print(line)

# 按辅助条件数量分
print("\n── 按辅助条件数量(C1/C2/C4满足几个) ──")
for aux_n in sorted(df_results["aux_count"].unique()):
    sub = df_results[df_results["aux_count"] == aux_n]
    n5 = len(sub[sub["horizon_target"] == 5])
    print(f"\n  满足{aux_n}个辅助条件 (n={n5}):")
    for line in stats_block(sub, f"aux={aux_n}"):
        print(line)

# 按年度分
print("\n── 按年度 ──")
df_results["year"] = df_results["wake_date"].str[:4]
for year in sorted(df_results["year"].unique()):
    ysub = df_results[df_results["year"] == year]
    print(f"\n  {year} (n_events={len(ysub[ysub['horizon_target']==5])}):")
    for line in stats_block(ysub, year):
        print(line)

# ── 核心问题回答：20日上涨概率 ──────────────────
print("\n" + "=" * 100)
print("核心问题：唤醒后20个交易日上涨的可能性")
print("=" * 100)

h20 = df_results[df_results["horizon_target"] == 20]
if len(h20) >= 5:
    ind_win20 = (h20["ind_cum"] > 0).mean()
    exc_win20 = (h20["excess"] > 0).mean()
    ind_mean20 = h20["ind_cum"].mean()
    exc_mean20 = h20["excess"].mean()
    t_ind, p_ind = ttest_1samp(h20["ind_cum"].values, 0)
    t_exc, p_exc = ttest_1samp(h20["excess"].values, 0)
    
    print(f"  样本数: {len(h20)}")
    print(f"  绝对涨幅: 均值 {ind_mean20:+.2f}%, 上涨概率 {ind_win20:.1%}, t={t_ind:+.2f} (p={p_ind:.4f})")
    print(f"  超额涨幅: 均值 {exc_mean20:+.2f}%, 跑赢概率 {exc_win20:.1%}, t={t_exc:+.2f} (p={p_exc:.4f})")
    print(f"  中位数: 绝对 {h20['ind_cum'].median():+.2f}%, 超额 {h20['excess'].median():+.2f}%")
else:
    print("  样本不足5个，无法得出可靠结论")

# ── 保存 ──────────────────────────────────────
out_path = "/Users/hyc/.hermes/quant/data/industry/wake_pure_backtest_detail.csv"
df_results.to_csv(out_path, index=False)
print(f"\n明细已保存 → {out_path}")
