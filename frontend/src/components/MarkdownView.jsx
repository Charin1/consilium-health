import React from 'react'

/**
 * Lightweight, robust Markdown renderer for executive boardroom chat.
 * Parses headers, bold, italics, bullet lists, numbered lists, blockquotes, inline code, and code blocks.
 */
function parseInlineMarkdown(text) {
    if (!text) return text

    // Split text into tokens based on bold (**), italic (*), and inline code (`)
    const parts = []
    let current = text
    let keyCounter = 0

    // Helper regex tokenizer for inline styles
    const inlineRegex = /(\*\*.*?\*\*|\*.*?\*|`.*?`)/g
    const matches = current.split(inlineRegex)

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
        return token
    })
}

export default function MarkdownView({ content }) {
    if (!content) return null

    const lines = content.split('\n')
    const elements = []
    let inCodeBlock = false
    let codeBuffer = []
    let codeLang = ''

    for (let i = 0; i < lines.length; i++) {
        const line = lines[i]

        // Code block toggle
        if (line.trim().startsWith('```')) {
            if (inCodeBlock) {
                // End code block
                elements.push(
                    <pre key={`code-${i}`} className="code-block">
                        <code>{codeBuffer.join('\n')}</code>
                    </pre>
                )
                codeBuffer = []
                inCodeBlock = false
            } else {
                // Start code block
                inCodeBlock = true
                codeLang = line.trim().slice(3)
            }
            continue
        }

        if (inCodeBlock) {
            codeBuffer.push(line)
            continue
        }

        const trimmed = line.trim()

        // Headers
        if (trimmed.startsWith('### ')) {
            elements.push(<h3 key={i} className="md-h3">{parseInlineMarkdown(trimmed.slice(4))}</h3>)
        } else if (trimmed.startsWith('## ')) {
            elements.push(<h2 key={i} className="md-h2">{parseInlineMarkdown(trimmed.slice(3))}</h2>)
        } else if (trimmed.startsWith('# ')) {
            elements.push(<h1 key={i} className="md-h1">{parseInlineMarkdown(trimmed.slice(2))}</h1>)
        }
        // Blockquotes
        else if (trimmed.startsWith('> ')) {
            elements.push(<blockquote key={i} className="md-quote">{parseInlineMarkdown(trimmed.slice(2))}</blockquote>)
        }
        // Unordered Bullet Lists
        else if (trimmed.startsWith('- ') || trimmed.startsWith('* ')) {
            elements.push(
                <li key={i} className="md-list-item">
                    <span className="md-bullet">•</span>
                    <span>{parseInlineMarkdown(trimmed.slice(2))}</span>
                </li>
            )
        }
        // Numbered Lists
        else if (/^\d+\.\s/.test(trimmed)) {
            const match = trimmed.match(/^(\d+\.)\s+(.*)/)
            if (match) {
                elements.push(
                    <li key={i} className="md-list-item">
                        <span className="md-number">{match[1]}</span>
                        <span>{parseInlineMarkdown(match[2])}</span>
                    </li>
                )
            } else {
                elements.push(<p key={i} className="md-p">{parseInlineMarkdown(line)}</p>)
            }
        }
        // Empty lines / Paragraph breaks
        else if (trimmed === '') {
            elements.push(<div key={i} className="md-spacer" />)
        }
        // Regular paragraphs
        else {
            elements.push(<p key={i} className="md-p">{parseInlineMarkdown(line)}</p>)
        }
    }

    return <div className="markdown-body">{elements}</div>
}
