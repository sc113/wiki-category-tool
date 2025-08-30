"""
Base worker class with common rate limiting functionality for Wiki Category Tool.
"""

import time
from PySide6.QtCore import QThread, Signal
import pywikibot


class BaseWorker(QThread):
    """
    Базовый класс для всех worker'ов с общей функциональностью rate limiting.
    
    Обеспечивает:
    - Адаптивный rate limiting с базовым интервалом 0.25 сек и максимальным 2.5 сек
    - Retry логику с экспоненциальным backoff (6 попыток)
    - Распознавание rate limit ошибок
    - Сигнал progress для отправки сообщений в UI
    - Корректную остановку операций
    """
    
    progress = Signal(str)
    
    def __init__(self, username: str, password: str, lang: str, family: str):
        """
        Инициализация базового worker'а.
        
        Args:
            username: Имя пользователя для авторизации
            password: Пароль для авторизации  
            lang: Код языка (ru, en, etc.)
            family: Семейство проекта (wikipedia, commons, etc.)
        """
        super().__init__()
        self.username = username
        self.password = password
        self.lang = lang
        self.family = family
        self._stop = False
        
        # Инициализация rate limiting
        self._init_save_ratelimit()
    
    def request_stop(self):
        """Запрос на корректную остановку операции."""
        self._stop = True
        # Попробуем разбудить поток, если он спит в ожидании
        try:
            # Небольшой пинок событий, чтобы выйти из внутренних циклов ожидания
            time.sleep(0)
        except Exception:
            pass

    def graceful_stop(self, timeout_ms: int = 7000):
        """Корректно останавливает поток и ждёт завершения."""
        try:
            self.request_stop()
        except Exception:
            pass
        try:
            # Если у потока есть цикл событий (обычно нет), попросим его завершиться
            self.quit()
        except Exception:
            pass
        try:
            self.wait(timeout_ms)
        except Exception:
            pass
        # Если поток всё ещё работает, принудительно завершаем
        try:
            if hasattr(self, 'isRunning') and self.isRunning():
                self.terminate()
                self.wait(2000)
        except Exception:
            pass
    
    def _init_save_ratelimit(self):
        """Инициализация адаптивного rate limiting."""
        # Базовый минимальный интервал между сохранениями; будет адаптироваться
        self._save_min_interval = 0.25
        self._last_save_ts = 0.0
    
    def _wait_before_save(self):
        """Ожидание перед сохранением согласно rate limiting."""
        now = time.time()
        to_wait = max(0.0, (self._last_save_ts + self._save_min_interval) - now)
        if to_wait > 0:
            time.sleep(to_wait)
        self._last_save_ts = time.time()
    
    def _increase_save_interval(self, attempt: int):
        """
        Увеличение интервала при срабатывании rate limits.
        
        Args:
            attempt: Номер попытки (для экспоненциального backoff)
        """
        # Увеличиваем интервал агрессивно при срабатывании лимитов
        target = max(self._save_min_interval * 1.5, 0.6 * attempt)
        self._save_min_interval = min(target, 2.5)
    
    def _decay_save_interval(self):
        """Плавное уменьшение интервала при стабильной работе."""
        self._save_min_interval = max(0.2, self._save_min_interval * 0.9)
    
    def _is_rate_error(self, err: Exception) -> bool:
        """
        Проверка, является ли ошибка связанной с rate limiting.
        
        Args:
            err: Исключение для проверки
            
        Returns:
            True если ошибка связана с rate limiting
        """
        msg = (str(err) or '').lower()
        return (
            '429' in msg or 'too many requests' in msg or 'ratelimit' in msg or
            'rate limit' in msg or 'maxlag' in msg or 'readonly' in msg
        )
    
    def _save_with_retry(self, page: 'pywikibot.Page', text: str, summary: str, minor: bool, retries: int = 6) -> bool:
        """
        Сохранение страницы с retry логикой и rate limiting.
        
        Args:
            page: Объект страницы pywikibot
            text: Новый текст страницы
            summary: Комментарий к правке
            minor: Флаг малой правки
            retries: Количество попыток (по умолчанию 6)
            
        Returns:
            True если сохранение успешно, False в противном случае
        """
        for attempt in range(1, retries + 1):
            try:
                self._wait_before_save()
                page.text = text
                page.save(summary=summary, minor=minor)
                self._decay_save_interval()
                return True
            except Exception as e:
                if self._is_rate_error(e) and attempt < retries:
                    self._increase_save_interval(attempt)
                    try:
                        self.progress.emit(f"Лимит запросов: пауза {self._save_min_interval:.2f}s · попытка {attempt}/{retries}")
                    except Exception:
                        pass
                    continue
                try:
                    self.progress.emit(f"Ошибка сохранения: {type(e).__name__}: {e}")
                except Exception:
                    pass
                return False
        return False