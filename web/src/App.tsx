import { useEffect, useState } from 'react'
import { api, bootstrapSession, upload, type ApiRecord } from './api/client'
import './App.css'

const nav = [
  ['dashboard', 'Dashboard', '⌂'], ['contacts', 'Contacts', '◎'], ['segments', 'Segments', '◇'], ['message-studio', 'Message Studio', '✎'], ['campaigns', 'Campaigns', '↗'], ['automations', 'Automations', '✦'], ['list-tools', 'List Tools', '▦'],
  ['inbound', 'Inbound', '↘'], ['providers', 'Providers', '◈'], ['activity', 'Activity', '≡'],
]

function App() {
  const [view, setView] = useState('dashboard')
  const [data, setData] = useState<ApiRecord>({})
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')

  const load = async (target = view) => {
    setBusy(true); setError('')
    try { setData(await api(target === 'dashboard' ? '/dashboard' : target === 'list-tools' ? '/list-tools/jobs' : target === 'message-studio' ? '/message-studio/documents' : `/${target}`)) }
    catch (err) { setError(err instanceof Error ? err.message : 'Unable to load this view') }
    finally { setBusy(false) }
  }
  useEffect(() => { bootstrapSession().then(() => load()).catch((err) => setError(err.message)) }, [])
  const run = async (action: () => Promise<unknown>, message: string) => {
    setBusy(true); setError('')
    try { await action(); setNotice(message); await load() }
    catch (err) { setError(err instanceof Error ? err.message : 'Action failed') }
    finally { setBusy(false) }
  }

  return <div className="shell">
    <aside className="rail"><div className="brand"><span className="brand-mark">M</span><span>Mailforge</span></div><p className="eyebrow">LOCAL OPERATIONS</p>
      <nav aria-label="Primary navigation">{nav.map(([id, label, icon]) => <button key={id} className={view === id ? 'nav-item active' : 'nav-item'} onClick={() => { setView(id); void load(id) }}><span aria-hidden="true">{icon}</span>{label}{id === 'activity' && <span className="nav-dot" />}</button>)}</nav>
      <div className="rail-foot"><span className="live-dot" /> Loopback only<br /><small>Simulation mode · no external delivery</small></div>
    </aside>
    <main className="main"><header className="topbar"><div><p className="eyebrow">WORKSPACE / {view.toUpperCase()}</p><h1>{nav.find(([id]) => id === view)?.[1]}</h1></div><div className="top-actions"><span className="status-chip"><span className="live-dot" /> Local</span><button className="icon-button" aria-label="Refresh" onClick={() => load()}>↻</button></div></header>
      {notice && <div className="notice" role="status">{notice}<button onClick={() => setNotice('')} aria-label="Dismiss">×</button></div>}{error && <div className="error" role="alert">{error}<button onClick={() => setError('')} aria-label="Dismiss">×</button></div>}{busy && <div className="loading" aria-live="polite">Updating local state…</div>}
      {view === 'dashboard' && <Dashboard data={data} onNavigate={setView} />}{view === 'contacts' && <Contacts data={data} onRun={run} />}{view === 'segments' && <Segments data={data} onRun={run} />}{view === 'message-studio' && <MessageStudio data={data} onRun={run} />}{view === 'campaigns' && <Campaigns data={data} onRun={run} />}{view === 'automations' && <Automations data={data} onRun={run} />}{view === 'list-tools' && <ListTools data={data} onRun={run} />}{view === 'inbound' && <Inbound data={data} onRun={run} />}{view === 'providers' && <Providers data={data} onRun={run} />}{view === 'activity' && <Activity data={data} />}
    </main>
  </div>
}

function Dashboard({ data, onNavigate }: { data: ApiRecord, onNavigate: (view: string) => void }) { const counts = data.counts || {}; return <div className="content"><div className="hero-copy"><div><p className="eyebrow teal">OPERATOR OVERVIEW</p><h2>Make every send<br /><em>accountable.</em></h2><p className="lede">A calm, local control room for contacts, policy-aware campaigns, and inbound signals.</p></div><div className="hero-note"><span className="stamp">LOCAL<br />FIRST</span><p>Durable actions<br />stay in your SQLite<br />authority.</p></div></div><div className="stat-grid"><Stat label="Contacts" value={counts.contacts ?? 0} tone="teal" /><Stat label="Lists" value={counts.lists ?? 0} /><Stat label="Inbound reviews" value={counts.inbound?.pending_reviews ?? 0} tone="amber" /><Stat label="Schema" value={`v${data.schema_version ?? '—'}`} /></div><section className="section-head"><div><p className="eyebrow">RECENT WORK</p><h3>Campaign pulse</h3></div><button className="text-button" onClick={() => onNavigate('campaigns')}>View campaigns ↗</button></section><CampaignTable items={data.campaigns || []} empty="No campaigns yet. Create a list and launch a synthetic campaign to begin." /></div> }
function Stat({ label, value, tone = '' }: { label: string, value: string | number, tone?: string }) { return <div className={`stat-card ${tone}`}><span>{label}</span><strong>{value}</strong><small>durable local state</small></div> }

type StudioBlock = ApiRecord & { type: string }

