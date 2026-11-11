"""工单协议：三级容错解析、语义校验与交付载荷渲染。

这套用例是整条协作链路的保险丝 —— 解析器一旦开始「猜」，角色之间就会传错话。
"""
from __future__ import annotations

from app.collab.schemas import TicketAction
from app.collab.tickets import (
    ChatUpdate,
    FENCE,
    TicketDraft,
    escape_inner_quotes,
    extract_body,
    extract_chat_body,
    merge_chat_update,
    normalize_title,
    parse_chat_update,
    parse_work_order,
    render_protocol_guide,
    resolve_order_targets,
    sanitize_json_text,
    titles_look_duplicated,
    validate_order,
)

GOOD = """我理解了这个需求，先安排下去。

```work_order
{
  "action": "delegate",
  "assignee": "cto",
  "ticket": {
    "title": "登录页暗色主题",
    "goal": "不改后端就能切换主题",
    "requirements": ["新增主题开关", "持久化选择"],
    "acceptance": ["刷新后主题保持"],
    "constraints": ["只改 frontend/src/**"]
  }
}
```
"""


def test_parse_standard_fenced_block():
    result = parse_work_order(GOOD)
    assert result.ok and result.source == "fence"
    order = result.order
    assert order is not None
    assert order.action is TicketAction.DELEGATE
    assert order.assignee == "cto"
    assert order.ticket is not None
    assert order.ticket.title == "登录页暗色主题"
    assert order.ticket.requirements == ["新增主题开关", "持久化选择"]
    # 正文只保留给人看的部分，工单块不重复出现在卡片上
    assert extract_body(GOOD) == "我理解了这个需求，先安排下去。"


def test_parse_tolerates_trailing_comma_and_control_chars():
    text = '```work_order\n{"action":"submit",\x07"summary":"做完了","artifacts":["a.py",],}\n```'
    result = parse_work_order(text)
    assert result.ok
    assert result.order is not None
    assert result.order.summary == "做完了"
    # 字符串形态的 artifacts 会被规整成 {kind, path}
    assert result.order.artifacts == [{"kind": "file", "path": "a.py"}]


def test_parse_repairs_smart_quotes():
    text = '```work_order\n{“action”:“submit”,“summary”:“ok”}\n```'
    result = parse_work_order(text)
    assert result.ok and result.order is not None and result.order.summary == "ok"


def test_parse_recovers_from_truncated_output():
    # 输出被截断：围栏没闭合、JSON 少了右括号
    text = '```work_order\n{"action":"submit","summary":"做完了","artifacts":["a.py"]'
    result = parse_work_order(text)
    assert result.ok, result.error
    assert result.order is not None and result.order.summary == "做完了"

    # 截断发生在字符串中间时，按最后一个逗号回退
    text = '```work_order\n{"action":"submit","summary":"做完了一半'
    result = parse_work_order(text)
    assert result.ok, result.error
    assert result.order is not None and result.order.action is TicketAction.SUBMIT


def test_parse_falls_back_to_inline_json():
    text = '我想了想，就这样吧：{"action":"done","remark":"收工"} 以上。'
    result = parse_work_order(text)
    assert result.ok and result.source == "inline"
    assert result.order is not None and result.order.remark == "收工"


def test_parse_takes_the_last_block_when_a_round_has_several():
    """工具型内核会把草稿和结论写进同一轮：取最后一个，但必须留下可看见的提醒。"""
    text = f'```{FENCE}\n{{"action":"done"}}\n```\n补充一句：\n```{FENCE}\n{{"action":"submit","summary":"x"}}\n```'
    result = parse_work_order(text)
    assert result.ok and result.order is not None
    assert result.order.action is TicketAction.SUBMIT and result.order.summary == "x"
    assert "2 个" in (result.warning or "") and FENCE in (result.warning or "")
    # 没有偏差时不产生噪声
    assert parse_work_order(f'```{FENCE}\n{{"action":"done"}}\n```').warning is None


