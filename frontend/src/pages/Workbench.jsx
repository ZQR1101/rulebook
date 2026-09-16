import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, formatTime, STATUS_LABELS } from "../api.js";
import {
  Chip,
  ErrorBanner,
  Icon,
  Modal,
  MonoId,
  PrimaryButton,
  SecondaryButton,
  Spinner,
  StatusChip,
  RatingCounts,
} from "../ui.jsx";

const PLAYBOOK_META = {
  "contract-compliance": { short: "审查供应商合同" },
  "delivery-intake": { short: "分析客户 SOW 与服务请求" },
};

function PlaybookCard({ spec, stats, active, onSelect }) {
  const short = PLAYBOOK_META[spec.id]?.short || spec.description.slice(0, 12);
  return (
    <button
      onClick={() => onSelect(spec.id)}
      className={`flex-1 rounded-lg border bg-surface p-5 text-left transition-colors ${
        active
          ? "border-[2px] border-accent shadow-card"
          : "border-line hover:border-line-strong"
      }`}
    >
      <div className="flex items-start justify-between">
        <span className={`text-[16px] font-semibold ${active ? "text-accent" : "text-ink"}`}>
          {spec.name}
        </span>
        {active && <Icon name="check" className="text-[20px] text-accent" />}
      </div>
      <div className="mt-1.5 text-[13px] text-ink-2">
        {spec.rule_count} 条规则 × {spec.dimensions.length} 维度 · {short}
      </div>
      <div className="mt-2.5 whitespace-nowrap text-[12px] text-ink-3">
        文档 <span className="tnum">{stats.total}</span> · 待签字{" "}
        <span className="tnum">{stats.awaiting}</span> · 红灯{" "}
        <span className="tnum">{stats.red}</span>
      </div>
    </button>
  );
}

function UploadModal({ playbookId, onClose, onUploaded }) {
  const [file, setFile] = useState(null);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState("");
  const [duplicate, setDuplicate] = useState(null);
  const [uploading, setUploading] = useState(false);
  const inputRef = useRef(null);

  const submit = async () => {
    if (!file) return;
    setError("");
    setDuplicate(null);
    setUploading(true);
    try {
      const data = await api.upload(file, playbookId);
      onUploaded(data);
    } catch (err) {
      if (err.status === 409 && err.detail?.friendly_id) {
        setDuplicate(err.detail);
      } else {
        setError(err.message || "上传失败");
      }
    } finally {
      setUploading(false);
    }
  };

  return (
    <Modal title="上传文档" onClose={onClose}>
      <div
        onClick={() => inputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          const dropped = e.dataTransfer.files?.[0];
          if (dropped) setFile(dropped);
        }}
        className={`flex cursor-pointer flex-col items-center justify-center rounded-lg border-[1.5px] border-dashed px-6 py-10 text-center transition-colors ${
          dragging ? "border-accent bg-accent-tint" : "border-line-strong hover:border-accent"
        }`}
      >
        <Icon name="upload_file" className="text-[32px] text-ink-3" />
        <div className="mt-2 text-[14px] font-medium text-ink">
          {file ? file.name : "点击选择或拖入文件"}
        </div>
        <div className="mt-1 text-[12px] text-ink-3">支持 PDF / Word / Markdown / TXT，≤ 20MB</div>
        <input
          ref={inputRef}
          type="file"
          accept=".pdf,.docx,.md,.txt"
          className="hidden"
          onChange={(e) => setFile(e.target.files?.[0] || null)}
        />
      </div>

      <div className="mt-4 flex items-center gap-2 rounded border border-line bg-canvas px-3 py-2 text-[12px] text-ink-2">
        <Icon name="info" className="text-[15px] text-ink-3" />
        相同内容的文档会被自动去重；上传后引擎自动开始逐条评分。
      </div>

      {error && <div className="mt-3"><ErrorBanner message={error} /></div>}
      {duplicate && (
        <div className="mt-3 flex items-center justify-between rounded border border-amber-line bg-amber-bg px-3 py-2 text-[13px] text-amber-text">
          <span>
            相同内容的文档已存在：{duplicate.friendly_id}（内容哈希去重）
          </span>
          <a
            href={`#/documents/${duplicate.document_id}`}
            onClick={onClose}
            className="whitespace-nowrap font-semibold underline"
          >
            打开已有文档 →
          </a>
        </div>
      )}

      <div className="mt-5 flex justify-end gap-2">
        <SecondaryButton onClick={onClose}>取消</SecondaryButton>
        <PrimaryButton onClick={submit} disabled={!file || uploading}>
          {uploading ? "上传中…" : "上传并开始审查"}
        </PrimaryButton>
      </div>
    </Modal>
  );
}