function MessageStudio({ data, onRun }: { data: ApiRecord, onRun: (action: () => Promise<unknown>, message: string) => void }) {
  const [selected, setSelected] = useState('')
  const [detail, setDetail] = useState<ApiRecord | null>(null)
  const [document, setDocument] = useState<ApiRecord | null>(null)
  const [preview, setPreview] = useState<ApiRecord | null>(null)
  const [contacts, setContacts] = useState<ApiRecord[]>([])
  const [contactId, setContactId] = useState('')
  const [segments, setSegments] = useState<ApiRecord[]>([])
  const [assets, setAssets] = useState<ApiRecord[]>([])
  const items: ApiRecord[] = data.items || []

  const refresh = async (id = selected) => {
    if (!id) return
    const next = await api<ApiRecord>(`/message-studio/documents/${id}`)
    setDetail(next)
    setDocument(next.draft?.document || next.published?.document || null)
  }
  useEffect(() => { void api<ApiRecord>('/contacts').then(result => setContacts(result.items || [])); void api<ApiRecord>('/segments').then(result => setSegments(result.items || [])); void api<ApiRecord>('/message-studio/assets').then(result => setAssets(result.items || [])) }, [])
  useEffect(() => { if (selected) void refresh(selected) }, [selected])

  const update = (changes: ApiRecord) => setDocument(current => current ? ({ ...current, ...changes }) : current)
  const blocks: StudioBlock[] = document?.blocks || []
  const updateBlock = (index: number, changes: ApiRecord) => update({ blocks: blocks.map((block, position) => position === index ? { ...block, ...changes } : block) })
  const addBlock = (type: string) => {
    const defaults: Record<string, ApiRecord> = {
      text: { type: 'text', text: 'Write a clear, useful paragraph.' },
      heading: { type: 'heading', level: 2, text: 'A section heading' },
      list: { type: 'list', items: ['One useful point', 'Another useful point'] },
      button: { type: 'button', label: 'Read more', url: 'https://example.com' },
      divider: { type: 'divider' },
      spacer: { type: 'spacer', height: 24 },
      image: { type: 'image', asset_id: assets.find(item => item.kind === 'image')?.id || '', alt: 'Message image', width: 600 },
      conditional: { type: 'conditional', condition: { segment_id: segments[0]?.id || '' }, if_true: [{ type: 'text', text: 'A message for this audience.' }], if_false: [{ type: 'text', text: 'A general message.' }] },
    }
    update({ blocks: [...blocks, defaults[type]] })
  }
  const moveBlock = (index: number, direction: number) => {
    const target = index + direction
    if (target < 0 || target >= blocks.length) return
    const next = [...blocks]; [next[index], next[target]] = [next[target], next[index]]; update({ blocks: next })
  }
  const create = () => onRun(async () => { const result = await api<ApiRecord>('/message-studio/documents', { method: 'POST', body: JSON.stringify({ name: 'New message', kind: 'message' }) }); setSelected(result.id); await refresh(result.id) }, 'Message draft created')
  const save = () => selected && document && onRun(() => api(`/message-studio/documents/${selected}/save`, { method: 'POST', body: JSON.stringify({ document, expected_revision_id: detail?.current_draft_revision_id }) }).then(() => refresh(selected)), 'Message draft saved')
  const publish = () => selected && onRun(() => api(`/message-studio/documents/${selected}/publish`, { method: 'POST', body: JSON.stringify({ expected_revision_id: detail?.current_draft_revision_id }) }).then(() => refresh(selected)), 'Message revision published')
  const renderPreview = () => document && onRun(async () => { setPreview(await api('/message-studio/preview', { method: 'POST', body: JSON.stringify({ document, contact_id: contactId || null }) })) }, 'Recipient preview rendered')

  return <div className="content"><div className="toolbar"><div><p className="eyebrow teal">MESSAGE STUDIO</p><p className="muted">Compose safe, reusable email content. Preview is local and never sends.</p></div><div className="toolbar-actions"><button className="primary" onClick={create}>+ New message</button></div></div><div className="studio-layout"><section className="panel"><div className="panel-head"><div><h3>Message library</h3><p className="muted">Drafts and published revisions</p></div><span className="count-badge">{items.length}</span></div>{items.length ? <div className="list-stack">{items.map(item => <button className={item.id === selected ? 'list-row selected' : 'list-row'} key={item.id} onClick={() => setSelected(item.id)}><span><strong>{item.name}</strong><small>{item.status} · {item.current_published_revision_id ? 'published' : 'draft only'}</small></span><b>Open →</b></button>)}</div> : <Empty text="Create a message to begin composing." />}<label className="file-drop studio-upload">Upload image or attachment<input aria-label="Message asset" type="file" onChange={event => { const file = event.target.files?.[0]; if (file) void onRun(async () => { const result = await upload('/message-studio/assets?kind=' + (file.type.startsWith('image/') ? 'image' : 'attachment'), file); setAssets(current => [result, ...current]) }, 'Asset stored locally') }} /></label></section>{selected && document ? <section className="panel studio-editor"><div className="panel-head"><div><h3>Compose message</h3><p className="muted">Structured blocks keep output email-safe.</p></div><span className="status-chip">{detail?.status || 'draft'}</span></div><label>Message name<input value={document.name || ''} onChange={event => update({ name: event.target.value })} /></label><label>Subject<input value={document.subject || ''} onChange={event => update({ subject: event.target.value })} /></label><label>Preheader <span className="muted">(optional)<input value={document.preheader || ''} onChange={event => update({ preheader: event.target.value })} /></span></label><div className="merge-toolbar" aria-label="Insert merge field"><span>Insert field:</span>{['name', 'email', 'contact.company'].map(field => <button key={field} onClick={() => update({ blocks: [...blocks, { type: 'text', text: `{{ ${field} }}` }] })}>{field}</button>)}</div><div className="studio-blocks">{blocks.map((block, index) => <article className="studio-block" key={index}><div className="studio-block-head"><strong>{block.type}</strong><div className="button-row"><button aria-label="Move block up" onClick={() => moveBlock(index, -1)}>↑</button><button aria-label="Move block down" onClick={() => moveBlock(index, 1)}>↓</button><button aria-label="Remove block" onClick={() => update({ blocks: blocks.filter((_, position) => position !== index) })}>×</button></div></div>{block.type === 'text' && <><label>Paragraph<textarea value={block.text || ''} onChange={event => updateBlock(index, { text: event.target.value })} rows={4} /></label><div className="inline-fields"><label><input type="checkbox" checked={Boolean(block.bold)} onChange={event => updateBlock(index, { bold: event.target.checked })} /> Bold</label><label><input type="checkbox" checked={Boolean(block.italic)} onChange={event => updateBlock(index, { italic: event.target.checked })} /> Italic</label><label>Align<select value={block.align || 'left'} onChange={event => updateBlock(index, { align: event.target.value })}><option>left</option><option>center</option><option>right</option></select></label></div></>}{block.type === 'heading' && <label>Heading<textarea value={block.text || ''} onChange={event => updateBlock(index, { text: event.target.value })} rows={2} /></label>}{block.type === 'list' && <label>List items<textarea value={(block.items || []).join('\n')} onChange={event => updateBlock(index, { items: event.target.value.split('\n') })} rows={3} /></label>}{block.type === 'button' && <div className="inline-fields"><label>Label<input value={block.label || ''} onChange={event => updateBlock(index, { label: event.target.value })} /></label><label>URL<input value={block.url || ''} onChange={event => updateBlock(index, { url: event.target.value })} /></label></div>}{block.type === 'image' && <div className="inline-fields"><label>Image<select value={block.asset_id || ''} onChange={event => updateBlock(index, { asset_id: event.target.value })}><option value="">Choose image</option>{assets.filter(asset => asset.kind === 'image').map(asset => <option key={asset.id} value={asset.id}>{asset.filename}</option>)}</select></label><label>Alt text<input value={block.alt || ''} onChange={event => updateBlock(index, { alt: event.target.value })} /></label></div>}{block.type === 'conditional' && <label>Audience segment<select value={block.condition?.segment_id || ''} onChange={event => updateBlock(index, { condition: { segment_id: event.target.value } })}><option value="">Choose segment</option>{segments.map(segment => <option key={segment.id} value={segment.id}>{segment.name}</option>)}</select></label>}</article>)}</div><div className="button-row"><select aria-label="Add message block" defaultValue="" onChange={event => { if (event.target.value) { addBlock(event.target.value); event.target.value = '' } }}><option value="">+ Add block</option><option value="text">Paragraph</option><option value="heading">Heading</option><option value="list">List</option><option value="button">Button</option><option value="image">Image</option><option value="divider">Divider</option><option value="spacer">Spacing</option><option value="conditional">Conditional</option></select><button onClick={save}>Save draft</button><button onClick={publish}>Publish revision</button></div><div className="studio-preview-controls"><select aria-label="Preview recipient" value={contactId} onChange={event => setContactId(event.target.value)}><option value="">Synthetic sample</option>{contacts.map(contact => <option key={contact.id} value={contact.id}>{contact.normalized_email}</option>)}</select><button className="primary" onClick={renderPreview}>Preview canonical output</button></div>{preview && <div className="studio-preview"><div className="panel-head"><div><h3>{preview.subject}</h3><p className="muted">{preview.approximate_bytes} bytes · {preview.attachments?.length || 0} assets</p></div></div><iframe title="Isolated message preview" sandbox="" srcDoc={preview.html} /><details><summary>Plain text and warnings</summary><pre>{preview.text}</pre>{preview.warnings?.length > 0 && <p className="warning-copy">{preview.warnings.join(' · ')}</p>}</details></div>}</section> : <section className="panel"><Empty text="Select a message or create a new draft to open the studio." /></section>}</div></div>
}


