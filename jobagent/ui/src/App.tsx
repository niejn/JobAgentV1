import { useEffect, useState, type ClipboardEvent, type DragEvent, type FormEvent, type ReactNode } from "react";

type Journey = { deleted_at?: string | null; id: string; company: string; role: string; department: string; recruiting_cycle: string; stage: string; job_description: string; task_count: number; artifact_count: number; updated_at: string };
type Conversation = { id: string; journey_id: string; title: string; updated_at: string; last_message?: string };
type Message = { id?: number; role: "user" | "assistant"; content: string };
type MatchOpinion = { score: number | null; evaluation_failed: boolean; reasoning: string[]; matched_skills?: string[]; missing_skills?: string[] };
type View = "officeAi" | "officeSkills" | "assistant" | "journeys";
type SkillCard = { id: string; name: string; title: string; summary: string; guidance: string; version: string; source: string; installed: boolean; has_icon: boolean };
type ResumeItem = { name: string; size_bytes: number; updated_at: string };
type PlaceholderKind = "profile" | "quota";
type IconName = "add" | "store" | "assistant" | "journeys" | "attachment" | "skills" | "send" | "settings" | "slides" | "document" | "data" | "design" | "research" | "pdf";

function AppIcon({ name }: { name: IconName }) {
  const common = { fill: "none", stroke: "currentColor", strokeWidth: 1.8, strokeLinecap: "round" as const, strokeLinejoin: "round" as const };
  const paths: Record<IconName, ReactNode> = {
    add: <><path {...common} d="M12 5v14M5 12h14" /></>,
    store: <><rect {...common} x="4" y="4" width="16" height="16" rx="3" /><path {...common} d="M8 8h.01M16 8h.01M8 16h8" /></>,
    assistant: <><path {...common} d="m12 3 1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3Z" /><path {...common} d="m19 16 .7 2.3L22 19l-2.3.7L19 22l-.7-2.3L16 19l2.3-.7L19 16Z" /></>,
    journeys: <><rect {...common} x="4" y="7" width="16" height="12" rx="2" /><path {...common} d="M9 7V5h6v2M4 11h16M10 14h4" /></>,
    attachment: <path {...common} d="m8.5 12.5 5.9-5.9a3 3 0 1 1 4.2 4.2l-7.3 7.3a4.5 4.5 0 1 1-6.4-6.4l7.1-7.1" />,
    skills: <><path {...common} d="M13 2 6 14h5l-1 8 7-12h-5l1-8Z" /></>,
    send: <><path {...common} d="m21 3-7.2 18-3.4-7.4L3 10.2 21 3Z" /><path {...common} d="m10.4 13.6 4-4" /></>,
    settings: <><circle {...common} cx="12" cy="12" r="3" /><path {...common} d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.1 2.1-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.5v.2h-3v-.2a1.7 1.7 0 0 0-1-1.5 1.7 1.7 0 0 0-1.9.3l-.1.1-2.1-2.1.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.5-1H5.4v-3h.2a1.7 1.7 0 0 0 1.5-1 1.7 1.7 0 0 0-.3-1.9l-.1-.1 2.1-2.1.1.1a1.7 1.7 0 0 0 1.9.3 1.7 1.7 0 0 0 1-1.5v-.2h3v.2a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.9-.3l.1-.1 2.1 2.1-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.5 1h.2v3h-.2a1.7 1.7 0 0 0-1.5 1Z" /></>,
    slides: <><rect {...common} x="3" y="4" width="18" height="14" rx="2" /><path {...common} d="M8 21h8M12 18v3M7 13l3-3 2 2 3-4" /></>,
    document: <><path {...common} d="M7 3h7l4 4v14H7z" /><path {...common} d="M14 3v5h5M10 12h5M10 16h5" /></>,
    data: <><path {...common} d="M4 20V4M4 20h16" /><path {...common} d="m7 16 4-5 3 2 4-6" /><circle {...common} cx="7" cy="16" r=".7" /><circle {...common} cx="11" cy="11" r=".7" /><circle {...common} cx="14" cy="13" r=".7" /><circle {...common} cx="18" cy="7" r=".7" /></>,
    design: <><path {...common} d="M12 3a9 9 0 1 0 0 18h1.4a1.6 1.6 0 0 0 .6-3.1 1.6 1.6 0 0 1 .6-3.1H17a4 4 0 0 0 4-4c0-4.3-4-7.7-9-7.7Z" /><circle {...common} cx="7.5" cy="11" r=".7" /><circle {...common} cx="10" cy="7.5" r=".7" /><circle {...common} cx="14" cy="7.5" r=".7" /></>,
    research: <><circle {...common} cx="10.5" cy="10.5" r="5.5" /><path {...common} d="m15 15 4 4M5 10.5h11M10.5 5a9 9 0 0 1 0 11" /></>,
    pdf: <><path {...common} d="M7 3h7l4 4v14H7z" /><path {...common} d="M14 3v5h5M9.5 15h5M9.5 18h3" /></>,
  };
  return <svg className="app-icon" viewBox="0 0 24 24" aria-hidden="true">{paths[name]}</svg>;
}


async function api<T>(url: string, options?: RequestInit): Promise<T> { const response = await fetch(url, { headers: { "Content-Type": "application/json" }, ...options }); if (!response.ok) throw new Error(await response.text() || `HTTP ${response.status}`); return response.json(); }
function Metric({ label, value, hint }: { label: string; value: string; hint: string }) { return <div className="metric"><div className="metric-label">{label}</div><div className="metric-number">{value}</div><div className="metric-change">{hint}</div></div>; }
function AssistantText({ content }: { content: string }) { const parts = content.split(/(初步匹配度：\d+%|工作地点默认填为[^，。]+|招聘周期默认填为[^。]+)/g); return <>{parts.map((part, index) => /^(初步匹配度：|工作地点默认填为|招聘周期默认填为)/.test(part) ? <strong key={index}>{part}</strong> : part)}</>; }

