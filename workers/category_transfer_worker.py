"""Category transfer reads and guarded category/Wikidata creation."""

from __future__ import annotations

import math
import time

import pywikibot
from PySide6.QtCore import Signal

from .base_worker import BaseWorker
from .create_worker import _format_summary
from .login_worker import LoginWorker
from ..core.category_transfer import (
    CategoryCreationRow, CategoryItemConflict, CategoryTransferReader, TransferStopped, message,
)
from ..core.pywikibot_config import use_wikidata_profile
from ..core.namespace_manager import normalize_title_by_selection
from ..core.localization import translate_runtime


_UNRESOLVED_ITEM = object()


class CategoryTransferReadWorker(BaseWorker):
    row_ready = Signal(object)
    read_progress = Signal(object)
    retry_wait = Signal(str)

    def __init__(self, *, source_lang: str, target_lang: str, family: str,
                 delay: float = 0.5, categories: list[dict] | None = None,
                 category: str = "", depth: int = 0, skip_existing_level: int = 2,
                 include_root: bool = False, namespace_selection: str | int = 'auto'):
        super().__init__("", "", target_lang, family)
        self.reader = CategoryTransferReader(source_lang, target_lang, family,
                                             self._stop_event, delay)
        self.reader.on_retry = self._retry_notice
        self.reader.on_activity = self._read_activity
        self.categories = categories
        self.category = category
        self.depth = depth
        self.skip_existing_level = skip_existing_level
        self.include_root = include_root
        self.namespace_selection = namespace_selection
        self.results: list[dict] = []
        self.error_count = 0

    def _retry_notice(self, text):
        self.progress.emit(text)
        self.retry_wait.emit(text)

    def _read_activity(self, payload):
        self.read_progress.emit(payload)
        phase = payload.get('phase')
        keys = {'members': 'ui.source.fetch_members_batch_progress',
                'tree': 'ui.source.fetch_subcategories_progress',
                'links': 'ui.transfer.checking_links_progress',
                'scan_categories': 'ui.transfer.scan_batch_categories',
                'scan_members': 'ui.transfer.scan_batch_members',
                'scan_links': 'ui.transfer.scan_batch_links',
                'scan_articles': 'ui.transfer.scan_article_packet'}
        if phase in keys:
            self.progress.emit(translate_runtime(keys[phase], keys[phase]).format(**payload))
        elif phase == 'expected' and payload.get('total') is not None:
            self.progress.emit(translate_runtime('ui.source.fetch_members_total_known',
                                                'Category total: {total}').format(
                category=payload['category'], total=payload['total'], pages=0, subcategories=payload['total']))

    def run(self):
        try:
            if self.categories is None:
                category = normalize_title_by_selection(self.category, self.reader.family,
                                                       self.reader.source_lang, self.namespace_selection)
                self.results = self.reader.discover(
                    category, self.depth, self.skip_existing_level, self.include_root,
                    self.row_ready.emit,
                    lambda title, count: self.read_progress.emit({"title": title, "count": count}),
                )
            else:
                for offset in range(0, len(self.categories), 5):
                    prepared = []
                    for row in self.categories[offset:offset + 5]:
                        self.reader.check_stop()
                        original = row['source_title']
                        error = None
                        try:
                            if row.get('manual_input'):
                                row = {**row, 'source_title': self.reader.category_title(original, self.reader.source_lang)}
                        except TransferStopped:
                            raise
                        except Exception as caught:
                            error = caught
                        prepared.append((row, original, error))
                    self.reader.prepare_scan_batch([row for row, _, error in prepared if error is None])
                    for row, original_title, error in prepared:
                        self.reader.check_stop()
                        self.read_progress.emit({'title': row['source_title'], 'count': len(self.results)})
                        try:
                            if error is not None:
                                raise error
                            result = self.reader.scan(row)
                        except TransferStopped:
                            raise
                        except Exception as error:
                            self.error_count += 1
                            result = {**row, 'status': 'error', 'error': str(error),
                                      'potential': None, 'total': None, 'target_articles': []}
                            self.progress.emit(message('row_error', title=row['source_title'], error=error))
                        if result['source_title'] != original_title:
                            result = {**result, 'input_source_title': original_title}
                        self.results.append(result)
                        self.row_ready.emit(result)
                        self.item_processed.emit()
        except TransferStopped:
            pass
        except Exception as error:
            self._set_failure(error)
            self.progress.emit(str(error))
        finally:
            self.reader.close()


