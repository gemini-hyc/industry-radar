#!/usr/bin/env python3
"""
贝叶斯行业反转跟踪引擎 — 系统A

正向逐日扫描，反转信号触发后每日更新分数 Sₜ = Sₜ₋₁ × λₜ
S₀ = 放大器(C1) × 基础分(C3)，C0/C2/C4已废弃(2026-08-02)
"""
import json, sys, time, logging
from pathlib import Path

logger = logging.getLogger(__name__)
from datetime import datetime, date
import pandas as pd
import numpy as np

# ── 配置（统一走 src/paths.py，消除容器路径硬编码）──
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.paths import INDUSTRY_DIR, FACTOR_DIR

STATE_FILE = INDUSTRY_DIR / "tracker_state.json"
REPORT_FILE = INDUSTRY_DIR / "tracker_report.csv"
WEIGHTED_PATH = INDUSTRY_DIR / "industry_weighted_returns.parquet"
CROWD_FACTOR_PATH = FACTOR_DIR / "ind_crowd_turnover_daily.parquet"

# 反转信号阈值
REVERSAL_BREADTH_MAX    = 0.45   # C1 沉睡上限（20日均宽度<45%）
REVERSAL_PCT_MIN      = 1.5    # C3 涨幅下限（单日加权涨幅>1.5%）
# C0(MA21<MA89) / C2(宽度跳升) / C4(放量确认) 已于 2026-08-02 废弃
RETIRE_BW     = 0.15   # 剔除宽度

# 跟踪期参数
MAX_TRACK_DAYS = 30          # 最长跟踪天数
ELIMINATE_SCORE = 30         # 持续低于此分→剔除
ELIMINATE_DAYS = 3           # 连续天数 (07-03 从5下调)
BREADTH_ELIMINATE_DAYS = 2   # 宽度剔除需连续天数 (07-16 新增，与低分剔除对称)
GRACE_DAYS = 3               # 反转信号后保护期天数 (07-16 新增，豁免剔除检查)

# 四阶段生命周期参数
GOLDEN_CROSS_DAYS = 3       # 金叉确认天数
GOLDEN_CROSS_GAP = 0.005    # 金叉间距阈值(0.5%)
GOLDEN_CROSS2_DAYS = 5      # 二次金叉确认天数
GOLDEN_CROSS2_GAP = 0.01    # 二次金叉间距阈值(1.0%)
DEATH_CROSS_DAYS = 3        # 死叉确认天数
R_THRESHOLD = 0.08           # 真上涨阈值(8%)
ALARM_FALLBACK_CROSS_DAYS = 2    # 保底预警: MA5下穿MA21确认天数
ALARM_FALLBACK_CROSS_GAP = 0.005 # 保底预警: MA5低于MA21的间距阈值(0.5%)

# 标准预警条件参数（2-of-4触发机制）
ALARM_BW13_DECLINE_DAYS = 3    # A1: 宽度(MA13基准)连续低于5日均宽的天数
ALARM_CROWD_HOT_DAYS = 3       # A2: 拥挤度连续>90%的天数
ALARM_CROWD_HOT_PCT = 0.90     # A2: 拥挤度过热阈值
ALARM_BIG_SHADOW_PCT = 0.05    # A3: 大实体阴线阈值(收盘/开盘-1 < -5%)
ALARM_HUGE_VOL_RATIO = 1.5     # A4: 单日巨量(当日/5日均量 > 1.5)
ALARM_VOL_MA_DAYS = 5          # A4: 巨量比较基期(近N日均量)
ALARM_MIN_SIGNALS = 2          # 触发预警所需最少信号数
ALARM_RECOVERY_DAYS = 3        # 预警消退确认: 创新高后连续N日不跌破原P_max

# ── 反转信号初始分 S₀ 计算 ──────────────────
# 方案B: C1=乘性放大器 × C3=基础分，C2/C4已废弃
# S₀ = clamp(amplifier(C1) × base(C3), 0, 100)

def _amplifier_c1(bw20_pct):
    """沉睡深度放大器：越深睡→反转力度越大"""
    if bw20_pct < 5:   return 2.5
    if bw20_pct < 10:  return 2.0
    if bw20_pct < 20:  return 1.5
    if bw20_pct < 35:  return 1.2
    if bw20_pct < 45:  return 1.0
    return 0.5  # 边界情况：轻度沉睡，弱放大

def _base_c3(pct):
    """涨幅基础分：涨幅越大→反转信号越强"""
    if pct > 5:   return 40
    if pct > 3:   return 32
    if pct > 2:   return 24
    if pct > 1.5: return 16
    return 0

def compute_S0(bw20_pct, pct):
    return min(round(_amplifier_c1(bw20_pct) * _base_c3(pct)), 100)

# ── 每日更新因子 λₜ ──────────────────────────

def _lambda_breadth(bw_change):
    """今日宽度变化 (pp) → 宽度因子"""
    if bw_change > 10:   return 1.4
    if bw_change > 3:    return 1.2
    if bw_change >= -3:  return 1.0
    if bw_change >= -10: return 0.8
    return 0.5

def _lambda_pct(pct):
    """当日涨幅 → 涨跌因子"""
    if pct > 3:   return 1.3
    if pct >= 0:  return 1.1
    if pct >= -2: return 0.85
    if pct >= -5: return 0.7
    return 0.4

def _lambda_amt(amt_ratio):
    """当日成交额 / 昨日成交额 → 量能因子"""
    if amt_ratio > 1.3: return 1.3
    if amt_ratio >= 1.0:return 1.1
    if amt_ratio >= 0.8:return 0.9
    return 0.7