def test_parse_reports_missing_action_and_unknown_action():
    assert "action" in (parse_work_order(f'```{FENCE}\n{{"summary":"没有动作"}}\n```').error or "")
    error = parse_work_order(f'```{FENCE}\n{{"action":"coding"}}\n```').error or ""
    assert "coding" in error and "delegate" in error


def test_parse_reports_garbage_and_empty_output():
    assert not parse_work_order("我这就去办，稍等。").ok
    assert not parse_work_order("   ").ok
    # 缺字段时给出的是可读原因，而不是抛异常
    assert parse_work_order(f'```{FENCE}\n{{"action":\n```').error


def test_sanitize_keeps_json_semantics():
    assert sanitize_json_text('{"a": 1,}') == '{"a": 1}'
    assert sanitize_json_text('\ufeff{"a":\x00 1}') == '{"a": 1}'


def test_validate_order_enforces_contract_and_whitelist():
    order = parse_work_order(GOOD).order
    assert order is not None

    # 派发对象不在白名单里 → 明确指出允许谁
    error = validate_order(order, allowed_actions=[TicketAction.DELEGATE], allowed_assignees=["se"])
    assert error is not None and "se" in error

    assert validate_order(order, allowed_actions=[TicketAction.DELEGATE], allowed_assignees=["cto"]) is None

    # 越权动作
    error = validate_order(order, allowed_actions=[TicketAction.SUBMIT], allowed_assignees=["cto"])
    assert error is not None and "delegate" in error

    # 审查必须给结论；打回必须给意见
    review = parse_work_order(f'```{FENCE}\n{{"action":"review","verdict":"changes_requested"}}\n```').order
    assert review is not None
    error = validate_order(review, allowed_actions=[TicketAction.REVIEW], allowed_assignees=[])
    assert error is not None and "review_comment" in error

    pass_order = parse_work_order(f'```{FENCE}\n{{"action":"review","verdict":"pass"}}\n```').order
    assert pass_order is not None
    assert validate_order(pass_order, allowed_actions=[TicketAction.REVIEW], allowed_assignees=[]) is None

    # 非入口节点不能向用户提问
    ask = parse_work_order(f'```{FENCE}\n{{"action":"ask_user","question":"要暗色还是亮色？"}}\n```').order
    assert ask is not None
    assert validate_order(ask, allowed_actions=[TicketAction.ASK_USER], allowed_assignees=[], is_entry=False)

    # 入口节点提问必须给可点选项；只把选项写在正文里不算
    error = validate_order(ask, allowed_actions=[TicketAction.ASK_USER], allowed_assignees=[], is_entry=True)
    assert error is not None and "options" in error


def test_ask_user_parses_and_requires_clickable_options():
    text = f'''```{FENCE}
{{
  "action": "ask_user",
  "question": "首期要先做最小闭环，还是一次做全？",
  "choices": [
    "先做最小闭环",
    {{"label": "一次做全", "value": "首期范围一次做全", "description": "周期更长"}},
    {{"label": "我来补充", "value": "", "freeform": true}}
  ]
}}
```'''
    parsed = parse_work_order(text)
    assert parsed.ok, parsed.error
    assert parsed.order is not None
    options = parsed.order.options
    assert [option.label for option in options] == ["先做最小闭环", "一次做全", "我来补充"]
    assert options[0].value == "先做最小闭环"
    assert options[1].description == "周期更长"
    assert options[2].freeform is True and options[2].value == ""
    assert validate_order(
        parsed.order,
        allowed_actions=[TicketAction.ASK_USER],
        allowed_assignees=[],
        is_entry=True,
    ) is None

    # 少于两条可比较的选项 → 明确拒绝，逼模型把选择做成卡片而不是一段长问题
    single = parse_work_order(
        f'```{FENCE}\n{{"action":"ask_user","question":"怎么办","options":["按默认来"]}}\n```'
    ).order
    assert single is not None
    error = validate_order(
        single, allowed_actions=[TicketAction.ASK_USER], allowed_assignees=[], is_entry=True
    )
    assert error is not None and "至少" in error

    # 只有 freeform 也没有可点的答复
    freeform_only = parse_work_order(
        f'```{FENCE}\n{{"action":"ask_user","question":"怎么办","options":'
        '[{"label":"我来补充","value":"","freeform":true},'
        '{"label":"还是补充","value":"","freeform":true}]}\n```'
    ).order
    assert freeform_only is not None
    error = validate_order(
        freeform_only,
        allowed_actions=[TicketAction.ASK_USER],
        allowed_assignees=[],
        is_entry=True,
    )
    assert error is not None and "可直接点选" in error