class CategoryTransferCreateWorker(BaseWorker):
    category_result = Signal(object)
    interactive_input_requested = Signal(str, bool)

    def __init__(self, *, rows: tuple[CategoryCreationRow, ...], username: str,
                 password: str, source_lang: str, target_lang: str, family: str,
                 summary: str, create_missing: bool = True):
        super().__init__(username, password, target_lang, family)
        self.rows = rows
        self.source_lang = source_lang
        self.summary = summary
        self.create_missing = create_missing
        self.linked_categories: list[str] = []
        self.stats = {"created": 0, "linked": 0, "labels": 0, "exists": 0, "already": 0, "failed": 0}
        self._label_changes = {}
        self._login_helper = LoginWorker(username, password, target_lang, family)
        self._login_helper.interactive_input_requested.connect(self.interactive_input_requested)

    def provide_interactive_input(self, answer):
        self._login_helper.provide_interactive_input(answer)

    def request_stop(self):
        super().request_stop()
        self._login_helper.request_stop()

    def _login_sites(self, target_site, repo):
        original = self._login_helper._patch_pywikibot_input(pywikibot)
        try:
            target_site.login(user=self.username)
            if self._stop:
                raise TransferStopped()
            repo.login(user=self.username)
        finally:
            self._login_helper._restore_pywikibot_input(pywikibot, *original)

    @staticmethod
    def _category(site, raw: str):
        # Localized and canonical category prefixes are parsed by Pywikibot.
        page = pywikibot.Category(site, raw)
        if page.namespace().id != 14 or page.section():
            raise ValueError(message("invalid_title", title=raw))
        return page

    @staticmethod
    def _item_for(page):
        if not page.exists():
            return None
        # Resolve by wiki/title directly in Wikidata. preload_entities performs
        # a fresh lookup and returns a fully loaded item, including its revision.
        # This avoids both the pageprops round trip and their replication lag.
        item = next(page.site.data_repository().preload_entities([page]), None)
        if item is not None:
            item.get()  # Cached; also reject redirects as the old lookup did.
        return item

    def _resolve_item(self, source, target, *, items=None):
        source_item, target_item = items if items is not None else (self._item_for(source), self._item_for(target))
        if source_item and target_item and source_item.getID() != target_item.getID():
            raise CategoryItemConflict(source.title(), target.title(), source_item.getID(), target_item.getID())
        item = source_item or target_item
        if item:
            for page in (source, target):
                sitelink = item.sitelinks.get(page.site.dbName())
                if sitelink:
                    # SiteLink.title excludes its namespace. canonical_title()
                    # restores the namespace in the linked wiki's language.
                    linked_title = sitelink.canonical_title()
                    if linked_title.replace("_", " ") != page.title().replace("_", " "):
                        raise ValueError(message("sitelink_conflict", title=page.title(), linked=linked_title))
        return item

    def _create_only(self, page, content: str) -> bool:
        summary = _format_summary(self.summary, content)
        for attempt in range(1, 7):
            if not self._wait_before_save():
                return False
            try:
                page.text = content
                page.save(summary=summary, minor=False, quiet=True, createonly=True)
                self.saved_edits += 1
                self._decay_save_interval()
                return True
            except Exception as error:
                if not self._is_rate_error(error) or attempt == 6:
                    raise
                self._increase_save_interval(attempt)
                wait = max(self._save_min_interval, self._extract_wait_seconds(str(error)))
                self._emit_rate_notice(wait, attempt, 6)
                if not self._interruptible_wait(wait):
                    return False
        return False

    @staticmethod
    def _link_data(source, target, item):
        data = {"sitelinks": {}, "labels": {}}
        for page in (source, target):
            wiki = page.site.dbName()
            if not item or wiki not in item.sitelinks:
                data["sitelinks"][wiki] = {"site": wiki, "title": page.title()}
            code = page.site.lang
            # Repair namespace-free labels generated by earlier versions, while
            # preserving manually chosen labels and other languages.
            label = item.labels.get(code, "") if item else ""
            if not label or label == page.title(with_ns=False):
                data["labels"][code] = page.title()
        return data

    def _link_categories(self, source, target, repo, *, resolved_item=_UNRESOLVED_ITEM) -> tuple[str, str]:
        self._label_changes = {}
        # Existing pages were checked immediately before this call. A newly
        # created page requires another check after its save has completed.
        item = self._resolve_item(source, target) if resolved_item is _UNRESOLVED_ITEM else resolved_item
        data = self._link_data(source, target, item)
        old_labels = item.labels if item else {}
        added = sorted(code for code, value in data['labels'].items() if not old_labels.get(code))
        updated = sorted(code for code, value in data['labels'].items()
                         if old_labels.get(code) and old_labels[code] != value)
        summary = self._wikidata_summary(data, added, updated, new_item=item is None)
        if item is None:
            item = pywikibot.ItemPage(repo)
            claim = pywikibot.Claim(repo, "P31")
            claim.setTarget(pywikibot.ItemPage(repo, "Q4167836"))
            data["claims"] = [claim.toJSON()]
        if not data["sitelinks"] and not data["labels"]:
            return item.getID(), "already_linked"
        if not self._wait_before_save():
            raise TransferStopped()
        # One atomic Wikidata edit includes both sitelinks, labels and new P31.
        # No blind retry of creation: an uncertain response must not duplicate an item.
        item.editEntity(data, summary=summary, bot=True)
        self.saved_edits += 1
        self._label_changes = {key: codes for key, codes in (('added', added), ('updated', updated)) if codes}
        return item.getID(), "linked" if data["sitelinks"] else "labels_updated"

    @staticmethod
    def _wikidata_summary(data, added, updated, *, new_item=False):
        parts = ['Create Wikimedia category item'] if new_item else []
        if data['sitelinks']:
            parts.append('Link Wikimedia categories')
        for action, codes in (('Add', added), ('Update', updated)):
            if codes:
                parts.append(f'{action} Wikimedia category labels ({", ".join(codes)})')
        return '; '.join(parts)

    def _label_note(self):
        return ' '.join(message('labels_' + key + '_detail', languages=', '.join(codes))
                        for key, codes in self._label_changes.items())

    def run(self):
        try:
            use_wikidata_profile(self.username)
            source_site = pywikibot.Site(self.source_lang, self.family, user=self.username)
            target_site = pywikibot.Site(self.lang, self.family, user=self.username)
            repo = target_site.data_repository()
            if repo is None or source_site.data_repository() != repo:
                raise ValueError(message("no_repository"))
            if source_site.dbName() == target_site.dbName():
                raise ValueError(message('same_wiki'))
            self._login_sites(target_site, repo)
            # Normalize ALL rows first, before creating any page.
            prepared = []
            sources, targets = set(), set()
            for row in self.rows:
                if self._stop:
                    return
                source = self._category(source_site, row.source_title)
                target = self._category(target_site, row.target_title)
                if source.title() in sources or target.title() in targets:
                    raise ValueError(message("tsv_duplicate", line=len(prepared) + 1))
                sources.add(source.title())
                targets.add(target.title())
                prepared.append((row, source, target))
            for row_index, (row, source, target) in enumerate(prepared, 1):
                if self._stop:
                    break
                created = False
                try:
                    if not source.exists() or source.isRedirectPage():
                        raise ValueError(message("source_missing", title=source.title()))
                    if target.exists() and target.isRedirectPage():
                        raise ValueError(message("target_redirect", title=target.title()))
                    if not self.create_missing and not target.exists():
                        raise ValueError(message('existing_target_required', title=target.title()))
                    item = self._resolve_item(source, target)
                    data = self._link_data(source, target, item)
                    if target.exists() and item and not data["sitelinks"] and not data["labels"]:
                        qid = item.getID()
                        self.stats["already"] += 1
                        self.linked_categories.append(target.title())
                        self.category_result.emit({"source_title": row.source_title, "target_title": target.title(),
                                                   "qid": qid, "source_qid": qid, "target_qid": qid,
                                                   "status": "already_linked"})
                        self.item_processed.emit()
                        continue
                    if not target.exists():
                        if not row.content.strip():
                            raise ValueError(message("tsv_content_required", line=row_index))
                        if not self._create_only(target, row.content):
                            break
                        created = True
                        self.stats["created"] += 1
                    else:
                        self.stats["exists"] += 1
                    if self._stop:
                        raise TransferStopped()
                    self._label_changes = {}
                    if created:
                        qid, action = self._link_categories(source, target, repo)
                    else:
                        qid, action = self._link_categories(source, target, repo, resolved_item=item)
                    self.stats[{"linked": "linked", "labels_updated": "labels", "already_linked": "already"}[action]] += 1
                    if action != 'labels_updated' and self._label_changes:
                        self.stats['labels'] += 1
                    self.linked_categories.append(target.title())
                    self.category_result.emit({"source_title": row.source_title,
                                               "target_title": target.title(), "qid": qid,
                                               "source_qid": qid, "target_qid": qid,
                                               "link_action": action,
                                               "label_changes": dict(self._label_changes), "detail": self._label_note(),
                                               "status": "created" if created else action})
                    log_key = {"linked": "linked_log", "labels_updated": "labels_updated_log", "already_linked": "skipped_log"}[action]
                    self.progress.emit(' '.join(filter(None, (message(log_key, title=target.title(), qid=qid), self._label_note()))))
                except TransferStopped:
                    self.category_result.emit({"source_title": row.source_title,
                                               "target_title": target.title(),
                                               "status": "unlinked" if created else "stopped"})
                    break
                except CategoryItemConflict as error:
                    self.stats["failed"] += 1
                    self.category_result.emit({"source_title": row.source_title, "target_title": target.title(),
                                               "source_qid": error.source_qid, "target_qid": error.target_qid,
                                               "status": "error", "error": str(error)})
                    self.progress.emit(message("row_error", title=target.title(), error=error))
                except Exception as error:
                    self.stats["failed"] += 1
                    self.category_result.emit({"source_title": row.source_title,
                                               "target_title": target.title(),
                                               "status": "unlinked" if created else "error",
                                               "error": str(error)})
                    self.progress.emit(message("row_error", title=target.title(), error=error))
                self.item_processed.emit()
        except Exception as error:
            self._set_failure(error)
            self.progress.emit(str(error))


