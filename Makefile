# Makefile —— 本地开发辅助（**不参与生产链路**）
#
# 目的：把 A1（tools/dev_verify.sh 同源 A/B）与 A2（tests/ 回归基线）收成提交前的一条命令。
# 全部复用项目自有脚本，**零新增依赖**。
#
# ⚠️ 解释器：生产日更链跑的就是 /usr/bin/python3（见 scripts/run_daily_scheduled.sh），
#    所以本文件的默认值与它一致 —— 开发与生产同解释器，才能把「本机绿」当作
#    「生产也绿」的证据。从机曾借用 stock-doctor/.venv，2026-10-07 汇入源端时
#    该路径在本机不存在，裸跑 make check 会直接失败（见 requirements.txt A3 纠偏）。
#    换机器用 `make check PY=/path/to/python` 覆盖，或在候选列表里加一条。

PY ?= $(shell for c in "$$PY" /usr/bin/python3 python3; do \
             command -v "$$c" >/dev/null 2>&1 || continue; \
             "$$c" -c 'import pandas, pyarrow' >/dev/null 2>&1 && { echo "$$c"; break; }; \
         done)

.PHONY: help check syntax test selftest snapshot verify clean-verify

help:                       ## 显示本文件可用目标
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/'

check: syntax test          ## 【提交前跑这条】语法 + 回归基线

syntax:                     ## 语法检查（AST 解析，零副作用、不写 __pycache__）
	$(PY) tools/syntax_check.py

test:                       ## 回归基线（结构硬断言 + 数值快照软报告）
	$(PY) tests/run_tests.py

selftest:                   ## 敏感性自测（注入破坏，验证每条断言真能抓到）
	$(PY) tests/selftest.py

snapshot:                    ## 更新数值快照基线（**仅改产物口径后**才需要）
	$(PY) tests/snapshot.py --update

verify:                      ## 同源 A/B：make verify SCRIPT=scripts/xxx.py
	@test -n "$(SCRIPT)" || { echo "用法: make verify SCRIPT=scripts/xxx.py"; exit 2; }
	tools/dev_verify.sh $(SCRIPT)

clean-verify:                ## 清理同源 A/B 沙箱（4.5G，可随时重建）
	tools/dev_verify.sh --clean
