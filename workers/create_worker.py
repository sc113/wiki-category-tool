"""
Create worker for creating new pages in Wikimedia projects.
"""

import csv
import re
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

    def _replace_placeholder(match: re.Match) -> str:
        leading = match.group('leading') or ''
        index = int(match.group('index')) - 1
        if 0 <= index < len(lines):
            return leading + lines[index]
        return ''

    # Заменяем за один проход, чтобы $1 не повреждал $10, $11, ...
    return re.sub(
        r'(?P<leading>\s*)\$(?P<index>\d+)',
        _replace_placeholder,
        template,
    )


class CreateWorker(BaseWorker):
    """
    Worker для создания новых страниц в проектах Wikimedia.

    Читает TSV файл с парами (название, содержимое) и создает новые страницы.
    Использует базовый класс для rate limiting и retry логики.
    """

    def __init__(self, tsv_path, username, password, lang, family, ns_selection: str, summary, minor: bool):
        """
        Инициализация CreateWorker.

        Args:
            tsv_path: Путь к TSV файлу с данными для создания
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
        # Малая правка НЕ применяется при создании страниц
        self.minor = False
        self.stats = {
            'total': 0,
            'created': 0,
            'exists': 0,
            'failed': 0,
            'invalid': 0,
        }

    def run(self):
        """Основной метод выполнения создания страниц."""
        from ..utils import debug
        debug(f'Login attempt create lang={self.lang}')

        try:
            site = pywikibot.Site(self.lang, self.family)
        except Exception as e:
            self._set_failure(e)
            self.stats['failed'] += 1
            self.progress.emit(self._fmt('log.create.error', error=e))
            return

        if self.username and self.password:
            try:
                site.login(user=self.username)
            except Exception as e:
                self._set_failure(e)
                self.stats['failed'] += 1
                self.progress.emit(
                    self._fmt('log.worker.auth_error', error_type=type(e).__name__, error=e)
                )
                return

        try:
            with open(self.tsv_path, newline='', encoding='utf-8-sig') as f:
                reader = csv.reader(f, delimiter='\t')
                for row in reader:
                    if self._stop:
                        break
                    if not row:
                        continue
                    raw_title = row[0] if row[0] is not None else ''
                    title = raw_title.strip().lstrip('\ufeff')
                    if not title:
                        self.stats['invalid'] += 1
                        continue
                    self.stats['total'] += 1
                    lines = [((s or '').lstrip('\ufeff')) for s in row[1:]]

                    norm_title = normalize_title_by_selection(
                        title, self.family, self.lang, self.ns_sel)
                    page = pywikibot.Page(site, norm_title)
                    if not page.exists():
                        content = "\n".join(lines)
                        # Форматируем комментарий с подстановкой переменных
                        formatted_summary = _format_summary(
                            self.summary, content)
                        ok = self._save_with_retry(
                            page, content, formatted_summary, self.minor)
                        if ok:
                            self.stats['created'] += 1
                            self.progress.emit(
                                self._fmt('log.create.created', title=title, lines=len(lines))
                            )
                        else:
                            if self._stop:
                                break
                            self.stats['failed'] += 1
                            self.progress.emit(
                                self._fmt('log.create.failed_create', title=title)
                            )
                    else:
                        self.stats['exists'] += 1
                        self.progress.emit(self._fmt('log.create.exists', title=title))
                    self.item_processed.emit()
        except Exception as e:
            self._set_failure(e)
            self.stats['failed'] += 1
            self.progress.emit(self._fmt('log.create.error', error=e))
        finally:
            # Финальные сообщения об окончании теперь пишет UI
            pass
