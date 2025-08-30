"""
Pywikibot configuration management module.
"""
import os
import sys
import re
from typing import Optional

# ВАЖНО: не импортировать pywikibot на уровне модуля, чтобы избежать
# ошибки конфигурации до вызова ensure_base_env(). Импорт выполняется
# лениво внутри функций, после подготовки окружения.


class PywikibotConfigManager:
    """Manages Pywikibot configuration, credentials, and cookies."""
    
    def _dist_configs_dir(self) -> str:
        """Get actual configs folder next to exe/script (for writing files)."""
        from ..utils import tool_base_dir
        base = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else tool_base_dir()
        return os.path.join(base, 'configs')
    
    def config_base_dir(self) -> str:
        """Get base directory for configuration."""
        cfg = os.environ.get('PYWIKIBOT_DIR')
        if cfg and os.path.isabs(cfg):
            return cfg
        return self._dist_configs_dir()

    def ensure_base_env(self) -> str:
        """Ensure base environment and configs folder for pywikibot.

        - Creates configs directory next to the package/exe
        - Sets PYWIKIBOT_DIR
        - Sets/clears PYWIKIBOT_NO_USER_CONFIG depending on user-config.py presence

        Returns:
            Path to configs directory
        """
        cfg_dir = self._dist_configs_dir()
        try:
            os.makedirs(cfg_dir, exist_ok=True)
        except Exception:
            pass

        uc_path = os.path.join(cfg_dir, 'user-config.py')
        try:
            if os.path.isfile(uc_path):
                os.environ.pop('PYWIKIBOT_NO_USER_CONFIG', None)
            else:
                os.environ['PYWIKIBOT_NO_USER_CONFIG'] = '1'
        except Exception:
            # Do not break app startup because of env quirks
            pass

        os.environ['PYWIKIBOT_DIR'] = cfg_dir
        return cfg_dir
    
    def write_pwb_credentials(self, lang: str, username: str, password: str, family: str = 'wikipedia') -> None:
        """
        Write Pywikibot credentials to config files.
        
        Args:
            lang: Language code
            username: Username
            password: Password
            family: Project family
        """
        from ..utils import debug
        
        cfg_dir = self._dist_configs_dir()
        os.makedirs(cfg_dir, exist_ok=True)
        uc_path = os.path.join(cfg_dir, 'user-config.py')
        
        usernames_map: dict[str, str] = {}
        if os.path.isfile(uc_path):
            try:
                with open(uc_path, 'r', encoding='utf-8') as f:
                    txt = f.read()
                fam_re = re.escape(family)
                for m in re.finditer(rf"usernames\['{fam_re}'\]\['([^']+)'\]\s*=\s*'([^']+)'", txt):
                    usernames_map[m.group(1)] = m.group(2)
            except Exception:
                usernames_map = {}
        
        usernames_map[lang] = username
        
        lines = [
            f"family = '{family}'",
            f"mylang = '{lang}'",
            "password_file = 'user-password.py'",
        ]
        for code in sorted(usernames_map.keys()):
            lines.append(f"usernames['{family}']['{code}'] = '{usernames_map[code]}'")
            
        debug(f"Write user-config.py → {uc_path}")
        with open(uc_path, 'w', encoding='utf-8') as f:
            f.write("\n".join(lines) + "\n")
        
        up_path = os.path.join(cfg_dir, 'user-password.py')
        debug(f"Write user-password.py → {up_path}")
        with open(up_path, 'w', encoding='utf-8') as f:
            f.write(repr((username, password)))
    
    def apply_pwb_config(self, lang: str, family: str = 'wikipedia') -> str:
        """
        Apply Pywikibot configuration.
        
        Args:
            lang: Language code
            family: Project family
            
        Returns:
            Configuration directory path
        """
        cfg_dir = self._dist_configs_dir()
        os.makedirs(cfg_dir, exist_ok=True)
        
        try:
            throttle_path = os.path.join(cfg_dir, 'throttle.ctrl')
            if not os.path.isfile(throttle_path):
                with open(throttle_path, 'w', encoding='utf-8') as _f:
                    _f.write('')
        except Exception:
            pass
        
        os.environ['PYWIKIBOT_DIR'] = cfg_dir
        # Ленивая инициализация pywikibot.config только после установки окружения
        from pywikibot import config as pwb_config  # type: ignore
        pwb_config.base_dir = cfg_dir
        pwb_config.family = family
        pwb_config.mylang = lang
        pwb_config.password_file = os.path.join(cfg_dir, 'user-password.py')
        
        # Speed up pywikibot write operations: remove internal delays between edits.
        # Write buttons in UI are only available with AWB access/bypass, so this is safe.
        try:
            pwb_config.put_throttle = 0.0
            pwb_config.maxlag = 5
        except Exception:
            pass
            
        return cfg_dir
    
    def cookies_exist(self, cfg_dir: str, username: str) -> bool:
        """
        Check if cookies exist for user.
        
        Args:
            cfg_dir: Configuration directory
            username: Username to check
            
        Returns:
            True if cookies exist
        """
        for cookie_name in (f'pywikibot-{username}.lwp', 'pywikibot.lwp'):
            if os.path.isfile(os.path.join(cfg_dir, cookie_name)):
                return True
        return False
    
    def _normalize_username(self, name: Optional[str]) -> str:
        """Normalize username for comparison (делегирование в utils.normalize_username)."""
        try:
            from ..utils import normalize_username as _norm
            return _norm(name)
        except Exception:
            if not name:
                return ''
            return name.strip().replace('_', ' ').casefold()
    
    def _delete_all_cookies(self, cfg_dir: str, username: Optional[str] = None) -> None:
        """
        Delete all cookie files.
        
        Args:
            cfg_dir: Configuration directory
            username: Specific username (unused, kept for compatibility)
        """
        from ..utils import debug
        
        try:
            for name in os.listdir(cfg_dir):
                if name == 'pywikibot.lwp' or (name.startswith('pywikibot-') and name.endswith('.lwp')):
                    try:
                        os.remove(os.path.join(cfg_dir, name))
                        debug(f"Удалён cookie: {name}")
                    except Exception as e:
                        debug(f"Не удалось удалить cookie {name}: {e}")
        except Exception:
            pass
    
    def reset_pywikibot_session(self, lang: Optional[str] = None) -> None:
        """
        Reset Pywikibot session.
        
        Args:
            lang: Language code (if None, resets all)
        """
        try:
            from pywikibot.comms import http as pywb_http  # type: ignore
            pywb_http.session_reset()
        except Exception:
            pass
            
        try:
            import pywikibot  # type: ignore
            from pywikibot import config as pwb_config  # type: ignore
            sites = getattr(pywikibot, '_sites', None)
            if isinstance(sites, dict):
                if lang is None:
                    sites.clear()
                else:
                    fam = getattr(pwb_config, 'family', 'wikipedia')
                    for k in list(sites.keys()):
                        try:
                            if isinstance(k, tuple):
                                f, l = k
                                if f == fam and l == lang:
                                    sites.pop(k, None)
                        except Exception:
                            pass
        except Exception:
            pass


# Global instance for backward compatibility
_config_manager = PywikibotConfigManager()

# Expose global functions for backward compatibility
def _dist_configs_dir() -> str:
    return _config_manager._dist_configs_dir()

def config_base_dir() -> str:
    return _config_manager.config_base_dir()

def write_pwb_credentials(lang: str, username: str, password: str, family: str = 'wikipedia') -> None:
    return _config_manager.write_pwb_credentials(lang, username, password, family)

def apply_pwb_config(lang: str, family: str = 'wikipedia') -> str:
    return _config_manager.apply_pwb_config(lang, family)

def cookies_exist(cfg_dir: str, username: str) -> bool:
    return _config_manager.cookies_exist(cfg_dir, username)

def _normalize_username(name: Optional[str]) -> str:
    return _config_manager._normalize_username(name)

def _delete_all_cookies(cfg_dir: str, username: Optional[str] = None) -> None:
    return _config_manager._delete_all_cookies(cfg_dir, username)

def reset_pywikibot_session(lang: Optional[str] = None) -> None:
    return _config_manager.reset_pywikibot_session(lang)

def ensure_base_env() -> str:
    """Public helper to ensure base pywikibot environment is configured."""
    return _config_manager.ensure_base_env()