def test_step_prompt_hides_ask_user_from_non_entry_nodes():
    """非入口节点的「允许的动作」里绝不能出现 ask_user。

    否则模型会照提示词做，触发「只有入口节点能向用户提问」的运行时错误。
    """
    from app.collab.prompt import TicketView, build_step_prompt
    from app.collab.schemas import Duty, NodeSpec, TicketState

    node = NodeSpec(
        key="dev",
        role_id="r-dev",
        duty=Duty.ENGINEER,
        output_contract=[TicketAction.DONE, TicketAction.ASK_USER],
    )
    ticket = TicketView(id="t1", title="做点事", brief={"goal": "目标"}, state=TicketState.OPEN.value)

    def prompt(*, is_entry: bool) -> str:
        return build_step_prompt(
            team_name="小组",
            workspace_path="/tmp/x",
            node=node,
            ticket=ticket,
            history=[],
            assignees=[],
            reviewer=None,
            is_entry=is_entry,
        )

    non_entry = prompt(is_entry=False)
    assert "ask_user" not in non_entry
    # 生效动作里没有 submit 时，本轮要求也不能催它去 submit
    assert "用 done 收尾" in non_entry and "submit 提交结果" not in non_entry
    assert "ask_user" in prompt(is_entry=True)


def test_validate_order_escalate_needs_reason_and_barred_at_the_top():
    """escalate 必须说清卡在哪；入口节点不许上报（上面没人了，应该 ask_user）。"""
    order = parse_work_order(f'```{FENCE}\n{{"action":"escalate"}}\n```').order
    assert order is not None
    error = validate_order(
        order, allowed_actions=[TicketAction.ESCALATE], allowed_assignees=[], is_entry=False
    )
    assert error is not None and "reason" in error

    with_reason = parse_work_order(
        f'```{FENCE}\n{{"action":"escalate","reason":"本环境禁止执行命令，测不了"}}\n```'
    ).order
    assert with_reason is not None
    assert (
        validate_order(
            with_reason,
            allowed_actions=[TicketAction.ESCALATE],
            allowed_assignees=[],
            is_entry=False,
        )
        is None
    )
    entry_error = validate_order(
        with_reason,
        allowed_actions=[TicketAction.ESCALATE],
        allowed_assignees=[],
        is_entry=True,
    )
    assert entry_error is not None and "ask_user" in entry_error


def test_step_prompt_for_escalated_ticket_bars_delegate():
    """上抛件：提示词不许再教它往下派；到了入口角色则要求用 ask_user 交给用户。

    真实事故：UI 卡在「环境禁止执行」后只能把失败写进 done，CTO 手里没有上报动作，
    只好把同一件事改写成「静态核对」又派给码农 —— 阻塞原地转了一圈。
    """
    from app.collab.prompt import TicketView, build_step_prompt
    from app.collab.schemas import Duty, NodeSpec, TicketState

    cto = NodeSpec(key="cto", role_id="r-cto", duty=Duty.COORDINATOR)
    ceo = NodeSpec(key="ceo", role_id="r-ceo", duty=Duty.COORDINATOR)
    ticket = TicketView(
        id="t1",
        title="跑运行时验收",
        brief={"goal": "启动服务并跑通验收"},
        state=TicketState.ESCALATED.value,
        escalation={
            "from": "se",
            "reason": "本环境禁止执行命令",
            "need": "需要用户决定是否授权",
            "hops": ["se", "cto"],
        },
    )

    def prompt(node: NodeSpec, *, is_entry: bool) -> str:
        return build_step_prompt(
            team_name="小组",
            workspace_path="/tmp/x",
            node=node,
            ticket=ticket,
            history=[],
            assignees=["se"],
            reviewer=None,
            is_entry=is_entry,
        )

    mid = prompt(cto, is_entry=False)
    actions_line = next(line for line in mid.splitlines() if "允许的动作" in line)
    assert "delegate" not in actions_line and "escalate" in actions_line
    assert "不要再往下派" in mid
    # 上报表：让上层看清是谁报的、卡在哪、需要什么
    assert "本环境禁止执行命令" in mid and "需要用户决定是否授权" in mid

    top = prompt(ceo, is_entry=True)
    assert "ask_user" in top
    # 已经到顶：入口角色的动作里不再有 escalate，只剩「转给用户」
    top_actions = next(line for line in top.splitlines() if "允许的动作" in line)
    assert "escalate" not in top_actions and "ask_user" in top_actions


