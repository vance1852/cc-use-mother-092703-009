"""供应商资格服务向 API 和命令行暴露的稳定错误。"""

from __future__ import annotations


class QualificationError(RuntimeError):
    code = "qualification_error"
    status = 400


class NotFound(QualificationError):
    code = "not_found"
    status = 404


class Conflict(QualificationError):
    code = "conflict"
    status = 409


class Forbidden(QualificationError):
    code = "forbidden"
    status = 403


class InvalidState(QualificationError):
    code = "invalid_state"
    status = 409


class ValidationFailed(QualificationError):
    code = "validation_failed"
    status = 422
