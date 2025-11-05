# -*- coding: utf-8 -*-
"""
Модуль для управления настройками проверки обновлений.
"""

import os
import json
from typing import Optional


class UpdateSettings:
    """Класс для управления настройками проверки обновлений"""
    
    def __init__(self, settings_dir: str):
        """
        Args:
            settings_dir: Путь к директории для хранения настроек
        """
        self.settings_file = os.path.join(settings_dir, 'update_settings.json')
        self.settings = self._load_settings()
    
    def _load_settings(self) -> dict:
        """Загружает настройки из файла"""
        if os.path.exists(self.settings_file):
            try:
                with open(self.settings_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        return {}
    
    def _save_settings(self):
        """Сохраняет настройки в файл"""
        try:
            os.makedirs(os.path.dirname(self.settings_file), exist_ok=True)
            with open(self.settings_file, 'w', encoding='utf-8') as f:
                json.dump(self.settings, f, indent=2, ensure_ascii=False)
        except Exception:
            pass
    
    def is_version_skipped(self, version: str) -> bool:
        """
        Проверяет, пропущена ли версия пользователем.
        
        Args:
            version: Версия для проверки
            
        Returns:
            True если версия пропущена, иначе False
        """
        return self.settings.get('skipped_version') == version
    
    def skip_version(self, version: str):
        """
        Отмечает версию как пропущенную.
        
        Args:
            version: Версия для пропуска
        """
        self.settings['skipped_version'] = version
        self._save_settings()
    
    def clear_skipped_version(self):
        """Очищает информацию о пропущенной версии"""
        if 'skipped_version' in self.settings:
            del self.settings['skipped_version']
            self._save_settings()