def test_protocol_guide_states_the_fence_and_the_whitelist():
    guide = render_protocol_guide(
        duty_label="协调者",
        actions=[TicketAction.DELEGATE, TicketAction.ASK_USER],
        assignees=["cto"],
    )
    assert FENCE in guide
    assert "cto" in guide
    assert "delegate" in guide and "ask_user" in guide
    assert "options" in guide and "freeform" in guide
    # 示例用四重围栏包住，避免模型把示例当成真输出照抄
    assert "````" in guide


def test_display_names_are_normalized_to_node_keys():
    """模型把 assignee 写成「CTO」时，唯一显示名应归一到图上的 key。"""
    order = parse_work_order(
        f'```{FENCE}\n{{"action":"delegate","assignee":"CTO",'
        '"mode":"handoff","ticket":{"title":"继续"}}\n```'
    ).order
    assert order is not None
    resolve_order_targets(order, {"CTO": "dev", "开发": "dev-2"})
    assert order.assignee == "dev"
    assert validate_order(order, allowed_actions=[TicketAction.DELEGATE], allowed_assignees=["dev"]) is None

    # 带括号的展示形式也认；同名节点则要求模型写 key，不猜
    wrapped = parse_work_order(
        f'```{FENCE}\n{{"action":"review","verdict":"changes_requested",'
        '"review_comment":"再改一下","rework_to":"SE（se）"}}\n```'
    ).order
    assert wrapped is not None
    resolve_order_targets(wrapped, {"SE": "se"})
    assert wrapped.rework_to == "se"

    ambiguous = parse_work_order(
        f'```{FENCE}\n{{"action":"delegate","assignee":"CTO","mode":"handoff"}}\n```'
    ).order
    assert ambiguous is not None
    resolve_order_targets(ambiguous, {})  # 调用方对重名节点不提供映射
    assert ambiguous.assignee == "CTO"


# --- 近似重名（防「复读式拆解」） ------------------------------------------


def test_normalize_title_strips_noise():
    assert normalize_title("  列出文件清单，并回报。 ") == "列出文件清单并回报"
    assert normalize_title("Write Login API") == "writeloginapi"


def test_titles_look_duplicated_catches_tail_tweaks():
    """模型最常见的绕法：在标题尾巴上追加几个字——必须仍然算同一件事。"""
    assert titles_look_duplicated("列出文件清单", "列出文件清单并回报")
    assert titles_look_duplicated("写登录接口", "写登录接口")
    assert titles_look_duplicated(
        "递归列出工作路径下的完整文件清单并回报结果",
        "递归列出工作路径下的完整文件清单并原样回报",
    )


def test_titles_look_duplicated_keeps_distinct_tasks_apart():
    assert not titles_look_duplicated("实现导出功能", "修复登录崩溃")
    assert not titles_look_duplicated("第 1 层", "第 2 层")
    assert not titles_look_duplicated("登录页改造为暗色主题", "登录页改造为浅色主题")
    assert not titles_look_duplicated("", "写登录接口")


def test_protocol_guide_tells_done_to_write_a_remark():
    """done 的 remark 是工单对外的交付摘要，协议说明里必须讲清楚。"""
    guide = render_protocol_guide(
        duty_label="协调者", actions=(TicketAction.DONE, TicketAction.DELEGATE), assignees=["se"]
    )
    assert "交付摘要" in guide