type WorkflowNode = { id: string, kind: string, label: string, config: ApiRecord }
type WorkflowEdge = { source: string, target: string, branch?: string }

const nodeDefaults: Record<string, WorkflowNode> = {
  CONDITION: { id: '', kind: 'CONDITION', label: 'Check whether…', config: { result: true } },
  WAIT: { id: '', kind: 'WAIT', label: 'Wait until…', config: { seconds: 60, timezone: 'UTC', catch_up_policy: 'run_once' } },
  ADD_TO_LIST: { id: '', kind: 'ADD_TO_LIST', label: 'Add to list', config: { list_id: '' } },
  REMOVE_FROM_LIST: { id: '', kind: 'REMOVE_FROM_LIST', label: 'Remove from list', config: { list_id: '' } },
  PREPARE_CAMPAIGN: { id: '', kind: 'PREPARE_CAMPAIGN', label: 'Prepare synthetic campaign', config: { campaign_id: '' } },
  NOTE: { id: '', kind: 'NOTE', label: 'Record operator note', config: { note: '' } },
  END: { id: '', kind: 'END', label: 'End', config: {} },
}

function Automations({ data, onRun }: { data: ApiRecord, onRun: (action: () => Promise<unknown>, message: string) => void }) {
  const [name, setName] = useState('New local automation')
  const [selected, setSelected] = useState('')
  const [detail, setDetail] = useState<ApiRecord | null>(null)
  const [graph, setGraph] = useState<ApiRecord>({ nodes: [], edges: [] })
  const [lists, setLists] = useState<ApiRecord[]>([])
  const [segments, setSegments] = useState<ApiRecord[]>([])
  const [campaigns, setCampaigns] = useState<ApiRecord[]>([])
  const [contacts, setContacts] = useState<ApiRecord[]>([])
  const [contactId, setContactId] = useState('')
  const [runs, setRuns] = useState<ApiRecord[]>([])
  const items: ApiRecord[] = data.items || []

  const refresh = async (id = selected) => {
    if (!id) return
    const next = await api<ApiRecord>(`/automations/${id}`)
    setDetail(next)
    setGraph(next.draft?.graph || next.active?.graph || { nodes: [], edges: [] })
    const runResult = await api<ApiRecord>(`/automations/${id}/runs`)
    setRuns(runResult.items || [])
  }

  useEffect(() => {
    void api<ApiRecord>('/lists').then(result => setLists(result.items || []))
    void api<ApiRecord>('/segments').then(result => setSegments(result.items || []))
    void api<ApiRecord>('/campaigns').then(result => setCampaigns(result.items || []))
    void api<ApiRecord>('/contacts').then(result => setContacts(result.items || []))
  }, [])
  useEffect(() => { if (selected) void refresh(selected) }, [selected])

  const nodes: WorkflowNode[] = graph.nodes || []
  const edges: WorkflowEdge[] = graph.edges || []
  const updateNode = (id: string, changes: Partial<WorkflowNode>) => setGraph((current: ApiRecord) => ({
    ...current,
    nodes: (current.nodes || []).map((node: WorkflowNode) => node.id === id ? { ...node, ...changes } : node),
  }))
  const addNode = (kind: string) => {
    const template = nodeDefaults[kind]
    const id = `${kind.toLowerCase()}_${Date.now()}`
    setGraph((current: ApiRecord) => ({
      ...current,
      nodes: [...(current.nodes || []), { ...template, id, config: { ...template.config } }],
    }))
  }
  const removeNode = (id: string) => {
    if (id === 'trigger' || id === 'end') return
    setGraph((current: ApiRecord) => ({
      ...current,
      nodes: current.nodes.filter((node: WorkflowNode) => node.id !== id),
      edges: current.edges.filter((edge: WorkflowEdge) => edge.source !== id && edge.target !== id),
    }))
  }
  const create = () => onRun(async () => {
    const created = await api<ApiRecord>('/automations', { method: 'POST', body: JSON.stringify({ name }) })
    setSelected(created.id)
    await api(`/automations/${created.id}/revisions`, { method: 'POST', body: JSON.stringify({ graph: created.draft.graph }) })
  }, 'Automation created')
  const save = () => selected && onRun(
    () => api(`/automations/${selected}/revisions`, {
      method: 'POST',
      body: JSON.stringify({ graph, revision_id: detail?.draft?.id }),
    }).then(() => refresh(selected)),
    'Draft saved',
  )
  const action = (path: string, message: string) => selected && onRun(
    () => api(`/automations/${selected}/${path}`, {
      method: 'POST',
      body: JSON.stringify({ revision_id: detail?.draft?.id }),
    }).then(() => refresh(selected)),
    message,
  )
  const enroll = () => selected && contactId && onRun(
    () => api(`/automations/${selected}/enroll`, {
      method: 'POST',
      body: JSON.stringify({ contact_id: contactId, event_kind: 'manual_enrollment', provenance: { ui: 'automation-console' } }),
    }).then(() => refresh(selected)),
    'Synthetic contact enrolled',
  )
  const connect = (source: string, target: string | undefined, branch: string) => setGraph((current: ApiRecord) => ({
    ...current,
    edges: [
      ...(current.edges as WorkflowEdge[]).filter(edge => !(edge.source === source && edge.branch === branch)),
      ...(target ? [{ source, target, branch }] : []),
    ],
  }))

  return <div className="content">
    <div className="toolbar"><div><p className="eyebrow teal">VISUAL WORKFLOWS</p><p className="muted">Compose accountable local automation. Every run is synthetic and revision-pinned.</p></div><div className="toolbar-actions"><input aria-label="Automation name" value={name} onChange={event => setName(event.target.value)} /><button className="primary" onClick={create}>+ New automation</button></div></div>
    <div className="automation-layout">
      <section className="panel"><div className="panel-head"><div><h3>Automation library</h3><p className="muted">Draft, active, paused, and archived definitions.</p></div><span className="count-badge">{items.length}</span></div>
        {items.length ? <div className="list-stack">{items.map(item => <button className={item.id === selected ? 'list-row selected' : 'list-row'} key={item.id} onClick={() => setSelected(item.id)}><span><strong>{item.name}</strong><small>{item.status} · {item.active_revision_id ? 'published revision' : 'draft only'}</small></span><b>Open →</b></button>)}</div> : <Empty text="Create a workflow to start mapping an operator journey." />}
      </section>
      {selected && detail ? <section className="panel automation-editor">
        <div className="panel-head"><div><h3>{detail.name}</h3><p className="muted">{detail.status} · draft revision {detail.draft?.revision_number || '—'}</p></div><span className="status-chip">{detail.status}</span></div>
        <div className="workflow-steps" aria-label="Workflow canvas"><p className="eyebrow">WHEN THIS HAPPENS… CHECK WHETHER… WAIT UNTIL… THEN DO… OTHERWISE… END</p>
          {nodes.map(node => <div className={`workflow-node ${node.kind.toLowerCase()}`} key={node.id}>
            <div className="workflow-node-top"><strong>{node.label}</strong><span className="tag">{node.kind}</span>{!['TRIGGER', 'END'].includes(node.kind) && <button className="icon-button" aria-label={`Remove ${node.label}`} onClick={() => removeNode(node.id)}>×</button>}</div>
            {node.kind === 'TRIGGER' && <label>Trigger<select value={node.config.trigger_kind || ''} onChange={event => updateNode(node.id, { config: { ...node.config, trigger_kind: event.target.value } })}><option value="manual_enrollment">Manual enrollment</option><option value="contact_imported">Contact imported</option><option value="contact_added_to_list">Contact added to list</option><option value="scheduled_occurrence">Scheduled occurrence</option><option value="trusted_inbound_event">Trusted inbound event</option></select></label>}
            {node.kind === 'TRIGGER' && node.config.trigger_kind === 'contact_added_to_list' && <label>Trigger list<select value={node.config.list_id || ''} onChange={event => updateNode(node.id, { config: { ...node.config, list_id: event.target.value } })}><option value="">Choose a list</option>{lists.map(list => <option key={list.id} value={list.id}>{list.name}</option>)}</select></label>}
            {node.kind === 'CONDITION' && <><label>Saved segment (optional)<select value={node.config.segment_id || ''} onChange={event => { const segment = segments.find(item => item.id === event.target.value); updateNode(node.id, { config: { ...node.config, segment_id: event.target.value || undefined, segment_revision_id: segment?.current_revision_id, result: event.target.value ? undefined : true } }) }}><option value="">Static result</option>{segments.map(segment => <option key={segment.id} value={segment.id}>{segment.name} · pinned {segment.current_revision_id?.slice(0, 8)}</option>)}</select></label>{!node.config.segment_id && <label>Condition result<select value={String(node.config.result)} onChange={event => updateNode(node.id, { config: { ...node.config, result: event.target.value === 'true' ? true : event.target.value === 'false' ? false : undefined } })}><option value="true">TRUE</option><option value="false">FALSE</option><option value="unknown">UNKNOWN (safe false)</option></select></label>}</>}
            {node.kind === 'WAIT' && <label>Wait seconds<input type="number" min="0" max="604800" value={node.config.seconds || 0} onChange={event => updateNode(node.id, { config: { ...node.config, seconds: Number(event.target.value) } })} /></label>}
            {['ADD_TO_LIST', 'REMOVE_FROM_LIST'].includes(node.kind) && <label>Static list<select value={node.config.list_id || ''} onChange={event => updateNode(node.id, { config: { ...node.config, list_id: event.target.value } })}><option value="">Choose a list</option>{lists.map(list => <option key={list.id} value={list.id}>{list.name}</option>)}</select></label>}
            {node.kind === 'PREPARE_CAMPAIGN' && <label>Synthetic campaign<select value={node.config.campaign_id || ''} onChange={event => updateNode(node.id, { config: { ...node.config, campaign_id: event.target.value } })}><option value="">Choose a campaign</option>{campaigns.map(campaign => <option key={campaign.id} value={campaign.id}>{campaign.name} · {campaign.status}</option>)}</select></label>}
            {node.kind === 'NOTE' && <label>Operator note<input value={node.config.note || ''} onChange={event => updateNode(node.id, { config: { ...node.config, note: event.target.value } })} /></label>}
          </div>)}
        </div>
        <div className="button-row"><select aria-label="Add workflow node" defaultValue="" onChange={event => { if (event.target.value) { addNode(event.target.value); event.target.value = '' } }}><option value="">+ Add node</option><option value="CONDITION">Check condition</option><option value="WAIT">Wait</option><option value="ADD_TO_LIST">Add to list</option><option value="REMOVE_FROM_LIST">Remove from list</option><option value="PREPARE_CAMPAIGN">Prepare synthetic campaign</option><option value="NOTE">Record note</option><option value="END">End</option></select><button onClick={save}>Save draft</button><button onClick={() => action('validate', 'Workflow validated')}>Validate</button><button onClick={() => action('publish', 'Revision published')}>Publish</button><button className="primary" onClick={() => action('activate', 'Workflow activated')}>Activate</button><button onClick={() => action('pause', 'Workflow paused')}>Pause</button></div>
        <div className="edge-editor"><div className="panel-head"><h3>Connections</h3><span className="muted">Keyboard-accessible edge editor</span></div>
          {nodes.map(node => { const outgoing = edges.filter(edge => edge.source === node.id); return <div className="edge-row" key={'edges-' + node.id}><strong>{node.label}</strong>{node.kind === 'CONDITION' ? ['true', 'false'].map(branch => <label key={branch}>{branch.toUpperCase()}<select aria-label={branch + ' connection from ' + node.label} value={outgoing.find(edge => edge.branch === branch)?.target || ''} onChange={event => connect(node.id, event.target.value || undefined, branch)}><option value="">Choose next node</option>{nodes.filter(target => target.id !== node.id).map(target => <option key={target.id} value={target.id}>{target.label}</option>)}</select></label>) : <select aria-label={'Connection from ' + node.label} value={outgoing.find(edge => edge.branch === 'default')?.target || ''} onChange={event => connect(node.id, event.target.value || undefined, 'default')}><option value="">Choose next node</option>{nodes.filter(target => target.id !== node.id).map(target => <option key={target.id} value={target.id}>{target.label}</option>)}</select>}</div>})}
          {edges.length > 0 && <p className="muted">{edges.length} explicit connections · {edges.map(edge => edge.source + ' → ' + edge.target + ' (' + (edge.branch || 'default') + ')').join(' · ')}</p>}
        </div>
        <div className="enrollment-box"><div><h3>Run a synthetic enrollment</h3><p className="muted">The active published revision is pinned to each run.</p></div><select aria-label="Contact to enroll" value={contactId} onChange={event => setContactId(event.target.value)}><option value="">Choose contact</option>{contacts.map(contact => <option key={contact.id} value={contact.id}>{contact.normalized_email}</option>)}</select><button className="primary" disabled={!contactId || detail.status !== 'active'} onClick={enroll}>Enroll contact</button></div>
        <div className="run-history"><div className="panel-head"><h3>Run history</h3><span className="muted">Durable step diagnostics</span></div>{runs.length ? runs.map(run => <div className="run-row" key={run.id}><span><strong>{run.status}</strong><small>{run.current_node_key} · revision {run.revision_id.slice(0, 8)}</small></span><div className="button-row"><button onClick={() => onRun(() => api(`/automations/runs/${run.id}/advance`, { method: 'POST' }).then(() => refresh(selected)), 'Run advanced')}>Advance</button><button onClick={() => onRun(() => api(`/automations/runs/${run.id}`, { method: 'GET' }), 'Run diagnostics loaded')}>Details</button><button onClick={() => onRun(() => api(`/automations/runs/${run.id}/${run.status === 'paused' ? 'resume' : 'pause'}`, { method: 'POST' }).then(() => refresh(selected)), run.status === 'paused' ? 'Run resumed' : 'Run paused')}>{run.status === 'paused' ? 'Resume' : 'Pause'}</button><button onClick={() => onRun(() => api(`/automations/runs/${run.id}/cancel`, { method: 'POST' }).then(() => refresh(selected)), 'Run canceled')}>Cancel</button></div></div>) : <Empty text="No enrollments yet." />}</div>
      </section> : <section className="panel"><Empty text="Select an automation or create one to open the workflow canvas." /></section>}
    </div>
  </div>
}

