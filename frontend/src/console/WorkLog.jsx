import React from 'react'
import MarkdownView from '../components/MarkdownView.jsx'

/**
 * The transcript of a convened board.
 *
 * Advisor replies are model output, so they render through MarkdownView,
 * which builds React elements rather than setting innerHTML. No LLM text
 * reaches the DOM as markup.
 */

function text(value, fallback = '') {
    return typeof value === 'string' ? value : fallback
}

function when(value) {
    if (!value) return ''
    const d = new Date(value)
    return Number.isNaN(d.getTime())
        ? ''
        : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

export default function WorkLog({
    messages,
    seatNames,
    seats,
    actionItems,
    busy,
    roundJob,
    error,
    onSynthesize,
    canSynthesize,
    target,
    onTargetChange,
    onSend,
    onStop,
    headline,
}) {
    const list = Array.isArray(messages) ? messages : []
    const roomSeats = (Array.isArray(seats) ? seats : []).filter((s) => s.id !== 'moderator')
    const [draft, setDraft] = React.useState('')

    const submit = (e) => {
        e.preventDefault()
        const message = draft.trim()
        if (!message || busy) return
        setDraft('')
        onSend(message)
    }

    return (
        <div className="section">
            <h3>
                {text(headline, 'Work log')}
                {canSynthesize && (
                    <button
                        type="button"
                        className="btn-ghost"
                        style={{ marginLeft: 10 }}
                        onClick={onSynthesize}
                        disabled={busy}
                    >
                        {busy ? 'Working…' : 'Ask the chair to synthesize'}
                    </button>
                )}
            </h3>

            {error && <div className="error-banner" role="alert">{text(error)}</div>}

            <RoundProgress job={roundJob} seatNames={seatNames} onStop={onStop} />

            <TaskList items={actionItems} seatNames={seatNames} />

            {list.length === 0 && !busy && (
                <p className="state" style={{ padding: '14px 0' }}>
                    <strong>Nothing convened yet</strong>
                    Seat a room and convene the board to start the debate.
                </p>
            )}

            <div aria-live="polite" aria-busy={busy ? 'true' : 'false'}>
                {list.map((msg) => {
                    const isUser = msg.role === 'user'
                    const who = isUser
                        ? 'You'
                        : seatNames.get(msg.agent_id) || text(msg.agent_id, 'Advisor')
                    return (
                        <article key={msg.id} className={`turn${isUser ? ' is-user' : ''}`}>
                            <header className="turn-head">
                                <span className="who">{who}</span>
                                {msg.metadata && msg.metadata.is_synthesis && (
                                    <span className="tag" aria-label="Chair synthesis">synthesis</span>
                                )}
                                <span className="when">{when(msg.created_at)}</span>
                            </header>
                            <div className="turn-body">
                                <MarkdownView content={text(msg.content)} />
                            </div>
                        </article>
                    )
                })}
                {busy && <div className="skeleton" style={{ height: 72 }} />}
            </div>

            {/*
              * Targeting is the difference between a boardroom and a
              * one-to-one. "The room" runs a round; naming a seat puts the
              * question to that person alone.
              */}
            <form className="composer" onSubmit={submit}>
                <label className="sr-only" htmlFor="composer-target">Who answers</label>
                <select
                    id="composer-target"
                    value={target || ''}
                    onChange={(e) => onTargetChange(e.target.value || null)}
                >
                    <option value="">The room</option>
                    {roomSeats.map((seat) => (
                        <option key={seat.id} value={seat.id}>
                            {text(seat.name, seat.id)}
                        </option>
                    ))}
                </select>
                <label className="sr-only" htmlFor="composer-text">Your message</label>
                <input
                    id="composer-text"
                    type="text"
                    value={draft}
                    placeholder={
                        target
                            ? `Ask ${seatNames.get(target) || target} directly…`
                            : 'Put a question to the room…'
                    }
                    onChange={(e) => setDraft(e.target.value)}
                />
                <button type="submit" className="btn" disabled={!draft.trim() || busy}>
                    Send
                </button>
            </form>
        </div>
    )
}


/**
 * Live status of a "Convene the board" round, with the Stop button.
 *
 * Renders nothing once the round is terminal -- the transcript itself is the
 * record at that point, and a finished progress bar is clutter. While it is
 * running, this is the only place a user learns whether Stop actually took:
 * pressing it does not end the round instantly (a live model call cannot be
 * interrupted mid-flight), so the label says "stopping" until the in-flight
 * turn finishes and the status genuinely changes.
 */
function RoundProgress({ job, seatNames, onStop }) {
    if (!job || job.is_terminal) return null

    const speaking = job.current_speaker ? seatNames.get(job.current_speaker) : null
    const stopping = job.cancel_requested

    return (
        <div className={`round-progress${stopping ? ' is-stopping' : ''}`} role="status">
            <span className="round-pulse" aria-hidden="true" />
            <span className="round-label">
                {speaking
                    ? `${speaking} is speaking${job.turn_total ? ` (${job.turn_index}/${job.turn_total})` : ''}`
                    : text(job.progress_label, 'Convening…')}
            </span>
            {!stopping && (
                <button type="button" className="btn-ghost round-stop" onClick={onStop}>
                    Stop
                </button>
            )}
            {stopping && <span className="round-stopping-label">stopping after this turn…</span>}
        </div>
    )
}

/**
 * Assigned work. `message_id` is what separates a task that was actually done
 * from one that is only owned — an item with no linked deliverable renders as
 * outstanding rather than quietly looking the same as a finished one.
 */
function TaskList({ items, seatNames }) {
    const list = Array.isArray(items) ? items : []
    if (list.length === 0) return null

    return (
        <div className="tasks" role="table" aria-label="Assigned tasks">
            <div className="task-row task-head" role="row">
                <span role="columnheader">Task</span>
                <span role="columnheader">Owner</span>
                <span role="columnheader">Priority</span>
                <span role="columnheader">State</span>
            </div>
            {list.map((item) => (
                <div className="task-row" role="row" key={item.id}>
                    <span role="cell">{text(item.task)}</span>
                    <span role="cell">{seatNames.get(item.owner) || text(item.owner)}</span>
                    <span role="cell" className={`priority p-${text(item.priority, 'Medium').toLowerCase()}`}>
                        {text(item.priority, 'Medium')}
                    </span>
                    <span role="cell" className={item.message_id ? 'delivered' : 'outstanding'}>
                        {item.message_id ? 'delivered' : 'outstanding'}
                    </span>
                </div>
            ))}
        </div>
    )
}
