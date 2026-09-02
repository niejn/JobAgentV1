import { useEffect, useState, type ClipboardEvent, type DragEvent, type FormEvent } from "react";

type Journey = { id: string; company: string; role: string; department: string; recruiting_cycle: string; stage: string; job_description: string; task_count: number; artifact_count: number; updated_at: string };
type Conversation = { id: string; journey_id: string; title: string; updated_at: string };
type Message = { id?: number; role: "user" | "assistant"; content: string };
type MatchOpinion = { score: number | null; evaluation_failed: boolean; reasoning: string[]; matched_skills?: string[]; missing_skills?: string[] };
type View = "journeys" | "tasks" | "settings";

const fallback: Journey[] = [
  { id: "demo-1", company: "澜舟科技", role: "后端开发工程师", department: "平台研发部", recruiting_cycle: "2026 秋招", stage: "targeted", job_description: "负责平台服务和基础设施建设。", task_count: 3, artifact_count: 5, updated_at: "10 分钟前" },
  { id: "demo-2", company: "字节跳动", role: "基础架构研发", department: "基础架构部", recruiting_cycle: "2026 社招", stage: "interview", job_description: "负责基础架构研发和稳定性建设。", task_count: 5, artifact_count: 8, updated_at: "1 小时前" },
];
async function api<T>(url: string, options?: RequestInit): Promise<T> { const response = await fetch(url, { headers: { "Content-Type": "application/json" }, ...options }); if (!response.ok) throw new Error(await response.text() || `HTTP ${response.status}`); return response.json(); }
function Metric({ label, value, hint }: { label: string; value: string; hint: string }) { return <div className="metric"><div className="metric-label">{label}</div><div className="metric-number">{value}</div><div className="metric-change">{hint}</div></div>; }
function AssistantText({ content }: { content: string }) { const parts = content.split(/(初步匹配度：\d+%|工作地点默认填为[^，。]+|招聘周期默认填为[^。]+)/g); return <>{parts.map((part, index) => /^(初步匹配度：|工作地点默认填为|招聘周期默认填为)/.test(part) ? <strong key={index}>{part}</strong> : part)}</>; }

function JourneyDetail({ journey, onBack }: { journey: Journey; onBack: () => void }) { const [conversations, setConversations] = useState<Conversation[]>([]); const [active, setActive] = useState<Conversation | null>(null); const [messages, setMessages] = useState<Message[]>([]); const [draft, setDraft] = useState(""); const [busy, setBusy] = useState(false); useEffect(() => { api<Conversation[]>(`/api/journeys/${journey.id}/conversations`).then(setConversations).catch(() => setConversations([])); }, [journey.id]); async function open(item: Conversation) { setActive(item); const data = await api<{ messages: Message[] }>(`/api/conversations/${item.id}`); setMessages(data.messages); } async function create() { const item = await api<Conversation>(`/api/journeys/${journey.id}/conversations`, { method: "POST", body: JSON.stringify({ title: `${journey.company} · ${journey.role}` }) }); setConversations(items => [item, ...items]); await open(item); } async function send(event: FormEvent) { event.preventDefault(); if (!active || !draft.trim() || busy) return; const text = draft.trim(); setDraft(""); setMessages(items => [...items, { role: "user", content: text }]); setBusy(true); try { const reply = await api<Message>(`/api/conversations/${active.id}/messages`, { method: "POST", body: JSON.stringify({ content: text }) }); setMessages(items => [...items, reply]); } catch (error) { setMessages(items => [...items, { role: "assistant", content: `发送失败：${error instanceof Error ? error.message : "未知错误"}` }]); } finally { setBusy(false); } } return <div><button className="back-link" onClick={onBack}>← 返回求职旅程</button><div className="detail-header"><div><p className="eyebrow">OPPORTUNITY JOURNEY</p><h1>{journey.company} · {journey.role}</h1><p className="muted">{journey.department || "未填写部门"} · {journey.recruiting_cycle || "未填写招聘周期"}</p></div><span className="tag">{journey.stage}</span></div><div className="detail-grid"><section className="card detail-card"><div className="card-head"><h2>Journey 上下文</h2><span className="muted">{journey.task_count} Tasks · {journey.artifact_count} Artifacts</span></div><h3>岗位描述</h3><p className="description">{journey.job_description}</p><h3>可执行任务</h3><div className="task-list"><button>⌕ 岗位与公司调研 <span>启动 →</span></button><button>▤ 简历匹配分析 <span>启动 →</span></button><button>✦ Mock Interview <span>启动 →</span></button></div></section><section className="card chat-card"><div className="card-head"><h2>Journey 对话</h2><button className="btn primary small" onClick={create}>＋ 新建对话</button></div><div className="conversation-layout"><aside className="conversation-list"><p className="section-label">历史对话</p>{conversations.length === 0 && <p className="empty">暂无历史对话</p>}{conversations.map(item => <button className={`conversation-item ${active?.id === item.id ? "selected" : ""}`} key={item.id} onClick={() => open(item)}><strong>{item.title}</strong><span>{item.updated_at}</span></button>)}</aside><div className="chat-pane">{!active ? <div className="empty chat-empty"><div className="big-icon">◌</div><h3>选择或新建一个对话</h3><p>Agent 会读取这个 Journey 已接受的产物，并在需要时启动任务。</p></div> : <><div className="messages">{messages.length === 0 && <div className="assistant-bubble">你好，我已经准备好围绕这个 Journey 工作。你可以让我调研岗位、分析匹配度或开始 Mock Interview。</div>}{messages.map((message, index) => <div className={message.role === "user" ? "user-bubble" : "assistant-bubble"} key={message.id ?? index}>{message.content}</div>)}{busy && <div className="assistant-bubble typing">Agent 思考中…</div>}</div><form className="chat-input" onSubmit={send}><input value={draft} onChange={event => setDraft(event.target.value)} placeholder="告诉 Agent 下一步做什么…" /><button className="btn primary" disabled={busy}>发送</button></form></>}</div></div></section></div></div>; }

