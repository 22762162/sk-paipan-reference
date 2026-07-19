"""AIFOS 统一异常类型。"""


class AifosError(Exception):
    """平台级异常基类。"""


class BudgetExceeded(AifosError):
    """成本超出单集预算,AI 导演中心拒绝继续调度。"""


class PermissionDenied(AifosError):
    """系统中心权限校验失败。"""


class ProviderError(AifosError):
    """生产 Provider 执行失败(可由路由器回退到下一个 Provider)。"""


class ProviderUnavailable(ProviderError):
    """Provider 当前不可用(未启用 / 命令缺失 / 额度耗尽 / 未配置凭据)。"""