function ListTools({ data, onRun }: { data: ApiRecord, onRun: (action: () => Promise<unknown>, message: string) => void }) {
  const [file, setFile] = useState<File | null>(null)
  const [profile, setProfile] = useState('full')
  const [job, setJob] = useState<ApiRecord | null>(null)
  const [preview, setPreview] = useState<ApiRecord | null>(null)
  const jobs: ApiRecord[] = data.items || []
  const refresh = async (id: string) => setJob(await api<ApiRecord>(`/list-tools/jobs/${id}`))
  useEffect(() => { if (!job || ['completed', 'failed', 'canceled', 'paused'].includes(job.status)) return; const timer = window.setInterval(() => void refresh(job.id), 750); return () => window.clearInterval(timer) }, [job])
  const inspect = async () => { if (file) setPreview(await upload('/list-tools/preview', file)) }
  const create = async () => { if (!file) return; const created = await upload('/list-tools/jobs', file, `?profile=${encodeURIComponent(profile)}&dns_mode=offline`); setJob(created); await refresh(created.id) }
  return <div className="content"><div className="toolbar"><div><p className="eyebrow teal">LIST TOOLS</p><h2>Organize by Provider</h2><p className="muted">Process a local TXT, CSV, JSON, or JSONL file without importing contacts or contacting a provider.</p></div><span className="status-chip">Offline evidence</span></div>
    <section className="panel"><div className="panel-head"><div><h3>Prepare a bounded local job</h3><p className="muted">Preview first, then choose a deterministic classification profile.</p></div></div><label className="file-drop">{file ? file.name : 'Choose a list file'}<input aria-label="List Tools input file" type="file" accept=".txt,.csv,.json,.jsonl" onChange={event => setFile(event.target.files?.[0] || null)} /></label><div className="button-row"><label>Profile<select value={profile} onChange={event => setProfile(event.target.value)}><option value="full">Full classification</option><option value="validation">Validation only</option><option value="dedupe">Dedupe</option><option value="classify">Classify</option></select></label><button disabled={!file} onClick={() => void onRun(inspect, 'Preview calculated without creating contacts')}>Preview</button><button className="primary" disabled={!file} onClick={() => void onRun(create, 'List Tools job started')}>Start processing</button></div>{preview && <div className="stat-grid"><Stat label="Accepted" value={preview.accepted} tone="teal" /><Stat label="Invalid" value={preview.invalid} tone="amber" /><Stat label="Duplicates" value={preview.duplicates} /></div>}</section>
    {job && <section className="panel"><div className="panel-head"><div><h3>Job {job.id.slice(0, 8)}</h3><p className="muted">{job.source_filename} · {job.status} · no Contacts side effect</p></div><span className={`status ${job.status}`}>{job.status}</span></div><p className="muted">Processed {job.processed_count} · accepted {job.accepted_count}</p>{job.summary?.counts && <div className="stat-grid"><Stat label="Invalid" value={job.summary.counts.invalid ?? 0} tone="amber" /><Stat label="Duplicates" value={job.summary.counts.duplicates_removed ?? 0} /><Stat label="Role" value={job.summary.counts.role_based ?? 0} /><Stat label="Disposable" value={job.summary.counts.disposable ?? 0} /></div>}{job.summary?.providers && <p className="muted">Provider groups: {Object.entries(job.summary.providers).map(([name, count]) => `${name} ${count}`).join(' · ')}</p>}<div className="button-row"><button onClick={() => onRun(() => api(`/list-tools/jobs/${job.id}/pause`, { method: 'POST' }).then(() => refresh(job.id)), 'List Tools job paused')}>Pause</button><button onClick={() => onRun(() => api(`/list-tools/jobs/${job.id}/resume`, { method: 'POST' }).then(() => refresh(job.id)), 'List Tools job resumed')}>Resume</button><button onClick={() => onRun(() => api(`/list-tools/jobs/${job.id}/cancel`, { method: 'POST' }).then(() => refresh(job.id)), 'List Tools job canceled')}>Cancel</button></div>{job.artifacts?.length ? <div className="list-stack">{job.artifacts.map((artifact: ApiRecord) => <a className="list-row" key={artifact.id} href={`/api/v1/list-tools/jobs/${job.id}/artifacts/${artifact.id}`}><span><strong>{artifact.filename}</strong><small>{artifact.kind} · {artifact.size_bytes} bytes</small></span><b>Download ↓</b></a>)}</div> : <p className="muted">Artifacts appear when processing completes.</p>}{job.error_message && <div className="error" role="alert">{job.error_message}</div>}</section>}
    <section className="panel table-panel"><div className="panel-head"><h3>Recent jobs</h3><span className="muted">Durable local history</span></div>{jobs.length ? <table><thead><tr><th>Input</th><th>Status</th><th>Accepted</th><th>Updated</th></tr></thead><tbody>{jobs.map(item => <tr key={item.id} onClick={() => void refresh(item.id)}><td><strong>{item.source_filename}</strong><small className="table-subline">{item.profile}</small></td><td>{item.status}</td><td>{item.accepted_count}</td><td>{new Date(item.updated_at).toLocaleString()}</td></tr>)}</tbody></table> : <Empty text="No List Tools jobs yet." />}</section>
  </div>
}

