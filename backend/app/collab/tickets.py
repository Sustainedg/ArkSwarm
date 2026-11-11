"""工单协议：解析角色的输出、校验语义、渲染下游交付载荷（纯函数）。

角色之间**不共享记忆**，唯一的交接物就是一个 ``work_order`` 代码块。因此这里的容错
设计是整条链路最关键的地方：

1. 严格 JSON。模型老实输出时走这条，最快也最可靠。
2. 正则清理后再解析：去掉不可见控制字符、把中文智能引号还原成 ``"``、修掉尾随逗号。
   这一步能救回大量「截断 / 转义 / 复制粘贴」造成的低级语法错误。
3. 扫描正文里平衡的 ``{...}`` 对象（含被多余文字包裹的情况），并在末尾被截断时
   逐段回退到最后一个可解析的 ``}``。

三级都失败就**明确报错**，既不猜也不静默（AGENTS.md 铁律 11）。
本模块只依赖标准库，可脱离数据库与 HTTP 单测。
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterator, Mapping, Sequence

from .schemas import VERDICT_CHANGES, VERDICT_PASS, VERDICTS, DeliveryMode, TicketAction

#: 工单块的围栏标记
FENCE = "work_order"

#: 允许的 JSON 对象扫描体积上限（避免畸形输出把解析拖死）
MAX_SCAN_CHARS = 200_000
#: 截断修复时最多回退尝试的 ``}`` 个数
MAX_REPAIR_TRIES = 200
#: ask_user 的可点选项数量：少于 2 条就不叫「选择」，多到用户看不完也没有意义
MIN_ASK_OPTIONS = 2
MAX_ASK_OPTIONS = 12

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SMART_QUOTES = {
    "\u201c": '"',  # “
    "\u201d": '"',  # ”
    "\uff02": '"',  # ＂
    "\u3002": "。",  # 全角句号（误当小数点的情况极少，这里只做等值替换，不改语义）
}
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
_FENCE_RE = re.compile(
    r"```[ \t]*" + FENCE + r"[ \t]*\r?\n?(.*?)(?:```|\Z)",
    re.DOTALL | re.IGNORECASE,
)
_COERCE = {
    "result_summary": "summary",
    "summary": "summary",
    "remark": "remark",
    "artifacts": "artifacts",
    "assignee": "assignee",
    "assigned_to": "assignee",
    "parent_ticket": "parent_ticket",
    "verdict": "verdict",
    "review_comment": "review_comment",
    "question": "question",
    "reason": "reason",
    "mode": "mode",
    "rework_to": "rework_to",
    "rework_target": "rework_to",
    "handoff_to": "rework_to",
    "options": "options",
    "choices": "options",
    "suggestions": "options",
    "ask_options": "options",
}


# --- 数据结构 ---------------------------------------------------------------


@dataclass(slots=True)
class TicketDraft:
    """工单字段。所有列表字段都容忍「模型只给了一个字符串」的情况。"""

    title: str = ""
    goal: str = ""
    requirements: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    deliverables: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_value(cls, value: Any) -> TicketDraft:
        if isinstance(value, str):
            return cls(title=value.strip())
        if not isinstance(value, dict):
            return cls()
        return cls(
            title=_text(value.get("title")),
            goal=_text(value.get("goal") or value.get("objective")),
            requirements=_lines(value.get("requirements")),
            acceptance=_lines(value.get("acceptance") or value.get("acceptance_criteria")),
            constraints=_lines(value.get("constraints")),
            deliverables=_lines(value.get("deliverables") or value.get("deliverable")),
        )


@dataclass(slots=True)
class WorkOrder:
    """解析并规范化后的一轮产出。"""

    action: TicketAction
    ticket: TicketDraft | None = None
    assignee: str | None = None
    parent_ticket: str | None = None
    verdict: str | None = None
    review_comment: str | None = None
    #: 仅 review 打回时有意义：打给哪个节点（有多条退路时必须显式给出）
    rework_to: str | None = None
    #: 仅 delegate 时有意义：handoff（转交）还是 split（拆子工单）
    mode: DeliveryMode = DeliveryMode.HANDOFF
    #: 交给下游看的**一句话摘要**（不是对话，是结论）
    summary: str | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    question: str | None = None
    #: 仅 ask_user 时有意义：给用户点选的选项（不是一段写在正文里的清单）
    options: list[AskOption] = field(default_factory=list)
    reason: str | None = None
    remark: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["action"] = self.action.value
        data["mode"] = self.mode.value
        return data


@dataclass(slots=True)
class ParseResult:
    ok: bool
    order: WorkOrder | None = None
    error: str | None = None
    #: 命中哪一级容错：fence / inline / None
    source: str | None = None
    #: 可继续但需要提醒用户的偏差（例如一轮里出现多个工单块，已取最后一个）
    warning: str | None = None


@dataclass(slots=True)
class AskOption:
    """``ask_user`` 的一条可点选项。

    ``freeform=True`` 表示这条不直接发送，而是把输入框交给用户自己补充
    （例如「我来指定目录」）。这样既能保证卡片始终可用，又不把开放式回答硬塞成单选题。
    """

    label: str
    value: str
    description: str | None = None
    freeform: bool = False

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"label": self.label, "value": self.value, "freeform": self.freeform}
        if self.description:
            data["description"] = self.description
        return data


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on", "是"}
    return False


def _strip_option_bullet(text: str) -> str:
    """去掉 ``1)``、``-``、``•`` 这类列表前缀，只留下选项正文。"""
    cleaned = re.sub(r"^\s*(?:[-+*•·]|\d+[.)、]|[（(]\d+[）)])\s*", "", text)
    return cleaned.strip()


def _ask_option(value: Any) -> AskOption | None:
    if isinstance(value, str):
        label = _strip_option_bullet(value)
        return AskOption(label=label, value=label) if label else None
    if isinstance(value, (int, float, bool)):
        label = _text(value)
        return AskOption(label=label, value=label) if label else None
    if not isinstance(value, dict):
        return None

    label = _text(
        value.get("label")
        or value.get("title")
        or value.get("text")
        or value.get("name")
        or value.get("option")
    )
    reply = _text(
        value.get("value")
        or value.get("reply")
        or value.get("answer")
        or value.get("content")
    )
    description = _text(
        value.get("description")
        or value.get("detail")
        or value.get("help")
        or value.get("hint")
    ) or None
    freeform = _truthy(
        value.get("freeform")
        if "freeform" in value
        else value.get("free_text")
        if "free_text" in value
        else value.get("allow_text")
    )
    if not label:
        label = reply
    if not reply and not freeform:
        reply = label
    if freeform and not label:
        label = "我来补充说明"
    if not label:
        return None
    return AskOption(label=label, value=reply, description=description, freeform=freeform)


def _ask_options(value: Any) -> list[AskOption]:
    """把模型常见的 ``["A","B"]`` / ``[{label,value}]`` / ``{"A":"说明"}`` 统一成选项列表。"""
    if value is None:
        return []
    if isinstance(value, dict):
        known = {"label", "title", "text", "name", "option", "value", "reply", "answer", "content"}
        if known & set(value):
            candidates: list[Any] = [value]
        else:
            candidates = [{"label": key, "value": _text(item)} for key, item in value.items()]
    elif isinstance(value, str):
        lines = [_strip_option_bullet(line) for line in value.splitlines() if line.strip()]
        candidates = lines or [value]
    elif isinstance(value, (list, tuple)):
        candidates = list(value)
    else:
        return []

    options: list[AskOption] = []
    for item in candidates:
        option = _ask_option(item)
        if option is not None:
            options.append(option)
    return options


def _options_from_question(question: str) -> list[AskOption]:
    """从模型写在问题正文里的编号/项目符号中，尽量恢复出可点选项。"""
    options: list[AskOption] = []
    for line in question.splitlines():
        raw = line.strip()
        if not raw:
            continue
        cleaned = _strip_option_bullet(raw)
        if cleaned and cleaned != raw:
            options.append(AskOption(label=cleaned, value=cleaned))
    if len(options) >= 2:
        return options[:MAX_ASK_OPTIONS]

    # 「暗色还是亮色？」这类二选一问题也能直接落成两张卡。
    sentence = question.strip().rstrip("。？?！!")
    parts = re.split(r"\s*(?:还是|或者)\s*", sentence, maxsplit=1)
    if len(parts) == 2:
        left, right = (_strip_option_bullet(part).strip() for part in parts)
        if left and right:
            return [AskOption(label=left, value=left), AskOption(label=right, value=right)]
    return []


def ensure_ask_options(order: WorkOrder) -> bool:
    """保证 ``ask_user`` 一定能渲染成可操作卡片。

    提示词要求模型给结构化 ``options``；但模型仍可能漏掉或只写在正文里。这里做一次
    运行期补救：优先从问题正文提取，再至少补「按推荐方案继续」和「我来补充说明」。
    返回是否改动了选项，调用方可据此决定是否给出提示。
    """
    if order.action is not TicketAction.ASK_USER:
        return False

    original = [option.to_dict() for option in order.options]
    options = list(order.options)[:MAX_ASK_OPTIONS]
    labels = {option.label.strip().lower() for option in options if option.label.strip()}
    has_direct = any(not option.freeform and option.value for option in options)
    # 模型已经给出完整选项时以它为准；只有漏给/只给自由填写时才从正文恢复。
    if not has_direct or len(options) < MIN_ASK_OPTIONS:
        for option in _options_from_question(order.question or ""):
            if option.label.lower() in labels:
                continue
            options.append(option)
            labels.add(option.label.lower())
            if len(options) >= MAX_ASK_OPTIONS:
                break
        has_direct = any(not option.freeform and option.value for option in options)

    has_freeform = any(option.freeform for option in options)
    if not has_direct:
        label = "按你的推荐方案继续"
        if label.lower() not in labels:
            options.append(
                AskOption(
                    label=label,
                    value=label,
                    description="让入口角色按它建议的方案继续推进。",
                )
            )
            labels.add(label.lower())
    if not has_freeform and len(options) < MIN_ASK_OPTIONS:
        options.append(AskOption(label="我来补充说明", value="", freeform=True))

    order.options = options[:MAX_ASK_OPTIONS]
    return [option.to_dict() for option in order.options] != original


# --- 文本工具 ---------------------------------------------------------------


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def _lines(value: Any) -> list[str]:
    """把各种形态规整成字符串列表：字符串按行拆，列表逐项取文本。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [line.strip() for line in value.splitlines() if line.strip()]
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            text = _text(item)
            if text:
                out.append(text)
        return out
    text = _text(value)
    return [text] if text else []


