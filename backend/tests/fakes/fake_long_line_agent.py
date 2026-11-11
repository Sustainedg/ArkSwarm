#!/usr/bin/env python3
"""超长单行输出的替身智能体：一行 JSONL 就超过 asyncio 默认的 64 KiB 行缓冲上限。

真实内核（如 Claude Code）会把整段代码或工具结果塞进一行 JSONL，执行层必须能整行读出来，
否则整轮运行会被 ``LimitOverrunError``（「Separator is found, but chunk is longer than limit」）打断。
"""
import json
import sys

sys.stdin.read()

#: 单行正文：足以越过 :class:`asyncio.StreamReader` 默认的 65536 字节上限。
huge = "A" * 200_000

print(json.dumps({"type": "message", "delta": huge}, ensure_ascii=False), flush=True)
print(json.dumps({"type": "message", "delta": "|尾巴"}, ensure_ascii=False), flush=True)
print(json.dumps({"type": "result", "usage": {"input_tokens": 3, "output_tokens": 7}}), flush=True)
