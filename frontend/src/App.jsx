import React, { useCallback, useEffect, useMemo, useState } from 'react'

import Directory from './console/Directory.jsx'
import Inspector from './console/Inspector.jsx'
import Room from './console/Room.jsx'
import AssignTask from './console/AssignTask.jsx'
import Floor, { POLL_MS } from './console/Floor.jsx'
import History from './console/History.jsx'
import Settings from './console/Settings.jsx'
import WorkLog from './console/WorkLog.jsx'
import {
    assignTask,
    assignTaskAsync,
    conveneAsync,
    createChatSession,
    getCurrentRound,
    getRound,
    listJobs,
    getOrgSeat,
    getChatSession,
    getSettings,
    inspectTensions,
    listOrgPacks,
    listOrgSeats,
    sendChatMessage,
    stopRound,
    summonRoom,
    synthesizeSession,
} from './lib/apiClient.js'
import './console/console.css'

/**
 * Consilium org console.
 *
 * One screen, three panes: pick seats from the directory, see the room and
 * whether it will actually argue, read the dossier of whatever you are
 * pointing at. The brief either summons a room through the deterministic
 * router or you assemble one by hand -- both paths land in the same place.
 *
 * State discipline (frontend.md #3): the roster and the session come from the
 * server and are refetched rather than mirrored. The only real client state is
 * the selection, the brief, and which seat is being inspected.
 */

const DEFAULT_CAP = 8
const LAST_SESSION_KEY = 'consilium.lastSession'

/**
 * Remember which session was open across reloads.
 *
 * Only the id is stored — the session itself is server data and is refetched.
 * Caching the transcript in the browser would let a stale copy outlive the
 * real one, which is the bug that makes people distrust a history view.
 */
function rememberSession(id) {
    try {
        if (id) window.localStorage.setItem(LAST_SESSION_KEY, id)
        else window.localStorage.removeItem(LAST_SESSION_KEY)
    } catch {
        // Private browsing and blocked storage are both fine: the session
        // still works, it just does not survive a refresh.
    }
}

function lastSessionId() {
    try {
        return window.localStorage.getItem(LAST_SESSION_KEY)
    } catch {
        return null
    }
}

function errorMessage(err, fallback) {
    if (err && typeof err.message === 'string' && err.message) return err.message
    return fallback
}

