"""Butler — a filesystem assistant for the Raspberry Pi."""

from .config import Config, SmbShare
from .core import Container
from .engine import Engine, EngineError
from .trash import Trash
from .search import Search
from .organizer import Organizer, Plan, PlanItem
from .decider import Decider, Intent

__all__ = [
    "Config", "SmbShare", "Container", "Engine", "EngineError", "Trash",
    "Search", "Organizer", "Plan", "PlanItem", "Decider", "Intent",
]

__version__ = "1.0.0"
PRODUCT_NAME = "Pi Butler"
