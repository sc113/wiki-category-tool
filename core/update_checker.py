# -*- coding: utf-8 -*-
"""
Модуль для проверки обновлений приложения.

Проверяет наличие новых версий через GitHub API и уведомляет пользователя.
"""

import requests
from packaging import version
from typing import Optional, Tuple
from ..constants import APP_VERSION, GITHUB_API_RELEASES, REQUEST_HEADERS
from .localization import translate_runtime


def _t(key: str) -> str:
    return translate_runtime(key, '')


def _fmt(key: str, **kwargs) -> str:
    text = _t(key)
    try:
        return text.format(**kwargs)
    except Exception:
        return text


def check_for_updates(timeout: int = 5) -> Optional[Tuple[str, str]]:
    """
    Проверяет наличие новой версии приложения на GitHub.

    Args:
        timeout: Таймаут запроса в секундах

    Returns:
        Кортеж (новая_версия, url_скачивания) если есть обновление, иначе None
    """
    try:
        from ..utils import debug

        debug(_fmt('log.auth.request_url', url=GITHUB_API_RELEASES))
        debug(_fmt('log.auth.request_headers', headers=REQUEST_HEADERS))

        # Запрос к GitHub API для получения последнего релиза
        debug(_t('log.auth.http_request_start'))
        response = requests.get(
            GITHUB_API_RELEASES + '/latest',
            headers=REQUEST_HEADERS,
            timeout=timeout
        )

        debug(_fmt('log.auth.response_status', status=response.status_code))

        if response.status_code != 200:
            debug(_fmt('log.update_checker.bad_status', status=response.status_code))
            return None

        debug(_t('log.auth.json_parse'))
        data = response.json()

        # Получаем версию из тега (убираем 'v' если есть)
        latest_version = data.get('tag_name', '').lstrip('v')
        release_url = data.get('html_url', '')

        debug(_fmt('log.update_checker.latest_version', version=latest_version))

        if not latest_version:
            debug(_t('log.update_checker.version_missing'))
            return None

        # Сравниваем версии
        try:
            current = version.parse(APP_VERSION)
            latest = version.parse(latest_version)

            if latest > current:
                debug(_fmt('log.update_checker.update_found', latest=latest_version, current=APP_VERSION))
                return (latest_version, release_url)
            else:
                debug(_fmt('log.update_checker.up_to_date', current=APP_VERSION, latest=latest_version))
        except Exception as e:
            debug(_fmt('log.update_checker.version_parse_error', error=e))
            # Если не удалось распарсить версию, просто сравниваем строки
            if latest_version != APP_VERSION:
                debug(_fmt('log.update_checker.update_found_string_compare', latest=latest_version, current=APP_VERSION))
                return (latest_version, release_url)

    except requests.exceptions.Timeout as e:
        try:
            from ..utils import debug
            debug(_fmt('log.update_checker.timeout', error=e))
        except Exception:
            pass
    except requests.exceptions.RequestException as e:
        try:
            from ..utils import debug
            debug(_fmt('log.update_checker.network_error', error=e))
        except Exception:
            pass
    except Exception as e:
        # Если проверка не удалась, просто продолжаем без уведомления
        try:
            from ..utils import debug
            debug(_fmt('log.update_checker.unexpected_error', error=e))
        except Exception:
            pass

    return None
