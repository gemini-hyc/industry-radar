#!/usr/bin/env python3
"""
行业轮动因子 A轮 AND 回测
========================
6 个行业因子 × 5 个生产因子 = 30 对 AND 策略回测。

AND 交集逻辑: 股票必须在两个因子上同时排名前 20%（默认阈值）。
评估: 等权持有 20 个交易日的平均收益 vs 基准。

生产因子:
  amount_log      小盘溢价 (direction=-1)
  vol_expansion   量能收缩比 (direction=-1)
  fa_cfp          现金收益比 (direction=+1)
  fa_bp           账面收益比 (direction=+1)
  mf_big_ratio    大单寂寥 (direction=-1)

行业因子 (均 direction=-1，A股行业反转):
  ind_mom_20d, ind_mom_60d, ind_rs_20d, ind_breadth_20d,
  ind_dispersion_20d, ind_crowd_turnover

输入:
  - data/factors/ind_*_daily.parquet (行业因子)
  - data/market/daily/*.parquet (日线)
  - data/fundamental/fina_all.parquet (财务)
  - data/moneyflow/mf_factors.parquet (资金流)
  - data/stock_basic.parquet (行业分类)

输出:
  - 30对 AND 利差汇总表
  - 最佳配对详情
"""

import sys, os, time, gc, glob
sys.path.insert(0, "/opt/data/quant")

import pandas as pd
import numpy as np
from scipy import stats
from pathlib import Path
from collections import defaultdict

# ── 配置 ──
DAILY_DIR = "/opt/data/quant-data/market/daily"
FACTOR_DIR = "/opt/data/quant-data/factors"
FINA_PATH = "/opt/data/quant-data/fundamental/fina_all.parquet"
MF_PATH = "/opt/data/quant-data/moneyflow/mf_factors.parquet"
STOCK_BASIC = "/opt/data/quant-data/stock_basic.parquet"

HOLDING_DAYS = 20          # 持有周期
TOP_PCT = 0.20             # AND 阈值: 前 20%
MIN_AND_STOCKS = 15        # 最少AND股票数（低于此数当日不交易）

# 生产因子
PROD_FACTORS = {
    'amount_log':     {'dir': -1, 'source': 'market'},
    'vol_expansion':  {'dir': -1, 'source': 'market'},
    'fa_cfp':         {'dir': +1, 'source': 'fina'},
    'fa_bp':          {'dir': +1, 'source': 'fina'},
    'mf_big_ratio':   {'dir': -1, 'source': 'moneyflow'},
}

# 行业因子
IND_FACTORS = [
    'ind_mom_20d', 'ind_mom_60d', 'ind_rs_20d',
    'ind_breadth_20d', 'ind_dispersion_20d', 'ind_crowd_turnover',
]
IND_DIRECTION = -1  # 全部行业反转因子


def load_market_data():
    """加载全量日线数据"""
    files = sorted(glob.glob(f"{DAILY_DIR}/*.parquet"))
    print(f"  加载 {len(files)} 个日线文件...")

    frames = []
    for i, f in enumerate(files):
        if i % 300 == 0:
            print(f"    {i}/{len(files)}...")
        df = pd.read_parquet(f)
        frames.append(df[['date', 'code', 'close', 'volume', 'amount', 'pctChg']])

    data = pd.concat(frames, ignore_index=True)
    data['date'] = pd.to_datetime(data['date'])
    data = data.sort_values(['code', 'date']).reset_index(drop=True)

    # 过滤
    data = data[~data['code'].str.match(r'^(sh\.000|sz\.399|bj\.|sh\.688)')]
    print(f"  加载完成: {len(data)} 行, {data['code'].nunique()} 只")
    return data


def compute_amount_log(data):
    """amount_log = ln(mean(amount, 20d))"""
    df = data.sort_values(['code', 'date']).copy()
    df['amount_log'] = np.log(
        df.groupby('code')['amount'].transform(
            lambda x: x.rolling(20, min_periods=5).mean()
        ).replace(0, np.nan)
    )
    return df[['date', 'code', 'amount_log']].dropna()


def compute_vol_expansion(data):
    """vol_expansion = SMA(volume,5) / SMA(volume,20)"""
    df = data.sort_values(['code', 'date']).copy()
    ma5 = df.groupby('code')['volume'].transform(lambda x: x.rolling(5, min_periods=3).mean())
    ma20 = df.groupby('code')['volume'].transform(lambda x: x.rolling(20, min_periods=10).mean())
    df['vol_expansion'] = (ma5 / ma20.replace(0, np.nan)).clip(0, 10)
    return df[['date', 'code', 'vol_expansion']].dropna()


