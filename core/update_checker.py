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
        # Запрос к GitHub API для получения последнего релиза
        response = requests.get(
            GITHUB_API_RELEASES + '/latest',
            headers=REQUEST_HEADERS,
            timeout=timeout
        )
        
        if response.status_code != 200:
            return None
            
        data = response.json()
        
        # Получаем версию из тега (убираем 'v' если есть)
        latest_version = data.get('tag_name', '').lstrip('v')
        release_url = data.get('html_url', '')
        
        if not latest_version:
            return None
        
        # Сравниваем версии
        try:
            current = version.parse(APP_VERSION)
            latest = version.parse(latest_version)
            
            if latest > current:
                return (latest_version, release_url)
        except Exception:
            # Если не удалось распарсить версию, просто сравниваем строки
            if latest_version != APP_VERSION:
                return (latest_version, release_url)
                
    except Exception:
        # Если проверка не удалась, просто продолжаем без уведомления
        pass
    
    return None

