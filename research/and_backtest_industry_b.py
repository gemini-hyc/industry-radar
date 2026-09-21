#!/usr/bin/env python3
"""
行业轮动因子 B轮 AND 回测 — 第三因子提纯
=========================================
A轮top配对 + 第三因子，看能否提纯利差。

测试组合:
  基底1: ind_crowd_turnover ∩ amount_log (A轮#1, +1.18%, t=15.28)
  基底2: ind_mom_60d ∩ amount_log        (A轮#2, +1.16%, t=14.20)

  第三因子: fa_cfp, fa_bp, mf_big_ratio, vol_expansion
  = 2 × 4 = 8 个三因子组合

同时对比:
  - amount_log solo:               +1.27%/20d (基准)
  - amount_log ∩ fa_cfp:           +1.33%/20d (现有双核)
  - amount_log ∩ fa_cfp ∩ fa_bp:   +3.27%/20d (现有三核)
"""

import sys, os, time, gc, glob
sys.path.insert(0, "/opt/data/quant")

import pandas as pd
import numpy as np
from scipy import stats

# ── 配置 ──
DAILY_DIR = "/opt/data/quant-data/market/daily"
FACTOR_DIR = "/opt/data/quant-data/factors"
FINA_PATH = "/opt/data/quant-data/fundamental/fina_all.parquet"
MF_PATH = "/opt/data/quant-data/moneyflow/mf_factors.parquet"

HOLDING_DAYS = 20
TOP_PCT = 0.20
MIN_AND_STOCKS = 10  # 三因子时降低最低股数

# ── 数据加载 (复用A轮逻辑) ──

def load_market_data():
    files = sorted(glob.glob(f"{DAILY_DIR}/*.parquet"))
    print(f"  加载 {len(files)} 个日线文件...")
    frames = []
    for i, f in enumerate(files):
        if i % 300 == 0:
            print(f"    {i}/{len(files)}...")
        df = pd.read_parquet(f)
        frames.append(df[['date', 'code', 'close', 'volume', 'amount']])
    data = pd.concat(frames, ignore_index=True)
    data['date'] = pd.to_datetime(data['date'])
    data = data.sort_values(['code', 'date']).reset_index(drop=True)
    data = data[~data['code'].str.match(r'^(sh\.000|sz\.399|bj\.|sh\.688)')]
    return data


def compute_amount_log(data):
    df = data.sort_values(['code', 'date']).copy()
    df['amount_log'] = np.log(
        df.groupby('code')['amount'].transform(
            lambda x: x.rolling(20, min_periods=5).mean()
        ).replace(0, np.nan)
    )
    return df[['date', 'code', 'amount_log']].dropna()


def compute_vol_expansion(data):
    df = data.sort_values(['code', 'date']).copy()
    ma5 = df.groupby('code')['volume'].transform(lambda x: x.rolling(5, min_periods=3).mean())
    ma20 = df.groupby('code')['volume'].transform(lambda x: x.rolling(20, min_periods=10).mean())
    df['vol_expansion'] = (ma5 / ma20.replace(0, np.nan)).clip(0, 10)
    return df[['date', 'code', 'vol_expansion']].dropna()