export default function App() {
    // -- roster ------------------------------------------------------------
    const [packs, setPacks] = useState([])
    const [selectedPacks, setSelectedPacks] = useState([])
    const [seats, setSeats] = useState([])
    const [disclaimer, setDisclaimer] = useState('')
    const [rosterLoading, setRosterLoading] = useState(true)
    const [rosterError, setRosterError] = useState(null)

    // -- room --------------------------------------------------------------
    const [brief, setBrief] = useState('')
    const [cap, setCap] = useState(DEFAULT_CAP)
    const [roomIds, setRoomIds] = useState([])
    const [tensions, setTensions] = useState([])
    const [tensionWarning, setTensionWarning] = useState(null)
    const [cost, setCost] = useState(null)
    const [rationale, setRationale] = useState([])
    const [summoning, setSummoning] = useState(false)
    const [chosenBy, setChosenBy] = useState(null)
    const [aiReason, setAiReason] = useState(null)
    const [roomError, setRoomError] = useState(null)

    // -- inspector ---------------------------------------------------------
    const [inspectedId, setInspectedId] = useState(null)
    const [dossier, setDossier] = useState(null)
    const [dossierError, setDossierError] = useState(null)

    // -- session -----------------------------------------------------------
    const [messages, setMessages] = useState([])
    const [dispatching, setDispatching] = useState(false)
    const [sessionError, setSessionError] = useState(null)
    const [target, setTarget] = useState(null)
    const [oneOnOne, setOneOnOne] = useState(null)
    const [sessionId, setSessionId] = useState(null)

    /**
     * The in-progress "Convene the board" round, if any.
     *
     * `null` means no round is being watched. Once set, an effect polls it
     * until `is_terminal` -- the same shape as the Floor's job polling, for
     * the same reason: the transcript is server data, refetched, never
     * mirrored client-side beyond what a poll tick just returned.
     */
    const [roundJob, setRoundJob] = useState(null)
    const convening = Boolean(roundJob) && !roundJob.is_terminal

    // -- settings ----------------------------------------------------------
    const [settingsOpen, setSettingsOpen] = useState(false)
    const [providerState, setProviderState] = useState(null)

    // -- assignment --------------------------------------------------------
    const [assignTo, setAssignTo] = useState(null)
    const [actionItems, setActionItems] = useState([])

    // -- middle panel tabs ---------------------------------------------------
    // Room / Seated / Work Log used to share one column via a fixed 58/42
    // vertical split, which meant each competed with the others for a slice
    // of a squeezed height -- the Seated grid could be crushed to nothing by
    // the brief-form above it, and the transcript could visually run into
    // whatever sat below it. A tab gives the active one the ENTIRE panel.
    const [middleTab, setMiddleTab] = useState('room')

    // -- floor -------------------------------------------------------------
    const [view, setView] = useState('console')
    const [jobsBySeat, setJobsBySeat] = useState({})
    const [activeJobs, setActiveJobs] = useState(0)
    const [jobsError, setJobsError] = useState(null)

    /**
     * Reopen a past session in the console.
     *
     * The console is the surface with a composer, so resuming switches to it
     * rather than trying to make History editable. One place to talk.
     */
    const resumeSession = useCallback((session) => {
        setSessionId(session.id)
        setMessages(Array.isArray(session.messages) ? session.messages : [])
        setActionItems(Array.isArray(session.action_items) ? session.action_items : [])
        setRoomIds(Array.isArray(session.selected_agent_ids) ? session.selected_agent_ids : [])
        setOneOnOne(null)
        setTarget(null)
        setView('console')
        setMiddleTab('log')
        // A round convened before this reload may still be running -- 404
        // just means nothing has ever convened here, which is the common case.
        getCurrentRound(session.id)
            .then((job) => setRoundJob(job.is_terminal ? null : job))
            .catch(() => setRoundJob(null))
    }, [])

    useEffect(() => { rememberSession(sessionId) }, [sessionId])

    const inRoom = useMemo(() => new Set(roomIds), [roomIds])
    const seatsById = useMemo(() => new Map(seats.map((s) => [s.id, s])), [seats])
    const seatNames = useMemo(
        () => new Map(seats.map((s) => [s.id, s.name])),
        [seats],
    )
    const roomSeats = useMemo(
        () => roomIds.map((id) => seatsById.get(id)).filter(Boolean),
        [roomIds, seatsById],
    )

    /**
     * Which packs a session actually needs, given the seats going into it.
     *
     * Not just `selectedPacks`. The Directory shows every seat in the org the
     * moment no pack chip is toggled -- `GET /api/org/seats` with no `packs`
     * param returns the whole roster, by design (org_service.normalize_packs:
     * empty means "the whole org"). But `POST .../sessions` reads an absent
     * `persona_packs` the other way -- empty means `["core"]` only, because a
     * deployment with no session-level override should behave exactly as it
     * did before packs existed. Same "nothing specified" input, two correct
     * but *opposite* defaults in two services that were never meant to agree
     * on it.
     *
     * The seam: a user can click a healthcare seat straight off an untouched
     * Directory (all 44 seats are right there), then hit Convene/Assign/Talk
     * and get "Unknown agent(s): ..." -- the session silently scoped itself to
     * core-only underneath a room that never was. Fixed by deriving the packs
     * to send from the seats actually being used, which is unambiguous
     * regardless of whether a chip was ever toggled.
     */
    const packsFor = useCallback((seatIds) => {
        const union = new Set(selectedPacks)
        for (const id of seatIds) {
            const pack = seatsById.get(id)?.pack
            if (pack) union.add(pack)
        }
        return union.size > 0 ? [...union] : undefined
    }, [selectedPacks, seatsById])

    // -- load packs + roster -----------------------------------------------

    const loadRoster = useCallback(async (activePacks) => {
        setRosterLoading(true)
        setRosterError(null)
        try {
            // Parallel, not a waterfall: the route needs both on load.
            const [packBody, seatBody] = await Promise.all([
                listOrgPacks(),
                listOrgSeats({ packs: activePacks }),
            ])
            setPacks(Array.isArray(packBody.packs) ? packBody.packs : [])
            setSeats(Array.isArray(seatBody.seats) ? seatBody.seats : [])
            setDisclaimer(typeof seatBody.disclaimer === 'string' ? seatBody.disclaimer : '')
        } catch (err) {
            setRosterError(errorMessage(err, 'Could not reach the backend. Is it running on :8000?'))
        } finally {
            setRosterLoading(false)
        }
    }, [])

    useEffect(() => {
        loadRoster(selectedPacks)
    }, [loadRoster, selectedPacks])

    // Reopen whatever was last open. A 404 means it was deleted elsewhere;
    // forget it rather than showing an error for something the user did.
    useEffect(() => {
        const id = lastSessionId()
        if (!id) return undefined
        let cancelled = false
        getChatSession(id)
            .then((session) => {
                if (cancelled || !session) return
                setSessionId(session.id)
                setMessages(Array.isArray(session.messages) ? session.messages : [])
                setActionItems(Array.isArray(session.action_items) ? session.action_items : [])
                return getCurrentRound(session.id)
                    .then((job) => { if (!cancelled) setRoundJob(job.is_terminal ? null : job) })
                    .catch(() => { if (!cancelled) setRoundJob(null) })
            })
            .catch(() => rememberSession(null))
        return () => { cancelled = true }
    }, [])

    useEffect(() => {
        let cancelled = false
        getSettings()
            .then((body) => { if (!cancelled) setProviderState(body.current || null) })
            .catch(() => { /* the masthead just stays quiet about the provider */ })
        return () => { cancelled = true }
    }, [])

    // Seats that leave the selected packs must leave the room with them,
    // or the room holds ids the session can no longer seat.
    useEffect(() => {
        setRoomIds((current) => {
            const kept = current.filter((id) => seatsById.has(id))
            return kept.length === current.length ? current : kept
        })
    }, [seatsById])

    // -- dossier -----------------------------------------------------------

    useEffect(() => {
        if (!inspectedId) {
            setDossier(null)
            return undefined
        }
        let cancelled = false
        setDossierError(null)
        getOrgSeat(inspectedId)
            .then((body) => { if (!cancelled) setDossier(body) })
            .catch((err) => { if (!cancelled) setDossierError(errorMessage(err, 'Could not load that seat.')) })
        return () => { cancelled = true }
    }, [inspectedId])

    // -- tension, recomputed whenever the room changes ----------------------

    useEffect(() => {
        if (roomIds.length === 0) {
            setTensions([])
            setTensionWarning(null)
            setCost(null)
            return undefined
        }
        let cancelled = false
        inspectTensions({ seatIds: roomIds, packs: selectedPacks })
            .then((body) => {
                if (cancelled) return
                setTensions(Array.isArray(body.tensions) ? body.tensions : [])
                setTensionWarning(body.warning || null)
                setCost(body.cost_estimate || null)
            })
            .catch(() => { /* the readout degrades to blank; the room still works */ })
        return () => { cancelled = true }
    }, [roomIds, selectedPacks])

    /**
     * Poll what every seat is doing.
     *
     * Only while the floor is open: a background poll on a view nobody is
     * looking at is pure cost. The cancel flag is checked after the await so a
     * response that lands during teardown cannot write to a dead component.
     */
    useEffect(() => {
        if (view !== 'floor') return undefined
        let cancelled = false

        const tick = async () => {
            try {
                const body = await listJobs({})
                if (cancelled) return
                setJobsBySeat(body.by_seat || {})
                setActiveJobs(body.active || 0)
                setJobsError(null)
            } catch (err) {
                if (cancelled) return
                setJobsError(errorMessage(err, 'Lost contact with the work queue.'))
            }
        }

        const timer = setInterval(tick, POLL_MS)
        tick()
        return () => { cancelled = true; clearInterval(timer) }
    }, [view])

    // -- actions -----------------------------------------------------------

    const toggleSeat = useCallback((seatId) => {
        setRoomIds((current) =>
            current.includes(seatId)
                ? current.filter((id) => id !== seatId)
                : [...current, seatId],
        )
    }, [])

    const togglePack = useCallback((packId) => {
        setSelectedPacks((current) =>
            current.includes(packId)
                ? current.filter((p) => p !== packId)
                : [...current, packId],
        )
    }, [])

    const handleSummon = useCallback(async () => {
        if (!brief.trim()) return
        setSummoning(true)
        setRoomError(null)
        try {
            const body = await summonRoom({
                brief: brief.trim(),
                packs: selectedPacks.length > 0 ? selectedPacks : undefined,
                cap,
            })
            setRoomIds(Array.isArray(body.seat_ids) ? body.seat_ids : [])
            setRationale(Array.isArray(body.rationale) ? body.rationale : [])
            setTensions(Array.isArray(body.tensions) ? body.tensions : [])
            setCost(body.cost_estimate || null)
            setTensionWarning(null)
            setChosenBy(body.chosen_by || 'rules')
            setAiReason(body.ai_reason || null)
            setMiddleTab('seated')
            // Only the true fallback is a problem worth an error banner. An
            // AI-picked room is a normal outcome, just a differently sourced
            // one, and the readout labels it.
            if (body.chosen_by === 'fallback') {
                setRoomError(
                    body.ai_error
                        ? `No rule matched and the model could not be reached (${body.ai_error}), ` +
                          'so the executive bench was seated. Add specialists by hand, or configure ' +
                          'a provider in Settings.'
                        : 'No rule matched this brief, so the executive bench was seated. ' +
                          'Add specialists by hand, or say more about the decision.',
                )
            }
        } catch (err) {
            setRoomError(errorMessage(err, 'Could not seat a room.'))
        } finally {
            setSummoning(false)
        }
    }, [brief, cap, selectedPacks])

    const handleClear = useCallback(() => {
        setRoomIds([])
        setRationale([])
        setChosenBy(null)
        setAiReason(null)
        setRoomError(null)
        setMessages([])
        setActionItems([])
        setTarget(null)
        setOneOnOne(null)
        setSessionId(null)
        setRoundJob(null)
        setMiddleTab('room')
    }, [])

    /**
     * Convene the board without blocking on the whole debate.
     *
     * `dispatching` now only covers the instant part -- creating the session
     * and firing the convene request, which returns as soon as the round is
     * queued. `convening` (derived from `roundJob`) covers the round itself,
     * which a poll effect below tracks turn by turn.
     */
    const handleDispatch = useCallback(async () => {
        const speakers = roomIds.filter((id) => id !== 'moderator')
        if (speakers.length === 0 || !brief.trim()) return
        setDispatching(true)
        setSessionError(null)
        try {
            const session = await createChatSession({
                title: brief.trim().slice(0, 120),
                selected_agent_ids: speakers,
                persona_packs: packsFor(speakers),
                turn_mode: 'automatic',
            })
            setSessionId(session.id)
            setOneOnOne(null)
            setTarget(null)
            if (typeof session.disclaimer === 'string' && session.disclaimer) {
                setDisclaimer(session.disclaimer)
            }
            const job = await conveneAsync(session.id, { message: brief.trim() })
            setRoundJob(job)
            setMiddleTab('log')
        } catch (err) {
            setSessionError(errorMessage(err, 'The board could not convene.'))
        } finally {
            setDispatching(false)
        }
    }, [brief, roomIds, packsFor])

    /**
     * Poll a convening round: its own status, and the growing transcript.
     *
     * Stops itself once the round is terminal -- `roundJob.is_terminal`
     * flipping true is what drops this effect's dependency and lets the next
     * run exit without scheduling another interval. Same shape as the Floor's
     * job polling and for the same reason (frontend.md #3): the transcript is
     * refetched from the server on every tick, never mirrored beyond that.
     */
    useEffect(() => {
        if (!roundJob || roundJob.is_terminal || !sessionId) return undefined
        // Depend on the job's id, not the job object: `tick` calls
        // `setRoundJob` on every poll, and if the effect depended on that
        // object it would tear down and rebuild its own interval on every
        // tick -- firing back-to-back instead of every 1.5s. The id is stable
        // for the life of one round; only a new round (a new id) should
        // restart this effect.
        const jobId = roundJob.id
        let cancelled = false
        let timer = null

        const tick = async () => {
            try {
                const [job, session] = await Promise.all([
                    getRound(jobId),
                    getChatSession(sessionId),
                ])
                if (cancelled) return
                setRoundJob(job)
                setMessages(Array.isArray(session.messages) ? session.messages : [])
                setActionItems(Array.isArray(session.action_items) ? session.action_items : [])
                if (job.status === 'failed' && job.error_detail) {
                    setSessionError(job.error_detail)
                }
                if (job.is_terminal && timer) {
                    clearInterval(timer)
                    timer = null
                }
            } catch (err) {
                if (!cancelled) setSessionError(errorMessage(err, 'Lost contact with the debate.'))
            }
        }

        timer = setInterval(tick, 1500)
        tick()
        return () => { cancelled = true; if (timer) clearInterval(timer) }
    }, [roundJob?.id, sessionId])

    /**
     * Ask a running round to stop. Not instant: a live model call cannot be
     * interrupted mid-flight, so this takes effect after whoever is currently
     * speaking finishes -- the backend enforces that, this just asks.
     */
    const handleStop = useCallback(async () => {
        if (!roundJob) return
        try {
            setRoundJob(await stopRound(roundJob.id))
        } catch (err) {
            setSessionError(errorMessage(err, 'Could not stop the debate.'))
        }
    }, [roundJob])

    /**
     * Continue the discussion after a round has already finished once.
     *
     * Not a resume: a stopped or delivered round is terminal on the backend,
     * there is no picking the exact same round back up mid-flight. This
     * starts a genuinely new round on the same session, with
     * `continue_dialogue: true` so the brief can be empty and the backend
     * still has something to say -- it falls back to a continuation prompt
     * built from memory rather than requiring fresh text.
     */
    const handleContinue = useCallback(async () => {
        if (!sessionId) return
        setDispatching(true)
        setSessionError(null)
        try {
            const job = await conveneAsync(sessionId, {
                message: brief.trim() || undefined,
                continueDialogue: true,
            })
            setRoundJob(job)
            setMiddleTab('log')
        } catch (err) {
            setSessionError(errorMessage(err, 'The board could not continue.'))
        } finally {
            setDispatching(false)
        }
    }, [sessionId, brief])

    /**
     * One-on-one. A private session seated with exactly this person, in manual
     * turn mode so nobody else speaks unless you ask them to. It does not
     * disturb the room you were assembling.
     */
    const handleTalkTo = useCallback(async (seatId) => {
        const seat = seatsById.get(seatId)
        if (!seat || seatId === 'moderator') return
        setDispatching(true)
        setSessionError(null)
        try {
            const session = await createChatSession({
                title: `1:1 — ${seat.name}`,
                selected_agent_ids: [seatId],
                persona_packs: packsFor([seatId]),
                turn_mode: 'manual',
                manual_agent_id: seatId,
            })
            setSessionId(session.id)
            setOneOnOne(seat)
            setTarget(seatId)
            setMessages([])
            // Same pattern as resumeSession: setting the tab alone only
            // matters once the user is actually looking at the console.
            // Talk is reachable from the Floor (a different top-level view),
            // and without this the session was created successfully in the
            // background while the screen stayed on Floor showing nothing --
            // "no action item" from the user's side even though it worked.
            setView('console')
            setMiddleTab('log')
            if (typeof session.disclaimer === 'string' && session.disclaimer) {
                setDisclaimer(session.disclaimer)
            }
        } catch (err) {
            setSessionError(errorMessage(err, `Could not open a session with ${seat.name}.`))
        } finally {
            setDispatching(false)
        }
    }, [seatsById, packsFor])

    const handleSend = useCallback(async (message) => {
        if (!sessionId) return
        setDispatching(true)
        setSessionError(null)
        try {
            const response = await sendChatMessage(sessionId, {
                message,
                agent_id: target || null,
            })
            setMessages(Array.isArray(response.session?.messages) ? response.session.messages : [])
            setActionItems(Array.isArray(response.session?.action_items) ? response.session.action_items : [])
            setMiddleTab('log')
        } catch (err) {
            setSessionError(errorMessage(err, 'That turn did not go through.'))
        } finally {
            setDispatching(false)
        }
    }, [sessionId, target])

    /**
     * Assign work to a seat. If there is no session yet, one is opened seated
     * with just that person -- assigning is a reason to start working, not
     * something you have to set up a boardroom for first.
     */
    const handleAssign = useCallback(async ({ task, agentId, priority }) => {
        setDispatching(true)
        setSessionError(null)
        try {
            let id = sessionId
            const seat = seatsById.get(agentId)

            // An existing session can only be assigned to if the seat is in it.
            const seatIsSeated = messages.length > 0 || oneOnOne
                ? (oneOnOne ? oneOnOne.id === agentId : roomIds.includes(agentId))
                : false

            if (!id || !seatIsSeated) {
                const session = await createChatSession({
                    title: `Task — ${seat ? seat.name : agentId}`,
                    selected_agent_ids: [agentId],
                    persona_packs: packsFor([agentId]),
                    turn_mode: 'manual',
                    manual_agent_id: agentId,
                })
                id = session.id
                setSessionId(id)
                setOneOnOne(seat || null)
                setTarget(agentId)
                setMessages([])
            }

            if (view === 'floor') {
                // On the floor the point is watching the work happen, so the
                // order lands instantly and the desk reports its own progress.
                await assignTaskAsync(id, { task, agentId, priority })
                setAssignTo(null)
            } else {
                const body = await assignTask(id, { task, agentId, priority })
                setMessages(Array.isArray(body.session?.messages) ? body.session.messages : [])
                setActionItems(Array.isArray(body.session?.action_items) ? body.session.action_items : [])
                setAssignTo(null)
            }
            setMiddleTab('log')
        } catch (err) {
            setSessionError(errorMessage(err, 'That task could not be assigned.'))
        } finally {
            setDispatching(false)
        }
    }, [messages.length, oneOnOne, roomIds, seatsById, packsFor, sessionId, view])

    const handleSynthesize = useCallback(async () => {
        if (!sessionId) return
        setDispatching(true)
        setSessionError(null)
        try {
            const session = await synthesizeSession(sessionId)
            setMessages(Array.isArray(session.messages) ? session.messages : [])
            setMiddleTab('log')
        } catch (err) {
            setSessionError(errorMessage(err, 'The chair could not synthesize.'))
        } finally {
            setDispatching(false)
        }
    }, [sessionId])

    const degraded = packs.filter((p) => p.degraded).map((p) => p.id)
    return (
        <div className={`console${view === 'floor' ? ' is-floor' : ''}`}>
            <header className="masthead">
                <h1>Consilium</h1>
                <span className="sub">
                    {rosterLoading ? 'loading the org…' : `${seats.length} seats`}
                </span>
                {/*
                  * Compact by request: this used to be a full-width banner row
                  * costing ~40px of vertical space permanently. The actual
                  * guardrail (declining individual-patient advice, no PHI) is
                  * enforced server-side in every prompt regardless of whether
                  * this text is visible -- this is the user-facing disclosure
                  * of that boundary, not the boundary itself, so shrinking it
                  * doesn't weaken anything. Full text stays reachable via
                  * title (hover) and aria-label (screen reader), not hidden.
                  */}
                {disclaimer && (
                    <span
                        className="disclaimer-badge"
                        title={disclaimer}
                        aria-label={`Advisory only. ${disclaimer}`}
                    >
                        Advisory only
                    </span>
                )}
                {degraded.length > 0 && (
                    <span
                        className="disclaimer-badge is-degraded"
                        title={`${degraded.join(', ')} declares a ladder or guardrail policy that does not resolve.`}
                        role="alert"
                    >
                        Degraded: {degraded.join(', ')}
                    </span>
                )}
                <div className="spacer" />
                <div className="view-switch" role="group" aria-label="View">
                    {['console', 'floor', 'history'].map((v) => (
                        <button
                            key={v}
                            type="button"
                            className="pack-chip"
                            aria-pressed={view === v}
                            onClick={() => setView(v)}
                        >
                            {v === 'console' ? 'Console' : v === 'floor' ? 'Floor' : 'History'}
                            {v === 'floor' && activeJobs > 0 && (
                                <span className="count">{activeJobs} working</span>
                            )}
                        </button>
                    ))}
                </div>
                <button
                    type="button"
                    className={`pack-chip${providerState && !providerState.ready ? ' needs-attention' : ''}`}
                    onClick={() => setSettingsOpen(true)}
                    title="Model provider settings"
                >
                    {providerState
                        ? providerState.ready
                            ? `${providerState.provider_label} · ${providerState.model}`
                            : 'No model configured'
                        : 'Settings'}
                </button>
                <div className="pack-picker" role="group" aria-label="Packs to seat from">
                    {packs.map((pack) => (
                        <button
                            key={pack.id}
                            type="button"
                            className="pack-chip"
                            aria-pressed={selectedPacks.includes(pack.id)}
                            onClick={() => togglePack(pack.id)}
                            title={pack.description || pack.display_name}
                        >
                            {pack.display_name}
                            <span className="count">{pack.own_seats}</span>
                        </button>
                    ))}
                </div>
            </header>

            {view === 'history' ? (
                <History seatNames={seatNames} onResume={resumeSession} />
            ) : view === 'floor' ? (
                <div className="floor-pane">
                    <Floor
                        seats={seats}
                        jobsBySeat={jobsBySeat}
                        activeCount={activeJobs}
                        inRoom={inRoom}
                        tensions={tensions}
                        error={jobsError}
                        onInspect={setInspectedId}
                        onAssign={setAssignTo}
                        onTalkTo={handleTalkTo}
                    />
                </div>
            ) : (
            <div className="panes">
                <Directory
                    seats={seats}
                    loading={rosterLoading}
                    error={rosterError}
                    inRoom={inRoom}
                    inspectedId={inspectedId}
                    onToggleSeat={toggleSeat}
                    onInspect={setInspectedId}
                    onRetry={() => loadRoster(selectedPacks)}
                />

                <section className="pane middle-pane" aria-label="Room, seated roster, and work log">
                    <div className="pane-head tab-bar" role="tablist" aria-label="Middle panel view">
                        {[
                            { id: 'room', label: 'Room' },
                            { id: 'seated', label: 'Seated', count: roomSeats.length },
                            { id: 'log', label: 'Work Log', count: messages.length || null },
                        ].map((tab) => (
                            <button
                                key={tab.id}
                                type="button"
                                role="tab"
                                aria-selected={middleTab === tab.id}
                                className="tab-button"
                                onClick={() => setMiddleTab(tab.id)}
                            >
                                {tab.label}
                                {Boolean(tab.count) && <span className="tally">{tab.count}</span>}
                            </button>
                        ))}

                        {/*
                          * One button, three states, living in the tab bar so
                          * it stays reachable no matter which tab is open --
                          * it used to live only inside the Room tab's form,
                          * which meant it vanished the moment "Convene"
                          * auto-switched you away to Work Log to watch it.
                          *
                          * Pushed to the right (margin-left: auto on the
                          * spacer) and NOT styled as a `.tab-button`: it is an
                          * action, not a fourth thing to navigate to, and
                          * should read as visually distinct from Room /
                          * Seated / Work Log rather than blending in as one
                          * more tab among equals.
                          *
                          * Not shown in a one-on-one: those use manual turns
                          * (handleSend) rather than the automatic-round/
                          * round-job machinery this button drives.
                          */}
                        {!oneOnOne && (
                            <>
                                <span className="tab-bar-spacer" />
                                <button
                                    type="button"
                                    className={`convene-toggle${convening ? ' is-stop' : ''}`}
                                    onClick={convening ? handleStop : roundJob ? handleContinue : handleDispatch}
                                    disabled={
                                        !convening && (
                                            roomSeats.length === 0
                                            || dispatching
                                            || (!roundJob && !brief.trim())
                                        )
                                    }
                                >
                                    {convening && <span className="tab-bar-live" aria-hidden="true" />}
                                    {convening
                                        ? 'Stop the debate'
                                        : roundJob
                                            ? (dispatching ? 'Continuing…' : 'Continue the discussion')
                                            : (dispatching ? 'Convening…' : 'Convene the board')}
                                </button>
                            </>
                        )}
                    </div>

                    <Room
                        activeTab={middleTab}
                        brief={brief}
                        onBriefChange={setBrief}
                        cap={cap}
                        onCapChange={setCap}
                        seats={roomSeats}
                        tensions={tensions}
                        rationale={rationale}
                        chosenBy={chosenBy}
                        aiReason={aiReason}
                        cost={cost}
                        warning={tensionWarning}
                        summoning={summoning}
                        convening={convening}
                        error={roomError}
                        onSummon={handleSummon}
                        onClear={handleClear}
                        onDropSeat={toggleSeat}
                        onInspect={setInspectedId}
                    />

                    <div className={`tab-panel log-panel${middleTab === 'log' ? ' is-active' : ''}`}>
                        <WorkLog
                            headline={oneOnOne ? `One-on-one — ${oneOnOne.name}` : 'Work log'}
                            messages={messages}
                            seatNames={seatNames}
                            seats={oneOnOne ? [oneOnOne] : roomSeats}
                            actionItems={actionItems}
                            busy={dispatching || convening}
                            roundJob={roundJob}
                            onStop={handleStop}
                            error={sessionError}
                            target={target}
                            onTargetChange={setTarget}
                            onSend={handleSend}
                            canSynthesize={
                                Boolean(sessionId) && !oneOnOne && !convening && messages.length > 1
                            }
                            onSynthesize={handleSynthesize}
                        />
                    </div>
                </section>

                <Inspector
                    seat={dossier}
                    loading={Boolean(inspectedId) && !dossier && !dossierError}
                    error={dossierError}
                    inRoom={inRoom}
                    onToggleSeat={toggleSeat}
                    onTalkTo={handleTalkTo}
                    onAssign={setAssignTo}
                    busy={dispatching}
                />
            </div>
            )}

            <AssignTask
                seat={assignTo}
                open={Boolean(assignTo)}
                busy={dispatching}
                error={sessionError}
                onAssign={handleAssign}
                onClose={() => setAssignTo(null)}
            />

            <Settings
                open={settingsOpen}
                onClose={() => setSettingsOpen(false)}
                onChanged={setProviderState}
            />
        </div>
    )
}
