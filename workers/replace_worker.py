"""
Replace worker for updating existing pages in Wikimedia projects.
"""

import csv
import pywikibot

from .base_worker import BaseWorker
from ..core.namespace_manager import normalize_title_by_selection


def _escape_wikitext_for_summary(text: str) -> str:
    """
    Экранирует вики-разметку для отображения в описании правки.

    Заменяет [[ и ]] на их Unicode-эквиваленты, чтобы MediaWiki
    не интерпретировала ссылки и категории в описании правки.

    Args:
        text: Исходный текст с вики-разметкой

    Returns:
        Текст с экранированной разметкой
    """
    # Заменяем квадратные скобки на похожие Unicode-символы
    # чтобы MediaWiki не парсила их как ссылки/категории
    text = text.replace('[[', '⟦').replace(']]', '⟧')
    return text


def _format_summary(template: str, content: str) -> str:
    """
    Форматирует комментарий к правке, заменяя переменные на реальные значения.

    Поддерживаемые переменные:
    - $1, $2, $3... - строки содержимого (1-я, 2-я, 3-я и т.д.)

    Args:
        template: Шаблон комментария с переменными
        content: Содержимое страницы

    Returns:
        Отформатированный комментарий
    """
    if not template:
        return template

    lines = content.split('\n')
    result = template

    # Заменяем $1, $2, $3... на соответствующие строки (с экранированием)
    for i, line in enumerate(lines, start=1):
        escaped_line = _escape_wikitext_for_summary(line)
        result = result.replace(f'${i}', escaped_line)

    # Удаляем неиспользованные теги $N (если строк меньше чем тегов)
    import re
    result = re.sub(r'\s*\$\d+', '', result)

    return result


class ReplaceWorker(BaseWorker):
    """
    Worker для перезаписи существующих страниц в проектах Wikimedia.

    Читает TSV файл с парами (название, новое_содержимое) и обновляет страницы.
    Использует базовый класс для rate limiting и retry логики.
    """

    def __init__(self, tsv_path, username, password, lang, family, ns_selection: str, summary, minor: bool):
        """
        Инициализация ReplaceWorker.

        Args:
            tsv_path: Путь к TSV файлу с данными для замены
            username: Имя пользователя для авторизации
            password: Пароль для авторизации
            lang: Код языка
            family: Семейство проекта
            ns_selection: Выбор пространства имен
            summary: Комментарий к правкам
            minor: Флаг малой правки
        """
        super().__init__(username, password, lang, family)
        self.tsv_path = tsv_path
        self.ns_sel = ns_selection
        self.summary = summary
        self.minor = minor
        self.stats = {
            'total': 0,
            'updated': 0,
            'missing': 0,
            'failed': 0,
            'invalid': 0,
        }

    def run(self):
        """Основной метод выполнения замены страниц."""
        site = pywikibot.Site(self.lang, self.family)
        from ..utils import debug
        debug(f'Login attempt replace lang={self.lang}')

        if self.username and self.password:
            try:
                site.login(user=self.username)
            except Exception as e:
                self.progress.emit(
                    self._fmt('log.worker.auth_error', error_type=type(e).__name__, error=e)
                )
                return

        try:
            # Читаем как utf-8-sig, чтобы убрать BOM у первой ячейки
            with open(self.tsv_path, newline='', encoding='utf-8-sig') as f:
                reader = csv.reader(f, delimiter='\t')
                for row in reader:
                    if self._stop:
                        break
                    raw_title = row[0] if row and row[0] is not None else ''
                    title_raw = raw_title.strip().lstrip('\ufeff')
                    has_title = bool(title_raw)

                    if len(row) < 2:
                        if row:
                            self.stats['invalid'] += 1
                            if has_title:
                                self.item_processed.emit()
                        continue

                    # Нормализуем заголовок и строки: убираем пробелы и возможный BOM
                    try:
                        import html as _html
                    except Exception:
                        _html = None
                    title = (_html.unescape(title_raw) if _html else title_raw)
                    if not title:
                        self.stats['invalid'] += 1
                        continue
                    self.stats['total'] += 1
                    lines_raw = [(s or '').lstrip('\ufeff') for s in row[1:]]
                    lines = [(_html.unescape(s) if _html else s)
                             for s in lines_raw]
                    # Нормализуем по выбору пользователя (Авто/Категория/Шаблон/Статья)
                    norm_title = normalize_title_by_selection(
                        title, self.family, self.lang, self.ns_sel)
                    page = pywikibot.Page(site, norm_title)
                    if page.exists():
                        content = "\n".join(lines)
                        # Форматируем комментарий с подстановкой переменных
                        formatted_summary = _format_summary(
                            self.summary, content)
                        ok = self._save_with_retry(
                            page, content, formatted_summary, self.minor)
                        if ok:
                            self.stats['updated'] += 1
                            self.progress.emit(
                                self._fmt('log.replace.written_lines', title=title, lines=len(lines))
                            )
                        else:
                            self.stats['failed'] += 1
                            self.progress.emit(
                                self._fmt('log.replace.failed_save', title=title)
                            )
                    else:
                        self.stats['missing'] += 1
                        self.progress.emit(self._fmt('log.replace.page_missing', title=title))
                    self.item_processed.emit()
        except Exception as e:
            self.stats['failed'] += 1
            self.progress.emit(self._fmt('log.replace.tsv_error', error=e))
        finally:
            # Финальные сообщения об окончании теперь пишет UI
            pass
