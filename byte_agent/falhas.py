"""Falhas que antes sumiam em silêncio — v13.57.

Vários pontos do Byte engolem erros de propósito (um gancho, a busca por significado ou o registro da conversa falhar
não pode derrubar o chat). O problema é que ninguém ficava sabendo: o servidor antigo jogava fora o formato dos modelos
de e-mail sem avisar, por exemplo. Agora esses pontos chamam note(onde, erro). O /saude mostra as falhas das últimas
24 h, agrupadas por lugar, com a mais recente de cada.

Só a classe do erro e um trecho curto da mensagem são guardados, sem dados do usuário.
"""
from __future__ import annotations

import re
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

_LOCK = threading.Lock()
_ITEMS: Deque[Tuple[float, str, str]] = deque(maxlen=200)
WINDOW_S = 24 * 3600


def note(where: str, err: BaseException, now: Optional[float] = None) -> None:
    """Registra a falha (nunca levanta exceção)."""
    try:
        msg = re.sub(r"\s+", " ", str(err))[:140]
        text = f"{type(err).__name__}: {msg}" if msg else type(err).__name__
        with _LOCK:
            _ITEMS.append((time.time() if now is None else now, str(where)[:60], text))
    except Exception:
        pass


def recent(now: Optional[float] = None, window_s: float = WINDOW_S) -> List[Dict[str, Any]]:
    """[{onde, vezes, ultima, erro}] das últimas 24 h, do lugar com mais falhas para o com menos."""
    now = time.time() if now is None else now
    groups: Dict[str, Dict[str, Any]] = {}
    with _LOCK:
        items = list(_ITEMS)
    for at, where, text in items:
        if now - at > window_s:
            continue
        g = groups.setdefault(where, {"onde": where, "vezes": 0, "ultima": 0.0, "erro": ""})
        g["vezes"] += 1
        if at >= g["ultima"]:
            g["ultima"], g["erro"] = at, text
    return sorted(groups.values(), key=lambda g: (-g["vezes"], -g["ultima"]))


def clear() -> None:
    with _LOCK:
        _ITEMS.clear()
