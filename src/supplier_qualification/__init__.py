"""关键供应商资格服务。"""

from .errors import (
    Conflict,
    Forbidden,
    InvalidState,
    NotFound,
    QualificationError,
    ValidationFailed,
)
from .rules import DeliverySpec, evaluate_applicability
from .service import QualificationService

__all__ = [
    "Conflict",
    "DeliverySpec",
    "Forbidden",
    "InvalidState",
    "NotFound",
    "QualificationError",
    "QualificationService",
    "ValidationFailed",
    "evaluate_applicability",
]

__version__ = "0.1.0"
