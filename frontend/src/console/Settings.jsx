import React, { useCallback, useEffect, useState } from 'react'

import {
    getSettings,
    listLocalModels,
    testProviderConnection,
    updateSettings,
} from '../lib/apiClient.js'

/**
 * Provider settings.
 *
 * Two things this screen is careful about:
 *
 * - **The key field is write-only.** The server reports whether a credential
 *   exists, never what it is, so this renders "key set" rather than a masked
 *   value. There is nothing to read back and nothing to leak in a screenshot.
 * - **"Save" is not proof of anything.** A typo'd key saves fine. The test
 *   button spends a few tokens on a real round trip and reports what actually
 *   came back, which is the only check that means something.
 */

function text(value, fallback = '') {
    return typeof value === 'string' ? value : fallback
}

function price(model) {
    if (!model || (model.input === 0 && model.output === 0)) return 'free / local'
    return `$${model.input}/$${model.output} per 1M`
}

export default function Settings({ open, onClose, onChanged }) {
    const [catalogue, setCatalogue] = useState(null)
    const [current, setCurrent] = useState(null)
    const [provider, setProvider] = useState('')
    const [model, setModel] = useState('')
    const [apiKey, setApiKey] = useState('')
    const [baseUrl, setBaseUrl] = useState('')
    const [localModels, setLocalModels] = useState(null)

    const [loading, setLoading] = useState(true)
    const [saving, setSaving] = useState(false)
    const [testing, setTesting] = useState(false)
    const [testResult, setTestResult] = useState(null)
    const [error, setError] = useState(null)

    const load = useCallback(async () => {
        setLoading(true)
        setError(null)
        try {
            const body = await getSettings()
            setCatalogue(body)
            setCurrent(body.current)
            setProvider(text(body.current?.provider))
            setModel(text(body.current?.model))
            setBaseUrl(text(body.current?.ollama_base_url))
        } catch (err) {
            setError(text(err?.message, 'Could not load settings.'))
        } finally {
            setLoading(false)
        }
    }, [])

    useEffect(() => {
        if (open) load()
    }, [open, load])

    // Close on Escape — a modal you can only leave with the mouse is a trap.
    useEffect(() => {
        if (!open) return undefined
        const onKey = (e) => { if (e.key === 'Escape') onClose() }
        window.addEventListener('keydown', onKey)
        return () => window.removeEventListener('keydown', onKey)
    }, [open, onClose])

    const selected = catalogue?.providers?.find((p) => p.id === provider) || null
    const isLocal = Boolean(selected?.local)

    // What Ollama has actually pulled beats what our catalogue guesses it has.
    useEffect(() => {
        if (!open || !isLocal) {
            setLocalModels(null)
            return undefined
        }
        let cancelled = false
        listLocalModels(baseUrl)
            .then((body) => { if (!cancelled) setLocalModels(body) })
            .catch(() => { if (!cancelled) setLocalModels({ reachable: false, models: [] }) })
        return () => { cancelled = true }
    }, [open, isLocal, baseUrl])

    const handleProviderChange = (nextId) => {
        setProvider(nextId)
        const next = catalogue?.providers?.find((p) => p.id === nextId)
        setModel(text(next?.default_model))
        setApiKey('')
        setTestResult(null)
    }

    const handleSave = async (e) => {
        e.preventDefault()
        setSaving(true)
        setError(null)
        setTestResult(null)
        try {
            const state = await updateSettings({
                provider,
                model: model || undefined,
                api_key: apiKey || undefined,
                ollama_base_url: isLocal ? baseUrl || undefined : undefined,
            })
            setCurrent(state)
            setApiKey('')
            await load()
            onChanged(state)
        } catch (err) {
            setError(text(err?.message, 'Could not save those settings.'))
        } finally {
            setSaving(false)
        }
    }

    const handleTest = async () => {
        setTesting(true)
        setTestResult(null)
        try {
            setTestResult(await testProviderConnection())
        } catch (err) {
            setTestResult({ ok: false, reason: text(err?.message, 'The test call failed.') })
        } finally {
            setTesting(false)
        }
    }

    if (!open) return null

    const offered = isLocal && localModels?.reachable && localModels.models.length > 0
        ? localModels.models
        : selected?.models || []

    return (
        <div className="modal-scrim" role="presentation" onClick={onClose}>
            <div
                className="modal"
                role="dialog"
                aria-modal="true"
                aria-labelledby="settings-title"
                onClick={(e) => e.stopPropagation()}
            >
                <header className="modal-head">
                    <h2 id="settings-title">Model provider</h2>
                    <button type="button" className="btn-ghost" onClick={onClose} aria-label="Close settings">
                        Close
                    </button>
                </header>

                <div className="modal-body">
                    {error && <div className="error-banner" role="alert">{error}</div>}
                    {loading && <div className="skeleton" style={{ height: 140 }} />}

                    {catalogue && (
                        <form onSubmit={handleSave} className="settings-form">
                            <fieldset className="provider-grid">
                                <legend className="sr-only">Provider</legend>
                                {catalogue.providers.map((p) => (
                                    <label
                                        key={p.id}
                                        className={`provider-card${provider === p.id ? ' is-active' : ''}`}
                                    >
                                        <input
                                            type="radio"
                                            name="provider"
                                            value={p.id}
                                            checked={provider === p.id}
                                            onChange={() => handleProviderChange(p.id)}
                                            className="sr-only"
                                        />
                                        <span className="provider-name">{text(p.label, p.id)}</span>
                                        <span className={`provider-state${p.has_key ? ' ok' : ''}`}>
                                            {p.local ? 'local' : p.has_key ? 'key set' : 'no key'}
                                        </span>
                                    </label>
                                ))}
                            </fieldset>

                            <label className="field">
                                <span>Model</span>
                                <select value={model} onChange={(e) => setModel(e.target.value)}>
                                    {offered.map((m) => (
                                        <option key={m.id} value={m.id}>
                                            {text(m.label, m.id)}
                                            {m.tier ? ` — ${m.tier}` : ''}
                                            {typeof m.input === 'number' ? ` — ${price(m)}` : ''}
                                        </option>
                                    ))}
                                </select>
                                <small>
                                    Leave a model set and every seat uses it. Clear it and the chair
                                    and executives get the frontier model while specialists get the
                                    balanced one.
                                </small>
                            </label>

                            {isLocal ? (
                                <label className="field">
                                    <span>Ollama address</span>
                                    <input
                                        type="text"
                                        value={baseUrl}
                                        placeholder="http://localhost:11434"
                                        onChange={(e) => setBaseUrl(e.target.value)}
                                    />
                                    <small>
                                        {localModels === null
                                            ? 'Checking…'
                                            : localModels.reachable
                                                ? `Reachable — ${localModels.models.length} model(s) pulled.`
                                                : 'Not reachable. Start Ollama, or pick another provider.'}
                                    </small>
                                </label>
                            ) : (
                                <label className="field">
                                    <span>API key</span>
                                    <input
                                        type="password"
                                        value={apiKey}
                                        autoComplete="off"
                                        placeholder={
                                            selected?.has_key
                                                ? 'A key is set. Type a new one to replace it.'
                                                : `Paste your ${text(selected?.label, 'provider')} key`
                                        }
                                        onChange={(e) => setApiKey(e.target.value)}
                                    />
                                    <small>
                                        Held in the server process for this run only — never written
                                        to disk and never sent back to this page.
                                        {selected?.env_key && ` Set ${selected.env_key} in .env to persist it.`}
                                    </small>
                                </label>
                            )}

                            <div className="modal-actions">
                                <button type="submit" className="btn" disabled={saving || !provider}>
                                    {saving ? 'Saving…' : 'Save'}
                                </button>
                                <button
                                    type="button"
                                    className="btn-ghost"
                                    onClick={handleTest}
                                    disabled={testing}
                                >
                                    {testing ? 'Testing…' : 'Test connection'}
                                </button>
                                {current && (
                                    <span className={`provider-state${current.ready ? ' ok' : ''}`}>
                                        {current.ready
                                            ? `Ready — ${text(current.provider_label)} / ${text(current.model)}`
                                            : text(current.reason, 'Not configured')}
                                    </span>
                                )}
                            </div>

                            {testResult && (
                                <p
                                    className={`notice ${testResult.ok ? 'notice-tension' : 'notice-guard'}`}
                                    role="status"
                                >
                                    {testResult.ok
                                        ? `${text(testResult.model)} replied: “${text(testResult.reply)}”`
                                        : `No reply — ${text(testResult.reason, 'unknown error')}`}
                                </p>
                            )}
                        </form>
                    )}
                </div>
            </div>
        </div>
    )
}