def compute_fa_factors(data, fina_df):
    fina_df = fina_df.copy()
    fina_df['end_date'] = pd.to_datetime(fina_df['end_date'])
    def ts2bs(code):
        parts = str(code).split('.')
        return f"{parts[1].lower()}.{parts[0]}" if len(parts) == 2 else code
    fina_df['code'] = fina_df['ts_code'].apply(ts2bs)
    all_dates = sorted(data['date'].unique())
    quarters = sorted(fina_df['end_date'].unique())

    results = []
    for trade_date in all_dates:
        avail_q = None
        for q in reversed(quarters):
            if q + pd.Timedelta(days=60) <= trade_date:
                avail_q = q
                break
        if avail_q is None:
            continue
        qdata = fina_df[fina_df['end_date'] == avail_q]
        day_data = data[data['date'] == trade_date][['code', 'close']].copy()

        ocfps_map = qdata.set_index('code')['ocfps'].dropna().to_dict()
        day_data['ocfps'] = day_data['code'].map(ocfps_map)
        day_data['fa_cfp'] = np.where(
            (day_data['ocfps'].notna()) & (day_data['close'] > 0),
            day_data['ocfps'] / day_data['close'], np.nan)

        bps_map = qdata.set_index('code')['bps'].dropna().to_dict()
        day_data['bps_raw'] = day_data['code'].map(bps_map)
        day_data['fa_bp'] = np.where(
            (day_data['bps_raw'].notna()) & (day_data['bps_raw'] > 0) & (day_data['close'] > 0),
            day_data['bps_raw'] / day_data['close'], np.nan)

        day_data['date'] = trade_date
        results.append(day_data[['date', 'code', 'fa_cfp', 'fa_bp']])
    return pd.concat(results, ignore_index=True)


def compute_pct(df, col, direction):
    result = df.groupby('date')[col].transform(lambda x: x.rank(pct=True) * 100)
    return 100 - result if direction == -1 else result


def load_ind_factor(name):
    fpath = os.path.join(FACTOR_DIR, f"{name}_daily.parquet")
    df = pd.read_parquet(fpath)
    df['date'] = pd.to_datetime(df['date'])
    df['pct'] = 100 - df.groupby('date')['value'].rank(pct=True) * 100  # direction=-1
    return df[['date', 'code', 'pct']]


def compute_fwd_returns(data, holding_days):
    df = data[['date', 'code', 'close']].sort_values(['code', 'date']).copy()
    col = f'fwd_ret_{holding_days}d'
    df[col] = df.groupby('code')['close'].transform(lambda x: x.shift(-holding_days) / x - 1)
    return df[['date', 'code', col]]


def triple_and_backtest(f1, f2, f3, fwd_df, top_pct, min_stocks):
    """三因子AND回测"""
    merged = f1.merge(f2, on=['date', 'code']).merge(f3, on=['date', 'code'])
    merged = merged.merge(fwd_df, on=['date', 'code'], how='inner')

    fwd_col = [c for c in merged.columns if c.startswith('fwd_ret')][0]
    pct_cols = [c for c in merged.columns if c.startswith('pct')]

    results = []
    for date, day_df in merged.groupby('date'):
        mask = pd.Series(True, index=day_df.index)
        for pc in pct_cols:
            mask = mask & (day_df[pc] >= (100 - top_pct * 100))
        and_stocks = day_df[mask]

        if len(and_stocks) < min_stocks:
            continue

        and_ret = and_stocks[fwd_col].mean()
        mkt_ret = day_df[fwd_col].mean()

        results.append({
            'date': date,
            'and_ret': and_ret,
            'mkt_ret': mkt_ret,
            'spread': and_ret - mkt_ret,
            'n_and': len(and_stocks),
        })
    return pd.DataFrame(results)


