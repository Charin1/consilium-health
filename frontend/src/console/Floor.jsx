import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'

import {
    DESK_H,
    MAX_ZOOM,
    MIN_ZOOM,
    TILE_H,
    TILE_W,
    ZOOM_STEP,
    clampZoom,
    detailLevel,
    fitToViewport,
    initialView,
    layoutSeats,
    layoutZones,
    seatStatus,
    toScreen,
    zoomAbout,
} from './floorLayout.js'

/**
 * The floor: the whole org from above, and what everyone is doing right now.
 *
 * Three decisions shape this:
 *
 * **Canvas for the floor, DOM for the seats.** The platforms, grid, and desks
 * are generated geometry and belong on a canvas. Every name and status is a
 * real `<button>` positioned over it — a pure-canvas floor is invisible to a
 * screen reader, unfocusable by keyboard, and its text cannot be selected. The
 * canvas is scenery; the interactive layer is HTML.
 *
 * **Detail falls as you zoom out.** 44 name cards at fit-everything scale is
 * unreadable noise, so below a threshold the cards drop to markers and the
 * canvas carries the floor alone. Same reason a map drops street names.
 *
 * **Motion means something or it does not happen.** A desk pulses because that
 * seat has a running job row, not because motion looks alive. Ambient
 * animation everywhere would bury the one signal the view exists to carry:
 * who is working, and who is stuck. Under `prefers-reduced-motion` the pulse
 * becomes a static ring and nothing is lost but the movement.
 */

const POLL_MS = 2500

function text(value, fallback = '') {
    return typeof value === 'string' ? value : fallback
}

const STATE_COLOR = {
    idle: 'rgba(138, 147, 161, 0.55)',
    queued: '#d99a3f',
    busy: '#2fb3b8',
    done: '#4c9a5a',
    failed: '#c05555',
}

