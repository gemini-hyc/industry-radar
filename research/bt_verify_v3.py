"""复核关键验证: §八事件去重+超额收益"""
import pandas as pd, numpy as np, warnings
warnings.filterwarnings('ignore')
D = '/opt/data/quant-data'

idf = pd.read_parquet(f'{D}/industry/industry_daily_full.parquet')
im = pd.read_parquet(f'{D}/industry/industry_members.parquet')
nm = im.drop_duplicates('ind_code')[['ind_code','ind_name']].rename(columns={'ind_code':'con_code'})
idf = idf.merge(nm, on='con_code', how='left').sort_values(['con_code','date']).reset_index(drop=True)

idf['pb20'] = idf.groupby('con_code')['breadth'].transform(lambda x: x.rolling(20, min_periods=10).mean())
idf['bdiff'] = idf.groupby('con_code')['breadth'].diff()
idf['is_exp'] = idf['bdiff'] > 0
def _cc(s):
    r = pd.Series(0, index=s.index, dtype=int); c = 0
    for i in range(len(s)):
        if s.iloc[i]: c += 1; r.iloc[i] = c
        else: c = 0
    return r
idf['exp_d'] = idf.groupby('con_code')['is_exp'].transform(_cc)
idf['cold'] = idf['pb20'] <= 0.30
idf['sig_all'] = idf['cold'] & (idf['exp_d'] >= 3)
idf['sig_first'] = idf['cold'] & (idf['exp_d'] == 3)

idf['d1'] = idf.groupby('con_code')['bdiff'].shift(2)
idf['d2'] = idf.groupby('con_code')['bdiff'].shift(1)
idf['d3'] = idf['bdiff']
idf['d1r'] = np.where((idf['exp_d']>=3)&((idf['d1']+idf['d2']+idf['d3'])>0), idf['d1']/(idf['d1']+idf['d2']+idf['d3']), np.nan)
idf['am3'] = idf.groupby('con_code')['total_amount'].transform(lambda x: x.rolling(3, min_periods=2).mean())
idf['am20'] = idf.groupby('con_code')['total_amount'].transform(lambda x: x.rolling(20, min_periods=10).mean().shift(1))
idf['vr'] = idf['am3'] / idf['am20']

# 前向收益(先算再筛选)
mkt = idf.groupby('date')['avg_pct'].mean().reset_index()
mkt.columns = ['date','mkt_pct']
idf = idf.merge(mkt, on='date', how='left')
for n in [20]:
    parts_abs = []
    parts_exc = []
    for _, g in idf.groupby('con_code'):
        g = g.sort_values('date')
        parts_abs.append(pd.Series(g['avg_pct'].rolling(n,min_periods=n).sum().shift(-n), index=g.index))
        parts_exc.append(pd.Series(g['avg_pct'].rolling(n,min_periods=n).sum().shift(-n) - g['mkt_pct'].rolling(n,min_periods=n).sum().shift(-n), index=g.index))
    idf['abs20'] = pd.concat(parts_abs).sort_index()
    idf['exc20'] = pd.concat(parts_exc).sort_index()
idf['win_abs'] = idf['abs20'] > 0
idf['win_exc'] = idf['exc20'] > 0

# ============ §八: 事件去重(exp_d=3) ============
S = idf[idf['sig_first']].copy()
print(f"§八事件去重N={len(S)} (小克: 2432)")

def _sc(r):
    b=1; pb=r['pb20']
    ps=3 if pb<0.10 else (2 if pb<0.20 else (1 if pb<0.30 else 0)) if pd.notna(pb) else 0
    d1r=r['d1r']
    ds=3 if d1r>=0.60 else (2 if d1r>=0.50 else (1 if d1r>=0.40 else 0)) if pd.notna(d1r) else 0
    vr=r['vr']
    vs=3 if vr>=1.3 else (2 if vr>=1.1 else (1 if vr>=1.0 else 0)) if pd.notna(vr) else 0
    return b+ps+ds+vs
S['score'] = S.apply(_sc, axis=1)

