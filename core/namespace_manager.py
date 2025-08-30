"""
Namespace management module for handling Wikimedia project namespaces.
"""
import os
import json
import re
from typing import Dict, Set, Tuple, List, Optional, Any

from ..constants import DEFAULT_EN_NS, EN_PREFIX_ALIASES


class NamespaceManager:
    """Manages namespace information and caching for Wikimedia projects."""
    
    def __init__(self, api_client):
        self.api_client = api_client
        self.ns_cache: Dict[Tuple[str, str], Dict[int, Dict[str, Set[str] | str]]] = {}
        self.default_ns_prefixes: Dict[Tuple[str, str], Dict[int, Dict[str, Set[str] | str]]] = {}
    
    def _ns_cache_dir(self) -> str:
        """Get directory for namespace cache files."""
        from .pywikibot_config import PywikibotConfigManager
        config_manager = PywikibotConfigManager()
        base = config_manager._dist_configs_dir()
        path = os.path.join(base, 'apicache')
        try:
            os.makedirs(path, exist_ok=True)
        except Exception:
            pass
        return path
    
    def _ns_cache_file(self, family: str, lang: str) -> str:
        """Get cache file path for specific family/language."""
        safe_f = re.sub(r"[^a-z0-9_-]+", "_", (family or '').lower())
        safe_l = re.sub(r"[^a-z0-9_-]+", "_", (lang or '').lower())
        return os.path.join(self._ns_cache_dir(), f"ns_{safe_f}_{safe_l}.json")
    
    def _get_cached_ns_info(self, family: str, lang: str) -> Optional[Dict[int, Dict[str, Set[str] | str]]]:
        """
        Get namespace information from cache only (no HTTP requests).
        
        Args:
            family: Project family
            lang: Language code
            
        Returns:
            Dictionary mapping namespace IDs to their info, or None if not cached
        """
        key = (family, lang)
        if key in self.ns_cache:
            return self.ns_cache[key]
            
        # Load from disk cache only
        cache_path = self._ns_cache_file(family, lang)
        try:
            if os.path.isfile(cache_path):
                with open(cache_path, 'r', encoding='utf-8') as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    restored: Dict[int, Dict[str, Set[str] | str]] = {}
                    for sid, meta in raw.items():
                        try:
                            ns_id = int(sid)
                        except Exception:
                            continue
                        if not isinstance(meta, dict):
                            continue
                        prim = str(meta.get('primary') or '')
                        all_list = meta.get('all') or []
                        if isinstance(all_list, list):
                            restored[ns_id] = {'primary': prim, 'all': {str(x).lower() for x in all_list}}
                    if restored:
                        self.ns_cache[key] = restored
                        return restored
        except Exception:
            pass
            
        return None
    
    def _get_cached_primary_ns_prefix(self, family: str, lang: str, ns_id: int) -> Optional[str]:
        """
        Get primary namespace prefix from cache only (no HTTP requests).
        
        Args:
            family: Project family
            lang: Language code
            ns_id: Namespace ID
            
        Returns:
            Primary prefix or None if not cached
        """
        info = self._get_cached_ns_info(family, lang)
        if info and ns_id in info:
            return info[ns_id].get('primary', '')
        return None
    
    def _load_ns_info(self, family: str, lang: str) -> Dict[int, Dict[str, Set[str] | str]]:
        """
        Load namespace information from cache or API.
        
        Args:
            family: Project family
            lang: Language code
            
        Returns:
            Dictionary mapping namespace IDs to their info
        """
        from ..utils import debug
        from ..constants import REQUEST_HEADERS
        import requests
        
        key = (family, lang)
        if key in self.ns_cache:
            return self.ns_cache[key]
            
        # Load from disk cache
        cache_path = self._ns_cache_file(family, lang)
        try:
            if os.path.isfile(cache_path):
                with open(cache_path, 'r', encoding='utf-8') as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    restored: Dict[int, Dict[str, Set[str] | str]] = {}
                    for sid, meta in raw.items():
                        try:
                            ns_id = int(sid)
                        except Exception:
                            continue
                        if not isinstance(meta, dict):
                            continue
                        prim = str(meta.get('primary') or '')
                        all_list = meta.get('all') or []
                        if isinstance(all_list, list):
                            restored[ns_id] = {'primary': prim, 'all': {str(x).lower() for x in all_list}}
                    if restored:
                        self.ns_cache[key] = restored
                        return restored
        except Exception as e:
            debug(f"NS disk cache read error: {e}")
            
        # Fetch from API and save to cache - using direct requests like original
        prefixes_by_id: Dict[int, Dict[str, Set[str] | str]] = {}
        try:
            # Build URL like original code
            url = f"https://{lang}.{family}.org/w/api.php"
            params = {
                'action': 'query', 
                'meta': 'siteinfo', 
                'siprop': 'namespaces|namespacealiases', 
                'format': 'json'
            }
            
            # Use direct requests like original
            response = requests.get(url, params=params, timeout=10, headers=REQUEST_HEADERS)
            
            if response.status_code == 200:
                data = response.json()
                
                query_data = data.get('query', {})
                ns_map = query_data.get('namespaces', {}) or {}
                aliases = query_data.get('namespacealiases', []) or []
                
                debug(f"Загружены namespace префиксы для {lang}.{family}: {len(ns_map)} пространств, {len(aliases)} алиасов")
                
                for sid, meta in ns_map.items():
                    try:
                        ns_id = int(sid)
                    except Exception:
                        continue
                    # Skip talk namespaces (odd ns) and negative ns
                    if ns_id % 2 == 1 or ns_id < 0:
                        continue
                        
                    names: Set[str] = set()
                    local_name = (meta.get('*') or '').strip()
                    canon = (meta.get('canonical') or '').strip()
                    
                    if local_name:
                        names.add(local_name + ':')
                    if canon:
                        names.add(canon + ':')
                        
                    prefixes_by_id[ns_id] = {
                        'primary': (local_name or canon) + ':' if (local_name or canon) else '',
                        'all': {p.lower() for p in names if p}
                    }
                    
                for a in aliases:
                    try:
                        ns_id = int(a.get('id'))
                        if ns_id % 2 == 1 or ns_id < 0:
                            continue
                        name = (a.get('*') or '').strip()
                        if not name:
                            continue
                        d = prefixes_by_id.setdefault(ns_id, {'primary': '', 'all': set()})
                        d['all'] = set(d.get('all') or set())
                        d['all'].add((name + ':').lower())
                    except Exception:
                        continue
                        
                debug(f"Обработано {len(prefixes_by_id)} namespace префиксов для {lang}.{family}")
            else:
                # Не логируем ошибки для несуществующих языков - это нормально при печатании
                pass
                    
        except Exception as e:
            # Не логируем ошибки сети для несуществующих языков - это нормально при печатании
            prefixes_by_id = {}
            
        if prefixes_by_id:
            self.ns_cache[key] = prefixes_by_id
            # Save disk cache
            try:
                to_dump = {
                    str(k): {
                        'primary': str(v.get('primary') or ''), 
                        'all': sorted([s for s in (v.get('all') or set())])
                    }
                    for k, v in prefixes_by_id.items()
                }
                with open(cache_path, 'w', encoding='utf-8') as f:
                    json.dump(to_dump, f, ensure_ascii=False)
                debug(f"NS cached: {family}/{lang} → {len(prefixes_by_id)} namespaces → {cache_path}")
            except Exception as e:
                debug(f"NS cache save error: {e}")
            return prefixes_by_id
            
        # Fallback to preset prefixes
        preset = self.default_ns_prefixes.get((family or '', lang or ''))
        if isinstance(preset, dict) and preset:
            self.ns_cache[key] = preset
            debug(f"NS fallback preset used: {family}/{lang} → {len(preset)} namespaces")
            return preset
            
        debug(f"NS load failed: {family}/{lang} - no data available")
        return {}
    
    def get_primary_ns_prefix(self, family: str, lang: str, ns_id: int, default_en: str) -> str:
        """Get primary namespace prefix for given namespace ID."""
        info = self._load_ns_info(family, lang)
        prim = (info.get(ns_id, {}).get('primary') or '') if isinstance(info.get(ns_id, {}), dict) else ''
        prim = str(prim)
        return prim if prim else (default_en if default_en.endswith(':') else default_en + ':')
    
    def title_has_ns_prefix(self, family: str, lang: str, title: str, ns_ids: Set[int]) -> bool:
        """Check if title has namespace prefix from given namespace IDs."""
        info = self._load_ns_info(family, lang)
        prefixes: Set[str] = set()
        for i in ns_ids:
            d = info.get(i) or {}
            allp = d.get('all') or set()
            if isinstance(allp, set):
                prefixes |= allp
        t = (title or '').lstrip('\ufeff')
        lower = t.casefold()
        return any(lower.startswith(p) for p in prefixes)
    
    def _has_en_prefix(self, title: str, ns_id: int) -> bool:
        """Check if title has English namespace prefix."""
        lower = (title or '').lstrip('\ufeff').casefold()
        
        base = (DEFAULT_EN_NS.get(ns_id) or '').strip()
        candidates: Set[str] = set()
        if base:
            candidates.add(base.casefold() if base.endswith(':') else (base + ':').casefold())
        candidates |= set(EN_PREFIX_ALIASES.get(ns_id, set()))
        return any(lower.startswith(p) for p in candidates) if candidates else False
    
    def has_prefix_by_policy(self, family: str, lang: str, title: str, ns_ids: Set[int]) -> bool:
        """Check if title has prefix according to policy (local or English)."""
        # First check local prefixes
        if self.title_has_ns_prefix(family, lang, title, ns_ids):
            return True
        # Then English prefixes
        return any(self._has_en_prefix(title, ns) for ns in ns_ids)
    
    def get_policy_prefix(self, family: str, lang: str, ns_id: int, default_en: str) -> str:
        """Get namespace prefix according to policy."""
        # If local primary is known - use it
        local = self.get_primary_ns_prefix(family, lang, ns_id, '')
        if local:
            return local if local.endswith(':') else local + ':'
        # Fallback to English
        return default_en if default_en.endswith(':') else default_en + ':'
    
    def _ensure_title_with_ns(self, title: str, family: str, lang: str, ns_id: int, default_en: str) -> str:
        """Ensure title has proper namespace prefix."""
        t = (title or '').lstrip('\ufeff').strip()
        if not t:
            return t
        if self.has_prefix_by_policy(family, lang, t, {ns_id}):
            return t
        prefix = self.get_policy_prefix(family, lang, ns_id, default_en)
        return f"{prefix}{t}"
    
    def normalize_title_by_selection(self, title: str, family: str, lang: str, selection: str | int) -> str:
        """
        Normalize title by selected namespace.
        
        When NS is specified, adds local primary prefix from API/JSON.
        English prefixes in source titles are recognized and considered valid.
        """
        base_title = (title or '').lstrip('\ufeff')
        try:
            if isinstance(selection, str):
                sel = selection.strip().lower()
                if sel in {'', 'auto'}:
                    return base_title
                alias_to_ns = {'cat': 14, 'category': 14, 'tpl': 10, 'template': 10, 'art': 0, 'article': 0}
                ns_id = alias_to_ns.get(sel, int(sel))
            else:
                ns_id = int(selection)
        except Exception:
            return base_title
            
        if ns_id == 0:
            return base_title
            
        # Add local primary prefix (fallback: English from DEFAULT_EN_NS)
        default_en = DEFAULT_EN_NS.get(ns_id, '')
        return self._ensure_title_with_ns(base_title, family, lang, ns_id, default_en)
    
    def _common_ns_ids(self) -> List[int]:
        """Get list of common namespace IDs across languages."""
        # Count how many languages each NS-ID appears in
        counts: Dict[int, int] = {}
        for (fam, lng), mapping in self.default_ns_prefixes.items():
            if fam != 'wikipedia':
                continue
            for ns_id in mapping.keys():
                counts[ns_id] = counts.get(ns_id, 0) + 1
        common = sorted([ns for ns, cnt in counts.items() if cnt >= 3])
        
        # Если нет общих namespace'ов, используем английские как fallback
        if not common:
            common = sorted([ns_id for ns_id in DEFAULT_EN_NS.keys() if ns_id > 0])
            
        return common
    
    def _primary_label_for_ns(self, family: str, lang: str, ns_id: int) -> str:
        """Get primary label for namespace (for UI display)."""
        # Try to get local primary prefix, otherwise English
        try:
            # Сначала пробуем получить локальный префикс без fallback
            local_prim = self._get_cached_primary_ns_prefix(family, lang, ns_id)
            if local_prim:
                return local_prim[:-1] if local_prim.endswith(':') else local_prim
            
            # Если локального нет, используем английский
            base = DEFAULT_EN_NS.get(ns_id, '')
            return base[:-1] if base.endswith(':') else base
        except Exception:
            base = DEFAULT_EN_NS.get(ns_id, '')
            return base[:-1] if base.endswith(':') else base
    
    def populate_ns_combo(self, combo, family: str, lang: str, force_load: bool = False) -> None:
        """Populate namespace combo box with available namespaces.

        When force_load=False, берём данные только из памяти/дискового кэша,
        без сетевых запросов. При отсутствии кэша — показываем общий набор
        namespace'ов с английскими подписями. """
        from ..utils import debug
        
        # debug(f"Заполнение namespace комбобокса для {family}:{lang}")  # Убираем спам
        
        try:
            combo.clear()
        except Exception:
            pass
            
        combo.addItem('Авто', 'auto')
        combo.addItem('(нет) [0]', 0)
        
        # Предпочитаем кэш, чтобы не дергать сеть лишний раз
        info = None
        if not force_load:
            info = self._get_cached_ns_info(family or 'wikipedia', lang or 'ru')
            if info:
                debug(f"NS из кэша: {family}/{lang} → {len(info)}")
        if info is None and force_load:
            info = self._load_ns_info(family or 'wikipedia', lang or 'ru')
        
        if info:
            ns_ids = sorted(info.keys())
            # debug(f"Получено {len(info)} пространств имён: {ns_ids}")  # Убираем спам
        else:
            ns_ids = self._common_ns_ids()
            # debug(f"Используются общие пространства имён: {ns_ids}")  # Убираем спам
            
        added_count = 0
        for ns_id in ns_ids:
            if ns_id == 0:
                continue
            label = f"{self._primary_label_for_ns(family, lang, ns_id)} [{ns_id}]"
            combo.addItem(label, ns_id)
            added_count += 1
            
        # debug(f"Добавлено {added_count} элементов в комбобокс")  # Убираем спам
        self._adjust_combo_popup_width(combo)
    
    @staticmethod
    def _adjust_combo_popup_width(combo) -> None:
        """Adjust combo box popup width to fit content."""
        try:
            view = combo.view()
            fm = view.fontMetrics() if hasattr(view, 'fontMetrics') else combo.fontMetrics()
            max_w = 0
            for i in range(combo.count()):
                text = combo.itemText(i) or ''
                w = fm.horizontalAdvance(text)
                if w > max_w:
                    max_w = w
                    
            max_w += 48
            try:
                view.setMinimumWidth(max_w)
            except Exception:
                pass
        except Exception:
            pass


