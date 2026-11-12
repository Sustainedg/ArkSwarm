#!/usr/bin/env python3
"""替身双向内核：从 stdin 读 JSON 行，回一行确认，收到第二条请求后主动收工。

用来固定执行层的「保持 stdin 打开 + 代写适配器排队的请求」这条路径：
Codex app-server 就是这种常驻内核 —— 先 ``initialize``，拿到应答再发下一个请求。
"""
import json
import sys

count = 0
for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    count += 1
    print(json.dumps({"echo": raw, "seq": count}), flush=True)
    if count >= 2:
        print(json.dumps({"done": True}), flush=True)
        break
