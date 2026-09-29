"""关键供应商资格服务。

覆盖企业资质与有效范围、获准产品与工厂、质量事件、暂停决定、
紧急替代双批准，以及资质变化对未发货/已验收订单的差异化影响。
"""

from .service import QualificationService

__all__ = ["QualificationService"]