function JourneyDetail({ journey, onBack }: { journey: Journey; onBack: () => void }) { const [conversations, setConversations] = useState<Conversation[]>([]); const [active, setActive] = useState<Conversation | null>(null); const [messages, setMessages] = useState<Message[]>([]); const [draft, setDraft] = useState(""); const [busy, setBusy] = useState(false); useEffect(() => { api<Conversation[]>(`/api/journeys/${journey.id}/conversations`).then(setConversations).catch(() => setConversations([])); }, [journey.id]); async function open(item: Conversation) { setActive(item); const data = await api<{ messages: Message[] }>(`/api/conversations/${item.id}`); setMessages(data.messages); } async function create() { const item = await api<Conversation>(`/api/journeys/${journey.id}/conversations`, { method: "POST", body: JSON.stringify({ title: `${journey.company} · ${journey.role}` }) }); setConversations(items => [item, ...items]); await open(item); } async function removeSelected() { if (!selected.length || removing) return; setRemoving(true); try { const ids = selected; await api("/api/assistant/conversations", { method: "DELETE", body: JSON.stringify({ ids }) }); setSelected([]); setEditing(false); if (active && ids.includes(active.id)) { setActive(null); setMessages([]); } setLoadingMore(false); await loadPage(0); } finally { setRemoving(false); } } async function send(event: FormEvent) { event.preventDefault(); if (!active || !draft.trim() || busy) return; const text = draft.trim(); setDraft(""); setMessages(items => [...items, { role: "user", content: text }]); setBusy(true); try { const reply = await api<Message>(`/api/conversations/${active.id}/messages`, { method: "POST", body: JSON.stringify({ content: text }) }); setMessages(items => [...items, reply]); } catch (error) { setMessages(items => [...items, { role: "assistant", content: `发送失败：${error instanceof Error ? error.message : "未知错误"}` }]); } finally { setBusy(false); } } return <div><button className="back-link" onClick={onBack}>← 返回求职旅程</button><div className="detail-header"><div><p className="eyebrow">OPPORTUNITY JOURNEY</p><h1>{journey.company} · {journey.role}</h1><p className="muted">{journey.department || "未填写部门"} · {journey.recruiting_cycle || "未填写招聘周期"}</p></div><span className="tag">{journey.stage}</span></div><div className="detail-grid"><section className="card detail-card"><div className="card-head"><h2>Journey 上下文</h2><span className="muted">{journey.task_count} Tasks · {journey.artifact_count} Artifacts</span></div><h3>岗位描述</h3><p className="description">{journey.job_description}</p><h3>可执行任务</h3><div className="task-list"><button>⌕ 岗位与公司调研 <span>启动 →</span></button><button>▤ 简历匹配分析 <span>启动 →</span></button><button>✦ Mock Interview <span>启动 →</span></button></div></section><section className="card chat-card"><div className="card-head"><h2>Journey 对话</h2><button className="btn primary small" onClick={create}>＋ 新建对话</button></div><div className="conversation-layout"><aside className="conversation-list"><p className="section-label">历史对话</p>{conversations.length === 0 && <p className="empty">暂无历史对话</p>}{conversations.map(item => <button className={`conversation-item ${active?.id === item.id ? "selected" : ""}`} key={item.id} onClick={() => open(item)}><strong>{item.title}</strong><span>{item.updated_at}</span></button>)}</aside><div className="chat-pane">{!active ? <div className="empty chat-empty"><div className="big-icon">◌</div><h3>选择或新建一个对话</h3><p>Agent 会读取这个 Journey 已接受的产物，并在需要时启动任务。</p></div> : <><div className="messages">{messages.length === 0 && <div className="assistant-bubble">你好，我已经准备好围绕这个 Journey 工作。你可以让我调研岗位、分析匹配度或开始 Mock Interview。</div>}{messages.map((message, index) => <div className={message.role === "user" ? "user-bubble" : "assistant-bubble"} key={message.id ?? index}>{message.content}</div>)}{busy && <div className="assistant-bubble typing">Agent 思考中…</div>}</div><form className="chat-input" onSubmit={send}><input value={draft} onChange={event => setDraft(event.target.value)} placeholder="告诉 Agent 下一步做什么…" /><button className="btn primary" disabled={busy}>发送</button></form></>}</div></div></section></div></div>; }

function JourneyList({ journeys, onOpen, onCreate }: { journeys: Journey[]; onOpen: (journey: Journey) => void; onCreate: () => void }) { return <><div className="welcome"><div><p className="eyebrow">OPPORTUNITY JOURNEY · 本周视图</p><h1>早上好，聂俊能 👋</h1><p className="muted">调研、匹配和 Mock Interview 都会沉淀在对应 Journey 中。</p></div><button className="btn primary" onClick={onCreate}>＋ 创建新 Journey</button></div><div className="metrics"><Metric label="进行中的 Journey" value={String(journeys.length)} hint="按公司、部门、职位和周期归档" /><Metric label="运行中任务" value="04" hint="2 个等待输入" /><Metric label="已接受产物" value="28" hint="↑ 8 个 · 本周" /><Metric label="待确认动作" value="03" hint="外部写操作需确认" /></div><div className="card"><div className="card-head"><h2>我的机会 Journey</h2><span className="muted">点击卡片打开详情与对话</span></div><div className="journeys">{journeys.map(journey => <button className="journey-card" key={journey.id} onClick={() => onOpen(journey)}><div className="company-logo">{journey.company.slice(0, 1)}</div><div className="journey-main"><strong>{journey.company}</strong><p>{journey.role}</p><div className="journey-meta"><span className="tag">{journey.department || "未填写部门"}</span><span>{journey.recruiting_cycle || "未填写周期"}</span><span>{journey.task_count} 个任务</span><span>{journey.artifact_count} 个产物</span></div></div><span className="time">{journey.updated_at}　→</span></button>)}</div></div></>; }

