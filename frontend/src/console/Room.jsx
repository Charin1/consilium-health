import React from 'react'

/**
 * The room's two tab bodies: the controls (brief, cap, summon) and the seated
 * info (who, tensions, why). The outer pane shell and tab bar live in
 * App.jsx, because a third tab -- Work Log -- is a different component
 * (WorkLog.jsx) sharing the same shell; no single child owns that chrome.
 *
 * Convene/Stop/Continue is NOT here. It lives in App.jsx's tab bar instead,
 * because it needs to stay reachable from whichever tab is open -- inside
 * the Room tab's own form it used to vanish the moment convening
 * auto-switched the view to Work Log to watch the debate happen.
 *
 * Both bodies always render (never unmounted) and are shown or hidden purely
 * with CSS (`.tab-panel.is-active`). Unmounting on tab switch would be the
 * wrong trade here: nothing heavy lives in these bodies, but it keeps scroll
 * position and any future local state (e.g. a hover) intact across a switch.
 *
 * Two things stay load-bearing regardless of which tab is open:
 *
 * - The tension readout. A room where nobody disagrees will only confirm what
 *   you already think, and that is worth knowing *before* you pay for it.
 * - The cost figure carries its own provenance. The backend says it is an
 *   estimate, so the UI says "est." -- a number that hides that it was modelled
 *   is a number the user will treat as a bill.
 */

function text(value, fallback = '') {
    return typeof value === 'string' ? value : fallback
}

function usd(value) {
    return typeof value === 'number' ? `$${value.toFixed(2)}` : '—'
}

