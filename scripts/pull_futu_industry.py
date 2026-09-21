#!/usr/bin/env python3
"""
行业板块数据拉取 — 富途OpenD版

通过 host.docker.internal:11111 连接宿主机上的富途OpenD网关，
拉取行业板块(Plate.INDUSTRY)列表和成分股，保存为 parquet 格式。

输出格式与概念板块兼容：
  industry_list.parquet    — ts_code, name, count
  industry_members.parquet — ind_code, ind_name, stock_code

⚠️ 富途API频率限制: 每30秒最多10次 get_plate_stock 调用
"""
import sys
import time
import pandas as pd
from pathlib import Path

FUTU_HOST = "host.docker.internal"
FUTU_PORT = 11111
OUTPUT_DIR = Path("/opt/data/quant-data/industry")

# 富途API限制: 30秒最多10次板块成分股查询
PLATE_MIN_INTERVAL = 3.0
BATCH_WINDOW = 30
BATCH_MAX_CALLS = 10


def connect():
    """连接富途OpenD"""
    from futu import OpenQuoteContext, RET_OK

    quote_ctx = OpenQuoteContext(host=FUTU_HOST, port=FUTU_PORT)
    ret, data = quote_ctx.get_global_state()
    if ret != RET_OK:
        raise ConnectionError(f"连接富途OpenD失败: {data}")

    print(f"✅ 已连接富途OpenD ({FUTU_HOST}:{FUTU_PORT})")
    return quote_ctx


def pull_industry_list(ctx) -> pd.DataFrame:
    """拉取行业板块列表（SH市场即可，与SZ重复）"""
    from futu import Plate, Market, RET_OK

    ret, data = ctx.get_plate_list(Market.SH, Plate.INDUSTRY)
    if ret != RET_OK or data.empty:
        raise RuntimeError("拉取行业板块列表失败")

    df = data.rename(columns={
        "code": "ts_code",
        "plate_name": "name",
    })[["ts_code", "name"]].copy()

    # 行业板块代码保持大写（get_plate_stock 需要大写格式如 SH.LIST0001）
    # 注意：概念拉取会转小写，因为概念数据要匹配 market 数据格式
    # 但行业板块是临时对比用，保持原格式

    print(f"  拉取到 {len(df)} 个行业板块")
    return df


def pull_plate_stocks(ctx, plate_code: str) -> tuple[int, pd.DataFrame]:
    """拉取单个板块的成分股，返回 (ret, data)"""
    from futu import RET_OK

    ret, data = ctx.get_plate_stock(plate_code)
    if ret != RET_OK or data.empty:
        return ret, pd.DataFrame()

    result = data[["code"]].rename(columns={"code": "stock_code"})
    result["stock_code"] = result["stock_code"].str.lower()
    return ret, result


