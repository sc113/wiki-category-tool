"""
Base worker class with common rate limiting functionality for Wiki Category Tool.
"""

import time
import re
from PySide6.QtCore import QThread, Signal
import pywikibot

from ..core.localization import translate_runtime


class BaseWorker(QThread):
    """
    Базовый класс для всех worker'ов с общей функциональностью rate limiting.
    
    Обеспечивает:
    - Адаптивный rate limiting с базовым интервалом 0.25 сек и максимальным 2.5 сек
    - Retry логику с экспоненциальным backoff (6 попыток)
    - Распознавание rate limit ошибок
    - Сигналы progress/item_processed для отправки сообщений и шагов прогресса в UI
    - Корректную остановку операций
    """
    
    progress = Signal(str)
    item_processed = Signal()
    
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
        self.saved_edits = 0
        self._last_rate_notice_ts = 0.0
        
        # Инициализация rate limiting
        self._init_save_ratelimit()

    def _t(self, key: str) -> str:
        return translate_runtime(key, '')

    def _fmt(self, key: str, **kwargs) -> str:
        text = self._t(key)
        try:
            return text.format(**kwargs)
        except Exception:
            return text
    
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

    @staticmethod
    def _extract_wait_seconds(text: str) -> float:
        """Пытается вытащить рекомендуемую паузу из текста ошибки."""
        raw = str(text or "")
        patterns = (
            r"retry[\s_-]*after[^0-9]*([0-9]+(?:\.[0-9]+)?)",
            r"sleep(?:ing)?\s+for[^0-9]*([0-9]+(?:\.[0-9]+)?)",
            r"wait(?:ing)?(?:\s+for)?[^0-9]*([0-9]+(?:\.[0-9]+)?)",
            r"maxlag[^0-9]*([0-9]+(?:\.[0-9]+)?)",
        )
        for pattern in patterns:
            m = re.search(pattern, raw, re.IGNORECASE)
            if not m:
                continue
            try:
                return float(m.group(1))
            except Exception:
                continue
        return 0.0

    def _emit_rate_notice(self, wait_s: float, attempt: int, retries: int):
        """Логирует rate-limit уведомление не чаще раза в ~0.8 сек."""
        now = time.time()
        if (now - float(getattr(self, "_last_rate_notice_ts", 0.0) or 0.0)) < 0.8:
            return
        self._last_rate_notice_ts = now
        try:
            self.progress.emit(
                self._fmt('log.base.rate_limit_pause', wait=wait_s, attempt=attempt, retries=retries)
            )
        except Exception:
            pass

    def _adapt_interval_on_slow_save(self, elapsed_s: float):
        """Адаптивно увеличивает интервал, если сам save заметно тормозит на сервере."""
        elapsed = max(0.0, float(elapsed_s or 0.0))
        if elapsed < 2.4:
            return
        target = min(2.5, max(self._save_min_interval, min(2.2, elapsed * 0.55)))
        if target > (self._save_min_interval + 0.05):
            self._save_min_interval = target
        try:
            self.progress.emit(
                self._fmt('log.base.server_pause', elapsed=elapsed, interval=self._save_min_interval)
            )
        except Exception:
            pass
    
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
                started_at = time.time()
                page.text = text
                page.save(summary=summary, minor=minor)
                elapsed = max(0.0, time.time() - started_at)
                self.saved_edits = int(getattr(self, 'saved_edits', 0) or 0) + 1
                self._adapt_interval_on_slow_save(elapsed)
                self._decay_save_interval()
                return True
            except Exception as e:
                if self._is_rate_error(e) and attempt < retries:
                    self._increase_save_interval(attempt)
                    hinted_wait = self._extract_wait_seconds(str(e))
                    wait_s = max(self._save_min_interval, hinted_wait, 0.35 * attempt)
                    self._emit_rate_notice(wait_s, attempt, retries)
                    try:
                        time.sleep(wait_s)
                    except Exception:
                        pass
                    continue
                try:
                    self.progress.emit(self._fmt('log.base.save_error', error_type=type(e).__name__, error=e))
                except Exception:
                    pass
                return False
        return False
