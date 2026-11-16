#!/usr/bin/env python3
"""替身内核：跑足一段指定时间再收工，用来验证单步限时的掐表。"""
import json
import sys
import time

seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
deadline = time.monotonic() + seconds
while time.monotonic() < deadline:
    time.sleep(0.2)
print(json.dumps({"type": "message", "delta": "done"}), flush=True)
print(json.dumps({"type": "result", "usage": {"output_tokens": 1}}), flush=True)
