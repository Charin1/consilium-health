import React, { useCallback, useEffect, useState } from 'react'

import MarkdownView from '../components/MarkdownView.jsx'
import { getChatSession, listAllTasks, listChatSessions } from '../lib/apiClient.js'

/**
 * Everything that has already happened.
 *
 * Sessions, transcripts, and tasks were all persisted from the beginning — but
 * nothing in the console ever read them back, so a refresh was indistinguishable
 * from a delete. This is that gap closed.
 *
 * Two tabs because there are two different questions:
 *
 * - **Sessions** — "what did we discuss?" Chronological, per conversation.
 * - **Tasks** — "what did I ask for, and did I get it?" That question spans
 *   sessions, so it cannot be answered from inside one.
 *
 * A task counts as delivered when a message fulfils it, never from a separate
 * status field. A flag can disagree with reality; a foreign key cannot.
 */

function text(value, fallback = '') {
    return typeof value === 'string' ? value : fallback
}

function when(value) {
    if (!value) return ''
    const d = new Date(value)
    if (Number.isNaN(d.getTime())) return ''
    const days = Math.floor((Date.now() - d.getTime()) / 86400000)
    if (days === 0) return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    if (days === 1) return 'yesterday'
    if (days < 7) return `${days} days ago`
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' })
}