class CategoryTransferPreviewWorker(BaseWorker):
    row_ready = Signal(object)
    retry_wait = Signal(str)
    activity = Signal(object)

    def __init__(self, *, rows: tuple[CategoryCreationRow, ...], source_lang: str,
                 target_lang: str, family: str, delay: float = 0.5):
        super().__init__("", "", target_lang, family)
        self.rows = rows
        self.results = []
        self.reader = CategoryTransferReader(source_lang, target_lang, family,
                                             self._stop_event, delay)
        self.reader.on_retry = self._retry_notice
        self.reader.on_activity = self._preview_activity
        self._preview_started = None
        self._preview_log_stage = None
        self._preview_percent = 0

    def _preview_activity(self, payload):
        now = time.monotonic()
        if self._preview_started is None:
            self._preview_started = now
        step = message(payload['phase'], count=payload['count'], lang=payload['lang'])
        stage = payload['stage']
        total = payload['count']
        fraction = payload['processed'] / total if total else 1
        overall = (stage - 1 + fraction) / payload['stages']
        # Completed steps and skipped/cached lookups must never move the bar back.
        self._preview_percent = max(self._preview_percent, min(100, int(overall * 100)))
        counter = message('check_counter', checked=payload['processed'], total=total) if total else ''
        elapsed = now - self._preview_started
        eta = ''
        if elapsed >= 5 and 0.1 <= overall < 1:
            seconds = max(5, math.ceil(elapsed * (1 - overall) / overall / 5) * 5)
            duration = (message('check_eta_minutes', minutes=math.ceil(seconds / 60)) if seconds >= 60
                        else message('check_eta_seconds', seconds=seconds))
            eta = message('check_eta', duration=duration)
        text = message('check_progress', percent=self._preview_percent, step=step,
                       counter=counter, eta=eta)
        if stage != self._preview_log_stage:
            self._preview_log_stage = stage
            self.progress.emit(step)
        self.activity.emit({**payload, 'percent': self._preview_percent, 'text': text})

    def _preview_row(self, result):
        self.row_ready.emit(result)

    def _retry_notice(self, text):
        self.progress.emit(text)
        self.retry_wait.emit(text)

    def run(self):
        try:
            self.results = self.reader.preview_creation(self.rows, self._preview_row, check_labels=True)
        except TransferStopped:
            pass
        except Exception as error:
            self._set_failure(error)
            self.progress.emit(str(error))
        finally:
            self.reader.close()


