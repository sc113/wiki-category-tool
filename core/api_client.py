"""
API client module for Wikimedia projects with rate limiting and retry logic.
"""
import requests
import time
import json
from threading import Lock
from typing import Optional, List

from ..constants import REQUEST_HEADERS, MIN_REQUEST_INTERVAL, MAX_RATE_INTERVAL


class WikimediaAPIClient:
    """HTTP client with rate limiting for Wikimedia API operations."""
    
    def __init__(self):
        self.session = requests.Session()
        self._rate_lock = Lock()
        self._last_req_ts = 0.0
        self._min_interval = MIN_REQUEST_INTERVAL
        
    def _rate_wait(self):
        """Wait to respect rate limiting between requests."""
        with self._rate_lock:
            now = time.time()
            wait = max(0.0, (self._last_req_ts + self._min_interval) - now)
            if wait > 0:
                time.sleep(wait)
            self._last_req_ts = time.time()
    
    def _rate_backoff(self, seconds: Optional[float] = None):
        """Increase rate limiting interval after errors."""
        with self._rate_lock:
            add = float(seconds) if seconds is not None else 0.0
            self._min_interval = min(MAX_RATE_INTERVAL, max(self._min_interval * 1.5, add if add > 0 else self._min_interval))
            from ..utils import debug
            debug(f"Rate backoff: MIN_INTERVAL={self._min_interval:.2f}s")
    
    def fetch_content(self, title: str, ns_selection: str | int, lang: str = 'ru', 
                     family: str = 'wikipedia', retries: int = 5, timeout: int = 6) -> List[str]:
        """
        Fetch page content from Wikimedia API with retry logic.
        
        Args:
            title: Page title to fetch
            ns_selection: Namespace selection ('auto' or namespace ID)
            lang: Language code
            family: Project family (wikipedia, commons, etc.)
            retries: Number of retry attempts
            timeout: Request timeout in seconds
            
        Returns:
            List of content lines, empty list if page not found or error
        """
        from ..utils import debug
        from .namespace_manager import NamespaceManager
        
        debug(f"API GET content lang={lang} title={title}")
        url = self._build_api_url(family, lang)
        
        # Normalize title by selected namespace
        full = (title or '').lstrip('\ufeff')
        try:
            if isinstance(ns_selection, str) and ns_selection == 'auto':
                pass
            else:
                ns_id = int(ns_selection)
                if ns_id != 0:
                    from ..constants import DEFAULT_EN_NS
                    default_en = DEFAULT_EN_NS.get(ns_id, '')
                    # Use namespace manager to ensure title has proper prefix
                    ns_manager = NamespaceManager(self)
                    full = ns_manager._ensure_title_with_ns(full, family, lang, ns_id, default_en)
        except Exception:
            pass
            
        params = {
            "action": "query", 
            "prop": "revisions", 
            "rvprop": "content", 
            "titles": full, 
            "format": "json"
        }
        
        for attempt in range(1, retries + 1):
            try:
                self._rate_wait()
                r = self.session.get(url, params=params, timeout=timeout, headers=REQUEST_HEADERS)
                r.encoding = 'utf-8'
                
                if r.status_code == 429:
                    debug(f"API ERR 429 (rate limit) for {title}; attempt {attempt}/{retries}")
                    if attempt < retries:
                        self._rate_backoff(0.6 * attempt)
                        continue
                    return []
                    
                if r.status_code != 200:
                    debug(f"API ERR {r.status_code} for {title}")
                    return []
                    
                pages = r.json().get("query", {}).get("pages", {})
                for p in pages.values():
                    if "missing" in p:
                        return []
                    text = p.get("revisions", [{}])[0].get("*", "")
                    return text.split("\n") if text else []
                    
            except requests.exceptions.Timeout:
                if attempt < retries:
                    time.sleep(2 ** attempt)
                    
        return []

    def fetch_contents_batch(self, titles: List[str], ns_selection: str | int, lang: str = 'ru',
                              family: str = 'wikipedia', retries: int = 3, timeout: int = 6) -> dict[str, List[str]]:
        """
        Fetch multiple page contents in one API call (up to 50 titles).

        Args:
            titles: List of page titles to fetch (already decoded/unescaped)
            ns_selection: Namespace selection ('auto' or namespace ID)
            lang: Language code
            family: Project family
            retries: Number of retry attempts
            timeout: Request timeout in seconds

        Returns:
            Mapping from returned canonical title to list of lines. Titles not returned
            by the API will be missing in the mapping.
        """
        from ..utils import debug
        from .namespace_manager import NamespaceManager

        if not titles:
            return {}

        url = self._build_api_url(family, lang)

        # Normalize titles by selection, like in fetch_content
        normalized: List[str] = []
        try:
            if isinstance(ns_selection, str) and ns_selection == 'auto':
                normalized = [(t or '').lstrip('\ufeff') for t in titles]
            else:
                ns_id = int(ns_selection)
                from ..constants import DEFAULT_EN_NS
                default_en = DEFAULT_EN_NS.get(ns_id, '')
                ns_manager = NamespaceManager(self)
                for t in titles:
                    full = (t or '').lstrip('\ufeff')
                    if ns_id != 0:
                        full = ns_manager._ensure_title_with_ns(full, family, lang, ns_id, default_en)
                    normalized.append(full)
        except Exception:
            normalized = [(t or '').lstrip('\ufeff') for t in titles]

        # Join titles with '|'; caller must ensure <= 50 per call
        params = {
            "action": "query",
            "prop": "revisions",
            "rvprop": "content",
            "titles": "|".join(normalized),
            "format": "json"
        }

        for attempt in range(1, retries + 1):
            try:
                debug(f"API POST batch content lang={lang} count={len(normalized)}")
                self._rate_wait()
                r = self.session.post(url, data=params, timeout=timeout, headers=REQUEST_HEADERS)
                r.encoding = 'utf-8'

                if r.status_code == 429:
                    debug(f"API ERR 429 (rate limit) for batch; attempt {attempt}/{retries}")
                    if attempt < retries:
                        self._rate_backoff(0.6 * attempt)
                        continue
                    return {}

                if r.status_code in (413, 414) and len(normalized) > 1:
                    # Payload too large / URI too long even with POST — split batch
                    mid = len(normalized) // 2
                    left = self.fetch_contents_batch(normalized[:mid], ns_selection, lang, family, retries, timeout)
                    right = self.fetch_contents_batch(normalized[mid:], ns_selection, lang, family, retries, timeout)
                    merged: dict[str, List[str]] = {}
                    merged.update(left or {})
                    merged.update(right or {})
                    return merged

                if r.status_code != 200:
                    debug(f"API ERR {r.status_code} for batch")
                    return {}

                data = r.json()
                pages = data.get("query", {}).get("pages", {})
                result: dict[str, List[str]] = {}
                for p in pages.values():
                    if "missing" in p:
                        # Missing titles are NOT added to result - they should be detected as not found
                        continue
                    title = p.get("title") or ""
                    text = p.get("revisions", [{}])[0].get("*", "")
                    result[title] = (text.split("\n") if text else [])
                return result

            except requests.exceptions.Timeout:
                if attempt < retries:
                    time.sleep(2 ** attempt)
            except Exception as e:
                debug(f"Batch fetch error: {e}")
                return {}

        return {}
    
    def get_namespace_info(self, family: str, lang: str) -> dict:
        """
        Fetch namespace information from Wikimedia API.
        
        Args:
            family: Project family
            lang: Language code
            
        Returns:
            Dictionary with namespace information
        """
        from ..utils import debug
        
        url = self._build_api_url(family, lang)
        params = {
            'action': 'query', 
            'meta': 'siteinfo', 
            'siprop': 'namespaces|namespacealiases', 
            'format': 'json'
        }
        
        try:
            debug(f"NS API fetch: {family}/{lang}")
            self._rate_wait()
            r = self.session.get(url, params=params, timeout=10, headers=REQUEST_HEADERS)
            data = r.json() if r.status_code == 200 else {}
            return data.get('query', {})
        except Exception as e:
            debug(f"NS API fetch error: {e}")
            return {}
    
    def get_awb_lists(self, family: str, lang: str, timeout: int = 15) -> tuple[str, dict | None]:
        """
        Fetch AWB user lists from CheckPageJSON.
        
        Args:
            family: Project family
            lang: Language code
            timeout: Request timeout
            
        Returns:
            Tuple of (state, data) where state is 'ok'|'missing'|'error'
        """
        from ..utils import debug
        
        url = self._build_project_awb_url(family, lang)
        params = {'action': 'raw'}
        
        try:
            self._rate_wait()
            r = self.session.get(url, params=params, timeout=timeout, headers=REQUEST_HEADERS)
            
            if r.status_code == 404:
                debug(f"AWB CheckPage missing lang={lang}")
                return 'missing', None
                
            if r.status_code != 200:
                debug(f"AWB CheckPage fetch HTTP {r.status_code} lang={lang}")
                return 'error', None
                
            txt = (r.text or '').strip()
            try:
                data = json.loads(txt)
            except Exception as e:
                debug(f"AWB CheckPage JSON parse error: {e}")
                return 'error', None
                
            if not isinstance(data, dict):
                debug("AWB CheckPage content is not a JSON object")
                return 'error', None
                
            users = data.get('enabledusers') or []
            bots = data.get('enabledbots') or []
            
            if not isinstance(users, list) or not isinstance(bots, list):
                debug("AWB CheckPage lists have unexpected types")
                return 'error', None
                
            debug(f"AWB lists loaded: users={len(users)} bots={len(bots)} lang={lang}")
            return 'ok', {'enabledusers': users, 'enabledbots': bots}
            
        except Exception as e:
            debug(f"AWB CheckPage fetch error: {e}")
            return 'error', None
    
    def check_updates(self) -> dict:
        """
        Check for application updates from GitHub API.
        
        Returns:
            Dictionary with update information
        """
        from ..utils import debug
        from ..constants import GITHUB_API_RELEASES
        
        try:
            debug('Проверка обновлений...')
            self._rate_wait()
            r = self.session.get(GITHUB_API_RELEASES, headers=REQUEST_HEADERS, timeout=10)
            
            if r.status_code != 200:
                debug(f'GitHub API status {r.status_code}')
                return {}
                
            return r.json()
        except Exception as e:
            debug(f'Ошибка проверки обновлений: {e}')
            return {}
    
    @staticmethod
    def _build_host(family: str, lang: str) -> str:
        """Build hostname for Wikimedia project."""
        fam = (family or 'wikipedia').strip()
        lng = (lang or 'ru').strip()
        
        if fam == 'commons':
            return 'commons.wikimedia.org'
        if fam == 'wikidata':
            return 'www.wikidata.org'
        if fam == 'meta':
            return 'meta.wikimedia.org'
        if fam == 'species':
            return 'species.wikimedia.org'
        if fam == 'incubator':
            return 'incubator.wikimedia.org'
        if fam == 'mediawiki':
            return 'www.mediawiki.org'
        if fam == 'wikifunctions':
            return 'www.wikifunctions.org'
            
        return f"{lng}.{fam}.org"
    
    @classmethod
    def _build_api_url(cls, family: str, lang: str) -> str:
        """Build API URL for Wikimedia project."""
        return f"https://{cls._build_host(family, lang)}/w/api.php"
    
    @classmethod
    def _build_project_awb_url(cls, family: str, lang: str) -> str:
        """Build AWB CheckPage URL for project."""
        return f"https://{cls._build_host(family, lang)}/wiki/Project:AutoWikiBrowser/CheckPageJSON"


# Global instance for backward compatibility
_api_client = WikimediaAPIClient()

# Expose global functions for backward compatibility
REQUEST_SESSION = _api_client.session
_rate_wait = _api_client._rate_wait
_rate_backoff = _api_client._rate_backoff
fetch_content = _api_client.fetch_content