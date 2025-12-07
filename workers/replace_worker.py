"""
Replace worker for updating existing pages in Wikimedia projects.
"""

import csv
import pywikibot

from .base_worker import BaseWorker
from ..core.namespace_manager import normalize_title_by_selection


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

    # Заменяем $1, $2, $3... на соответствующие строки
    for i, line in enumerate(lines, start=1):
        result = result.replace(f'${i}', line)

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
                    f"Ошибка авторизации: {type(e).__name__}: {e}")
                return

        try:
            # Читаем как utf-8-sig, чтобы убрать BOM у первой ячейки
            with open(self.tsv_path, newline='', encoding='utf-8-sig') as f:
                reader = csv.reader(f, delimiter='\t')
                for row in reader:
                    if self._stop:
                        break
                    if len(row) < 2:
                        continue

                    raw_title = row[0] if row[0] is not None else ''
                    # Нормализуем заголовок и строки: убираем пробелы и возможный BOM
                    try:
                        import html as _html
                    except Exception:
                        _html = None
                    title_raw = raw_title.strip().lstrip('\ufeff')
                    title = (_html.unescape(title_raw) if _html else title_raw)
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
                            self.progress.emit(
                                f"{title}: записано {len(lines)} строк")
                        else:
                            self.progress.emit(
                                f"{title}: не удалось сохранить после повторных попыток")
                    else:
                        self.progress.emit(f"{title}: страница отсутствует")
        except Exception as e:
            self.progress.emit(f"Ошибка работы с файлом TSV: {e}")
        finally:
            # Финальные сообщения об окончании теперь пишет UI
            pass
