/**
 * Consilium Frontend Logger Utility
 * Provides structured console logging with timestamping, levels, and formatted styling.
 */

const LOG_LEVELS = {
    DEBUG: 0,
    INFO: 1,
    WARN: 2,
    ERROR: 3,
}

class Logger {
    constructor() {
        const isDev = import.meta.env?.DEV !== false
        this.level = isDev ? LOG_LEVELS.INFO : LOG_LEVELS.WARN
    }

    _formatPrefix(level) {
        const time = new Date().toISOString().substring(11, 19)
        return `[${time}] [Apex ${level}]`
    }

    debug(message, ...args) {
        if (this.level <= LOG_LEVELS.DEBUG) {
            console.debug(`%c${this._formatPrefix('DEBUG')}`, 'color: #8b5cf6; font-weight: bold;', message, ...args)
        }
    }

    info(message, ...args) {
        if (this.level <= LOG_LEVELS.INFO) {
            console.info(`%c${this._formatPrefix('INFO')}`, 'color: #3b82f6; font-weight: bold;', message, ...args)
        }
    }

    warn(message, ...args) {
        if (this.level <= LOG_LEVELS.WARN) {
            console.warn(`%c${this._formatPrefix('WARN')}`, 'color: #f59e0b; font-weight: bold;', message, ...args)
        }
    }

    error(message, ...args) {
        if (this.level <= LOG_LEVELS.ERROR) {
            console.error(`%c${this._formatPrefix('ERROR')}`, 'color: #ef4444; font-weight: bold;', message, ...args)
        }
    }
}

export const logger = new Logger()
export default logger
