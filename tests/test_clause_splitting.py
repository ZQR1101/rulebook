"""Clause splitting must key off structure, not off "this is a line".

``_HEADING_RE`` is compiled with ``re.VERBOSE``, where ``#`` starts a comment.
The markdown branch wrote it unescaped, which turned that branch into an empty
alternative — and an empty alternative matches every line, so each line became
its own clause and the branch that was supposed to support ``## 标题`` never
actually ran.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.engine.parsing import MAX_CLAUSE_CHARS, _is_clause_start, split_clauses  # noqa: E402

PROSE = [
    "通知与送达",
    "标题与解释",
    "本合同一式肆份，甲乙双方各持贰份，具有同等法律效力。",
    "一方按本合同载明的通讯地址发出书面通知的，交付快递之日起第三个工作日视为送达。",
    "乙方应于每月五日前向甲方提供上月运行报告。",
]

HEADINGS = [
    "第五条 合同终止",
    "第三十七条之一 附则",
    "1. 项目背景与目标",
    "2.1 乙方负责三个模块的开发",
    "（一）总体要求",
    "一、总则",
    "## 标题",
    "Article 4 of the agreement",
]


class TestClauseStartDetection:
    def test_ordinary_prose_lines_are_not_clause_starts(self):
        for line in PROSE:
            assert not _is_clause_start(line), f"普通句子被当成了条款起点：{line}"

    def test_every_supported_heading_style_is_a_clause_start(self):
        for line in HEADINGS:
            assert _is_clause_start(line), f"标题没有被识别：{line}"

    def test_the_markdown_heading_branch_actually_runs(self):
        # Regression for the unescaped '#': in re.VERBOSE it comments out the
        # rest of the line, leaving an alternative that matches everything.
        assert _is_clause_start("### 三级标题")
        assert not _is_clause_start("这句话里有 # 号但没有标题结构")


class TestNoTextIsLost:
    """An over-long block used to be truncated, so everything past 2000 chars vanished."""

    @staticmethod
    def _prose(lines: int) -> str:
        return "\n".join(
            f"这是第{index}句正文，不含任何编号结构，用于测量解析器是否会丢字。" for index in range(1, lines + 1)
        )

    def test_a_long_prose_document_loses_no_characters(self):
        text = self._prose(400)
        parsed = "".join(clause.text for clause in split_clauses(text))
        assert len(text.replace("\n", "")) == len(parsed.replace("\n", ""))
        assert "第400句正文" in parsed

    def test_every_clause_still_respects_the_length_cap(self):
        clauses = split_clauses(self._prose(400))
        assert len(clauses) > 1
        assert all(len(clause.text) <= MAX_CLAUSE_CHARS for clause in clauses)

    def test_a_long_numbered_article_keeps_its_tail(self):
        tail = "本款最后一句约定数据只保留三十日。"
        text = "第三条 数据处理\n" + ("乙方应妥善保管甲方数据。" * 200) + tail
        clauses = split_clauses(text)
        assert tail in "".join(clause.text for clause in clauses)

    def test_chunks_break_on_sentence_boundaries_not_mid_sentence(self):
        clauses = split_clauses(self._prose(200))
        for clause in clauses[:-1]:
            assert clause.text.rstrip().endswith(("。", "；", "！", "？", "：", ",")), clause.text[-12:]

    def test_the_whole_document_fallback_is_chunked_too(self):
        text = self._prose(400).replace("，不含任何编号结构，用于测量解析器是否会丢字。", "")
        parsed = "".join(clause.text for clause in split_clauses(text))
        assert len(text.replace("\n", "")) == len(parsed.replace("\n", ""))


class TestSplitClauses:
    def test_one_numbered_article_yields_one_clause_not_one_per_line(self):
        text = "第三条 付款账期\n甲方应在收到有效发票后 120 日内支付。\n逾期未付款的，乙方有权暂停服务。"
        clauses = split_clauses(text)
        assert [clause.heading for clause in clauses] == ["第三条 付款账期"]
        assert "有权暂停服务" in clauses[0].text

    def test_a_document_without_any_heading_stays_a_single_clause(self):
        clauses = split_clauses("\n".join(PROSE))
        assert len(clauses) == 1
        assert clauses[0].heading is None

    def test_unstructured_prose_does_not_shatter_into_per_line_clauses(self):
        body = "\n".join(f"这是第{index}句正文，不含任何编号结构。" for index in range(1, 9))
        clauses = split_clauses(body)
        assert len(clauses) < len(body.splitlines())
