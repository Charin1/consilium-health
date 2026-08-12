const JSON_HEADERS = {
    'Content-Type': 'application/json',
}

export class ApiError extends Error {
    constructor(message, { status = 0, code = 'UNKNOWN_ERROR', retryable = false, details = null } = {}) {
        super(message)
        this.name = 'ApiError'
        this.status = status
        this.code = code
        this.retryable = retryable
        this.details = details
    }
}

async function parseResponseBody(response) {
    const contentType = response.headers.get('content-type') || ''
    if (!contentType.includes('application/json')) {
        const text = await response.text()
        return text ? { message: text } : null
    }

    try {
        return await response.json()
    } catch {
        return null
    }
}

function normalizeServerError(body, status) {
    if (body && typeof body === 'object' && body.error && typeof body.error === 'object') {
        return {
            message: body.error.message || `Request failed with status ${status}`,
            code: body.error.code || 'HTTP_ERROR',
            retryable: Boolean(body.error.retryable),
            details: body.error.details || null,
        }
    }

    if (body && typeof body === 'object' && body.detail) {
        return {
            message: String(body.detail),
            code: 'HTTP_ERROR',
            retryable: status >= 500,
            details: null,
        }
    }

    return {
        message: `Request failed with status ${status}`,
        code: 'HTTP_ERROR',
        retryable: status >= 500,
        details: null,
    }
}

async function request(path, options = {}) {
    const config = {
        ...options,
        headers: {
            ...JSON_HEADERS,
            ...(options.headers || {}),
        },
    }

    let response
    try {
        response = await fetch(path, config)
    } catch {
        throw new ApiError('Network error. Please check your connection and backend status.', {
            code: 'NETWORK_ERROR',
            retryable: true,
        })
    }

    const body = await parseResponseBody(response)
    if (!response.ok) {
        const normalized = normalizeServerError(body, response.status)
        throw new ApiError(normalized.message, {
            status: response.status,
            code: normalized.code,
            retryable: normalized.retryable,
            details: normalized.details,
        })
    }

    return body
}

// --- Settings: providers, models, credentials --------------------------
// An API key goes UP only. Every response reports `has_key` as a boolean;
// there is deliberately no endpoint that reads a key back.

export function getSettings() {
    return request('/api/config')
}

export function updateSettings(payload) {
    return request('/api/config', {
        method: 'POST',
        body: JSON.stringify(payload),
    })
}

export function testProviderConnection() {
    return request('/api/config/test', { method: 'POST' })
}

export function listLocalModels(baseUrl) {
    const query = baseUrl ? `?base_url=${encodeURIComponent(baseUrl)}` : ''
    return request(`/api/ollama/models${query}`)
}

// --- Org: the roster directory and the deterministic router ------------
// These back the console. `/api/org/seats` is the browsable directory and
// never returns system prompts; `/api/chat/agents` does and is for the chat
// client only.

function packQuery(packs) {
    if (!Array.isArray(packs) || packs.length === 0) return ''
    return `packs=${encodeURIComponent(packs.join(','))}`
}

export function listOrgPacks() {
    return request('/api/org/packs')
}

export function listOrgSeats({ packs, tag, q } = {}) {
    const parts = [packQuery(packs)]
    if (tag) parts.push(`tag=${encodeURIComponent(tag)}`)
    if (q) parts.push(`q=${encodeURIComponent(q)}`)
    const query = parts.filter(Boolean).join('&')
    return request(`/api/org/seats${query ? `?${query}` : ''}`)
}

export function getOrgSeat(seatId) {
    return request(`/api/org/seats/${encodeURIComponent(seatId)}`)
}

export function summonRoom({ brief, packs, cap, pinned }) {
    return request('/api/org/summon', {
        method: 'POST',
        body: JSON.stringify({ brief, packs, cap, pinned }),
    })
}

export function inspectTensions({ seatIds, packs }) {
    return request('/api/org/tensions', {
        method: 'POST',
        body: JSON.stringify({ seat_ids: seatIds, packs }),
    })
}

export function createMission(payload) {
    return request('/api/missions', {
        method: 'POST',
        body: JSON.stringify(payload),
    })
}

export function getMission(missionId) {
    return request(`/api/missions/${missionId}`)
}

export function submitDecision(missionId, payload) {
    return request(`/api/missions/${missionId}/decision`, {
        method: 'POST',
        body: JSON.stringify(payload),
    })
}

export function retryMission(missionId) {
    return request(`/api/missions/${missionId}/retry`, {
        method: 'POST',
    })
}

export function createReport(payload) {
    return request('/api/reports', {
        method: 'POST',
        body: JSON.stringify(payload),
    })
}