type SegmentRule = { fieldId: string, operator: string, value: string }

const fieldTypes: Record<string, string> = { 'contact.email': 'TEXT', 'contact.display_name': 'TEXT' }
const operatorsByType: Record<string, string[]> = {
  TEXT: ['EQUALS', 'NOT_EQUALS', 'CONTAINS', 'STARTS_WITH', 'ENDS_WITH', 'IS_PRESENT', 'IS_MISSING'],
  NUMBER: ['EQUALS', 'NOT_EQUALS', 'GREATER_THAN', 'LESS_THAN', 'GREATER_THAN_OR_EQUAL', 'LESS_THAN_OR_EQUAL', 'IS_PRESENT', 'IS_MISSING'],
  BOOLEAN: ['EQUALS', 'NOT_EQUALS', 'IS_PRESENT', 'IS_MISSING'],
  DATE: ['EQUALS', 'NOT_EQUALS', 'GREATER_THAN', 'LESS_THAN', 'IS_PRESENT', 'IS_MISSING'],
  DATETIME: ['EQUALS', 'NOT_EQUALS', 'GREATER_THAN', 'LESS_THAN', 'IS_PRESENT', 'IS_MISSING'],
}

function Segments({ data, onRun }: { data: ApiRecord, onRun: (action: () => Promise<unknown>, message: string) => void }) {
  const [name, setName] = useState('New dynamic audience')
  const [logic, setLogic] = useState('ALL')
  const [nested, setNested] = useState(false)
  const [nestedLogic, setNestedLogic] = useState('ALL')
  const [negate, setNegate] = useState(false)
  const [rules, setRules] = useState<SegmentRule[]>([{ fieldId: 'contact.email', operator: 'EQUALS', value: '' }])
  const [fields, setFields] = useState<ApiRecord[]>([])
  const [preview, setPreview] = useState<ApiRecord | null>(null)
  const [machineKey, setMachineKey] = useState('')
  const [fieldLabel, setFieldLabel] = useState('')
  const [fieldType, setFieldType] = useState('TEXT')
  const refreshFields = () => api('/fields').then(result => setFields(result.items || []))
  useEffect(() => { void refreshFields() }, [])
  const items: ApiRecord[] = data.items || []
  const fieldTypeFor = (fieldId: string) => fields.find(field => field.id === fieldId)?.field_type || fieldTypes[fieldId] || 'TEXT'
  const updateRule = (index: number, changes: Partial<SegmentRule>) => setRules(current => current.map((rule, position) => position === index ? { ...rule, ...changes } : rule))
  const predicates = rules.map(rule => ({ kind: 'PREDICATE', field_id: rule.fieldId, operator: rule.operator, ...( ['IS_PRESENT', 'IS_MISSING'].includes(rule.operator) ? {} : { value: rule.value }) }))
  const group = { kind: logic, children: nested ? [{ kind: nestedLogic, children: predicates }] : predicates }
  const definition = { version: 1, root: negate ? { kind: 'NOT', child: group } : group }
  return <div className="content"><div className="toolbar"><div><p className="eyebrow teal">AUDIENCE LOGIC</p><p className="muted">Build a reusable dynamic audience. Matching is evaluated by the local Python authority.</p></div><span className="status-chip">{items.length} segments</span></div><div className="split"><section className="panel"><div className="panel-head"><h3>New segment</h3><span className="tag">revisioned</span></div><label>Segment name<input value={name} onChange={event => setName(event.target.value)} /></label><label>Combine conditions<select value={logic} onChange={event => setLogic(event.target.value)}><option value="ALL">All conditions (AND)</option><option value="ANY">Any condition (OR)</option></select></label><label><input type="checkbox" checked={nested} onChange={event => setNested(event.target.checked)} /> Nest conditions in a group</label>{nested && <label>Nested group logic<select value={nestedLogic} onChange={event => setNestedLogic(event.target.value)}><option value="ALL">All nested conditions (AND)</option><option value="ANY">Any nested condition (OR)</option></select></label>}<label><input type="checkbox" checked={negate} onChange={event => setNegate(event.target.checked)} /> Negate this group (NOT)</label>{rules.map((rule, index) => { const type = fieldTypeFor(rule.fieldId); const options = operatorsByType[type] || operatorsByType.TEXT; return <div className="rule-row" key={index}><label>Field<select value={rule.fieldId} onChange={event => { const nextType = fieldTypeFor(event.target.value); updateRule(index, { fieldId: event.target.value, operator: operatorsByType[nextType][0], value: '' }) }}><option value="contact.email">Email</option><option value="contact.display_name">Display name</option>{fields.map(field => <option key={field.id} value={field.id}>{field.label}</option>)}</select></label><label>Operator<select value={rule.operator} onChange={event => updateRule(index, { operator: event.target.value, value: '' })}>{options.map(option => <option key={option}>{option}</option>)}</select></label>{!['IS_PRESENT', 'IS_MISSING'].includes(rule.operator) && <label>Value<input type={type === 'NUMBER' ? 'number' : type === 'DATE' ? 'date' : 'text'} value={rule.value} onChange={event => updateRule(index, { value: event.target.value })} /></label>}{rules.length > 1 && <button className="icon-button" aria-label={`Remove condition ${index + 1}`} onClick={() => setRules(current => current.filter((_, position) => position !== index))}>×</button>}</div> })}<div className="button-row"><button onClick={() => setRules(current => [...current, { fieldId: 'contact.email', operator: 'EQUALS', value: '' }])} disabled={rules.length >= 5}>+ Condition</button><button className="primary" disabled={!name || rules.some(rule => !['IS_PRESENT', 'IS_MISSING'].includes(rule.operator) && !rule.value)} onClick={() => onRun(async () => { await api('/segments', { method: 'POST', body: JSON.stringify({ name, definition }) }) }, 'Segment saved')}>Save segment</button></div><details><summary>Create typed field</summary><div className="button-row"><input aria-label="Machine key" placeholder="machine_key" value={machineKey} onChange={event => setMachineKey(event.target.value)} /><input aria-label="Field label" placeholder="Field label" value={fieldLabel} onChange={event => setFieldLabel(event.target.value)} /><select aria-label="Field type" value={fieldType} onChange={event => setFieldType(event.target.value)}><option>TEXT</option><option>NUMBER</option><option>BOOLEAN</option><option>DATE</option><option>DATETIME</option></select><button disabled={!machineKey || !fieldLabel} onClick={() => onRun(async () => { await api('/fields', { method: 'POST', body: JSON.stringify({ machine_key: machineKey, label: fieldLabel, field_type: fieldType }) }); setMachineKey(''); setFieldLabel(''); await refreshFields() }, 'Typed field created')}>Create field</button></div></details></section><section className="panel"><div className="panel-head"><div><h3>Saved segments</h3><p className="muted">Edits create immutable revisions.</p></div></div>{items.length ? <div className="list-stack">{items.map(item => <button className="list-row" key={item.id} onClick={() => onRun(async () => setPreview(await api(`/segments/${item.id}/preview`)), 'Preview loaded')}><span><strong>{item.name}</strong><small>{item.description || 'Dynamic audience'}</small></span><b>Preview →</b></button>)}</div> : <Empty text="Create a segment to preview dynamic membership." />}{preview && <div className="preview-box"><strong>{preview.matched_count} matching contacts</strong><span>Revision {preview.revision_number} · {preview.definition_hash?.slice(0, 12)}</span></div>}</section></div></div>
}

