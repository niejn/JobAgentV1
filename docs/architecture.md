# JobAgent Architecture

## Core boundary

JobAgent is a conversational Agent organized around one concrete Opportunity Journey. The user
does not operate scrapers or downloaders directly. They describe a target job, provide a JD when
available, and answer concise follow-up questions. JobAgent decides which read-only business Tool
to call and keeps application, outreach, and other high-impact actions behind human confirmation.

```text
Job Search Profile + Resume/Candidate Background
                         |
User conversation -> JobAgent -> Opportunity Tools -> Source adapters
                         |              |                  |
                         |              |                  +-- Boss / company ATS
                         |              |                  +-- Spider_XHS
                         |              |
                         |              +-- job discovery
                         |              +-- interview evidence discovery
                         |              +-- interview preparation
                         |              +-- referral discovery (later)
                         |
                         +-- Opportunity Journey state + HITL checkpoints
```

## Module interfaces

### JobAgent

The public conversational interface is intentionally small:

```python
await agent.reply(message, thread_id="candidate-session")
```

Its implementation owns conversation state, missing-information questions, Tool selection and
HITL policy. The first runtime uses one LangChain Agent with durable SQLite LangGraph checkpoints;
conversation history survives process restarts by `thread_id`. The first product slice remains a
single Agent until the JD-to-preparation workflow is reliable. PostgreSQL checkpoints and Deep
Agents subagents remain later adapters at the same seam.

The Agent receives only explicitly registered job-search Tools. Deep Agents' general filesystem
and shell tools are not enabled for the end-user Agent.

## Progressive Agent topology

The target architecture uses Deep Agents, but the first implementation does not require multiple
agents:

```text
Phase 1
User -> single JobAgent
          -> interview evidence research Tool
          -> OCR/relevance Tool
          -> interview preparation Tool

Later
User -> JobAgent Supervisor
          -> Interview Research Agent
          -> Interview Preparation Agent
          -> Tailored Resume Agent
          -> Application Agent
          -> Scheduling Agent
```

Phase 1 first makes the business Tools and Journey artifacts reliable. Later, the Supervisor
delegates those same interfaces to specialized Deep Agents. Agents exchange artifact IDs through
the Opportunity Journey store rather than copying raw posts, OCR output, resumes and calendars
through chat messages. This keeps context isolated and makes each handoff resumable and auditable.

Detailed state machines, Artifact envelopes, validation gates and per-Agent handoff contracts are
defined in [`agent-state-and-handoff.md`](agent-state-and-handoff.md). A subagent's final message is
never sufficient to advance state: the Supervisor advances only after persisted artifacts pass the
handoff contract.

The future responsibilities are deliberately narrow:

- **Interview Research Agent** runs the bounded evidence research loop and produces traceable A/B
  Interview Evidence plus an Evidence Gap Report when needed.
- **Interview Preparation Agent** turns admitted evidence into questions, completes missing answers,
  and compares the JD with confirmed Candidate Background to identify focus areas and learning gaps.
  Source-derived answers and model-supplemented answers must be labelled separately.
- **Tailored Resume Agent** selects and compresses the most relevant facts and projects from the
  immutable base resume. It creates a Tailored Resume draft and may never invent experience.
- **Application Agent** prepares and submits an approved Tailored Resume to one concrete job and
  records the result. Every external submission requires human confirmation.
- **Scheduling Agent** reads normalized calendar free/busy windows, proposes interview slots,
  prepares HR replies and records confirmed interviews. Sending a reply or creating/changing an
  event requires human confirmation.

Ordered dependencies use synchronous delegation first. Long-running evidence collection may later
use Deep Agents async subagents, but that preview capability is not a Phase 1 dependency.

### State ownership

State is separated into four layers:

1. Opportunity Journey records real-world job-search progress.
2. Journey Workstreams independently track research, preparation, resume, referral, application and
   interview work.
3. Task Runs record one bounded Agent/Tool execution, inputs, outputs, budgets and checkpoints.
4. Versioned Journey Artifacts and Handoff Manifests carry validated results between tasks/agents.