function JourneyList({ journeys, onOpen, onCreate }: { journeys: Journey[]; onOpen: (journey: Journey) => void; onCreate: () => void }) { return <><div className="welcome"><div><p className="eyebrow">OPPORTUNITY JOURNEY · 本周视图</p><h1>早上好，聂俊能 👋</h1><p className="muted">调研、匹配和 Mock Interview 都会沉淀在对应 Journey 中。</p></div><button className="btn primary" onClick={onCreate}>＋ 创建新 Journey</button></div><div className="metrics"><Metric label="进行中的 Journey" value={String(journeys.length)} hint="按公司、部门、职位和周期归档" /><Metric label="运行中任务" value="04" hint="2 个等待输入" /><Metric label="已接受产物" value="28" hint="↑ 8 个 · 本周" /><Metric label="待确认动作" value="03" hint="外部写操作需确认" /></div><div className="card"><div className="card-head"><h2>我的机会 Journey</h2><span className="muted">点击卡片打开详情与对话</span></div><div className="journeys">{journeys.map(journey => <button className="journey-card" key={journey.id} onClick={() => onOpen(journey)}><div className="company-logo">{journey.company.slice(0, 1)}</div><div className="journey-main"><strong>{journey.company}</strong><p>{journey.role}</p><div className="journey-meta"><span className="tag">{journey.department || "未填写部门"}</span><span>{journey.recruiting_cycle || "未填写周期"}</span><span>{journey.task_count} 个任务</span><span>{journey.artifact_count} 个产物</span></div></div><span className="time">{journey.updated_at}　→</span></button>)}</div></div></>; }