export default function WorkbenchPage() {
  const [playbooks, setPlaybooks] = useState([]);
  const [documents, setDocuments] = useState([]);
  const [activePlaybook, setActivePlaybook] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [showUpload, setShowUpload] = useState(false);
  const pollRef = useRef(null);

  const load = useCallback(async () => {
    try {
      const [pb, docs] = await Promise.all([api.playbooks(), api.documents()]);
      setPlaybooks(pb.playbooks);
      setDocuments(docs.documents);
      setActivePlaybook((current) => current || pb.playbooks[0]?.id || null);
      setError("");
    } catch (err) {
      setError(err.message || "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Poll while any document is being processed.
  const processing = documents.some((d) => d.status === "parsing" || d.status === "scoring");
  useEffect(() => {
    if (!processing) return undefined;
    pollRef.current = setInterval(load, 3000);
    return () => clearInterval(pollRef.current);
  }, [processing, load]);

  const byPlaybook = useMemo(
    () => documents.filter((d) => d.playbook_id === activePlaybook),
    [documents, activePlaybook],
  );

  const statsFor = useCallback(
    (playbookId) => {
      const rows = documents.filter((d) => d.playbook_id === playbookId);
      return {
        total: rows.length,
        awaiting: rows.filter((d) => d.status === "awaiting_review").length,
        red: rows.reduce((sum, d) => sum + (d.scorecard?.counts?.red || 0), 0),
      };
    },
    [documents],
  );

  if (loading) return <Spinner />;
  if (error && !playbooks.length) return <ErrorBanner message={error} />;

  return (
    <>
      <div className="flex items-center justify-between">
        <h1 className="text-[20px] font-semibold text-ink">工作台</h1>
        <PrimaryButton onClick={() => setShowUpload(true)}>
          <Icon name="add" className="text-[18px]" />
          <span className="hidden sm:inline">上传文档</span>
        </PrimaryButton>
      </div>

      <div className="flex flex-col gap-4 sm:flex-row sm:gap-5">
        {playbooks.map((spec) => (
          <PlaybookCard
            key={spec.id}
            spec={spec}
            stats={statsFor(spec.id)}
            active={spec.id === activePlaybook}
            onSelect={setActivePlaybook}
          />
        ))}
      </div>

      {error && <ErrorBanner message={error} />}

      <div className="overflow-x-auto rounded-lg border border-line bg-surface shadow-card">
        <table className="w-full min-w-[720px]">
          <thead>
            <tr className="border-b border-line-strong bg-canvas text-left text-[12px] font-medium uppercase tracking-wider text-ink-2">
              <th className="h-9 px-4 font-medium">编号</th>
              <th className="h-9 px-4 font-medium">标题</th>
              <th className="h-9 px-4 font-medium">状态</th>
              <th className="h-9 px-4 font-medium">红黄绿</th>
              <th className="h-9 px-4 text-right font-medium">覆盖率</th>
              <th className="h-9 px-4 text-right font-medium">更新时间</th>
            </tr>
          </thead>
          <tbody>
            {byPlaybook.map((doc) => (
              <tr
                key={doc.id}
                className="cursor-pointer border-b border-line-subtle transition-colors last:border-0 hover:bg-canvas"
                onClick={() => (window.location.hash = `#/documents/${doc.id}`)}
              >
                <td className="h-11 px-4">
                  <MonoId>{doc.friendly_id}</MonoId>
                </td>
                <td className="max-w-[420px] truncate px-4 text-[14px] font-medium text-ink">
                  {doc.title}
                </td>
                <td className="whitespace-nowrap px-4">
                  <div className="flex items-center gap-2">
                    <StatusChip status={doc.status} />
                    {doc.status === "failed" && doc.status_reason && (
                      <span className="max-w-[240px] truncate text-[12px] text-ink-3">
                        {doc.status_reason}
                      </span>
                    )}
                  </div>
                </td>
                <td className="whitespace-nowrap px-4">
                  {doc.scorecard ? (
                    <RatingCounts
                      red={doc.scorecard.counts.red}
                      amber={doc.scorecard.counts.amber}
                      green={doc.scorecard.counts.green}
                    />
                  ) : (
                    <span className="text-[12px] text-ink-3">—</span>
                  )}
                </td>
                <td className="tnum whitespace-nowrap px-4 text-right text-[13px] text-ink-2">
                  {doc.scorecard ? `${doc.scorecard.coverage_pct}%` : "—"}
                </td>
                <td className="tnum whitespace-nowrap px-4 text-right text-[13px] text-ink-3">
                  {formatTime(doc.updated_at)}
                </td>
              </tr>
            ))}
            {!byPlaybook.length && (
              <tr>
                <td colSpan={6} className="py-16 text-center text-[13px] text-ink-3">
                  还没有文档——上传第一份文档开始审查
                </td>
              </tr>
            )}
          </tbody>
        </table>
        <div className="border-t border-line-subtle px-4 py-2.5 text-[12px] text-ink-3">
          共 {byPlaybook.length} 份文档
        </div>
      </div>

      {showUpload && (
        <UploadModal
          playbookId={activePlaybook}
          onClose={() => setShowUpload(false)}
          onUploaded={() => {
            setShowUpload(false);
            load();
          }}
        />
      )}
    </>
  );
}
