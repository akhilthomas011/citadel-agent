import { useEffect, useRef, useState } from "react";
import { Button, Field, Input, Textarea } from "@fluentui/react-components";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { PortalApi } from "./api";
import { count, LoadState, Notice, readable, StateBadge, useLoad } from "./components";
import type { Change, Conversation, Portal } from "./types";

const suggestions = [
  "Explain what models and backends are available",
  "Help onboard a new AI workload",
  "Why are requests being throttled?",
];

export function Assistant({ api, portal, subjectId, initialPrompt, onPromptConsumed }: {
  api: PortalApi; portal: Portal; subjectId: string; initialPrompt?: string; onPromptConsumed?: () => void;
}) {
  const canReadInventory = portal.allowedActions.includes("gateway.read");
  const inventory = useLoad((signal) => canReadInventory ? api.inventory(signal) : Promise.resolve(undefined), [api, canReadInventory]);
  const conversations = useLoad((signal) => api.conversations(signal), [api]);
  const canChange = portal.allowedActions.some((a) => a === "change.draft" || a === "change.approve");
  const changes = useLoad((signal) => canChange ? api.changes(signal) : Promise.resolve([]), [api, canChange]);
  const [conversation, setConversation] = useState<Conversation>();
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [retry, setRetry] = useState<{ text: string; id: string }>();
  useEffect(() => {
    if (initialPrompt) { setMessage(initialPrompt); onPromptConsumed?.(); }
  }, [initialPrompt, onPromptConsumed]);
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => { end.current?.scrollIntoView?.({ behavior: "smooth", block: "nearest" }); }, [conversation?.messages.length]);
  useEffect(() => {
    if (!portal.allowedActions.includes("gateway.read")) return;
    const interval = setInterval(inventory.refresh, 60_000);
    return () => clearInterval(interval);
  }, [api, portal]);
  useEffect(() => {
    if (!conversation || !["queued", "running"].includes(conversation.status)) return;
    const controller = new AbortController();
    const timer = setInterval(() => {
      api.conversation(conversation.id, controller.signal).then((result) => {
        if (controller.signal.aborted) return;
        setConversation(result);
        if (!["queued", "running"].includes(result.status)) { conversations.refresh(); changes.refresh(); }
      }).catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Could not poll the turn.");
      });
    }, 3000);
    return () => { controller.abort(); clearInterval(timer); };
  }, [api, conversation?.id, conversation?.status]);

  async function submit(text = message, retryId?: string) {
    if (!text.trim() || busy || ["queued", "running"].includes(conversation?.status ?? "")) return;
    setBusy(true); setError(undefined);
    const requestId = retryId ?? crypto.randomUUID();
    try {
      const current = conversation ?? await api.createConversation();
      setConversation(current);
      setRetry({ text, id: requestId });
      const result = await api.turn(current.id, text, requestId);
      setConversation(result); setMessage(""); setRetry(undefined);
      conversations.refresh(); changes.refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The assistant could not complete the turn.");
    } finally { setBusy(false); }
  }

  async function select(id: string) {
    setBusy(true); setError(undefined);
    try { setConversation(await api.conversation(id)); setRetry(undefined); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Could not load conversation."); }
    finally { setBusy(false); }
  }

  const observed = inventory.value;
  return <>
    {portal.allowedActions.includes("gateway.read") && <section aria-label="Live gateway inventory">
      <div className="section-heading"><h2>Gateway at a glance</h2><div className="inline">
        {observed && <StateBadge state={observed.state} />}
        <Button size="small" onClick={inventory.refresh} disabled={inventory.loading}>Refresh status</Button>
      </div></div>
      <LoadState {...inventory} retry={inventory.refresh} />
      {observed && <><div className="inventory-grid">
        {([
          ["Backends", observed.backends], ["Model deployments", observed.modelDeployments],
          ["Access contracts", observed.accessContracts], ["MCP services", observed.mcpTools],
          ["Published agents", observed.publishedAgents],
        ] as const).map(([label, value]) => <div className="inventory-item" key={label}>
          <strong>{count(value.value)}</strong><span>{label}</span><small>{readable(value.source)}</small></div>)}
      </div><p className="muted">Observed {new Date(observed.observedAt).toLocaleString()}. Canonical Git counts describe configured contracts, not live reachability.</p>
        {observed.warnings.length > 0 && <details><summary>Inventory limitations</summary><ul>
          {observed.warnings.map((warning) => <li key={warning}>{readable(warning)}</li>)}</ul></details>}</>}
    </section>}
    <div className="assistant-layout">
      <aside className="conversation-list">
        <Button appearance="primary" onClick={() => { setConversation(undefined); setError(undefined); setRetry(undefined); }} disabled={busy}>New conversation</Button>
        <h3>Recent conversations</h3>
        <LoadState {...conversations} retry={conversations.refresh} />
        {conversations.value?.map((item) => <button key={item.id} className={`conversation-link ${conversation?.id === item.id ? "selected" : ""}`}
          onClick={() => select(item.id)} disabled={busy}>{item.title}<small>{new Date(item.updatedAt).toLocaleDateString()}</small></button>)}
        {conversations.value?.length === 0 && <p className="muted">Your conversations will appear here. Only you can view them.</p>}
      </aside>
      <section className="chat-panel" aria-label="Gateway assistant">
        {conversation && ["queued", "running"].includes(conversation.status) && <div className="inline" role="status">
          <span>{conversation.status === "queued" ? "Turn durably queued" : "A turn is still running. Its result will appear when complete."}</span>
          <Button onClick={async () => {
            try { setConversation(await api.cancelTurn(conversation.id)); }
            catch (reason) { setError(reason instanceof Error ? reason.message : "Turn cancellation failed."); }
          }}>Cancel assistant turn</Button>
        </div>}
        {conversation?.status === "failed" && <Notice error>Turn stopped: {readable(conversation.errorCode ?? "unknown")}. Review prepared drafts and audit evidence before sending a new turn; interrupted effects are not replayed.</Notice>}
        <div className="section-heading"><h2>Gateway assistant</h2><span className="eyebrow">Grounded in your gateway</span></div>
        <div className="chat-messages" aria-live="polite" aria-busy={busy}>
          {!conversation?.messages.length && <div className="chat-welcome"><span className="assistant-mark">C</span>
            <h3>What would you like to do?</h3><p>Understand your gateway, investigate an issue, or prepare a reviewed change. No APIM expertise needed.</p>
            <div className="suggestions">{suggestions.map((text) => <Button key={text} onClick={() => setMessage(text)}>{text}</Button>)}</div></div>}
          {conversation?.messages.map((item, index) => <article className={`message message-${item.role}`} key={index}>
            <div className="message-author">{item.role === "user" ? "You" : "Citadel"}</div>
            <div className="message-text">{item.role === "user" ? item.content : <Markdown skipHtml remarkPlugins={[remarkGfm]}
              disallowedElements={["img", "iframe", "script", "style", "input"]}
              urlTransform={(url) => {
                if (url.startsWith("#")) return url;
                try { return new URL(url).protocol === "https:" ? url : ""; } catch { return ""; }
              }}
              components={{ a: ({ href, children }) => <a href={href} target="_blank" rel="noreferrer noopener">{children}</a> }}>
              {item.content}
            </Markdown>}</div></article>)}
          {busy && <p role="status" className="muted">Working with your configured gateway and model...</p>}
          <div ref={end} />
        </div>
        {error && <div className="chat-error"><Notice error>{error}</Notice>
          {retry && <Button onClick={() => submit(retry.text, retry.id)} disabled={busy}>Check / retry same request</Button>}</div>}
        <form className="composer" onSubmit={(event) => { event.preventDefault(); void submit(); }}>
          <Field label="Message Citadel"><Textarea value={message} onChange={(_, data) => setMessage(data.value)} maxLength={8000}
            placeholder="For example: help me onboard a document summarization workload" resize="vertical" rows={3} disabled={busy} /></Field>
          <div className="composer-footer"><small>Do not paste credentials. Reference secrets in Key Vault.</small>
            <Button appearance="primary" type="submit" disabled={busy || !message.trim() || ["queued", "running"].includes(conversation?.status ?? "")}>Send message</Button></div>
        </form>
      </section>
    </div>
    {canChange && <section className="changes-panel"><div className="section-heading"><h2>Change review</h2><Button size="small" onClick={changes.refresh}>Refresh changes</Button></div>
      <p className="muted">Preview, approval and submission are separate. Submission opens a pull request; your canonical repository's deployment gates remain in force.</p>
      <LoadState {...changes} retry={changes.refresh} />
      {changes.value?.length === 0 && <p>No prepared changes. Ask Citadel to draft and preview a change first.</p>}
      {changes.value?.map((change) => <ChangeCard key={change.id} change={change} api={api} portal={portal}
        subjectId={subjectId} refresh={changes.refresh} />)}
    </section>}
  </>;
}

