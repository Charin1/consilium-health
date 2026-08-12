/**
 * Where every seat sits on the floor, and how the floor maps to the screen.
 *
 * Kept out of the canvas component on purpose: this is arithmetic, it is the
 * part most likely to be wrong, and it can be checked without a rendering
 * context. The component draws what this returns and owns no geometry.
 *
 * Positions are DETERMINISTIC — a seat is in the same place every visit.
 * A floor whose desks move between loads is a floor nobody learns, and
 * learning where the Risk Adjustment Specialist sits is the entire benefit of
 * a spatial view over a list.
 */

/**
 * Tile size drives desk spacing, and desk spacing has to clear the label.
 *
 * In this projection the nearest two desks are TILE_W/2 apart horizontally.
 * The first version used TILE_W = 96, which put neighbours 48px apart while
 * their name cards were 108–148px wide — every card overlapped its neighbour.
 * At 264 the nearest pair is 132px apart with a 66px vertical stagger, which
 * clears a card capped at 124px. Change one of these and check the other.
 */
export const TILE_W = 264
export const TILE_H = 132
export const DESK_H = 30

export const MIN_ZOOM = 0.25
export const MAX_ZOOM = 2.4
export const ZOOM_STEP = 1.25

/**
 * How much detail a card shows, by how far in you are.
 *
 * Below `dot` the labels are unreadable anyway and 44 of them are just noise,
 * so the canvas markers carry the floor on their own. This is the same reason
 * a map drops street names when you zoom out — density has to fall as scale
 * does or the view stops being legible.
 */
export const DETAIL = {
    dot: 0.45,     // below this: canvas markers only
    name: 0.62,    // name only
    full: 0.85,    // name + live status
}

export function detailLevel(scale) {
    if (scale < DETAIL.dot) return 'dot'
    if (scale < DETAIL.name) return 'compact'
    if (scale < DETAIL.full) return 'name'
    return 'full'
}

/**
 * Wings, laid out around the boardroom.
 *
 * Core sits at the origin because every domain pack inherits from it — the
 * arrangement encodes the roster's actual structure rather than decorating it.
 * Each wing's hue matches the pack dots elsewhere in the console, so a seat is
 * the same colour wherever you meet it.
 */
export const ZONES = {
    core: { label: 'Core Boardroom', origin: [0, 0], cols: 5, hue: '#8a93a1' },
    healthcare: { label: 'Consilium Health', origin: [-8, 1], cols: 4, hue: '#2f8f8f' },
    pharma: { label: 'Consilium Pharma', origin: [8, 1], cols: 3, hue: '#7b6bd6' },
    lifesciences: { label: 'Life Sciences', origin: [0, 8], cols: 3, hue: '#4c9a5a' },
}

const FALLBACK_ZONE = { label: 'Unassigned', origin: [0, -7], cols: 4, hue: '#8a93a1' }

/** Isometric projection. Tile space in, floor space out. */
export function project(gx, gy) {
    return {
        x: (gx - gy) * (TILE_W / 2),
        y: (gx + gy) * (TILE_H / 2),
    }
}

/**
 * Deterministic desk positions for a roster.
 *
 * Seats are grouped by pack and laid out in reading order within their wing.
 * Sorting by id rather than by array order means adding a persona to the
 * middle of a manifest does not shuffle everyone else's desk.
 */
export function layoutSeats(seats) {
    const list = Array.isArray(seats) ? seats : []
    const byPack = new Map()

    for (const seat of list) {
        const pack = seat && typeof seat.pack === 'string' ? seat.pack : 'core'
        if (!byPack.has(pack)) byPack.set(pack, [])
        byPack.get(pack).push(seat)
    }

    const placed = []
    for (const [pack, packSeats] of byPack) {
        const zone = ZONES[pack] || FALLBACK_ZONE
        const ordered = [...packSeats].sort((a, b) => String(a.id).localeCompare(String(b.id)))

        ordered.forEach((seat, index) => {
            const col = index % zone.cols
            const row = Math.floor(index / zone.cols)
            const gx = zone.origin[0] + col
            const gy = zone.origin[1] + row
            placed.push({
                ...seat,
                zone: pack,
                zoneLabel: zone.label,
                hue: zone.hue,
                grid: { gx, gy },
                floor: project(gx, gy),
            })
        })
    }
    return placed
}