class CategoryTextReadWorker(BaseWorker):
    content_ready = Signal(object)
    retry_wait = Signal(str)

    def __init__(self, *, rows, source_lang, target_lang, family, delay=0.5):
        super().__init__("", "", target_lang, family)
        self.rows = tuple(rows)
        self.reader = CategoryTransferReader(source_lang, target_lang, family, self._stop_event, delay)
        self.reader.on_retry = self.retry_wait.emit
        self.stats = {"loaded": 0, "failed": 0}

    def run(self):
        try:
            normalized = []
            for index, title in self.rows:
                self.reader.check_stop()
                try:
                    normalized.append((index, self.reader.category_title(title, self.reader.source_lang)))
                except ValueError as error:
                    self.stats["failed"] += 1
                    self.progress.emit(message("row_error", title=title, error=error))
                    self.item_processed.emit()
            for offset in range(0, len(normalized), 50):
                batch = normalized[offset:offset + 50]
                pages = self.reader.pages(self.reader.source_lang, [title for _, title in batch], props="info|revisions")
                for index, title in batch:
                    self.reader.check_stop()
                    page = pages.get(title)
                    if not self.reader.valid_page(page, 14):
                        self.stats["failed"] += 1
                        self.progress.emit(message("source_missing", title=title))
                    else:
                        revision = (page.get("revisions") or [{}])[0]
                        slot = revision.get("slots", {}).get("main", revision)
                        if "content" not in slot and "*" not in slot:
                            self.stats["failed"] += 1
                            self.progress.emit(message("content_unavailable", title=title))
                        else:
                            self.content_ready.emit(dict(index=index, content=slot.get("content", slot.get("*", ""))))
                            self.stats["loaded"] += 1
                    self.item_processed.emit()
        except TransferStopped:
            pass
        except Exception as error:
            self._set_failure(error)
            self.progress.emit(str(error))
        finally:
            self.reader.close()


