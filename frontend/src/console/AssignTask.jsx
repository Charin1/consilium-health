import React, { useEffect, useRef, useState } from 'react'

/**
 * Assign a piece of work to one seat.
 *
 * Deliberately not a "create task" form. The item and the deliverable arrive
 * together, because a task list where everything is owned and nothing is done
 * is worse than no task list.
 */

function text(value, fallback = '') {
    return typeof value === 'string' ? value : fallback
}

const PRIORITIES = ['High', 'Medium', 'Low']

export default function AssignTask({ seat, open, busy, error, onAssign, onClose }) {
    const [task, setTask] = useState('')
    const [priority, setPriority] = useState('Medium')
    const inputRef = useRef(null)

    useEffect(() => {
        if (open) {
            setTask('')
            setPriority('Medium')
            // Focus the thing the user came here to type into.
            requestAnimationFrame(() => inputRef.current?.focus())
        }
    }, [open, seat])

    useEffect(() => {
        if (!open) return undefined
        const onKey = (e) => { if (e.key === 'Escape') onClose() }
        window.addEventListener('keydown', onKey)
        return () => window.removeEventListener('keydown', onKey)
    }, [open, onClose])

    if (!open || !seat) return null

    const submit = (e) => {
        e.preventDefault()
        const cleaned = task.trim()
        if (!cleaned || busy) return
        onAssign({ task: cleaned, agentId: seat.id, priority })
    }

    return (
        <div className="modal-scrim" role="presentation" onClick={onClose}>
            <div
                className="modal"
                role="dialog"
                aria-modal="true"
                aria-labelledby="assign-title"
                onClick={(e) => e.stopPropagation()}
            >
                <header className="modal-head">
                    <h2 id="assign-title">Assign to {text(seat.name, seat.id)}</h2>
                    <button type="button" className="btn-ghost" onClick={onClose}>Close</button>
                </header>

                <div className="modal-body">
                    {error && <div className="error-banner" role="alert">{text(error)}</div>}

                    <form onSubmit={submit} className="settings-form">
                        <label className="field">
                            <span>The work</span>
                            <textarea
                                ref={inputRef}
                                value={task}
                                rows={4}
                                placeholder={`e.g. Size the RAF lift from closing our top 200 suspect gaps, with the retrieval rate you'd actually assume.`}
                                onChange={(e) => setTask(e.target.value)}
                            />
                            <small>
                                {text(seat.role)} — ask for the deliverable, not an opinion.
                            </small>
                        </label>

                        <label className="field">
                            <span>Priority</span>
                            <select value={priority} onChange={(e) => setPriority(e.target.value)}>
                                {PRIORITIES.map((p) => <option key={p} value={p}>{p}</option>)}
                            </select>
                        </label>

                        <div className="modal-actions">
                            <button type="submit" className="btn" disabled={!task.trim() || busy}>
                                {busy ? 'Working…' : 'Assign and get the work'}
                            </button>
                            <span className="provider-state">
                                Creates a tracked task owned by this seat, and its answer.
                            </span>
                        </div>
                    </form>
                </div>
            </div>
        </div>
    )
}
