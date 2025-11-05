"""
Диалоговые окна для Wiki Category Tool

Содержит диалоги:
- DebugDialog - окно отладки
- TemplateReviewDialog - диалог проверки шаблонов
- UpdateDialog - диалог уведомления о новой версии
"""

from .debug_dialog import DebugDialog
from .template_review_dialog import TemplateReviewDialog, show_template_review_dialog
from .update_dialog import UpdateDialog

__all__ = [
    'DebugDialog',
    'TemplateReviewDialog', 'show_template_review_dialog',
    'UpdateDialog'
]