class CategoryArticlePresenceWorker(BaseWorker):
    """On-demand read of existing membership; never edit an article."""

    result_ready = Signal(object)

    def __init__(self, *, articles, category, source_lang, target_lang, family, source_category='', delay=0.5):
        super().__init__('', '', target_lang, family)
        self.articles = tuple(dict.fromkeys(articles)) if articles is not None else None
        self.category = category
        self.source_category = source_category
        self.source_lang = source_lang
        self.reader = CategoryTransferReader(source_lang, target_lang, family, self._stop_event, delay)
        self.reader.on_retry = self.progress.emit

    def run(self):
        try:
            if self.articles is None:
                source = self.reader.category_title(self.source_category, self.source_lang)
                pages = self.reader.pages(self.source_lang, [source], props='info')
                if not self.reader.valid_page(pages.get(source), 14):
                    raise ValueError(message('source_missing', title=source))
                matches = self.reader.article_matches(self.reader.members(source, 'page'))
                self.articles = tuple(dict.fromkeys(matches.values()))
            if not self.category:
                self.result_ready.emit([(title, None) for title in self.articles])
                return
            title = self.reader.category_title(self.category, self.lang)
            pages = self.reader.pages(self.lang, [title], props='info')
            if not self.reader.valid_page(pages.get(title), 14):
                raise ValueError(message('source_missing', title=title))
            members = set(self.reader.members(title, 'page', lang=self.lang))
            self.result_ready.emit([(title, title in members) for title in self.articles])
        except TransferStopped:
            pass
        except Exception as error:
            self._set_failure(error)
            self.progress.emit(str(error))
        finally:
            self.reader.close()


