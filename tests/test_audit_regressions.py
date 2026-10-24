"""Offline behavioral regressions for the October 2026 audit."""
import asyncio
import ast
import copy
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.archive import ArchiveManager
from core.config import ConfigManager
from core.exceptions import ConfigError, DownloadFailedError
from core.files import contained_path, safe_name
from core.models import ArchiveEntry, BatchSummaryReport, DownloadTask, TaskStatus
from core.queue_manager import QueueManager
from core.workers import run_blocking

ROOT = Path(__file__).resolve().parent.parent


def test_competing_queue_instances_preserve_additions(tmp_path):
    first = QueueManager(tmp_path / 'queue.json')
    second = QueueManager(first.queue_file)
    first.add('https://example.com/a.mp4')
    second.add('https://example.com/b.mp4')
    assert {item.url for item in first.list_all()} == {
        'https://example.com/a.mp4', 'https://example.com/b.mp4'}


def test_queue_writes_are_serialized_across_processes(tmp_path):
    queue = tmp_path / 'queue.json'
    script = "from core.queue_manager import QueueManager; import sys; q=QueueManager(sys.argv[1]); [q.add('https://example.com/'+sys.argv[2]+'/'+str(i)) for i in range(8)]"
    children = [subprocess.Popen([sys.executable, '-c', script, str(queue), str(i)], cwd=ROOT,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(3)]
    for child in children:
        _, error = child.communicate(timeout=40)
        assert child.returncode == 0, error.decode(errors='replace')
    assert len(QueueManager(queue).list_all()) == 24


def test_queue_recovers_abandoned_work_but_preserves_live_claim(tmp_path):
    queue = QueueManager(tmp_path / 'queue.json')
    item = queue.add('https://example.com/a.mp4')
    assert queue.mark_status(item.id, TaskStatus.DOWNLOADING)
    assert QueueManager(queue.queue_file).get_next() is None
    assert queue.mark_status(item.id, TaskStatus.DOWNLOADING) is False
    data = json.loads(queue.queue_file.read_text())
    data[0]['owner_pid'] = None
    queue.queue_file.write_text(json.dumps(data))
    recovered = QueueManager(queue.queue_file)
    assert recovered.get_next().id == item.id


def test_corrupt_queue_is_not_silently_overwritten(tmp_path):
    path = tmp_path / 'queue.json'
    path.write_text('[{')
    with pytest.raises(ConfigError, match='preserved'):
        QueueManager(path)
    assert path.read_text() == '[{'


def test_active_queue_items_cannot_be_deleted(tmp_path):
    queue = QueueManager(tmp_path / 'queue.json')
    item = queue.add('https://example.com/a.mp4')
    queue.mark_status(item.id, TaskStatus.DOWNLOADING)
    assert queue.clear() == 0
    with pytest.raises(ConfigError, match='active download'):
        queue.remove(item.id)


@pytest.mark.parametrize('path,value', [('engine_limits.yt-dlp', -1), ('concurrent_downloads', 'abc'),
                                      ('domain_limits.max_per_domain', 0), ('network.timeout', 0),
                                      ('general.organize_by_site', 'maybe'), ('general.typo', 1)])
def test_invalid_config_changes_are_rejected_without_persisting(tmp_path, path, value):
    config = ConfigManager(tmp_path / 'config.json')
    before = config.config_path.read_bytes()
    with pytest.raises(ConfigError):
        config.set_path(path, value)
    assert config.config_path.read_bytes() == before


def test_secret_settings_are_persisted_outside_public_config(tmp_path):
    config = ConfigManager(tmp_path / 'config.json')
    config.set('telegram_dl', 'api_hash', 'audit-test-secret')
    assert 'audit-test-secret' not in config.config_path.read_text()
    assert ConfigManager(config.config_path).get('telegram_dl', 'api_hash') == 'audit-test-secret'


def test_config_concurrent_edits_preserve_unrelated_changes(tmp_path):
    first = ConfigManager(tmp_path / 'config.json')
    second = ConfigManager(first.config_path)
    first.set('general', 'concurrent_downloads', 5)
    second.set('general', 'organize_by_site', False)
    restored = ConfigManager(first.config_path)
    assert restored.get('general', 'concurrent_downloads') == 5
    assert restored.get('general', 'organize_by_site') is False


def test_environment_proxy_is_used_by_runtime_config(tmp_path, monkeypatch):
    config = ConfigManager(tmp_path / 'config.json')
    monkeypatch.setenv('GLOBAL_PROXY', 'http://proxy.invalid:8888')
    assert config.proxy == 'http://proxy.invalid:8888'
    for engine in ['yt-dlp', 'gallery-dl', 'telegram-dl', 'terabox-dl', 'cyberdrop-dl']:
        assert config.get_backend_config(engine)['proxy'] == config.proxy


@pytest.mark.parametrize('name', ['..', '.', 'CON', 'con.txt', 'nul', 'LPT1.mp4', '\x1b[31mfile'])
def test_untrusted_path_components_are_safe(name, tmp_path):
    result = safe_name(name)
    assert result not in ('.', '..', '')
    assert '\x1b' not in result
    assert contained_path(tmp_path, result).is_relative_to(tmp_path.resolve())


def test_path_containment_rejects_escape(tmp_path):
    with pytest.raises(ValueError, match='escapes'):
        contained_path(tmp_path, '..', 'escape.bin')


def test_telegram_distinct_messages_have_distinct_names():
    from backends.telegram_backend import _get_message_filename
    first = SimpleNamespace(file=SimpleNamespace(name='video.mp4'), id=1)
    second = SimpleNamespace(file=SimpleNamespace(name='video.mp4'), id=2)
    assert _get_message_filename(first) != _get_message_filename(second)


@pytest.mark.asyncio
async def test_telegram_force_replaces_existing_file_and_checks_length(tmp_path):
    from backends.telegram_backend import TelegramDlBackend
    backend = TelegramDlBackend.__new__(TelegramDlBackend)
    backend.config = {}
    target = tmp_path / 'test.mp4'
    target.write_bytes(b'old!')
    class Client:
        async def iter_download(self, **kwargs):
            yield b'new!'
    with patch('backends.telegram_backend._resolve_media_object', return_value=object()), patch('backends.telegram_backend._get_file_size', return_value=4):
        await backend._download_message_stream(Client(), object(), target, force=True)
    assert target.read_bytes() == b'new!'
    class ShortClient:
        async def iter_download(self, **kwargs):
            yield b'x'
    with patch('backends.telegram_backend._resolve_media_object', return_value=object()), patch('backends.telegram_backend._get_file_size', return_value=4):
        with pytest.raises(DownloadFailedError):
            await backend._download_message_stream(ShortClient(), object(), target, force=True)
    assert target.read_bytes() == b'new!'


def fake_terabox(tmp_path, content=b'complete!!', status=200, headers=None):
    from backends.terabox_backend import TeraboxDlBackend
    backend = TeraboxDlBackend.__new__(TeraboxDlBackend)
    backend.config = {'max_retries': 1, 'fallback_browser_cookies': None}
    backend._cached_js_token = None
    class Response:
        status_code = status
        def __init__(self):
            self.headers = headers or {}
        def iter_content(self, **kwargs):
            yield content
        def close(self):
            pass
    class Session:
        headers = {}
        proxies = {}
        closed = False
        def get(self, *args, **kwargs):
            return Response()
        def close(self):
            self.closed = True
    session = Session()
    backend._create_session = lambda cookie: session
    backend._resolve_ndus_cookie = lambda opts: ''
    backend._get_share_info = lambda *args: dict(title='audit', share_id=1, uk=1, sign='s', timestamp=1, randsk='r', file_list=[dict(isdir=0, server_filename='test.bin', size=10, fs_id=1, dlink='https://example.invalid/file')])
    task = DownloadTask(url='https://terabox.com/s/1audit', backend='terabox-dl', output_dir=str(tmp_path), options={'quiet': True})
    return backend, task, session


@pytest.mark.asyncio
async def test_terabox_rejects_truncation_and_does_not_create_final_file(tmp_path):
    backend, task, session = fake_terabox(tmp_path, b'abc')
    with pytest.raises(DownloadFailedError, match='expected 10'):
        await backend.download(task)
    assert session.closed
    assert not list(tmp_path.rglob('*.bin'))
    assert list(tmp_path.rglob('*.part'))


@pytest.mark.asyncio
async def test_terabox_rejects_wrong_range(tmp_path):
    backend, task, session = fake_terabox(tmp_path, b'x' * 10, 206, {'Content-Range': 'bytes 2-11/12'})
    with pytest.raises(DownloadFailedError, match='Content-Range'):
        await backend.download(task)
    assert session.closed


@pytest.mark.asyncio
async def test_terabox_partial_batches_are_failures(tmp_path):
    backend, task, _ = fake_terabox(tmp_path)
    info = backend._get_share_info(None)
    info['file_list'].append(dict(isdir=0, server_filename='missing.bin', size=10, fs_id=2))
    backend._get_share_info = lambda *args: info
    backend._get_download_link = lambda *args, **kwargs: None
    with pytest.raises(DownloadFailedError, match='Partial TeraBox'):
        await backend.download(task)
    assert list(tmp_path.rglob('*.bin'))


def test_gallery_jobs_reset_filters_and_restore_external_config(tmp_path):
    from backends.gallerydl_backend import GalleryDlBackend, gdl_config
    backend = GalleryDlBackend({'config_file': str(tmp_path / 'empty.json')})
    previous = copy.deepcopy(gdl_config._config)
    try:
        gdl_config.set(('extractor',), 'filter', 'outside-filter')
        def filtered():
            assert gdl_config.get(('extractor',), 'filter') is None
            gdl_config.set(('extractor',), 'filter', 'images-only')
        backend._isolated_run(filtered)
        assert gdl_config.get(('extractor',), 'filter') == 'outside-filter'
        backend._isolated_run(lambda: None)
        assert gdl_config.get(('extractor',), 'filter') == 'outside-filter'
    finally:
        gdl_config.clear()
        gdl_config._config.update(previous)


@pytest.mark.asyncio
async def test_cancellation_joins_worker_before_returning():
    import threading
    started = threading.Event()
    cancelled = threading.Event()
    done = threading.Event()
    def worker():
        started.set()
        assert cancelled.wait(5)
        done.set()
    task = asyncio.create_task(run_blocking(worker, cancel_event=cancelled))
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert done.is_set()


@pytest.mark.asyncio
async def test_dispatcher_cancellation_requeues_claimed_item(tmp_path):
    from core.dispatcher import QueueDispatcher
    from core.gui_tracker import GuiMultiBarTracker
    config = ConfigManager(tmp_path / 'config.json')
    queue = QueueManager(tmp_path / 'queue.json')
    item = queue.add('https://example.com/a.mp4')
    started = asyncio.Event()
    class Backend:
        name = 'yt-dlp'
        async def download(self, *args, **kwargs):
            started.set()
            await asyncio.Event().wait()
    backend = Backend()
    router = SimpleNamespace(route=lambda *a, **k: backend, backends={})
    dispatcher = QueueDispatcher(config, queue, ArchiveManager(tmp_path / 'archive.db'), router)
    task = asyncio.create_task(dispatcher.run(tracker=GuiMultiBarTracker(), quiet=True))
    await asyncio.wait_for(started.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert queue.get_item(item.id).status == TaskStatus.QUEUED


def test_pending_archive_records_recover_on_next_start(tmp_path):
    archive = ArchiveManager(tmp_path / 'archive.db')
    entry = ArchiveEntry(url='https://example.com/a', backend='yt-dlp', file_path=str(tmp_path / 'a'), file_name='a')
    archive.defer_entry(entry)
    restored = ArchiveManager(archive.db_path)
    assert restored.get_by_url(entry.url)
    assert not list((tmp_path / 'pending_archive').glob('*.json'))


@pytest.mark.asyncio
async def test_gui_buttons_are_async_and_links_are_queued_while_busy():
    tree = ast.parse((ROOT / 'multi-dl-gui.py').read_text(encoding='utf-8'))
    names = {node.name for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)}
    assert {'on_download_click', 'on_start_queue_click', 'on_watcher_switch_change'} <= names
    trigger = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == 'trigger_download')
    # Exercise the busy branch through a wrapper that supplies its closure state.
    factory = ast.parse('async def outer():\n    is_downloading = True\n').body[0]
    factory.body.append(trigger)
    factory.body.append(ast.Return(value=ast.Name(id='trigger_download', ctx=ast.Load())))
    ast.fix_missing_locations(factory)
    queue = MagicMock()
    namespace = dict(Optional=__import__('typing').Optional, List=list, queue_manager=queue, show_toast=lambda *a, **k: None)
    exec(compile(ast.Module(body=[factory], type_ignores=[]), 'gui_trigger_test', 'exec'), namespace)
    handler = await namespace['outer']()
    await handler(['https://example.com/new'])
    queue.add_batch.assert_called_once_with(['https://example.com/new'])