function Contacts({ data: _data, onRun }: { data: ApiRecord, onRun: (action: () => Promise<unknown>, message: string) => void }) { const [name, setName] = useState('New audience'); const [listId, setListId] = useState(''); const [file, setFile] = useState<File | null>(null); const [preview, setPreview] = useState<ApiRecord | null>(null); const [lists, setLists] = useState<ApiRecord[]>([]); useEffect(() => { api('/lists').then(r => setLists(r.items || [])) }, []);
  return <div className="content"><div className="toolbar"><div><p className="eyebrow">AUDIENCE WORKBENCH</p><p className="muted">Import, inspect, and keep source provenance close to the record.</p></div><div className="toolbar-actions"><input aria-label="List name" value={name} onChange={e => setName(e.target.value)} /><button className="primary" onClick={() => onRun(async () => { await api('/lists', { method: 'POST', body: JSON.stringify({ name }) }); setLists((await api('/lists')).items || []) }, 'List created')}>+ New list</button></div></div><div className="split"><section className="panel"><div className="panel-head"><h3>Lists</h3><span className="count-badge">{lists.length}</span></div>{lists.length ? <div className="list-stack">{lists.map(list => <button className={list.id === listId ? 'list-row selected' : 'list-row'} key={list.id} onClick={() => setListId(list.id)}><span><strong>{list.name}</strong><small>{list.description || 'No description'}</small></span><b>{list.contact_count} <small>contacts</small></b></button>)}</div> : <Empty text="Create your first audience list." />}</section><section className="panel"><div className="panel-head"><div><h3>Import contacts</h3><p className="muted">CSV, JSONL, JSON, or plain text · 16 MiB cap</p></div></div><label className="file-drop">{file ? file.name : 'Choose a local file'}<input type="file" onChange={e => setFile(e.target.files?.[0] || null)} /></label><div className="button-row"><button disabled={!file} onClick={() => file && onRun(async () => setPreview(await upload('/imports/preview', file)), 'Preview ready')}>Preview</button><button className="primary" disabled={!file || !listId} onClick={() => file && onRun(() => upload('/imports', file, `?list_id=${encodeURIComponent(listId)}`), 'Contacts imported')}>Import to selected list</button></div>{preview && <div className="preview-box"><strong>{preview.accepted} accepted</strong><span>{preview.duplicates} duplicates · {preview.invalid} invalid</span></div>}</section></div><div className="split">{listId ? <StructuredImportPanel listId={listId} onRun={onRun} /> : <section className="panel"><Empty text="Choose a list to validate a structured import." /></section>}{listId && <MembershipHistory listId={listId} />}</div><section className="panel table-panel"><div className="panel-head"><h3>Contact records</h3><span className="muted">Select a list to inspect it.</span></div>{listId ? <ContactTable listId={listId} /> : <Empty text="Choose a list to inspect contacts." />}</section></div> }
