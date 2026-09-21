#!/usr/bin/env python3
"""
冷区扩张组合回测 — 15%冷区 + 3天连续扩张
=========================================

模拟实际交易：
- 每日选出满足"前20日均宽<15% 且 连续扩张≥3天"的行业
- 等权持有N天（默认20天），计算组合收益
- 对比全市场等权基准，算超额

输出：
- 累计收益曲线数据
- 年化收益/超额/IR/最大回撤
- 年度分拆表现
- 持仓分布统计
"""

import warnings
warnings.filterwarnings('ignore')

import numpy as np
import pandas as pd
from scipy import stats

DATA_DIR = '/Users/hyc/.hermes/quant/data'
IND_DAILY_PATH = f'{DATA_DIR}/industry/industry_daily_full.parquet'

SIGNAL_START = '2020-07-01'  # 热启动后
HOLD_DAYS = 20
COLD_THRESHOLD = 0.15
EXPANSION_MIN = 3


def load_and_prepare():
    df = pd.read_parquet(IND_DAILY_PATH)
    df = df.sort_values(['con_code', 'date']).reset_index(drop=True)
    print(f"数据: {len(df)} 行, {df['con_code'].nunique()} 行业, "
          f"{df['date'].min().strftime('%Y-%m-%d')} ~ {df['date'].max().strftime('%Y-%m-%d')}")

    # 前20日均宽（滚动窗口）
    df['pre_breadth_20d'] = df.groupby('con_code')['breadth'].transform(
        lambda x: x.rolling(20, min_periods=15).mean().shift(1)
    )

    # 连续扩张天数
    df['breadth_diff'] = df.groupby('con_code')['breadth'].diff()
    df['is_expanding'] = df['breadth_diff'] > 0

    def _consecutive_count(s):
        r = np.zeros(len(s), dtype=int)
        c = 0
        for i in range(len(s)):
            if s.iloc[i]:
                c += 1
            else:
                c = 0
            r[i] = c
        return pd.Series(r, index=s.index)

    df['expansion_days'] = df.groupby('con_code')['is_expanding'].transform(_consecutive_count)

    # 信号：冷区 + 连续扩张≥N天
    df['signal'] = (
        (df['pre_breadth_20d'] < COLD_THRESHOLD) &
        (df['expansion_days'] >= EXPANSION_MIN)
    )

    # 前向收益
    for n in [5, 10, 20, 30]:
        df[f'fwd_{n}d_ret'] = df.groupby('con_code')['avg_pct'].transform(
            lambda x: x.rolling(n, min_periods=n).sum().shift(-n)
        )

    # 市场日收益
    market_daily = df.groupby('date')['avg_pct'].mean().reset_index()
    market_daily.columns = ['date', 'market_avg_pct']
    df = df.merge(market_daily, on='date', how='left')

    # 市场前向收益
    mkt_fwd = market_daily.copy()
    for n in [5, 10, 20, 30]:
        mkt_fwd[f'mkt_fwd_{n}d'] = mkt_fwd['market_avg_pct'].rolling(n, min_periods=n).sum().shift(-n)
    df = df.merge(mkt_fwd[['date'] + [f'mkt_fwd_{n}d' for n in [5, 10, 20, 30]]], on='date', how='left')

    df['date'] = pd.to_datetime(df['date'])
    return df


