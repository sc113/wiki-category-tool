"""
Диалоговые окна для Wiki Category Tool

Содержит диалоги:
- DebugDialog - окно отладки
- TemplateReviewDialog - диалог проверки шаблонов
"""

from .debug_dialog import DebugDialog
from .template_review_dialog import TemplateReviewDialog, show_template_review_dialog

__all__ = [
    'DebugDialog',
    'TemplateReviewDialog', 'show_template_review_dialog'
]