def main():
    t0 = time.time()
    print("=" * 60)
    print("  行业轮动因子 B轮 AND 回测 — 第三因子提纯")
    print("=" * 60)

    # ── 加载数据 ──
    print("\n[1/4] 加载市场数据...")
    market = load_market_data()
    print(f"  {len(market)} 行, {market['code'].nunique()} 只")

    print("  计算 amount_log...")
    al_df = compute_amount_log(market)
    al_df['pct'] = compute_pct(al_df, 'amount_log', -1)
    al_f = al_df[['date', 'code', 'pct']].rename(columns={'pct': 'pct_al'})

    print("  计算 vol_expansion...")
    ve_df = compute_vol_expansion(market)
    ve_df['pct'] = compute_pct(ve_df, 'vol_expansion', -1)
    ve_f = ve_df[['date', 'code', 'pct']].rename(columns={'pct': 'pct_ve'})

    print("  加载财务数据...")
    fina_df = pd.read_parquet(FINA_PATH)
    fa_df = compute_fa_factors(market, fina_df)
    fa_df['fa_cfp_pct'] = compute_pct(fa_df, 'fa_cfp', +1)
    fa_df['fa_bp_pct'] = compute_pct(fa_df, 'fa_bp', +1)
    fa_cfp_f = fa_df[['date', 'code', 'fa_cfp_pct']].rename(columns={'fa_cfp_pct': 'pct_cfp'})
    fa_bp_f = fa_df[['date', 'code', 'fa_bp_pct']].rename(columns={'fa_bp_pct': 'pct_bp'})

    print("  加载 mf_big_ratio...")
    mf_df = pd.read_parquet(MF_PATH)
    mf_df['date'] = pd.to_datetime(mf_df['date'])
    mf_df = mf_df[['date', 'code', 'mf_big_ratio']].dropna()
    mf_df['pct'] = compute_pct(mf_df, 'mf_big_ratio', -1)
    mf_f = mf_df[['date', 'code', 'pct']].rename(columns={'pct': 'pct_mf'})

    print("  加载行业因子...")
    ind_crowd_f = load_ind_factor('ind_crowd_turnover')
    ind_crowd_f = ind_crowd_f.rename(columns={'pct': 'pct_crowd'})
    ind_mom60_f = load_ind_factor('ind_mom_60d')
    ind_mom60_f = ind_mom60_f.rename(columns={'pct': 'pct_mom60'})

    print("  计算前向收益...")
    fwd_df = compute_fwd_returns(market, HOLDING_DAYS)
    fwd_col = f'fwd_ret_{HOLDING_DAYS}d'
    gc.collect()

    # ── B轮测试 ──
    print(f"\n[2/4] B轮三因子测试 (A轮top2对 × 4第三因子)...")

    base_pairs = [
        ('ind_crowd ∩ amount_log', ind_crowd_f, al_f),
        ('ind_mom60d ∩ amount_log', ind_mom60_f, al_f),
    ]

    third_factors = [
        ('fa_cfp', fa_cfp_f),
        ('fa_bp', fa_bp_f),
        ('mf_big_ratio', mf_f),
        ('vol_expansion', ve_f),
    ]

    all_results = {}

    for base_name, f_ind, f_al in base_pairs:
        for third_name, f_third in third_factors:
            combo_name = f"{base_name} ∩ {third_name}"
            print(f"  {combo_name}...")

            bt = triple_and_backtest(f_ind, f_al, f_third, fwd_df, TOP_PCT, MIN_AND_STOCKS)

            if len(bt) == 0:
                print(f"    ❌ 无有效交易日")
                all_results[combo_name] = None
                continue

            mean_spread = bt['spread'].mean()
            t_stat = mean_spread / bt['spread'].std() * np.sqrt(len(bt)) if bt['spread'].std() > 0 else 0
            win_rate = (bt['spread'] > 0).mean()
            avg_n = bt['n_and'].mean()

            all_results[combo_name] = {
                'mean_spread': mean_spread,
                't_stat': t_stat,
                'win_rate': win_rate,
                'avg_n_stocks': avg_n,
                'n_days': len(bt),
            }

            marker = '✅' if t_stat > 2.0 else '⚠️'
            print(f"    {marker} 利差={mean_spread:+.4f}, t={t_stat:+.2f}, 胜率={win_rate:.1%}, 日均{avg_n:.0f}只")

    # ── 对比基准 ──
    print(f"\n[3/4] 计算对比基准...")

    # amount_log solo
    print("  amount_log solo...")
    al_solo_f = al_f.copy()
    al_solo_f = al_solo_f.rename(columns={'pct_al': 'pct'})
    bt_solo = triple_and_backtest(al_solo_f, al_solo_f, al_solo_f, fwd_df, TOP_PCT, MIN_AND_STOCKS)
    # Actually this won't work - triple_and expects 3 different dataframes with different pct columns.
    # Let me do solo separately.
    
    # Simpler approach: just use the AND logic directly
    solo_merged = al_f.merge(fwd_df, on=['date', 'code'], how='inner')
    solo_results = []
    for date, day_df in solo_merged.groupby('date'):
        mask = day_df['pct_al'] >= (100 - TOP_PCT * 100)
        top = day_df[mask]
        if len(top) < MIN_AND_STOCKS:
            continue
        solo_results.append({
            'spread': top[fwd_col].mean() - day_df[fwd_col].mean(),
        })
    solo_df = pd.DataFrame(solo_results)
    solo_spread = solo_df['spread'].mean()
    solo_t = solo_spread / solo_df['spread'].std() * np.sqrt(len(solo_df))
    print(f"    amount_log solo: {solo_spread:+.4f}, t={solo_t:+.2f}")

    # ── 汇总排名 ──
    print(f"\n[4/4] 汇总...")
    print(f"\n{'='*90}")
    print(f"  B轮 AND 回测 — 第三因子提纯 ({HOLDING_DAYS}日持有, top{int(TOP_PCT*100)}%阈值)")
    print(f"{'='*90}")

    valid = {k: v for k, v in all_results.items() if v is not None}
    ranked = sorted(valid.items(), key=lambda x: x[1]['t_stat'], reverse=True)

    # 加入基准
    print(f"\n  {'排名':<4s} {'策略':<55s} {'利差':>8s} {'t值':>8s} {'胜率':>7s} {'日均只':>7s}")
    print(f"  {'-'*95}")
    
    # 基准线
    print(f"  {'基准':<4s} {'amount_log solo':<55s} {solo_spread:>+8.4f} {solo_t:>+8.2f} {'—':>7s} {'—':>7s}")

    for i, (name, r) in enumerate(ranked):
        delta_t = r['t_stat'] - solo_t
        delta_str = f"(Δt={delta_t:+.1f})" if abs(delta_t) > 1 else ""
        print(f"  {i+1:<4d} {name:<55s} {r['mean_spread']:>+8.4f} {r['t_stat']:>+8.2f} {r['win_rate']:>7.1%} {r['avg_n_stocks']:>7.0f} {delta_str}")

    # ── 最佳 vs A轮对比 ──
    print(f"\n{'─'*90}")
    print(f"  A轮 vs B轮 对比 (最佳行业因子配对)")
    print(f"{'─'*90}")
    print(f"  {'策略':<55s} {'利差':>8s} {'t值':>8s} {'胜率':>7s} {'日均只':>7s}")
    print(f"  {'─'*55}  {'─'*8}  {'─'*8}  {'─'*7}  {'─'*7}")
    
    # A轮 best
    a_best = [
        ("ind_crowd ∩ amount_log (A轮)", +0.0118, 15.28, 0.658, 164),
        ("ind_mom60d ∩ amount_log (A轮)", +0.0116, 14.20, 0.660, 166),
    ]
    for name, spread, t, wr, n in a_best:
        print(f"  {name:<55s} {spread:>+8.4f} {t:>+8.2f} {wr:>7.1%} {n:>7.0f}")
    
    # B轮 corresponding best
    for prefix in ['ind_crowd ∩ amount_log ∩ ', 'ind_mom60d ∩ amount_log ∩ ']:
        matches = [(n, r) for n, r in ranked if n.startswith(prefix)]
        if matches:
            n, r = matches[0]
            print(f"  {n:<55s} {r['mean_spread']:>+8.4f} {r['t_stat']:>+8.2f} {r['win_rate']:>7.1%} {r['avg_n_stocks']:>7.0f}")

    total_time = time.time() - t0
    print(f"\n✅ 完成! 耗时: {total_time:.1f}s ({total_time/60:.1f}min)")


if __name__ == "__main__":
    main()