function AssistantView({ initialPrompt, onConsumedPrompt }: { initialPrompt?: string; onConsumedPrompt?: () => void }) { const [items, setItems] = useState<Conversation[]>([]); const [active, setActive] = useState<Conversation | null>(null); const [messages, setMessages] = useState<Message[]>([]); const [draft, setDraft] = useState(initialPrompt ?? ""); const [busy, setBusy] = useState(false); const [offset, setOffset] = useState(0); const [hasMore, setHasMore] = useState(true); const [loadingMore, setLoadingMore] = useState(false); const [editing, setEditing] = useState(false); const [selected, setSelected] = useState<string[]>([]); const [removing, setRemoving] = useState(false); async function loadPage(nextOffset: number, append = false) { if (loadingMore || (!hasMore && append)) return; setLoadingMore(true); try { const page = await api<Conversation[]>(`/api/assistant/conversations?limit=10&offset=${nextOffset}`); setItems(current => append ? [...current, ...page] : page); setOffset(current => current + page.length); setHasMore(page.length === 10); } finally { setLoadingMore(false); } } useEffect(() => { void loadPage(0); }, []); async function select(item: Conversation) { setActive(item); const result = await api<{ messages: Message[] }>(`/api/assistant/conversations/${item.id}`); setMessages(result.messages); } async function create() { const item = await api<Conversation>("/api/assistant/conversations", { method: "POST", body: JSON.stringify({ title: "新求职对话" }) }); setItems(current => [item, ...current]); setOffset(current => current + 1); await select(item); return item; } async function deliver(conversationId: string, content: string) { if (busy) return; setDraft(""); setMessages(current => [...current, { role: "user", content }]); setBusy(true); try { const reply = await api<Message>(`/api/assistant/conversations/${conversationId}/messages`, { method: "POST", body: JSON.stringify({ content }) }); setMessages(current => [...current, reply]); } catch (error) { setMessages(current => [...current, { role: "assistant", content: `发送失败：${error instanceof Error ? error.message : "未知错误"}` }]); } finally { setBusy(false); } } useEffect(() => { const content = initialPrompt?.trim(); if (!content) return; onConsumedPrompt?.(); void (async () => { let id = active?.id; if (!id) { const item = await api<Conversation>("/api/assistant/conversations", { method: "POST", body: JSON.stringify({ title: "新求职对话" }) }); setItems(current => [item, ...current]); setOffset(current => current + 1); setActive(item); id = item.id; } await deliver(id, content); })(); }, [initialPrompt]); async function removeSelected() { if (!selected.length || removing) return; setRemoving(true); try { const ids = selected; await api("/api/assistant/conversations", { method: "DELETE", body: JSON.stringify({ ids }) }); setSelected([]); setEditing(false); if (active && ids.includes(active.id)) { setActive(null); setMessages([]); } setLoadingMore(false); await loadPage(0); } finally { setRemoving(false); } } async function send(event: FormEvent) { event.preventDefault(); if (!active || !draft.trim() || busy) return; await deliver(active.id, draft.trim()); } return <div className="assistant-view"><div className="assistant-toolbar"><div><p className="eyebrow">GLOBAL JOB ASSISTANT</p><h1>求职助手</h1><p className="muted">管理求职目标、创建 Journey，并处理跨岗位问题。</p></div><div className="assistant-toolbar-actions"><button className="btn ghost" disabled={removing} title={editing ? (selected.length ? "删除所选历史对话" : "结束编辑模式") : "批量管理历史对话"} onClick={() => { if (editing && selected.length) { void removeSelected(); } else { setEditing(value => !value); setSelected([]); } }}>{editing && selected.length ? `删除所选（${selected.length}）` : editing ? "完成编辑" : "编辑历史对话"}</button><button className="btn primary" onClick={create}>＋ 新建对话</button></div></div><div className="assistant-workspace"><aside className="assistant-history" onScroll={event => { const target = event.currentTarget; if (target.scrollTop + target.clientHeight >= target.scrollHeight - 40) void loadPage(offset, true); }}><p className="section-label">历史对话</p>{items.length === 0 && !loadingMore && <p className="empty">暂无历史对话</p>}{items.map(item => <button className={`conversation-item ${editing ? "selecting" : ""} ${active?.id === item.id ? "selected" : ""} ${editing && selected.includes(item.id) ? "checked" : ""}`} key={item.id} onClick={() => editing ? setSelected(current => current.includes(item.id) ? current.filter(id => id !== item.id) : [...current, item.id]) : select(item)}><span className="item-title-row"><span className="item-check-slot">{editing && <input type="checkbox" className="item-check" checked={selected.includes(item.id)} onClick={event => event.stopPropagation()} onChange={() => setSelected(current => current.includes(item.id) ? current.filter(id => id !== item.id) : [...current, item.id])} aria-label="选择会话" />}</span><strong className="item-title" title={item.last_message || item.title}>{(() => { const label = item.last_message || item.title; return label.length > 200 ? `${label.slice(0, 200)}…` : label; })()}</strong></span><span className="item-time">{item.updated_at}</span></button>)}{loadingMore && <p className="empty">加载中…</p>}{!hasMore && items.length > 0 && <p className="empty">已加载全部对话</p>}</aside><section className="assistant-chat">{!active ? <div className="chat-empty empty"><div className="big-icon">✦</div><h3>开始一个求职对话</h3><p>可以咨询求职方向、创建 Journey 或管理多个岗位。</p><button className="btn primary" onClick={create}>新建对话</button></div> : <><div className="messages">{messages.length === 0 && <div className="assistant-bubble">你好，我是你的求职助手。可以帮你创建和管理多个岗位 Journey。</div>}{messages.map((message, index) => <div className={message.role === "user" ? "user-bubble" : "assistant-bubble"} key={message.id ?? index}>{message.content}</div>)}{busy && <div className="assistant-bubble typing">Agent 思考中…</div>}</div><form className="chat-input" onSubmit={send}><input value={draft} onChange={event => setDraft(event.target.value)} placeholder="告诉求职助手你想做什么…" /><button className="btn primary" disabled={busy}>发送</button></form></>}</section></div></div>; }

type JourneyDraft = { company: string; role: string; department: string; cycle: string; description: string; location: string; salary: string };
type DraftResult = { draft: JourneyDraft; message: string; missing_fields: string[]; method: string };
type SavedJourneyDraft = JourneyDraft & { id: string; updated_at: string };
const JOURNEY_DRAFT_STORAGE = "jobagent:journey-draft";

const today = new Date().toISOString().slice(0, 10);
const emptyDraft: JourneyDraft = { company: "", role: "", department: "", cycle: today, description: "", location: "", salary: "" };

