"""Rate limiting leve baseado no cache do Django.

Janela fixa por chave, contando apenas FALHAS. Backend padrão (LocMemCache)
basta para uma única instância; trocar por Redis se escalar horizontalmente.
"""
from __future__ import annotations

import os

from django.core.cache import cache

# Quantos proxies confiáveis ficam na frente da app (Railway = 1).
# A app NUNCA deve confiar em entradas do X-Forwarded-For além desse offset,
# pois o cliente pode forjar as entradas mais à esquerda.
TRUSTED_PROXY_COUNT = int(os.environ.get("TRUSTED_PROXY_COUNT", "1"))


def client_ip(request) -> str:
    """IP real do cliente, à prova de spoofing do X-Forwarded-For.

    O cliente pode injetar valores à esquerda do XFF; cada proxy confiável
    *acrescenta* o IP que observou. Logo o IP confiável é o N-ésimo a partir
    da direita, onde N = TRUSTED_PROXY_COUNT. Se o cabeçalho for mais curto
    que o esperado, cai para REMOTE_ADDR (conexão direta).
    """
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff and TRUSTED_PROXY_COUNT > 0:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if len(parts) >= TRUSTED_PROXY_COUNT:
            return parts[-TRUSTED_PROXY_COUNT]
    return request.META.get("REMOTE_ADDR", "") or "unknown"


def _key(scope: str, ident: str) -> str:
    return f"rl:{scope}:{ident}"


def is_rate_limited(scope: str, ident: str, *, limit: int) -> bool:
    """True se já houve `limit` falhas registradas na janela atual (sem contar esta)."""
    return cache.get(_key(scope, ident), 0) >= limit


def register_failure(scope: str, ident: str, *, window: int) -> None:
    """Registra UMA falha. Tentativas bem-sucedidas não devem chamar isto."""
    key = _key(scope, ident)
    if not cache.add(key, 1, timeout=window):
        try:
            cache.incr(key)
        except ValueError:
            # chave expirou entre add e incr — recomeça a janela
            cache.add(key, 1, timeout=window)


def clear_attempts(scope: str, ident: str) -> None:
    """Zera o contador após sucesso, para não penalizar o uso legítimo."""
    cache.delete(_key(scope, ident))