def main():
    t0 = time.time()
    print("=" * 50)
    print("富途行业板块拉取")
    print("=" * 50)

    ctx = connect()
    try:
        # Step 1: 拉行业板块列表
        print("\n📋 拉取行业板块列表...")
        plate_list = pull_industry_list(ctx)
        if plate_list.empty:
            print("❌ 未拉取到行业板块")
            sys.exit(1)

        # Step 2: 拉成分股（严格遵守频率限制）
        print(f"\n📊 拉取成分股（{len(plate_list)}个行业，间隔{PLATE_MIN_INTERVAL}秒）...")
        all_members = []
        total = len(plate_list)

        call_count_in_window = 0
        window_start = time.time()
        rate_limited_count = 0
        retry_queue = []

        for i, (_, row) in enumerate(plate_list.iterrows()):
            code = row["ts_code"]
            name = row["name"]

            # 频率控制
            call_count_in_window += 1
            if call_count_in_window > BATCH_MAX_CALLS:
                elapsed = time.time() - window_start
                if elapsed < BATCH_WINDOW:
                    wait = BATCH_WINDOW - elapsed
                    print(f"  ⏳ 频率限制等待 {wait:.0f} 秒...")
                    time.sleep(wait)
                call_count_in_window = 1
                window_start = time.time()

            time.sleep(PLATE_MIN_INTERVAL)

            try:
                ret, stocks = pull_plate_stocks(ctx, code)
                if ret != 0:
                    if rate_limited_count < 3:
                        print(f"  ⚠️ [{i+1}/{total}] {name} → ret={ret}: {stocks}")
                    rate_limited_count += 1
                    retry_queue.append((i, row))
                    continue

                if not stocks.empty:
                    stocks["ind_code"] = code
                    stocks["ind_name"] = name
                    all_members.append(stocks)
                    print(f"  [{i+1}/{total}] {name} ({len(stocks)}只) ✓")
                else:
                    print(f"  [{i+1}/{total}] {name} (空) -")

            except Exception as e:
                print(f"  [{i+1}/{total}] {name} ⚠️ {e}")

        # 重试失败的请求（限3轮）
        MAX_RETRY_ROUNDS = 3
        retry_round = 0
        while retry_queue and retry_round < MAX_RETRY_ROUNDS:
            retry_round += 1
            print(f"\n🔄 第{retry_round}轮重试 {len(retry_queue)} 个行业...")
            current_retry = retry_queue
            retry_queue = []

            call_count_in_window = 0
            window_start = time.time()

            for i, row in current_retry:
                code = row["ts_code"]
                name = row["name"]

                call_count_in_window += 1
                if call_count_in_window > BATCH_MAX_CALLS:
                    elapsed = time.time() - window_start
                    if elapsed < BATCH_WINDOW:
                        wait = BATCH_WINDOW - elapsed
                        time.sleep(wait)
                    call_count_in_window = 1
                    window_start = time.time()

                time.sleep(PLATE_MIN_INTERVAL)

                try:
                    ret, stocks = pull_plate_stocks(ctx, code)
                    if ret != 0:
                        retry_queue.append((i, row))
                        continue

                    if not stocks.empty:
                        stocks["ind_code"] = code
                        stocks["ind_name"] = name
                        all_members.append(stocks)
                        print(f"  [{i+1}/{total}] {name} ({len(stocks)}只) ✓ (重试{retry_round})")
                    else:
                        print(f"  [{i+1}/{total}] {name} (空) - (重试{retry_round})")

                except Exception as e:
                    retry_queue.append((i, row))
                    print(f"  [{i+1}/{total}] {name} ⚠️ {e} (重试{retry_round})")

        if retry_queue:
            print(f"\n⚠️ 仍有 {len(retry_queue)} 个行业未能拉取（已跳过）")
        elif rate_limited_count > 0:
            print(f"\n✅ 全部重试成功")

        if not all_members:
            print("❌ 未拉取到任何成分股数据")
            sys.exit(1)

        member_df = pd.concat(all_members, ignore_index=True)
        print(f"\n✅ 成分股拉取完成: {len(member_df)} 条记录，{member_df['ind_code'].nunique()} 个行业")

        # Step 3: 构建 list 表
        ind_codes = member_df["ind_code"].unique()
        ind_names = member_df.groupby("ind_code")["ind_name"].first()
        stock_counts = member_df.groupby("ind_code")["stock_code"].nunique()

        list_df = pd.DataFrame({
            "ts_code": ind_codes,
            "name": ind_names.values,
            "count": stock_counts.values,
        }).reset_index(drop=True)

        # Step 4: 保存
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        list_df.to_parquet(OUTPUT_DIR / "industry_list.parquet", index=False)
        member_df.to_parquet(OUTPUT_DIR / "industry_members.parquet", index=False)

        print(f"\n✅ 已保存:")
        print(f"   industry_list.parquet    — {len(list_df)} 个行业")
        print(f"   industry_members.parquet — {len(member_df)} 条记录")

    finally:
        ctx.close()
        print("\n🔌 已断开富途OpenD")

    elapsed = time.time() - t0
    print(f"耗时: {elapsed:.0f} 秒 ({elapsed/60:.1f} 分钟)")


if __name__ == "__main__":
    main()