export default function History({ seatNames, onResume }) {
    const [tab, setTab] = useState('sessions')
    const [sessions, setSessions] = useState([])
    const [tasks, setTasks] = useState(null)
    const [taskFilter, setTaskFilter] = useState('all')
    const [openId, setOpenId] = useState(null)
    const [detail, setDetail] = useState(null)
    const [loading, setLoading] = useState(true)
    const [error, setError] = useState(null)

    const load = useCallback(async () => {
        setLoading(true)
        setError(null)
        try {
            const [sessionBody, taskBody] = await Promise.all([
                listChatSessions({ limit: 100 }),
                listAllTasks({ state: taskFilter }),
            ])
            setSessions(Array.isArray(sessionBody) ? sessionBody : [])
            setTasks(taskBody)
        } catch (err) {
            setError(text(err?.message, 'Could not load your history.'))
        } finally {
            setLoading(false)
        }
    }, [taskFilter])

    useEffect(() => { load() }, [load])

    // Fetch a transcript only when one is opened. Preloading a hundred of them
    // to render a list of titles is the classic waterfall-in-reverse.
    useEffect(() => {
        if (!openId) { setDetail(null); return undefined }
        let cancelled = false
        setDetail(null)
        getChatSession(openId)
            .then((body) => { if (!cancelled) setDetail(body) })
            .catch((err) => {
                if (!cancelled) setError(text(err?.message, 'Could not open that session.'))
            })
        return () => { cancelled = true }
    }, [openId])

    const nameFor = (id) => seatNames.get(id) || text(id, 'Advisor')

    return (
        <div className="history">
            <div className="history-list">
                <div className="pane-head">
                    <h2>History</h2>
                    <div className="history-tabs" role="tablist">
                        {['sessions', 'tasks'].map((t) => (
                            <button
                                key={t}
                                type="button"
                                role="tab"
                                aria-selected={tab === t}
                                className="tag"
                                onClick={() => setTab(t)}
                            >
                                {t}
                                {t === 'tasks' && tasks ? ` ${tasks.total}` : ''}
                            </button>
                        ))}
                    </div>
                </div>

                <div className="pane-body">
                    {error && (
                        <div className="error-banner" role="alert">
                            <span>{error}</span>
                            <button type="button" className="btn-ghost" onClick={load}>Retry</button>
                        </div>
                    )}
                    {loading && [0, 1, 2, 3].map((i) => <div key={i} className="skeleton" />)}

                    {!loading && tab === 'sessions' && (
                        sessions.length === 0 ? (
                            <p className="state">
                                <strong>Nothing yet</strong>
                                Convene a board or assign a task and it will show up here.
                            </p>
                        ) : sessions.map((session) => {
                            const items = Array.isArray(session.action_items) ? session.action_items : []
                            const done = items.filter((i) => i.message_id).length
                            return (
                                <button
                                    key={session.id}
                                    type="button"
                                    className={`history-row${openId === session.id ? ' is-open' : ''}`}
                                    onClick={() => setOpenId(session.id === openId ? null : session.id)}
                                >
                                    <span className="history-title">{text(session.title, 'Untitled')}</span>
                                    <span className="history-meta">
                                        {(session.selected_agent_ids || []).length} seats
                                        {items.length > 0 && ` · ${done}/${items.length} tasks`}
                                        {' · '}{when(session.updated_at)}
                                    </span>
                                    <span className="history-packs">
                                        {(session.persona_packs || []).map((pack) => (
                                            <span key={pack} className="pack-dot" data-pack={pack} />
                                        ))}
                                    </span>
                                </button>
                            )
                        })
                    )}

                    {!loading && tab === 'tasks' && tasks && (
                        <>
                            <div className="tag-row" style={{ marginBottom: 10 }}>
                                {['all', 'outstanding', 'delivered'].map((f) => (
                                    <button
                                        key={f}
                                        type="button"
                                        className="tag"
                                        aria-pressed={taskFilter === f}
                                        onClick={() => setTaskFilter(f)}
                                    >
                                        {f}
                                        {f === 'outstanding' ? ` ${tasks.outstanding}` : ''}
                                        {f === 'delivered' ? ` ${tasks.delivered}` : ''}
                                    </button>
                                ))}
                            </div>

                            {tasks.tasks.length === 0 ? (
                                <p className="state">
                                    <strong>No {taskFilter === 'all' ? '' : taskFilter} tasks</strong>
                                    Assign work to a seat and it will be tracked here.
                                </p>
                            ) : tasks.tasks.map((task) => (
                                <button
                                    key={task.id}
                                    type="button"
                                    className={`history-row${openId === task.session_id ? ' is-open' : ''}`}
                                    onClick={() => { setTab('sessions'); setOpenId(task.session_id) }}
                                >
                                    <span className="history-title">{text(task.task)}</span>
                                    <span className="history-meta">
                                        {text(task.owner_name, task.owner)}
                                        {' · '}
                                        <span className={task.delivered ? 'delivered' : 'outstanding'}>
                                            {task.delivered ? 'delivered' : 'outstanding'}
                                        </span>
                                        {' · '}{when(task.created_at)}
                                    </span>
                                    <span className="history-meta history-source">
                                        in “{text(task.session_title, 'a session')}”
                                    </span>
                                </button>
                            ))}
                        </>
                    )}
                </div>
            </div>

            <div className="history-detail">
                {!openId && (
                    <p className="state">
                        <strong>Nothing open</strong>
                        Pick a session to read its transcript and the work it produced.
                    </p>
                )}

                {openId && !detail && <div className="skeleton" style={{ height: 160, margin: 16 }} />}

                {detail && (
                    <>
                        <div className="pane-head">
                            <h2>{text(detail.title, 'Session')}</h2>
                            <button
                                type="button"
                                className="btn-ghost"
                                style={{ marginLeft: 'auto' }}
                                onClick={() => onResume(detail)}
                            >
                                Resume
                            </button>
                        </div>
                        <div className="pane-body">
                            {Array.isArray(detail.action_items) && detail.action_items.length > 0 && (
                                <div className="tasks" role="table" aria-label="Tasks in this session">
                                    <div className="task-row task-head" role="row">
                                        <span role="columnheader">Task</span>
                                        <span role="columnheader">Owner</span>
                                        <span role="columnheader">Priority</span>
                                        <span role="columnheader">State</span>
                                    </div>
                                    {detail.action_items.map((item) => (
                                        <div className="task-row" role="row" key={item.id}>
                                            <span role="cell">{text(item.task)}</span>
                                            <span role="cell">{nameFor(item.owner)}</span>
                                            <span role="cell">{text(item.priority, 'Medium')}</span>
                                            <span
                                                role="cell"
                                                className={item.message_id ? 'delivered' : 'outstanding'}
                                            >
                                                {item.message_id ? 'delivered' : 'outstanding'}
                                            </span>
                                        </div>
                                    ))}
                                </div>
                            )}

                            {(detail.messages || []).map((msg) => (
                                <article
                                    key={msg.id}
                                    className={`turn${msg.role === 'user' ? ' is-user' : ''}`}
                                >
                                    <header className="turn-head">
                                        <span className="who">
                                            {msg.role === 'user' ? 'You' : nameFor(msg.agent_id)}
                                        </span>
                                        {msg.metadata?.is_synthesis && <span className="tag">synthesis</span>}
                                        {msg.metadata?.degraded && (
                                            <span className="tag" style={{ color: 'var(--guard)' }}>
                                                degraded
                                            </span>
                                        )}
                                        <span className="when">{when(msg.created_at)}</span>
                                    </header>
                                    <div className="turn-body">
                                        <MarkdownView content={text(msg.content)} />
                                    </div>
                                </article>
                            ))}

                            {(detail.messages || []).length === 0 && (
                                <p className="state">
                                    <strong>No turns</strong>
                                    This session was created but nothing was said in it.
                                </p>
                            )}
                        </div>
                    </>
                )}
            </div>
        </div>
    )
}