class CategoryInterwikiPreviewWorker(CategoryTransferPreviewWorker):
    def run(self):
        try:
            self.results = self.reader.preview_interwiki(self.rows, self._preview_row)
        except TransferStopped:
            pass
        except Exception as error:
            self._set_failure(error)
            self.progress.emit(str(error))
        finally:
            self.reader.close()


class CategoryInterwikiLinkWorker(CategoryTransferCreateWorker):
    """Link only existing pages; leave distinct items for a conflict report."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.conflicts = []
        self.stats = {"linked": 0, "labels": 0, "already": 0, "conflicts": 0, "failed": 0, "items": 0}

    def run(self):
        try:
            use_wikidata_profile(self.username)
            source_site = pywikibot.Site(self.source_lang, self.family, user=self.username)
            target_site = pywikibot.Site(self.lang, self.family, user=self.username)
            repo = target_site.data_repository()
            if repo is None or source_site.data_repository() != repo:
                raise ValueError(message("no_repository"))
            if source_site.dbName() == target_site.dbName():
                raise ValueError(message('same_wiki'))
            self._login_sites(target_site, repo)
            prepared, sources, targets = [], set(), set()
            for row in self.rows:
                if self._stop:
                    return
                source, target = self._category(source_site, row.source_title), self._category(target_site, row.target_title)
                if source.title() in sources or target.title() in targets:
                    raise ValueError(message("tsv_duplicate", line=len(prepared) + 1))
                sources.add(source.title())
                targets.add(target.title())
                prepared.append((row, source, target))
            for row, source, target in prepared:
                if self._stop:
                    break
                result = dict(source_title=row.source_title, target_title=target.title(), source_qid="", target_qid="")
                try:
                    if not source.exists() or source.isRedirectPage():
                        result['status'] = 'missing_source'
                        raise ValueError(message("source_missing", title=source.title()))
                    if not target.exists():
                        result['status'] = 'target_missing'
                        raise ValueError(message("existing_target_required", title=target.title()))
                    if target.isRedirectPage():
                        result['status'] = 'redirect_target'
                        raise ValueError(message('target_redirect', title=target.title()))
                    source_item, target_item = self._item_for(source), self._item_for(target)
                    source_qid = source_item.getID() if source_item else ""
                    target_qid = target_item.getID() if target_item else ""
                    result.update(source_qid=source_qid, target_qid=target_qid)
                    if source_qid and target_qid and source_qid != target_qid:
                        result.update(status="different_items")
                        self.conflicts.append(dict(result))
                        self.stats["conflicts"] += 1
                    elif source_qid and source_qid == target_qid:
                        result.update(status="already_linked")
                        self.stats["already"] += 1
                        self.linked_categories.append(target.title())
                    else:
                        self._label_changes = {}
                        item = self._resolve_item(source, target, items=(source_item, target_item))
                        qid, action = self._link_categories(source, target, repo, resolved_item=item)
                        result.update(status=action, source_qid=qid, target_qid=qid,
                                      label_changes=dict(self._label_changes), detail=self._label_note())
                        self.stats[{"linked": "linked", "labels_updated": "labels", "already_linked": "already"}[action]] += 1
                        if action != 'labels_updated' and self._label_changes:
                            self.stats['labels'] += 1
                        self.stats["items"] += int(not source_qid and not target_qid)
                        self.linked_categories.append(target.title())
                except CategoryItemConflict as error:
                    # A pair may change after preview or during revalidation.
                    result.update(status="different_items", source_qid=error.source_qid, target_qid=error.target_qid)
                    self.conflicts.append(dict(result))
                    self.stats["conflicts"] += 1
                except TransferStopped:
                    break
                except Exception as error:
                    result.update(status=result.get('status', 'error'), error=str(error))
                    self.stats["failed"] += 1
                    self.progress.emit(message("row_error", title=target.title(), error=error))
                self.category_result.emit(result)
                self.item_processed.emit()
        except Exception as error:
            self._set_failure(error)
            self.progress.emit(str(error))