function NewJourney({ onClose, onSaveDraft, initialDraft, onCreated }: { onClose: () => void; onSaveDraft?: (draft: JourneyDraft) => void; initialDraft?: SavedJourneyDraft; onCreated: (journey: Journey) => void }) {
  const [draft, setDraft] = useState<JourneyDraft>(() => { if (initialDraft) return { ...initialDraft }; try { const saved = localStorage.getItem(JOURNEY_DRAFT_STORAGE); return saved ? { ...emptyDraft, ...JSON.parse(saved) } : { ...emptyDraft }; } catch { return { ...emptyDraft }; } });
  const [messages, setMessages] = useState<Message[]>([{ role: "assistant", content: "把岗位 JD 粘贴给我吧。我会自动提取公司、岗位和关键信息，缺少的内容再向你确认。" }]);
  const [input, setInput] = useState("");
  const [step, setStep] = useState<"input" | "questions" | "confirm">("input");
  const [busy, setBusy] = useState(false);
  const [ocrBusy, setOcrBusy] = useState(false);
  const [imageName, setImageName] = useState("");
  const [dragging, setDragging] = useState(false);
  const [matchOpinion, setMatchOpinion] = useState<MatchOpinion | null>(null);
  const [error, setError] = useState("");
  function closeAndSave() { if (draft.description.trim() || draft.company.trim() || draft.role.trim()) onSaveDraft?.(draft); onClose(); }
  useEffect(() => { if (draft.description.trim() || draft.company.trim() || draft.role.trim()) localStorage.setItem(JOURNEY_DRAFT_STORAGE, JSON.stringify(draft)); }, [draft]);
  const missing = [!draft.company && "公司名称", !draft.role && "岗位名称"].filter(Boolean) as string[];
  function update(key: keyof JourneyDraft, value: string) { setDraft(current => ({ ...current, [key]: value })); setMatchOpinion(null); }
  async function evaluateMatch(next: JourneyDraft) {
    if (!next.company || !next.role || !next.description) return;
    try { const result = await api<MatchOpinion>("/api/journey-drafts/match", { method: "POST", body: JSON.stringify({ company: next.company, role: next.role, description: next.description, location: next.location }) }); setMatchOpinion(result); setMessages(current => [...current, { role: "assistant", content: result.score !== null && result.score < 60 ? `初步匹配度：${result.score}%。匹配度偏低，建议检查岗位要求与个人经历；你仍然可以继续创建 Journey。` : result.score !== null ? `初步匹配度：${result.score}%。${result.reasoning[0] || "整体匹配情况良好，可以继续确认。"}` : "暂时无法完成匹配评估，你可以重新评估或直接继续创建。" }]); }
    catch { const unavailable = { score: null, evaluation_failed: true, reasoning: ["暂时无法完成匹配评估"] }; setMatchOpinion(unavailable); setMessages(current => [...current, { role: "assistant", content: "暂时无法完成匹配评估，你可以稍后重试或直接继续创建。" }]); }
  }
  async function handleImage(file: File | undefined) {
    if (!file || busy || ocrBusy) return;
    setOcrBusy(true); setError(""); setImageName(file.name);
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 40000);
    try {
      const body = new FormData(); body.append("file", file);
      const result = await api<DraftResult & { text: string; confidence: number; filename: string }>("/api/journey-drafts/ocr", { method: "POST", headers: {}, body, signal: controller.signal });
      if (!result.text.trim()) throw new Error("没有识别到文字，请换一张更清晰的截图");
      acceptExtraction(result, result.text);
    } catch (e) { setError(e instanceof DOMException && e.name === "AbortError" ? "图片识别超时，请换一张更清晰的截图" : e instanceof Error ? e.message : "图片识别失败"); }
    finally { window.clearTimeout(timeout); setOcrBusy(false); }
  }
  function pasteImage(event: ClipboardEvent<HTMLElement>) {
    const item = Array.from(event.clipboardData.items).find(entry => entry.type.startsWith("image/"));
    if (item) { event.preventDefault(); handleImage(item.getAsFile() || undefined); }
  }
  function dropImage(event: DragEvent<HTMLElement>) {
    event.preventDefault(); setDragging(false);
    const file = Array.from(event.dataTransfer.files).find(item => item.type.startsWith("image/"));
    if (file) handleImage(file);
    else setError("请拖入 JPG、PNG 或 WEBP 图片");
  }
  function acceptExtraction(result: DraftResult, text: string) {
    if (!result.draft) throw new Error("后端尚未提供摘要提取接口，请重启更新后的后端服务");
    const next = result.draft;
    setDraft(next);
    setMatchOpinion(null);
    setMessages(current => [...current, { role: "user", content: text }, { role: "assistant", content: result.message }]);
    setInput("");
    setStep(result.missing_fields.length ? "questions" : "confirm");
    if (next.company && next.role) void evaluateMatch(next);
  }
  async function handleReply(event: FormEvent) {
    event.preventDefault();
    const text = input.trim();
    if (!text || busy || ocrBusy) return;
    setBusy(true); setError("");
    try {
      const result = await api<DraftResult>("/api/journey-drafts/extract", {
        method: "POST", body: JSON.stringify({ text, current: step === "input" ? null : draft }),
        signal: AbortSignal.timeout(40000),
      });
      acceptExtraction(result, text);
    } catch (e) { setError(e instanceof Error ? e.message : "摘要提取失败，请重试"); }
    finally { setBusy(false); }
  }
  async function submit() { if (!draft.company.trim() || !draft.role.trim() || !draft.description.trim()) { setError("至少需要确认公司、岗位和 JD"); return; } setBusy(true); setError(""); try { const journey = await api<Journey>("/api/journeys", { method: "POST", body: JSON.stringify({ company: draft.company, department: draft.department, role: draft.role, recruiting_cycle: draft.cycle, job_description: draft.description }) }); if (journey.deleted_at) { setError("该 Journey 已软删除，请在 chat 中查询并批准恢复。"); return; } localStorage.removeItem(JOURNEY_DRAFT_STORAGE); onCreated(journey); } catch (e) { setError(e instanceof Error ? e.message : "创建失败"); } finally { setBusy(false); } }
  return <div className="modal-backdrop"><div className="modal journey-create-modal"><button type="button" className="dialog-close" onClick={onClose}>×</button><div className="create-heading"><div><p className="eyebrow">NEW OPPORTUNITY JOURNEY</p><h2>让 Agent 帮你创建 Journey</h2><p className="muted">粘贴 JD 或上传截图，Agent 会提取信息并在创建前交给你确认。</p></div><span className={`draft-status ${step}`}>{step === "input" ? "等待 JD" : step === "questions" ? "待补充" : "待确认"}</span></div><div className="create-workspace"><section className={`create-chat ${dragging ? "dragging" : ""}`} onPaste={pasteImage} onDragOver={event => { event.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={dropImage}><div className="drop-hint">{dragging ? "松开鼠标即可识别截图" : "截图可直接拖到这里，或复制图片后按 Ctrl/Cmd+V"}</div><div className="messages">{messages.map((message, index) => <div className={message.role === "user" ? "user-bubble" : "assistant-bubble"} key={index}>{message.content}</div>)}</div><form className="create-input" onSubmit={handleReply}><textarea value={input} onChange={event => setInput(event.target.value)} placeholder={step === "input" ? "粘贴完整 JD，或描述你想申请的岗位…" : "补充信息，或告诉我需要修改什么…"} /><div className="input-footer"><label className="image-upload"><input type="file" accept="image/png,image/jpeg,image/webp,image/bmp" onChange={event => handleImage(event.target.files?.[0])} /><span>{ocrBusy ? "识别中…" : "▧ 上传 JD 截图"}</span></label><span className="upload-name">{imageName || "支持 JPG、PNG、WEBP"}</span><button className="btn primary" disabled={busy || ocrBusy || !input.trim()}>{step === "input" ? "提取信息" : "发送"}</button></div></form></section><aside className="draft-summary"><div className="card-head"><h3>Journey 摘要</h3><span className="muted">实时更新</span></div><label>公司名称<input disabled={busy || ocrBusy} value={draft.company} onChange={event => update("company", event.target.value)} placeholder="待识别" /></label><label>岗位名称<input disabled={busy || ocrBusy} value={draft.role} onChange={event => update("role", event.target.value)} placeholder="待识别" /></label><label>部门 / 业务线<input disabled={busy || ocrBusy} value={draft.department} onChange={event => update("department", event.target.value)} placeholder="可选" /></label><label>工作地点<input disabled={busy || ocrBusy} value={draft.location} onChange={event => update("location", event.target.value)} placeholder="可选" /></label><label>招聘周期<input disabled={busy || ocrBusy} value={draft.cycle} onChange={event => update("cycle", event.target.value)} placeholder="可选，例如：2026 社招" /></label><label>薪资范围<input disabled={busy || ocrBusy} value={draft.salary} onChange={event => update("salary", event.target.value)} placeholder="可选" /></label><div className="jd-preview"><strong>原始 JD</strong><p>{draft.description || "粘贴后将在这里保留原文"}</p></div>{error && <p className="error">{error}</p>}<div className="dialog-actions"><button type="button" className="btn ghost" onClick={onClose}>取消</button><button type="button" className="btn primary" onClick={submit} disabled={busy || ocrBusy || !draft.company || !draft.role || !draft.description}>{busy ? "创建中…" : "确认并创建"}</button></div></aside></div></div></div>;
}

const TASK_CATEGORIES: { key: string; label: string; prompt: string; icon: IconName }[] = [
  { key: "slides", label: "生成幻灯片", prompt: "帮我生成一份 PPT 演示文稿：", icon: "slides" },
  { key: "docs", label: "撰写文档", prompt: "帮我撰写一份 Word 文档：", icon: "document" },
  { key: "data", label: "数据分析&可视化", prompt: "帮我分析数据并生成图表洞察：", icon: "data" },
  { key: "design", label: "生成设计", prompt: "帮我生成一张设计图：", icon: "design" },
  { key: "research", label: "批量调研", prompt: "帮我批量调研以下主题：", icon: "research" },
  { key: "pdf", label: "PDF转换", prompt: "帮我把这份 PDF 转换为 Word 文档", icon: "pdf" },
];

const HOT_TASKS = [
  { title: "周报生成", sub: "述职开挂", tag: "Word", prompt: "根据我本周的工作内容，生成一份周报 Word 文档" },
  { title: "分析数据生成图表洞察", sub: "提炼核心结论，找出关键指标变化", tag: "XLSX", prompt: "分析数据生成图表洞察，提炼核心结论，找出关键指标变化" },
  { title: "PDF转Word", sub: "PDF 快速转换为 Word 文档", tag: "PDF", prompt: "把这份 PDF 快速转换为 Word 文档" },
  { title: "AI文本润色", sub: "句句出彩", tag: "润色", prompt: "对以下文本进行润色：" },
  { title: "生成月度销售分析表", sub: "月度汇总、趋势图和商品分析", tag: "XLSX", prompt: "生成月度销售分析表，包含月度汇总、趋势图和商品分析" },
  { title: "项目计划书", sub: "立项无忧", tag: "Word", prompt: "帮我撰写一份项目计划书" },
  { title: "读PDF提炼重点", sub: "提炼核心结论，掌握全文要点", tag: "PDF", prompt: "读取这份 PDF，提炼重点和核心结论" },
  { title: "修复并补全Excel公式", sub: "检查错误公式", tag: "XLSX", prompt: "检查这份 Excel，修复并补全错误公式" },
];

const INSPIRATION_CARDS = [
  { title: "项目工作总结汇报", desc: "适用于项目汇报、阶段复盘、产品上线、运营活动与管理层汇报等场景。" },
  { title: "求职简历一键优化", desc: "上传简历与目标 JD，生成岗位定制版简历 docx/pdf。" },
  { title: "面试准备包", desc: "汇总目标公司面经证据、高频问题与能力矩阵，输出可追溯准备材料。" },
];

function TitleBar({ online, office, onQuota, onProfile, onSettings }: { online: boolean; office?: boolean; onQuota: () => void; onProfile: () => void; onSettings: () => void }) {
  return <header className={`op-titlebar ${office ? "op-office-titlebar" : ""}`}>
    <div className="op-titlebar-status"><span className={`op-dot ${online ? "on" : ""}`} />{online ? "网关在线" : "网关离线"}</div>
    <div className="op-titlebar-actions">
      <button className="op-chip" onClick={onQuota} title="积分余额（占位）"><span className="op-gem">◆</span>—</button>
      <button className="op-chip" onClick={onProfile}>{office ? "Office 账户" : "个人中心"}</button>
      <button className="op-icon-btn" aria-label="设置" title="设置" onClick={onSettings}><AppIcon name="settings" /></button>
      <span className="op-win-controls" title="窗口控制由桌面壳（pywebview）提供">
        <button disabled>—</button><button disabled>▢</button><button disabled>✕</button>
      </span>
    </div>
  </header>;
}

function ResumePanel({ onUseResume }: { onUseResume: (name: string) => void }) {
  const [items, setItems] = useState<ResumeItem[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  function load() { api<ResumeItem[]>("/api/resumes").then(setItems).catch(() => setItems([])); }
  useEffect(() => { load(); }, []);
  async function upload(files: FileList | null) {
    const file = files?.[0];
    if (!file || busy) return;
    setBusy(true); setError("");
    try {
      const body = new FormData(); body.append("file", file);
      const response = await fetch("/api/resumes/upload", { method: "POST", body });
      if (!response.ok) throw new Error((await response.json().catch(() => ({} as { detail?: string }))).detail || `上传失败（HTTP ${response.status}）`);
      load();
    } catch (e) { setError(e instanceof Error ? e.message : "上传失败"); }
    finally { setBusy(false); }
  }
  return <div className="op-resume-panel">
    <p className="section-label">我的简历</p>
    <label className="op-upload">{busy ? "上传中…" : "＋ 上传简历"}<input type="file" hidden accept=".pdf,.docx,.doc,.md,.txt" onChange={event => { void upload(event.target.files); event.currentTarget.value = ""; }} /></label>
    {error && <p className="op-error">{error}</p>}
    {items.length === 0 && !busy && <p className="empty">暂无简历。上传后点击即可让 Agent 排版生成 docx/pdf。</p>}
    <div className="op-resume-list">{items.map(item => <button key={item.name} className="op-resume-item" title={`${item.updated_at} · 点击交给 Agent`} onClick={() => onUseResume(item.name)}>
      <span className="op-file-tag">{(item.name.split(".").pop() || "").toUpperCase()}</span>
      <span className="op-resume-name">{item.name}</span>
    </button>)}</div>
  </div>;
}

type OfficeConversation = { id: string; title: string; updated_at: string };
function OfficeRecent({ onOpen }: { onOpen: (conversationId?: string) => void }) {
  const [items, setItems] = useState<OfficeConversation[]>([]); const [query, setQuery] = useState("");
  useEffect(() => { api<OfficeConversation[]>("/api/office-ai/conversations?limit=5").then(setItems).catch(() => setItems([])); }, []);
  const visible = items.filter(item => item.title.includes(query));
  async function remove(id: string) { try { await api(`/api/office-ai/conversations/${id}`, { method: "DELETE" }); setItems(current => current.filter(item => item.id !== id)); } catch (error) { window.alert(error instanceof Error ? `删除失败：${error.message}` : "删除失败，请重试"); } }
  return <section className="op-recent-work"><div className="op-recent-head"><p className="section-label">最近工作</p><span><input value={query} onChange={event => setQuery(event.target.value)} placeholder="⌕" aria-label="筛选最近工作" /></span></div>{visible.length ? <div className="op-recent-list">{visible.map(item => <div className="op-recent-row" key={item.id}><button className="op-recent-item" onClick={() => onOpen(item.id)}><strong>{item.title}</strong><span>{item.updated_at}</span></button><button type="button" className="op-recent-delete" title="删除工作" onClick={event => { event.preventDefault(); event.stopPropagation(); void remove(item.id); }}>×</button></div>)}</div> : <p className="empty">没有匹配的工作</p>}</section>;
}

function OfficeAiView({ conversationId }: { conversationId?: string | null }) {
  const [items, setItems] = useState<OfficeConversation[]>([]); const [active, setActive] = useState<OfficeConversation | null>(null); const [messages, setMessages] = useState<Message[]>([]); const [draft, setDraft] = useState(""); const [busy, setBusy] = useState(false);
  const load = () => api<OfficeConversation[]>("/api/office-ai/conversations?limit=30").then(setItems).catch(() => setItems([]));
  useEffect(() => { load(); }, []);
  useEffect(() => { if (conversationId) void api<OfficeConversation & { messages: Message[] }>(`/api/office-ai/conversations/${conversationId}`).then(result => { setActive(result); setMessages(result.messages); }); }, [conversationId]);
  async function open(item: OfficeConversation) { setActive(item); const result = await api<{ messages: Message[] }>(`/api/office-ai/conversations/${item.id}`); setMessages(result.messages); }
  async function create() { const item = await api<OfficeConversation>("/api/office-ai/conversations", { method: "POST", body: JSON.stringify({ title: "新建工作" }) }); setItems(current => [item, ...current]); setActive(item); setMessages([]); }
  async function send(event: FormEvent) { event.preventDefault(); if (!active || !draft.trim() || busy) return; const content = draft.trim(); setDraft(""); setMessages(current => [...current, { role: "user", content }]); setBusy(true); try { const reply = await api<Message>(`/api/office-ai/conversations/${active.id}/messages`, { method: "POST", body: JSON.stringify({ content }) }); setMessages(current => [...current, reply]); load(); } catch (error) { setMessages(current => [...current, { role: "assistant", content: `发送失败：${error instanceof Error ? error.message : "未知错误"}` }]); } finally { setBusy(false); } }
  if (!active) return <WelcomeView onStart={prompt => { void (async () => { const item = await api<OfficeConversation>("/api/office-ai/conversations", { method: "POST", body: JSON.stringify({ title: prompt.slice(0, 36) || "新建工作" }) }); setItems(current => [item, ...current]); setActive(item); setMessages([{ role: "user", content: prompt }]); setBusy(true); try { const reply = await api<Message>(`/api/office-ai/conversations/${item.id}/messages`, { method: "POST", body: JSON.stringify({ content: prompt }) }); setMessages(current => [...current, reply]); load(); } finally { setBusy(false); } })(); }} />;
  return <div className="office-ai-view"><div className="assistant-toolbar"><div><p className="eyebrow">OFFICE AI</p><h1>Office AI 工作</h1></div><div className="office-work-actions"><button className="btn" onClick={() => active && open(active)}>刷新</button><details><summary className="btn">产物 ▾</summary><div className="office-artifact-menu">本会话暂未生成文件</div></details></div></div><section className="assistant-chat office-ai-chat"><div className="messages">{messages.map((message, index) => <div className={message.role === "user" ? "user-bubble" : "assistant-bubble"} key={message.id ?? index}>{message.content}</div>)}{busy && <div className="assistant-bubble typing">Office AI 思考中…</div>}</div><form className="chat-input" onSubmit={send}><input value={draft} onChange={event => setDraft(event.target.value)} placeholder="描述这项 Office 工作…" /><button className="btn primary" disabled={busy}>发送</button></form></section></div>;
}

function WelcomeView({ onStart }: { onStart: (prompt: string) => void }) {
  const [value, setValue] = useState("");
  const [selectedCategory, setSelectedCategory] = useState<(typeof TASK_CATEGORIES)[number] | null>(null);
  const [models, setModels] = useState<{ current: string; available: string[] } | null>(null);
  const [uploadName, setUploadName] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  useEffect(() => { api<{ current: string; available: string[] }>("/api/models").then(setModels).catch(() => setModels(null)); }, []);
  function submit() {
    const attachment = uploadName ? `\n（已上传附件：data/uploads/${uploadName}）` : "";
    const skillPrefix = selectedCategory ? `${selectedCategory.prompt}\n` : "";
    onStart(skillPrefix + (value.trim() || "把一份打卡数据，变成考勤表并统计迟到和缺勤") + attachment);
  }
  async function switchModel(model: string) {
    if (!models || model === models.current || busy) return;
    setBusy(true); setNotice("");
    try { const next = await api<{ current: string; available: string[] }>("/api/models", { method: "POST", body: JSON.stringify({ model }) }); setModels(next); setNotice(`已切换模型：${next.current}`); }
    catch (error) { setNotice(error instanceof Error ? error.message : "切换失败"); }
    finally { setBusy(false); }
  }
  async function upload(files: FileList | null) {
    const file = files?.[0];
    if (!file || busy) return;
    if (file.size > 20 * 1024 * 1024) { setNotice("暂不支持上传超过 20 MB 的单个文件"); return; }
    setBusy(true); setNotice("");
    try {
      const body = new FormData(); body.append("file", file);
      const response = await fetch("/api/uploads", { method: "POST", body });
      if (!response.ok) throw new Error((await response.json().catch(() => ({} as { detail?: string }))).detail || `上传失败（HTTP ${response.status}）`);
      const result = await response.json() as { saved_as: string };
      setUploadName(result.saved_as);
      setNotice(`已上传：${result.saved_as}`);
    } catch (error) { setNotice(error instanceof Error ? error.message : "上传失败"); }
    finally { setBusy(false); }
  }
  return <div className="op-welcome">
    <h1>Hi，让我们随时开始。</h1>
    <div className="op-composer">
      {uploadName && <p className="op-attachment"><span>📎</span>{uploadName}<button type="button" onClick={() => setUploadName("")}>×</button></p>}
      <textarea className="op-composer-input" aria-label="消息输入框" placeholder="描述你的任务，例如：把一份打卡数据，变成考勤表并统计迟到和缺勤" value={value} onChange={event => setValue(event.target.value)} onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); submit(); } }} />
      <div className="op-composer-actions">
        <div className="op-composer-left-actions">
          <label className="op-icon-btn" title={busy ? "处理中…" : "上传附件（≤20 MB）"}><input type="file" hidden disabled={busy} accept=".pdf,.docx,.doc,.md,.txt,.png,.jpg,.jpeg,.webp,.csv,.xlsx,.pptx,.json" onChange={event => { void upload(event.target.files); event.currentTarget.value = ""; }} />{busy ? <span aria-hidden="true">…</span> : <AppIcon name="attachment" />}<span className="op-action-label">添加附件</span></label>
          <button className="op-icon-btn" disabled title="选择技能（即将开放）"><AppIcon name="skills" /><span className="op-action-label">选择技能</span></button>
          {selectedCategory && <button type="button" className={`op-selected-skill ${selectedCategory.key}`} onClick={() => setSelectedCategory(null)} title="取消已选任务"><AppIcon name={selectedCategory.icon} />{selectedCategory.label}<span>×</span></button>}
        </div>
        <div className="op-composer-right-actions">
          <label className="op-model-select" title="切换对话模型">
            <select value={models?.current ?? ""} disabled={!models || busy} onChange={event => void switchModel(event.target.value)} aria-label="默认模型">
              {!models && <option value="">加载中…</option>}
              {models?.available.map(model => <option key={model} value={model}>{model}</option>)}
            </select>
          </label>
          <button className="op-send" onClick={submit} title="发送" aria-label="发送消息"><AppIcon name="send" /></button>
        </div>
        {notice && <span className="op-notice" role="status">{notice}</span>}
      </div>
    </div>
    <p className="op-workspace-status">正在准备工作台环境，完成后即可开始对话<span className="op-ellipsis">…</span></p>
    {!selectedCategory && <nav className="op-categories">{TASK_CATEGORIES.map(category => <button key={category.key} title={category.label} aria-label={category.label} onClick={() => setSelectedCategory(category)}><span className={`op-category-icon ${category.key}`}><AppIcon name={category.icon} /></span><span className="op-category-label">{category.label}</span></button>)}</nav>}
    <section className="op-section">
      <h2>热门任务</h2>
      <div className="op-carousel">{HOT_TASKS.map(task => <button key={task.title} className="op-task-card" onClick={() => onStart(task.prompt)}>
        <p>{task.title}</p><span>{task.sub}</span><em>{task.tag}</em>
      </button>)}</div>
    </section>
    <section className="op-section">
      <h2>灵感卡片</h2>
      <div className="op-inspiration">{INSPIRATION_CARDS.map(card => <div key={card.title} className="op-insp-card"><h3>{card.title}</h3><p>{card.desc}</p></div>)}</div>
    </section>
  </div>;
}