export function listReports({ missionId } = {}) {
    const query = missionId ? `?mission_id=${encodeURIComponent(missionId)}` : ''
    return request(`/api/reports${query}`)
}

export function getReport(reportId) {
    return request(`/api/reports/${reportId}`)
}

export function getReportDownloadUrl(reportId) {
    return `/api/reports/${reportId}/download`
}

// Chat / Brainstorming
export function listChatAgents() {
    return request('/api/chat/agents')
}

export function listChatTemplates() {
    return request('/api/chat/templates')
}

export function createChatSession(payload) {
    return request('/api/chat/sessions', {
        method: 'POST',
        body: JSON.stringify(payload || {}),
    })
}

export function listChatSessions({ limit } = {}) {
    const query = limit ? `?limit=${encodeURIComponent(limit)}` : ''
    return request(`/api/chat/sessions${query}`)
}

export function listAllTasks({ state } = {}) {
    const query = state && state !== 'all' ? `?state=${encodeURIComponent(state)}` : ''
    return request(`/api/chat/tasks${query}`)
}

export function getChatSession(sessionId) {
    return request(`/api/chat/sessions/${sessionId}`)
}

export function updateChatSession(sessionId, payload) {
    return request(`/api/chat/sessions/${sessionId}`, {
        method: 'PATCH',
        body: JSON.stringify(payload),
    })
}

export function sendChatMessage(sessionId, payload) {
    return request(`/api/chat/sessions/${sessionId}/messages`, {
        method: 'POST',
        body: JSON.stringify(payload),
    })
}

// --- Rounds: "Convene the board" without blocking on the whole debate --
// Starting a round is a chat action (it needs the session and the opening
// brief); watching and stopping one only needs its job id, hence the split
// between /api/chat/sessions/{id}/convene-async and /api/rounds/{jobId}.

export function conveneAsync(sessionId, { message, continueDialogue } = {}) {
    return request(`/api/chat/sessions/${sessionId}/convene-async`, {
        method: 'POST',
        body: JSON.stringify({ message: message || null, continue_dialogue: Boolean(continueDialogue) }),
    })
}

export function getCurrentRound(sessionId) {
    return request(`/api/chat/sessions/${sessionId}/round`)
}

export function getRound(jobId) {
    return request(`/api/rounds/${jobId}`)
}

export function stopRound(jobId) {
    return request(`/api/rounds/${jobId}/stop`, { method: 'POST' })
}

// --- Jobs: what each seat is doing right now ---------------------------
// The floor polls these. Work is STARTED through the domain endpoint that owns
// it and only watched here.

export function assignTaskAsync(sessionId, { task, agentId, priority }) {
    return request(`/api/chat/sessions/${sessionId}/assign-async`, {
        method: 'POST',
        body: JSON.stringify({ task, agent_id: agentId, priority: priority || 'Medium' }),
    })
}

export function listJobs({ sessionId, activeOnly } = {}) {
    const parts = []
    if (sessionId) parts.push(`session_id=${encodeURIComponent(sessionId)}`)
    if (activeOnly) parts.push('active_only=true')
    const query = parts.join('&')
    return request(`/api/jobs${query ? `?${query}` : ''}`)
}

export function assignTask(sessionId, { task, agentId, priority }) {
    return request(`/api/chat/sessions/${sessionId}/assign`, {
        method: 'POST',
        body: JSON.stringify({ task, agent_id: agentId, priority: priority || 'Medium' }),
    })
}

export function synthesizeSession(sessionId) {
    return request(`/api/chat/sessions/${sessionId}/synthesize`, {
        method: 'POST',
    })
}

export function deleteChatSession(sessionId) {
    return request(`/api/chat/sessions/${sessionId}`, {
        method: 'DELETE',
    })
}

export function deleteChatMessage(messageId) {
    return request(`/api/chat/messages/${messageId}`, {
        method: 'DELETE',
    })
}

export function truncateChatMessages(sessionId, messageId) {
    return request(`/api/chat/sessions/${sessionId}/messages/${messageId}/truncate`, {
        method: 'DELETE',
    })
}

export function createActionItem(sessionId, payload) {
    return request(`/api/chat/sessions/${sessionId}/action-items`, {
        method: 'POST',
        body: JSON.stringify(payload),
    })
}

export function updateActionItem(sessionId, itemId, payload) {
    return request(`/api/chat/sessions/${sessionId}/action-items/${itemId}`, {
        method: 'PATCH',
        body: JSON.stringify(payload),
    })
}

export function deleteActionItem(sessionId, itemId) {
    return request(`/api/chat/sessions/${sessionId}/action-items/${itemId}`, {
        method: 'DELETE',
    })
}