# Global instance for backward compatibility
_namespace_manager = None

def get_namespace_manager():
    """Get global namespace manager instance."""
    global _namespace_manager
    if _namespace_manager is None:
        from .api_client import _api_client
        _namespace_manager = NamespaceManager(_api_client)
    return _namespace_manager

# Expose global variables and functions for backward compatibility
NS_CACHE = {}
DEFAULT_NS_PREFIXES = {}

def _ns_cache_dir() -> str:
    return get_namespace_manager()._ns_cache_dir()

def _ns_cache_file(family: str, lang: str) -> str:
    return get_namespace_manager()._ns_cache_file(family, lang)

def _load_ns_info(family: str, lang: str):
    return get_namespace_manager()._load_ns_info(family, lang)

def get_primary_ns_prefix(family: str, lang: str, ns_id: int, default_en: str) -> str:
    return get_namespace_manager().get_primary_ns_prefix(family, lang, ns_id, default_en)

def title_has_ns_prefix(family: str, lang: str, title: str, ns_ids: set[int]) -> bool:
    return get_namespace_manager().title_has_ns_prefix(family, lang, title, ns_ids)

def has_prefix_by_policy(family: str, lang: str, title: str, ns_ids: set[int]) -> bool:
    return get_namespace_manager().has_prefix_by_policy(family, lang, title, ns_ids)

def get_policy_prefix(family: str, lang: str, ns_id: int, default_en: str) -> str:
    return get_namespace_manager().get_policy_prefix(family, lang, ns_id, default_en)

def normalize_title_by_selection(title: str, family: str, lang: str, selection: str | int) -> str:
    return get_namespace_manager().normalize_title_by_selection(title, family, lang, selection)

def _common_ns_ids() -> list[int]:
    return get_namespace_manager()._common_ns_ids()

def _primary_label_for_ns(family: str, lang: str, ns_id: int) -> str:
    return get_namespace_manager()._primary_label_for_ns(family, lang, ns_id)

def _populate_ns_combo(combo, family: str, lang: str) -> None:
    return get_namespace_manager().populate_ns_combo(combo, family, lang)

def _adjust_combo_popup_width(combo) -> None:
    return NamespaceManager._adjust_combo_popup_width(combo)

def _ensure_title_with_ns(title: str, family: str, lang: str, ns_id: int, default_en: str) -> str:
    return get_namespace_manager()._ensure_title_with_ns(title, family, lang, ns_id, default_en)