def test_cli_download_failure_uses_nonzero_exit():
    cli = importlib.import_module('multi-dl')
    async def failed():
        return BatchSummaryReport(total_items=1, failed_count=1)
    with pytest.raises(SystemExit) as error:
        cli.run_download(failed())
    assert error.value.code == 1


def test_updater_check_only_does_not_run_updaters():
    from core.updater import EngineUpdater
    updater = EngineUpdater.__new__(EngineUpdater)
    updater._get_pkg_version = lambda name: 'test-version'
    updater.update_self = MagicMock(side_effect=AssertionError('must not update'))
    assert updater.update_all(check_only=True)
    updater.update_self.assert_not_called()


def test_gui_tracker_does_not_reuse_active_slot_ids():
    from core.gui_tracker import GuiMultiBarTracker
    tracker = GuiMultiBarTracker()
    first = tracker.assign_slot('first', 'yt-dlp', 'first')
    active = tracker.assign_slot('active', 'yt-dlp', 'active')
    tracker.finish_slot(first)
    tracker.clear_finished()
    third = tracker.assign_slot('third', 'yt-dlp', 'third')
    assert third != active
    assert tracker.slots[active].task_id == 'active'


@pytest.mark.asyncio
async def test_dispatcher_uses_configured_fallback_and_preserves_engine_limit(tmp_path):
    from core.dispatcher import QueueDispatcher
    from core.gui_tracker import GuiMultiBarTracker
    config = ConfigManager(tmp_path / 'config.json')
    config.set('engine_limits', 'cyberdrop-dl', 1)
    config.set('archive', 'enabled', False)
    queue = QueueManager(tmp_path / 'queue.json')
    queue.add('https://example.com/album1', backend='gallery-dl')
    queue.add('https://example.org/album2', backend='gallery-dl')
    active = 0
    maximum = 0
    class Primary:
        name = 'gallery-dl'
        config = {}
        async def download(self, task, progress_callback=None):
            assert task.options['_managed_failover']
            raise DownloadFailedError('primary unavailable')
    class Fallback:
        name = 'cyberdrop-dl'
        def can_handle(self, url):
            return True
        async def download(self, task, progress_callback=None):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.02)
            active -= 1
            return ArchiveEntry(url=task.url, backend=self.name, file_path=str(tmp_path / 'a'), file_name='a', file_size=1)
    first, second = Primary(), Fallback()
    router = SimpleNamespace(route=lambda *a, **k: first, backends={'gallery-dl': first, 'cyberdrop-dl': second})
    dispatcher = QueueDispatcher(config, queue, ArchiveManager(tmp_path / 'archive.db'), router)
    result = await dispatcher.run(concurrency=2, tracker=GuiMultiBarTracker(), quiet=True)
    assert result.succeeded_count == 2
    assert maximum == 1