def _lambda_crowd_direction(crowd_today, crowd_baseline):
    """今日拥挤度 − 前5日拥挤度中位数 → 拥挤度方向因子 (07-16: 基线从昨日改为5日MA)

    crowd 值范围 0~1（百分位），5日变化幅度天然小于日间变化，阈值相应收紧。
    w₄ 信号弱于 w₁/w₂/w₃，体现在 compute_lambda 中用 ^0.6 折权。
    """
    if crowd_today is None or crowd_baseline is None:
        return 1.0
    change = crowd_today - crowd_baseline
    if change > 0.05:     return 1.3   # 拥挤度持续攀升
    if change > 0.01:     return 1.1   # 小幅攀升
    if change >= -0.01:   return 1.0   # 基本持平
    if change >= -0.05:   return 0.9   # 小幅回落
    return 0.7                          # 持续回落

def compute_lambda(today, yesterday, crowd_today=None, crowd_baseline=None):
    """计算综合更新因子 λₜ

    w₁=宽度方向, w₂=涨跌方向, w₃=量能方向, w₄=拥挤度方向(^0.6折权)
    crowd_baseline 为前5日拥挤度中位数 (07-16: 从昨日改为5日MA)
    """
    try:
        c1 = _lambda_breadth((today["breadth"] - yesterday["breadth"]) * 100)
    except Exception as e:
        logger.warning("计算宽度因子失败: %s", e)
        c1 = 1.0
    try:
        c2 = _lambda_pct(today["avg_pct"] or 0)
    except (TypeError, ValueError) as e:
        logger.warning("计算涨跌因子失败: %s", e)
        c2 = 1.0
    try:
        amt_ratio = today["total_amount"] / yesterday["total_amount"] if yesterday["total_amount"] > 0 else 1.0
    except Exception as e:
        logger.warning("计算量能因子失败: %s", e)
        amt_ratio = 1.0
    c3 = _lambda_amt(amt_ratio)
    c4 = _lambda_crowd_direction(crowd_today, crowd_baseline)
    return (c1 * c2 * c3 * c4 ** 0.6) ** (1 / 3.6)

# ── 核心引擎 ─────────────────────────────────