def run_portfolio_backtest(df):
    """组合回测：每日等权持有所有信号行业"""
    df_sig = df[df['signal'] & (df['date'] >= SIGNAL_START)].copy()
    print(f"\n{'='*70}")
    print(f"组合回测: 冷区<{COLD_THRESHOLD:.0%} + 连续扩张≥{EXPANSION_MIN}天, 持有{HOLD_DAYS}天")
    print(f"{'='*70}")
    print(f"信号总次数: {len(df_sig)}, 涉及行业: {df_sig['con_code'].nunique()}")

    # ── 每日组合前向收益（等权） ──
    daily_portfolio = df_sig.groupby('date').agg(
        n_signals=('con_code', 'count'),
        port_ret=('fwd_20d_ret', 'mean'),
        mkt_ret=('mkt_fwd_20d', 'first'),
    ).dropna()

    daily_portfolio['port_excess'] = daily_portfolio['port_ret'] - daily_portfolio['mkt_ret']

    print(f"\n每日组合统计（共{len(daily_portfolio)}个交易日有信号）:")
    print(f"  持仓数: mean={daily_portfolio['n_signals'].mean():.1f}, "
          f"median={daily_portfolio['n_signals'].median():.0f}, "
          f"max={daily_portfolio['n_signals'].max()}")

    # ── 整体指标 ──
    n_days = len(daily_portfolio)
    total_port = daily_portfolio['port_ret'].mean() * n_days / HOLD_DAYS * 252
    total_mkt = daily_portfolio['mkt_ret'].mean() * n_days / HOLD_DAYS * 252
    mean_excess = daily_portfolio['port_excess'].mean()

    # 年化超额 IR
    port_annual_excess = mean_excess * 252 / HOLD_DAYS
    port_annual_std = daily_portfolio['port_excess'].std() * np.sqrt(252 / HOLD_DAYS)
    ir = port_annual_excess / port_annual_std if port_annual_std > 0 else 0

    # 胜率
    win_rate = (daily_portfolio['port_excess'] > 0).mean()

    # t检验
    t_val, p_val = stats.ttest_1samp(daily_portfolio['port_excess'].dropna(), 0)

    print(f"\n{'─'*50}")
    print(f"📊 整体表现（持有{HOLD_DAYS}天）")
    print(f"{'─'*50}")
    print(f"  组合日均超额:   {mean_excess:+.4f}%")
    print(f"  年化超额:       {port_annual_excess:+.2f}%")
    print(f"  年化超额标准差: {port_annual_std:.2f}%")
    print(f"  IR:             {ir:+.2f}")
    print(f"  胜率:           {win_rate:.1%}")
    print(f"  t={t_val:+.2f}, p={p_val:.3f}")

    # ── 累计超额曲线 ──
    daily_portfolio['cum_port'] = (1 + daily_portfolio['port_ret'] / 100).cumprod()
    daily_portfolio['cum_mkt'] = (1 + daily_portfolio['mkt_ret'] / 100).cumprod()
    daily_portfolio['cum_excess'] = daily_portfolio['cum_port'] / daily_portfolio['cum_mkt']

    # ── 年度分拆 ──
    print(f"\n{'─'*50}")
    print(f"📊 年度表现")
    print(f"{'─'*50}")
    print(f"  {'年份':>6}  {'信号日':>5}  {'组合均超额':>9}  {'胜率':>5}  {'IR':>6}")
    annual_rows = []
    for year in sorted(daily_portfolio.index.year.unique()):
        sub = daily_portfolio[daily_portfolio.index.year == year]
        if len(sub) < 5:
            continue
        yr_excess = sub['port_excess'].mean()
        yr_win = (sub['port_excess'] > 0).mean()
        yr_ir = sub['port_excess'].mean() / sub['port_excess'].std() * np.sqrt(len(sub)) if sub['port_excess'].std() > 0 else 0
        print(f"  {year:>6}  {len(sub):>5}  {yr_excess:+9.3f}%  {yr_win:5.1%}  {yr_ir:+6.2f}")
        annual_rows.append({'year': year, 'n': len(sub), 'mean_excess': yr_excess, 'win_rate': yr_win, 'ir': yr_ir})

    # ── 不同持有期 ──
    print(f"\n{'─'*50}")
    print(f"📊 不同持有期对比")
    print(f"{'─'*50}")
    for n in [5, 10, 20, 30]:
        col_ret = f'fwd_{n}d_ret'
        col_mkt = f'mkt_fwd_{n}d'
        daily_n = df_sig.groupby('date').agg(
            port_ret=(col_ret, 'mean'),
            mkt_ret=(col_mkt, 'first'),
        ).dropna()
        daily_n['excess'] = daily_n['port_ret'] - daily_n['mkt_ret']
        ann_ex = daily_n['excess'].mean() * 252 / n
        ann_std = daily_n['excess'].std() * np.sqrt(252 / n)
        ir_n = ann_ex / ann_std if ann_std > 0 else 0
        wr = (daily_n['excess'] > 0).mean()
        t_n, p_n = stats.ttest_1samp(daily_n['excess'], 0)
        print(f"  {n:2d}天: 年化超额={ann_ex:+.2f}%, IR={ir_n:+.2f}, 胜率={wr:.1%}, t={t_n:+.2f}, p={p_n:.3f}")

    # ── 扩张天数细分（3/4/5天） ──
    print(f"\n{'─'*50}")
    print(f"📊 扩张天数细分（均冷区<15%）")
    print(f"{'─'*50}")
    for exp_n in [3, 4, 5]:
        sub_sig = df[
            (df['pre_breadth_20d'] < COLD_THRESHOLD) &
            (df['expansion_days'] >= exp_n) &
            (df['date'] >= SIGNAL_START)
        ]
        # 去重：同一天同一行业只算一次（取首次满足exp_n的时刻）
        # 但expansion_days>=exp_n意味着>=3天已经包含了，所以我们取expansion_days==exp_n作为"恰好第N天"
        sub_exact = df[
            (df['pre_breadth_20d'] < COLD_THRESHOLD) &
            (df['expansion_days'] == exp_n) &
            (df['date'] >= SIGNAL_START)
        ]
        daily_exp = sub_exact.groupby('date').agg(
            port_ret=('fwd_20d_ret', 'mean'),
            mkt_ret=('mkt_fwd_20d', 'first'),
        ).dropna()
        if len(daily_exp) < 10:
            print(f"  ≥{exp_n}天恰好: 信号太少({len(sub_exact)})")
            continue
        daily_exp['excess'] = daily_exp['port_ret'] - daily_exp['mkt_ret']
        ann_ex = daily_exp['excess'].mean() * 252 / HOLD_DAYS
        ann_std = daily_exp['excess'].std() * np.sqrt(252 / HOLD_DAYS)
        ir_e = ann_ex / ann_std if ann_std > 0 else 0
        wr = (daily_exp['excess'] > 0).mean()
        t_e, p_e = stats.ttest_1samp(daily_exp['excess'], 0)
        n_signals = len(sub_exact)
        print(f"  恰好{exp_n}天: n={n_signals}, 年化超额={ann_ex:+.2f}%, IR={ir_e:+.2f}, 胜率={wr:.1%}, t={t_e:+.2f}, p={p_e:.3f}")

    # ── 逐笔统计 ──
    print(f"\n{'─'*50}")
    print(f"📊 逐笔信号统计")
    print(f"{'─'*50}")
    all_excess = df_sig['fwd_20d_ret'] - df_sig['mkt_fwd_20d']
    valid = all_excess.dropna()
    print(f"  总信号笔数: {len(valid)}")
    print(f"  均超额: {valid.mean():+.2f}%")
    print(f"  中位超额: {valid.median():+.2f}%")
    print(f"  胜率: {(valid>0).mean():.1%}")
    print(f"  正超额>2%比例: {(valid>2).mean():.1%}")
    print(f"  亏损>2%比例: {(valid<-2).mean():.1%}")

    # ── 最大回撤 ──
    cum = daily_portfolio['cum_excess']
    rolling_max = cum.cummax()
    drawdown = (cum - rolling_max) / rolling_max
    max_dd = drawdown.min()
    max_dd_date = drawdown.idxmin()
    print(f"\n  累计超额最大回撤: {max_dd:.2%} (日期: {max_dd_date.strftime('%Y-%m-%d')})")

    # ── 月度超额分布 ──
    print(f"\n{'─'*50}")
    print(f"📊 月度超额分布")
    print(f"{'─'*50}")
    monthly = daily_portfolio['port_excess'].resample('M').mean()
    pos_months = (monthly > 0).sum()
    total_months = len(monthly)
    print(f"  正超额月: {pos_months}/{total_months} ({pos_months/total_months:.1%})")
    print(f"  月均超额: {monthly.mean():+.3f}%")
    print(f"  月超额标准差: {monthly.std():.3f}%")

    # ── 4-5天扩张增强标记 ──
    print(f"\n{'─'*50}")
    print(f"📊 4-5天扩张增强效果")
    print(f"{'─'*50}")
    for label, exp_cond in [('3天', df['expansion_days'] == 3), ('4-5天', df['expansion_days'].isin([4, 5]))]:
        sub = df[
            (df['pre_breadth_20d'] < COLD_THRESHOLD) &
            exp_cond &
            (df['date'] >= SIGNAL_START)
        ].copy()
        if len(sub) < 10:
            print(f"  {label}: 样本不足")
            continue
        excess = sub['fwd_20d_ret'] - sub['mkt_fwd_20d']
        valid = excess.dropna()
        if len(valid) < 10:
            print(f"  {label}: 有效样本不足")
            continue
        t_v, p_v = stats.ttest_1samp(valid, 0)
        print(f"  {label}: n={len(valid)}, 均超额={valid.mean():+.2f}%, 胜率={(valid>0).mean():.1%}, t={t_v:+.2f}, p={p_v:.3f}")

    return daily_portfolio


def main():
    df = load_and_prepare()
    result = run_portfolio_backtest(df)
    print("\n✅ 回测完成")


if __name__ == '__main__':
    main()
