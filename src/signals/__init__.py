"""
signals — 信号台账与验证底座（P0）

模块：
  ledger    台账结构、幂等写入、前向收益回填
  sources   信号来源适配器（冷区历史回填 + 跟踪池/主线每日快照）
  evaluate  事件研究评估与 markdown 报告
"""