function SkillStoreView() {
  const [cards, setCards] = useState<SkillCard[]>([]);
  const [tab, setTab] = useState<"all" | "mine" | "officeplus">("all");
  const [toast, setToast] = useState("");
  useEffect(() => { api<SkillCard[]>("/api/skills").then(setCards).catch(() => setCards([])); }, []);
  const list = cards.filter(card => tab === "all" || tab === "mine" && card.source === "workspace" || tab === "officeplus" && card.source === "officeplus");
  return <div className="op-store">
    <div className="op-store-head"><h1>Skill 商店</h1>
      <div className="op-tabs">{([["all", "全部"], ["officeplus", "Office热门精选"], ["mine", "我的skills"]] as const).map(([key, label]) => <button key={key} className={tab === key ? "active" : ""} onClick={() => setTab(key)}>{label}</button>)}</div>
    </div>
    {list.length === 0 && <p className="empty">暂无 Skill</p>}
    <div className="op-store-grid">{list.map(card => <div key={`${card.source}/${card.id}`} className="op-skill-card">
      <div className="op-skill-icon">{card.title.slice(0, 1).toUpperCase()}</div>
      <h3>{card.title}</h3>
      <p>{card.summary || "（无描述）"}</p>
      <footer><span className={`op-badge ${card.installed ? "" : "ghost"}`}>{card.installed ? "已安装" : "可安装"}</span>
        <button onClick={() => setToast(`「${card.title}」${card.installed ? "已在工作区，可在对话中直接使用" : "安装能力即将开放"}`)}>{card.installed ? "详情" : "安装"}</button></footer>
    </div>)}</div>
    {toast && <div className="op-toast" role="status" onClick={() => setToast("")}>{toast}</div>}
  </div>;
}