function ContactTable({ listId }: { listId: string }) { const [items, setItems] = useState<ApiRecord[]>([]); useEffect(() => { api(`/contacts?list_id=${listId}`).then(r => setItems(r.items || [])) }, [listId]); return items.length ? <table><thead><tr><th>Email</th><th>Display name</th><th>State</th></tr></thead><tbody>{items.map(item => <tr key={item.id}><td>{item.normalized_email}</td><td>{item.display_name || '—'}</td><td><span className="tag">Active</span></td></tr>)}</tbody></table> : <Empty text="No contacts in this list yet." /> }

function StructuredImportPanel({ listId, onRun }: { listId: string, onRun: (action: () => Promise<unknown>, message: string) => void }) { const [rows, setRows] = useState('[{"email":"person@example.com","plan":"pro"}]'); const [mappings, setMappings] = useState('{"email":"contact.email"}'); const [result, setResult] = useState<ApiRecord | null>(null); const request = () => { const body = { list_id: listId, rows: JSON.parse(rows), mappings: JSON.parse(mappings) }; return api('/imports/structured/preview', { method: 'POST', body: JSON.stringify(body) }).then(setResult) }; return <section className="panel"><div className="panel-head"><div><h3>Structured import</h3><p className="muted">Map JSON columns to registered typed fields before writing.</p></div></div><label>Rows (JSON array)<textarea value={rows} onChange={event => setRows(event.target.value)} rows={4} /></label><label>Mappings (JSON object)<textarea value={mappings} onChange={event => setMappings(event.target.value)} rows={2} /></label><div className="button-row"><button disabled={!listId} onClick={() => onRun(request, 'Structured import preview ready')}>Validate mapping</button><button className="primary" disabled={!listId || !result?.valid} onClick={() => onRun(async () => { await api('/imports/structured', { method: 'POST', body: JSON.stringify({ list_id: listId, rows: JSON.parse(rows), mappings: JSON.parse(mappings) }) }) }, 'Structured contacts imported')}>Import typed rows</button></div>{result && <div className="preview-box"><strong>{result.valid ? 'Ready to import' : 'Validation needs attention'}</strong><span>{result.records} records · {result.invalid?.length || 0} invalid</span></div>}</section> }

function MembershipHistory({ listId }: { listId: string }) { const [items, setItems] = useState<ApiRecord[]>([]); useEffect(() => { api(`/lists/${listId}/history`).then(r => setItems(r.items || [])) }, [listId]); return <section className="panel"><div className="panel-head"><h3>Membership history</h3><span className="muted">Append-only list changes</span></div>{items.length ? <div className="list-stack">{items.slice(0, 8).map(item => <div className="list-row" key={item.id}><span><strong>{item.action}</strong><small>{item.source} · {item.reason}</small></span><code>{item.occurred_at}</code></div>)}</div> : <Empty text="No membership changes recorded yet." />}</section> }

function Campaigns({ data, onRun }: { data: ApiRecord, onRun: (action: () => Promise<unknown>, message: string) => void }) {
  const [name, setName] = useState('Synthetic launch'); const [listId, setListId] = useState(''); const [segmentId, setSegmentId] = useState(''); const [segmentRevisionId, setSegmentRevisionId] = useState(''); const [segmentRevisions, setSegmentRevisions] = useState<ApiRecord[]>([]); const [subject, setSubject] = useState('A local test message'); const [email, setEmail] = useState('sender@example.com'); const [messageRevisionId, setMessageRevisionId] = useState(''); const [messages, setMessages] = useState<ApiRecord[]>([]); const [selected, setSelected] = useState(''); const [preview, setPreview] = useState<ApiRecord | null>(null); const [lists, setLists] = useState<ApiRecord[]>([]); const [segments, setSegments] = useState<ApiRecord[]>([])
  useEffect(() => { api('/lists').then(r => setLists(r.items || [])); api('/segments').then(r => setSegments(r.items || [])); api('/message-studio/documents').then(r => setMessages((r.items || []).filter((item: ApiRecord) => item.current_published_revision_id))) }, [])
  useEffect(() => { if (segmentId) void api(`/segments/${segmentId}`).then(r => { setSegmentRevisions(r.revisions || []); setSegmentRevisionId(r.revision?.id || '') }).catch(() => { setSegmentRevisions([]); setSegmentRevisionId('') }); else { setSegmentRevisions([]); setSegmentRevisionId('') } }, [segmentId])
  const items = data.items || []; const usingSegment = Boolean(segmentId)
  return <div className="content"><div className="toolbar"><div><p className="eyebrow">OUTBOUND CONTROL</p><p className="muted">Prepare policy-aware messages and run synthetic delivery only.</p></div><div className="status-chip amber-chip">No network delivery</div></div><div className="split"><section className="panel"><div className="panel-head"><h3>New campaign</h3><span className="tag">simulation</span></div><label>Campaign name<input value={name} onChange={e => setName(e.target.value)} /></label><label>Audience list<select value={listId} disabled={usingSegment} onChange={e => { setListId(e.target.value); setSegmentId('') }}><option value="">Choose a list</option>{lists.map(list => <option key={list.id} value={list.id}>{list.name}</option>)}</select></label><label>Or dynamic segment<select value={segmentId} disabled={Boolean(listId)} onChange={e => { setSegmentId(e.target.value); setListId('') }}><option value="">Choose a segment</option>{segments.map(segment => <option key={segment.id} value={segment.id}>{segment.name}</option>)}</select></label>{segmentId && <label>Pin segment revision<select value={segmentRevisionId} onChange={e => setSegmentRevisionId(e.target.value)}>{segmentRevisions.map(revision => <option key={revision.id} value={revision.id}>Revision {revision.revision_number}</option>)}</select></label>}<label>Message revision<select aria-label="Message revision" value={messageRevisionId} onChange={e => setMessageRevisionId(e.target.value)}><option value="">Use legacy campaign text</option>{messages.map(message => <option key={message.current_published_revision_id} value={message.current_published_revision_id}>{message.name} · published</option>)}</select></label><label>Sender email<input value={email} onChange={e => setEmail(e.target.value)} /></label><label>Subject<input value={subject} onChange={e => setSubject(e.target.value)} /></label><button className="primary full" disabled={!listId && !segmentId} onClick={() => onRun(async () => { const result = await api<ApiRecord>('/campaigns', { method: 'POST', body: JSON.stringify({ name, list_id: listId || null, segment_id: segmentId || null, segment_revision_id: segmentRevisionId || null, message_revision_id: messageRevisionId || null, sender_email: email, subject }) }); setSelected(result.id) }, 'Campaign created')}>Create campaign</button></section><section className="panel"><div className="panel-head"><div><h3>Campaign queue</h3><p className="muted">Every transition is durable and reviewable.</p></div></div><CampaignTable items={items} empty="No campaigns have been created." onSelect={setSelected} />{selected && <CampaignActions id={selected} onRun={onRun} onPreview={setPreview} />}{preview && <pre className="preview-json">{JSON.stringify(preview, null, 2)}</pre>}</section></div></div>
}
function CampaignActions({ id, onRun, onPreview }: { id: string, onRun: (action: () => Promise<unknown>, message: string) => void, onPreview: (value: ApiRecord) => void }) { return <div className="action-dock"><span>Selected campaign</span><code>{id.slice(0, 8)}</code><button onClick={() => onRun(() => api(`/campaigns/${id}/prepare`, { method: 'POST', body: JSON.stringify({}) }), 'Campaign prepared')}>Prepare</button><button onClick={() => onRun(async () => onPreview(await api(`/campaigns/${id}/preview`)), 'Campaign preview loaded')}>Preview</button><button onClick={() => onRun(() => api(`/campaigns/${id}/authorize`, { method: 'POST' }), 'Simulation authorized')}>Authorize</button><button className="primary" onClick={() => onRun(() => api(`/campaigns/${id}/simulate`, { method: 'POST' }), 'Synthetic worker started')}>Run synthetic</button><button onClick={() => onRun(() => api(`/campaigns/${id}/pause`, { method: 'POST' }), 'Pause requested')}>Pause</button><button onClick={() => onRun(() => api(`/campaigns/${id}/resume`, { method: 'POST' }), 'Resume requested')}>Resume</button><button onClick={() => { if (window.confirm('Cancel this campaign?')) void onRun(() => api(`/campaigns/${id}/cancel`, { method: 'POST' }), 'Campaign canceled') }}>Cancel</button></div> }
function CampaignTable({ items, empty, onSelect }: { items: ApiRecord[], empty: string, onSelect?: (id: string) => void }) { return items.length ? <div className="campaign-list">{items.map(item => <button className="campaign-row" key={item.id} onClick={() => onSelect?.(item.id)}><span className="campaign-icon">↗</span><span className="campaign-main"><strong>{item.name}</strong><small>{item.description || item.template_ref || 'Local campaign'}</small></span><span className={`status ${item.status}`}>{item.status?.replace('_', ' ')}</span><span className="row-arrow">→</span></button>)}</div> : <Empty text={empty} /> }

