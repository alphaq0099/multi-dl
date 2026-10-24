// Installed beside the bundled provider by core.pot_security.
import { timingSafeEqual } from 'node:crypto';

const token = process.env.MULTI_DL_POT_TOKEN;
if (!token || token.length < 32) {
    throw new Error('Start the token provider through MULTI_DOWNLOADER to supply its authentication token.');
}

export function guard(request, response, next) {
    const supplied = Buffer.from(request.get('authorization') || '');
    const expected = Buffer.from(`Bearer ${token}`);
    if (request.get('origin') || supplied.length !== expected.length || !timingSafeEqual(supplied, expected)) {
        return response.status(403).json({ error: 'Forbidden' });
    }
    next();
}

export function validateInterpreterUrl(raw) {
    const url = new URL(`https:${raw}`);
    const hosts = new Set(['www.google.com', 'google.com', 'www.youtube.com', 'youtube.com']);
    if (url.protocol !== 'https:' || url.username || url.password || url.port ||
        !hosts.has(url.hostname) || !url.pathname.startsWith('/js/bg/')) {
        throw new Error('Untrusted BotGuard interpreter URL');
    }
    return url.href;
}