function AssistantView() { const [items, setItems] = useState<Conversation[]>([]); const [active, setActive] = useState<Conversation | null>(null); const [messages, setMessages] = useState<Message[]>([]); const [draft, setDraft] = useState(""); const [busy, setBusy] = useState(false); const [offset, setOffset] = useState(0); const [hasMore, setHasMore] = useState(true); const [loadingMore, setLoadingMore] = useState(false); async function loadPage(nextOffset: number, append = false) { if (loadingMore || (!hasMore && append)) return; setLoadingMore(true); try { const page = await api<Conversation[]>(`/api/assistant/conversations?limit=10&offset=${nextOffset}`); setItems(current => append ? [...current, ...page] : page); setOffset(nextOffset + page.length); setHasMore(page.length === 10); } finally { setLoadingMore(false); } } useEffect(() => { void loadPage(0); }, []); async function select(item: Conversation) { setActive(item); const result = await api<{ messages: Message[] }>(`/api/assistant/conversations/${item.id}`); setMessages(result.messages); } async function create() { const item = await api<Conversation>("/api/assistant/conversations", { method: "POST", body: JSON.stringify({ title: "新求职对话" }) }); setItems(current => [item, ...current]); setOffset(current => current + 1); await select(item); } async function send(event: FormEvent) { event.preventDefault(); if (!active || !draft.trim() || busy) return; const content = draft.trim(); setDraft(""); setMessages(current => [...current, { role: "user", content }]); setBusy(true); try { const reply = await api<Message>(`/api/assistant/conversations/${active.id}/messages`, { method: "POST", body: JSON.stringify({ content }) }); setMessages(current => [...current, reply]); } catch (error) { setMessages(current => [...current, { role: "assistant", content: `发送失败：${error instanceof Error ? error.message : "未知错误"}` }]); } finally { setBusy(false); } } return <div className="assistant-view"><div className="assistant-toolbar"><div><p className="eyebrow">GLOBAL JOB ASSISTANT</p><h1>求职助手</h1><p className="muted">管理求职目标、创建 Journey，并处理跨岗位问题。</p></div><button className="btn primary" onClick={create}>＋ 新建对话</button></div><div className="assistant-workspace"><aside className="assistant-history" onScroll={event => { const target = event.currentTarget; if (target.scrollTop + target.clientHeight >= target.scrollHeight - 40) void loadPage(offset, true); }}><p className="section-label">历史对话</p>{items.length === 0 && !loadingMore && <p className="empty">暂无历史对话</p>}{items.map(item => <button className={`conversation-item ${active?.id === item.id ? "selected" : ""}`} key={item.id} onClick={() => select(item)}><strong>{item.title}</strong><span>{item.updated_at}</span></button>)}{loadingMore && <p className="empty">加载中…</p>}{!hasMore && items.length > 0 && <p className="empty">已加载全部对话</p>}</aside><section className="assistant-chat">{!active ? <div className="chat-empty empty"><div className="big-icon">✦</div><h3>开始一个求职对话</h3><p>可以咨询求职方向、创建 Journey 或管理多个岗位。</p><button className="btn primary" onClick={create}>新建对话</button></div> : <><div className="messages">{messages.length === 0 && <div className="assistant-bubble">你好，我是你的求职助手。可以帮你创建和管理多个岗位 Journey。</div>}{messages.map((message, index) => <div className={message.role === "user" ? "user-bubble" : "assistant-bubble"} key={message.id ?? index}>{message.content}</div>)}{busy && <div className="assistant-bubble typing">Agent 思考中…</div>}</div><form className="chat-input" onSubmit={send}><input value={draft} onChange={event => setDraft(event.target.value)} placeholder="告诉求职助手你想做什么…" /><button className="btn primary" disabled={busy}>发送</button></form></>}</section></div></div>; }

type JourneyDraft = { company: string; role: string; department: string; cycle: string; description: string; location: string; salary: string };
type SavedJourneyDraft = JourneyDraft & { id: string; updated_at: string };
const JOURNEY_DRAFT_STORAGE = "jobagent:journey-draft";

const today = new Date().toISOString().slice(0, 10);
const emptyDraft: JourneyDraft = { company: "", role: "", department: "", cycle: today, description: "", location: "上海", salary: "" };

function cleanOcrText(text: string): string {
  return text.split(/\r?\n/).map(line => line.replace(/(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])/g, "").trim()).filter(line => {
    if (!line) return false;
    const compact = line.replace(/[\s，。,.、:：；;|/\\-]/g, "");
    const digits = (compact.match(/[0-9]/g) || []).length;
    return digits < 8 || digits / Math.max(compact.length, 1) < .55;
  }).join("\n").replace(/\n{3,}/g, "\n\n");
}