function Inbound({ data, onRun }: { data: ApiRecord, onRun: (action: () => Promise<unknown>, message: string) => void }) { const reviews = data.items || []; const messages = data.messages || []; const [file, setFile] = useState<File | null>(null); return <div className="content"><div className="toolbar"><div><p className="eyebrow">INBOUND SIGNALS</p><p className="muted">Evidence is retained locally; policy changes wait for operator review.</p></div><span className="status-chip">{reviews.length} pending</span></div><section className="panel"><div className="panel-head"><div><h3>Acquire local evidence</h3><p className="muted">RFC 5322 EML only · 10 MiB cap · no mailbox connection</p></div></div><label className="file-drop">{file ? file.name : 'Choose an EML file'}<input aria-label="Inbound EML file" type="file" accept=".eml,message/rfc822" onChange={e => setFile(e.target.files?.[0] || null)} /></label><button className="primary" disabled={!file} onClick={() => file && onRun(() => upload('/inbound/import', file), 'Inbound evidence acquired')}>Import evidence for review</button></section><section className="panel table-panel"><div className="panel-head"><h3>Review queue</h3><span className="muted">No automatic external actions</span></div>{reviews.length ? reviews.map((review: ApiRecord) => <div className="review-row" key={review.id}><div><strong>{review.action_type}</strong><p>{review.action_reason || review.reason}</p><small>{review.target || 'No target'} · {review.status}</small></div><div className="button-row"><button onClick={() => onRun(() => api(`/inbound/reviews/${review.action_id}`, { method: 'POST', body: JSON.stringify({ decision: 'reject', reason: 'Rejected in local console' }) }), 'Review rejected')}>Reject</button><button className="primary" onClick={() => onRun(() => api(`/inbound/reviews/${review.action_id}`, { method: 'POST', body: JSON.stringify({ decision: 'approve', reason: 'Approved in local console' }) }), 'Review approved')}>Approve</button></div></div>) : <Empty text="No inbound actions need review." />}</section><section className="panel table-panel"><div className="panel-head"><h3>Acquired messages</h3><span className="muted">Classification, correlation, and trust are persisted</span></div>{messages.length ? <table><thead><tr><th>Subject</th><th>Classification</th><th>Correlation</th><th>Trust</th></tr></thead><tbody>{messages.map((message: ApiRecord) => { const correlation = message.correlations?.[0] || {}; return <tr key={message.id}><td><strong>{message.subject || 'No subject'}</strong><small className="table-subline">{message.sender || 'Unknown sender'}</small></td><td>{message.classification || message.parse_status}</td><td>{correlation.match_kind || 'unmatched'}</td><td>{correlation.trust_status || 'unknown'}</td></tr> })}</tbody></table> : <Empty text="No inbound evidence acquired yet." />}</section></div> }
function Providers({ data, onRun }: { data: ApiRecord, onRun: (action: () => Promise<unknown>, message: string) => void }) { const profiles = data.items || []; return <div className="content"><div className="toolbar"><div><p className="eyebrow">PROVIDER CATALOG</p><p className="muted">Secret-free profiles and diagnostics. Local fixtures are the only runtime contact.</p></div><span className="status-chip">{profiles.length} profiles</span></div><section className="panel table-panel">{profiles.length ? <table><thead><tr><th>Reference</th><th>Kind</th><th>Endpoint</th><th>Credential</th><th></th></tr></thead><tbody>{profiles.map((profile: ApiRecord) => <tr key={profile.ref}><td><strong>{profile.ref}</strong></td><td><span className="tag">{profile.kind}</span></td><td>{profile.host}:{profile.port}</td><td>{profile.credential_ref}</td><td><button onClick={() => onRun(() => api(`/providers/${profile.ref}/validate`, { method: 'POST' }), 'Provider validated without network contact')}>Validate</button></td></tr>)}</tbody></table> : <Empty text="No provider profiles registered. Add them through the CLI or API." />}</section></div> }
function Activity({ data }: { data: ApiRecord }) { const items = data.items || []; return <div className="content"><div className="toolbar"><div><p className="eyebrow">OPERATIONAL LEDGER</p><p className="muted">Append-only events from the canonical SQLite authority.</p></div></div><section className="panel table-panel">{items.length ? <table><thead><tr><th>When</th><th>Aggregate</th><th>Event</th><th>Details</th></tr></thead><tbody>{items.map((item: ApiRecord) => <tr key={item.id}><td>{new Date(item.occurred_at).toLocaleString()}</td><td>{item.aggregate_type} · {item.aggregate_id.slice(0, 8)}</td><td><strong>{item.event_type}</strong></td><td><code>{item.payload?.name || '—'}</code></td></tr>)}</tbody></table> : <Empty text="No operational events yet." />}</section></div> }
function Empty({ text }: { text: string }) { return <div className="empty"><span>○</span><p>{text}</p></div> }
export default App