Conversation history is context, not the system of record. Phase 1 persists these records in SQLite
and large immutable artifacts in the filesystem. Repository interfaces keep a later PostgreSQL or
MySQL adapter possible. `InMemorySaver` remains suitable only for tests and short local prototypes.

### Opportunity Tools

Tools expose business intent, not crawler controls. For example:

```text
discover_interview_evidence(company, role, city?, job_description?)
```

The Agent does not provide page numbers, rate limits, output directories, XHS URLs or xsec tokens.
Those details remain inside `InterviewEvidenceDiscovery`. This Tool interface is also the future
MCP seam: an MCP adapter may expose the same operation to external clients without copying search,
OCR, persistence or relevance logic.

### Source adapters

Source adapters implement platform transport and normalization. `SpiderXhsBackend` reuses the
external Spider_XHS library for authentication, signing, search, detail and image download.
Adapters never decide whether a post is interview evidence; that belongs to the application Tool.

## Candidate context

The startup YAML references three separate concepts:

- **Job Search Profile**: desired roles, locations, salary, industries and constraints.
- **Resume**: the user-owned source document.
- **Candidate Background**: confirmed experience, projects and skills derived from the resume.

These inputs are not interchangeable. Raw resume bytes and local paths are not automatically sent
to the model. The first version loads a structured Candidate Background; resume parsing is a later
step with explicit provenance and user confirmation.

## LLM providers

The first release has one model path: `openai-compatible`. `ChatOpenAI` receives configurable
`OPENAI_BASE_URL`, `OPENAI_API_KEY` and `JOBAGENT_LLM_MODEL`, covering OpenAI, DeepSeek,
Volcengine Ark and compatible services without provider-specific branches.

Claude OAuth and Anthropic are deliberately deferred. Their dormant legacy transport is not wired
into JobAgent; support can return later through the same model-construction seam after tool-call
events and authentication have dedicated tests.

## Interview evidence flow

```text
Job/JD
  -> bounded Interview Evidence Research Loop
       -> reason over evidence gaps and prior-round feedback
       -> act with the next structured Interview Search Plan
       -> observe search, snapshots, OCR and relevance assessments
       -> update A/B evidence and Evidence Coverage
       -> repeat until coverage, marginal-gain, budget or safety stop
  -> Markdown + JSON Interview Preparation Pack
```

The Phase-1 implementation includes the Tool seam, Spider_XHS collection, freshness and seller-risk
filters, raw body/image download, local Tesseract OCR, immutable source manifests, LLM-structured A/B
admission, deterministic Evidence Coverage and adaptive replanning from rejection feedback. It
persists Journey/TaskRun/Artifact records in SQLite and produces traceable Markdown and JSON
preparation packs. A pack may be completed with `coverage.sufficient=false`; that means the bounded
run ended without enough admissible evidence, not that evidence was fabricated.

The loop is internal to the `InterviewEvidenceDiscovery` deep module. The outer JobAgent makes one
business-level Tool call; it does not micromanage crawler queries. LLM decisions are constrained to
structured next actions. Deterministic code owns deduplication, budgets, source-risk stops and final
termination, preventing an open-ended ReAct agent from repeatedly querying Xiaohongshu.

## Safety

- No automatic XHS comments or private messages in the initial Agent.
- No application submission or referral outreach without explicit human confirmation.
- API keys, cookies and ephemeral source tokens are never returned in Tool results.
- Source automation receives bounded defaults internally; a shared rate limiter is a later module.
- Research calls have independent LLM timeouts and one total workflow timeout; cancellation records
  the TaskRun as failed rather than leaving it running forever.
- MCP is an adapter, not a second implementation of business workflows.
- Base resumes are immutable; tailored versions retain provenance to selected source facts.
- Reading calendar free/busy data is separate from reading private event details.
- Sending an HR message, submitting a resume, or writing/changing a calendar event always pauses for
  approve/edit/reject and requires a durable checkpointer.