function PlaceholderDialog({ kind, onClose }: { kind: PlaceholderKind; onClose: () => void }) {
  const copy = kind === "quota"
    ? { title: "积分余额", body: "积分与配额体系为占位组件。接入计费后端后，此处将展示余额、消耗明细与充值入口。" }
    : { title: "个人中心", body: "账号体系为占位组件。接入登录后，此处将展示账号信息、云同步与配额管理。" };
  return <div className="modal-backdrop" onClick={onClose}><div className="modal op-placeholder" onClick={event => event.stopPropagation()}>
    <button type="button" className="dialog-close" onClick={onClose}>×</button>
    <h2>{copy.title}</h2>
    <p className="muted">{copy.body}</p>
    <span className="op-badge ghost">即将开放</span>
  </div></div>;
}

function SettingsPanel({ onClose }: { onClose: () => void }) {
  const [version, setVersion] = useState("0.1.0");
  const [syncOn, setSyncOn] = useState(false);
  const [toast, setToast] = useState("");
  useEffect(() => { api<{ version: string }>("/api/app/version").then(info => setVersion(info.version)).catch(() => setVersion("0.1.0")); }, []);
  return <div className="modal-backdrop"><div className="modal op-settings">
    <button type="button" className="dialog-close" onClick={onClose}>×</button>
    <h2>设置</h2>
    <div className="op-setting-row"><div><strong>云同步</strong><p className="muted">会话与简历云备份（占位，后续开放）</p></div>
      <button className={`op-toggle ${syncOn ? "on" : ""}`} aria-pressed={syncOn} onClick={() => { setSyncOn(!syncOn); setToast(syncOn ? "云同步已关闭（占位）" : "云同步已开启（占位）"); }}>{syncOn ? "已开启" : "已关闭"}</button></div>
    <div className="op-setting-row"><div><strong>账号登录</strong><p className="muted">登录后同步积分与配额（占位）</p></div>
      <button className="btn ghost" onClick={() => setToast("登录功能即将开放")}>登录</button></div>
    <div className="op-setting-row"><div><strong>JobAgent 使用协议</strong><p className="muted">使用条款与许可协议</p></div>
      <button className="btn ghost" onClick={() => setToast("使用协议即将上线")}>查看</button></div>
    <div className="op-setting-row"><div><strong>当前版本</strong><p className="muted">v{version} · 更新器占位</p></div>
      <button className="btn ghost" onClick={() => setToast("已是最新版本（占位）")}>检查更新</button></div>
    {toast && <div className="op-toast" role="status" onClick={() => setToast("")}>{toast}</div>}
  </div></div>;
}

