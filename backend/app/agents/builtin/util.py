"""内置适配器共用的小工具函数。"""
from __future__ import annotations

import json
from typing import Any


def truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


def as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part for part in value.replace(",", " ").split() if part]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v not in (None, "")]
    return [str(value)]


def flatten_content(content: Any, limit: int = 4000) -> str:
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get("text") or json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        text = "\n".join(parts)
    elif content is None:
        text = ""
    else:
        text = json.dumps(content, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + f"\n… (+{len(text) - limit} chars)"


def dig(payload: Any, *path: str, default: Any = None) -> Any:
    cursor = payload
    for key in path:
        if not isinstance(cursor, dict) or key not in cursor:
            return default
        cursor = cursor[key]
    return cursor


def npm_prepare_argv(*packages: str) -> list[str]:
    """构造「清掉 npm 上一次被中断留下的中转目录」的清扫命令。

    npm 在卸载 / 覆盖安装前，会把旧目录 rename 成一个隐藏中转目录
    （``.<包名>-<绝对路径的 sha1 前 8 位>``）再删除。这一步若被打断（超时、服务重启），
    残留目录会让之后**每一次**安装 / 更新 / 卸载都永久失败在 ``ENOTEMPTY``：
    中转名只由绝对路径决定，每次都一样，rename 永远撞上那个非空目录。

    这里按包名先把残留清掉 —— 「用哪个包管理器、残留叫什么名字」是适配器的知识，
    所以这份声明写在适配器层（铁律 1），框架只负责照常执行 ``prepare_argv``。
    """
    targets: list[str] = []
    for package in packages:
        # 作用域包（@scope/name）的残留目录在 <root>/<scope>/ 下，普通包在 <root>/ 下
        scope, _, name = package.rpartition("/")
        parent = f'"$root"/{scope}/' if scope else '"$root"/'
        targets.append(f"{parent}.{name}-*")
    return ["sh", "-c", f'root="$(npm root -g)"; rm -rf {" ".join(targets)}']


def npm_install_argv(package: str, *, bin_name: str, latest: bool = False) -> list[str]:
    """构造「npm 全局安装 / 更新」的命令，顺带处理本机既有的 bin 占位。

    本机的 ``<npm 前缀>/bin/<bin_name>`` 可能已经被别的安装占着（例如系统里另一个
    node 装的同一内核，或用户手写的垫片）。npm 只肯覆盖**指向自己安装目录**的链接，
    否则整个动作失败在 ``EEXIST: file already exists``（连包都不装）。

    这里先看那个占位是不是我们的（解析后落在 npm 全局目录里）：不是就挪到一个隐藏备份名
    让出位置；万一 npm 自己失败，再把备份放回去 —— 不留「命令没了、包也没装上」的半截状态。
    备份留在原地，用户随时可以自己挪回去。占位本来就是我们的链接时为无操作。
    """
    spec = f"{package}@latest" if latest else package
    script = f'''bin="$(npm prefix -g)/bin"
link="$bin/{bin_name}"
bak="$bin/.{bin_name}.arkswarm-bak"
root="$(npm root -g)"
moved=0
if [ -e "$link" ] || [ -L "$link" ]; then
  case "$(readlink -f "$link" 2>/dev/null)" in
    "$root"/*) ;;
    *)
      if mv -f "$link" "$bak"; then
        moved=1
        echo "已把本机既有的 $link 挪到 $bak：它不属于平台的这份安装，npm 不肯直接覆盖"
      fi
      ;;
  esac
fi
npm install -g {spec}
code=$?
if [ "$code" -ne 0 ] && [ "$moved" -eq 1 ]; then
  mv -f "$bak" "$link" && echo "安装未成功，已把 $link 放回原位"
fi
exit "$code"
'''
    return ["sh", "-c", script]


def workspace_only(ctx: Any) -> bool:
    """智能体是否选了「文件访问范围 = 仅工作目录」。

    ``file_access_scope`` 是多个内核共用的语义（Claude Code / Codex / Gemini CLI 各自
    落到自己的机制），读取方式只有这一份，避免各适配器自己写默认值时对不齐。
    """
    return (ctx.option("file_access_scope") or "unrestricted") == "workspace_only"
