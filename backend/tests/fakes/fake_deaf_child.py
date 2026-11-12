#!/usr/bin/env python3
"""替身内核：完全不读 stdin（只写一行，睡一会儿就退出）。

用来固定「内核不读 stdin 时，执行层不能把整轮运行挂在 ``drain()`` 上」这条边界（铁律 10）。
"""
import json
import sys
import time

print(json.dumps({"type": "message", "delta": "start"}), flush=True)
time.sleep(3)