export function App() {
  const [view, setView] = useState<View>(() => window.location.pathname === "/office-ai" ? "officeAi" : "assistant");
  const [journeys, setJourneys] = useState<Journey[]>([]);
  const [selected, setSelected] = useState<Journey | null>(null);
  const [dialog, setDialog] = useState(false);
  const [apiOnline, setApiOnline] = useState(false);
  const [pendingPrompt, setPendingPrompt] = useState("");
  const [placeholder, setPlaceholder] = useState<PlaceholderKind | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [officeConversationId, setOfficeConversationId] = useState<string | null>(null);
  useEffect(() => {
    let active = true; let pending = false;
    const refresh = async () => {
      if (pending) return; pending = true;
      try { const items = await api<Journey[]>("/api/journeys", { signal: AbortSignal.timeout(10000) }); if (active) { setJourneys(items); setApiOnline(true); } }
      catch { if (active) setApiOnline(false); }
      finally { pending = false; }
    };
    const onVisible = () => { if (document.visibilityState === "visible") void refresh(); };
    void refresh(); window.addEventListener("focus", refresh); document.addEventListener("visibilitychange", onVisible);
    return () => { active = false; window.removeEventListener("focus", refresh); document.removeEventListener("visibilitychange", onVisible); };
  }, []);
  function navigateView(next: View) { window.history.pushState({}, "", next === "officeAi" || next === "officeSkills" ? "/office-ai" : "/"); setView(next); }
  function startWithPrompt(prompt: string) { setPendingPrompt(prompt); navigateView("assistant"); }
  function openOfficeAi() { navigateView("officeAi"); }
  const navItems: { key: View; label: string; icon: IconName }[] = [
    { key: "assistant", label: "求职助手", icon: "assistant" },
    { key: "journeys", label: "岗位 Journeys", icon: "journeys" },
  ];
  const officeNav: { key: View; label: string; icon: IconName }[] = [
    { key: "officeAi", label: "新建任务", icon: "add" },
    { key: "officeSkills", label: "Skill 商店", icon: "store" },
  ];
  const isOffice = view === "officeAi" || view === "officeSkills";
  return <div className="app-shell op-shell">
    {!isOffice && <aside className="op-sidebar">
      <div className="op-brand"><span className="op-logo">J</span><div><strong>求职助手</strong><span>历史对话与岗位工作</span></div></div>
      <nav className="op-nav">{navItems.map(item => <button key={item.key} className={view === item.key ? "active" : ""} onClick={() => navigateView(item.key)}><span><AppIcon name={item.icon} /></span>{item.label}</button>)}</nav>
      <div className="op-sidebar-scroll"><ResumePanel onUseResume={name => startWithPrompt(`请读取 data/resumes/${name}，优化排版（不改内容）后生成简历 docx，并用 soffice 转出 pdf`)} /></div>
      <p className={`op-gateway ${apiOnline ? "on" : ""}`}>{apiOnline ? "网关在线" : "网关启动中…"}</p>
    </aside>}
    {isOffice && <aside className="op-sidebar op-office-sidebar">
      <div className="op-brand"><span className="op-logo">O</span><div><strong>Office AI</strong><span>智能办公工作台</span></div></div>
      <nav className="op-nav">{officeNav.map(item => <button key={item.key} className={view === item.key ? "active" : ""} onClick={() => navigateView(item.key)}><span><AppIcon name={item.icon} /></span>{item.label}</button>)}</nav>
      <div className="op-sidebar-scroll"><OfficeRecent onOpen={id => { setOfficeConversationId(id || null); navigateView("officeAi"); }} /><section className="op-artifacts"><p className="section-label">产物</p><p className="empty">生成的文件会出现在这里</p></section></div>
      <p className={`op-gateway ${apiOnline ? "on" : ""}`}>{apiOnline ? "网关在线" : "网关启动中…"}</p>
    </aside>}
    <main className={`op-main ${isOffice ? "op-office-main" : ""}`}>
      <TitleBar online={apiOnline} office={isOffice} onQuota={() => setPlaceholder("quota")} onProfile={() => setPlaceholder("profile")} onSettings={() => setSettingsOpen(true)} />
      <div className="op-content">
        {view === "officeAi" && <OfficeAiView conversationId={officeConversationId} />}
        {view === "officeSkills" && <SkillStoreView />}
        {view === "assistant" && <AssistantView initialPrompt={pendingPrompt} onConsumedPrompt={() => setPendingPrompt("")} />}
        {view === "journeys" && (selected
          ? <JourneyDetail journey={selected} onBack={() => setSelected(null)} />
          : <JourneyList journeys={journeys} onOpen={setSelected} onCreate={() => setDialog(true)} />)}
      </div>
      {!isOffice && <nav className="op-mobile-nav" aria-label="主导航">
        {navItems.map(item => <button key={item.key} className={view === item.key ? "active" : ""} aria-current={view === item.key ? "page" : undefined} onClick={() => navigateView(item.key)}>
          <span aria-hidden="true"><AppIcon name={item.icon} /></span><span>{item.label}</span>
        </button>)}
      </nav>}
    </main>
    {dialog && <NewJourney onClose={() => setDialog(false)} onCreated={journey => { setJourneys(items => [journey, ...items.filter(item => item.id !== journey.id)]); setDialog(false); setSelected(journey); }} />}
    {settingsOpen && <SettingsPanel onClose={() => setSettingsOpen(false)} />}
    {placeholder && <PlaceholderDialog kind={placeholder} onClose={() => setPlaceholder(null)} />}
  </div>;
}
