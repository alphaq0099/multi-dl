"""Optional loopback-only smoke check of the bundled compiled token server."""
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request


def main():
    root = Path(__file__).resolve().parent.parent
    node = shutil.which('node')
    if not node:
        raise RuntimeError('Node is required for this integration check')
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    token = secrets.token_urlsafe(48)
    env = dict(os.environ, MULTI_DL_POT_TOKEN=token)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    folder = root / 'scratch'
    folder.mkdir(exist_ok=True)
    with open(folder / 'token-service-smoke.log', 'w', encoding='utf-8') as output:
        proc = subprocess.Popen([node, str(root / 'tools/pot-provider/server/build/main.js'), '--port', str(port)],
                                env=env, stdout=output, stderr=output,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        try:
            endpoint = f'http://127.0.0.1:{port}'
            deadline = time.monotonic() + 20
            while True:
                try:
                    request = urllib.request.Request(endpoint + '/ping', headers={'Authorization': 'Bearer ' + token})
                    with opener.open(request, timeout=1) as response:
                        assert json.load(response)['multi_dl_hardened'] is True
                    break
                except (OSError, urllib.error.URLError):
                    if time.monotonic() > deadline or proc.poll() is not None:
                        raise
                    time.sleep(0.1)
            for headers in ({}, {'Authorization': 'Bearer ' + token, 'Origin': 'https://untrusted.invalid'}):
                try:
                    opener.open(urllib.request.Request(endpoint + '/ping', headers=headers), timeout=2)
                    raise AssertionError('Unauthenticated/cross-origin request was accepted')
                except urllib.error.HTTPError as error:
                    assert error.code == 403
            request = urllib.request.Request(endpoint + '/invalidate_caches', data=b'{}',
                                            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
            with opener.open(request, timeout=2) as response:
                assert response.status == 204
            print('Compiled token service: authenticated ping and cache operation passed; unauthenticated and cross-origin requests rejected.')
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


if __name__ == '__main__':
    main()
