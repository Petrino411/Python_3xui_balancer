"""xray-client-balancer — sticky-распределение клиентов 3x-ui по балансировщикам Xray.

Уровень 1 (управляет этот сервис): client -> sticky assignment -> client-balancer-N
Уровень 2 (управляет сам Xray): client-balancer-N -> primary, при падении -> fallback
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
