"""Playbook 1: 供应商合同合规（source: VendorGuard closed loop）."""

from __future__ import annotations

from backend.playbooks.base import PlaybookSpec

_CONTRACT_COMPLIANCE_RULES = (
    # —— 商业 ——
    dict(
        dimension="商业",
        name="付款账期",
        guidance="检查付款账期条款。账期不超过 60 天为绿；61–90 天为黄；超过 90 天或未约定账期为红。引用账期原文。",
        weight=2,
    ),
    dict(
        dimension="商业",
        name="定价清晰度",
        guidance="检查价格与费用条款是否明确定义（单价、总价、调价机制、货币）。全部明确为绿；有模糊调价机制为黄；缺少定价条款或允许单方调价为红。",
    ),
    dict(
        dimension="商业",
        name="交付与验收条款",
        guidance="检查交付时间、地点、验收标准与流程。明确且可执行为绿；验收标准模糊为黄；无交付或验收条款为红。",
    ),
    # —— 法律 ——
    dict(
        dimension="法律",
        name="责任上限",
        guidance="检查责任上限（liability cap）。上限不低于合同年度总额为绿；上限过低（低于年度总额 50%）为黄；无责任上限或责任无限为红。",
        weight=2,
    ),
    dict(
        dimension="法律",
        name="终止条款",
        guidance="检查合同终止条件（违约终止、便利终止、通知期）。双方对等且通知期合理为绿；仅单方可终止为黄；无终止条款为红。",
        weight=2,
    ),
    dict(
        dimension="法律",
        name="知识产权归属",
        guidance="检查知识产权归属与许可范围。交付成果 IP 归我方或明确许可为绿；共有或范围模糊为黄；IP 全部归供应商且我方无许可为红。",
    ),
    # —— 数据隐私 ——
    dict(
        dimension="数据隐私",
        name="数据保护义务",
        guidance="检查个人数据与 confidential 信息保护义务（加密、访问控制、保密期）。义务完整为绿；义务不完整为黄；无数据保护条款为红。",
        weight=2,
    ),
    dict(
        dimension="数据隐私",
        name="数据泄露通知",
        guidance="检查数据泄露通知时限。不超过 72 小时为绿；72 小时至 7 天为黄；超 7 天或无通知义务为红。",
    ),
    dict(
        dimension="数据隐私",
        name="数据存储地",
        guidance="检查数据存储与处理地域要求。境内或经批准地域为绿；可跨境但需保障为黄；无地域约束为红。",
    ),
    # —— SLA 与绩效 ——
    dict(
        dimension="SLA与绩效",
        name="可用性承诺",
        guidance="检查服务可用性 SLA。不低于 99.9% 为绿；99.0%–99.9% 为黄；低于 99.0% 或无 SLA 为红。",
    ),
    dict(
        dimension="SLA与绩效",
        name="响应时限",
        guidance="检查故障响应与处理时限。分级明确（P1 响应 ≤ 1 小时）为绿；仅有笼统时限为黄；无响应时限为红。",
    ),
    dict(
        dimension="SLA与绩效",
        name="违约补偿条款",
        guidance="检查未达 SLA 的补偿（service credit / 罚则）。有明确补偿机制为绿；补偿上限模糊为黄；无补偿机制为红。",
    ),
    # —— 监管 ——
    dict(
        dimension="监管",
        name="适用法律",
        guidance="检查适用法律与争议解决条款。约定我方所在地法律且有管辖为绿；仅约定对方地法律为黄；未约定适用法律为红。",
    ),
    dict(
        dimension="监管",
        name="审计权",
        guidance="检查我方对供应商的审计权（现场审计、提供合规证据）。审计权完整为绿；审计权受限为黄；无审计权为红。",
        weight=2,
    ),
    dict(
        dimension="监管",
        name="分包披露",
        guidance="检查分包（subcontracting）披露与同意条款。需书面同意且披露为绿；仅需通知为黄；允许自由分包为红。",
    ),
)

CONTRACT_COMPLIANCE_PLAYBOOK = PlaybookSpec(
    id="contract-compliance",
    name="供应商合同合规",
    description="对照公司采购标准逐条审查供应商合同，产出带引用的合规记分卡与审查报告。",
    friendly_id_prefix="CG",
    dimensions=("商业", "法律", "数据隐私", "SLA与绩效", "监管"),
    rule_seeds=_CONTRACT_COMPLIANCE_RULES,
    deliverables=("compliance_report_docx", "scorecard_xlsx"),
    scoring_instructions=(
        "你是供应商合同合规评审代理。本剧本站在我方立场，我方＝采购方（合同中的甲方/委托人），"
        "供应商为乙方；guidance 中的「我方」「对方」一律按此对应，不得从条款行文推测立场。"
        "对每条规则：在合同条款中寻找对应内容，"
        "按规则 guidance 给出 red/amber/green 判定；判定必须引用合同原文片段；"
        "合同中找不到对应内容时判 red 并说明缺失（gap_reason），不得臆造绿色判定。"
    ),
)
