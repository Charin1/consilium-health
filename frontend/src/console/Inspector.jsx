import React from 'react'

/**
 * Seat dossier. Conflicts are shown in both directions, and the ones already
 * in the room are marked -- that is the answer to "will this seat actually
 * change the debate, or just agree with everyone?".
 */

function text(value, fallback = '') {
    return typeof value === 'string' ? value : fallback
}

export default function Inspector({ seat, loading, error, inRoom, onToggleSeat, onTalkTo, onAssign, busy }) {
    return (
        <section className="pane" aria-label="Seat dossier">
            <div className="pane-head">
                <h2>Inspector</h2>
            </div>
            <div className="pane-body">
                {error && <div className="error-banner" role="alert">{text(error)}</div>}

                {loading && !seat && <div className="skeleton" style={{ height: 120 }} />}

                {!seat && !loading && !error && (
                    <p className="state">
                        <strong>Nothing selected</strong>
                        Hover a seat in the directory to read its dossier.
                    </p>
                )}

                {seat && (
                    <div className="dossier">
                        <h3>{text(seat.name, seat.id)}</h3>
                        <p className="role">{text(seat.role)}</p>

                        <dl className="kv">
                            <dt>Pack</dt>
                            <dd>
                                {text(seat.pack, 'core')}
                                {seat.inherited_from ? ` (inherited by ${text(seat.inherited_from)})` : ''}
                            </dd>
                            <dt>Tier</dt>
                            <dd>{text(seat.tier_label, String(seat.tier ?? ''))}</dd>
                            <dt>Tone</dt>
                            <dd>{text(seat.tone, '—')}</dd>
                            <dt>Tags</dt>
                            <dd>{(Array.isArray(seat.tags) ? seat.tags : []).join(', ') || '—'}</dd>
                        </dl>

                        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                            <button
                                type="button"
                                className={inRoom.has(seat.id) ? 'btn-ghost' : 'btn'}
                                onClick={() => onToggleSeat(seat.id)}
                            >
                                {inRoom.has(seat.id) ? 'Remove from the room' : 'Seat in the room'}
                            </button>
                            {/* A one-to-one is a room of one, in manual turn mode. */}
                            <button
                                type="button"
                                className="btn-ghost"
                                onClick={() => onTalkTo(seat.id)}
                                disabled={busy}
                            >
                                Talk one-on-one
                            </button>
                            <button
                                type="button"
                                className="btn-ghost"
                                onClick={() => onAssign(seat)}
                                disabled={busy}
                            >
                                Assign a task
                            </button>
                        </div>

                        <ConflictBlock
                            title="Argues with"
                            entries={seat.conflicts}
                            inRoom={inRoom}
                            empty="Declares no conflicts — it will not push back on anyone."
                        />
                        <ConflictBlock
                            title="Challenged by"
                            entries={seat.conflicted_by}
                            inRoom={inRoom}
                            empty="Nobody is on record challenging this seat."
                        />
                    </div>
                )}
            </div>
        </section>
    )
}

function ConflictBlock({ title, entries, inRoom, empty }) {
    const list = Array.isArray(entries) ? entries : []
    return (
        <div className="section" style={{ marginTop: 16 }}>
            <h3>{title}</h3>
            {list.length === 0 ? (
                <p className="state" style={{ padding: '6px 0', textAlign: 'left' }}>{empty}</p>
            ) : (
                <ul className="conflict-list">
                    {list.map((c) => (
                        <li key={c.id}>
                            <span className="pack-dot" data-pack={text(c.pack, 'core')} aria-hidden="true" />
                            <span className={inRoom.has(c.id) ? 'in-room' : undefined}>
                                {text(c.name, c.id)}
                            </span>
                            {inRoom.has(c.id) && <span className="one-sided">in the room</span>}
                        </li>
                    ))}
                </ul>
            )}
        </div>
    )
}
