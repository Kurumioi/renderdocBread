"""AI connector factories.

Connector modules keep optional SDK imports lazy so profile management and the
connection wizard remain usable without installing every provider extra.
"""

from .models import build_model

__all__ = ["build_model"]
