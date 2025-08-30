"""
Parse worker for reading pages from Wikimedia projects.
"""

import csv

from .base_worker import BaseWorker
from ..core.api_client import WikimediaAPIClient
from ..utils import write_row


class ParseWorker(BaseWorker):
    """
    Worker для чтения страниц из проектов Wikimedia.
    
    Читает список страниц и сохраняет их содержимое в TSV файл.
    Использует многопоточность для ускорения процесса.
    """
    
    def __init__(self, titles, out_path, ns_sel, lang, family):
        """
        Инициализация ParseWorker.
        
        Args:
            titles: Список заголовков страниц для чтения
            out_path: Путь к выходному TSV файлу
            ns_sel: Выбор пространства имен ('auto' или ID)
            lang: Код языка
            family: Семейство проекта
        """
        # ParseWorker не требует авторизации, поэтому передаем пустые значения
        super().__init__('', '', lang, family)
        self.titles = titles
        self.out_path = out_path
        self.ns_sel = ns_sel
        self.api_client = WikimediaAPIClient()
        self.output_file = None
        self.writer = None

    def run(self):
        """Основной метод выполнения чтения страниц."""
        # Открываем файл для живой записи результатов
        try:
            self.output_file = open(self.out_path, 'w', newline='', encoding='utf-8-sig')
            self.writer = csv.writer(self.output_file, delimiter='\t')
        except Exception as e:
            self.progress.emit(f"Ошибка создания файла: {e}")
            return
        
        processed_count = 0

        # Батчим запросы по 50 заголовков (ограничение MediaWiki API)
        batch_size = 50
        titles = list(self.titles)
        i = 0
        while i < len(titles) and not self._stop:
            batch = titles[i:i + batch_size]
            i += batch_size
            try:
                # Разрешаем HTML сущности в заголовках перед запросом
                try:
                    import html as _html
                except Exception:
                    _html = None
                decoded = [(_html.unescape(t) if _html else t) for t in batch]
                mapping = self.api_client.fetch_contents_batch(decoded, self.ns_sel, lang=self.lang, family=self.family)
            except Exception:
                mapping = {}

            # Сопоставляем результаты, чтобы сохранить в исходном порядке по заголовкам батча
            title_to_lines = mapping or {}
            for original in decoded:
                if self._stop:
                    break
                # API возвращает каноничное имя; попробуем найти по точному совпадению, иначе пропустим
                lines = None
                if original in title_to_lines:
                    lines = title_to_lines.get(original)
                else:
                    # Попробуем найти регистронезависимо среди ключей
                    try:
                        key = next((k for k in title_to_lines.keys() if k.casefold() == original.casefold()), None)
                        if key is not None:
                            lines = title_to_lines.get(key)
                    except Exception:
                        pass

                if lines is None:
                    # Ничего не вернулось для этого заголовка (missing/ошибка)
                    self.progress.emit(f"{original}: не найдено")
                    self._write_result_immediately((original, []))
                    processed_count += 1
                else:
                    self.progress.emit(f"{original}: {len(lines)} строк(и)")
                    self._write_result_immediately((original, lines))
                    processed_count += 1
        
        # Закрываем файл
        try:
            if self.output_file:
                self.output_file.close()
            if processed_count > 0:
                self.progress.emit(f"Сохранено {processed_count} страниц в {self.out_path}")
            else:
                self.progress.emit("Нет данных для сохранения")
        except Exception as e:
            self.progress.emit(f"Ошибка закрытия файла: {e}")
    
    def _write_result_immediately(self, result):
        """Немедленная запись результата в файл"""
        try:
            title, lines = result
            if lines:
                self.writer.writerow([title, *lines])
                self.output_file.flush()  # Принудительная запись на диск
        except Exception as e:
            self.progress.emit(f"Ошибка записи результата: {e}")
    
    def request_stop(self):
        """Переопределяем метод остановки для корректного закрытия файла"""
        super().request_stop()
        # Файл будет закрыт в методе run() при завершении

    def process_single(self, title):
        """
        Обработка одной страницы.
        
        Args:
            title: Заголовок страницы
            
        Returns:
            tuple: (title, lines) или None если ошибка
        """
        if self._stop:
            return None
            
        try:
            # Разрешаем HTML-сущности в заголовке (например, &quot; → ") перед запросом
            try:
                import html as _html
            except Exception:
                _html = None
            title_decoded = (_html.unescape(title) if _html else title)
            lines = self.api_client.fetch_content(title_decoded, self.ns_sel, lang=self.lang, family=self.family)
            if self._stop:
                return None
                
            if lines:
                self.progress.emit(f"{title_decoded}: {len(lines)} строк(и)")
                return (title_decoded, lines)
            else:
                self.progress.emit(f"{title_decoded}: не найдено")
                return (title_decoded, [])
        except Exception as e:
            self.progress.emit(f"{title}: ошибка - {e}")
            return None