# --- 字符串里的裸引号（线上真实故障） ----------------------------------------------

#: 模型在 JSON 字符串里直接写 ASCII 双引号：第一个引号会提前结束字符串，整块判死。
INNER_QUOTES_BLOCK = """```work_order
{
  "action": "delegate",
  "assignee": "se",
  "mode": "split",
  "ticket": {
    "title": "UI 子工单",
    "goal": "步骤要能被非技术人员照着做通，含"换电脑恢复"的具体步骤",
    "requirements": ["样式只用内联 SVG", "不得引用 CDN"]
  }
}
```"""


def test_inner_unescaped_quotes_are_repaired():
    """字符串里的裸引号要被自动补转义，而不是把整轮运行判成协议错误。"""
    result = parse_work_order(INNER_QUOTES_BLOCK)
    assert result.ok, result.error
    order = result.order
    assert order is not None and order.ticket is not None
    assert '含"换电脑恢复"的具体步骤' in order.ticket.goal
    assert order.ticket.requirements == ["样式只用内联 SVG", "不得引用 CDN"]


def test_escape_inner_quotes_leaves_valid_json_alone():
    text = '{"a": 1, "b": ["x", "y"], "c": {"d": "e\\"f"}, "g": "收尾"}'
    assert escape_inner_quotes(text) == text
    assert sanitize_json_text(text) == text


def test_unparseable_block_error_keeps_position_and_hint():
    """真的救不回来时，报错要带**出错位置**与可操作的原因，不能只说「Expecting value」。"""
    result = parse_work_order(f'```{FENCE}\n{{"action": delegate}}\n```')
    assert not result.ok
    assert "第 1 行" in (result.error or "")
    assert "双引号" in (result.error or "")


def test_ticket_draft_keeps_description_field():
    """模型自造的 description 字段不能被静默丢掉（否则下游只收到一个标题）。"""
    only_description = TicketDraft.from_value({"title": "UI 子工单", "description": "第一行\n第二行"})
    assert only_description.goal == "第一行\n第二行"
    assert only_description.requirements == []

    with_goal = TicketDraft.from_value({"title": "t", "goal": "目标", "description": "细节一\n细节二"})
    assert with_goal.goal == "目标"
    assert with_goal.requirements == ["细节一", "细节二"]


# --- 入口角色聊天里的需求细化块 ------------------------------------------------


def test_chat_update_is_optional_and_stripped_from_display_text():
    text = "进度正常，CTO 正在拆任务。"
    update, warning = parse_chat_update(text)
    assert update is None and warning is None
    assert extract_chat_body(text) == text


def test_chat_update_parses_additions_and_is_removed_from_body():
    text = (
        "好，我把这两条补进工单。\n"
        "```chat_update\n"
        '{"requirements_add": ["增加重置按钮"], "acceptance_add": ["重置后恢复默认主题"]}\n'
        "```\n"
    )
    update, warning = parse_chat_update(text)
    assert warning is None
    assert update is not None
    assert update.requirements_add == ["增加重置按钮"]
    assert update.acceptance_add == ["重置后恢复默认主题"]
    assert extract_chat_body(text) == "好，我把这两条补进工单。"


def test_chat_update_merge_is_additive_and_deduplicated():
    brief = {"goal": "暗色主题", "requirements": ["主题开关"], "acceptance": ["刷新后保持"]}
    merged, changed = merge_chat_update(
        brief,
        ChatUpdate(
            requirements_add=["主题开关", "增加重置按钮"],
            acceptance_add=["重置后恢复默认主题"],
        ),
    )
    assert merged["requirements"] == ["主题开关", "增加重置按钮"]
    assert merged["acceptance"] == ["刷新后保持", "重置后恢复默认主题"]
    assert changed == ["requirements", "acceptance"]
    # 原 brief 不被原地修改，纯函数语义
    assert brief["requirements"] == ["主题开关"]


def test_malformed_chat_update_warns_but_does_not_raise():
    update, warning = parse_chat_update("```chat_update\n{不是 JSON}\n```")
    assert update is None
    assert warning and "不是合法 JSON" in warning