export default function Floor({
    seats,
    jobsBySeat,
    activeCount,
    inRoom,
    tensions,
    onInspect,
    onAssign,
    onTalkTo,
    error,
}) {
    const canvasRef = useRef(null)
    const wrapRef = useRef(null)
    const dragRef = useRef(null)

    const [size, setSize] = useState({ width: 0, height: 0 })
    const [transform, setTransform] = useState(null)
    const [hovered, setHovered] = useState(null)
    const [panning, setPanning] = useState(false)

    const placed = layoutSeats(seats)
    const zones = layoutZones(placed)

    // Measure the wrapper rather than the window: the floor lives in a pane.
    useLayoutEffect(() => {
        const el = wrapRef.current
        if (!el) return undefined
        const observer = new ResizeObserver(([entry]) => {
            const { width, height } = entry.contentRect
            setSize({ width, height })
        })
        observer.observe(el)
        return () => observer.disconnect()
    }, [])

    const fit = useCallback(() => {
        if (size.width > 0 && placed.length > 0) {
            setTransform(fitToViewport(placed, size.width, size.height))
        }
    }, [placed, size])

    // Set the opening view once the roster and the viewport are both known.
    // Re-running this on every change would yank the view back while someone
    // is reading a corner of it.
    useEffect(() => {
        if (!transform && size.width > 0 && placed.length > 0) {
            setTransform(initialView(placed, size.width, size.height))
        }
    }, [transform, size, placed])

    const view = transform || initialView(placed, size.width, size.height)
    const detail = detailLevel(view.scale)

    // -- zoom ---------------------------------------------------------------

    const zoomBy = useCallback((factor, px, py) => {
        setTransform((current) => {
            const base = current || fitToViewport(placed, size.width, size.height)
            return zoomAbout(
                base,
                base.scale * factor,
                px ?? size.width / 2,
                py ?? size.height / 2,
            )
        })
    }, [placed, size])

    // Non-passive listener: React's onWheel is passive, so preventDefault
    // inside it is ignored and the page scrolls behind the floor.
    useEffect(() => {
        const el = wrapRef.current
        if (!el) return undefined
        const onWheel = (e) => {
            e.preventDefault()
            const rect = el.getBoundingClientRect()
            // Trackpads emit many small deltas; exponentiating keeps the zoom
            // rate even across a wheel notch and a two-finger swipe.
            const factor = Math.exp(-e.deltaY * 0.0016)
            zoomBy(factor, e.clientX - rect.left, e.clientY - rect.top)
        }
        el.addEventListener('wheel', onWheel, { passive: false })
        return () => el.removeEventListener('wheel', onWheel)
    }, [zoomBy])

    // -- pan ----------------------------------------------------------------

    const onPointerDown = (e) => {
        // Only drag from the floor itself; a card owns its own clicks.
        if (e.target.closest('.floor-card, .floor-tip, .floor-controls')) return
        dragRef.current = { x: e.clientX, y: e.clientY, moved: false }
        setPanning(true)
        e.currentTarget.setPointerCapture(e.pointerId)
    }

    const onPointerMove = (e) => {
        const drag = dragRef.current
        if (!drag) return
        const dx = e.clientX - drag.x
        const dy = e.clientY - drag.y
        drag.x = e.clientX
        drag.y = e.clientY
        setTransform((current) => {
            const base = current || view
            return { ...base, offsetX: base.offsetX + dx, offsetY: base.offsetY + dy }
        })
    }

    const endPan = (e) => {
        if (!dragRef.current) return
        dragRef.current = null
        setPanning(false)
        if (e.currentTarget.hasPointerCapture?.(e.pointerId)) {
            e.currentTarget.releasePointerCapture(e.pointerId)
        }
    }

    const onKeyDown = (e) => {
        const nudge = 60
        const moves = {
            ArrowUp: [0, nudge], ArrowDown: [0, -nudge],
            ArrowLeft: [nudge, 0], ArrowRight: [-nudge, 0],
        }
        if (moves[e.key]) {
            e.preventDefault()
            const [dx, dy] = moves[e.key]
            setTransform((c) => {
                const base = c || view
                return { ...base, offsetX: base.offsetX + dx, offsetY: base.offsetY + dy }
            })
        } else if (e.key === '+' || e.key === '=') {
            e.preventDefault(); zoomBy(ZOOM_STEP)
        } else if (e.key === '-' || e.key === '_') {
            e.preventDefault(); zoomBy(1 / ZOOM_STEP)
        } else if (e.key === '0') {
            e.preventDefault(); fit()
        }
    }

    // -- draw ---------------------------------------------------------------

    const draw = useCallback((time) => {
        const canvas = canvasRef.current
        if (!canvas || size.width === 0) return

        // Back the canvas at device resolution or every edge is soft on a
        // retina display — the "blurry floor" that fixed-size canvases give.
        const dpr = window.devicePixelRatio || 1
        if (canvas.width !== Math.round(size.width * dpr)) {
            canvas.width = Math.round(size.width * dpr)
            canvas.height = Math.round(size.height * dpr)
        }
        const ctx = canvas.getContext('2d')
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
        ctx.clearRect(0, 0, size.width, size.height)

        const { scale, offsetX, offsetY } = view
        const at = (p) => ({ x: p.x * scale + offsetX, y: p.y * scale + offsetY })

        // -- wing platforms ------------------------------------------------
        for (const zone of zones) {
            const pts = zone.corners.map(at)
            ctx.beginPath()
            ctx.moveTo(pts[0].x, pts[0].y)
            for (const p of pts.slice(1)) ctx.lineTo(p.x, p.y)
            ctx.closePath()
            ctx.fillStyle = `${zone.hue}12`
            ctx.fill()
            ctx.strokeStyle = `${zone.hue}55`
            ctx.lineWidth = 1
            ctx.stroke()
        }

        // -- declared tensions, under the desks so desks stay readable ------
        if (Array.isArray(tensions) && tensions.length > 0) {
            const byId = new Map(placed.map((s) => [s.id, s]))
            ctx.save()
            ctx.setLineDash([6, 7])
            ctx.strokeStyle = 'rgba(217, 154, 63, 0.5)'
            ctx.lineWidth = 1.5
            for (const t of tensions) {
                const a = byId.get(t.a)
                const b = byId.get(t.b)
                if (!a || !b) continue
                const pa = at(a.floor)
                const pb = at(b.floor)
                ctx.beginPath()
                ctx.moveTo(pa.x, pa.y)
                ctx.lineTo(pb.x, pb.y)
                ctx.stroke()
            }
            ctx.restore()
        }

        // -- desks, back to front so the overlap reads correctly ------------
        const ordered = [...placed].sort((a, b) => a.floor.y - b.floor.y)
        const w = (TILE_W * 0.42) * scale
        const h = (TILE_H * 0.42) * scale
        const lift = DESK_H * scale

        for (const seat of ordered) {
            const p = at(seat.floor)
            // Skip anything off screen. At full zoom most of a 44-seat floor
            // is outside the viewport and drawing it is wasted work.
            if (p.x < -200 || p.x > size.width + 200 || p.y < -200 || p.y > size.height + 200) {
                continue
            }

            const status = seatStatus(seat.id, jobsBySeat)
            const seated = inRoom && inRoom.has(seat.id)

            if (status.state === 'busy') {
                const pulse = 0.5 + 0.5 * Math.sin(time / 380)
                const r = Math.max(18, w * 1.6) * (0.85 + pulse * 0.22)
                const glow = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, r)
                glow.addColorStop(0, `rgba(47, 179, 184, ${0.3 + pulse * 0.2})`)
                glow.addColorStop(1, 'rgba(47, 179, 184, 0)')
                ctx.fillStyle = glow
                ctx.beginPath()
                ctx.arc(p.x, p.y, r, 0, Math.PI * 2)
                ctx.fill()
            }

            const top = [
                { x: p.x, y: p.y - h - lift },
                { x: p.x + w, y: p.y - lift },
                { x: p.x, y: p.y + h - lift },
                { x: p.x - w, y: p.y - lift },
            ]
            ctx.beginPath()
            ctx.moveTo(top[0].x, top[0].y)
            for (const c of top.slice(1)) ctx.lineTo(c.x, c.y)
            ctx.closePath()
            ctx.fillStyle = seated ? `${seat.hue}50` : `${seat.hue}20`
            ctx.fill()
            ctx.strokeStyle = seated ? seat.hue : `${seat.hue}80`
            ctx.lineWidth = seated ? 1.6 : 1
            ctx.stroke()

            ctx.beginPath()
            ctx.moveTo(top[3].x, top[3].y)
            ctx.lineTo(top[2].x, top[2].y)
            ctx.lineTo(p.x, p.y + h)
            ctx.lineTo(p.x - w, p.y)
            ctx.closePath()
            ctx.fillStyle = 'rgba(0, 0, 0, 0.32)'
            ctx.fill()

            ctx.beginPath()
            ctx.moveTo(top[2].x, top[2].y)
            ctx.lineTo(top[1].x, top[1].y)
            ctx.lineTo(p.x + w, p.y)
            ctx.lineTo(p.x, p.y + h)
            ctx.closePath()
            ctx.fillStyle = 'rgba(0, 0, 0, 0.18)'
            ctx.fill()

            // Status marker. Grows a little when it is the only thing left,
            // because at that zoom it is carrying the whole floor.
            const markerR = detail === 'dot' ? Math.max(4, 7 * scale) : Math.max(3.5, 5 * scale)
            ctx.beginPath()
            ctx.arc(p.x, p.y - lift - h * 0.6, markerR, 0, Math.PI * 2)
            ctx.fillStyle = STATE_COLOR[status.state] || STATE_COLOR.idle
            ctx.fill()
            if (status.state !== 'idle') {
                ctx.strokeStyle = 'rgba(6, 12, 16, 0.85)'
                ctx.lineWidth = 1.5
                ctx.stroke()
            }
        }
    }, [placed, zones, view, jobsBySeat, inRoom, tensions, size, detail])

    useEffect(() => {
        const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
        let raf = null
        let cancelled = false

        if (activeCount > 0 && !reduced) {
            const loop = (t) => {
                if (cancelled) return
                draw(t)
                raf = requestAnimationFrame(loop)
            }
            raf = requestAnimationFrame(loop)
        } else {
            draw(0)
        }
        return () => {
            cancelled = true
            if (raf) cancelAnimationFrame(raf)
        }
    }, [draw, activeCount])

    // Cards scale with the floor but stay legible at both ends. Scaling the
    // whole card rather than restyling per zoom keeps the text crisp, since a
    // CSS transform on text still renders as vectors.
    const cardScale = Math.min(1.3, Math.max(0.78, view.scale))
    const zoomPercent = Math.round(view.scale * 100)

    return (
        <div
            className={`floor${panning ? ' is-panning' : ''}`}
            ref={wrapRef}
            role="application"
            aria-label="Org floor. Arrow keys pan, plus and minus zoom, zero fits."
            tabIndex={0}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={endPan}
            onPointerCancel={endPan}
            onKeyDown={onKeyDown}
        >
            <canvas ref={canvasRef} style={{ width: '100%', height: '100%' }} aria-hidden="true" />

            {error && <div className="error-banner floor-error" role="alert">{text(error)}</div>}

            {zones.map((zone) => {
                const p = toScreen(zone.labelAt, view)
                return (
                    <span
                        key={zone.id}
                        className="floor-zone-label"
                        style={{ left: p.x, top: p.y - 20, color: zone.hue }}
                    >
                        {zone.label} · {zone.count}
                    </span>
                )
            })}

            {/*
              * The interactive layer. Real buttons, in the tab order, over the
              * canvas — so the floor is operable without a mouse and legible
              * to a screen reader even when the cards are hidden.
              */}
            <ul className={`floor-seats detail-${detail}`}>
                {placed.map((seat) => {
                    const p = toScreen(seat.floor, view)
                    const status = seatStatus(seat.id, jobsBySeat)
                    const offscreen =
                        p.x < -180 || p.x > size.width + 180 ||
                        p.y < -180 || p.y > size.height + 180
                    if (offscreen) return null

                    return (
                        <li
                            key={seat.id}
                            className="floor-seat"
                            style={{
                                left: p.x,
                                top: p.y - DESK_H * view.scale - 30 * cardScale,
                                '--card-scale': cardScale,
                            }}
                        >
                            <button
                                type="button"
                                className={`floor-card is-${status.state}${inRoom && inRoom.has(seat.id) ? ' is-seated' : ''}`}
                                onMouseEnter={() => { setHovered(seat.id); onInspect(seat.id) }}
                                onFocus={() => { setHovered(seat.id); onInspect(seat.id) }}
                                onMouseLeave={() => setHovered(null)}
                                onBlur={() => setHovered(null)}
                                onClick={() => onAssign(seat)}
                                aria-label={`${text(seat.name, seat.id)}, ${text(seat.role)}. ${text(status.label, 'Idle')}. Assign a task.`}
                            >
                                <span className="floor-name">{text(seat.name, seat.id)}</span>
                                <span className="floor-status">{text(status.label, 'Idle')}</span>
                            </button>

                            {hovered === seat.id && (
                                <div className="floor-tip" role="tooltip">
                                    <strong>{text(seat.name, seat.id)}</strong>
                                    <span className="floor-tip-role">{text(seat.role)}</span>
                                    <span className={`floor-tip-state is-${status.state}`}>
                                        {text(status.label, 'Idle')}
                                        {status.job && status.job.duration_ms
                                            ? ` · ${(status.job.duration_ms / 1000).toFixed(1)}s`
                                            : ''}
                                    </span>
                                    {status.job && (
                                        <span className="floor-tip-job">
                                            “{text(status.job.brief).slice(0, 100)}”
                                        </span>
                                    )}
                                    {status.job && status.job.error_detail && (
                                        <span className="floor-tip-error">
                                            {text(status.job.error_detail).slice(0, 120)}
                                        </span>
                                    )}
                                    <div className="floor-tip-actions">
                                        <button
                                            type="button"
                                            className="btn-ghost"
                                            onClick={(e) => { e.stopPropagation(); onTalkTo(seat.id) }}
                                        >
                                            Talk
                                        </button>
                                        <button
                                            type="button"
                                            className="btn-ghost"
                                            onClick={(e) => { e.stopPropagation(); onAssign(seat) }}
                                        >
                                            Assign
                                        </button>
                                    </div>
                                </div>
                            )}
                        </li>
                    )
                })}
            </ul>

            <div className="floor-controls">
                <button
                    type="button"
                    onClick={() => zoomBy(1 / ZOOM_STEP)}
                    disabled={view.scale <= MIN_ZOOM + 0.001}
                    aria-label="Zoom out"
                >
                    −
                </button>
                <span className="zoom-readout" aria-live="off">{zoomPercent}%</span>
                <button
                    type="button"
                    onClick={() => zoomBy(ZOOM_STEP)}
                    disabled={view.scale >= MAX_ZOOM - 0.001}
                    aria-label="Zoom in"
                >
                    +
                </button>
                <button type="button" className="fit" onClick={fit}>Fit</button>
            </div>

            <div className="floor-legend">
                {['idle', 'queued', 'busy', 'done', 'failed'].map((state) => (
                    <span key={state}>
                        <i style={{ background: STATE_COLOR[state] }} aria-hidden="true" />
                        {state}
                    </span>
                ))}
            </div>
        </div>
    )
}

export { POLL_MS }