@pytest.mark.asyncio
async def test_telegram_proxy_reaches_client_constructor(monkeypatch):
    from backends.telegram_backend import TelegramDlBackend
    backend = TelegramDlBackend.__new__(TelegramDlBackend)
    backend.config = {'proxy': 'socks5://user:password@proxy.invalid:1080'}
    backend.client = None
    backend._resolve_credentials = lambda: (123, 'test-hash', 'test-session')
    client = SimpleNamespace(connect=AsyncMock(), is_user_authorized=AsyncMock(return_value=True))
    factory = MagicMock(return_value=client)
    monkeypatch.setattr('backends.telegram_backend.TelegramClient', factory)
    await backend._ensure_client()
    proxy = factory.call_args.kwargs['proxy']
    assert proxy['proxy_type'] == 'socks5'
    assert proxy['addr'] == 'proxy.invalid'
    assert proxy['port'] == 1080
    assert proxy['username'] == 'user'


def test_updater_reports_failed_git_pull(tmp_path, monkeypatch):
    from core.updater import EngineUpdater
    updater = EngineUpdater.__new__(EngineUpdater)
    updater.project_root = tmp_path
    (tmp_path / '.git').mkdir()
    monkeypatch.setattr('core.updater.shutil.which', lambda name: 'git')
    monkeypatch.setattr('core.updater.subprocess.run', MagicMock(side_effect=[
        SimpleNamespace(returncode=0, stdout='abc'),
        SimpleNamespace(returncode=1, stdout='', stderr='unavailable')]))
    ok, message = updater.update_self()
    assert ok is False
    assert 'failed' in message.lower()


