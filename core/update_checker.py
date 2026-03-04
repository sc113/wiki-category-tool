# -*- coding: utf-8 -*-
"""
Модуль для проверки обновлений приложения.

Проверяет наличие новых версий через GitHub API и уведомляет пользователя.
"""

import requests
from packaging import version
from typing import Optional, Tuple
from ..constants import APP_VERSION, GITHUB_API_RELEASES, REQUEST_HEADERS


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

        debug(f"Запрос к: {GITHUB_API_RELEASES}")
        debug(f"Заголовки: {REQUEST_HEADERS}")

        # Запрос к GitHub API для получения последнего релиза
        debug("Отправляем HTTP запрос...")
        response = requests.get(
            GITHUB_API_RELEASES + '/latest',
            headers=REQUEST_HEADERS,
            timeout=timeout
        )

        debug(f"Получен ответ: статус {response.status_code}")

        if response.status_code != 200:
            debug(f"Неуспешный статус код: {response.status_code}")
            return None

        debug("Парсим JSON ответ...")
        data = response.json()

        # Получаем версию из тега (убираем 'v' если есть)
        latest_version = data.get('tag_name', '').lstrip('v')
        release_url = data.get('html_url', '')

        debug(f"Последняя версия на GitHub: {latest_version}")

        if not latest_version:
            debug("Версия не найдена в ответе")
            return None

        # Сравниваем версии
        try:
            current = version.parse(APP_VERSION)
            latest = version.parse(latest_version)

            if latest > current:
                debug(f"Найдено обновление: {latest_version} > {APP_VERSION}")
                return (latest_version, release_url)
            else:
                debug(
                    f"Текущая версия актуальна: {APP_VERSION} >= {latest_version}")
        except Exception as e:
            debug(f"Ошибка парсинга версий: {e}")
            # Если не удалось распарсить версию, просто сравниваем строки
            if latest_version != APP_VERSION:
                debug(
                    f"Найдено обновление (сравнение строк): {latest_version} != {APP_VERSION}")
                return (latest_version, release_url)

    except requests.exceptions.Timeout as e:
        try:
            from ..utils import debug
            debug(f"Таймаут при проверке обновлений: {e}")
        except Exception:
            pass
    except requests.exceptions.RequestException as e:
        try:
            from ..utils import debug
            debug(f"Ошибка сети при проверке обновлений: {e}")
        except Exception:
            pass
    except Exception as e:
        # Если проверка не удалась, просто продолжаем без уведомления
        try:
            from ..utils import debug
            debug(f"Неожиданная ошибка при проверке обновлений: {e}")
        except Exception:
            pass

    return None