print("\n§八 叠加效果对照 — 绝对收益 vs 超额收益")
for lb,sub,xn,xe,xw in [
    ('仅触发',S,2432,2.02,62),
    ('①<20%+③≥1.1',S[(S['pb20']<0.20)&(S['vr']>=1.1)],467,2.91,66),
    ('②≥50%+③≥1.1',S[(S['d1r']>=0.50)&(S['vr']>=1.1)],205,3.38,68),
    ('②≥60%+③≥1.1',S[(S['d1r']>=0.60)&(S['vr']>=1.1)],111,4.20,73),
]:
    e_abs=sub['abs20'].dropna(); e_exc=sub['exc20'].dropna()
    w_abs=sub['win_abs'].mean(); w_exc=sub['win_exc'].mean()
    print(f"  {lb}: N={len(sub)}(小克{xn})")
    print(f"    绝对收益={e_abs.mean():.2f}%, 超额收益={e_exc.mean():.2f}%, 小克={xe:.2f}%")
    print(f"    胜率(abs)={w_abs:.1%}, 胜率(exc)={w_exc:.1%}, 小克={xw}%")

# ============ §一: 扩张天数 ============
print("\n§一 扩张天数对照 — 绝对收益 vs 超额收益")
for d in [3,5]:
    sub=idf[idf['exp_d']>=d]
    e_abs=sub['abs20'].dropna().mean(); e_exc=sub['exc20'].dropna().mean()
    w_abs=sub['win_abs'].mean(); w_exc=sub['win_exc'].mean()
    print(f"  ≥{d}天: N={len(sub)}, 绝对={e_abs:.2f}%, 超额={e_exc:.2f}%")
    print(f"    胜率(abs)={w_abs:.1%}, 胜率(exc)={w_exc:.1%}")

# 小克§一写的"超额": ≥3天0.89%/61.5%; ≥5天1.82%/67.8%
# 我的超额: ≥3天0.34%/47.3%; ≥5天0.65%/50.8%
# 我的绝对: ≥3天2.40%/60.7%; ≥5天3.33%/67.5%
# 小克的数值介于绝对和超额之间 → 可能用的是不同的基准

# ============ §3.5: 得分档位(事件去重版) ============
print("\n§3.5 得分档位(事件去重版) — 绝额 vs 超额")
nd=S['date'].nunique()
for th,xn_d,xe,xw,xir in [(3,1.1,1.65,62,1.15),(5,0.4,3.26,67,1.54),(7,0.1,6.71,74,2.43)]:
    sub=S[S['score']>=th]
    e_abs=sub['abs20'].dropna(); e_exc=sub['exc20'].dropna()
    w_abs=sub['win_abs'].mean(); w_exc=sub['win_exc'].mean()
    ir_exc=e_exc.mean()/e_exc.std()*np.sqrt(252) if e_exc.std()>0 else 0
    ir_abs=e_abs.mean()/e_abs.std()*np.sqrt(252) if e_abs.std()>0 else 0
    print(f"  ≥{th}: N={len(sub)}, 日均{len(sub)/nd:.2f}(小克{xn_d})")
    print(f"    绝额={e_abs.mean():.2f}%, 超额={e_exc.mean():.2f}%, 小克={xe}%")
    print(f"    胜率(abs)={w_abs:.1%}, 胜率(exc)={w_exc:.1%}, 小克={xw}%")
    print(f"    IR(exc)={ir_exc:.2f}, IR(abs)={ir_abs:.2f}, 小克={xir}")

# ============ §四: 所有天数版(事件去重版) ============
print("\n§四 分层(事件去重版) — 绝额 vs 超额")
for lb,lo,hi,xn,xe,xw in [('<10%',0,0.10,280,9.06,84),('10-20%',0.10,0.20,1499,3.41,68),('20-30%',0.20,0.31,2507,2.46,65)]:
    sub=S[(S['pb20']>=lo)&(S['pb20']<hi)]
    e_abs=sub['abs20'].dropna(); e_exc=sub['exc20'].dropna()
    w_abs=sub['win_abs'].mean(); w_exc=sub['win_exc'].mean()
    print(f"  {lb}: N={len(sub)}(小克{xn})")
    print(f"    绝额={e_abs.mean():.2f}%(小克{xe:.2f}%), 超额={e_exc.mean():.2f}%")
    print(f"    胜率(abs)={w_abs:.1%}(小克{xw}%), 胜率(exc)={w_exc:.1%}")

print("\n=== 完成 ===")
