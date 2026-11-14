#!/usr/bin/env python3
"""「自动起标题」的替身智能体：不管收到什么提示词，都只回一个固定标题。

真内核（Claude Code 等）会把标题写在正文里；这里用同一套 JSONL 输出协议模拟，
让「起标题」这条链路（执行 → 收集正文 → 净化 → 写回）可以端到端被测到。
"""
import json
import sys

sys.stdin.read()

# 故意带上包裹与标点：验证净化会把「」与句号去掉、只留标题本身。
print(json.dumps({"type": "session", "session_id": "title-session"}), flush=True)
for piece in ["「修 ", "Python 的 ", "bug」"]:
    print(json.dumps({"type": "message", "delta": piece}), flush=True)
print(json.dumps({"type": "result", "usage": {"input_tokens": 7, "output_tokens": 4}}), flush=True)