export default function Room({
    activeTab,
    brief,
    onBriefChange,
    cap,
    onCapChange,
    seats,
    tensions,
    rationale,
    chosenBy,
    aiReason,
    cost,
    warning,
    summoning,
    convening,
    error,
    onSummon,
    onClear,
    onDropSeat,
    onInspect,
}) {
    const roomSeats = Array.isArray(seats) ? seats : []
    const roomTensions = Array.isArray(tensions) ? tensions : []
    const rules = Array.isArray(rationale) ? rationale : []

    const contentious = new Set()
    for (const t of roomTensions) {
        contentious.add(t.a)
        contentious.add(t.b)
    }

    const perSeat = cost && Array.isArray(cost.per_seat) ? cost.per_seat : []
    const dearest = perSeat.length > 0 ? perSeat[0] : null

    return (
        <>
            <div className={`tab-panel${activeTab === 'room' ? ' is-active' : ''}`}>
                <form
                    className="brief-form"
                    onSubmit={(e) => {
                        e.preventDefault()
                        onSummon()
                    }}
                >
                    <label className="sr-only" htmlFor="brief">What is the decision?</label>
                    <textarea
                        id="brief"
                        value={brief}
                        placeholder="Describe the decision. e.g. Should we build retrospective risk-adjustment chart chase in-house or buy it?"
                        onChange={(e) => onBriefChange(e.target.value)}
                    />
                    <div className="brief-actions">
                        <button type="submit" className="btn" disabled={!brief.trim() || summoning || convening}>
                            {summoning ? 'Seating…' : 'Summon a room'}
                        </button>
                        <label className="cap-field">
                            Cap
                            <input
                                type="number"
                                min="2"
                                max="16"
                                value={cap}
                                disabled={convening}
                                onChange={(e) => onCapChange(Number(e.target.value))}
                            />
                        </label>
                        {roomSeats.length > 0 && (
                            <button type="button" className="btn-ghost" onClick={onClear} disabled={convening}>
                                Clear
                            </button>
                        )}
                    </div>
                </form>

                {roomSeats.length > 0 && (
                    <div className="readout">
                        <span>
                            Tension <span className={`figure${roomTensions.length === 0 ? ' warn' : ''}`}>
                                {roomTensions.length}
                            </span>
                        </span>
                        <span>
                            Est. cost <span className="figure">{usd(cost && cost.total_usd)}</span>{' '}
                            <span className="est">est.</span>
                        </span>
                        {dearest && (
                            <span>Dearest seat <span className="figure">{text(dearest.name, dearest.id)}</span></span>
                        )}
                        {chosenBy && (
                            <span>
                                Seated by <span className="figure">
                                    {chosenBy === 'ai' ? 'model' : chosenBy === 'rules' ? 'rules' : 'fallback'}
                                </span>
                            </span>
                        )}
                    </div>
                )}

                {error && (
                    <div className="pane-body" style={{ flex: '0 1 auto' }}>
                        <div className="error-banner" role="alert">{text(error)}</div>
                    </div>
                )}
            </div>

            <div className={`tab-panel${activeTab === 'seated' ? ' is-active' : ''}`}>
                <div className="pane-body">
                    {warning && (
                        <p className="notice notice-tension" role="status">{text(warning)}</p>
                    )}

                    {roomSeats.length === 0 && !summoning && (
                        <p className="state">
                            <strong>No room yet</strong>
                            Write a brief and summon one on the Room tab, or click seats in the
                            directory to build it yourself.
                        </p>
                    )}

                    {roomSeats.length > 0 && (
                        <div className="section">
                            <h3>Seated</h3>
                            <div className="block-grid">
                                {roomSeats.map((seat) => (
                                    <article
                                        key={seat.id}
                                        className={`block${contentious.has(seat.id) ? ' has-tension' : ''}`}
                                        onMouseEnter={() => onInspect(seat.id)}
                                    >
                                        <button
                                            type="button"
                                            className="drop"
                                            aria-label={`Remove ${text(seat.name, seat.id)} from the room`}
                                            onClick={() => onDropSeat(seat.id)}
                                        >
                                            ×
                                        </button>
                                        <div className="name">{text(seat.name, seat.id)}</div>
                                        <div className="role">{text(seat.role)}</div>
                                        <div className="why">
                                            {contentious.has(seat.id) ? 'argues here' : text(seat.pack, 'core')}
                                        </div>
                                    </article>
                                ))}
                            </div>
                        </div>
                    )}

                    {roomTensions.length > 0 && (
                        <div className="section">
                            <h3>Live tensions</h3>
                            <ul className="tension-list">
                                {roomTensions.map((t) => (
                                    <li key={`${t.a}-${t.b}`}>
                                        <span>{text(t.a_name, t.a)}</span>
                                        <span className="vs">vs</span>
                                        <span>{text(t.b_name, t.b)}</span>
                                        {!t.mutual && <span className="one-sided">one-sided</span>}
                                    </li>
                                ))}
                            </ul>
                        </div>
                    )}

                    {chosenBy === 'ai' && (
                        <div className="section">
                            <h3>Why these seats</h3>
                            <p className="rationale-row">
                                <span className="rule">ai</span>
                                {aiReason
                                    ? text(aiReason)
                                    : 'No keyword rule matched, so a model picked the room from the roster.'}
                            </p>
                        </div>
                    )}

                    {rules.length > 0 && (
                        <div className="section">
                            <h3>Why these seats</h3>
                            <div className="rationale">
                                {rules.map((r) => (
                                    <p key={r.rule} className="rationale-row">
                                        <span className="rule">{text(r.rule)}</span>
                                        matched{' '}
                                        {(Array.isArray(r.matched) ? r.matched : []).map((m, i) => (
                                            <React.Fragment key={m}>
                                                {i > 0 && ', '}
                                                <span className="term">“{text(m)}”</span>
                                            </React.Fragment>
                                        ))}
                                        {Array.isArray(r.not_available) && r.not_available.length > 0 && (
                                            <> — {r.not_available.length} seat(s) not in the selected packs</>
                                        )}
                                    </p>
                                ))}
                            </div>
                        </div>
                    )}
                </div>
            </div>
        </>
    )
}
