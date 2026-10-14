"""Cooperative cancellation for Pywikibot calls made by desktop workers."""

from contextlib import contextmanager
from functools import wraps
import threading

import pywikibot
from pywikibot.data.api import Request
from pywikibot.throttle import Throttle


class OperationCancelled(BaseException):
    """Unwind library retries and broad per-page exception handlers on Stop."""


class EditFilterRejected(BaseException):
    """An edit filter needs human review, rather than an automatic retry."""

    def __init__(self, code, filter_id='', description=''):
        self.code = code
        self.filter_id = str(filter_id or '')
        self.description = str(description or '')


_current = threading.local()
_install_lock = threading.Lock()
_installed = False


def _check_cancelled():
    worker = getattr(_current, 'worker', None)
    if worker is not None and (worker._stop or worker._stop_event.is_set()):
        raise OperationCancelled()
    return worker


def _install_hooks():
    global _installed
    with _install_lock:
        if _installed:
            return
        original_sleep = pywikibot.sleep
        original_http = Request._http_request
        original_json = Request._json_loads
        original_throttle_wait = Throttle.wait

        @wraps(original_sleep)
        def sleep(seconds):
            worker = _check_cancelled()
            if worker is None:
                return original_sleep(seconds)
            if seconds >= 30:
                pywikibot.stopme()
            worker.progress.emit(worker._fmt('log.base.api_retry_pause', seconds=seconds))
            if not worker._interruptible_wait(seconds):
                raise OperationCancelled()

        @wraps(original_http)
        def http_request(request, *args, **kwargs):
            _check_cancelled()
            return original_http(request, *args, **kwargs)

        @wraps(original_throttle_wait)
        def throttle_wait(seconds):
            worker = _check_cancelled()
            if worker is None:
                return original_throttle_wait(seconds)
            if seconds >= 1:
                worker.progress.emit(worker._fmt('log.base.api_retry_pause', seconds=seconds))
            if not worker._interruptible_wait(seconds):
                raise OperationCancelled()

        @wraps(original_json)
        def json_loads(request, response):
            # Parse a successful response even if Stop arrived in flight, so a
            # completed save is still counted. Never send another request.
            result = original_json(request, response)
            worker = getattr(_current, 'worker', None)
            error = result.get('error', {}) if isinstance(result, dict) else {}
            if worker is not None and error:
                _check_cancelled()
                if error.get('code') in {'abusefilter-warning', 'abusefilter-disallowed'}:
                    details = error.get('abusefilter') or {}
                    raise EditFilterRejected(error['code'], details.get('id'), details.get('description'))
            return result

        pywikibot.sleep = sleep
        Request._http_request = http_request
        Request._json_loads = json_loads
        Throttle.wait = staticmethod(throttle_wait)
        _installed = True


@contextmanager
def pywikibot_operation(worker):
    """Scope cancellation to this worker thread; leave other operations alone."""
    _install_hooks()
    previous = getattr(_current, 'worker', None)
    _current.worker = worker
    try:
        _check_cancelled()
        yield
    finally:
        _current.worker = previous