class BayesianTracker:
    def __init__(self):
        self.state = {}  # ind_code → {status, signal_date, S0, score, days, low_days, last_date}
        self.industry_list = None
        self.daily_data = None
        self.name_to_code = {}
        self.code_to_name = {}
        self.ma_bench = {}  # (ind_code, date_str) → {MA21, MA89, price, below}
        self._load_state()
    
    # ── 数据加载 ──
    
    def load_daily_data(self, days=250, end_date=None):
        """加载所有行业日频数据"""
        from src.utils.market_data import load_recent_market, compute_concept_daily, load_industry_members
        
        print("[加载] 行业日频数据...")
        members = load_industry_members()
        mkt = load_recent_market(days=days, end_date=end_date)
        daily = compute_concept_daily(mkt, members)
        
        # 替换为总市值加权
        if WEIGHTED_PATH.exists():
            wr = pd.read_parquet(WEIGHTED_PATH)
            wr["date"] = pd.to_datetime(wr["date"])
            daily = daily.merge(
                wr[["date", "ind_code", "weighted_pct"]],
                left_on=["date", "con_code"],
                right_on=["date", "ind_code"],
                how="left",
            )
            mask = daily["weighted_pct"].notna()
            daily.loc[mask, "avg_pct"] = daily.loc[mask, "weighted_pct"]
            daily = daily.drop(columns=["ind_code", "weighted_pct"])
        
        self.daily_data = daily.sort_values(["con_code", "date"]).reset_index(drop=True)
        
        # 行业名称映射
        list_path = INDUSTRY_DIR / "industry_list.parquet"
        if list_path.exists():
            self.industry_list = pd.read_parquet(list_path)
            self.name_to_code = dict(zip(self.industry_list["name"], self.industry_list["ts_code"]))
            self.code_to_name = dict(zip(self.industry_list["ts_code"], self.industry_list["name"]))
        else:
            # fallback: 从 daily_data 的 con_code 唯一值构造
            logger.warning("行业列表文件不存在，从 daily_data.con_code 唯一值构造")
            codes = sorted(self.daily_data["con_code"].unique())
            self.industry_list = pd.DataFrame({"ts_code": codes, "name": codes})
            self.name_to_code = {c: c for c in codes}
            self.code_to_name = {c: c for c in codes}
        
        print(f"[加载] 完成: {len(self.daily_data)} 行, {len(self.daily_data['con_code'].unique())} 行业")
        # 加载拥挤度因子
        self._load_crowd_factor()
        # 预计算行业均线MA21/MA89（金叉/死叉判断用）
        self._compute_moving_averages()
        return self
    
    def _compute_moving_averages(self):
        """从加权涨跌幅反推行业价格指数，计算MA21/MA89

        MA89不可用时默认视为下方（没有证据证明在上方世界，就当它在下方）
        结果存入 self.ma_bench: {(ind_code, date_str): {"MA21": float, "MA89": float|None, "price": float, "below": bool}}

        注：MA数据用于四阶段生命周期中的金叉/死叉判断。
        """
        if not WEIGHTED_PATH.exists():
            logger.warning("加权涨跌幅文件不存在，跳过MA计算，金叉/死叉判断将不可用")
            self.ma_bench = {}
            return
        
        wr = pd.read_parquet(WEIGHTED_PATH)
        wr["date"] = pd.to_datetime(wr["date"])
        
        # 构建行业价格指数（基期=1000）
        wr = wr.sort_values(["ind_code", "date"])
        price_rows = []
        for code, grp in wr.groupby("ind_code"):
            prices = 1000 * np.cumprod(1 + grp["weighted_pct"].values / 100)
            tmp = grp[["date", "ind_code"]].copy()
            tmp["price"] = prices
            # 计算均线
            tmp["MA5"] = tmp["price"].rolling(5, min_periods=5).mean()
            tmp["MA21"] = tmp["price"].rolling(21, min_periods=21).mean()
            tmp["MA89"] = tmp["price"].rolling(89, min_periods=89).mean()
            price_rows.append(tmp)
        
        price_df = pd.concat(price_rows, ignore_index=True)
        price_df["date_str"] = price_df["date"].dt.strftime("%Y-%m-%d")
        
        # 构建查找表
        self.ma_bench = {}
        for row in price_df.itertuples(index=False):
            date_str = row.date_str
            ind_code = row.ind_code
            ma5 = row.MA5
            ma21 = row.MA21
            ma89 = row.MA89
            # MA89不可用(NaN) → 默认在下方
            ma5_valid = not (pd.isna(ma5) if isinstance(ma5, float) else False)
            ma21_valid = not (pd.isna(ma21) if isinstance(ma21, float) else False)
            ma89_valid = not (pd.isna(ma89) if isinstance(ma89, float) else False)
            below = True if (not ma21_valid or not ma89_valid) else (ma21 < ma89)
            price_val = row.price
            price_valid = not (pd.isna(price_val) if isinstance(price_val, float) else False)
            self.ma_bench[(ind_code, date_str)] = {
                "MA5": round(ma5, 2) if ma5_valid else None,
                "MA21": round(ma21, 2) if ma21_valid else None,
                "MA89": round(ma89, 2) if ma89_valid else None,
                "price": round(price_val, 2) if price_valid else None,
                "below": below,
            }
        
        n_below = sum(1 for v in self.ma_bench.values() if v["below"])
        n_above = sum(1 for v in self.ma_bench.values() if not v["below"])
        print(f"[均线] MA21/MA89已计算: {len(self.ma_bench)} 条记录, 下方{n_below} 上方{n_above}")
    
    def _get_ma_info(self, ind_code, date_str):
        """返回均线详情 dict: {MA5, MA21, MA89, price, below, gap_pct}
        
        gap_pct = (MA21-MA89)/MA89*100, None时gap=0
        """
        entry = self.ma_bench.get((ind_code, date_str))
        if entry is None:
            return {"MA5": None, "MA21": None, "MA89": None, "price": None, "below": True, "gap_pct": 0}
        ma5 = entry.get("MA5")
        ma21 = entry.get("MA21")
        ma89 = entry.get("MA89")
        if ma21 is not None and ma89 is not None and ma89 != 0:
            gap_pct = (ma21 - ma89) / ma89 * 100
        else:
            gap_pct = 0
        return {
            "MA5": ma5,
            "MA21": ma21,
            "MA89": ma89,
            "price": entry.get("price"),
            "below": entry.get("below", True),
            "gap_pct": gap_pct,
            "ma5_gap_pct": (ma21 - ma5) / ma21 * 100 if (ma5 is not None and ma21 is not None and ma21 != 0) else 0,
        }
    
    def _load_crowd_factor(self):
        """加载行业拥挤度因子，建立 {date_str: {ind_code: crowd_percentile}} 索引
        
        拥挤度：行业20日换手率均值 / 60日换手率均值
        crowd_percentile 越接近1.0 = 行业越拥挤（过热）
        """
        self.crowd_data = {}
        if not CROWD_FACTOR_PATH.exists():
            print("[拥挤度] 因子文件不存在，跳过")
            return
        
        try:
            # 行业成分股映射: stock_code → ind_code
            members = pd.read_parquet(INDUSTRY_DIR / "industry_members.parquet")
            stock_to_ind = dict(zip(members["stock_code"], members["ind_code"]))
            
            df = pd.read_parquet(CROWD_FACTOR_PATH)
            if df.empty:
                print("[拥挤度] 空文件，跳过")
                return
            
            # 映射股票到行业，按日期+行业聚合
            df["ind_code"] = df["code"].map(stock_to_ind)
            df = df.dropna(subset=["ind_code"])
            
            if df.empty:
                print("[拥挤度] 无有效行业映射，跳过")
                return
            
            # 按日期和行业求均值
            grouped = df.groupby(["date", "ind_code"])["value"].mean().reset_index()
            grouped["date_str"] = grouped["date"].dt.strftime("%Y-%m-%d")
            
            # 转成嵌套dict {date_str: {ind_code: percentile}}
            self.crowd_data = {}
            for _, row in grouped.iterrows():
                d = row["date_str"]
                if d not in self.crowd_data:
                    self.crowd_data[d] = {}
                self.crowd_data[d][row["ind_code"]] = round(row["value"], 4)
            
            dates_loaded = len(self.crowd_data)
            inds_loaded = len(set(
                ic for day in self.crowd_data.values() for ic in day.keys()
            ))
            print(f"[拥挤度] 已加载 {dates_loaded} 个交易日, {inds_loaded} 个行业")
        except Exception as e:
            print(f"[拥挤度] 加载失败: {e}")
            self.crowd_data = {}
    
    def _get_crowd_percentile(self, date_str, ind_code):
        """获取指定行业在指定日期的拥挤度百分位，取不到返回None"""
        day_data = self.crowd_data.get(date_str) if hasattr(self, 'crowd_data') else None
        if day_data is None:
            return None
        return day_data.get(ind_code)
    
    def _load_state(self):
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE) as f:
                    self.state = json.load(f)
                print(f"[状态] 已加载 {len(self.state)} 行业记录")
            except Exception as e:
                logger.warning("加载状态文件失败: %s", e)
                self.state = {}
        else:
            self.state = {}
    
    def save_state(self):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)
        print(f"[状态] 已保存 {len(self.state)} 行业记录 → {STATE_FILE}")
    
    # ── 正向扫描 ──
    
    def run_forward(self, start_date="2026-06-01", end_date=None):
        """从 start_date 正向扫描到 end_date（默认到今天）"""
        if self.daily_data is None:
            self.load_daily_data()
        
        if end_date is None:
            end_date = str(date.today())
        
        start_dt = pd.Timestamp(start_date)
        end_dt = pd.Timestamp(end_date)
        
        all_dates = sorted(self.daily_data["date"].unique())
        date_to_idx = {d: i for i, d in enumerate(all_dates)}
        process_dates = [d for d in all_dates if start_dt <= d <= end_dt]
        
        print(f"\n{'='*60}")
        print(f"  正向扫描: {start_date} → {end_date} ({len(process_dates)}个交易日)")
        print(f"{'='*60}")
        
        for day_idx, eval_date in enumerate(process_dates):
            self._process_day(eval_date, all_dates, date_to_idx)
            if (day_idx + 1) % 5 == 0 or day_idx == 0:
                n_track = sum(1 for s in self.state.values() if s.get("status") == "tracking")
                n_sleep = sum(1 for s in self.state.values() if s.get("status") == "sleeping")
                n_triggered = sum(1 for s in self.state.values() if s.get("status") == "triggered")
                n_elim = sum(1 for s in self.state.values() if s.get("status") == "eliminated")
                # 阶段统计
                n_early = sum(1 for s in self.state.values() if s.get("status") == "tracking" and s.get("phase", "early") == "early")
                n_growing = sum(1 for s in self.state.values() if s.get("status") == "tracking" and s.get("phase") == "growing")
                n_mature = sum(1 for s in self.state.values() if s.get("status") == "tracking" and s.get("phase") == "mature")
                n_alarming = sum(1 for s in self.state.values() if s.get("status") == "tracking" and s.get("phase") == "alarming")
                dt_str = str(eval_date.date())
                print(f"  [{dt_str}] d{day_idx+1}: 休眠{n_sleep} 反转信号{n_triggered} 跟踪{n_track}(早期{n_early}/成长{n_growing}/成熟{n_mature}/预警{n_alarming}) 已剔除{n_elim}")
        
        self.save_state()
        self._export_report()
        return self
    
    def _check_reversal_conditions(self, row, hist, code, eval_date, require_min_breadth=True, is_reentry=False):
        """检查是否满足反转条件（C1+C3），满足则创建跟踪条目并返回True，否则返回False

        C0(MA21<MA89)/C2(宽度跳升)/C4(放量确认)已于2026-08-02废弃。

        Args:
            row: 当日行情行数据
            hist: 历史数据DataFrame（已按日期排序）
            code: 行业代码
            eval_date: 评估日期
            require_min_breadth: 是否要求当前宽度>=RETIRE_BW（eliminated/sleeping均需）
            is_reentry: 是否为重新入场（eliminated路径），影响打印标签
        """
        if len(hist) < 20:
            return False

        date_str = str(eval_date.date())
        bw20 = hist.tail(20)["breadth"].mean()
        pct = row["avg_pct"] or 0

        # 宽度下限检查
        if require_min_breadth and row["breadth"] < RETIRE_BW:
            return False

        if not (bw20 < REVERSAL_BREADTH_MAX and pct > REVERSAL_PCT_MIN):
            return False

        # 满足反转条件 → 创建条目
        S0 = compute_S0(bw20 * 100, pct)
        crowd_pct = self._get_crowd_percentile(date_str, code)
        conf = max(0.1, 1.0 - crowd_pct) if crowd_pct is not None else 1.0

        self.state[code] = {
            "status": "triggered",
            "phase": "early",           # 生命周期阶段
            "signal_date": date_str,
            "S0": S0,
            "score": S0,
            "days": 0,
            "low_days": 0,
            "breadth_low_days": 0,       # 宽度连续低于15%的天数
            "last_date": date_str,
            "entry_confidence": round(conf, 2),
            "growth_count": 0,           # 进入成长期次数
            "entry_price": None,         # 成长期入场价（金叉确认日价格）
            "max_price": None,           # 成长期最高价
            "golden_cross_start": None,  # 金叉确认起始日
            "death_cross_start": None,   # 死叉确认起始日
            "alarm_bw13_low_days": 0,    # A1宽度衰减连续天数
            "alarm_crowd_hot_days": 0,   # A2拥挤过热连续天数
        }
        name = self.code_to_name.get(code, code)
        tag = "重新入场" if is_reentry else "反转信号"
        print(f"  ★ {name:12s} {tag} S₀={S0:2d} 置信={conf:.0%} | "
              f"C1={bw20*100:.0f}% C3=+{pct:.1f}%")
        return True
    
    def _process_day(self, eval_date, all_dates, date_to_idx):
        """处理一天的扫描——四阶段生命周期"""
        today_data = self.daily_data[self.daily_data["date"] == eval_date]
        if today_data.empty:
            return
        
        date_str = str(eval_date.date())
        
        for _, row in today_data.iterrows():
            code = row["con_code"]
            name = self.code_to_name.get(code, code)
            entry = self.state.get(code, {"status": "sleeping", "phase": "sleeping"})
            status = entry.get("status", "sleeping")
            
            hist = self.daily_data[
                (self.daily_data["con_code"] == code) &
                (self.daily_data["date"] < eval_date)
            ].sort_values("date")
            
            # ── 已剔除：检查是否重新触发反转 ──
            if status == "eliminated":
                if self._check_reversal_conditions(row, hist, code, eval_date, require_min_breadth=True, is_reentry=True):
                    # _check_reversal_conditions 已创建新条目，phase=early
                    pass
                continue

            # ── 休眠态：检查是否触发反转 ──
            if status == "sleeping":
                self._check_reversal_conditions(row, hist, code, eval_date, require_min_breadth=True)
                continue
            
            # ── 反转信号日(triggered)：第二天进入跟踪 ──
            if status == "triggered":
                trigger_dt = pd.Timestamp(entry["signal_date"])
                if eval_date > trigger_dt:
                    entry["status"] = "tracking"
                    entry["phase"] = "early"
                    entry["days"] = 1
                    entry["last_date"] = date_str
                    # 更新分数
                    self._update_score(entry, row, code, eval_date, all_dates, date_to_idx)
                continue
            
            # ── 跟踪态：根据phase执行不同逻辑 ──
            if status == "tracking":
                entry["days"] += 1
                entry["last_date"] = date_str

                phase = entry.get("phase", "early")
                ma_info = self._get_ma_info(code, date_str)
                current_price = ma_info.get("price")

                # 更新贝叶斯分数（保护期内也更新，仅豁免剔除）
                self._update_score(entry, row, code, eval_date, all_dates, date_to_idx)

                # ── 保护期：前 GRACE_DAYS 天豁免剔除 ──
                in_grace = entry["days"] <= GRACE_DAYS

                if not in_grace:
                    # ── 通用剔除检查（所有phase共用）──
                    # 宽度剔除：需连续 BREADTH_ELIMINATE_DAYS 天 < RETIRE_BW
                    if row["breadth"] < RETIRE_BW:
                        entry["breadth_low_days"] = entry.get("breadth_low_days", 0) + 1
                    else:
                        entry["breadth_low_days"] = 0

                    if entry.get("breadth_low_days", 0) >= BREADTH_ELIMINATE_DAYS:
                        entry["status"] = "eliminated"
                        entry["elim_reason"] = f"连续{BREADTH_ELIMINATE_DAYS}天宽度<15%"
                        print(f"  ✕ {name:12s} 剔除(宽<15%) phase={phase} 最终分={entry['score']:.0f} "
                              f"连续{entry['breadth_low_days']}天")
                        continue

                    # 超期剔除
                    if entry["days"] > MAX_TRACK_DAYS:
                        entry["status"] = "eliminated"
                        entry["elim_reason"] = f"跟踪超{MAX_TRACK_DAYS}天"
                        print(f"  ✕ {name:12s} 剔除(超期) phase={phase} 最终分={entry['score']:.0f}")
                        continue

                    # 低分剔除：需连续 ELIMINATE_DAYS 天 < ELIMINATE_SCORE
                    if entry["score"] < ELIMINATE_SCORE:
                        entry["low_days"] = entry.get("low_days", 0) + 1
                    else:
                        entry["low_days"] = 0

                    if entry.get("low_days", 0) >= ELIMINATE_DAYS:
                        entry["status"] = "eliminated"
                        entry["elim_reason"] = f"连续{ELIMINATE_DAYS}天<{ELIMINATE_SCORE}分"
                        print(f"  ✕ {name:12s} 剔除(低分) phase={phase} 最终分={entry['score']:.0f}")
                        continue

                # ── Phase-specific 逻辑 ──
                if phase == "early":
                    self._process_early_phase(entry, code, name, ma_info, current_price, date_str)
                elif phase == "growing":
                    self._process_growing_phase(entry, code, name, ma_info, current_price, date_str, eval_date, row, all_dates, date_to_idx)
                elif phase == "mature":
                    self._process_mature_phase(entry, code, name, ma_info, current_price, date_str, eval_date, row, all_dates, date_to_idx)
                elif phase == "alarming":
                    self._process_alarming_phase(entry, code, name, ma_info, current_price, date_str)
    
    def _update_score(self, entry, row, code, eval_date, all_dates, date_to_idx):
        """更新贝叶斯分数 Sₜ = clamp(Sₜ₋₁ × λₜ, 0, 100)"""
        yest = self._get_yesterday(code, eval_date, all_dates, date_to_idx)
        if yest is not None:
            date_str = str(eval_date.date())
            crowd_today = self._get_crowd_percentile(date_str, code)
            # w₄ 基线改为前5日均值（07-16: 日间噪声→短期趋势，与成交额5日均线对齐）
            idx = date_to_idx.get(eval_date, -1)
            crowd_vals = [crowd_today]  # 含当日，取中位数时更稳健
            for offset in range(1, 6):  # 回溯1-5天
                back_date = str(all_dates[idx - offset].date()) if idx - offset >= 0 else None
                if back_date:
                    cv = self._get_crowd_percentile(back_date, code)
                    if cv is not None:
                        crowd_vals.append(cv)
            crowd_vals.sort()
            crowd_baseline = crowd_vals[len(crowd_vals) // 2]  # 中位数，抗极端值
            lam = compute_lambda(row, yest, crowd_today, crowd_baseline)
            entry["score"] = min(max(entry["score"] * lam, 0), 100)
        # 更新Sₜ峰值
        if entry.get("score", 0) > entry.get("peak_score", 0):
            entry["peak_score"] = entry["score"]
    
    def _process_early_phase(self, entry, code, name, ma_info, current_price, date_str):
        """早期阶段转换逻辑"""
        below = ma_info.get("below", True)
        gap_pct = ma_info.get("gap_pct", 0)
        growth_count = entry.get("growth_count", 0)
        
        if not below and gap_pct > 0:  # MA21 > MA89，可能金叉
            if growth_count == 0:
                # 首次金叉: 3天+0.5%
                required_days = GOLDEN_CROSS_DAYS
                required_gap = GOLDEN_CROSS_GAP * 100  # 转为百分比
            else:
                # 二次金叉: 5天+1.0%
                required_days = GOLDEN_CROSS2_DAYS
                required_gap = GOLDEN_CROSS2_GAP * 100
            
            if gap_pct >= required_gap:
                gc_start = entry.get("golden_cross_start")
                if gc_start is None:
                    entry["golden_cross_start"] = date_str
                    entry["golden_cross_day"] = entry.get("days", 0)
                    return  # 第1天，等确认
                
                # 计算连续天数（用 tracking days 差）
                days_in_cross = entry.get("days", 0) - entry.get("golden_cross_day", entry.get("days", 0))
                
                if days_in_cross + 1 >= required_days:
                    # 金叉确认 → 进入成长期
                    entry["phase"] = "growing"
                    entry["growth_count"] = growth_count + 1
                    entry["entry_price"] = current_price
                    entry["max_price"] = current_price
                    entry["golden_cross_start"] = None
                    entry["golden_cross_day"] = None
                    tag = "🆕首次成长" if growth_count == 0 else "🔄二次成长"
                    if current_price:
                        print(f"  ↑ {name:12s} {tag} 入场价={current_price:.0f}")
                    else:
                        print(f"  ↑ {name:12s} {tag}")
            else:
                # 间距不够，重置确认
                entry["golden_cross_start"] = None
                entry["golden_cross_day"] = None
        else:
            # MA21 < MA89，不在金叉中
            entry["golden_cross_start"] = None
            entry["golden_cross_day"] = None
    
    def _process_growing_phase(self, entry, code, name, ma_info, current_price, date_str, eval_date, row, all_dates, date_to_idx):
        """成长期阶段转换逻辑（R<8%）
        
        成长期演化规则:
        1. R<8%: 两种方向——留在growing(MA21仍在MA89上方) / 退回early(死叉确认)
        2. R≥8%: 升级到成熟期(mature)，不可退回
        3. R=(P_max-P_entry)/P_entry, P_max只升不降
        """
        below = ma_info.get("below", True)
        R = 0  # 安全默认值
        
        # 更新最高价
        if current_price and entry.get("entry_price"):
            if entry.get("max_price") is None or current_price > entry["max_price"]:
                entry["max_price"] = current_price
            
            # 计算R
            if entry["entry_price"] and entry["entry_price"] != 0:
                R = (entry["max_price"] - entry["entry_price"]) / entry["entry_price"]
        
        # ── R≥8%: 升级到成熟期 ──
        if R >= R_THRESHOLD:
            entry["phase"] = "mature"
            # 重置保底预警计数器（成熟期重新开始计算）
            entry["fallback_cross_start"] = None
            entry["fallback_cross_day"] = None
            print(f"  ↑ {name:12s} 成长→成熟 R={R:.1%}")
            return
        
        # ── R<8%: 检查死叉(假成长退回) ──
        if below:
            dc_start = entry.get("death_cross_start")
            if dc_start is None:
                entry["death_cross_start"] = date_str
                entry["death_cross_day"] = entry.get("days", 0)
            else:
                days_in_dc = entry.get("days", 0) - entry.get("death_cross_day", entry.get("days", 0))
                if days_in_dc + 1 >= DEATH_CROSS_DAYS:
                    # 死叉确认 → 退回早期(无论第几次，自然过渡)
                    entry["phase"] = "early"
                    entry["golden_cross_start"] = None
                    entry["golden_cross_day"] = None
                    entry["death_cross_start"] = None
                    entry["death_cross_day"] = None
                    entry["entry_price"] = None
                    entry["max_price"] = None
                    entry["fallback_cross_start"] = None
                    entry["fallback_cross_day"] = None
                    print(f"  ↓ {name:12s} 假成长退回早期 R={R:.1%}")
        else:
            # MA21>MA89，不在死叉中
            entry["death_cross_start"] = None
            entry["death_cross_day"] = None
    
    def _process_mature_phase(self, entry, code, name, ma_info, current_price, date_str, eval_date, row, all_dates, date_to_idx):
        """成熟期阶段转换逻辑（R≥8%）
        
        成熟期演化规则:
        1. 标准预警条件(2-of-4)触发 → 进入预警期
        2. 保底条件(MA5下穿MA21, 2天+0.5%间距)触发 → 进入预警期
        3. 无预警信号 → 继续留在成熟期
        R≥8%不可逆，成熟期不会退回成长期
        """
        ma5 = ma_info.get("MA5")
        ma21 = ma_info.get("MA21")
        R = 0
        
        # 更新最高价
        if current_price and entry.get("entry_price"):
            if entry.get("max_price") is None or current_price > entry["max_price"]:
                entry["max_price"] = current_price
            if entry["entry_price"] and entry["entry_price"] != 0:
                R = (entry["max_price"] - entry["entry_price"]) / entry["entry_price"]
        
        # ── 检查标准预警条件（2-of-4）──
        hist = self.daily_data[
            (self.daily_data["con_code"] == code) &
            (self.daily_data["date"] < eval_date)
        ].sort_values("date").tail(ALARM_VOL_MA_DAYS + 5)
        alarm_signals = self._check_alarm_signals(entry, code, date_str, row, hist)
        if alarm_signals:
            entry["phase"] = "alarming"
            entry["alarm_trigger"] = "standard"
            entry["alarm_signals"] = alarm_signals
            print(f"  ⚠ {name:12s} 进入预警(标准) R={R:.1%} 信号={alarm_signals}")
            return
        
        # ── 检查保底条件: MA5下穿MA21 ──
        if ma5 is not None and ma21 is not None and ma5 < ma21:
            ma5_gap = ma_info.get("ma5_gap_pct", 0)
            if ma5_gap >= ALARM_FALLBACK_CROSS_GAP * 100:
                fb_start = entry.get("fallback_cross_start")
                if fb_start is None:
                    entry["fallback_cross_start"] = date_str
                    entry["fallback_cross_day"] = entry.get("days", 0)
                else:
                    days_in_fb = entry.get("days", 0) - entry.get("fallback_cross_day", entry.get("days", 0))
                    if days_in_fb + 1 >= ALARM_FALLBACK_CROSS_DAYS:
                        entry["phase"] = "alarming"
                        entry["alarm_trigger"] = "fallback(MA5<MA21)"
                        print(f"  ⚠ {name:12s} 进入预警(保底) R={R:.1%} MA5={ma5:.0f}<MA21={ma21:.0f} 间距={ma5_gap:.2f}%")
                        return
            else:
                # 间距不够，重置
                entry["fallback_cross_start"] = None
                entry["fallback_cross_day"] = None
        else:
            # MA5≥MA21，重置保底确认计数
            entry["fallback_cross_start"] = None
            entry["fallback_cross_day"] = None
        
        # 无预警信号 → 继续留在成熟期
    
    def _process_alarming_phase(self, entry, code, name, ma_info, current_price, date_str):
        """预警期阶段转换逻辑
        
        两条出路:
        1. 死叉确认(MA21下穿MA89持续3天) → 进入衰退
        2. 预警消退: 价格创新高 + 连续3日不跌破原P_max → 退回成熟期
        """
        below = ma_info.get("below", True)
        
        # ── 死叉确认 → 进入衰退 ──
        if below:
            dc_start = entry.get("death_cross_start")
            if dc_start is None:
                entry["death_cross_start"] = date_str
                entry["death_cross_day"] = entry.get("days", 0)
            else:
                days_in_dc = entry.get("days", 0) - entry.get("death_cross_day", entry.get("days", 0))
                if days_in_dc + 1 >= DEATH_CROSS_DAYS:
                    entry["status"] = "eliminated"
                    entry["phase"] = "decaying"
                    entry["elim_reason"] = "预警→衰退(死叉确认)"
                    print(f"  ✕ {name:12s} 预警→衰退")
                    return
        else:
            entry["death_cross_start"] = None
            entry["death_cross_day"] = None
        
        # ── 预警消退: 创新高 + 3日不跌破原P_max ──
        if current_price and entry.get("max_price"):
            # 预警期内继续追踪最高价(不冻结)
            if current_price > entry["max_price"]:
                # 创新高! 记录原P_max作为回踩支撑基准
                if entry.get("alarm_recovery_base") is None:
                    entry["alarm_recovery_base"] = entry["max_price"]
                entry["max_price"] = current_price
                # 创新高当天开始计数
                entry["alarm_recovery_days"] = 0
                print(f"  ↑ {name:12s} 预警中创新高 {current_price:.0f}(基线={entry['alarm_recovery_base']:.0f})")
            
            # 如果已创新高，检查是否连续N日不跌破原P_max
            if entry.get("alarm_recovery_base") is not None:
                base = entry["alarm_recovery_base"]
                if current_price >= base:
                    # 当日不跌破基线
                    entry["alarm_recovery_days"] = entry.get("alarm_recovery_days", 0) + 1
                    if entry["alarm_recovery_days"] >= ALARM_RECOVERY_DAYS:
                        # ✅ 确认退回成熟期（预警来自成熟期，R≥8%）
                        new_max = entry["max_price"]
                        entry["phase"] = "mature"
                        entry["alarm_trigger"] = None
                        entry["alarm_signals"] = None
                        entry["alarm_recovery_base"] = None
                        entry["alarm_recovery_days"] = 0
                        # 重置预警相关计数器
                        entry["alarm_bw13_low_days"] = 0
                        entry["alarm_crowd_hot_days"] = 0
                        entry["fallback_cross_start"] = None
                        entry["fallback_cross_day"] = None
                        print(f"  ↩ {name:12s} 预警消退→退回成熟 新高={new_max:.0f} 基线={base:.0f} 确认{ALARM_RECOVERY_DAYS}天")
                        return
                else:
                    # 跌破基线! 创新高失败，重置
                    entry["alarm_recovery_base"] = None
                    entry["alarm_recovery_days"] = 0
    
    def _check_alarm_signals(self, entry, code, date_str, row, hist):
        """检查标准预警信号（2-of-4触发机制）
        
        返回触发的信号名列表，长度>=ALARM_MIN_SIGNALS时触发预警。
        
        A1 宽度衰减: 当日宽度(MA13基准)连续3日 < 近5日平均宽度
        A2 拥挤过热: 拥挤度百分位连续3日 > 90%
        A3 大实体阴线: 等权均收盘价/等权均开盘价 - 1 < -5%
        A4 单日巨量: 当日成交额 / 近5日均量 > 1.5
        """
        signals = []
        
        # ── A1: 宽度衰减（MA13基准，连续3日<5日均宽）──
        if hist is not None and len(hist) >= 5 and "breadth_ma13" in hist.columns:
            bw13_avg5 = hist.tail(5)["breadth_ma13"].mean()
            today_bw13 = row.get("breadth_ma13")
            if today_bw13 is not None and not pd.isna(today_bw13) and bw13_avg5 > 0:
                if today_bw13 < bw13_avg5:
                    entry["alarm_bw13_low_days"] = entry.get("alarm_bw13_low_days", 0) + 1
                else:
                    entry["alarm_bw13_low_days"] = 0
                if entry.get("alarm_bw13_low_days", 0) >= ALARM_BW13_DECLINE_DAYS:
                    signals.append(f"宽度衰减(MA13×{entry['alarm_bw13_low_days']}日)")
            # 无数据时不重置计数器（保持之前状态）
        else:
            # 无历史数据，无法判断，不计数
            pass
        
        # ── A2: 拥挤过热（连续3日>90%）──
        crowd_pct = self._get_crowd_percentile(date_str, code)
        if crowd_pct is not None and crowd_pct > ALARM_CROWD_HOT_PCT:
            entry["alarm_crowd_hot_days"] = entry.get("alarm_crowd_hot_days", 0) + 1
            if entry["alarm_crowd_hot_days"] >= ALARM_CROWD_HOT_DAYS:
                signals.append(f"拥挤过热({crowd_pct:.0%}×{entry['alarm_crowd_hot_days']}日)")
        else:
            entry["alarm_crowd_hot_days"] = 0
        
        # ── A3: 大实体阴线（等权均收盘/等权均开盘-1 < -5%）──
        avg_open = row.get("avg_open")
        avg_close = row.get("avg_close")
        if (avg_open is not None and avg_close is not None
                and not pd.isna(avg_open) and not pd.isna(avg_close)
                and avg_open > 0):
            body_pct = (avg_close / avg_open - 1) * 100  # 百分比
            if body_pct < -ALARM_BIG_SHADOW_PCT * 100:
                signals.append(f"大阴线({body_pct:.1f}%)")
        
        # ── A4: 单日巨量（当日/近5日均量 > 1.5）──
        today_amt = row.get("total_amount")
        if today_amt and hist is not None and len(hist) >= ALARM_VOL_MA_DAYS and "total_amount" in hist.columns:
            avg_amt5 = hist.tail(ALARM_VOL_MA_DAYS)["total_amount"].mean()
            if avg_amt5 > 0 and today_amt / avg_amt5 > ALARM_HUGE_VOL_RATIO:
                signals.append(f"巨量({today_amt/avg_amt5:.1f}x)")
        
        # 2-of-4: 只有触发数>=阈值才返回，否则返回空列表
        if len(signals) >= ALARM_MIN_SIGNALS:
            return signals
        return []
    
    def _get_yesterday(self, code, eval_date, all_dates, date_to_idx):
        """获取前一个交易日的数据"""
        idx = date_to_idx.get(eval_date, -1)
        if idx <= 0:
            return None
        yest_date = all_dates[idx - 1]
        yest = self.daily_data[
            (self.daily_data["con_code"] == code) &
            (self.daily_data["date"] == yest_date)
        ]
        return yest.iloc[0] if len(yest) > 0 else None
    
    # ── 报告输出 ──
    
    def _export_report(self):
        """输出当前跟踪池报告"""
        rows = []
        for code, entry in self.state.items():
            name = self.code_to_name.get(code, code)
            conf = entry.get("entry_confidence")
            rows.append({
                "code": code,
                "name": name,
                "status": entry.get("status", "?"),
                "phase": entry.get("phase", "early"),
                "signal_date": entry.get("signal_date", ""),
                "S0": entry.get("S0", 0),
                "score": round(entry.get("score", 0), 1),
                "entry_confidence": conf if conf is not None else "",
                "days": entry.get("days", 0),
                "last_date": entry.get("last_date", ""),
                "elim_reason": entry.get("elim_reason", ""),
            })
        df = pd.DataFrame(rows)
        df.to_csv(REPORT_FILE, index=False)
        print(f"\n[报告] 已输出 → {REPORT_FILE}")
        
        # 打印跟踪池（按phase分组：early → growing → mature → alarming）
        tracking = [r for r in rows if r["status"] == "tracking"]
        phase_order = {"early": 0, "growing": 1, "mature": 2, "alarming": 3}
        tracking.sort(key=lambda x: (phase_order.get(x.get("phase", "early"), 9),
                                     -(x.get("entry_confidence", 0) or 0),
                                     -x["score"]))
        print(f"\n{'='*70}")
        print(f"  当前跟踪池 ({len(tracking)} 个)")
        print(f"{'='*70}")
        print(f"  {'#':3} {'行业':14s} {'阶段':8s} {'信号日':12s} {'S₀':4} {'Sₜ':6} {'置信':6} {'天数':5}")
        print(f"  {'─'*61}")
        for i, r in enumerate(tracking):
            conf = r.get("entry_confidence", "") or ""
            phase = r.get("phase", "early")
            print(f"  {i+1:2d} {r['name']:14s} {phase:8s} {r['signal_date']:12s} {r['S0']:3d}  {r['score']:>5.1f}  {str(conf):>5s}  {r['days']:3d}")
        
        return df
    
    # ── 查询 ──
    
    def get_tracking_pool(self, min_score=50):
        """获取可同步到富途的跟踪列表"""
        tracking = []
        for code, entry in self.state.items():
            status = entry.get("status", "")
            if status in ("tracking", "triggered") and entry.get("score", 0) >= min_score:
                conf = entry.get("entry_confidence")
                tracking.append({
                    "ind_code": code,
                    "ind_name": self.code_to_name.get(code, code),
                    "signal_date": entry.get("signal_date", ""),
                    "days_since": entry.get("days", 0),
                    "score": round(entry.get("score", 0), 1),
                    "S0": entry.get("S0", 0),
                    "entry_confidence": conf if conf is not None else 1.0,
                    "phase": entry.get("phase", "early"),
                })
        # 按phase分组(early→growing→mature→alarming)，再按置信度降序
        phase_order = {"early": 0, "growing": 1, "mature": 2, "alarming": 3}
        tracking.sort(key=lambda x: (phase_order.get(x.get("phase", "early"), 9),
                                     x["entry_confidence"], x["score"]), reverse=False)
        # 在同phase内按置信度+分数降序
        tracking.sort(key=lambda x: (phase_order.get(x.get("phase", "early"), 9),
                                     -x["entry_confidence"], -x["score"]))
        return tracking


# ── 主入口 ───────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-06-01", help="回测起点")
    parser.add_argument("--end", default=None, help="回测终点")
    parser.add_argument("--list", action="store_true", help="仅列出当前状态")
    args = parser.parse_args()
    
    t = BayesianTracker()
    
    if args.list:
        t.load_daily_data()
        t._export_report()
    else:
        t.load_daily_data().run_forward(start_date=args.start, end_date=args.end)
        pool = t.get_tracking_pool(min_score=50)
        print(f"\n{'='*60}")
        print(f"  富途同步候选 (Sₜ ≥ 50): {len(pool)} 个")
        print(f"{'='*60}")
        print(f"  {'#':3} {'行业':14s} {'S₀':4} {'Sₜ':6} {'置信':6} {'信号日':12s} {'天数':5}")
        print(f"  {'─'*50}")
        for i, r in enumerate(pool):
            print(f"  {i+1:2d} {r['ind_name']:14s} {r['S0']:3d}  {r['score']:>5.1f}  {r['entry_confidence']:>5.2f}  {r['signal_date']:12s} d{r['days_since']}")