@pytest.mark.parametrize('section', ['general', 'telegram_dl', 'network'])
def test_invalid_config_section_reports_config_error(tmp_path, section):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({section: []}))
    with pytest.raises(ConfigError, match='must be an object'):
        ConfigManager(path)


def test_clipboard_history_is_bounded_and_can_be_cleared(tmp_path, monkeypatch):
    import core.clipboard as clipboard
    monkeypatch.setattr(clipboard, 'CLIPBOARD_HISTORY_PATH', tmp_path / 'history.txt')
    monkeypatch.setattr(clipboard, 'HISTORY_MAX_URLS', 2)
    clipboard.append_clipboard_history(['https://example.com/old', 'https://example.com/new'])
    clipboard.append_clipboard_history(['https://example.com/latest'])
    assert clipboard.load_clipboard_history() == ['https://example.com/new', 'https://example.com/latest']
    clipboard.clear_clipboard_history()
    assert clipboard.load_clipboard_history() == []


@pytest.mark.asyncio
async def test_cyberdrop_force_no_archive_and_filters_use_supported_policy(tmp_path, monkeypatch):
    from backends.cyberdrop_backend import CyberdropDlBackend
    backend = CyberdropDlBackend({'enable_failover_to_gallerydl': False})
    monkeypatch.setattr(backend, 'is_installed', lambda: True)
    monkeypatch.setattr(backend, '_resolve_executable', lambda: ['synthetic-engine'])
    monkeypatch.setattr('backends.cyberdrop_backend._probe_file_size', lambda *a: (None, None))
    monkeypatch.setattr('backends.cyberdrop_backend._query_cdl_db_size', lambda *a, **k: (None, None))
    commands = []
    async def launch(*args, **kwargs):
        commands.extend(args)
        destination = Path(args[args.index('--download-folder') + 1])
        (destination / 'sample.jpg').write_bytes(b'JPEG')
        return SimpleNamespace(returncode=0, stdout=SimpleNamespace(readline=AsyncMock(return_value=b'')), wait=AsyncMock())
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', launch)
    task = DownloadTask(url='https://example.invalid/a', backend='cyberdrop-dl', output_dir=str(tmp_path),
                        options={'force': True, 'no_archive': True, 'images_only': True, 'quiet': True,
                                 'proxy': 'http://user:synthetic-secret@proxy.invalid:8080'})
    entry = await backend.download(task)
    assert Path(entry.file_path).parent.name.startswith('forced_')
    assert '--images' in commands and '--no-videos' in commands and '--no-audio' in commands
    assert '--ignore-history' in commands and '--ignore-hashes' in commands
    assert not Path(commands[commands.index('--database-file') + 1]).parent.exists()
    assert 'synthetic-secret' not in json.dumps(entry.metadata)


