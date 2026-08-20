import React from 'react'

/**
 * Lightweight, robust Markdown renderer for executive boardroom chat.
 * Parses headers, bold, italics, inline code, line breaks, bullet lists,
 * numbered lists, blockquotes, code blocks, and GFM-style tables.
 *
 * Personas routinely answer in structured comparison tables ("Question |
 * Considerations | Preliminary Verdict") and inline <br> tags inside table
 * cells -- neither was previously recognized, so both rendered as literal
 * pipe-and-dash text and a literal "<br>" string instead of a table and a
 * line break.
 */
function parseInlineMarkdown(text) {
    if (!text) return text

    // Split text into tokens based on bold (**), italic (*), inline code (`),
    // and <br> tags. Order matters: bold before italic, so "**x**" isn't
    // first consumed as two italic markers.
    const inlineRegex = /(\*\*.*?\*\*|\*.*?\*|`.*?`|<br\s*\/?>)/gi
    const matches = text.split(inlineRegex)

    return matches.map((token, i) => {
        if (!token) return null
        if (token.startsWith('**') && token.endsWith('**') && token.length > 4) {
            return <strong key={i}>{token.slice(2, -2)}</strong>
        }
        if (token.startsWith('*') && token.endsWith('*') && token.length > 2) {
            return <em key={i}>{token.slice(1, -1)}</em>
        }
        if (token.startsWith('`') && token.endsWith('`') && token.length > 2) {
            return <code key={i} className="inline-code">{token.slice(1, -1)}</code>
        }
        if (/^<br\s*\/?>$/i.test(token)) {
            return <br key={i} />
        }
        return token
    })
}

// A GFM table separator row: |---|:---:|---:| (dashes/colons, any pipe
// placement). Distinguishes a real table header from a paragraph that
// merely happens to contain a "|" character.
const TABLE_SEPARATOR = /^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$/

function splitTableRow(line) {
    const trimmed = line.trim()
    const cells = trimmed.split('|').map((c) => c.trim())
    // A leading/trailing "|" produces an empty first/last cell -- drop them,
    // but only when the row actually used the pipe-fenced form, so a row
    // that has real content flush against one edge is never eaten.
    if (cells.length && cells[0] === '') cells.shift()
    if (cells.length && cells[cells.length - 1] === '') cells.pop()
    return cells
}

export default function MarkdownView({ content }) {
    if (!content) return null

    const lines = content.split('\n')
    const elements = []
    let inCodeBlock = false
    let codeBuffer = []

    // Buffered list items, flushed into one <ul>/<ol> when the run of
    // same-type items ends -- <li> was previously pushed directly as a
    // sibling of <p>/<h3>, which is invalid outside a list container.
    let listBuffer = []
    let listType = null // 'ul' | 'ol'

    const flushList = (key) => {
        if (!listBuffer.length) return
        const Tag = listType === 'ol' ? 'ol' : 'ul'
        elements.push(
            <Tag key={`list-${key}`} className="md-list">
                {listBuffer}
            </Tag>
        )
        listBuffer = []
        listType = null
    }

    for (let i = 0; i < lines.length; i++) {
        const line = lines[i]

        // Code block toggle
        if (line.trim().startsWith('```')) {
            flushList(i)
            if (inCodeBlock) {
                elements.push(
                    <pre key={`code-${i}`} className="code-block">
                        <code>{codeBuffer.join('\n')}</code>
                    </pre>
                )
                codeBuffer = []
                inCodeBlock = false
            } else {
                inCodeBlock = true
            }
            continue
        }

        if (inCodeBlock) {
            codeBuffer.push(line)
            continue
        }

        const trimmed = line.trim()

        // GFM table: a "|"-bearing line immediately followed by a
        // dash/colon separator row. Consumes every following row that still
        // contains "|", so the whole table (not just the header) becomes
        // one <table>, wrapped for horizontal scroll rather than pushing
        // the page wide (design-lessons: wide content scrolls in its own
        // container, the page never scrolls horizontally).
        if (trimmed.includes('|') && i + 1 < lines.length && TABLE_SEPARATOR.test(lines[i + 1].trim())) {
            flushList(i)
            const header = splitTableRow(trimmed)
            let j = i + 2
            const bodyRows = []
            while (j < lines.length && lines[j].trim() !== '' && lines[j].trim().includes('|')) {
                bodyRows.push(splitTableRow(lines[j]))
                j++
            }
            elements.push(
                <div key={`table-${i}`} className="md-table-wrap">
                    <table className="md-table">
                        <thead>
                            <tr>
                                {header.map((cell, ci) => (
                                    <th key={ci}>{parseInlineMarkdown(cell)}</th>
                                ))}
                            </tr>
                        </thead>
                        <tbody>
                            {bodyRows.map((row, ri) => (
                                <tr key={ri}>
                                    {row.map((cell, ci) => (
                                        <td key={ci}>{parseInlineMarkdown(cell)}</td>
                                    ))}
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            )
            i = j - 1 // outer loop's i++ lands exactly after the last consumed row
            continue
        }

        // Headers
        if (trimmed.startsWith('### ')) {
            flushList(i)
            elements.push(<h3 key={i} className="md-h3">{parseInlineMarkdown(trimmed.slice(4))}</h3>)
        } else if (trimmed.startsWith('## ')) {
            flushList(i)
            elements.push(<h2 key={i} className="md-h2">{parseInlineMarkdown(trimmed.slice(3))}</h2>)
        } else if (trimmed.startsWith('# ')) {
            flushList(i)
            elements.push(<h1 key={i} className="md-h1">{parseInlineMarkdown(trimmed.slice(2))}</h1>)
        }
        // Blockquotes
        else if (trimmed.startsWith('> ')) {
            flushList(i)
            elements.push(<blockquote key={i} className="md-quote">{parseInlineMarkdown(trimmed.slice(2))}</blockquote>)
        }
        // Horizontal rules -- personas use "---" as a section divider
        // (visible directly in this bug's own sample content); previously
        // fell through to the generic paragraph branch as literal text.
        else if (/^(-{3,}|\*{3,}|_{3,})$/.test(trimmed)) {
            flushList(i)
            elements.push(<hr key={i} className="md-hr" />)
        }
        // Unordered bullet lists
        else if (trimmed.startsWith('- ') || trimmed.startsWith('* ')) {
            if (listType !== 'ul') flushList(i)
            listType = 'ul'
            listBuffer.push(
                <li key={i} className="md-list-item">{parseInlineMarkdown(trimmed.slice(2))}</li>
            )
        }
        // Numbered lists
        else if (/^\d+\.\s/.test(trimmed)) {
            const match = trimmed.match(/^(\d+)\.\s+(.*)/)
            if (match) {
                if (listType !== 'ol') flushList(i)
                listType = 'ol'
                listBuffer.push(
                    <li key={i} className="md-list-item">{parseInlineMarkdown(match[2])}</li>
                )
            } else {
                flushList(i)
                elements.push(<p key={i} className="md-p">{parseInlineMarkdown(line)}</p>)
            }
        }
        // Empty lines / paragraph breaks
        else if (trimmed === '') {
            flushList(i)
            elements.push(<div key={i} className="md-spacer" />)
        }
        // Regular paragraphs
        else {
            flushList(i)
            elements.push(<p key={i} className="md-p">{parseInlineMarkdown(line)}</p>)
        }
    }
    flushList('end')

    return <div className="markdown-body">{elements}</div>
}
