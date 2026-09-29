"""供应商资格服务向 API 和 CLI 暴露的稳定错误。"""


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
