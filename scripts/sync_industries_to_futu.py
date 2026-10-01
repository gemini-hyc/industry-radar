#!/usr/bin/env python3
"""
每晚自动同步跟踪中的行业到富途"行业"分组

规则：
  直接读取模块二产出的 tracker_state.json，筛选 Sₜ ≥ 50 的跟踪行业，
  不再重复运行贝叶斯引擎。
"""
import sys
import time
import json
from pathlib import Path
from datetime import datetime, date

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.paths import FUTU_HOST, FUTU_PORT, INDUSTRY_DIR

GROUP_NAME = "行业"
MAX_ITEMS  = 25
STORY_SCORE_MIN = 50

TRACKER_STATE_PATH = INDUSTRY_DIR / "tracker_state.json"
INDUSTRY_LIST_PATH = INDUSTRY_DIR / "industry_list.parquet"


def load_tracking_industries() -> list[dict]:
    """从 tracker_state.json 读取模块二的贝叶斯跟踪结果，筛选 Sₜ ≥ 50 的行业"""
    if not TRACKER_STATE_PATH.exists():
        print(f"❌ tracker_state.json 不存在: {TRACKER_STATE_PATH}")
        sys.exit(1)

    with open(TRACKER_STATE_PATH) as f:
        state = json.load(f)

    # 行业名映射: ts_code → name
    import pandas as pd
    ind_list = pd.read_parquet(INDUSTRY_LIST_PATH)
    name_map = dict(zip(ind_list["ts_code"].str.upper(), ind_list["name"]))

    items = []
    for ts_code, info in state.items():
        if info.get("status") != "tracking":
            continue
        score = info.get("score", 0)
        if score < STORY_SCORE_MIN:
            continue

        ind_name = name_map.get(ts_code.upper(), "")
        if not ind_name:
            print(f"  ⚠️ 找不到行业名: {ts_code}")
            continue

        items.append({
            "ind_code": ts_code.upper(),
            "ind_name": ind_name,
            "signal_date": info.get("signal_date", ""),
            "days_since": info.get("days", 0),
            "story_score": score,
            "S0": info.get("S0", 0),
            "entry_confidence": info.get("entry_confidence", 1.0),
            "phase": info.get("phase", ""),
            "status": "跟踪",
        })

    # 按 Sₜ 降序排列，取前 MAX_ITEMS
    items.sort(key=lambda x: -(x["story_score"] or 0))
    items = items[:MAX_ITEMS]

    print(f"  tracker_state.json: 跟踪池 → Sₜ≥{STORY_SCORE_MIN}: {len(items)}个")
    return items


def connect_futu():
    from futu import OpenQuoteContext, RET_OK
    ctx = OpenQuoteContext(host=FUTU_HOST, port=FUTU_PORT)
    ret, data = ctx.get_global_state()
    if ret != RET_OK:
        ctx.close()
        raise ConnectionError(f"连接失败: {data}")
    print(f"✅ 已连接富途")
    return ctx


def sync_to_futu(ctx, items: list[dict]):
    from futu import ModifyUserSecurityOp, RET_OK

    # 获取当前"行业"分组内容
    print(f"\n📋 获取当前「{GROUP_NAME}」分组...")
    ret, secs = ctx.get_user_security(GROUP_NAME)
    if ret != RET_OK:
        print(f"❌ 获取失败: {secs}")
        return False
    print(f"  当前: {len(secs)} 项")

    # 删除旧的PLATE类型项
    old_plates = []
    for _, row in secs.iterrows():
        code = row.get("code", "")
        stype = row.get("stock_type", "")
        if stype == "PLATE" or code.upper().startswith("SH.LIST"):
            old_plates.append(code)
    if old_plates:
        print(f"🗑️ 清除旧数据 {len(old_plates)} 项...")
        ret, msg = ctx.modify_user_security(GROUP_NAME, ModifyUserSecurityOp.DEL, old_plates)
        if ret != RET_OK:
            print(f"❌ 清除失败: {msg}")
            return False
        time.sleep(1)

    # 新行业代码（已从 tracker_state 的 key 直接获取，无需二次映射）
    new_codes = [it["ind_code"] for it in items]

    print(f"➕ 添加 {len(new_codes)} 个跟踪行业到「{GROUP_NAME}」...")
    for item in items:
        print(f"  {item['ind_code']:15s} {item['ind_name']:12s} S₀={item['S0']:2d} Sₜ={item['story_score']:>5.1f} 置信={item['entry_confidence']:.2f} 阶段={item['phase']} 距信号{item['days_since']}天")

    # 分批
    for i in range(0, len(new_codes), 10):
        batch = new_codes[i:i+10]
        ret, msg = ctx.modify_user_security(GROUP_NAME, ModifyUserSecurityOp.ADD, batch)
        if ret != RET_OK:
            print(f"❌ 添加失败: {msg}")
            return False
        print(f"  ✅ 批次 {i//10+1} 添加成功 ({len(batch)}个)")
        time.sleep(0.5)

    # 验证
    ret, secs_new = ctx.get_user_security(GROUP_NAME)
    if ret == RET_OK:
        print(f"\n✅ 同步完成！「{GROUP_NAME}」共 {len(secs_new)} 项（含非行业项）")
    return True


def main():
    print("=" * 50)
    print(f"🏭 行业反转同步 → 富途「{GROUP_NAME}」")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  数据源: tracker_state.json（模块二产出）")
    print("=" * 50)

    items = load_tracking_industries()
    if not items:
        print("❌ 无跟踪行业")
        sys.exit(1)

    print(f"\n跟踪中行业: {len(items)} 个")
    for i, it in enumerate(items[:5]):
        print(f"  {i+1}. {it['ind_name']:12s} {it['status']} Sₜ={it['story_score']:.1f} 置信={it['entry_confidence']:.2f} 距信号{it['days_since']}天")

    try:
        ctx = connect_futu()
        try:
            ok = sync_to_futu(ctx, items)
            if not ok:
                sys.exit(1)
        finally:
            ctx.close()
    except Exception as e:
        print(f"❌ 同步失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
