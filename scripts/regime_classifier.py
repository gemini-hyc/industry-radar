"""
行情类型量化判断系统 v2
========================
逐日判断行情类型：全面上涨 / 偏强结构性 / 结构性 / 边缘结构性 / 轮动 / 偏弱轮动 /
混合 / 全面下跌。

判断维度：
1. up_pct        — 上涨行业占比（市场宽度），区分涨跌方向
2. top5_conc     — Top5 集中度，区分"少数领涨" vs "普涨"
3. rank_consistency — 5 日窗口内的行业排名稳定性（低方差=排名稳=结构性）
4. rank_autocorr — 相邻日行业排名的 Spearman 相关（v2 新增，原 docstring 承诺但从未实现）

──────────────────────────────────────────────────────────────────────────
⚠️ 效度声明（2026-10-01 全历史 1636 天实测，勿省略）
──────────────────────────────────────────────────────────────────────────
对未来的预测力检验：
  · rank_consistency → 次日排名延续性          Pearson = -0.029  Spearman = -0.047
  · rank_consistency → 5×5 横截面动量自相关    Pearson = -0.034
  · rank_autocorr    → 5×5 横截面动量自相关    Pearson = +0.022
  · 按 rank_consistency 四分位分组的冷区信号 20 日超额：
      2.60% / 3.08% / 3.24% / 2.94%  —— 非单调，无区分度
有效判据（同一口径下的单独区分度）：
  · up_pct      ：1.89%(@<20) → 4.07%(@>80)   大体单调递增 ✔
  · top5_conc   ：4.47%(@Q1低集中) → 1.99%(@Q4高集中)  单调递减 ✔
  · 但 top5_conc 与 up_pct 高度共线（up<30 时 tc 中位数 0.54；up>=50 时仅 0.185），
    独立性有限——两者本质上都在度量"市场广度"。
结论：本分类器定位为【市场状态描述】，**不得作为交易信号或预测依据**。
      其信息量主要来自 up_pct 的两端（全面上涨 / 全面下跌），中间的
      "结构性 / 轮动"细分缺乏实证支持。

──────────────────────────────────────────────────────────────────────────
v2 变更（2026-10-01）
──────────────────────────────────────────────────────────────────────────
1. 【修复】rolling_rank_corr 的归一化基准 ddof 口径不一致。
   原实现：分子 rank_df.var(axis=1) 是 ddof=1，分母 (n²-1)/12·(1-1/W) 是 ddof=0
   口径的期望方差，两者相差 W/(W-1) 因子 → 随机排名下
   consistency ≡ 1 - W/(W-1) = **-0.25**（而非 0）。实测全历史均值 -0.2456，与理论吻合。
   后果：阈值 rc>0.3 / 0.4 在 1636 天里**零次**触发，「结构性 / 偏强结构性 /
   边缘结构性」成为死类（报告里从未出现过）。
   修复后锚点归零：随机排名 → 0，完全稳定 → 1，实测均值 +0.0038。
2. 【重校准】分类阈值。原阈值按 0 中心设，在新锚点下不可达。
   新阈值依据全历史实测分布选取，目标是让每个类别都有足够样本覆盖
   （各类 ≥ 3%），**不是**优化预测力（该指标无预测力，见上）。
3. 【新增】rank_autocorr 列（docstring 一直声称有、代码里从未计算）。
4. 【修正】"偏强结构性"的定义方向。原为 up_pct ∈ [50,60) 且排名极稳定，
   但实测该区间与高排名一致性几乎无交集（仅 3 天）——因为上涨过半时
   集中度天然偏低。新定义改为"上涨过半 + 集中度偏高 + 排名稳定"，
   语义为「少数领涨带动了普涨」，与"结构性"（少数领涨、多数不涨）形成对称。
5. 【工程】加 main() 结构，模块可安全 import（原为顶层代码，import 即执行）。
6. 【已知缺陷，暂不改】calc_top5_conc 在上涨行业不足 5 个时会返回负值
   或极端值（实测 min=-8.33，10 天为负）。因影响面小（仅出现在 up<2% 的日子）
   且改动会波及全部历史标签，本次仅记录不修改。
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.paths import INDUSTRY_DIR, DATA_DIR
from src.utils.provenance import save_dataframe

DATA_PATH = INDUSTRY_DIR / 'industry_weighted_returns.parquet'
OUT_PATH = INDUSTRY_DIR / 'daily_regime.parquet'
# 批次2 情绪汇总表（两融/龙虎榜/大宗/北向），由 quant-data/scripts/build_sentiment_daily.py 产出
SENTI_PATH = DATA_DIR / 'sentiment' / 'sentiment_daily.parquet'

# ============================================================
# 分类阈值
# ============================================================
# 选取依据：2020-01-02 ~ 2026-09-30 全历史 1636 天实测分布，
# 目标是各类别样本覆盖均衡（各类 ≥ 3%），而非优化预测力（见模块 docstring 效度声明）。
# rank_consistency 新锚点：0 = 随机排名水平，>0 = 比随机更稳定。
#   q50 = -0.007 | q75 = +0.042 | q80 = +0.058 | q90 = +0.098 | q95 = +0.139
UP_FULL_RISE = 70.0     # 全面上涨：上涨行业占比下限
CONC_FULL_RISE = 0.25   # 全面上涨：集中度上限（低于此视为普涨）
UP_FULL_FALL = 30.0     # 全面下跌：上涨行业占比上限
CONC_STRUCT = 0.30      # 结构性：集中度下限
CONC_STRONG = 0.20      # 偏强结构性：集中度下限
RC_STRUCT = 0.05        # 结构性：排名一致性下限（≈全历史 q80）
RC_EDGE = 0.02          # 偏强结构性 / 边缘结构性：排名一致性门槛（≈全历史 q70）
UP_STRONG = 50.0        # 上涨过半的分界
UP_MID_LO = 40.0        # 轮动区间下界
UP_WEAK_LO = 30.0       # 偏弱轮动区间下界
RANK_WINDOW = 5         # 排名一致性的滚动窗口（交易日）


# ============================================================
# 1. 特征计算
# ============================================================
def calc_top5_conc(x):
    """Top5 加权涨幅之和 / 全部上涨行业涨幅之和。

    ⚠️ 已知缺陷（2026-10-01）：当上涨行业不足 5 个时，sorted_pcts[:5] 可能包含
    下跌行业，导致比值为负或极端（实测 min=-8.33，全历史 10 天为负，均出现在
    up_pct < 2% 的日子）。因影响面小且修改会波及全部历史标签，暂不修改。
    """
    pcts = x['weighted_pct'].values
    sorted_pcts = np.sort(pcts)[::-1]
    top5_sum = sorted_pcts[:5].sum()
    total_pos = pcts[pcts > 0].sum()
    return top5_sum / total_pos if total_pos > 0 else 0


def compute_daily_features(returns: pd.DataFrame) -> pd.DataFrame:
    """逐日计算 up_pct / mean_ret / std_ret / skew_ret / top5_conc"""
    daily_rows = []
    for d, group in returns.groupby('date'):
        pcts = group['weighted_pct'].values
        up_pct = (pcts > 0).mean() * 100
        mean_ret = pcts.mean()
        std_ret = pcts.std()
        skew_ret = float(
            np.mean((pcts - pcts.mean()) ** 3)
            / (np.mean((pcts - pcts.mean()) ** 2) ** 1.5 + 1e-10)
        )
        daily_rows.append({
            'date': d,
            'up_pct': up_pct,
            'mean_ret': mean_ret,
            'std_ret': std_ret,
            'skew_ret': skew_ret,
            'top5_conc': calc_top5_conc(group),
        })
    return pd.DataFrame(daily_rows).sort_values('date')


def rolling_rank_corr(returns_df: pd.DataFrame, window: int = RANK_WINDOW) -> pd.DataFrame:
    """过去 window 天行业排名的「稳定性」。

    定义：consistency = 1 - mean_i Var_t(rank_{i,t}) / Var(Uniform(1..n))
    - 0  = 排名变动幅度与随机排列相同
    - >0 = 排名比随机更稳定（少数行业持续占优）
    - <0 = 排名比随机更不稳定

    返回列：date / rank_consistency / rank_var_mean
    """
    # 预建 date -> 当日排名 Series，避免循环内反复过滤整表
    rank_by_date = {
        d: g.set_index('ind_code')['weighted_pct'].rank()
        for d, g in returns_df.groupby('date')
    }
    dates = sorted(rank_by_date)

    results = []
    for i, d in enumerate(dates):
        start = 0 if i < window else i - window + 1
        window_dates = dates[start:i + 1]
        if len(window_dates) < 2:
            results.append({'date': d, 'rank_consistency': 0.0, 'rank_var_mean': np.nan})
            continue

        # 转置后：行 = 窗口内各日，列 = 各行业
        rank_df = pd.DataFrame({wd: rank_by_date[wd] for wd in window_dates}).T

        # 各行业在窗口内的排名方差（按列聚合，ddof=1）
        rank_var = rank_df.var(axis=0)
        mean_rank_var = rank_var.mean()

        # 归一化基准：均匀分布 1..n 的**总体**方差
        # ⚠️ 修复（2026-10-01）：原实现为 (n_ind**2-1)/12 * (1 - 1/len(window_dates))，
        #    即 ddof=0 口径下随机排名的期望方差；而分子用的是 ddof=1 的样本方差。
        #    两者相差 W/(W-1) 因子，导致随机排名下 consistency ≡ 1 - W/(W-1) = -0.25。
        #    实测全历史均值 -0.2456 与该理论值吻合，确证为 bug。
        n_ind = rank_df.shape[1]
        max_var = (n_ind ** 2 - 1) / 12

        consistency = 1 - mean_rank_var / max_var if max_var > 0 else 0.0

        results.append({
            'date': d,
            'rank_consistency': consistency,
            'rank_var_mean': mean_rank_var,
        })

    return pd.DataFrame(results)


def rank_autocorr_series(returns_df: pd.DataFrame, lag: int = 1) -> pd.DataFrame:
    """相邻 lag 日行业排名的 Spearman 相关（纯回看，逐日）。

    Spearman = 排名的 Pearson（本机 venv 无 scipy，故在秩上直接算）。
    返回列：date / rank_autocorr
    """
    rank_by_date = {
        d: g.set_index('ind_code')['weighted_pct'].rank()
        for d, g in returns_df.groupby('date')
    }
    dates = sorted(rank_by_date)

    out = []
    for i, d in enumerate(dates):
        if i < lag:
            out.append(np.nan)
            continue
        a, b = rank_by_date[d], rank_by_date[dates[i - lag]]
        common = a.index.intersection(b.index)
        if len(common) < 6:
            out.append(np.nan)
            continue
        av = a.reindex(common).values.astype(float)
        bv = b.reindex(common).values.astype(float)
        av, bv = av - av.mean(), bv - bv.mean()
        den = np.sqrt((av ** 2).sum() * (bv ** 2).sum())
        out.append(float((av * bv).sum() / den) if den > 0 else np.nan)

    return pd.DataFrame({'date': dates, 'rank_autocorr': out})


# ============================================================
# 2. 行情类型判定
# ============================================================
def classify_regime(row) -> str:
    """按优先级判定当日行情类型。

    优先级：全面下跌/全面上涨 → 结构性 → 偏强结构性 → 轮动类 → 边缘结构性 → 混合
    """
    up = row['up_pct']
    rc = row['rank_consistency'] if pd.notna(row['rank_consistency']) else 0.0
    tc = row['top5_conc']

    # ── 方向极端 ──
    if up > UP_FULL_RISE and tc < CONC_FULL_RISE:
        return '全面上涨'          # 普涨：多数行业涨，集中度低
    if up < UP_FULL_FALL:
        return '全面下跌'

    # ── 结构性：少数行业持续领涨，其余不涨 ──
    if up < UP_STRONG and rc > RC_STRUCT and tc > CONC_STRUCT:
        return '结构性'

    # ── 偏强结构性：少数领涨带动了普涨（上涨过半 + 集中 + 排名稳定）──
    if up >= UP_STRONG and tc > CONC_STRONG and rc > RC_EDGE:
        return '偏强结构性'

    # ── 轮动类：排名比随机更乱（涨的行业不停换）──
    if UP_MID_LO <= up < 60 and rc < 0.0:
        return '轮动'
    if UP_WEAK_LO <= up < UP_MID_LO and rc < 0.0:
        return '偏弱轮动'

    # ── 边缘结构性：上涨偏低，排名一致性中等（可能正在形成主线）──
    if UP_WEAK_LO <= up < UP_STRONG and 0.0 <= rc <= RC_STRUCT:
        return '边缘结构性'

    return '混合'


REGIME_ORDER = ['全面上涨', '偏强结构性', '结构性', '边缘结构性',
                '轮动', '偏弱轮动', '混合', '全面下跌']


# ============================================================
# 3. 主流程
# ============================================================
def main() -> int:
    returns = pd.read_parquet(DATA_PATH)

    # ── 逐日特征 ──
    daily_df = compute_daily_features(returns)

    # ── 排名一致性（含 v2 修复） + 排名自相关（v2 新增）──
    rank_cons = rolling_rank_corr(returns, window=RANK_WINDOW)
    daily_df = daily_df.merge(rank_cons, on='date', how='left')
    daily_df = daily_df.merge(rank_autocorr_series(returns, lag=1), on='date', how='left')

    daily_df['regime'] = daily_df.apply(classify_regime, axis=1)

    # ============================================================
    # 4. 统计分析
    # ============================================================
    print("=" * 80)
    print("逐日行情类型判断结果")
    print("=" * 80)

    print("\n行情类型分布:")
    regime_dist = daily_df['regime'].value_counts()
    for r in REGIME_ORDER:
        n = int(regime_dist.get(r, 0))
        if n == 0:
            continue
        print(f"  {r}: {n}天 ({n / len(daily_df) * 100:.1f}%)")

    print("\n各行情类型的特征均值:")
    for regime in REGIME_ORDER:
        sub = daily_df[daily_df['regime'] == regime]
        if len(sub) == 0:
            continue
        print(f"  {regime}: n={len(sub)}, up_pct={sub['up_pct'].mean():.1f}%, "
              f"rank_cons={sub['rank_consistency'].mean():.3f}, "
              f"top5_conc={sub['top5_conc'].mean():.3f}, "
              f"std_ret={sub['std_ret'].mean():.2f}, "
              f"mean_ret={sub['mean_ret'].mean():.3f}%")

    # ── 指标诊断（v2 新增）：让使用者直接看到指标当前处于历史什么位置 ──
    print("\n指标诊断:")
    rc = daily_df['rank_consistency'].dropna()
    ac = daily_df['rank_autocorr'].dropna()
    if len(rc):
        latest_rc = rc.iloc[-1]
        pct = (rc <= latest_rc).mean() * 100
        print(f"  rank_consistency: 均值={rc.mean():+.4f} (0=随机基线), "
              f"最新={latest_rc:+.4f} 处于历史 {pct:.0f}% 分位")
        print(f"    ⚠️ 该指标对未来的预测力为零（详见模块 docstring 效度声明）")
    if len(ac):
        print(f"  rank_autocorr   : 均值={ac.mean():+.4f} 标准差={ac.std():.4f}")

    # ============================================================
    # 5. 月度汇总
    # ============================================================
    print("\n" + "=" * 80)
    print("月度行情类型汇总")
    print("=" * 80)

    daily_df['month'] = daily_df['date'].dt.to_period('M')

    monthly_summary = []
    for month, group in daily_df.groupby('month'):
        regime_counts = group['regime'].value_counts()
        dominant = regime_counts.index[0] if len(regime_counts) > 0 else 'N/A'
        dominant_pct = regime_counts.iloc[0] / len(group) * 100 if len(regime_counts) > 0 else 0

        # 结构性占比（三类合计）
        structural_pct = (
            regime_counts.get('结构性', 0)
            + regime_counts.get('偏强结构性', 0)
            + regime_counts.get('边缘结构性', 0)
        ) / len(group) * 100

        monthly_summary.append({
            'month': str(month),
            'dominant_regime': dominant,
            'dominant_pct': dominant_pct,
            'structural_pct': structural_pct,
            'avg_up_pct': group['up_pct'].mean(),
            'avg_rank_cons': group['rank_consistency'].mean(),
            'avg_top5_conc': group['top5_conc'].mean(),
            'n_days': len(group),
        })

    ms_df = pd.DataFrame(monthly_summary)

    print(f"\n{'月份':>10s} {'主导类型':>12s} {'占比':>6s} {'结构性%':>8s} "
          f"{'up_pct':>8s} {'rank_cons':>10s} {'top5_conc':>10s}")
    print("-" * 70)
    for _, r in ms_df.iterrows():
        print(f"{r['month']:>10s} {r['dominant_regime']:>12s} {r['dominant_pct']:>5.0f}% "
              f"{r['structural_pct']:>7.1f}% {r['avg_up_pct']:>7.1f}% "
              f"{r['avg_rank_cons']:>9.3f} {r['avg_top5_conc']:>9.3f}")

    # ============================================================
    # 6. 行情类型与唤醒信号交互（验证策略适配）
    # ============================================================
    print("\n" + "=" * 80)
    print("策略适配验证：不同行情类型下哪些因子有效")
    print("=" * 80)

    _wake_detail_path = INDUSTRY_DIR / 'wake_backtest_v2_detail.csv'
    if _wake_detail_path.exists():
        detail = pd.read_csv(_wake_detail_path)
        detail['date'] = pd.to_datetime(detail['wake_date'])

        daily_regime_map = dict(zip(daily_df['date'].dt.strftime('%Y-%m-%d'), daily_df['regime']))
        detail['regime'] = detail['date'].dt.strftime('%Y-%m-%d').map(daily_regime_map)

        print("\n唤醒事件在不同行情下的分布:")
        regime_events = detail.groupby('regime')['name'].nunique()
        for r, n in regime_events.items():
            print(f"  {r}: {n}个行业")

        print("\n唤醒5日超额 by regime:")
        for regime in detail['regime'].unique():
            if pd.isna(regime):
                continue
            sub = detail[(detail['regime'] == regime) & (detail['horizon_target'] == 5)]
            if len(sub) < 3:
                print(f"  {regime}: n={len(sub)}, 样本不足")
                continue
            print(f"  {regime}: n={len(sub)}, 超额={sub['excess'].mean():.2f}%, "
                  f"胜率={(sub['excess'] > 0).mean():.0%}")
    else:
        print(f"\n⚠️ 唤醒回测明细不存在，跳过策略适配验证: {_wake_detail_path}")

    # ============================================================
    # 7. 接入情绪数据（批次2 消费方）
    # ============================================================
    # 设计原则：**只并入列，不改分类逻辑**。
    #   regime 的判定阈值是长期校准出来的，贸然把情绪指标塞进判定式会让历史
    #   口径不可比。所以这一步先把情绪列 join 进 daily_regime.parquet，并输出
    #   「各行情类型下的情绪指标均值」——先证明它有区分度，再考虑是否进入判定。
    SENTI_COLS = ['margin_bal', 'margin_bal_chg_pct', 'margin_rz_buy_share', 'margin_leverage',
                  'lhb_stock_n', 'lhb_net', 'lhb_inst_net', 'lhb_hsgt_net',
                  'block_amt', 'block_disc',
                  'north_top10_amt', 'north_top10_net', 'south_hold_ratio', 'north_hold_ratio']

    if SENTI_PATH.exists():
        senti = pd.read_parquet(SENTI_PATH)
        senti['date'] = pd.to_datetime(senti['date'])
        # 两融余额环比：权威值由 build_sentiment_daily.py 产出（已做跨缺口掩码），
        # 这里只在列缺失时兜底，避免两处各算一遍导致口径不一致。
        # ⚠️ 兜底分支必须显式 fill_method=None：pandas 默认 'pad' 会把缺值前向填充后
        #    算出 0.0，让「无数据」伪装成「零变动」（2026-09-24 踩过）。
        if 'margin_bal' in senti.columns and 'margin_bal_chg_pct' not in senti.columns:
            print("⚠️ 情绪表缺 margin_bal_chg_pct 列，本地兜底计算"
                  "（口径可能不一致，建议重跑 build_sentiment_daily.py）")
            senti = senti.sort_values('date')
            senti['margin_bal_chg_pct'] = senti['margin_bal'].pct_change(fill_method=None) * 100

        have = [c for c in SENTI_COLS if c in senti.columns]
        daily_df = daily_df.merge(senti[['date'] + have], on='date', how='left')
        n_hit = int(daily_df[have[0]].notna().sum()) if have else 0
        print("\n" + "=" * 80)
        print(f"情绪数据接入：{SENTI_PATH}")
        print(f"  并入 {len(have)} 列，命中 {n_hit}/{len(daily_df)} 天"
              f"（缺失日=该情绪数据尚未回溯到）")
        print(f"  列：{have}")

        if n_hit > 20:
            print("\n各行情类型的情绪指标均值（先看区分度，再决定是否进判定式）:")
            hdr = f"  {'行情类型':<12s} {'n':>4s}"
            for c in ['margin_bal_chg_pct', 'margin_rz_buy_share', 'lhb_inst_net',
                      'block_disc', 'north_top10_amt']:
                if c in daily_df.columns:
                    hdr += f" {c[:16]:>18s}"
            print(hdr)
            print("  " + "-" * (len(hdr) - 2))
            scales = {'margin_bal_chg_pct': 1, 'margin_rz_buy_share': 1,
                      'lhb_inst_net': 1e8, 'block_disc': 1, 'north_top10_amt': 1e8}
            for regime in REGIME_ORDER:
                sub = daily_df[daily_df['regime'] == regime]
                if len(sub) == 0:
                    continue
                n_senti = int(sub['margin_bal_chg_pct'].notna().sum()) \
                    if 'margin_bal_chg_pct' in sub.columns else 0
                line = f"  {regime:<12s} {n_senti:>4d}"
                for c in ['margin_bal_chg_pct', 'margin_rz_buy_share', 'lhb_inst_net',
                          'block_disc', 'north_top10_amt']:
                    if c in sub.columns:
                        v = sub[c].mean()
                        line += f" {v / scales[c]:>18.2f}" if pd.notna(v) else f" {'-':>18s}"
                print(line)
    else:
        print(f"\n⚠️ 情绪汇总表不存在，跳过接入: {SENTI_PATH}"
              f"\n   （跑 quant-data/scripts/build_sentiment_daily.py 生成）")

    # ============================================================
    # 8. 保存（带写入护栏）
    # ============================================================
    # 本脚本是全量重算，正常情况下新结果行数不应少于旧文件。
    # 若输入 industry_weighted_returns.parquet 被截断或损坏，输出会骤然变少——
    # 此时拒绝写入，避免用残缺数据覆盖健康的历史（regime 是报告与台账评估的输入）。
    if OUT_PATH.exists():
        try:
            prev_n = len(pd.read_parquet(OUT_PATH, columns=['date']))
            new_n = len(daily_df)
            if prev_n > 0 and new_n < prev_n * 0.9:
                print(f"\n❌ 拒绝写入：新结果 {new_n} 行 < 旧文件 {prev_n} 行的 90%。")
                print(f"   请检查输入 {DATA_PATH.name} 是否完整后重跑。")
                return 1
            print(f"\n行数护栏: 旧 {prev_n} → 新 {new_n} 行 ✓")
        except Exception as e:
            print(f"\n⚠️ 旧文件读取失败（{e}），跳过行数护栏")

    written = save_dataframe(
        daily_df, OUT_PATH,
        producer="scripts/regime_classifier.py",
        params={"classifier": "regime_v2", "validity": "state-description-only"},
        inputs=[DATA_PATH, SENTI_PATH],
    )
    print(f"\n已保存: {written}")
    print(f"  {daily_df.shape[0]}天 x {daily_df.shape[1]}列")

    written_ms = save_dataframe(
        ms_df, OUT_PATH.parent / 'monthly_regime_summary.csv',
        producer="scripts/regime_classifier.py",
        params={"classifier": "regime_v2", "aggregation": "monthly"},
        inputs=[DATA_PATH],
    )
    print(f"  月度汇总: {written_ms}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
