import React, { useCallback, useEffect, useMemo, useState } from "react";
import { api, RATING_LABELS } from "../api.js";
import {
  Chip,
  ErrorBanner,
  Icon,
  Modal,
  MonoId,
  PrimaryButton,
  SecondaryButton,
  Spinner,
  StateTag,
  StatusChip,
  TextAction,
} from "../ui.jsx";

function DecisionModal({ document_, verdict, initialDecision = "approve", onClose, onDone }) {
  const [decision, setDecision] = useState(initialDecision);
  const [newRating, setNewRating] = useState(verdict.rating);
  const [note, setNote] = useState("");
  const [rationale, setRationale] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const ratingText = RATING_LABELS[verdict.rating] || verdict.rating;

  const submit = async () => {
    setError("");
    if (decision === "reject" && !note.trim()) {
      setError("驳回必须填写专家意见");
      return;
    }
    if (decision === "edit" && newRating === verdict.rating && !rationale.trim()) {
      setError("改判请选择新等级或填写修改理由");
      return;
    }
    setBusy(true);
    try {
      await api.decide(document_.id, verdict.id, {
        decision,
        expert_note: note.trim() || null,
        new_rating: decision === "edit" ? newRating : null,
        rationale: decision === "edit" ? rationale.trim() || null : null,
      });
      onDone();
    } catch (err) {
      setError(err.message || "操作失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title="确认判定" onClose={onClose} width="max-w-md">
      <div className="flex items-center gap-2">
        <Chip tone={verdict.rating === "red" ? "red" : verdict.rating === "amber" ? "amber" : "green"} dot>
          {ratingText}灯
        </Chip>
        <span className="text-[14px] font-semibold text-ink">{verdict.rule_name}</span>
      </div>

      <div className="mt-3 flex gap-1 rounded-md border border-line bg-canvas p-1">
        {[
          ["approve", "批准"],
          ["edit", "改判"],
          ["reject", "驳回"],
        ].map(([key, label]) => (
          <button
            key={key}
            onClick={() => {
              setDecision(key);
              setError("");
            }}
            className={`flex-1 rounded px-3 py-1.5 text-[13px] font-medium transition-colors ${
              decision === key ? "bg-surface text-accent shadow-pop" : "text-ink-2 hover:text-ink"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {decision === "approve" && (
        <p className="mt-3 text-[13px] text-ink-2">
          确认将该判定保持为{ratingText}灯吗？批准后将计入定稿报告。
        </p>
      )}

      {decision === "edit" && (
        <div className="mt-3">
          <div className="text-[13px] font-medium text-ink">新等级</div>
          <div className="mt-1.5 flex gap-2">
            {["red", "amber", "green"].map((r) => (
              <button
                key={r}
                onClick={() => setNewRating(r)}
                className={`flex items-center gap-1.5 rounded border px-3 py-1.5 text-[13px] transition-colors ${
                  newRating === r
                    ? "border-accent bg-accent-tint font-semibold text-accent"
                    : "border-line text-ink-2 hover:border-line-strong"
                }`}
              >
                <span
                  className={`h-2 w-2 rounded-full ${
                    r === "red" ? "bg-red-text" : r === "amber" ? "bg-amber-text" : "bg-green-text"
                  }`}
                />
                {RATING_LABELS[r]}
              </button>
            ))}
          </div>
          <textarea
            value={rationale}
            onChange={(e) => setRationale(e.target.value)}
            placeholder="改判理由（可留空，将记录你的新等级）"
            rows={2}
            className="mt-3 w-full rounded-md border border-line-strong px-3 py-2 text-[13px] outline-none focus:border-accent focus:ring-[3px] focus:ring-accent/10"
          />
        </div>
      )}

      {decision === "reject" && (
        <p className="mt-3 text-[13px] text-ink-2">
          驳回表示不同意机器判定；被驳回的判定将阻止文档定稿，需后续处理。
        </p>
      )}

      <label className="mt-3 block text-[13px] font-medium text-ink">
        专家意见{decision === "reject" ? <span className="text-red-text">（必填）</span> : "（选填）"}
      </label>
      <textarea
        value={note}
        onChange={(e) => setNote(e.target.value)}
        rows={2}
        className="mt-1.5 w-full rounded-md border border-line-strong px-3 py-2 text-[13px] outline-none focus:border-accent focus:ring-[3px] focus:ring-accent/10"
      />

      {error && <div className="mt-3"><ErrorBanner message={error} /></div>}

      <div className="mt-4 flex justify-end gap-2">
        <SecondaryButton onClick={onClose}>取消</SecondaryButton>
        <PrimaryButton onClick={submit} disabled={busy} className={decision === "reject" ? "bg-red-text hover:bg-red-text/90" : ""}>
          {decision === "approve" ? "确认批准" : decision === "edit" ? "确认改判" : "确认驳回"}
        </PrimaryButton>
      </div>
    </Modal>
  );
}

function VerdictCard({ verdict, canReview, onDecide }) {
  const [open, setOpen] = useState(false);
  const stateTone =
    verdict.review_state === "awaiting_review" ? "amber" : verdict.review_state === "rejected" ? "red" : "muted";

  return (
    <div className="rounded-lg border border-line bg-surface p-4 shadow-card">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <span
            className={`mt-0.5 inline-flex h-6 w-6 shrink-0 items-center justify-center rounded border text-[12px] font-semibold whitespace-nowrap ${
              verdict.rating === "red"
                ? "border-red-line bg-red-bg text-red-text"
                : verdict.rating === "amber"
                  ? "border-amber-line bg-amber-bg text-amber-text"
                  : "border-green-line bg-green-bg text-green-text"
            }`}
          >
            {RATING_LABELS[verdict.rating]}
          </span>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-[14px] font-semibold text-ink">{verdict.rule_name}</span>
              {verdict.weight > 1 && (
                <span className="rounded border border-line bg-canvas px-1.5 py-0.5 text-[11px] font-medium text-ink-2">
                  ×{verdict.weight}
                </span>
              )}
              <Chip tone={stateTone}>{verdict.review_state === "awaiting_review" ? "待签字" : verdict.review_state === "approved" ? "已批准" : verdict.review_state === "edited" ? "已改判" : verdict.review_state === "rejected" ? "已驳回" : "初判"}</Chip>
            </div>
            <p className="mt-1.5 text-[13px] leading-5 text-ink-2">{verdict.rationale}</p>
            {verdict.gap_reason && (
              <div className="mt-2 flex items-center gap-1.5 rounded border border-amber-line bg-amber-bg px-2.5 py-1.5 text-[12px] text-amber-text">
                <Icon name="warning" className="text-[15px]" />
                缺口：{verdict.gap_reason}
              </div>
            )}
            {(verdict.citations?.length || 0) > 0 && (
              <div className="mt-2">
                <button
                  onClick={() => setOpen((v) => !v)}
                  className="flex items-center gap-1 text-[12px] font-medium text-ink-2 transition-colors hover:text-accent"
                >
                  引用原文
                  <Icon name={open ? "expand_less" : "expand_more"} className="text-[16px]" />
                </button>
                {open && (
                  <div className="mt-1.5 space-y-1.5">
                    {verdict.citations.map((citation, index) => (
                      <blockquote
                        key={index}
                        className="rounded-r border-l-[3px] border-accent bg-canvas px-3 py-2 text-[13px] italic leading-5 text-ink-2"
                      >
                        “{citation.quote}”
                      </blockquote>
                    ))}
                  </div>
                )}
              </div>
            )}
            {verdict.expert_note && (
              <div className="mt-2 text-[12px] text-ink-3">
                专家意见（{verdict.reviewed_by}）：{verdict.expert_note}
              </div>
            )}
          </div>
        </div>

        {canReview && verdict.review_state === "awaiting_review" && (
          <div className="flex shrink-0 items-center gap-1">
            <TextAction onClick={() => onDecide(verdict, "approve")}>批准</TextAction>
            <TextAction onClick={() => onDecide(verdict, "edit")}>改判</TextAction>
            <TextAction tone="red" onClick={() => onDecide(verdict, "reject")}>
              驳回
            </TextAction>
          </div>
        )}
        {!canReview && verdict.review_state === "approved" && (
          <div className="flex shrink-0 items-center gap-1 whitespace-nowrap text-[12px] text-green-text">
            <Icon name="verified" className="text-[15px]" />
            专家已签字确认
          </div>
        )}
        {!canReview && verdict.review_state === "edited" && (
          <div className="flex shrink-0 items-center gap-1 whitespace-nowrap text-[12px] text-accent">
            <Icon name="published_with_changes" className="text-[15px]" />
            {verdict.reviewed_by ? `已改判 · ${verdict.reviewed_by}` : "已改判"}
          </div>
        )}
      </div>
    </div>
  );
}

function AskPanel({ documentId }) {  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState({}); // `${index}-${ordinal}` → bool

  const send = async () => {
    const question = input.trim();
    if (!question || busy) return;
    setInput("");
    setError("");
    setMessages((m) => [...m, { role: "user", text: question }]);
    setBusy(true);
    try {
      const data = await api.ask(documentId, question);
      setMessages((m) => [...m, { role: "assistant", text: data.answer, citations: data.citations }]);
    } catch (err) {
      setError(err.status === 502 ? "问答模型调用失败，请稍后重试" : err.message || "问答失败");
    } finally {
      setBusy(false);
    }
  };

  const renderAnswer = (message, index) => {
    const parts = message.text.split(/(\[条款 \d+\])/g);
    return parts.map((part, partIndex) => {
      const match = part.match(/^\[条款 (\d+)\]$/);
      if (!match) return <span key={partIndex}>{part}</span>;
      const ordinal = Number(match[1]);
      const citation = (message.citations || []).find((c) => c.ordinal === ordinal);
      const key = `${index}-${ordinal}`;
      const open = expanded[key];
      return (
        <span key={partIndex} className="inline">
          <button
            onClick={() => citation && setExpanded((e) => ({ ...e, [key]: !e[key] }))}
            className={`mx-0.5 inline-flex items-center whitespace-nowrap rounded border px-1.5 py-0.5 align-baseline text-[11px] font-medium transition-colors ${
              citation && open
                ? "border-accent bg-accent-tint text-accent"
                : citation
                  ? "border-line bg-canvas text-accent hover:border-accent"
                  : "border-line bg-canvas text-ink-3"
            }`}
            title={citation ? "点击查看条款原文" : undefined}
          >
            [条款 {ordinal}]
          </button>
          {open && citation && (
            <span className="block rounded-r border-l-[3px] border-accent bg-canvas px-3 py-2 my-1 text-[12px] italic leading-5 text-ink-2">
              {citation.heading ? `${citation.heading}：` : ""}
              {citation.quote}
            </span>
          )}
        </span>
      );
    });
  };

  return (
    <section className="rounded-lg border border-line bg-surface p-4 shadow-card">
      <div className="flex items-center gap-2">
        <Icon name="forum" className="text-[18px] text-accent" />
        <span className="text-[15px] font-semibold text-ink">文档追问</span>
        <span className="text-[12px] text-ink-3">仅基于本文档条款回答，回答标注条款出处</span>
      </div>

      {messages.length > 0 && (
        <div className="mt-3 space-y-3">
          {messages.map((message, index) =>
            message.role === "user" ? (
              <div key={index} className="flex justify-end">
                <div className="max-w-[70%] rounded-lg rounded-br-sm bg-accent px-3.5 py-2 text-[13px] text-white">
                  {message.text}
                </div>
              </div>
            ) : (
              <div key={index} className="flex justify-start">
                <div className="max-w-[85%] rounded-lg rounded-bl-sm border border-line bg-canvas px-3.5 py-2 text-[13px] leading-6 text-ink">
                  {renderAnswer(message, index)}
                </div>
              </div>
            ),
          )}
        </div>
      )}

      {error && <div className="mt-3"><ErrorBanner message={error} /></div>}

      <div className="mt-3 flex gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !e.nativeEvent.isComposing && send()}
          placeholder="例如：付款账期是怎么约定的？"
          className="h-9 flex-1 rounded-md border border-line-strong bg-surface px-3 text-[13px] outline-none focus:border-accent focus:ring-[3px] focus:ring-accent/10"
        />
        <PrimaryButton onClick={send} disabled={busy || !input.trim()}>
          {busy ? "思考中…" : "提问"}
        </PrimaryButton>
      </div>
    </section>
  );
}

export default function DocumentDetailPage({ documentId, user }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [deciding, setDeciding] = useState(null); // {verdict, decision}
  const [busy, setBusy] = useState(false);
  const [exporting, setExporting] = useState("");
  const [suggestMsg, setSuggestMsg] = useState("");

  const analyzeGaps = async () => {
    setSuggestMsg("");
    setBusy(true);
    try {
      const data = await api.generateSuggestions(documentId);
      setSuggestMsg(
        data.created > 0
          ? `代理起草了 ${data.created} 条新规则建议——去「规则手册」页确认后生效。`
          : "代理未发现值得新增的规则。"
      );
    } catch (err) {
      setSuggestMsg(err.message || "分析失败");
    } finally {
      setBusy(false);
    }
  };

  const load = useCallback(async () => {
    try {
      const detail = await api.document(documentId);
      setData(detail);
      setError("");
    } catch (err) {
      setError(err.message || "加载失败");
    }
  }, [documentId]);

  useEffect(() => {
    load();
  }, [load]);

  const processing = data?.document?.status === "parsing" || data?.document?.status === "scoring";
  useEffect(() => {
    if (!processing) return undefined;
    const timer = setInterval(load, 3000);
    return () => clearInterval(timer);
  }, [processing, load]);

  const grouped = useMemo(() => {
    if (!data) return [];
    const groups = new Map();
    for (const verdict of data.verdicts) {
      if (!groups.has(verdict.dimension)) groups.set(verdict.dimension, []);
      groups.get(verdict.dimension).push(verdict);
    }
    return [...groups.entries()];
  }, [data]);

  if (error && !data) return <ErrorBanner message={error} />;
  if (!data) return <Spinner />;

  const document_ = data.document;
  const scorecard = document_.scorecard || { counts: { red: 0, amber: 0, green: 0 } };
  const pending = data.verdicts.filter((v) => v.review_state === "awaiting_review").length;
  const finalized = document_.status === "finalized";
  const canReview = document_.status === "awaiting_review";

  const doFinalize = async () => {
    setBusy(true);
    setError("");
    try {
      await api.finalize(document_.id);
      await load();
    } catch (err) {
      setError(err.message || "定稿失败");
    } finally {
      setBusy(false);
    }
  };

  const doExport = async (format) => {
    setExporting(format);
    setError("");
    try {
      const blob = await api.export(document_.id, format);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${document_.friendly_id}.${format}`;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(err.message || "导出失败");
    } finally {
      setExporting("");
    }
  };

  return (
    <>
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <a
            href="#/"
            className="flex h-8 w-8 items-center justify-center rounded text-ink-2 transition-colors hover:bg-line-subtle hover:text-ink"
          >
            <Icon name="arrow_back" className="text-[20px]" />
          </a>
          <MonoId>{document_.friendly_id}</MonoId>
          <Chip tone="muted">{document_.playbook_id === "delivery-intake" ? "客户交付件风险分析" : "供应商合同合规"}</Chip>
          <h1 className="truncate text-[20px] font-semibold text-ink">{document_.title}</h1>
          <StatusChip status={document_.status} />
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {canReview && pending > 0 && (
            <span className="whitespace-nowrap text-[12px] text-ink-3">待签字 {pending} 项</span>
          )}
          {user?.role === "admin" && (document_.status === "awaiting_review" || finalized) && (
            <SecondaryButton onClick={analyzeGaps} disabled={busy}>
              <Icon name="lightbulb" className="text-[16px]" />
              {busy ? "分析中…" : "分析规则缺口"}
            </SecondaryButton>
          )}
          <SecondaryButton
            disabled={!finalized}
            title={finalized ? undefined : "红/黄判定需专家签字后才能导出"}
            onClick={() => doExport("docx")}
          >
            <Icon name="file_download" className="text-[16px]" />
            {exporting === "docx" ? "导出中…" : "导出报告"}
          </SecondaryButton>
          <PrimaryButton
            disabled={!canReview || pending > 0 || busy}
            title={
              finalized
                ? "文档已定稿"
                : pending > 0
                  ? `还有 ${pending} 项待签字`
                  : undefined
            }
            onClick={doFinalize}
          >
            <Icon name="check_circle" className="text-[16px]" />
            {finalized ? "已定稿" : "完成定稿"}
          </PrimaryButton>
        </div>
      </div>

      {suggestMsg && (
        <div className="flex items-center gap-2 rounded border border-accent/30 bg-accent-tint px-3 py-2 text-[13px] text-accent">
          <Icon name="lightbulb" className="text-[16px]" />
          {suggestMsg}
        </div>
      )}

      {error && <ErrorBanner message={error} />}
      {document_.status === "failed" && document_.status_reason && (
        <div className="flex items-center justify-between rounded border border-red-line bg-red-bg px-3 py-2 text-[13px] text-red-text">
          <span>处理失败：{document_.status_reason}</span>
          <TextAction
            onClick={async () => {
              try {
                await api.process(document_.id);
                load();
              } catch (err) {
                setError(err.message);
              }
            }}
          >
            重试
          </TextAction>
        </div>
      )}
      {processing && (
        <div className="flex items-center gap-2 rounded border border-line bg-surface px-3 py-2 text-[13px] text-ink-2">
          <Icon name="progress_activity" className="animate-spin text-[16px] text-accent" />
          引擎正在评分，页面会自动刷新…
        </div>
      )}

      <div className="flex items-center justify-between rounded-lg border border-line bg-surface px-5 py-3.5 shadow-card">
        <div className="flex items-baseline gap-1.5">
          <span className="text-[13px] text-ink-2">风险指数</span>
          <span className="tnum text-[24px] font-bold leading-7 text-ink">{scorecard.risk_index ?? 0}</span>
          <span className="text-[12px] text-ink-3">/100</span>
        </div>
        <div className="flex items-baseline gap-1.5">
          <span className="text-[13px] text-ink-2">条款覆盖率</span>
          <span className="tnum text-[16px] font-semibold text-ink">{scorecard.coverage_pct ?? 0}%</span>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-[13px] text-ink-2">审核分布</span>
          <Chip tone="red" dot>红 <span className="tnum font-semibold">{scorecard.counts.red}</span></Chip>
          <Chip tone="amber" dot>黄 <span className="tnum font-semibold">{scorecard.counts.amber}</span></Chip>
          <Chip tone="green" dot>绿 <span className="tnum font-semibold">{scorecard.counts.green}</span></Chip>
        </div>
      </div>

      {grouped.map(([dimension, verdicts]) => {
        const dimensionPending = verdicts.filter((v) => v.review_state === "awaiting_review").length;
        return (
          <section key={dimension}>
            <div className="mb-2.5 flex items-center justify-between">
              <div className="flex items-center gap-2">
                <span className="h-[16px] w-[3px] rounded bg-accent" />
                <span className="text-[15px] font-semibold text-ink">{dimension}</span>
                <span className="text-[12px] text-ink-3">{verdicts.length} 条</span>
              </div>
              <span className="text-[12px] text-ink-3">
                {dimensionPending > 0 ? `需专家签字确认 ${dimensionPending} 项` : "全部已就绪"}
              </span>
            </div>
            <div className="space-y-3">
              {verdicts.map((verdict) => (
                <VerdictCard
                  key={verdict.id}
                  verdict={verdict}
                  canReview={canReview}
                  onDecide={(v, decision) => setDeciding({ verdict: v, decision })}
                />
              ))}
            </div>
          </section>
        );
      })}

      {deciding && (
        <DecisionModal
          document_={document_}
          verdict={deciding.verdict}
          initialDecision={deciding.decision}
          onClose={() => setDeciding(null)}
          onDone={async () => {
            setDeciding(null);
            await load();
          }}
        />
      )}

      {(document_.status === "awaiting_review" || finalized) && (
        <AskPanel documentId={document_.id} />
      )}
    </>
  );
}
