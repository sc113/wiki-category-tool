"""Exercise cancellation against Pywikibot's real request/retry loop, offline."""

# ruff: noqa: E402
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pywikibot
from pywikibot.data.api import Request
from pywikibot.throttle import Throttle
from PySide6.QtWidgets import QApplication, QWidget
from PySide6.QtTest import QTest
from shiboken6 import isValid

from wiki_cat_tool.core.pywikibot_operation import OperationCancelled, pywikibot_operation
from wiki_cat_tool.gui.tabs.replace_tab import ReplaceTab
from wiki_cat_tool.workers.base_worker import BaseWorker
from wiki_cat_tool.workers.replace_worker import ReplaceWorker


class OfflineRequest(Request):
    """Skip site discovery while retaining the installed submit/HTTP/JSON code."""

    def __init__(self):
        self.site = Mock()
        self.site.ratelimit.return_value.delay = 20
        self.action, self.write, self.throttle = 'edit', True, False
        self._params = {'action': ['edit']}
        self._add_defaults = Mock()
        self._use_get = Mock(return_value=False)
        self._http_param_string = Mock(return_value='action=edit')
        self._simulate = Mock(return_value=None)
        self._get_request_params = Mock(return_value=(False, '/w/api.php', {}, {}))
        self._userinfo_query = Mock(return_value=False)
        self._handle_warnings = Mock(return_value=False)
        self._logged_in = Mock(return_value=True)
        self._internal_api_error = Mock(return_value=False)


def response(payload):
    return Mock(headers={}, json=Mock(return_value=payload))


class RetryWorker(BaseWorker):
    def run(self):
        self.after_request = False
        OfflineRequest().submit()
        self.after_request = True


class WorkerCancellationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_stop_interrupts_internal_rate_limit_wait_before_next_http_request(self):
        worker = RetryWorker('', '', 'zh', 'wikipedia')
        waiting = threading.Event()
        original_wait = worker._interruptible_wait
        def wait(seconds):
            waiting.set()
            return original_wait(seconds)
        with patch.object(worker, '_interruptible_wait', side_effect=wait), \
             patch('pywikibot.data.api._requests.http.request', return_value=response(
                 {'error': {'code': 'ratelimited', 'info': 'Please wait'}})) as http, \
             patch('pywikibot.warning'), patch('pywikibot.log'):
            worker.start()
            try:
                self.assertTrue(waiting.wait(2))
                worker.request_stop()
                self.assertTrue(worker.wait(1000))
                self.assertFalse(worker.after_request)
                self.assertFalse(worker.failed)
                self.assertEqual(1, http.call_count)
            finally:
                worker.request_stop()
                worker.wait(2000)

    def test_throttle_wait_is_interruptible_and_cancellation_is_thread_scoped(self):
        stopped = BaseWorker('', '', 'zh', 'wikipedia')
        other = BaseWorker('', '', 'ru', 'wikipedia')
        with pywikibot_operation(stopped):
            stopped.request_stop()
            with self.assertRaises(OperationCancelled):
                Throttle.wait(20)
        with patch.object(other, '_interruptible_wait', return_value=True) as wait:
            with pywikibot_operation(other):
                Throttle.wait(0.1)
                pywikibot.sleep(0.1)
            self.assertEqual(2, wait.call_count)

    def test_unscoped_pywikibot_sleep_keeps_the_original_behavior(self):
        with pywikibot_operation(BaseWorker('', '', 'zh', 'wikipedia')):
            pass
        with patch.object(sys.modules[pywikibot.sleep.__wrapped__.__module__], 'time_sleep') as sleep:
            pywikibot.sleep(0.1)
        sleep.assert_called_once_with(0.1)

    def _replace(self, directory):
        path = Path(directory) / 'categories.tsv'
        path.write_text(''.join(f'Page {i}\tnew text\n' for i in range(7)), encoding='utf-8')
        worker = ReplaceWorker(str(path), '', '', 'zh', 'wikipedia', 'auto', 'summary', False)
        worker._save_min_interval = 0
        return worker

    def test_edit_filter_stops_after_five_saves_without_acknowledging_or_retrying(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = self._replace(directory)
            page = Mock()
            page.exists.return_value = True
            page.save.side_effect = lambda **kwargs: OfflineRequest().submit()
            payloads = [response({'edit': {'result': 'Success'}}) for _ in range(5)]
            payloads.append(response({'error': {'code': 'abusefilter-warning',
                                               'info': 'slow down', 'abusefilter': {
                                                   'id': '253', 'description': 'New-user edit frequency'}}}))
            logs = []
            worker.progress.connect(logs.append)
            with patch('wiki_cat_tool.workers.replace_worker.pywikibot.Site', return_value=Mock()), \
                 patch('wiki_cat_tool.workers.replace_worker.pywikibot.Page', return_value=page), \
                 patch('wiki_cat_tool.workers.replace_worker.normalize_title_by_selection', side_effect=lambda title, *args: title), \
                 patch('pywikibot.data.api._requests.http.request', side_effect=payloads) as http:
                worker.run()
            self.assertTrue(worker.failed)
            self.assertEqual(5, worker.saved_edits)
            self.assertEqual((6, 5, 1), (worker.stats['total'], worker.stats['updated'], worker.stats['failed']))
            self.assertEqual(6, http.call_count)
            self.assertIn('253', worker.failure_message)
            self.assertIn('New-user edit frequency', worker.failure_message)
            self.assertEqual(worker.failure_message, logs[-1])

    def test_successful_inflight_save_is_counted_after_stop_without_starting_another(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = self._replace(directory)
            page = Mock()
            page.exists.return_value = True
            page.save.side_effect = lambda **kwargs: OfflineRequest().submit()
            def complete(*args, **kwargs):
                worker.request_stop()
                return response({'edit': {'result': 'Success'}})
            with patch('wiki_cat_tool.workers.replace_worker.pywikibot.Site', return_value=Mock()), \
                 patch('wiki_cat_tool.workers.replace_worker.pywikibot.Page', return_value=page), \
                 patch('wiki_cat_tool.workers.replace_worker.normalize_title_by_selection', side_effect=lambda title, *args: title), \
                 patch('pywikibot.data.api._requests.http.request', side_effect=complete) as http:
                worker.run()
            self.assertEqual(1, worker.saved_edits)
            self.assertEqual(1, worker.stats['updated'])
            self.assertEqual(1, http.call_count)
            self.assertFalse(worker.failed)

    def test_replace_tab_blocks_restart_until_finished_and_reenables_controls(self):
        parent = QWidget()
        parent._ui_lang = 'ru'
        tab = ReplaceTab(parent)
        worker = tab.rworker = Mock()
        worker.isRunning.return_value = True
        worker.stats = dict(total=5, updated=5, missing=0, failed=0, invalid=0)
        worker.saved_edits = 5
        worker.failed = False
        worker._stop = True
        tab.replace_btn.setEnabled(False)
        tab.preview_btn.setEnabled(False)
        with patch('wiki_cat_tool.gui.tabs.replace_tab.ReplaceWorker') as create:
            tab.start_replace()
            tab.preview_replace()
            create.assert_not_called()
        tab.stop_replace()
        worker.request_stop.assert_called_once()
        self.assertFalse(tab.replace_btn.isEnabled())
        self.assertFalse(tab.preview_btn.isEnabled())
        self.assertFalse(tab.replace_stop_btn.isEnabled())
        worker.isRunning.return_value = False
        with patch.object(tab, 'sender', return_value=Mock()):
            tab._on_replace_finished()
        self.assertIs(worker, tab.rworker)
        tab._on_replace_finished()
        self.assertIsNone(tab.rworker)
        self.assertTrue(tab.replace_btn.isEnabled())
        self.assertTrue(tab.preview_btn.isEnabled())
        self.assertFalse(tab.replace_stop_btn.isEnabled())
        worker.deleteLater.assert_called_once()
        parent.close()

    def test_real_worker_finished_signal_restores_replace_tab_after_stop(self):
        parent = QWidget()
        parent._ui_lang = 'ru'
        tab = ReplaceTab(parent)
        worker = tab.rworker = RetryWorker('', '', 'zh', 'wikipedia')
        worker.finished.connect(tab._on_replace_finished)
        tab.replace_btn.setEnabled(False)
        tab.preview_btn.setEnabled(False)
        tab.replace_stop_btn.setEnabled(True)
        waiting = threading.Event()
        original_wait = worker._interruptible_wait
        def wait(seconds):
            waiting.set()
            return original_wait(seconds)
        with patch.object(worker, '_interruptible_wait', side_effect=wait), \
             patch('pywikibot.data.api._requests.http.request', return_value=response(
                 {'error': {'code': 'ratelimited', 'info': 'Please wait'}})), \
             patch('pywikibot.warning'), patch('pywikibot.log'):
            worker.start()
            try:
                self.assertTrue(waiting.wait(2))
                tab.stop_replace()
                self.assertTrue(worker.wait(1000))
                for _ in range(40):
                    if tab.rworker is None:
                        break
                    QTest.qWait(5)
                self.assertIsNone(tab.rworker)
                self.assertTrue(tab.replace_btn.isEnabled())
                self.assertTrue(tab.preview_btn.isEnabled())
                self.assertFalse(tab.replace_stop_btn.isEnabled())
            finally:
                if isValid(worker) and worker.isRunning():
                    worker.request_stop()
                    worker.wait(2000)
        parent.close()


if __name__ == '__main__':
    unittest.main()