def sanitize_json_text(text: str) -> str:
    """第 2 级容错：清理不可见字符与常见的低级语法错误。"""
    cleaned = text.replace("\ufeff", "")
    cleaned = _CTRL.sub("", cleaned)
    for src, dst in _SMART_QUOTES.items():
        cleaned = cleaned.replace(src, dst)
    for _ in range(3):  # 连续尾随逗号要迭代几次才能清干净
        new = _TRAILING_COMMA.sub(r"\1", cleaned)
        if new == cleaned:
            break
        cleaned = new
    return cleaned.strip()


def _scan_objects(text: str) -> Iterator[str]:
    """第 3 级容错：扫描出所有平衡的 ``{...}``（先内层后外层）。"""
    if len(text) > MAX_SCAN_CHARS:
        text = text[:MAX_SCAN_CHARS]
    stack: list[int] = []
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            stack.append(index)
        elif char == "}" and stack:
            yield text[stack.pop() : index + 1]


def _close_unbalanced(text: str) -> str | None:
    """补齐未闭合的 ``{`` / ``[``（必要时先补上被截断的引号）；无法补救时返回 None。"""
    stack: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]" and stack:
            stack.pop()
    if not stack and not in_string:
        return None
    closers = "".join("}" if char == "{" else "]" for char in reversed(stack))
    if in_string:
        text += '"'
    return text + closers