def test_custom_auth_config_does_not_rewrite_project_environment(tmp_path):
    from backends.terabox_backend import TeraboxDlBackend
    env = ROOT / '.env'
    before = env.read_bytes() if env.exists() else None
    backend = TeraboxDlBackend({'config_file': str(tmp_path / 'terabox.json'), 'ndus_cookie': 'synthetic-new-cookie'})
    backend._save_modular_config()
    after = env.read_bytes() if env.exists() else None
    assert after == before
    assert json.loads((tmp_path / 'terabox.json').read_text())['ndus_cookie'] == 'synthetic-new-cookie'


def test_token_provider_guard_rejects_unauthorized_requests_and_interpreters():
    import shutil
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is optional and not installed')
    script = '''
        import assert from 'node:assert/strict';
        const { guard, validateInterpreterUrl } = await import(process.argv[1]);
        let rejected = 0, accepted = 0;
        const res = { status(code) { assert.equal(code, 403); rejected++; return this; }, json() {} };
        guard({get: () => undefined}, res, () => accepted++);
        guard({get: key => key === 'authorization' ? 'Bearer ' + process.env.MULTI_DL_POT_TOKEN : undefined}, res, () => accepted++);
        guard({get: key => key === 'origin' ? 'https://untrusted.invalid' : 'Bearer ' + process.env.MULTI_DL_POT_TOKEN}, res, () => accepted++);
        assert.equal(rejected, 2); assert.equal(accepted, 1);
        assert.throws(() => validateInterpreterUrl('//untrusted.invalid/payload.js'));
        assert.throws(() => validateInterpreterUrl('//www.google.com/redirect'));
        assert.equal(validateInterpreterUrl('//www.google.com/js/bg/test.js'), 'https://www.google.com/js/bg/test.js');
    '''
    env = dict(os.environ, MULTI_DL_POT_TOKEN='synthetic-test-token-' + 'x' * 32)
    result = subprocess.run([node, '--input-type=module', '-e', script,
                             (ROOT / 'support' / 'pot_security.js').as_uri()], env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_cyberdrop_cancellation_terminates_subprocess(tmp_path, monkeypatch):
    from backends.cyberdrop_backend import CyberdropDlBackend
    backend = CyberdropDlBackend()
    monkeypatch.setattr(backend, 'is_installed', lambda: True)
    monkeypatch.setattr(backend, '_resolve_executable', lambda: ['synthetic-engine'])
    monkeypatch.setattr('backends.cyberdrop_backend._probe_file_size', lambda *a: (None, None))
    monkeypatch.setattr('backends.cyberdrop_backend._query_cdl_db_size', lambda *a, **k: (None, None))
    started = asyncio.Event()
    async def read():
        started.set()
        await asyncio.Event().wait()
    proc = SimpleNamespace(returncode=None, stdout=SimpleNamespace(readline=read),
                           terminate=MagicMock(), kill=MagicMock(), wait=AsyncMock())
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(return_value=proc))
    task = asyncio.create_task(backend.download(DownloadTask(url='https://example.invalid/a', backend='cyberdrop-dl', output_dir=str(tmp_path), options={'quiet': True})))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    proc.terminate.assert_called_once()
    proc.wait.assert_awaited()