function extractDraft(text: string): JourneyDraft {
  const normalized = cleanOcrText(text);
  const draft = { ...emptyDraft, description: normalized };
  const company = text.match(/(?:公司|雇主|Employer)\s*[:：]\s*([^\n]+)/i);
  const role = text.match(/(?:职位|岗位|职位名称|Role|Position)\s*[:：]\s*([^\n]+)/i);
  const department = normalized.match(/(?:部门|业务线|事业部|Department|Business\s*Line)\s*[:：]?\s*([^\n，,；;]+)/i);
  const location = text.match(/(?:地点|工作地点|Location)\s*[:：]\s*([^\n]+)/i);
  const salary = text.match(/(?:薪资|薪酬|Salary)\s*[:：]\s*([^\n]+)/i);
  if (company) draft.company = company[1].trim();
  if (role) draft.role = role[1].trim();
  if (department) draft.department = department[1].replace(/[()（）]/g, "").trim();
  if (location) draft.location = location[1].trim();
  if (salary) draft.salary = salary[1].trim();
  if (!draft.salary) {
    const salaryRange = normalized.match(/\b\d+(?:\.\d+)?\s*[kK万千]\s*[-~至到—–]\s*\d+(?:\.\d+)?\s*[kK万千](?:\s*\/\s*(?:月|年))?|\b\d+(?:\.\d+)?\s*[-~至到—–]\s*\d+(?:\.\d+)?\s*[kK]\b/i);
    if (salaryRange) draft.salary = salaryRange[0].replace(/\s+/g, "").replace(/[—–至到~]/g, "-");
  }
  if (!draft.company) {
    const companyLine = normalized.split("\n").find(line => /(?:有限公司|科技|集团|信息技术|股份公司|信息)$/.test(line) && !/(?:公司基本信息|基本信息)$/.test(line) && line.length >= 2 && line.length <= 30);
    if (companyLine) draft.company = companyLine.replace(/^(公司|雇主)[:：]?/, "").trim();
  }
  if (!draft.role) {
    const roleLine = normalized.split("\n").find(line => /(?:工程师|经理|专员|主管|总监|开发|架构师|设计师|负责人)/.test(line) && line.length <= 30);
    if (roleLine) draft.role = roleLine.replace(/^(职位|岗位)[:：]?/, "").trim();
  }
  if (!draft.department) {
    const domain = normalized.match(/(?:DQE|质量|开发|研发|技术|产品|运营)[^\n]{0,12}[（(]\s*([^）)]+)\s*[）)]/i);
    if (domain) draft.department = domain[1].trim();
  }
  if (!location) {
    const city = normalized.match(/北京|上海|广州|深圳|杭州|南京|苏州|成都|武汉|西安|重庆|天津|厦门|香港|澳门/);
    if (city) draft.location = city[0];
  }
  return draft;
}

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
    if (!file) return;
    setOcrBusy(true); setError(""); setImageName(file.name);
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 40000);
    try {
      const body = new FormData(); body.append("file", file);
      const result = await api<{ text: string; confidence: number; filename: string }>("/api/journey-drafts/ocr", { method: "POST", headers: {}, body, signal: controller.signal });
      if (!result.text.trim()) throw new Error("没有识别到文字，请换一张更清晰的截图");
      analyze(result.text);
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
  function analyze(text: string) {
    if (!text.trim()) return;
    const next = extractDraft(text);
    setDraft(next);
    setMatchOpinion(null);
    setMessages(current => [...current, { role: "user", content: next.description }, { role: "assistant", content: next.company && next.role ? `我已经识别出公司和岗位了。工作地点默认填为${next.location || "上海"}，招聘周期默认填为${next.cycle}。请确认右侧信息；部门未识别时可以直接补充。` : "我已保存这段 JD，但还缺少公司名称和岗位名称。请直接告诉我，例如：公司是 ABC，岗位是后端工程师。" }]);
    setInput("");
    setStep(next.company && next.role ? "confirm" : "questions");
    if (next.company && next.role) void evaluateMatch(next);
  }
  function handleReply(event: FormEvent) { event.preventDefault(); const text = input.trim(); if (!text) return; if (step === "questions") { const company = text.match(/公司(?:是|：|:)?\s*([^，,。；;\n]+)/); const role = text.match(/(?:岗位|职位)(?:是|：|:)?\s*([^，,。；;\n]+)/); const next = { ...draft, company: company?.[1]?.trim() || draft.company, role: role?.[1]?.trim() || draft.role }; setDraft(next); setMessages(current => [...current, { role: "user", content: text }, { role: "assistant", content: next.company && next.role ? "信息已经补齐，请检查右侧摘要后确认创建。" : "还需要公司名称和岗位名称。你可以分别告诉我，也可以点击右侧直接编辑。" }]); setInput(""); if (next.company && next.role) { setStep("confirm"); void evaluateMatch(next); } return; } if (step === "confirm") { const company = text.match(/公司(?:改为|是|：|:)?\s*([^，,。；;\n]+)/); const role = text.match(/(?:岗位|职位)(?:改为|是|：|:)?\s*([^，,。；;\n]+)/); const next = { ...draft, company: company?.[1]?.trim() || draft.company, role: role?.[1]?.trim() || draft.role }; setDraft(next); setMessages(current => [...current, { role: "user", content: text }, { role: "assistant", content: "已记录你的补充，请继续检查右侧摘要；如果字段已正确，可以直接确认创建。" }]); setInput(""); if (company || role) { setMatchOpinion(null); void evaluateMatch(next); } return; } analyze(text); }
  async function submit() { if (!draft.company.trim() || !draft.role.trim() || !draft.description.trim()) { setError("至少需要确认公司、岗位和 JD"); return; } setBusy(true); setError(""); try { const journey = await api<Journey>("/api/journeys", { method: "POST", body: JSON.stringify({ company: draft.company, department: draft.department, role: draft.role, recruiting_cycle: draft.cycle, job_description: draft.description }) }); localStorage.removeItem(JOURNEY_DRAFT_STORAGE); onCreated(journey); } catch (e) { setError(e instanceof Error ? e.message : "创建失败"); } finally { setBusy(false); } }
  return <div className="modal-backdrop"><div className="modal journey-create-modal"><button type="button" className="dialog-close" onClick={onClose}>×</button><div className="create-heading"><div><p className="eyebrow">NEW OPPORTUNITY JOURNEY</p><h2>让 Agent 帮你创建 Journey</h2><p className="muted">粘贴 JD 或上传截图，Agent 会提取信息并在创建前交给你确认。</p></div><span className={`draft-status ${step}`}>{step === "input" ? "等待 JD" : step === "questions" ? "待补充" : "待确认"}</span></div><div className="create-workspace"><section className={`create-chat ${dragging ? "dragging" : ""}`} onPaste={pasteImage} onDragOver={event => { event.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={dropImage}><div className="drop-hint">{dragging ? "松开鼠标即可识别截图" : "截图可直接拖到这里，或复制图片后按 Ctrl/Cmd+V"}</div><div className="messages">{messages.map((message, index) => <div className={message.role === "user" ? "user-bubble" : "assistant-bubble"} key={index}>{message.content}</div>)}</div><form className="create-input" onSubmit={handleReply}><textarea value={input} onChange={event => setInput(event.target.value)} placeholder={step === "input" ? "粘贴完整 JD，或描述你想申请的岗位…" : "补充信息，或告诉我需要修改什么…"} /><div className="input-footer"><label className="image-upload"><input type="file" accept="image/png,image/jpeg,image/webp,image/bmp" onChange={event => handleImage(event.target.files?.[0])} /><span>{ocrBusy ? "识别中…" : "▧ 上传 JD 截图"}</span></label><span className="upload-name">{imageName || "支持 JPG、PNG、WEBP"}</span><button className="btn primary" disabled={busy || ocrBusy || !input.trim()}>{step === "input" ? "提取信息" : "发送"}</button></div></form></section><aside className="draft-summary"><div className="card-head"><h3>Journey 摘要</h3><span className="muted">实时更新</span></div><label>公司名称<input value={draft.company} onChange={event => update("company", event.target.value)} placeholder="待识别" /></label><label>岗位名称<input value={draft.role} onChange={event => update("role", event.target.value)} placeholder="待识别" /></label><label>部门 / 业务线<input value={draft.department} onChange={event => update("department", event.target.value)} placeholder="可选" /></label><label>工作地点<input value={draft.location} onChange={event => update("location", event.target.value)} placeholder="可选" /></label><label>招聘周期<input value={draft.cycle} onChange={event => update("cycle", event.target.value)} placeholder="可选，例如：2026 社招" /></label><label>薪资范围<input value={draft.salary} onChange={event => update("salary", event.target.value)} placeholder="可选" /></label><div className="jd-preview"><strong>原始 JD</strong><p>{draft.description || "粘贴后将在这里保留原文"}</p></div>{error && <p className="error">{error}</p>}<div className="dialog-actions"><button type="button" className="btn ghost" onClick={onClose}>取消</button><button type="button" className="btn primary" onClick={submit} disabled={busy || ocrBusy || !draft.company || !draft.role || !draft.description}>{busy ? "创建中…" : "确认并创建"}</button></div></aside></div></div></div>;
}

export function App() { const [view, setView] = useState<View>("journeys"); const [journeys, setJourneys] = useState<Journey[]>(fallback); const [selected, setSelected] = useState<Journey | null>(null); const [dialog, setDialog] = useState(false); const [apiOnline, setApiOnline] = useState(false); useEffect(() => { Promise.all([api<Journey[]>("/api/journeys"), api<{ name: string | null }>("/api/profile")]).then(([items]) => { setJourneys(items); setApiOnline(true); }).catch(() => setApiOnline(false)); }, []); if (selected) return <div className="app-shell"><main className="main"><header className="topbar"><div className="breadcrumb">求职旅程 <span>/</span> <b>{selected.company} · {selected.role}</b></div><span className={`connection ${apiOnline ? "online" : "offline"}`}>{apiOnline ? "API 已连接" : "演示数据"}</span></header><section className="content"><JourneyDetail journey={selected} onBack={() => setSelected(null)} /></section></main></div>; return <div className="app-shell"><aside className="sidebar"><div className="brand"><div className="brand-mark">J</div><div><strong>JobAgent</strong><span>求职工作台</span></div></div><nav className="nav"><button className={`nav-item ${view === "journeys" ? "active" : ""}`} onClick={() => setView("journeys")}><span>⌂</span>求职旅程</button><button className={`nav-item ${view === "tasks" ? "active" : ""}`} onClick={() => setView("tasks")}><span>✦</span>求职助手</button><button className={`nav-item ${view === "settings" ? "active" : ""}`} onClick={() => setView("settings")}><span>⚙</span>设置</button></nav><div className="sidebar-bottom"><div className="profile"><div className="avatar">聂</div><div><strong>聂俊能</strong><span>求职中 · 后端工程师</span></div></div></div></aside><main className="main"><header className="topbar"><div className="mobile-brand"><div className="brand-mark">J</div><strong>JobAgent</strong></div><div className="breadcrumb">我的求职空间 <span>/</span> <b>{view === "journeys" ? "求职旅程" : view === "tasks" ? "求职助手" : "设置"}</b></div><div className="top-actions"><span className={`connection ${apiOnline ? "online" : "offline"}`}>{apiOnline ? "API 已连接" : "演示数据"}</span><div className="top-avatar">聂</div></div></header><section className="content">{view === "journeys" ? <JourneyList journeys={journeys} onOpen={setSelected} onCreate={() => setDialog(true)} /> : view === "tasks" ? <AssistantView /> : <div className="view-placeholder"><div className="card"><div className="big-icon">⚙</div><h1>设置</h1><p className="muted">管理候选人资料、模型连接、通知方式和隐私偏好。</p><button className="btn primary" onClick={() => setView("journeys")}>返回求职旅程 →</button></div></div>}</section></main>{dialog && <NewJourney onClose={() => setDialog(false)} onCreated={journey => { setJourneys(items => [journey, ...items]); setDialog(false); }} />}</div>; }
