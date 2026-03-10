"""
Login worker for authenticating with Wikimedia projects.
"""

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
        try:
            self.quit()
        except Exception:
            pass

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
            site.login(user=self.username)
            
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
            marker_ru = translate_key('log.login.password_change_marker', 'ru', '')
            marker_en = translate_key('log.login.password_change_marker', 'en', '')
            if (
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
