"""
Template management module for handling template rules and caching.
"""
import os
import json
import re
import mwparserfromhell
from typing import Dict, List, Tuple, Any, Optional
from threading import Event

from ..constants import TEMPLATE_RULES_FILE


class TemplateManager:
    """Manages template rules, caching, and template parameter processing."""
    
    def __init__(self, config_manager=None):
        """
        Initialize template manager.
        
        Args:
            config_manager: Configuration manager instance
        """
        self.config_manager = config_manager
        # Кэш правил в памяти. Ключ — нормализованный ключ шаблона
        # вида "family::lang::name_cf" (см. _norm_tmpl_key).
        self.template_auto_cache: Dict[str, Any] = {}
        self.auto_skip_templates: set = set()
        self.auto_confirm_direct_all = False
        self.auto_skip_direct_all = False
        self._rules_file_path: Optional[str] = None
        self._rules_mtime: Optional[float] = None
        self._prompt_events: Dict[int, Event] = {}
        # Отображаемые (читаемые) названия шаблонов для сохранения в файл
        self._display_names: Dict[str, str] = {}
        
        # Initialize rules file
        self._init_rules_file()
    
    def _init_rules_file(self):
        """Initialize template rules file and load existing rules."""
        try:
            if self.config_manager:
                base_cfg = self.config_manager._dist_configs_dir()
            else:
                from .pywikibot_config import _dist_configs_dir
                base_cfg = _dist_configs_dir()
                
            self._rules_file_path = os.path.join(base_cfg, TEMPLATE_RULES_FILE)
            
            # Create directory if it doesn't exist
            os.makedirs(os.path.dirname(self._rules_file_path), exist_ok=True)
            
            # Load existing rules from file if available
            self._load_rules()
            # Store current mtime
            try:
                if os.path.exists(self._rules_file_path):
                    self._rules_mtime = os.path.getmtime(self._rules_file_path)
            except Exception:
                self._rules_mtime = None
            
            # Restore auto-skip flags from saved rules
            try:
                for _k, _bucket in (self.template_auto_cache or {}).items():
                    if not isinstance(_bucket, dict):
                        continue
                    auto_val = str(_bucket.get('auto', '')).strip().casefold()
                    if auto_val == 'skip':
                        self.auto_skip_templates.add(_k)
            except Exception:
                pass
                
        except Exception:
            self._rules_file_path = None
    
    # ===== Helper/utility API (shared by GUI/Workers) =====
    def get_rules_file_path(self) -> Optional[str]:
        """Return current rules file path if initialized."""
        return self._rules_file_path

    @staticmethod
    def resolve_rules_file_path(config_manager=None) -> str:
        """Compute rules file path similarly to internal initialization.

        Safe to call without constructing a long-lived manager.
        """
        try:
            if config_manager:
                base_cfg = config_manager._dist_configs_dir()
            else:
                from .pywikibot_config import _dist_configs_dir
                base_cfg = _dist_configs_dir()
            from ..constants import TEMPLATE_RULES_FILE as _TFILE
            path = os.path.join(base_cfg, _TFILE)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            return path
        except Exception:
            # Fallback: place alongside module
            try:
                base = os.path.dirname(__file__)
            except Exception:
                base = os.getcwd()
            return os.path.join(base, 'configs', 'template_rules.json')
    
    def _project_key(self, family: str, lang: str) -> str:
        """Возвращает ключ проекта в формате 'lang:family' (например 'ru:wikipedia')."""
        return f"{(lang or '').strip().lower()}:{(family or '').strip().lower()}"

    def _load_rules(self):
        """Load template rules from JSON.

        Preferred format uses a single string field 'auto':
        - 'approve' → approve=True,  skip=False
        - 'skip'    → approve=False, skip=True
        - 'none' or missing/unknown → approve=False, skip=False
        Boolean fields 'approve' and 'skip' are also accepted for compatibility.
        'auto' has priority if present.
        """
        try:
            if self._rules_file_path and os.path.exists(self._rules_file_path):
                with open(self._rules_file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    # Формат V2: ключи верхнего уровня — 'lang:family'
                    # Значения — dict с шаблонами напрямую или в поле 'templates'.
                    # Совместимый формат: data['rename_worker'] содержит словарь
                    # '{fam::lang::name_cf}' → bucket.
                    if 'rename_worker' in data and isinstance(data.get('rename_worker'), dict):
                        scoped = data.get('rename_worker') or {}
                        for key, bucket in scoped.items():
                            try:
                                if not isinstance(bucket, dict):
                                    continue
                                # Нормализуем bucket
                                rules_list_raw = list(bucket.get('rules') or [])
                                rules_list: List[Dict[str, Any]] = []
                                for r in rules_list_raw:
                                    if isinstance(r, dict):
                                        rr = dict(r)
                                        if 'auto' not in rr or not isinstance(rr.get('auto'), str):
                                            rr['auto'] = 'none'
                                        rules_list.append(rr)
                                auto_val = bucket.get('auto')
                                if isinstance(auto_val, str):
                                    auto_cf = auto_val.strip().casefold()
                                    auto_norm = auto_cf if auto_cf in ('approve', 'skip') else 'none'
                                else:
                                    approve_flag = bool(bucket.get('approve', False))
                                    skip_flag = bool(bucket.get('skip', False))
                                    auto_norm = 'approve' if approve_flag else ('skip' if skip_flag else 'none')
                                # Храним как есть по старому ключу
                                self.template_auto_cache[key] = {'rules': rules_list, 'auto': auto_norm}
                                if auto_norm == 'skip':
                                    try:
                                        self.auto_skip_templates.add(key)
                                    except Exception:
                                        pass
                            except Exception:
                                continue
                    else:
                        # Новый формат: {'ru:wikipedia': {'templates': { 'Публицист': {...}}}}
                        for proj_key, tmpl_map in data.items():
                            try:
                                if not isinstance(tmpl_map, dict):
                                    continue
                                # Разрешаем как с 'templates', так и без него
                                inner = tmpl_map.get('templates') if isinstance(tmpl_map.get('templates'), dict) else tmpl_map
                                if not isinstance(inner, dict):
                                    continue
                                # Разобрать проект
                                lang = ''
                                family = ''
                                try:
                                    parts = str(proj_key or '').split(':', 1)
                                    if len(parts) == 2:
                                        lang, family = parts[0].strip(), parts[1].strip()
                                except Exception:
                                    pass
                                if not lang or not family:
                                    continue
                                for display_name, bucket in inner.items():
                                    if not isinstance(bucket, dict):
                                        continue
                                    # Нормализация
                                    rules_list_raw = list(bucket.get('rules') or [])
                                    rules_list: List[Dict[str, Any]] = []
                                    for r in rules_list_raw:
                                        if isinstance(r, dict):
                                            rr = dict(r)
                                            if 'auto' not in rr or not isinstance(rr.get('auto'), str):
                                                rr['auto'] = 'none'
                                            rules_list.append(rr)
                                    auto_val = bucket.get('auto')
                                    auto_cf = str(auto_val or 'none').strip().casefold()
                                    auto_norm = auto_cf if auto_cf in ('approve', 'skip') else 'none'
                                    # Ключ в памяти
                                    tmpl_key = self._norm_tmpl_key(display_name, family, lang)
                                    self.template_auto_cache[tmpl_key] = {
                                        'rules': rules_list,
                                        'auto': auto_norm
                                    }
                                    self._display_names[tmpl_key] = str(display_name)
                                    if auto_norm == 'skip':
                                        self.auto_skip_templates.add(tmpl_key)
                            except Exception:
                                continue
                try:
                    self._rules_mtime = os.path.getmtime(self._rules_file_path)
                except Exception:
                    pass
        except Exception:
            pass

    def _maybe_reload_rules(self):
        """Reload rules from disk if the underlying file has changed externally."""
        try:
            if not self._rules_file_path:
                return
            if not os.path.exists(self._rules_file_path):
                return
            current = os.path.getmtime(self._rules_file_path)
            if self._rules_mtime is None or current > (self._rules_mtime or 0.0):
                # Clear and reload from disk
                self.template_auto_cache.clear()
                self.auto_skip_templates.clear()
                self._load_rules()
        except Exception:
            pass
    
    def _save_rules_file(self):
        """Save template rules to JSON file using new 'auto' field.

        Persist only minimal supported fields: rules + auto (approve|skip|none).
        """
        try:
            if not self._rules_file_path:
                return
            # Группируем по проекту 'lang:family' и сохраняем читаемые имена шаблонов
            by_project: Dict[str, Dict[str, Any]] = {}
            for key, bucket in self.template_auto_cache.items():
                try:
                    # Разобрать fam/lang из ключа вида fam::lang::name
                    fam = ''
                    lng = ''
                    try:
                        parts = str(key).split('::', 2)
                        if len(parts) >= 2:
                            fam, lng = parts[0].strip(), parts[1].strip()
                    except Exception:
                        pass
                    proj_key = self._project_key(fam, lng)
                    # Правила и auto
                    rules_src = list((bucket.get('rules') or []))
                    rules_norm: List[Dict[str, Any]] = []
                    for r in rules_src:
                        if isinstance(r, dict):
                            rr = dict(r)
                            if 'auto' not in rr or not isinstance(rr.get('auto'), str):
                                rr['auto'] = 'none'
                            rules_norm.append(rr)
                    auto_val = str(bucket.get('auto') or 'none').strip().casefold()
                    if auto_val not in ('approve', 'skip'):
                        auto_val = 'none'
                    # Отображаемое имя шаблона
                    disp = self._display_names.get(key)
                    if not disp:
                        # Fallback: взять последнюю часть ключа (не идеально, но лучше, чем пусто)
                        try:
                            disp = str(key).split('::', 2)[-1]
                        except Exception:
                            disp = str(key)
                    # Размещаем
                    dst = by_project.setdefault(proj_key, {})
                    dst_templates = dst.setdefault('templates', {})
                    dst_templates[disp] = {
                        'rules': rules_norm,
                        'auto': auto_val,
                    }
                except Exception:
                    continue
            file_data = by_project
            with open(self._rules_file_path, 'w', encoding='utf-8') as f:
                json.dump(file_data, f, ensure_ascii=False, indent=2)
            try:
                self._rules_mtime = os.path.getmtime(self._rules_file_path)
            except Exception:
                pass
        except Exception:
            pass
    
    @staticmethod
    def _parse_single_template(template_chunk: str):
        """Parse a chunk containing exactly one top-level template."""
        try:
            code = mwparserfromhell.parse(template_chunk or '')
            templates = list(code.filter_templates(recursive=False))
            if len(templates) != 1:
                return None
            template = templates[0]
            if str(code).strip() != str(template).strip():
                return None
            return template
        except Exception:
            return None

    def _parse_template_tokens(self, template_chunk: str) -> Tuple[str, List[str]]:
        """
        Parse template {{...}} into name and parameter list.
        
        Args:
            template_chunk: Template string like {{template|param1|param2}}
            
        Returns:
            Tuple of (template_name, parameters_list)
        """
        template = self._parse_single_template(template_chunk)
        if template is None:
            return '', []
        return str(template.name), [str(param) for param in template.params]
    
    def _split_params(self, params: List[str]) -> Tuple[List[str], List[Tuple[str, str, str]]]:
        """
        Split parameter list into unnamed and named parameters.
        
        Args:
            params: List of parameter strings
            
        Returns:
            Tuple of (unnamed_tokens, named_triplets) where named_triplets = (left, eq, val)
        """
        unnamed: List[str] = []
        named: List[Tuple[str, str, str]] = []
        
        for raw in params:
            if '=' in raw:
                m = re.match(r"^(?P<left>\s*[^=]+?)(?P<eq>\s*=\s*)(?P<val>.*)$", raw, flags=re.S)
                if m:
                    named.append((m.group('left'), m.group('eq'), m.group('val')))
                else:
                    unnamed.append(raw)
            else:
                unnamed.append(raw)
                
        return unnamed, named
    
    def _diff_template_params(self, before_chunk: str, after_chunk: str) -> Dict[str, Any]:
        """
        Calculate parameter changes between two template versions.
        
        Args:
            before_chunk: Original template string
            after_chunk: Modified template string
            
        Returns:
            Dictionary with changes:
            {
                'name': str,
                'named': { name_cf: (old_val_stripped, new_val_stripped) },
                'unnamed': [ (idx1_based, old_val_stripped, new_val_stripped), ... ]
            }
        """
        before = self._parse_single_template(before_chunk)
        after = self._parse_single_template(after_chunk)
        result = {
            'name': str(after.name if after is not None else before.name if before is not None else ''),
            'named': {},
            'unnamed': [],
        }

        if before is None or after is None:
            return result
        
        # Match named parameters by name (case-insensitive, normalize spaces/underscores)
        def _norm_name(s: str) -> str:
            s2 = (s or '').strip()
            try:
                s2 = re.sub(r"[_\s]+", " ", s2)
            except Exception:
                pass
            return s2.casefold()
        
        map_b: Dict[str, str] = {}
        un_b: List[str] = []
        for param in before.params:
            if param.showkey:
                map_b[_norm_name(str(param.name))] = str(param.value).strip()
            else:
                un_b.append(str(param.value))
            
        map_a: Dict[str, str] = {}
        un_a: List[str] = []
        for param in after.params:
            if param.showkey:
                map_a[_norm_name(str(param.name))] = str(param.value).strip()
            else:
                un_a.append(str(param.value))
            
        for nm, oldv in map_b.items():
            newv = map_a.get(nm)
            if newv is not None and oldv != newv:
                result['named'][nm] = (oldv, newv)
        
        # Unnamed parameters by position
        max_len = max(len(un_b), len(un_a))
        for i in range(max_len):
            oldv = (un_b[i] if i < len(un_b) else '').strip()
            newv = (un_a[i] if i < len(un_a) else '').strip()
            if oldv != newv:
                # 1-based indexing
                result['unnamed'].append((i + 1, oldv, newv))
                
        return result
    
    def _norm_tmpl_key(self, name: str, family: str, lang: str) -> str:
        """
        Normalize template name for use as cache key.
        
        Args:
            name: Template name
            family: Project family
            lang: Language code
            
        Returns:
            Normalized template key
        """
        base = self._strip_tmpl_prefix(name, family, lang)
        try:
            base = re.sub(r"[_\s]+", " ", base).strip()
        except Exception:
            pass
        # ВКЛЮЧАЕМ СКОУП по проекту/языку, чтобы правила не пересекались
        fam = str(family or '').strip().lower()
        lng = str(lang or '').strip().lower()
        # Разделитель двойное двоеточие, чтобы исключить коллизии с названиями шаблонов
        return f"{fam}::{lng}::{base.casefold()}"
    
    def _strip_tmpl_prefix(self, title: str, family: str, lang: str) -> str:
        """
        Strip template namespace prefix from title.
        
        Args:
            title: Template title
            family: Project family  
            lang: Language code
            
        Returns:
            Title without template prefix
        """
        from .namespace_manager import get_namespace_manager
        
        t = (title or '').lstrip('\ufeff').strip()
        if not t:
            return t
            
        ns_manager = get_namespace_manager()
        info = ns_manager._load_ns_info(family, lang)
        prefixes = set(info.get(10, {}).get('all') or set())  # Template namespace = 10
        
        # Add English aliases
        from ..constants import EN_PREFIX_ALIASES
        prefixes |= set(EN_PREFIX_ALIASES.get(10, set()))
        
        lower = t.casefold()
        for p in prefixes:
            if lower.startswith(p):
                return t[len(p):].strip()
                
        return t
    
    def _ensure_cache_bucket(self, tmpl_key: str) -> Dict[str, Any]:
        """
        Ensure cache bucket exists for template key.
        
        Args:
            tmpl_key: Template cache key
            
        Returns:
            Cache bucket dictionary
        """
        b = self.template_auto_cache.get(tmpl_key)
        if not b:
            b = {'named': {}, 'unnamed_single': {}, 'unnamed_sequence': [], 'auto': 'none', 'rules': []}
            self.template_auto_cache[tmpl_key] = b
        else:
            # Гарантируем наличие контейнера для независимых правил
            if 'rules' not in b or not isinstance(b.get('rules'), list):
                b['rules'] = []
            if 'auto' not in b or not isinstance(b.get('auto'), str):
                # Переносим старые флаги, если они остались в памяти
                approve_flag = bool(b.get('approve', False))
                skip_flag = bool(b.get('skip', False))
                b['auto'] = 'approve' if approve_flag else ('skip' if skip_flag else 'none')
                # Не удаляем ключи сразу, чтобы не ломать возможные внешние ссылки
        return b

    def _upsert_rule(self, bucket: Dict[str, Any], rule: Dict[str, Any], auto: str = 'none') -> None:
        """Добавляет или обновляет правило в bucket['rules'], игнорируя поле auto при сравнении.

        Если правило уже есть — обновляет его поле 'auto' (approve/skip/none).
        Если нет — добавляет новое с заданным 'auto'.
        """
        try:
            rules = bucket.get('rules') or []
            # Специальная логика для unnamed_single, чтобы не плодить второе правило с to=""
            try:
                if (rule.get('type') == 'unnamed_single'):
                    new_from = (rule.get('from') or '').strip()
                    new_to = (rule.get('to') or '')
                    # Найти уже существующее правило для того же from
                    for r in rules:
                        if r.get('type') == 'unnamed_single' and (r.get('from') or '').strip() == new_from:
                            # Если новое to пустое, а в существующем сохраняется непустое — обновим только dedupe/auto
                            if (new_to or '') == '' and (r.get('to') or '') != '':
                                if 'dedupe' in rule:
                                    r['dedupe'] = str(rule.get('dedupe'))
                                r['auto'] = str(auto or r.get('auto') or 'none')
                                bucket['rules'] = rules
                                return
                            # Если наоборот — существующее пустое, а новое непустое — улучшим существующее
                            if (r.get('to') or '') == '' and (new_to or '') != '':
                                r['to'] = str(new_to)
                                if 'dedupe' in rule:
                                    r['dedupe'] = str(rule.get('dedupe'))
                                r['auto'] = str(auto or r.get('auto') or 'none')
                                bucket['rules'] = rules
                                return
            except Exception:
                pass
            def _key(r: Dict[str, Any]) -> str:
                # Ключ без поля auto/dedupe — они описывают поведение, но не идентичность замены
                return f"{r.get('type')}|{r.get('param')}|{r.get('from')}|{r.get('to')}|{r.get('sequence')}"
            cand_key = _key(rule)
            for r in rules:
                try:
                    if _key(r) == cand_key:
                        r['auto'] = str(auto or r.get('auto') or 'none')
                        # Обновляем дополнительный параметр дедупликации, если передан
                        if 'dedupe' in rule:
                            try:
                                r['dedupe'] = str(rule.get('dedupe'))
                            except Exception:
                                pass
                        bucket['rules'] = rules
                        return
                except Exception:
                    continue
            new_rule = dict(rule)
            new_rule['auto'] = str(auto or 'none')
            rules.append(new_rule)
            bucket['rules'] = rules
        except Exception:
            pass
    
    def update_template_cache_from_edit(self, family: str, lang: str, before_chunk: str, after_chunk: str, rule_auto: str = 'none', dedupe_mode: Optional[str] = None) -> None:
        """
        Update template cache from edit operation.
        
        Args:
            family: Project family
            lang: Language code
            before_chunk: Original template
            after_chunk: Modified template
        """
        try:
            diff = self._diff_template_params(before_chunk, after_chunk)
            name_for_key = diff.get('name') or ''
            tmpl_key = self._norm_tmpl_key(name_for_key, family, lang)
            if not tmpl_key:
                return
                
            bucket = self._ensure_cache_bucket(tmpl_key)
            # Сохраняем отображаемое имя для человекочитаемого JSON
            try:
                self._display_names[tmpl_key] = self._strip_tmpl_prefix(name_for_key, family, lang)
            except Exception:
                self._display_names[tmpl_key] = name_for_key
            
            # Named parameters → только правило (не дублируем в старую структуру)
            for name_cf, (oldv, newv) in (diff.get('named') or {}).items():
                if not name_cf or oldv is None:
                    continue
                # Сохраняем/обновляем независимое правило
                self._upsert_rule(bucket, {
                    'type': 'named',
                    'param': name_cf,
                    'from': (oldv or '').strip(),
                    'to': (newv or '').strip()
                }, rule_auto)
            
            # Unnamed parameters
            unnamed_changes = diff.get('unnamed') or []
            if len(unnamed_changes) == 1:
                _, oldv, newv = unnamed_changes[0]
                if (oldv or '').strip():
                    # Правило для одиночного безымянного параметра
                    _rule = {
                        'type': 'unnamed_single',
                        'from': (oldv or '').strip(),
                        'to': (newv or '').strip()
                    }
                    # Сохраняем пожелание пользователя для дедупликации, только если явно задано
                    if dedupe_mode is not None and str(dedupe_mode).strip() != '':
                        _rule['dedupe'] = str(dedupe_mode)
                    self._upsert_rule(bucket, _rule, rule_auto)
            elif len(unnamed_changes) > 1:
                # Save as sequence idx=>value
                seq = []
                for idx1, oldv, newv in unnamed_changes:
                    seq.append((int(idx1), (oldv or '').strip(), (newv or '').strip()))
                # Независимое правило для последовательности безымянных параметров
                try:
                    seq_rule = [{'idx': int(i), 'from': (o or '').strip(), 'to': (n or '').strip()} for i, o, n in seq]
                except Exception:
                    seq_rule = []
                self._upsert_rule(bucket, {
                    'type': 'unnamed_sequence',
                    'sequence': seq_rule
                }, rule_auto)
            
            # Save to disk
            try:
                self._save_rules_file()
            except Exception:
                pass
                
        except Exception:
            pass

    # ===== Dedupe helpers used by worker/UI =====
    def normalize_dedupe_mode(self, mode: Optional[str]) -> str:
        """Normalize dedupe mode to one of: 'left' | 'right' | 'keep_both' | '' (unset)."""
        try:
            m = str(mode or '').strip()
        except Exception:
            m = ''
        if not m:
            return ''
        m_cf = m.casefold()
        if m_cf == 'keep_first':
            return 'left'
        if m_cf == 'keep_second':
            return 'right'
        if m_cf in ('left', 'right', 'keep_both'):
            return m_cf
        return ''

    def apply_positional_dedupe(self, template_text: str, target_new_value: str, dedupe_mode: str) -> str:
        """Apply positional deduplication to a template string for given new value.

        Keeps only the first ('left') or last ('right') occurrence of target_new_value
        among positional parameters. When 'keep_both' or unknown/empty mode is passed,
        the template is returned unchanged.
        """
        try:
            mode = self.normalize_dedupe_mode(dedupe_mode)
            if mode not in ('left', 'right'):
                return template_text
            template = self._parse_single_template(template_text)
            if template is None:
                return template_text
            def _norm_val(s: str) -> str:
                try:
                    return (s or '').strip().strip('"\'"')
                except Exception:
                    return (s or '').strip()
            tgt = _norm_val(target_new_value)
            matches = []
            for param in template.params:
                try:
                    if param.showkey:
                        continue
                    if _norm_val(str(param.value)) == tgt and tgt != '':
                        matches.append(param)
                except Exception:
                    continue
            if len(matches) < 2:
                return template_text
            keep = matches[0] if mode == 'left' else matches[-1]
            for param in matches:
                if param is not keep:
                    template.remove(param)
            return str(template)
        except Exception:
            return template_text
    
    def _apply_cache_to_chunk(self, family: str, lang: str, chunk: str) -> Tuple[str, int]:
        """
        Apply cached rules to single template chunk.
        
        Args:
            family: Project family
            lang: Language code
            chunk: Template chunk string
            
        Returns:
            Tuple of (new_chunk, number_of_replacements)
        """
        try:
            template = self._parse_single_template(chunk)
            if template is None or not template.params:
                return chunk, 0

            tmpl_key = self._norm_tmpl_key(str(template.name), family, lang)
            bucket = self.template_auto_cache.get(tmpl_key) or {}
            if not bucket:
                return chunk, 0

            from ..utils import normalize_spaces_for_compare as _norm

            def _normalize_for_compare(value: str) -> str:
                return _norm(value)

            def _strip_quotes(value: str) -> str:
                stripped = str(value or '').strip()
                if (
                    len(stripped) >= 2
                    and stripped[0] == stripped[-1]
                    and stripped[0] in ('"', "'")
                ):
                    return stripped[1:-1]
                return stripped

            def _unnamed_params():
                return [param for param in template.params if not param.showkey]

            changed = False
            drop_params = []

            # Compatibility with legacy cached positional mappings.
            applied_sequence = False
            for sequence in bucket.get('unnamed_sequence') or []:
                positional = _unnamed_params()
                valid = True
                for idx1, old_value, _new_value in sequence:
                    idx = int(idx1) - 1
                    if (
                        idx < 0
                        or idx >= len(positional)
                        or _normalize_for_compare(str(positional[idx].value))
                        != _normalize_for_compare(old_value)
                    ):
                        valid = False
                        break
                if valid:
                    for idx1, _old_value, new_value in sequence:
                        positional[int(idx1) - 1].value = str(new_value)
                    changed = True
                    applied_sequence = True
                    break

            if not applied_sequence:
                for old_value, new_value in (bucket.get('unnamed_single') or {}).items():
                    positional = _unnamed_params()
                    matches = [
                        param
                        for param in positional
                        if _normalize_for_compare(str(param.value))
                        == _normalize_for_compare(old_value)
                    ]
                    if len(matches) == 1:
                        matches[0].value = str(new_value)
                        changed = True

            bucket_auto = str(bucket.get('auto') or '').strip().casefold()
            for rule in bucket.get('rules') or []:
                try:
                    rule_auto = str(rule.get('auto', 'none')).strip().casefold()
                    if rule_auto != 'approve' and bucket_auto != 'approve':
                        continue
                    rule_type = str(rule.get('type') or '')

                    if rule_type == 'named':
                        target_name = re.sub(
                            r'[_\s]+', ' ', str(rule.get('param') or '').strip()
                        ).casefold()
                        source = str(rule.get('from') or '').strip()
                        target = str(rule.get('to') or '').strip()
                        for param in template.params:
                            if not param.showkey:
                                continue
                            param_name = re.sub(
                                r'[_\s]+', ' ', str(param.name).strip()
                            ).casefold()
                            if (
                                param_name == target_name
                                and _normalize_for_compare(str(param.value))
                                == _normalize_for_compare(source)
                            ):
                                param.value = target
                                changed = True

                    elif rule_type == 'unnamed_single':
                        source = str(rule.get('from') or '').strip()
                        target = str(rule.get('to') or '').strip()
                        positional = _unnamed_params()
                        matches = []
                        for index, param in enumerate(positional):
                            value = str(param.value)
                            plain = _strip_quotes(value)
                            if target and (target in value or target in plain):
                                continue
                            if (
                                _normalize_for_compare(value)
                                == _normalize_for_compare(source)
                                or _normalize_for_compare(plain)
                                == _normalize_for_compare(source)
                            ):
                                quote_char = ''
                                stripped = value.strip()
                                if (
                                    len(stripped) >= 2
                                    and stripped[0] == stripped[-1]
                                    and stripped[0] in ('"', "'")
                                ):
                                    quote_char = stripped[0]
                                matches.append((index, param, quote_char))
                        if len(matches) != 1:
                            continue

                        match_index, match_param, quote_char = matches[0]
                        new_value = (
                            f'{quote_char}{target}{quote_char}' if quote_char else target
                        )
                        trial_values = [str(param.value) for param in positional]
                        trial_values[match_index] = new_value
                        duplicate_indices = [
                            index
                            for index, value in enumerate(trial_values)
                            if _normalize_for_compare(_strip_quotes(value))
                            == _normalize_for_compare(target)
                        ]
                        dedupe_mode = self.normalize_dedupe_mode(rule.get('dedupe'))
                        if len(duplicate_indices) >= 2 and not dedupe_mode:
                            continue

                        match_param.value = new_value
                        changed = True
                        if len(duplicate_indices) >= 2 and dedupe_mode in ('left', 'right'):
                            keep_index = (
                                duplicate_indices[0]
                                if dedupe_mode == 'left'
                                else duplicate_indices[-1]
                            )
                            drop_params.extend(
                                positional[index]
                                for index in duplicate_indices
                                if index != keep_index
                            )

                    elif rule_type == 'unnamed_sequence':
                        sequence = rule.get('sequence') or []
                        positional = _unnamed_params()
                        valid = True
                        for item in sequence:
                            idx = int(item.get('idx', 0)) - 1
                            source = str(item.get('from') or '').strip()
                            if (
                                idx < 0
                                or idx >= len(positional)
                                or _normalize_for_compare(str(positional[idx].value))
                                != _normalize_for_compare(source)
                            ):
                                valid = False
                                break
                        if valid:
                            for item in sequence:
                                idx = int(item.get('idx', 0)) - 1
                                positional[idx].value = str(item.get('to') or '').strip()
                            changed = True
                except Exception:
                    continue

            for param in drop_params:
                try:
                    template.remove(param)
                except Exception:
                    pass

            return (str(template), 1) if changed else (chunk, 0)
        except Exception:
            return chunk, 0
    
    def apply_cached_template_rules(self, text: str, family: str, lang: str) -> Tuple[str, int]:
        """
        Apply all suitable cached rules to page text.
        
        Args:
            text: Page text
            family: Project family
            lang: Language code
            
        Returns:
            Tuple of (modified_text, total_replacements)
        """
        try:
            # Check for external changes
            self._maybe_reload_rules()
            if not self.template_auto_cache:
                return text, 0

            code = mwparserfromhell.parse(text or '')
            total_applied = 0
            # ``filter_templates`` is pre-order. Reverse it so nested templates
            # are processed before their parents and every rule stays scoped to
            # the template it was learned from.
            templates = list(code.filter_templates(recursive=True))
            for template in reversed(templates):
                chunk = str(template)
                tmpl_key = self._norm_tmpl_key(str(template.name), family, lang)
                bucket = self.template_auto_cache.get(tmpl_key) or {}
                has_rules = (
                    bool(bucket.get('rules') or [])
                    or bool(bucket.get('unnamed_single'))
                    or bool(bucket.get('unnamed_sequence'))
                )
                auto_allowed = (
                    has_rules
                    or str(bucket.get('auto', '')).strip().casefold() == 'approve'
                    or bool(self.auto_confirm_direct_all)
                )
                if not auto_allowed:
                    continue

                new_chunk, applied = self._apply_cache_to_chunk(family, lang, chunk)
                if applied and new_chunk != chunk:
                    code.replace(template, new_chunk, recursive=True)
                    total_applied += applied

            return str(code), total_applied
        except Exception:
            return text, 0
    
    def set_template_auto_flag(self, template_name: str, family: str, lang: str, auto: bool = True):
        """
        Set approval (auto-confirmation) flag for template.
        
        Args:
            template_name: Template name
            family: Project family
            lang: Language code
            auto: Auto-confirmation flag
        """
        tmpl_key = self._norm_tmpl_key(template_name, family, lang)
        if tmpl_key:
            bucket = self._ensure_cache_bucket(tmpl_key)
            bucket['auto'] = 'approve' if auto else 'none'
            if auto:
                # Взаимоисключаем с пропуском
                self.auto_skip_templates.discard(tmpl_key)
            self._save_rules_file()
    
    def set_template_skip_flag(self, template_name: str, family: str, lang: str, skip: bool = True):
        """
        Set auto-skip flag for template.
        
        Args:
            template_name: Template name
            family: Project family
            lang: Language code
            skip: Auto-skip flag
        """
        tmpl_key = self._norm_tmpl_key(template_name, family, lang)
        if tmpl_key:
            if skip:
                self.auto_skip_templates.add(tmpl_key)
            else:
                self.auto_skip_templates.discard(tmpl_key)
            bucket = self._ensure_cache_bucket(tmpl_key)
            bucket['auto'] = 'skip' if skip else 'none'
            self._save_rules_file()
    
    def is_template_auto_skip(self, template_name: str, family: str, lang: str) -> bool:
        """
        Check if template is marked for auto-skip.
        
        Args:
            template_name: Template name
            family: Project family
            lang: Language code
            
        Returns:
            True if template should be auto-skipped
        """
        # Ensure latest rules are loaded
        self._maybe_reload_rules()
        tmpl_key = self._norm_tmpl_key(template_name, family, lang)
        return tmpl_key in self.auto_skip_templates
    
    def clear_template_cache(self):
        """Clear all template cache data."""
        self.template_auto_cache.clear()
        self.auto_skip_templates.clear()
        self.auto_confirm_direct_all = False
        self.auto_skip_direct_all = False
        try:
            self._save_rules_file()
        except Exception:
            pass


# Global instance for backward compatibility
_template_manager = None
