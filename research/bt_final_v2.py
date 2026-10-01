"""模块二复核回测最终版v2 — 用绝对收益(匹配小克定义)"""
import pandas as pd, numpy as np, warnings
warnings.filterwarnings('ignore')
D = '/opt/data/quant-data'

idf = pd.read_parquet(f'{D}/industry/industry_daily_full.parquet')
im = pd.read_parquet(f'{D}/industry/industry_members.parquet')
nm = im.drop_duplicates('ind_code')[['ind_code','ind_name']].rename(columns={'ind_code':'con_code'})
idf = idf.merge(nm, on='con_code', how='left').sort_values(['con_code','date']).reset_index(drop=True)

# 含当日前20日均宽
idf['pb20'] = idf.groupby('con_code')['breadth'].transform(lambda x: x.rolling(20, min_periods=10).mean())
# 连续扩张
idf['bdiff'] = idf.groupby('con_code')['breadth'].diff()
idf['is_exp'] = idf['bdiff'] > 0
def _cc(s):
    r = pd.Series(0, index=s.index, dtype=int); c = 0
    for i in range(len(s)):
        if s.iloc[i]: c += 1; r.iloc[i] = c
        else: c = 0
    return r
idf['exp_d'] = idf.groupby('con_code')['is_exp'].transform(_cc)
# D1占比
idf['d1'] = idf.groupby('con_code')['bdiff'].shift(2)
idf['d2'] = idf.groupby('con_code')['bdiff'].shift(1)
idf['d3'] = idf['bdiff']
idf['d1r'] = np.where((idf['exp_d']>=3)&((idf['d1']+idf['d2']+idf['d3'])>0), idf['d1']/(idf['d1']+idf['d2']+idf['d3']), np.nan)
# 放量
idf['am3'] = idf.groupby('con_code')['total_amount'].transform(lambda x: x.rolling(3, min_periods=2).mean())
idf['am20'] = idf.groupby('con_code')['total_amount'].transform(lambda x: x.rolling(20, min_periods=10).mean().shift(1))
idf['vr'] = idf['am3'] / idf['am20']
# 冷区+信号
idf['cold'] = idf['pb20'] <= 0.30
idf['sig'] = idf['cold'] & (idf['exp_d'] >= 3)
# 前向绝对收益(不减大盘)
for n in [5,10,20]:
    parts = []
    for _, g in idf.groupby('con_code'):
        g = g.sort_values('date')
        parts.append(pd.Series(g['avg_pct'].rolling(n,min_periods=n).sum().shift(-n), index=g.index))
    idf[f'fwd{n}'] = pd.concat(parts).sort_index()
idf['win20'] = idf['fwd20'] > 0
# 超额收益(减大盘)
mkt = idf.groupby('date')['avg_pct'].mean().reset_index()
mkt.columns = ['date','mkt_pct']
idf = idf.merge(mkt, on='date', how='left')
for n in [20]:
    parts = []
    for _, g in idf.groupby('con_code'):
        g = g.sort_values('date')
        parts.append(pd.Series(g['avg_pct'].rolling(n,min_periods=n).sum().shift(-n) - g['mkt_pct'].rolling(n,min_periods=n).sum().shift(-n), index=g.index))
    idf[f'exc{n}'] = pd.concat(parts).sort_index()
print("数据准备完成")

# 评分
S = idf[idf['sig']].copy()
def _sc(r):
    b=1; pb=r['pb20']
    ps=3 if pb<0.10 else (2 if pb<0.20 else (1 if pb<0.30 else 0)) if pd.notna(pb) else 0
    d1r=r['d1r']
    ds=3 if d1r>=0.60 else (2 if d1r>=0.50 else (1 if d1r>=0.40 else 0)) if pd.notna(d1r) else 0
    vr=r['vr']
    vs=3 if vr>=1.3 else (2 if vr>=1.1 else (1 if vr>=1.0 else 0)) if pd.notna(vr) else 0
    return b+ps+ds+vs