/** Wing platforms, sized to the seats actually present. */
export function layoutZones(placedSeats) {
    const bounds = new Map()
    for (const seat of placedSeats) {
        const current = bounds.get(seat.zone)
        const { gx, gy } = seat.grid
        if (!current) {
            bounds.set(seat.zone, { minX: gx, maxX: gx, minY: gy, maxY: gy, count: 1 })
            continue
        }
        current.minX = Math.min(current.minX, gx)
        current.maxX = Math.max(current.maxX, gx)
        current.minY = Math.min(current.minY, gy)
        current.maxY = Math.max(current.maxY, gy)
        current.count += 1
    }

    return [...bounds.entries()].map(([zoneId, b]) => {
        const zone = ZONES[zoneId] || FALLBACK_ZONE
        return {
            id: zoneId,
            label: zone.label,
            hue: zone.hue,
            count: b.count,
            corners: [
                project(b.minX - 0.6, b.minY - 0.6),
                project(b.maxX + 0.6, b.minY - 0.6),
                project(b.maxX + 0.6, b.maxY + 0.6),
                project(b.minX - 0.6, b.maxY + 0.6),
            ],
            labelAt: project(b.minX - 0.6, b.minY - 0.6),
        }
    })
}

/** The bounding box of the whole floor, in floor space. */
export function floorBounds(placedSeats) {
    if (placedSeats.length === 0) {
        return { minX: 0, maxX: 0, minY: 0, maxY: 0 }
    }
    const xs = placedSeats.map((s) => s.floor.x)
    const ys = placedSeats.map((s) => s.floor.y)
    return {
        minX: Math.min(...xs) - TILE_W,
        maxX: Math.max(...xs) + TILE_W,
        minY: Math.min(...ys) - TILE_H,
        maxY: Math.max(...ys) + TILE_H,
    }
}

export function clampZoom(scale) {
    return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, scale))
}

/**
 * The transform that fits the whole floor in the viewport.
 *
 * Returned rather than applied so the caller uses the same numbers for the
 * canvas and for the DOM overlay — two different fits would put the labels
 * somewhere other than the desks they name.
 */
export function fitToViewport(placedSeats, width, height, padding = 110) {
    if (placedSeats.length === 0 || width <= 0 || height <= 0) {
        return { scale: 1, offsetX: width / 2, offsetY: height / 2 }
    }
    const b = floorBounds(placedSeats)
    const scale = clampZoom(Math.min(
        (width - padding * 2) / (b.maxX - b.minX),
        (height - padding * 2) / (b.maxY - b.minY),
    ))
    return {
        scale,
        offsetX: width / 2 - ((b.minX + b.maxX) / 2) * scale,
        offsetY: height / 2 - ((b.minY + b.maxY) / 2) * scale,
    }
}

/**
 * The view the floor opens on.
 *
 * NOT fit-everything. 44 seats fitted into a laptop pane lands around 0.34,
 * which is below the threshold where names are legible — so the first thing
 * you would see is forty anonymous dots. A floor whose default view answers
 * nothing is a worse default than one you have to pan.
 *
 * So: open at a readable zoom, centred, and let "Fit" give the overview on
 * demand. This is how every map application behaves, for the same reason.
 */
export const READABLE_ZOOM = 0.78

export function initialView(placedSeats, width, height) {
    const fitted = fitToViewport(placedSeats, width, height)
    if (fitted.scale >= READABLE_ZOOM) return fitted

    const b = floorBounds(placedSeats)
    const scale = READABLE_ZOOM
    return {
        scale,
        offsetX: width / 2 - ((b.minX + b.maxX) / 2) * scale,
        offsetY: height / 2 - ((b.minY + b.maxY) / 2) * scale,
    }
}


/**
 * Zoom about a fixed screen point.
 *
 * The point under the cursor must stay under the cursor, or wheel-zooming
 * feels like the floor is sliding away from you. Solve for the offset that
 * keeps the floor coordinate beneath `(px, py)` unchanged.
 */
export function zoomAbout(transform, nextScale, px, py) {
    const scale = clampZoom(nextScale)
    const floorX = (px - transform.offsetX) / transform.scale
    const floorY = (py - transform.offsetY) / transform.scale
    return {
        scale,
        offsetX: px - floorX * scale,
        offsetY: py - floorY * scale,
    }
}

/** Centre the view on one seat without changing the zoom. */
export function centreOn(transform, seat, width, height) {
    return {
        scale: transform.scale,
        offsetX: width / 2 - seat.floor.x * transform.scale,
        offsetY: height / 2 - seat.floor.y * transform.scale,
    }
}

export function toScreen(point, transform) {
    return {
        x: point.x * transform.scale + transform.offsetX,
        y: point.y * transform.scale + transform.offsetY,
    }
}

/**
 * What a seat is doing, as one of a closed set the floor knows how to draw.
 *
 * `busy` is derived from a real job row, never from "we sent a request a
 * moment ago". A desk that pulses without a job behind it is a fake spinner
 * with extra steps.
 */
export function seatStatus(seatId, jobsBySeat) {
    const job = jobsBySeat ? jobsBySeat[seatId] : null
    if (!job) return { state: 'idle', label: 'Idle', job: null }
    if (job.status === 'queued') return { state: 'queued', label: job.progress_label, job }
    if (job.status === 'running') return { state: 'busy', label: job.progress_label, job }
    if (job.status === 'failed') return { state: 'failed', label: job.progress_label, job }
    return { state: 'done', label: job.progress_label, job }
}
