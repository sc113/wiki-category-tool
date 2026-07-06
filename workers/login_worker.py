"""
Login worker for authenticating with Wikimedia projects.
"""

import threading

from PySide6.QtCore import QThread, Signal

from ..core.pywikibot_config import write_pwb_credentials, apply_pwb_config, _delete_all_cookies, reset_pywikibot_session
from ..core.localization import translate_runtime, translate_key


class LoginWorker(QThread):
    """
    Worker для авторизации в проектах Wikimedia.
    
    Выполняет полный цикл авторизации:
    - Запись учетных данных в конфигурацию
    - Очистка старых cookies
    - Сброс сессии pywikibot
    - Авторизация и проверка
    """
    
    success = Signal(str, str, str, str)  # username, password, lang, family
    failure = Signal(str)
    interactive_input_requested = Signal(str, bool)  # question, password

    def __init__(self, username: str, password: str, lang: str, family: str):
        """
        Инициализация LoginWorker.
        
        Args:
            username: Имя пользователя
            password: Пароль
            lang: Код языка
            family: Семейство проекта
        """
        super().__init__()
        self.username = username
        self.password = password
        self.lang = lang
        self.family = family
        self._stop = False
        self._input_lock = threading.Lock()
        self._input_event = None
        self._input_answer = None
        self._input_cancelled = False

    def _t(self, key: str) -> str:
        return translate_runtime(key, '')

    def _fmt(self, key: str, **kwargs) -> str:
        text = self._t(key)
        try:
            return text.format(**kwargs)
        except Exception:
            return text

    def _log(self, key: str, **kwargs) -> None:
        from ..utils import debug
        debug(self._fmt(key, **kwargs))
    
    def request_stop(self):
        """Запрос на корректную остановку операции."""
        self._stop = True
        self.provide_interactive_input(None)
        try:
            self.quit()
        except Exception:
            pass

    def provide_interactive_input(self, answer):
        """Передать ответ из GUI в поток авторизации."""
        event = None
        with self._input_lock:
            self._input_answer = answer
            self._input_cancelled = answer is None
            event = self._input_event
        if event is not None:
            event.set()

    def _request_interactive_input(self, question: str, password: bool = False) -> str:
        """Запросить у GUI дополнительный код/ответ, который требует pywikibot."""
        event = threading.Event()
        with self._input_lock:
            self._input_event = event
            self._input_answer = None
            self._input_cancelled = False

        self._log('log.login.interactive_input_requested', question=question)
        self.interactive_input_requested.emit(question, password)

        while not event.wait(0.1):
            if self._stop:
                self.provide_interactive_input(None)
                break

        with self._input_lock:
            answer = self._input_answer
            cancelled = self._input_cancelled or self._stop
            if self._input_event is event:
                self._input_event = None

        if cancelled:
            self._stop = True
            raise RuntimeError(self._t('log.login.interactive_input_cancelled'))

        return '' if answer is None else str(answer)

    def _patch_pywikibot_input(self, pywikibot):
        """Временно направить интерактивные запросы pywikibot в GUI."""
        original_input = getattr(pywikibot, 'input', None)
        pwb_bot = None
        original_bot_input = None
        try:
            import pywikibot.bot as pwb_bot_module
            pwb_bot = pwb_bot_module
            original_bot_input = getattr(pwb_bot, 'input', None)
        except Exception:
            pwb_bot = None

        def gui_input(question: str, password: bool = False, default='', force: bool = False) -> str:
            if force and default is not None:
                return str(default)
            answer = self._request_interactive_input(str(question or ''), bool(password))
            if answer == '' and default is not None:
                return str(default)
            return answer

        pywikibot.input = gui_input
        if pwb_bot is not None:
            pwb_bot.input = gui_input
        return original_input, pwb_bot, original_bot_input

    def _restore_pywikibot_input(self, pywikibot, original_input, pwb_bot, original_bot_input) -> None:
        """Вернуть pywikibot.input после попытки логина."""
        if original_input is not None:
            pywikibot.input = original_input
        if pwb_bot is not None and original_bot_input is not None:
            pwb_bot.input = original_bot_input

    def run(self):
        """Основной метод выполнения авторизации."""
        try:
            if self._stop:
                return
            self._log('log.login.start', user=self.username, lang=self.lang, family=self.family)
            
            if self._stop:
                return
            # Записываем учетные данные в конфигурацию pywikibot
            self._log('log.login.write_credentials')
            write_pwb_credentials(self.lang, self.username, self.password, self.family)
            
            # Применяем конфигурацию и получаем путь к директории
            self._log('log.login.apply_config')
            cfg_dir = apply_pwb_config(self.lang, self.family)
            
            # Удаляем старые cookies
            self._log('log.login.delete_cookies')
            _delete_all_cookies(cfg_dir)
            
            # Сбрасываем сессию pywikibot
            self._log('log.login.reset_session')
            reset_pywikibot_session(self.lang)
            
            # Импортируем pywikibot только после применения конфигурации
            import pywikibot
            from pywikibot import config as pwb_config

            # Ограничиваем таймауты сетевых запросов pywikibot
            self._log('log.login.set_timeouts')
            try:
                pwb_config.socket_timeout = 20
            except Exception:
                pass
            
            # Создаем объект сайта и выполняем авторизацию
            self._log('log.login.create_site', lang=self.lang, family=self.family)
            site = pywikibot.Site(self.lang, self.family)
            
            self._log('log.login.logout_cleanup')
            try:
                site.logout()
            except Exception:
                pass

            self._log('log.login.login_user', user=self.username)
            input_patch = self._patch_pywikibot_input(pywikibot)
            try:
                site.login(user=self.username)
            finally:
                self._restore_pywikibot_input(pywikibot, *input_patch)

            # Проверяем подключение к сайту
            self._log('log.login.check_site')
            try:
                site_name = site.siteinfo.get('name')
                self._log('log.login.site_ok', site=site_name)
            except Exception as e:
                self._log('log.login.site_warning', error=e)
                pass
            
            # Получаем имя авторизованного пользователя для проверки
            self._log('log.login.fetch_user')
            usr = None
            try:
                usr = site.user()
                self._log('log.login.user_ok', user=usr)
            except Exception as e:
                self._log('log.login.user_error', error=e)
                usr = None
            
            if not usr:
                raise Exception(self._t('log.login.user_missing'))
            
            # Авторизация успешна
            self._log('log.login.success', user=usr)
            self.success.emit(self.username, self.password, self.lang, self.family)
            
        except Exception as e:
            # Авторизация не удалась
            self._log('log.login.error', error_type=type(e).__name__, error=e)
            # Дружественное сообщение при требовании смены пароля/доп.запросах аутентификации
            try:
                emsg = str(e)
            except Exception:
                emsg = ''
            cancelled_msg = self._t('log.login.interactive_input_cancelled')
            marker_ru = translate_key('log.login.password_change_marker', 'ru', '')
            marker_en = translate_key('log.login.password_change_marker', 'en', '')
            if cancelled_msg and emsg == cancelled_msg:
                self.failure.emit(cancelled_msg)
            elif (
                'PasswordAuthenticationRequest' in emsg or
                (marker_ru and marker_ru in emsg) or
                (marker_en and marker_en in emsg) or
                'retype' in emsg
            ):
                friendly = self._t('log.login.password_change_required')
                self.failure.emit(friendly)
            else:
                self.failure.emit(f"{type(e).__name__}: {e}")
        finally:
            # Гарантируем корректное завершение потока
            self._log('log.login.finish')
