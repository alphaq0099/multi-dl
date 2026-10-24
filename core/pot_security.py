"""Install a reviewed security boundary into the optional bundled token service.

The dependency is ignored by Git; keeping the patcher and its JS guard in the
main repository makes the hardening reproducible after reinstalling it.
"""
import os
from pathlib import Path
import secrets

from core.storage import atomic_json_write, file_lock

ROOT = Path(__file__).resolve().parent.parent
MARKER = 'MULTI_DL_HARDENED_V1'


def provider_token():
    import json
    path = ROOT / 'data' / 'pot-auth.secrets.json'
    with file_lock(path):
        if path.exists():
            token = json.loads(path.read_text(encoding='utf-8'))['token']
        else:
            token = secrets.token_urlsafe(48)
            atomic_json_write(path, {'token': token})
    os.environ['MULTI_DL_POT_TOKEN'] = token
    return token


def harden_provider(server):
    server = Path(server)
    guard = (ROOT / 'support' / 'pot_security.js').read_text(encoding='utf-8')
    with file_lock(server / '.multi-dl-hardening'):
        for folder, ext in [('src', 'ts'), ('build', 'js')]:
            main = server / folder / f'main.{ext}'
            session = server / folder / f'session_manager.{ext}'
            if not main.exists() or not session.exists():
                raise RuntimeError('Token provider source/build is incomplete; refusing unsafe startup')
            text = main.read_text(encoding='utf-8')
            if MARKER not in text:
                if 'host: "::"' not in text or 'body.challenge' not in text:
                    raise RuntimeError('Unrecognized token provider build; refusing unsafe startup')
                text = "import { guard } from './multi_dl_security.js';\n// " + MARKER + '\n' + text
                text = text.replace('host: "::"', 'host: "127.0.0.1"').replace('host: "0.0.0.0"', 'host: "127.0.0.1"')
                text = text.replace('httpServer.use(express.json());', 'httpServer.use(guard);\nhttpServer.use(express.json({ limit: "64kb" }));')
                text = text.replace('body.challenge,', 'undefined,')
                text = text.replace('body.disable_tls_verification || false', 'false')
                text = text.replace('server_uptime: process.uptime(),', 'multi_dl_hardened: true,\n        server_uptime: process.uptime(),')
            code = session.read_text(encoding='utf-8')
            if MARKER not in code:
                anchor = '`https:${privateDoNotAccessOrElseTrustedResourceUrlWrappedValue}`'
                if anchor not in code or 'headers: options?.headers,' not in code:
                    raise RuntimeError('Unrecognized token interpreter; refusing unsafe startup')
                code = "import { validateInterpreterUrl } from './multi_dl_security.js';\n// " + MARKER + '\n' + code
                code = code.replace(anchor, 'validateInterpreterUrl(privateDoNotAccessOrElseTrustedResourceUrlWrappedValue), { redirect: "error" }')
                code = code.replace('headers: options?.headers,', 'headers: options?.headers,\n                        timeout: 15000,\n                        maxRedirects: options?.redirect === "error" ? 0 : 5,')
            (server / folder / 'multi_dl_security.js').write_text(guard, encoding='utf-8')
            main.write_text(text, encoding='utf-8')
            session.write_text(code, encoding='utf-8')
        plugin = server.parent / 'plugin' / 'yt_dlp_plugins' / 'extractor' / 'getpot_bgutil_http.py'
        if plugin.exists():
            text = plugin.read_text(encoding='utf-8')
            if MARKER not in text:
                text = text.replace('import json\n', 'import json\nimport os\n# ' + MARKER + '\n')
                text = text.replace("extensions={'timeout': self._GET_SERVER_VSN_TIMEOUT}", "headers={'Authorization': 'Bearer ' + os.environ.get('MULTI_DL_POT_TOKEN', '')}, extensions={'timeout': self._GET_SERVER_VSN_TIMEOUT}")
                text = text.replace("headers={'Content-Type': 'application/json'}", "headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + os.environ.get('MULTI_DL_POT_TOKEN', '')}")
                plugin.write_text(text, encoding='utf-8')
