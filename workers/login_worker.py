"""
Login worker for authenticating with Wikimedia projects.
"""

from PySide6.QtCore import QThread, Signal

from ..core.pywikibot_config import write_pwb_credentials, apply_pwb_config, _delete_all_cookies, reset_pywikibot_session


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
    
    def request_stop(self):
        """Запрос на корректную остановку операции."""
        self._stop = True
        try:
            self.quit()
        except Exception:
            pass

    def run(self):
        """Основной метод выполнения авторизации."""
        from ..utils import debug
        
        try:
            if self._stop:
                return
            debug(f'LoginWorker: начинаем авторизацию {self.username}@{self.lang}.{self.family}')
            
            if self._stop:
                return
            # Записываем учетные данные в конфигурацию pywikibot
            debug('LoginWorker: записываем учетные данные в конфигурацию')
            write_pwb_credentials(self.lang, self.username, self.password, self.family)
            
            # Применяем конфигурацию и получаем путь к директории
            debug('LoginWorker: применяем конфигурацию pywikibot')
            cfg_dir = apply_pwb_config(self.lang, self.family)
            
            # Удаляем старые cookies
            debug('LoginWorker: удаляем старые cookies')
            _delete_all_cookies(cfg_dir)
            
            # Сбрасываем сессию pywikibot
            debug('LoginWorker: сбрасываем сессию pywikibot')
            reset_pywikibot_session(self.lang)
            
            # Импортируем pywikibot только после применения конфигурации
            import pywikibot
            from pywikibot import config as pwb_config

            # Ограничиваем таймауты сетевых запросов pywikibot
            debug('LoginWorker: настраиваем таймауты')
            try:
                pwb_config.socket_timeout = 20
            except Exception:
                pass
            
            # Создаем объект сайта и выполняем авторизацию
            debug(f'LoginWorker: создаем объект сайта для {self.lang}.{self.family}')
            site = pywikibot.Site(self.lang, self.family)
            
            debug('LoginWorker: выполняем logout (очистка)')
            try:
                site.logout()
            except Exception:
                pass
            
            debug(f'LoginWorker: выполняем login для пользователя {self.username}')
            site.login(user=self.username)
            
            # Проверяем подключение к сайту
            debug('LoginWorker: проверяем подключение к сайту')
            try:
                site_name = site.siteinfo.get('name')
                debug(f'LoginWorker: подключение к сайту успешно, название: {site_name}')
            except Exception as e:
                debug(f'LoginWorker: предупреждение при проверке сайта: {e}')
                pass
            
            # Получаем имя авторизованного пользователя для проверки
            debug('LoginWorker: получаем имя авторизованного пользователя')
            usr = None
            try:
                usr = site.user()
                debug(f'LoginWorker: авторизованный пользователь: {usr}')
            except Exception as e:
                debug(f'LoginWorker: ошибка получения пользователя: {e}')
                usr = None
            
            if not usr:
                raise Exception('Сервер не вернул имя авторизованного пользователя')
            
            # Авторизация успешна
            debug(f'LoginWorker: авторизация успешна для {usr}')
            self.success.emit(self.username, self.password, self.lang, self.family)
            
        except Exception as e:
            # Авторизация не удалась
            debug(f'LoginWorker: ошибка авторизации: {type(e).__name__}: {e}')
            # Дружественное сообщение при требовании смены пароля/доп.запросах аутентификации
            try:
                emsg = str(e)
            except Exception:
                emsg = ''
            if (
                'PasswordAuthenticationRequest' in emsg or
                'Новый пароль' in emsg or
                'retype' in emsg
            ):
                friendly = (
                    'Требуется смена пароля аккаунта. Зайдите на сайт через браузер и смените пароль, '
                    'либо (рекомендовано) создайте BotPassword на странице Special:BotPasswords и авторизуйтесь '
                    'как "Имя@Метка" с паролем бота.'
                )
                self.failure.emit(friendly)
            else:
                self.failure.emit(f"{type(e).__name__}: {e}")
        finally:
            # Гарантируем корректное завершение потока
            debug('LoginWorker: завершение работы')