function ChangeCard({ change, api, portal, subjectId, refresh }: {
  change: Change; api: PortalApi; portal: Portal; subjectId: string; refresh: () => void;
}) {
  const [confirmation, setConfirmation] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  async function decide(action: "approve" | "execute") {
    setBusy(true); setError(undefined);
    try { await api.decision(change, action, confirmation || undefined); refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Change decision failed."); }
    finally { setBusy(false); }
  }
  const independent = !change.production || change.authorId !== subjectId;
  return <article className="change-card">
    <div className="section-heading"><h3>{change.summary}</h3><span className="tag">{change.approvalStatus} / {change.executionStatus}</span></div>
    <p className="muted">{change.id}{change.production && " | Production: independent approver required"}</p>
    <details><summary>Review immutable preview and contract diff</summary><pre>{change.preview}</pre><small>Content hash: {change.approvalHash}</small></details>
    {error && <Notice error>{error}</Notice>}
    <label className="acknowledge"><input type="checkbox" checked={acknowledged} onChange={(e) => setAcknowledged(e.target.checked)} />
      I reviewed this stored preview and contract diff.</label>
    {change.confirmationWord && <Field label={`Type ${change.confirmationWord} to acknowledge key replacement`}>
      <Input value={confirmation} onChange={(_, data) => setConfirmation(data.value)} /></Field>}
    <div className="inline">
      {portal.allowedActions.includes("change.approve") && <Button onClick={() => decide("approve")}
        disabled={busy || !acknowledged || !independent || change.approvalStatus !== "pending"}>Approve exact change</Button>}
      {portal.allowedActions.includes("change.draft") && <Button appearance="primary" onClick={() => decide("execute")}
        disabled={busy || !acknowledged || change.approvalStatus !== "approved" || change.executionStatus !== "prepared"
          || (!!change.confirmationWord && confirmation !== change.confirmationWord)}>Submit pull request</Button>}
      {change.pullRequestUrl && <a href={change.pullRequestUrl} target="_blank" rel="noreferrer">Open pull request</a>}
    </div>
  </article>;
}