def compute_fa_factors(data, fina_df):
    """
    计算基本面因子: fa_cfp, fa_bp。
    fa_cfp = ocfps / close
    fa_bp = bps / close
    每季度更新，滞后60天。
    """
    fina_df = fina_df.copy()
    fina_df['end_date'] = pd.to_datetime(fina_df['end_date'])

    # 转换代码格式
    def ts2bs(code):
        parts = str(code).split('.')
        if len(parts) == 2:
            return f"{parts[1].lower()}.{parts[0]}"
        return code

    fina_df['code'] = fina_df['ts_code'].apply(ts2bs)

    # 获取所有交易日
    all_dates = sorted(data['date'].unique())
    quarters = sorted(fina_df['end_date'].unique())

    results = []
    for trade_date in all_dates:
        # 找到可用季度（滞后60天）
        avail_q = None
        for q in reversed(quarters):
            if q + pd.Timedelta(days=60) <= trade_date:
                avail_q = q
                break

        if avail_q is None:
            continue

        qdata = fina_df[fina_df['end_date'] == avail_q]
        day_data = data[data['date'] == trade_date][['code', 'close']].copy()

        # ocfps → fa_cfp
        ocfps_map = qdata.set_index('code')['ocfps'].dropna().to_dict()
        day_data['ocfps'] = day_data['code'].map(ocfps_map)
        day_data['fa_cfp'] = np.where(
            (day_data['ocfps'].notna()) & (day_data['close'] > 0),
            day_data['ocfps'] / day_data['close'],
            np.nan
        )

        # bps → fa_bp
        bps_map = qdata.set_index('code')['bps'].dropna().to_dict()
        day_data['bps_raw'] = day_data['code'].map(bps_map)
        day_data['fa_bp'] = np.where(
            (day_data['bps_raw'].notna()) & (day_data['bps_raw'] > 0) & (day_data['close'] > 0),
            day_data['bps_raw'] / day_data['close'],
            np.nan
        )

        day_data['date'] = trade_date
        results.append(day_data[['date', 'code', 'fa_cfp', 'fa_bp']])

    return pd.concat(results, ignore_index=True)


def compute_percentile(df, col, direction):
    """
    每日横截面百分位排名 (0-100)。
    direction: +1=高值高分, -1=低值高分
    """
    result = df.groupby('date')[col].transform(
        lambda x: x.rank(pct=True) * 100
    )
    if direction == -1:
        result = 100 - result
    return result


def load_industry_factor(name):
    """加载行业因子预计算文件"""
    fpath = os.path.join(FACTOR_DIR, f"{name}_daily.parquet")
    df = pd.read_parquet(fpath)
    df['date'] = pd.to_datetime(df['date'])
    return df


def compute_forward_returns(data, holding_days):
    """计算前向持有期收益"""
    df = data[['date', 'code', 'close']].sort_values(['code', 'date']).copy()
    fwd_col = f'fwd_ret_{holding_days}d'
    df[fwd_col] = df.groupby('code')['close'].transform(
        lambda x: x.shift(-holding_days) / x - 1
    )
    return df[['date', 'code', fwd_col]]


def and_backtest(factor1_vals, factor2_vals, top_pct, min_stocks):
    """
    单对AND回测。

    Args:
        factor1_vals: DataFrame with [date, code, pct1]
        factor2_vals: DataFrame with [date, code, pct2]
        top_pct: threshold (e.g. 20 = top 20%)
        min_stocks: minimum AND stocks required

    Returns:
        DataFrame with daily AND returns
    """
    merged = factor1_vals.merge(factor2_vals, on=['date', 'code'], how='inner')

    results = []
    for date, day_df in merged.groupby('date'):
        # AND 条件: 两个因子都在 top_pct%
        mask = (day_df['pct1'] >= (100 - top_pct)) & (day_df['pct2'] >= (100 - top_pct))
        and_stocks = day_df[mask]

        if len(and_stocks) < min_stocks:
            continue

        # 等权平均前向收益
        fwd_col = [c for c in and_stocks.columns if c.startswith('fwd_ret')]
        if not fwd_col:
            continue

        and_ret = and_stocks[fwd_col[0]].mean()

        # 基准: 全市场等权
        mkt_ret = day_df[fwd_col[0]].mean()

        results.append({
            'date': date,
            'and_ret': and_ret,
            'mkt_ret': mkt_ret,
            'spread': and_ret - mkt_ret,
            'n_and': len(and_stocks),
            'n_total': len(day_df),
        })

    return pd.DataFrame(results)


