import React, { useCallback, useEffect, useState } from "react";
import { api, RATING_LABELS } from "../api.js";
import {
  Chip,
  ErrorBanner,
  Icon,
  MonoId,
  SecondaryButton,
  Spinner,
  TextAction,
} from "../ui.jsx";

export default function ReviewQueuePage() {
  const [items, setItems] = useState(null);
  const [filter, setFilter] = useState("all");
  const [error, setError] = useState("");
  const [deciding, setDeciding] = useState(null); // {item, decision}
  const [note, setNote] = useState("");
  const [newRating, setNewRating] = useState("green");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const data = await api.queue();
      setItems(data.items);
      setError("");
    } catch (err) {
      setError(err.message || "加载失败");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const submitDecision = async () => {
    if (deciding.decision === "reject" && !note.trim()) {
      setError("驳回必须填写专家意见");
      return;
    }
    setBusy(true);
    try {
      await api.decide(deciding.item.document_id, deciding.item.verdict_id, {
        decision: deciding.decision,
        expert_note: note.trim() || null,
        new_rating: deciding.decision === "edit" ? newRating : null,
      });
      setDeciding(null);
      setNote("");
      await load();
    } catch (err) {
      setError(err.message || "操作失败");
    } finally {
      setBusy(false);
    }
  };

  if (!items && !error) return <Spinner />;
  const filtered = (items || []).filter((item) => {
    if (filter === "red") return item.rating === "red";
    if (filter === "amber") return item.rating === "amber";
    return true;
  });
  const redCount = (items || []).filter((i) => i.rating === "red").length;
  const amberCount = (items || []).filter((i) => i.rating === "amber").length;

  return (
    <>
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <h1 className="text-[20px] font-semibold text-ink">审批队列</h1>
          {items && items.length > 0 && <Chip tone="red">{items.length} 待决</Chip>}
        </div>
        <div className="flex items-center gap-2 whitespace-nowrap text-[12px] text-ink-3">
          <Icon name="sort" className="text-[15px]" />
          按风险级别自高向低排列
        </div>
      </div>
      <p className="-mt-4 text-[13px] text-ink-3">红灯置顶 · 全部处理后才能定稿导出</p>

      {error && <ErrorBanner message={error} />}

      <div className="flex flex-wrap gap-2">
        {[
          ["all", `全部 ${items?.length ?? 0}`],
          ["red", `红灯 ${redCount}`],
          ["amber", `黄灯 ${amberCount}`],
        ].map(([key, label]) => (
          <button
            key={key}
            onClick={() => setFilter(key)}
            className={`whitespace-nowrap rounded border px-3 py-1.5 text-[13px] font-medium transition-colors ${
              filter === key
                ? "border-accent bg-accent text-white"
                : "border-line bg-surface text-ink-2 hover:border-line-strong"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      <div className="space-y-3">
        {filtered.map((item) => (
          <div key={item.verdict_id} className="rounded-lg border border-line bg-surface p-4 shadow-card">
            <div className="flex items-center justify-between gap-3">
              <div className="flex min-w-0 items-center gap-2.5">
                <Chip tone={item.rating} dot>
                  {RATING_LABELS[item.rating]}灯
                </Chip>
                <span className="truncate text-[14px] font-semibold text-ink">{item.rule_name}</span>
                <a
                  href={`#/documents/${item.document_id}`}
                  className="flex shrink-0 items-center gap-0.5 text-[12px] text-accent hover:underline"
                >
                  <MonoId>{item.friendly_id}</MonoId>
                  <Icon name="arrow_forward" className="text-[14px]" />
                </a>
              </div>
              <div className="flex shrink-0 items-center">
                <TextAction
                  onClick={() => {
                    setDeciding({ item, decision: "approve" });
                    setNote("");
                    setError("");
                  }}
                >
                  批准
                </TextAction>
                <TextAction
                  onClick={() => {
                    setDeciding({ item, decision: "edit" });
                    setNote("");
                    setNewRating("green");
                    setError("");
                  }}
                >
                  改判
                </TextAction>
                <TextAction
                  tone="red"
                  onClick={() => {
                    setDeciding({ item, decision: "reject" });
                    setNote("");
                    setError("");
                  }}
                >
                  驳回
                </TextAction>
              </div>
            </div>
            <p className="mt-2 text-[13px] leading-5 text-ink-2">{item.rationale}</p>
            {item.gap_reason && (
              <div className="mt-2 inline-flex items-center gap-1.5 rounded border border-amber-line bg-amber-bg px-2.5 py-1 text-[12px] text-amber-text">
                <Icon name="warning" className="text-[14px]" />
                缺口：{item.gap_reason}
              </div>
            )}
          </div>
        ))}

        {items && items.length > 0 && filtered.length === 0 && (
          <div className="rounded-lg border border-line bg-surface py-14 text-center text-[13px] text-ink-3">
            该筛选条件下暂无待办
          </div>
        )}

        {items && items.length === 0 && (
          <div className="flex items-center justify-between rounded-lg border border-green-line bg-green-bg px-4 py-3.5 text-[14px] font-medium text-green-text">
            <span className="flex items-center gap-2">
              <Icon name="task_alt" className="text-[20px]" />
              队列已清空 — 可以去工作台定稿导出
            </span>
            <a href="#/" className="flex items-center gap-1 whitespace-nowrap text-[13px] font-semibold hover:underline">
              返回工作台
              <Icon name="arrow_forward" className="text-[15px]" />
            </a>
          </div>
        )}
      </div>

      {deciding && (
        <div
          className="fixed inset-0 z-[60] flex items-center justify-center bg-[rgba(33,37,41,0.4)] p-6"
          onClick={() => setDeciding(null)}
        >
          <div
            className="w-full max-w-md rounded-lg bg-surface p-5 shadow-modal"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center gap-2">
              <Chip tone={deciding.item.rating} dot>
                {RATING_LABELS[deciding.item.rating]}灯
              </Chip>
              <span className="text-[15px] font-semibold text-ink">{deciding.item.rule_name}</span>
            </div>
            <div className="mt-3 flex items-center gap-2">
              <label className="text-[13px] font-medium text-ink">
                {deciding.decision === "reject" ? "专家意见（必填）" : "专家意见（选填）"}
              </label>
              {deciding.decision === "edit" && (
                <div className="ml-auto flex gap-1.5">
                  {["red", "amber", "green"].map((r) => (
                    <button
                      key={r}
                      onClick={() => setNewRating(r)}
                      className={`rounded border px-2.5 py-1 text-[12px] font-medium transition-colors ${
                        newRating === r
                          ? "border-accent bg-accent-tint text-accent"
                          : "border-line text-ink-2"
                      }`}
                    >
                      改判为{RATING_LABELS[r]}
                    </button>
                  ))}
                </div>
              )}
            </div>
            <textarea
              value={note}
              onChange={(e) => setNote(e.target.value)}
              rows={2}
              className="mt-2 w-full rounded-md border border-line-strong px-3 py-2 text-[13px] outline-none focus:border-accent focus:ring-[3px] focus:ring-accent/10"
            />
            <div className="mt-4 flex justify-end gap-2">
              <SecondaryButton onClick={() => setDeciding(null)}>取消</SecondaryButton>
              <button
                onClick={submitDecision}
                disabled={busy}
                className={`whitespace-nowrap rounded-md px-3 py-1.5 text-[13px] font-semibold text-white transition-colors disabled:opacity-50 ${
                  deciding.decision === "reject" ? "bg-red-text hover:bg-red-text/90" : "bg-accent hover:bg-accent-hover"
                }`}
              >
                {deciding.decision === "approve"
                  ? "确认批准"
                  : deciding.decision === "edit"
                    ? "确认改判"
                    : "确认驳回"}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
