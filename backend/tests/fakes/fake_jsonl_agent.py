#!/usr/bin/env python3
"""真实 CLI 智能体的替身：读取提示词，并按 JSONL 逐行输出到标准输出。"""
import json
import sys
import time

prompt = sys.stdin.read().strip()
session_id = sys.argv[1] if len(sys.argv) > 1 else "fake-session-1"

def emit(payload):
    print(json.dumps(payload), flush=True)
    time.sleep(0.005)

emit({"type": "session", "session_id": session_id})
# 正文 → 工具调用 → 工具结果 → 再正文：用于验证落库后的 parts 仍保持真实先后顺序。
for piece in ["Echo: ", prompt[:40], "..."]:
    emit({"type": "message", "delta": piece})
emit({"type": "tool_call", "tool_call": {"id": "t1", "name": "run_shell", "args": {"command": "ls"}}})
emit({"type": "tool_result", "id": "t1", "output": "a.txt"})
emit({"type": "message", "delta": " done"})
emit({"type": "result", "usage": {"input_tokens": 11, "output_tokens": 22}})