S['score'] = S.apply(_sc, axis=1)

# §四
print("\n"+"="*60+"\n§四 冷区信号按前20日均宽分层(绝对收益)\n"+"="*60)
for lb,lo,hi,xn,xe,xw in [('<10%',0,0.10,280,9.06,84),('10-20%',0.10,0.20,1499,3.41,68),('20-30%',0.20,0.31,2507,2.46,65)]:
    sub=S[(S['pb20']>=lo)&(S['pb20']<hi)]
    e=sub['fwd20'].dropna(); w=sub['win20'].mean()
    print(f"  {lb}: N={len(sub)}(小克{xn}), 收益={e.mean():.2f}%(小克{xe:.2f}%), 胜率={w:.1%}(小克{xw}%), 差={e.mean()-xe:.2f}pp/{w*100-xw:.1f}pp")

# §八
print("\n"+"="*60+"\n§八 叠加效果(绝对收益)\n"+"="*60)
for lb,sub,xn,xe,xw in [
    ('仅触发',S,2432,2.02,62),
    ('①<20%+③≥1.1',S[(S['pb20']<0.20)&(S['vr']>=1.1)],467,2.91,66),
    ('②≥50%+③≥1.1',S[(S['d1r']>=0.50)&(S['vr']>=1.1)],205,3.38,68),
    ('②≥60%+③≥1.1',S[(S['d1r']>=0.60)&(S['vr']>=1.1)],111,4.20,73),
    ('①<10%+②≥60%+③≥1.2',S[(S['pb20']<0.10)&(S['d1r']>=0.60)&(S['vr']>=1.2)],50,4.50,74),
]:
    e=sub['fwd20'].dropna(); w=sub['win20'].mean()
    print(f"  {lb}: N={len(sub)}(小克{xn}), 收益={e.mean():.2f}%(小克{xe:.2f}%), 胜率={w:.1%}(小克{xw}%)")

# §3.5
print("\n"+"="*60+"\n§3.5 得分档位(绝对收益)\n"+"="*60)
nd=S['date'].nunique()
for th,xn_d,xe,xw,xir in [(3,1.1,1.65,62,1.15),(5,0.4,3.26,67,1.54),(7,0.1,6.71,74,2.43)]:
    sub=S[S['score']>=th]; e=sub['fwd20'].dropna(); w=sub['win20'].mean()
    ir=e.mean()/e.std()*np.sqrt(252) if e.std()>0 else 0
    print(f"  ≥{th}: N={len(sub)}, 日均{len(sub)/nd:.2f}(小克{xn_d}), 收益={e.mean():.2f}%(小克{xe}%), 胜率={w:.1%}(小克{xw}%), IR={ir:.2f}(小克{xir})")

# ABC四组
print("\n"+"="*60+"\nABC四组对照(绝对收益)\n"+"="*60)
for lb,m in [('A:纯扩张≥3',idf['exp_d']>=3),('B:纯冷区≤30%',idf['cold']),('C:冷区+扩张',idf['sig']),('D:热区+扩张',(idf['pb20']>0.70)&(idf['exp_d']>=3))]:
    g=idf[m]; e=g['fwd20'].dropna(); w=g['win20'].mean()
    t=e.mean()/(e.std()/np.sqrt(len(e))) if len(e)>1 else 0
    print(f"  {lb}: N={len(g)}, 收益={e.mean():.2f}%, 胜率={w:.1%}, t={t:.2f}")

# 扩张天数
print("\n"+"="*60+"\n§一 扩张天数(绝对收益)\n"+"="*60)
for d in [3,5,7]:
    sub=idf[idf['exp_d']>=d]; e=sub['fwd20'].dropna(); w=sub['win20'].mean()
    print(f"  ≥{d}天: N={len(sub)}, 收益={e.mean():.2f}%, 胜率={w:.1%}")
print("  小克: ≥3天0.89%/61.5%; ≥5天1.82%/67.8%")

