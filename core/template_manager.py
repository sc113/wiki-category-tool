"""
Template management module for handling template rules and caching.
"""
import os
import json
import re
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
    
    def _project_key(self, family: str, lang: str) -> str:
        """Возвращает ключ проекта в формате 'lang:family' (например 'ru:wikipedia')."""
        return f"{(lang or '').strip().lower()}:{(family or '').strip().lower()}"

    def _load_rules(self):
        """Load template rules from JSON file supporting legacy and new formats.

        New format consolidates approve/skip into a single string field 'auto':
        - 'approve' → approve=True,  skip=False
        - 'skip'    → approve=False, skip=True
        - 'none' or missing/unknown → approve=False, skip=False
        Legacy boolean fields 'approve' and 'skip' continue to be accepted.
        'auto' has priority if present.
        """
        try:
            if self._rules_file_path and os.path.exists(self._rules_file_path):
                with open(self._rules_file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    # Формат V2: ключи верхнего уровня — 'lang:family'
                    # Значения — dict с шаблонами напрямую или в поле 'templates'.
                    # Формат V1 (legacy): data['rename_worker'] содержит словарь
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
    
    def _parse_template_tokens(self, template_chunk: str) -> Tuple[str, List[str]]:
        """
        Parse template {{...}} into name and parameter list.
        
        Args:
            template_chunk: Template string like {{template|param1|param2}}
            
        Returns:
            Tuple of (template_name, parameters_list)
        """
        if not (template_chunk.startswith('{{') and template_chunk.endswith('}}')):
            return '', []
        inner = template_chunk[2:-2]
        parts = inner.split('|')
        if not parts:
            return '', []
        head = parts[0]
        params = parts[1:]
        return head, params
    
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
        name_b, params_b = self._parse_template_tokens(before_chunk)
        name_a, params_a = self._parse_template_tokens(after_chunk)
        result = {'name': name_a or name_b, 'named': {}, 'unnamed': []}
        
        if not params_b or not params_a:
            return result
            
        un_b, nm_b = self._split_params(params_b)
        un_a, nm_a = self._split_params(params_a)
        
        # Match named parameters by name (case-insensitive, normalize spaces/underscores)
        def _norm_name(s: str) -> str:
            s2 = (s or '').strip()
            try:
                s2 = re.sub(r"[_\s]+", " ", s2)
            except Exception:
                pass
            return s2.casefold()
        
        map_b: Dict[str, str] = {}
        for left, eq, val in nm_b:
            nm = _norm_name(left)
            map_b[nm] = (val or '').strip()
            
        map_a: Dict[str, str] = {}
        for left, eq, val in nm_a:
            nm = _norm_name(left)
            map_a[nm] = (val or '').strip()
            
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
            name, params = self._parse_template_tokens(chunk)
            if not params:
                return chunk, 0
                
            tmpl_key = self._norm_tmpl_key(name, family, lang)
            if not tmpl_key:
                return chunk, 0
                
            bucket = self.template_auto_cache.get(tmpl_key) or {}
            if not bucket:
                return chunk, 0
                
            unnamed, named = self._split_params(params)
            changed = False
            # Индексы безымянных параметров, которые нужно удалить при реконструкции (дедупликация)
            drop_unnamed_indices: set[int] = set()
            
            # Старые маппинги больше не используются — применяем только rules
            
            # Нормализация через utils для единообразия по всему проекту
            from ..utils import normalize_spaces_for_compare as _norm
            def _normalize_for_compare(s: str) -> str:
                return _norm(s)
            
            # First try unnamed sequences
            applied_sequence = False
            for seq in (bucket.get('unnamed_sequence') or []):
                ok = True
                for idx1, oldv, newv in seq:
                    if idx1 - 1 >= len(unnamed) or _normalize_for_compare(unnamed[idx1 - 1]) != _normalize_for_compare(oldv):
                        ok = False
                        break
                if ok:
                    # Apply all sequence elements
                    tmp = list(unnamed)
                    for idx1, oldv, newv in seq:
                        tmp[idx1 - 1] = newv
                    unnamed = tmp
                    changed = True
                    applied_sequence = True
                    break
            
            # If no sequence applied - single unnamed rules
            if not applied_sequence and (bucket.get('unnamed_single') or {}):
                mapping = bucket.get('unnamed_single') or {}
                # For each rule find EXACTLY one match
                tmp = list(unnamed)
                for oldv, newv in mapping.items():
                    matches = [i for i, tok in enumerate(tmp) if _normalize_for_compare(tok) == _normalize_for_compare(oldv)]
                    if len(matches) == 1:
                        tmp[matches[0]] = newv
                        changed = True
                unnamed = tmp

            # Дополнительно применяем независимые правила bucket['rules'] (поддержка множества правил на шаблон)
            # Применяем только те, для которых auto=approve
            # bucket-level автофлаг
            bucket_auto = str((bucket.get('auto') or '')).strip().casefold()
            for rule in (bucket.get('rules') or []):
                try:
                    rtype = rule.get('type')
                    rule_auto = str(rule.get('auto', 'none')).strip().casefold()
                    # Разрешаем применение, если auto=approve у правила ИЛИ у всего шаблона
                    if not ((rule_auto == 'approve') or (bucket_auto == 'approve')):
                        continue
                    if rtype == 'named':
                        nm_cf = (rule.get('param') or '').strip().casefold()
                        src = (rule.get('from') or '').strip()
                        dst = (rule.get('to') or '').strip()
                        new_named2: List[Tuple[str, str, str]] = []
                        for left, eq, val in named:
                            left_cf = re.sub(r"[_\s]+", " ", (left or '').strip()).casefold()
                            if left_cf == nm_cf and _normalize_for_compare(val) == _normalize_for_compare(src):
                                val = dst
                                changed = True
                            new_named2.append((left, eq, val))
                        named = new_named2
                    elif rtype == 'unnamed_single':
                        src = (rule.get('from') or '').strip()
                        dst = (rule.get('to') or '').strip()
                        tmp = list(unnamed)
                        matches = [i for i, tok in enumerate(tmp) if _normalize_for_compare(tok) == _normalize_for_compare(src)]
                        if len(matches) == 1:
                            # Пробное применение для оценки дублей, если дедуп не задан
                            trial_tmp = list(tmp)
                            trial_tmp[matches[0]] = dst
                            # Определим режим дедупликации, если сохранён
                            try:
                                dedupe_mode = str(rule.get('dedupe') or '').strip()
                            except Exception:
                                dedupe_mode = ''
                            # Если дедуп не задан (unset) и создаются дубли — НЕ применяем автоматически (пусть отработает интерактивный диалог)
                            if not dedupe_mode:
                                dup_idx = [i for i, tok in enumerate(trial_tmp) if _normalize_for_compare(tok) == _normalize_for_compare(dst)]
                                if len(dup_idx) >= 2:
                                    # пропускаем авто‑замену этой позиции, чтобы диалог показался позже
                                    pass
                                else:
                                    tmp = trial_tmp
                                    changed = True
                            else:
                                # Обратная совместимость: keep_first/keep_second → left/right
                                if dedupe_mode == 'keep_first':
                                    dedupe_mode = 'left'
                                elif dedupe_mode == 'keep_second':
                                    dedupe_mode = 'right'
                                tmp = trial_tmp
                                changed = True
                                # Применяем удаление дублей по политике, если они есть
                                dup_idx = [i for i, tok in enumerate(tmp) if _normalize_for_compare(tok) == _normalize_for_compare(dst)]
                                if len(dup_idx) >= 2:
                                    if dedupe_mode == 'left':
                                        for i in dup_idx[1:]:
                                            drop_unnamed_indices.add(i)
                                    elif dedupe_mode == 'right':
                                        for i in dup_idx[:-1]:
                                            drop_unnamed_indices.add(i)
                                    elif dedupe_mode == 'keep_both':
                                        pass
                        unnamed = tmp
                    elif rtype == 'unnamed_sequence':
                        seq = rule.get('sequence') or []
                        ok = True
                        for item in seq:
                            idx1 = int(item.get('idx', 0))
                            src = (item.get('from') or '').strip()
                            if idx1 - 1 >= len(unnamed) or _normalize_for_compare(unnamed[idx1 - 1]) != _normalize_for_compare(src):
                                ok = False
                                break
                        if ok:
                            tmp = list(unnamed)
                            for item in seq:
                                idx1 = int(item.get('idx', 0))
                                dst = (item.get('to') or '').strip()
                                tmp[idx1 - 1] = dst
                            unnamed = tmp
                            changed = True
                except Exception:
                    continue
            
            if not changed:
                return chunk, 0
            
            # Rebuild template
            parts_new: List[str] = [name]
            # Preserve order: first unnamed and named in original params order
            rebuilt_params: List[str] = []
            unnamed_idx = 0
            named_idx = 0
            
            for raw in params:
                if '=' in raw:
                    # Named сохраняем как есть по текущему списку named
                    try:
                        left, eq, val = named[named_idx]
                    except Exception:
                        # На всякий случай: если рассинхронизация
                        left, eq, val = ('', '=', '')
                    named_idx += 1
                    rebuilt_params.append(f"{left}{eq}{val}")
                else:
                    # Проверяем, не помечен ли текущий безымянный индекс
                    if unnamed_idx in drop_unnamed_indices:
                        unnamed_idx += 1
                        continue
                    try:
                        tok = unnamed[unnamed_idx]
                    except Exception:
                        tok = ''
                    unnamed_idx += 1
                    rebuilt_params.append(tok)
                    
            parts_new.extend(rebuilt_params)
            new_inner = '|'.join(parts_new)
            return '{{' + new_inner + '}}', 1
            
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
                
            total_applied = 0
            start = 0
            out_text = text
            
            while True:
                l = out_text.find('{{', start)
                if l == -1:
                    break
                r = out_text.find('}}', l + 2)
                if r == -1:
                    break
                    
                chunk = out_text[l:r+2]
                if '|' not in chunk:
                    start = r + 2
                    continue
                
                # Check approve flag for specific template
                name, _ = self._parse_template_tokens(chunk)
                tmpl_key = self._norm_tmpl_key(name, family, lang)
                bucket = self.template_auto_cache.get(tmpl_key) or {}
                # Автоприменяем, если для шаблона существуют сохранённые правила
                # (rules/unnamed_*), либо явно разрешён автоаппрув, либо включён глобальный флаг.
                has_rules = bool((bucket.get('rules') or [])) or bool(bucket.get('unnamed_single')) or bool(bucket.get('unnamed_sequence'))
                auto_allowed = has_rules or (str(bucket.get('auto', '')).strip().casefold() == 'approve') or bool(self.auto_confirm_direct_all)
                if not auto_allowed:
                    start = r + 2
                    continue
                
                new_chunk, applied = self._apply_cache_to_chunk(family, lang, chunk)
                if applied and new_chunk != chunk:
                    out_text = out_text[:l] + new_chunk + out_text[r+2:]
                    total_applied += applied
                    start = l + len(new_chunk)
                else:
                    start = r + 2
                    
            return out_text, total_applied
            
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