def _repair_truncated(text: str) -> dict[str, Any] | None:
    """输出被截断时的兜底：先回退到最后一个完整的 ``}``，再尝试补齐未闭合的括号。"""
    start = text.find("{")
    if start < 0:
        return None
    body = text[start:]
    candidates: list[str] = []
    closes = [index for index, char in enumerate(body) if char == "}"]
    candidates.extend(body[: index + 1] for index in reversed(closes[-MAX_REPAIR_TRIES:]))
    closed = _close_unbalanced(body)
    if closed:
        candidates.append(closed)
        cut = body.rfind(",")
        if cut > 0:
            again = _close_unbalanced(body[:cut])
            if again:
                candidates.append(again)

    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "action" in obj:
            return obj
    return None


def _pick(obj: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(obj, dict):
        return None, "工单块必须是一个 JSON 对象"
    if "action" not in obj:
        return None, "工单块缺少 action 字段"
    return obj, None


def _extract(raw: str) -> tuple[dict[str, Any] | None, str | None]:
    """按三级容错从一段文本里取出工单对象。"""
    text = raw.strip()
    if not text:
        return None, "工单块是空的"

    cleaned = sanitize_json_text(text)
    last_error: str | None = None
    missing_action: str | None = None

    for level, candidate in (("严格", text), ("清理", cleaned)):
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError as exc:
            if level == "严格":
                last_error = f"工单块不是合法 JSON：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）"
            else:
                last_error = f"工单块不是合法 JSON，清理后仍无法解析：{exc.msg}"
            continue
        found, reason = _pick(obj)
        if found is not None:
            return found, None
        missing_action = reason

    for obj_text in _scan_objects(cleaned):
        try:
            obj = json.loads(obj_text)
        except json.JSONDecodeError:
            repaired = _repair_truncated(obj_text)
            if repaired is None:
                continue
            obj = repaired
        found, reason = _pick(obj)
        if found is not None:
            return found, None
        missing_action = missing_action or reason

    repaired = _repair_truncated(cleaned)
    if repaired is not None:
        return repaired, None

    return None, missing_action or last_error or "没有找到可解析的工单对象"


def _build_order(obj: dict[str, Any]) -> WorkOrder:
    raw_action = _text(obj.get("action")).lower()
    try:
        action = TicketAction(raw_action)
    except ValueError:
        allowed = "、".join(item.value for item in TicketAction)
        raise ValueError(f"不认识的 action：{raw_action or '（空）'}；允许的取值：{allowed}") from None

    fields = {_COERCE[key]: value for key, value in obj.items() if key in _COERCE}
    ticket_value = obj.get("ticket") or obj.get("tickets") or obj.get("work_order")
    options_value = next(
        (obj[key] for key in ("options", "choices", "suggestions", "ask_options") if key in obj),
        None,
    )

    verdict = _text(fields.get("verdict")).lower() or None
    if verdict and verdict not in VERDICTS:
        verdict = None

    artifacts: list[dict[str, Any]] = []
    for item in fields.get("artifacts") or []:
        if isinstance(item, dict):
            artifacts.append({k: v for k, v in item.items()})
        elif isinstance(item, str) and item.strip():
            artifacts.append({"kind": "file", "path": item.strip()})

    raw_mode = _text(fields.get("mode")).lower()
    try:
        mode = DeliveryMode(raw_mode) if raw_mode else DeliveryMode.HANDOFF
    except ValueError:
        allowed_modes = "、".join(item.value for item in DeliveryMode)
        raise ValueError(f"不认识的 mode：{raw_mode}；允许的取值：{allowed_modes}") from None

    draft = TicketDraft.from_value(ticket_value)
    return WorkOrder(
        action=action,
        ticket=draft if (ticket_value is not None) else None,
        assignee=_text(fields.get("assignee")) or None,
        parent_ticket=_text(fields.get("parent_ticket")) or None,
        verdict=verdict,
        review_comment=_text(fields.get("review_comment")) or None,
        rework_to=_text(fields.get("rework_to")) or None,
        mode=mode,
        summary=_text(fields.get("summary")) or None,
        artifacts=artifacts,
        question=_text(fields.get("question")) or None,
        options=_ask_options(options_value),
        reason=_text(fields.get("reason")) or None,
        remark=_text(fields.get("remark")) or None,
        raw=obj,
    )


# --- 对外接口 ---------------------------------------------------------------


def extract_body(text: str) -> str:
    """去掉工单块后剩下的正文（时间线卡片上显示给人看的部分）。"""
    return _FENCE_RE.sub("", text or "").strip()


def parse_work_order(text: str) -> ParseResult:
    """把一轮产出解析成 :class:`WorkOrder`。"""
    if not text or not text.strip():
        return ParseResult(False, None, "本轮没有任何输出，无法解析工单", None)

    fenced = [match.group(1) for match in _FENCE_RE.finditer(text)]
    found: list[tuple[str, dict[str, Any]]] = []
    reasons: list[str] = []
    for body in fenced:
        obj, reason = _extract(body)
        if obj is not None:
            found.append(("fence", obj))
        elif reason:
            reasons.append(reason)

    warning: str | None = None
    if len(found) > 1:
        # 工具型内核（Claude Code 等）常把「草稿」和「最终答复」写进同一轮：草稿在前、结论在后。
        # 取最后一个而不是把整轮判死，同时把偏差显式写进 warning，由引擎提示用户（铁律 11）。
        warning = f"检测到 {len(found)} 个 {FENCE} 代码块，已按最后一个执行（模型应当只输出一个）"

    if found:
        source, obj = found[-1]
    else:
        obj, reason = _extract(text)
        if obj is None:
            return ParseResult(False, None, reason or f"没有找到 {FENCE} 代码块", None)
        source = "inline"

    try:
        order = _build_order(obj)
    except ValueError as exc:
        return ParseResult(False, None, str(exc), source)
    return ParseResult(True, order, None, source, warning)


def resolve_order_targets(order: WorkOrder, aliases: Mapping[str, str]) -> None:
    """把岗位显示名归一成节点 key。

    模型经常把 ``assignee`` 写成「CTO」而不是图上的 ``dev``；提示词里虽然同时给了两者，
    但校验与派发都只认 key。这里只做**唯一匹配**的归一（同名节点不猜），
    避免为了一个显示名把整轮工单打回。
    """
    normalized: dict[str, str] = {}
    for alias, key in aliases.items():
        if alias and alias not in normalized:
            normalized[alias] = key
            normalized[alias.lower()] = key

    def resolve(value: str | None) -> str | None:
        if not value:
            return value
        text = value.strip()
        if not text:
            return value
        exact = normalized.get(text) or normalized.get(text.lower())
        if exact:
            return exact
        for alias, key in aliases.items():
            if not alias or alias == key:
                continue
            wrapped = {f"{alias}（{key}）", f"{alias}({key})", f"{key}（{alias}）", f"{key}({alias})"}
            if text in wrapped:
                return key
        return value

    assignee = resolve(order.assignee)
    rework_to = resolve(order.rework_to)
    if assignee != order.assignee:
        order.assignee = assignee
    if rework_to != order.rework_to:
        order.rework_to = rework_to


def validate_order(
    order: WorkOrder,
    *,
    allowed_actions: Sequence[TicketAction],
    allowed_assignees: Sequence[str],
    allowed_rework_targets: Sequence[str] = (),
    is_entry: bool = False,
) -> str | None:
    """语义校验：动作是否越权、派发对象是否在白名单内、必填字段是否齐全。"""
    actions = {item.value for item in allowed_actions}
    if order.action.value not in actions:
        return (
            f"本轮不允许的动作：{order.action.value}；"
            f"你只能做：{'、'.join(sorted(actions))}"
        )

    assignees = sorted(allowed_assignees)
    if order.action is TicketAction.DELEGATE:
        if not assignees:
            return "你没有 assign 出边，不能派发工单（请让用户在协作设置里连线）"
        if not order.assignee:
            return f"delegate 必须给出 assignee；你只能派给：{'、'.join(assignees)}"
        if order.assignee not in assignees:
            return f"不能把工单交给「{order.assignee}」；你只能派给：{'、'.join(assignees)}"
        if order.mode is DeliveryMode.SPLIT and (order.ticket is None or not order.ticket.title):
            return "mode=split 时必须带有 ticket，且 ticket.title 不能为空（子工单要有名字）"

    if order.action is TicketAction.REVIEW:
        if order.verdict is None:
            return f"review 必须给出 verdict；取值：{VERDICT_PASS} 或 {VERDICT_CHANGES}"
        if order.verdict == VERDICT_CHANGES:
            if not order.review_comment:
                return f"verdict={VERDICT_CHANGES} 时必须给出 review_comment（返工必须说明原因）"
            targets = sorted(allowed_rework_targets)
            if order.rework_to and order.rework_to not in targets:
                return (
                    f"不能把工单退给「{order.rework_to}」；你只能退给："
                    f"{'、'.join(targets) if targets else '（你没有 rework 出边）'}"
                )

    if order.action is TicketAction.ASK_USER:
        if not is_entry:
            return "只有入口节点能向用户提问（ask_user）"
        if not order.question:
            return "ask_user 必须给 question（说明要用户决定什么）"
        if len(order.options) < MIN_ASK_OPTIONS:
            return (
                f"ask_user 必须给至少 {MIN_ASK_OPTIONS} 条 options 供用户点选；"
                "不要把可选项只写在 question 正文里"
            )
        if len(order.options) > MAX_ASK_OPTIONS:
            return f"ask_user 的 options 最多 {MAX_ASK_OPTIONS} 条，请收敛到用户能快速比较的范围"
        if not any(not option.freeform and option.value for option in order.options):
            return "ask_user 的 options 至少要有一条可直接点选的答复（freeform 只用于让用户自己补充）"

    if order.action is TicketAction.DELEGATE and order.ticket is not None and not order.ticket.acceptance:
        # 不拦死，但这是最常见的返工根因，提示得更明确一些。
        return None

    return None


def render_protocol_guide(
    *,
    duty_label: str,
    actions: Sequence[TicketAction],
    assignees: Sequence[str],
    reviewer: str | None = None,
    rework_targets: Sequence[str] = (),
) -> str:
    """渲染写进 prompt 的输出协议说明。

    规则按顺序自动编号；示例按岗位挑一个**该岗位真会用到**的动作，
    示例本身用四重围栏包住，避免模型把示例当成真输出照抄。
    """
    action_list = "、".join(item.value for item in actions)
    assignee_text = "、".join(sorted(assignees)) if assignees else "（你没有派发对象）"
    rework_text = "、".join(sorted(rework_targets))

    # 「谁审我」只对提交方有意义；审查者本人不需要这句。
    reviewer_text = ""
    if TicketAction.SUBMIT in actions:
        reviewer_text = f"你提交后由「{reviewer}」审查。" if reviewer else "本协作没有安排审查者，提交即完成。"

    rules: list[str] = [
        "先写给人看的说明（可用 Markdown）。",
        f"最后必须附**一个**以 {FENCE} 标记的围栏代码块，内容是一个 JSON 对象；整轮只能出现一个，"
        "正文里不要粘贴草稿或示例。",
        f"你本轮的岗位是「{duty_label}」，允许的动作：{action_list}。",
        f"允许派发的对象：{assignee_text}。{reviewer_text}",
    ]
    if TicketAction.DELEGATE in actions:
        rules.append(
            "delegate 必须给 assignee（填括号里的节点 key，例如 dev）；mode 决定语义——"
            "handoff（转交：同一件活换人做，我继续等结果，ticket 可省略）；"
            "split（拆子工单：我是子工单的发起人，等它完成后我来汇总，必须给 ticket.title）。"
        )
    if TicketAction.REVIEW in actions:
        rules.append(
            f"review 必须给 verdict（{VERDICT_PASS} / {VERDICT_CHANGES}）；"
            f"{VERDICT_CHANGES} 时必须给 review_comment，逐条说明不符合哪一条验收标准。"
        )
        if rework_text:
            rules.append(
                f"打回（{VERDICT_CHANGES}）只能退给：{rework_text}；有多条退路时必须用 rework_to 指明目标。"
            )
    if TicketAction.SUBMIT in actions:
        rules.append("submit 要给 summary（一句话结论）与 artifacts（改动的文件路径清单）。")
    if TicketAction.ASK_USER in actions:
        rules.append(
            "需要用户拍板时用 ask_user：question 写清要决定什么；options 给 2-6 条可点选项，"
            "每条形如 {\"label\":\"选项标题\",\"value\":\"点选后回传给角色的完整答复\","
            "\"description\":\"可选的一句说明\"}；需要用户自由填写时再加一条 "
            "{\"label\":\"我来补充\",\"value\":\"\",\"freeform\":true}。不要把选项只写在正文里。"
        )

    examples = [_example_for(actions, assignees)]
    if TicketAction.ASK_USER in actions and examples[0].get("action") != TicketAction.ASK_USER.value:
        examples.append(_ask_example())
    lines = [f"{index}. {rule}" for index, rule in enumerate(rules, start=1)]
    return (
        "\n".join(lines)
        + "\n\n示例（仅示意格式，内容别照抄）：\n\n"
        + "\n\n".join(
            "````\n```"
            + FENCE
            + "\n"
            + json.dumps(example, ensure_ascii=False, indent=2)
            + "\n```\n````"
            for example in examples
        )
    )


def _ask_example() -> dict[str, Any]:
    return {
        "action": "ask_user",
        "question": "首期先做材料与人工台账，还是连报表导出一起做？",
        "options": [
            {
                "label": "先做最小闭环",
                "value": "先做材料与人工台账，报表导出放到下一阶段",
                "description": "最快拿到可用版本",
            },
            {
                "label": "一次做全首期范围",
                "value": "材料、人工、成本汇总、报表导出一起做",
            },
            {"label": "我来补充", "value": "", "freeform": True},
        ],
    }


def _example_for(actions: Sequence[TicketAction], assignees: Sequence[str]) -> dict[str, Any]:
    """挑一个该岗位真会用到的动作作为示例。"""
    if TicketAction.REVIEW in actions:
        return {
            "action": "review",
            "verdict": VERDICT_CHANGES,
            "review_comment": "第 2 条验收未满足：刷新后主题没有保持，见 src/theme.ts",
            "rework_to": "<退回给谁，仅在有多条退路时必需>",
        }
    if TicketAction.SUBMIT in actions:
        return {
            "action": "submit",
            "summary": "已完成主题开关与持久化，改动集中在 src/theme.ts",
            "artifacts": [{"kind": "file", "path": "src/theme.ts"}],
        }
    return {
        "action": "delegate",
        "mode": "split",
        "assignee": sorted(assignees)[0] if assignees else "<下游节点key>",
        "ticket": {
            "title": "…",
            "goal": "…",
            "requirements": ["…"],
            "acceptance": ["…（可判定的验收标准）"],
            "constraints": ["…"],
        },
    }