def main():
    t0 = time.time()
    print("=" * 60)
    print("  行业轮动因子 A轮 AND 回测")
    print(f"  6 行业 × 5 生产 = 30 对, 持有{HOLDING_DAYS}日")
    print("=" * 60)

    # ── 1. 加载/计算生产因子 ──
    print("\n[1/5] 加载市场数据 + 计算生产因子...")
    market = load_market_data()
    gc.collect()

    print("  计算 amount_log...")
    al_df = compute_amount_log(market)
    print("  计算 vol_expansion...")
    ve_df = compute_vol_expansion(market)

    print("  加载财务数据 + 计算 fa_cfp/fa_bp...")
    fina_df = pd.read_parquet(FINA_PATH)
    fa_df = compute_fa_factors(market, fina_df)

    print("  加载 mf_big_ratio...")
    mf_df = pd.read_parquet(MF_PATH)
    mf_df['date'] = pd.to_datetime(mf_df['date'])
    mf_df = mf_df[['date', 'code', 'mf_big_ratio']].dropna()

    # 百分位化生产因子
    print("  百分位化生产因子...")
    al_df['pct'] = compute_percentile(al_df, 'amount_log', -1)
    ve_df['pct'] = compute_percentile(ve_df, 'vol_expansion', -1)
    fa_df['fa_cfp_pct'] = compute_percentile(fa_df, 'fa_cfp', +1)
    fa_df['fa_bp_pct'] = compute_percentile(fa_df, 'fa_bp', +1)
    mf_df['pct'] = compute_percentile(mf_df, 'mf_big_ratio', -1)

    # 统一格式
    prod_dfs = {
        'amount_log':    al_df[['date', 'code', 'pct']].rename(columns={'pct': 'pct1'}),
        'vol_expansion': ve_df[['date', 'code', 'pct']].rename(columns={'pct': 'pct1'}),
        'fa_cfp':        fa_df[['date', 'code', 'fa_cfp_pct']].rename(columns={'fa_cfp_pct': 'pct1'}),
        'fa_bp':         fa_df[['date', 'code', 'fa_bp_pct']].rename(columns={'fa_bp_pct': 'pct1'}),
        'mf_big_ratio':  mf_df[['date', 'code', 'pct']].rename(columns={'pct': 'pct1'}),
    }
    gc.collect()

    # ── 2. 计算前向收益 ──
    print(f"\n[2/5] 计算前向{HOLDING_DAYS}日收益...")
    fwd_df = compute_forward_returns(market, HOLDING_DAYS)
    fwd_col = f'fwd_ret_{HOLDING_DAYS}d'
    gc.collect()

    # ── 3. 回测每一对 ──
    print(f"\n[3/5] 回测 30 对 AND 策略...")
    results = {}

    for ind_name in IND_FACTORS:
        print(f"\n  --- {ind_name} ---")
        ind_f = load_industry_factor(ind_name)

        # 方向修正: 行业因子全部 direction=-1
        ind_f['pct2'] = 100 - (ind_f.groupby('date')['value'].rank(pct=True) * 100)

        for prod_name, prod_f in prod_dfs.items():
            pair_name = f"{ind_name} ∩ {prod_name}"

            # 附加前向收益
            f1 = prod_f.merge(fwd_df[['date', 'code', fwd_col]], on=['date', 'code'], how='inner')
            f2 = ind_f[['date', 'code', 'pct2']].copy()

            bt = and_backtest(f1, f2, TOP_PCT * 100, MIN_AND_STOCKS)

            if len(bt) == 0:
                print(f"    {pair_name}: 无有效交易日")
                results[pair_name] = None
                continue

            # 统计
            mean_spread = bt['spread'].mean()
            t_stat = bt['spread'].mean() / bt['spread'].std() * np.sqrt(len(bt)) if bt['spread'].std() > 0 else 0
            win_rate = (bt['spread'] > 0).mean()
            avg_n = bt['n_and'].mean()

            results[pair_name] = {
                'mean_spread': mean_spread,
                't_stat': t_stat,
                'win_rate': win_rate,
                'avg_n_stocks': avg_n,
                'n_days': len(bt),
            }

            marker = '✅' if t_stat > 2.0 else ('⚠️' if t_stat > 1.0 else '❌')
            print(f"    {marker} {pair_name}: 利差={mean_spread:+.4f}, t={t_stat:+.2f}, 胜率={win_rate:.1%}, 日均{avg_n:.0f}只, {len(bt)}天")

    # ── 4. 汇总排名 ──
    print(f"\n[4/5] 汇总排名...")
    print(f"\n{'='*80}")
    print(f"  A轮 AND 回测结果 (持有{HOLDING_DAYS}日, top{int(TOP_PCT*100)}% 阈值, ≥{MIN_AND_STOCKS}只)")
    print(f"{'='*80}")

    valid = {k: v for k, v in results.items() if v is not None}
    ranked = sorted(valid.items(), key=lambda x: x[1]['t_stat'], reverse=True)

    print(f"\n  {'排名':<4s} {'配对':<45s} {'利差':>8s} {'t值':>8s} {'胜率':>7s} {'日均只':>7s} {'交易日':>7s}")
    print(f"  {'-'*90}")
    for i, (name, r) in enumerate(ranked):
        print(f"  {i+1:<4d} {name:<45s} {r['mean_spread']:>+8.4f} {r['t_stat']:>+8.2f} {r['win_rate']:>7.1%} {r['avg_n_stocks']:>7.0f} {r['n_days']:>7d}")

    # ── 5. 最佳配对详情 ──
    print(f"\n[5/5] 最佳配对 TOP 5 详情...")
    for i, (name, r) in enumerate(ranked[:5]):
        print(f"\n  {i+1}. {name}")
        print(f"     平均利差: {r['mean_spread']:+.4f} | t={r['t_stat']:+.2f} | 胜率={r['win_rate']:.1%}")
        print(f"     日均AND股数: {r['avg_n_stocks']:.0f} | 有效交易日: {r['n_days']}")

    total_time = time.time() - t0
    print(f"\n✅ 全部完成! 总耗时: {total_time:.1f}s ({total_time/60:.1f}min)")


if __name__ == "__main__":
    main()
