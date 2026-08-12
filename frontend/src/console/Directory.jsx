import React, { useMemo, useState } from 'react'

/**
 * The roster, grouped by tier. Clicking a seat toggles it into the room;
 * the name opens its dossier in the inspector.
 *
 * Filtering happens here rather than server-side: the whole directory is
 * 44 rows and already loaded, so a round trip per keystroke would be slower
 * and no more correct.
 */

const TIER_ORDER = [0, 1, 2, 3, 4]

function text(value, fallback = '') {
    return typeof value === 'string' ? value : fallback
}

export default function Directory({
    seats,
    loading,
    error,
    inRoom,
    inspectedId,
    onToggleSeat,
    onInspect,
    onRetry,
}) {
    const [query, setQuery] = useState('')
    const [activeTag, setActiveTag] = useState(null)

    const safeSeats = Array.isArray(seats) ? seats : []

    const tags = useMemo(() => {
        const counts = new Map()
        for (const seat of safeSeats) {
            for (const tag of Array.isArray(seat.tags) ? seat.tags : []) {
                counts.set(tag, (counts.get(tag) || 0) + 1)
            }
        }
        return [...counts.entries()]
            .filter(([tag, n]) => n > 1 && tag !== 'all')
            .sort((a, b) => b[1] - a[1])
            .slice(0, 12)
            .map(([tag]) => tag)
    }, [safeSeats])

    const filtered = useMemo(() => {
        const needle = query.trim().toLowerCase()
        return safeSeats.filter((seat) => {
            const seatTags = Array.isArray(seat.tags) ? seat.tags : []
            if (activeTag && !seatTags.includes(activeTag)) return false
            if (!needle) return true
            return (
                text(seat.name).toLowerCase().includes(needle) ||
                text(seat.role).toLowerCase().includes(needle) ||
                seatTags.some((t) => text(t).toLowerCase().includes(needle))
            )
        })
    }, [safeSeats, query, activeTag])

    const grouped = useMemo(() => {
        const byTier = new Map()
        for (const seat of filtered) {
            const tier = typeof seat.tier === 'number' ? seat.tier : 2
            if (!byTier.has(tier)) byTier.set(tier, [])
            byTier.get(tier).push(seat)
        }
        return TIER_ORDER.filter((t) => byTier.has(t)).map((t) => ({
            tier: t,
            label: text(byTier.get(t)[0].tier_label, `Tier ${t}`),
            seats: byTier.get(t),
        }))
    }, [filtered])

    return (
        <section className="pane" aria-label="Roster directory">
            <div className="pane-head">
                <h2>Directory</h2>
                <span className="tally">
                    {filtered.length}
                    {filtered.length !== safeSeats.length ? ` / ${safeSeats.length}` : ''} seats
                </span>
            </div>

            <div className="filters">
                <label className="sr-only" htmlFor="seat-search">Search the roster</label>
                <input
                    id="seat-search"
                    type="search"
                    placeholder="Search name, role, or tag…"
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                />
                {tags.length > 0 && (
                    <div className="tag-row">
                        {tags.map((tag) => (
                            <button
                                key={tag}
                                type="button"
                                className="tag"
                                aria-pressed={activeTag === tag}
                                onClick={() => setActiveTag(activeTag === tag ? null : tag)}
                            >
                                {tag}
                            </button>
                        ))}
                    </div>
                )}
            </div>

            <div className="pane-body">
                {error && (
                    <div className="error-banner" role="alert">
                        <span>{text(error, 'Could not load the roster.')}</span>
                        <button type="button" className="btn-ghost" onClick={onRetry}>Retry</button>
                    </div>
                )}

                {loading && (
                    <div aria-busy="true" aria-label="Loading the roster">
                        {[0, 1, 2, 3, 4, 5].map((i) => <div key={i} className="skeleton" />)}
                    </div>
                )}

                {!loading && !error && filtered.length === 0 && (
                    <p className="state">
                        <strong>No seat matches</strong>
                        Try a different term, or clear the tag filter.
                    </p>
                )}

                {grouped.map((group) => (
                    <div key={group.tier}>
                        <h3 className="tier-heading">{group.label}</h3>
                        {group.seats.map((seat) => {
                            const seated = inRoom.has(seat.id)
                            return (
                                <button
                                    key={seat.id}
                                    type="button"
                                    className={`seat${seat.id === inspectedId ? ' is-inspected' : ''}`}
                                    aria-pressed={seated}
                                    onClick={() => onToggleSeat(seat.id)}
                                    onFocus={() => onInspect(seat.id)}
                                    onMouseEnter={() => onInspect(seat.id)}
                                    title={seated ? 'Remove from the room' : 'Add to the room'}
                                >
                                    <span className="name">{text(seat.name, seat.id)}</span>
                                    <span className="role">{text(seat.role)}</span>
                                    <span className="meta">
                                        <span className="pack-dot" data-pack={text(seat.pack, 'core')} aria-hidden="true" />
                                        {text(seat.pack, 'core')}
                                        {seat.inherited_from ? ' · inherited' : ''}
                                    </span>
                                </button>
                            )
                        })}
                    </div>
                ))}
            </div>
        </section>
    )
}
