"""Playbook 2: 客户交付件风险分析（source: Engagement Hub governance core）."""

from __future__ import annotations

from backend.playbooks.base import PlaybookSpec

_DELIVERY_INTAKE_RULES = (
    dict(
        dimension="完整性",
        name="需求范围明确",
        guidance="检查文档是否明确交付范围、目标和边界。范围清晰且边界明确为绿；范围有歧义或部分缺失为黄；无范围定义为红。",
        weight=2,
    ),
    dict(
        dimension="完整性",
        name="验收标准",
        guidance="检查是否定义可度量的验收标准。标准可度量且双方签字为绿；标准笼统为黄；无验收标准为红。",
        weight=2,
    ),
    dict(
        dimension="完整性",
        name="关键角色与联系人",
        guidance="检查客户方关键角色、决策人与联系人是否明确。角色齐全为绿；部分缺失为黄；完全缺失为红。",
    ),
    dict(
        dimension="完整性",
        name="时间与里程碑",
        guidance="检查项目时间表与里程碑。日期明确且合理为绿；仅有结束日期为黄；无时间定义为红。",
    ),
    dict(
        dimension="风险",
        name="范围蔓延风险",
        guidance="识别范围蔓延信号：模糊的“支持”“协助”措辞、无变更流程、开放式交付。变更控制完善为绿；有变更流程但宽松为黄；无变更控制且措辞开放为红。",
        weight=2,
    ),
    dict(
        dimension="风险",
        name="不切实际的承诺",
        guidance="识别不切实际的时间、成本或质量承诺（如同时要求极快、极便宜、零风险）。承诺与资源匹配为绿；个别指标偏紧为黄；明显不可达为红。",
        weight=2,
    ),
    dict(
        dimension="风险",
        name="依赖与前置条件",
        guidance="检查客户方依赖与前置条件（数据、访问、审批）是否明确。全部列明为绿；部分列明为黄；未识别依赖为红。",
    ),
    dict(
        dimension="风险",
        name="单点依赖",
        guidance="识别对单一人员、系统或供应商的强依赖。有备份安排为绿；依赖明显但有缓解意向为黄；单点依赖且无缓解为红。",
    ),
    dict(
        dimension="商务",
        name="计费与结算",
        guidance="检查计费方式、费率、结算周期与发票要求。全部明确为绿；部分明确为黄；无计费定义或与公司标准冲突为红。",
    ),
    dict(
        dimension="商务",
        name="责任与赔偿",
        guidance="检查责任划分与赔偿条款。责任对等且上限合理为绿；责任偏客户方或我方为黄；无限责任或无条款为红。",
    ),
    dict(
        dimension="合规",
        name="数据处理合规",
        guidance="检查是否涉及个人数据/敏感数据及其处理授权。不涉数据或授权完整为绿；涉数据但授权不全为黄；涉敏感数据且无授权为红。",
        weight=2,
    ),
    dict(
        dimension="合规",
        name="保密与知识产权",
        guidance="检查保密条款与交付成果 IP 归属。双向保密且 IP 明确为绿；单向保密为黄；无保密条款或 IP 归属不清为红。",
    ),
)

DELIVERY_INTAKE_PLAYBOOK = PlaybookSpec(
    id="delivery-intake",
    name="客户交付件风险分析",
    description="对客户提交的 SOW / 服务请求做完整性与风险体检，产出缺口清单、调研简报与交接文档。",
    friendly_id_prefix="DI",
    dimensions=("完整性", "风险", "商务", "合规"),
    rule_seeds=_DELIVERY_INTAKE_RULES,
    deliverables=("discovery_brief_docx", "handover_docx"),
    scoring_instructions=(
        "你是客户交付件风险分析代理。本剧本站在我方立场，我方＝交付方/服务提供方（文档中的乙方），"
        "客户为甲方；guidance 中的「我方」「客户方」一律按此对应，不得从文档行文推测立场。"
        "对每条资质标准：在客户提交文档中寻找对应内容，"
        "按规则 guidance 给出 red/amber/green 判定；判定必须引用文档原文片段；"
        "文档中找不到对应内容时判 red 并说明缺失（gap_reason），并标注该缺失应向客户追问什么。"
    ),
)