# 三阶段
print("\n"+"="*60+"\n三阶段稳定性(绝对收益)\n"+"="*60)
for lb,lo,hi in [('P1结构牛','2020','2021'),('P2震荡熊','2022','2024'),('P3政策牛','2024','2026')]:
    sub=S[(S['date']>=f'{lo}-01-01')&(S['date']<=f'{hi}-12-31')]
    e=sub['fwd20'].dropna(); w=sub['win20'].mean()
    print(f"  {lb}: N={len(sub)}, 收益={e.mean():.2f}%, 胜率={w:.1%}")

# 加速vs减速
print("\n"+"="*60+"\n§七 加速vs减速(绝对收益)\n"+"="*60)
e3=idf[idf['exp_d']>=3].copy()
e3['dec']=(e3['d1']>e3['d2'])&(e3['d2']>e3['d3'])&(e3['d1']>0)
e3['acc']=(e3['d1']<e3['d2'])&(e3['d2']<e3['d3'])&(e3['d3']>0)
for lb,c in [('减速','dec'),('加速','acc')]:
    sub=e3[e3[c]]; e=sub['fwd20'].dropna(); w=sub['win20'].mean()
    print(f"  全样本{lb}: N={len(sub)}, 收益={e.mean():.2f}%, 胜率={w:.1%}")
ce3=e3[e3['sig']]
for lb,c in [('冷区减速','dec'),('冷区加速','acc')]:
    sub=ce3[ce3[c]]; e=sub['fwd20'].dropna(); w=sub['win20'].mean()
    print(f"  {lb}: N={len(sub)}, 收益={e.mean():.2f}%, 胜率={w:.1%}")
print("  小克: 减速N=888/冷区减速N=275+2.60%; 加速N=998/冷区加速N=419+1.67%")

# 因果归因
print("\n"+"="*60+"\n因果归因: 冷区中扩张 vs 无扩张(绝对收益)\n"+"="*60)
cold_all=idf[idf['cold']]
ce=cold_all[cold_all['exp_d']>=3]; cn=cold_all[cold_all['exp_d']<3]
for lb,sub in [('冷区+扩张',ce),('冷区无扩张',cn)]:
    e=sub['fwd20'].dropna(); w=sub['win20'].mean()
    t=e.mean()/(e.std()/np.sqrt(len(e))) if len(e)>1 else 0
    print(f"  {lb}: N={len(sub)}, 收益={e.mean():.2f}%, 胜率={w:.1%}, t={t:.2f}")
diff=ce['fwd20'].mean()-cn['fwd20'].mean()
print(f"  扩张增量: {diff:.2f}%")

# 热区
print("\n"+"="*60+"\n热区候选(绝对收益)\n"+"="*60)
hot=idf[idf['pb20']>0.70]
for lb,m in [('急跌(<-3%)',hot['avg_pct']<-3),('5日跌(<-2%)',hot.groupby('con_code')['avg_pct'].transform(lambda x:x.rolling(5,min_periods=5).sum())<-2)]:
    sub=hot[m]; e=sub['fwd20'].dropna(); w=sub['win20'].mean()
    print(f"  热区{lb}: N={len(sub)}, 收益={e.mean():.2f}%, 胜率={w:.1%}")
print("  小克: 急跌N=1087+3.01%/65%; 温跌N=1873+2.95%/63%")

# §一超额版本(小克明确写"超额"的)
print("\n"+"="*60+"\n§一 扩张天数(超额版本,匹配小克'超额'标注)\n"+"="*60)
for d in [3,5]:
    sub=idf[idf['exp_d']>=d]; e=sub['exc20'].dropna(); w=(sub['exc20']>0).mean()
    print(f"  ≥{d}天: N={len(sub)}, 超额={e.mean():.2f}%, 胜率={w:.1%}")
print("  小克: ≥3天超额0.89%/胜率61.5%; ≥5天超额1.82%/胜率67.8%")

print("\n=== 完成 ===")
