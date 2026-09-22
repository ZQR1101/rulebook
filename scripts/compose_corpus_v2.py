"""Compose the v2 corpus manifest from the clause library — no model calls.

``build_seeded_corpus.py`` turns a manifest into documents and gold; this script
produces the manifest. It owns three decisions that must not be left to chance:

- **which rule gets which colour, and where.** Gap and defect duty is spread by
  usage count so no rule is only ever tested as compliant — a corpus where
  「审计权」 is never violated says nothing about 审计权.
- **what the numbers are**, drawn from the bands the playbook's own guidance
  defines: 61–90 days is yellow for 付款账期 because the guidance says so.
- **which decoy prose goes in**, subject to the real guarantee here: a rule
  marked as a gap must have nothing in that document answering it. Every other
  clause — and every candidate decoy — is scanned against ``GAP_SIGNATURES``, so
  「文档未约定」 is a statement about the text, not a hope.

Output is a plain manifest the builder consumes; documents land under
``eval_cases/seeded_defects_v2/`` and are regenerable from this file alone.

Usage:
    python scripts/compose_corpus_v2.py                  # write the manifest
    python scripts/compose_corpus_v2.py --check          # compose, report, write nothing
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.engine.parsing import split_clauses  # noqa: E402
from scripts.build_seeded_corpus import (  # noqa: E402
    DEFAULT_MAX_CHARS,
    DEFAULT_MIN_CHARS,
    RETRIEVAL_BUDGET_CHARS,
    ClauseSpec,
    DocumentSpec,
    playbook_rules,
    render_document,
)
from scripts.corpus_v2_library import (  # noqa: E402
    DECOYS_APPENDIX,
    DECOYS_GENERAL,
    DECOYS_OPS,
    GAP_SIGNATURES,
    LIBRARIES,
    PART_TITLES,
    Template,
)

# (playbook, how many documents, id prefix)
SHAPES = (("contract-compliance", 16, "CG-V2"), ("delivery-intake", 8, "DI-V2"))
GAP_RANGE = (2, 3)
DEFECT_RANGE = (3, 5)
# The clause text must clear the retrieval budget with room to spare, otherwise a
# small edit would silently drop the document back into whole-document feeding.
CLAUSE_CHARS_FLOOR = RETRIEVAL_BUDGET_CHARS + 800
MAX_ATTEMPTS = 4000
# 8k is the floor the user accepted; aim above it so the 6,000-character
# retrieval budget really is the binding constraint.
VOLUME_TARGET = 10000

# Bands for slots whose admissible range depends on which colour was chosen.
# These are the guidance thresholds, restated: 付款账期「不超过 60 天为绿；61–90
# 天为黄」, 不切实际的承诺 green/amber/red = 匹配 / 偏紧 / 明显不可达.
BANDS: dict[tuple[str, str], dict[str, tuple[int, int]]] = {
    ("付款账期", "green"): {"payment_days": (30, 60)},
    ("付款账期", "amber"): {"payment_days": (61, 90)},
    ("不切实际的承诺", "green"): {"duration": (180, 300)},
    ("不切实际的承诺", "amber"): {"duration": (120, 179)},
    ("不切实际的承诺", "red"): {"duration": (30, 60)},
}
# Slots that occur in a single colour band only, so no plan dependence to track.
FIXED_BANDS: dict[str, tuple[int, int]] = {
    "payment_days": (30, 60),
    "duration": (120, 240),
    "hours": (8, 16),
    "days": (15, 30),
    "tail_days": (365, 730),
    "people": (6, 14),
    "fp": (96, 260),
}
# Discrete candidates: continuous sampling would round 99.9–99.99 up to 100%.
CHOICES: dict[str, tuple[float, ...]] = {
    "breach_hours": (4, 12, 24, 48, 72),
    "late_breach_hours": (96, 120, 168),
    "sla_pct": (99.9, 99.95, 99.99),
    "low_pct": (99.0, 99.2, 99.5, 99.8),
    "cap_pct": (20, 30, 40, 45),
    "credit_pct": (5, 8, 10, 15, 20),
    "pass_pct": (95, 96, 98),
    "scope_pct": (8, 10, 15),
    "penalty_pct": (0.3, 0.5, 0.8),
    "penalty_cap_pct": (5, 8, 10),
    "sample_pct": (10, 20, 30),
    "response_days": (3, 5),
    "handover_days": (3, 5),
    "p1_hours": (1,),
    "late_pct": (0.03, 0.05, 0.08),
}

PARTY_A = (
    "华宁轨道交通集团有限公司", "江州医药控股集团有限责任公司", "临海港口物流股份有限公司",
    "晟元电力科学研究院", "广衡建设发展集团有限公司", "川原食品股份有限公司",
    "澜溪水务集团有限公司", "中宸航空器材有限公司", "恒济保险经纪有限公司",
    "南岭烟草有限责任公司", "睿城城市投资发展集团", "启帆汽车制造有限公司",
    "盛通石化炼油有限公司", "云岭广电网络股份有限公司", "昌原粮食储备有限责任公司",
    "沁园地铁运营有限公司", "同泰港口机械股份有限公司", "汇川轨道交通装备有限公司",
    "紫云医药商业集团", "长风能源化工有限责任公司",
)
PARTY_B = (
    "拓维信息技术有限公司", "云枢数据服务有限公司", "明衡软件科技有限公司",
    "川流智能系统有限公司", "澜图数字科技有限公司", "启明星云计算有限公司",
    "锦程信息技术服务有限公司", "博彦网络工程有限公司", "恒天系统集成有限公司",
    "睿见数据科技有限公司", "南天资讯产业有限公司", "同信软件股份有限公司",
    "海通电子信息有限公司", "泽富科技服务有限公司", "翰辰信息技术有限公司",
    "青梧数字化解决方案有限公司", "晟达工业软件有限公司", "观澜网络科技有限公司",
    "连理智能技术有限公司", "泰铭电气自动化有限公司",
)
SYSTEMS = (
    "智慧运维管理平台", "档案数字化管理系统", "临床数据中台", "调度指挥系统",
    "业财一体化平台", "客户服务平台", "生产执行系统", "资产全生命周期管理系统",
    "在线学习与考核平台", "供应链协同平台", "数据治理平台", "智能客服系统",
    "电子签章平台", "风险监测预警系统", "设备物联管理平台", "仓储管理系统",
    "统一身份认证系统", "报表与分析平台", "工单流转系统", "视频监控联网系统",
)
VENDORS = ("鲲鹏", "澜图", "启明", "川流", "翰辰", "青梧", "泰铭", "云枢")
CAMPUSES = ("城西科技园", "滨江科创园", "高新软件基地", "经开信息港", "临港数据产业园", "南湖软件园")
CITIES = ("宁波", "成都", "西安", "苏州", "佛山", "长沙", "厦门", "合肥", "昆明", "青岛")
CONTACTS = (
    "周敏", "李振国", "陈雅琴", "许洋", "罗建平", "高欣", "谢岚", "邓一鸣", "韩雪松", "曹宇",
    "袁立群", "沈佳", "汪镇", "孟岩", "祝婉", "石敢", "龚晴", "戴牧",
)
# Business-domain names the attachment clauses rotate through, so the same
# template can appear twice in one document without repeating its text.
MODULES = (
    "台账管理", "工单流转", "指标看板", "收文办理", "库存核算", "巡检作业", "合同台账",
    "班次排程", "预警订阅", "报表导出", "基础编码", "审批流转",
)
ORDINALS = ("一", "二", "三", "四", "五")
APPENDIX_ROUNDS = len(ORDINALS)

CLEAN_NOTE = "guidance 所列要件齐备，应为绿"
DEFECT_NOTES = {
    ("付款账期", "amber"): "账期落在 61–90 天区间，且审批期限可顺延",
    ("付款账期", "red"): "以主管部门批复为付款前提、未约定明确账期，乙方垫资",
    ("定价清晰度", "amber"): "暂定总价＋按人月据实结算，调价机制需年度协商",
    ("定价清晰度", "red"): "无固定价格，允许乙方单方调整计费口径",
    ("交付与验收条款", "amber"): "交付物与评审依据留在实施计划里，验收判定不可逐项核对",
    ("交付与验收条款", "red"): "只移交使用权，交付范围、形式与时点均未约定",
    ("责任上限", "amber"): "上限仅为年度服务费的不足一半，低于合同年度总额",
    ("责任上限", "red"): "赔偿一切损失且不以合同价款为限，责任无限",
    ("终止条款", "amber"): "只有甲方可以便利终止，乙方不得单方解除，权利不对等",
    ("终止条款", "red"): "全文没有终止或解除安排，任何一方不得中途退出",
    ("知识产权归属", "amber"): "成果共有，对外许可需对方同意，使用边界模糊",
    ("知识产权归属", "red"): "成果全部归乙方，且未授予我方任何使用许可",
    ("数据保护义务", "amber"): "只有一般保密承诺，技术与管理措施按乙方自有规范执行",
    ("数据保护义务", "red"): "未约定访问控制、留存期限与返还删除义务",
    ("数据泄露通知", "amber"): "通知时限落在 72 小时至 7 天之间",
    ("数据泄露通知", "red"): "是否通报由乙方依内部流程自行判断，无通知义务",
    ("数据存储地", "amber"): "存放于乙方云平台并可能在关联节点复制副本，跨境仅承诺另行评估",
    ("数据存储地", "red"): "承载方式由乙方自行确定，全文无地域约束",
    ("可用性承诺", "amber"): "SLA 落在 99.0%–99.9% 之间，且排除上游与第三方中断",
    ("可用性承诺", "red"): "仅承诺「尽力保证」，没有可用性指标",
    ("响应时限", "amber"): "只有一个笼统的响应工作日，未分级、无恢复目标",
    ("响应时限", "red"): "只有报修渠道，「尽快处理」不构成时限",
    ("违约补偿条款", "amber"): "补偿形式为延长服务或等值支持，价值由双方协商，上限模糊",
    ("违约补偿条款", "red"): "未达标只有原因分析与改进建议，没有补偿机制",
    ("适用法律", "amber"): "仲裁机构设在对方所在地，管辖不利于我方",
    ("适用法律", "red"): "只写友好协商，未约定适用法律与争议解决方式",
    ("审计权", "amber"): "只能索取书面材料，没有现场核查与进入系统的权利",
    ("审计权", "red"): "只有乙方自我声明与认证证书，我方无审计权",
    ("分包披露", "amber"): "分包仅需事后通知，无须事先同意",
    ("分包披露", "red"): "乙方可自行安排协作单位，无须告知甲方",
    ("需求范围明确", "amber"): "范围只到系统开发与部署，其余相关事项留待协商纳入",
    ("需求范围明确", "red"): "工作内容以客户届时实际需要为准，全文没有范围定义",
    ("验收标准", "amber"): "以评审满意为准，没有可逐项核对的量化判定",
    ("验收标准", "red"): "开始使用即视为认可，无验收标准",
    ("关键角色与联系人", "amber"): "只有转达需求的对接联系人，决策人与业务答复人缺失",
    ("关键角色与联系人", "red"): "只写各自安排人员，未指明任何角色或联系人",
    ("时间与里程碑", "amber"): "只有一个总完成期限，无阶段里程碑",
    ("时间与里程碑", "red"): "时间以客户届时通知为准，全文无时间定义",
    ("范围蔓延风险", "amber"): "有调整确认流程，但小比例调整可先口头实施",
    ("范围蔓延风险", "red"): "开放式「协助、支持、配合新增要求」且不另行计费，无变更控制",
    ("不切实际的承诺", "amber"): "工期较同类项目偏紧，需并行推进开发与测试",
    ("不切实际的承诺", "red"): "极短工期＋一次性上线＋零缺陷＋不加费用，明显不可达",
    ("依赖与前置条件", "amber"): "只说客户方尽力配合提供环境与数据，未列明前置事项",
    ("依赖与前置条件", "red"): "所需条件按需协调，全文未识别任何依赖",
    ("单点依赖", "amber"): "接口工作集中于单一平台专家，仅有协商处理的缓解意向",
    ("单点依赖", "red"): "开发、部署、运维全部由一名项目经理掌握，且无备份安排",
    ("计费与结算", "amber"): "有固定总价与一次性支付，缺结算周期与发票要件",
    ("计费与结算", "red"): "费用结算时商定、支付以客户审批完成为准，无计费定义",
    ("责任与赔偿", "amber"): "违约金只约束乙方逾期交付，客户方延迟不承担责任",
    ("责任与赔偿", "red"): "乙方赔偿一切损失且不以合同总价为限，客户方免责",
    ("数据处理合规", "amber"): "涉及个人信息但未约定授权范围、期限与删除返还",
    ("数据处理合规", "red"): "开放生产库完整读写权限并含健康等敏感字段，无任何授权安排",
    ("保密与知识产权", "amber"): "仅乙方单方承担保护义务，成果权属留待协商",
    ("保密与知识产权", "red"): "披露与成果归属均不作安排",
}
GAP_NOTES = {
    "付款账期": "全文未约定付款账期与支付前提",
    "定价清晰度": "全文无价格与费用条款",
    "交付与验收条款": "全文未约定交付内容、时间与验收流程",
    "责任上限": "全文无责任限额安排",
    "终止条款": "全文未约定合同解除或终止条件与通知期",
    "知识产权归属": "全文未涉及成果权利归属与许可范围",
    "数据保护义务": "全文未约定数据与机密信息的保护义务",
    "数据泄露通知": "全文未约定安全事件的通报义务与时限",
    "数据存储地": "全文未约定数据存储与处理的地域要求",
    "可用性承诺": "全文未给出服务可用性指标",
    "响应时限": "全文未约定故障响应与处理时限",
    "违约补偿条款": "全文未约定未达服务目标时的补偿机制",
    "适用法律": "全文未约定适用法律与争议解决方式",
    "审计权": "全文未赋予我方核查或审计的权利",
    "分包披露": "全文未涉及分包披露与同意安排",
    "需求范围明确": "全文未定义交付范围与边界",
    "验收标准": "全文未定义任何成果确认标准",
    "关键角色与联系人": "全文未指明客户方关键角色与联系人",
    "时间与里程碑": "全文未给出时间表或里程碑",
    "范围蔓延风险": "全文未出现工作内容调整或新增要求的处理安排",
    "不切实际的承诺": "全文未给出工期、投入与质量目标，无从判断承诺是否可达",
    "依赖与前置条件": "全文未识别客户侧依赖与开工前提",
    "单点依赖": "全文未涉及关键人员、组件的冗余安排",
    "计费与结算": "全文未约定计费方式与支付安排",
    "责任与赔偿": "全文未约定违约责任与赔偿安排",
    "数据处理合规": "全文未涉及数据提供与使用授权",
    "保密与知识产权": "全文未约定资料保护与成果权属",
}


def _number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


def _text_of(template: Template) -> str:
    return "\n".join(template.sentences)


def _clashes(text: str, blocked: tuple[str, ...]) -> list[str]:
    """Gap signatures present in ``text`` — any hit makes a gap contestable."""

    return [term for term in blocked if term in text]


def _base_slots(rng: random.Random) -> dict[str, str]:
    """Names plus the document-wide numbers; colour-dependent bands come later."""

    amount = rng.randrange(90, 900) * 10000
    slots = {
        "party_a": rng.choice(PARTY_A),
        "party_b": rng.choice(PARTY_B),
        "system": rng.choice(SYSTEMS),
        "vendor": rng.choice(VENDORS),
        "campus": rng.choice(CAMPUSES),
        "city": rng.choice(CITIES),
        "contact_a": rng.choice(CONTACTS),
        "contact_b": rng.choice(CONTACTS),
        "amount": f"{amount:,}",
        "annual": f"{amount // 2:,}",
        "rate": f"{rng.randrange(24, 46) * 1000:,}",
    }
    for name, (low, high) in FIXED_BANDS.items():
        slots[name] = _number(rng.randint(low, high))
    for name, choices in CHOICES.items():
        slots[name] = _number(rng.choice(choices))
    return slots


def _slots_for(rng: random.Random, ratings: dict[str, str]) -> dict[str, str]:
    slots = _base_slots(rng)
    for rule, rating in ratings.items():
        for name, (low, high) in BANDS.get((rule, rating), {}).items():
            slots[name] = _number(rng.randint(low, high))
    return _fill_dates(rng, slots)


def _fill_dates(rng: random.Random, slots: dict[str, str]) -> dict[str, str]:
    """Lay the calendar out along the chosen duration, so dates and 工期 agree."""

    duration = int(float(slots["duration"]))
    begin = date(2026, rng.randint(1, 6), rng.randint(1, 28))
    slots["date"] = _cn_date(begin + timedelta(days=max(1, round(duration * 0.1))))
    slots["date2"] = _cn_date(begin + timedelta(days=round(duration * 0.45)))
    slots["date3"] = _cn_date(begin + timedelta(days=round(duration * 0.7)))
    slots["date4"] = _cn_date(begin + timedelta(days=duration))
    return slots


def _cn_date(day: date) -> str:
    return f"{day.year}年{day.month}月{day.day}日"


def _fill(template: Template, slots: dict[str, str]) -> list[str]:
    try:
        return [sentence.format(**slots) for sentence in template.sentences]
    except KeyError as exc:  # a template grew a slot the composer does not know
        raise ValueError(f"模板「{template.heading}」缺少槽位 {exc}") from exc


def _plan(
    rng: random.Random,
    rules: tuple[str, ...],
    library: dict[str, dict[str, Template]],
    usage: dict[str, dict[str, int]],
) -> tuple[tuple[str, ...], dict[str, str]] | None:
    """Colour the rules, then take gaps only from the rules that can really be empty."""

    defect_wanted = rng.randint(*DEFECT_RANGE)
    defect_order = sorted(rules, key=lambda rule: (usage[rule]["defect"], rng.random()))
    booked = set(defect_order[:defect_wanted])
    ratings = {rule: "green" for rule in rules}
    for rule in booked:
        # Both violated bands have to be exercised: guidance writes most rules as
        # 绿 / 黄 / 红, and a corpus that only ever plants red tests half the rubric.
        ratings[rule] = min(("red", "amber"), key=lambda colour: (usage[rule][colour], rng.random()))

    bodies = {rule: _text_of(library[rule][ratings[rule]]) for rule in rules}
    eligible = [
        candidate
        for candidate in rules
        if candidate not in booked
        and not _clashes(
            "\n".join(text for rule, text in bodies.items() if rule != candidate), _blockers((candidate,))
        )
    ]
    # A gap must not steal a rule already booked as a defect, or the document
    # silently drops below the agreed minimum number of planted violations.
    if len(eligible) < GAP_RANGE[0]:
        return None
    gap_order = sorted(eligible, key=lambda rule: (usage[rule]["gap"], rng.random()))
    gaps = tuple(gap_order[: min(len(gap_order), rng.randint(*GAP_RANGE))])
    return gaps, {rule: rating for rule, rating in ratings.items() if rule not in gaps}


def _answered_clauses(
    library: dict[str, dict[str, Template]],
    rules: tuple[str, ...],
    gaps: tuple[str, ...],
    ratings: dict[str, str],
    slots: dict[str, str],
    blocked: tuple[str, ...],
) -> list[ClauseSpec] | None:
    """One clause per answered rule, or None if any of them answers a gap rule too."""

    clauses: list[ClauseSpec] = []
    for rule in rules:
        if rule in gaps:
            continue
        rating = ratings[rule]
        template = library[rule][rating]
        sentences = _fill(template, slots)
        if _clashes("\n".join(sentences), blocked):
            return None
        clauses.append(
            ClauseSpec(
                heading=template.heading,
                sentences=sentences,
                answers=rule,
                kind="clean" if rating == "green" else "defect",
                expected_rating=None if rating == "green" else rating,
                note=DEFECT_NOTES.get((rule, rating), CLEAN_NOTE),
            )
        )
    return clauses


_BLOCKERS: dict[tuple[str, ...], tuple[str, ...]] = {}


def _blockers(gaps: tuple[str, ...]) -> tuple[str, ...]:
    if gaps not in _BLOCKERS:
        terms = {term for rule in gaps for term in GAP_SIGNATURES[rule]}
        _BLOCKERS[gaps] = tuple(sorted(terms))
    return _BLOCKERS[gaps]


def _decoy_pools(
    gaps: tuple[str, ...], rng: random.Random
) -> tuple[list[Template], list[Template], list[Template]]:
    blocked = _blockers(gaps)

    def allowed(pool: tuple[Template, ...]) -> list[Template]:
        picked = [template for template in pool if not _clashes(_text_of(template), blocked)]
        rng.shuffle(picked)
        return picked

    return allowed(DECOYS_GENERAL), allowed(DECOYS_OPS), allowed(DECOYS_APPENDIX)


def _appendix_instances(rng: random.Random, slots: dict[str, str], templates: list[Template]) -> list[ClauseSpec]:
    """The same attachment text, once per named business domain — the volume knob."""

    modules = list(MODULES)
    rng.shuffle(modules)
    instances: list[ClauseSpec] = []
    for round_index in range(APPENDIX_ROUNDS):
        local = dict(slots, module=modules[round_index % len(modules)])
        for template in templates:
            instances.append(
                ClauseSpec(
                    heading=f"{template.heading}（{ORDINALS[round_index]}）",
                    sentences=_fill(template, local),
                )
            )
    return instances


def _assemble(
    playbook_id: str,
    doc_id: str,
    slots: dict[str, str],
    answered: list[ClauseSpec],
    general: list[ClauseSpec],
    mechanics: list[ClauseSpec],
    gaps: tuple[str, ...],
) -> DocumentSpec:
    halfway = len(answered) // 2 + 1
    parts = [
        answered[:halfway],
        answered[halfway:] + mechanics,
        general,
    ]
    return DocumentSpec(
        id=doc_id,
        title=_title(playbook_id, slots),
        playbook_id=playbook_id,
        parts=parts,
        part_titles=PART_TITLES[playbook_id],
        preamble=_preamble(playbook_id, doc_id, slots),
        gap_notes={rule: GAP_NOTES[rule] for rule in gaps},
    )


def _decoy_clause(template: Template, slots: dict[str, str]) -> ClauseSpec:
    return ClauseSpec(heading=template.heading, sentences=_fill(template, slots))


def compose_document(
    playbook_id: str,
    doc_id: str,
    rng: random.Random,
    usage: dict[str, dict[str, int]],
) -> DocumentSpec:
    library = LIBRARIES[playbook_id]
    rules = tuple(library)
    rejects: Counter[str] = Counter()
    for _ in range(MAX_ATTEMPTS):
        plan = _plan(rng, rules, library, usage)
        if plan is None:
            rejects["没有可构成真空缺口的规则"] += 1
            continue
        gaps, ratings = plan
        slots = _slots_for(rng, ratings)
        blocked = _blockers(gaps)
        answered = _answered_clauses(library, rules, gaps, ratings, slots, blocked)
        if answered is None:
            rejects["条款与所选缺口的签名词冲突"] += 1
            continue
        general, mechanics, appendix = _decoy_pools(gaps, rng)
        spec = _fit_length(
            playbook_id, doc_id, slots, answered,
            [_decoy_clause(template, slots) for template in general],
            [_decoy_clause(template, slots) for template in mechanics],
            _appendix_instances(rng, slots, appendix),
            gaps,
        )
        if spec is None:
            rejects["诱饵不足以撑过检索预算或正文超长"] += 1
            continue
        if _head_names_rule(spec):
            rejects["标题或前言写出规则名"] += 1
            continue
        if _answers_a_gap(spec, gaps):
            rejects["缺口被正文别处回答"] += 1
            continue
        if _has_duplicate_clause_bodies(spec):
            rejects["存在正文完全相同的条款，金标无法定位"] += 1
            continue
        for rule in gaps:
            usage[rule]["gap"] += 1
        for clause in answered:
            if clause.kind == "defect":
                usage[clause.answers]["defect"] += 1
                usage[clause.answers][clause.expected_rating] += 1
        return spec
    detail = "；".join(f"{reason}×{count}" for reason, count in rejects.items())
    raise ValueError(f"{doc_id}：在 {MAX_ATTEMPTS} 次尝试内找不到自洽的语料组合（{detail}）")


def _fit_length(
    playbook_id: str,
    doc_id: str,
    slots: dict[str, str],
    answered: list[ClauseSpec],
    general: list[ClauseSpec],
    mechanics: list[ClauseSpec],
    appendix: list[ClauseSpec],
    gaps: tuple[str, ...],
) -> DocumentSpec | None:
    """Fewest attachment clauses that carry the document to the target volume."""

    for take in range(len(appendix) + 1):
        spec = _assemble(playbook_id, doc_id, slots, answered, general, mechanics + appendix[:take], gaps)
        text = render_document(spec)
        clause_chars = sum(len(clause.text) for clause in split_clauses(text))
        if clause_chars < CLAUSE_CHARS_FLOOR or len(text) < VOLUME_TARGET:
            continue
        return spec if len(text) <= DEFAULT_MAX_CHARS else None
    return None


def _title(playbook_id: str, slots: dict[str, str]) -> str:
    if playbook_id == "contract-compliance":
        return f"{slots['party_a']}{slots['system']}采购项目合同"
    return f"{slots['party_a']}{slots['system']}服务项目工作说明书"


def _preamble(playbook_id: str, doc_id: str, slots: dict[str, str]) -> list[str]:
    role_a, role_b = ("采购方", "供应商") if playbook_id == "contract-compliance" else ("客户", "服务方")
    return [
        f"文档编号：{doc_id}",
        f"甲方（{role_a}）：{slots['party_a']}",
        f"乙方（{role_b}）：{slots['party_b']}",
        "本文件为双方合作内容的约定文本，附件与正文具有同等效力。",
    ]


def _head_names_rule(spec: DocumentSpec) -> bool:
    """The title and header block must not announce what the document is testing."""

    names = playbook_rules(spec.playbook_id)
    head = "\n".join([spec.title, *spec.preamble, *spec.part_titles])
    return any(name in head for name in names)


def _answers_a_gap(spec: DocumentSpec, gaps: tuple[str, ...]) -> bool:
    """Nothing in the rendered document — part titles and preamble included — may answer a gap."""

    text = render_document(spec)
    return any(_clashes(text, _blockers((rule,))) for rule in gaps)


def _has_duplicate_clause_bodies(spec: DocumentSpec) -> bool:
    """Gold look-up needs every clause body to resolve to exactly one segment."""

    bodies = [clause.body for part in spec.parts for clause in part]
    return len(set(bodies)) != len(bodies)


def spec_to_manifest(spec: DocumentSpec) -> dict:
    def clause_to_dict(clause: ClauseSpec) -> dict:
        fields = {
            "heading": clause.heading,
            "sentences": clause.sentences,
            "answers": clause.answers,
            "kind": clause.kind,
            "expected_rating": clause.expected_rating,
            "note": clause.note,
        }
        return {key: value for key, value in fields.items() if value not in (None, "")}

    return {
        "id": spec.id,
        "title": spec.title,
        "playbook_id": spec.playbook_id,
        "part_titles": spec.part_titles,
        "preamble": spec.preamble,
        "gap_notes": spec.gap_notes,
        "parts": [{"clauses": [clause_to_dict(clause) for clause in part]} for part in spec.parts],
    }


def compose_all(seed: int) -> tuple[list[DocumentSpec], dict[str, int]]:
    usage: dict[str, dict[str, int]] = {
        rule: {"gap": 0, "defect": 0, "red": 0, "amber": 0}
        for library in LIBRARIES.values()
        for rule in library
    }
    specs: list[DocumentSpec] = []
    lengths: dict[str, int] = {}
    for playbook_id, count, prefix in SHAPES:
        rng = random.Random(f"{seed}:{playbook_id}")
        for index in range(count):
            doc_id = f"{prefix}-{index + 1:02d}"
            spec = compose_document(playbook_id, doc_id, rng, usage)
            specs.append(spec)
            lengths[doc_id] = len(render_document(spec))
    return specs, lengths


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "eval_cases" / "corpus_v2_manifest.json")
    parser.add_argument("--seed", type=int, default=20260922)
    parser.add_argument("--check", action="store_true", help="只组卷并报告，不写 manifest")
    args = parser.parse_args()

    specs, lengths = compose_all(args.seed)
    rules = {rule for library in LIBRARIES.values() for rule in library}
    gaps = {rule for spec in specs for rule in spec.gap_notes}
    defects = {
        clause.answers
        for spec in specs
        for part in spec.parts
        for clause in part
        if clause.kind == "defect"
    }
    sizes = sorted(lengths.values())
    red = sum(
        1
        for spec in specs
        for part in spec.parts
        for clause in part
        if clause.expected_rating == "red"
    )
    amber = sum(
        1
        for spec in specs
        for part in spec.parts
        for clause in part
        if clause.expected_rating == "amber"
    )
    print(
        f"[INFO] {len(specs)} 份文档 · 正文长度 {sizes[0]}~{sizes[-1]} 字 · 平均 {sum(sizes) // len(sizes)} 字"
    )
    print(
        f"[INFO] 被标为缺口的规则 {len(gaps)}/{len(rules)} 条 · "
        f"被埋入缺陷的规则 {len(defects)}/{len(rules)} 条 · 缺陷档位 red={red} amber={amber}"
    )
    never = sorted(rules - gaps - defects)
    if never:
        print(f"[WARN] 以下规则既未作缺口也未埋入缺陷，等于没被考到：{never}")
    print(f"[INFO] 未作为缺口出现过的规则：{sorted(rules - gaps)}")

    if args.check:
        return 0

    manifest = {
        "defaults": {
            "playbook_id": "contract-compliance",
            "min_chars": DEFAULT_MIN_CHARS,
            "max_chars": DEFAULT_MAX_CHARS,
            "out_dir": "eval_cases/seeded_defects_v2",
            "cases_path": "eval_cases/seeded_defect_cases_v2.json",
        },
        "documents": [spec_to_manifest(spec) for spec in specs],
    }
    path = args.out if args.out.is_absolute() else PROJECT_ROOT / args.out
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[OK] manifest 写入 {path.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
