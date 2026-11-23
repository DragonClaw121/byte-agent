"""Agenda unificada — v13.38 (R2.2).

Os módulos que olham a agenda (resumo da manhã, gatilho "compromisso chegando", rotina "hora de sair" e horários livres)
liam só o endereço iCal (BYTE_AGENDA_ICS). Agora também somam os compromissos das contas conectadas — Microsoft
(Outlook/Microsoft 365) e, depois, Google — registradas em service.calendar_providers. Compromissos repetidos (o mesmo
evento vindo do iCal e da conta) aparecem uma vez só.

Cada provedor: função (início, fim) -> lista de {"when": datetime com fuso, "end": datetime|None, "all_day": bool,
"title": str, "location": str, "uid": str, "source": "outlook"|"google"|..., "online_url": str}.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List


def provider_events(service: Any, start: datetime, end: datetime) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    errors = []
    for provider in list(getattr(service, "calendar_providers", []) or []):
        try:
            out.extend(provider(start, end))
        except Exception as err:                      # uma conta fora do ar não apaga a agenda das outras
            errors.append(f"{type(err).__name__}: {str(err)[:120]}")
    service.calendar_errors = errors
    return out


def has_providers(service: Any) -> bool:
    """Há alguma conta de agenda CONECTADA? (provedor sem conta não conta; ele pode ter `active()`.)"""
    for provider in getattr(service, "calendar_providers", None) or []:
        try:
            if getattr(provider, "active", lambda: True)():
                return True
        except Exception:
            continue
    return False


def _key(ev: Dict[str, Any]) -> tuple:
    title = re.sub(r"\W+", " ", str(ev.get("title") or "").lower()).strip()
    return title, ev["when"].strftime("%Y-%m-%d") if ev.get("all_day") else ev["when"].astimezone().strftime("%Y-%m-%d %H:%M")


def merge(*lists: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen, out = set(), []
    for items in lists:
        for ev in items:
            k = _key(ev)
            if k in seen:
                continue
            seen.add(k)
            out.append(ev)
    out.sort(key=lambda e: (e["when"], not e.get("all_day")))
    return out
