"""Avisos com retorno e com ação — v13.68.

Proatividade sem retorno vira ruído. Cada aviso das categorias abaixo chega com 👍 / 👎:
  👍  "isso ajudou": anotado;
  👎  "não ajudou": no 2º 👎 seguido (em 30 dias) da mesma categoria, o agente oferece silenciá-la por 30 dias.
Lembretes que você pediu, aprovações, atas, saúde e atualizações não têm 👎: são sempre entregues.

/avisos mostra as categorias, o placar e o que está silenciado; /avisos silenciar <categoria> [dias] e
/avisos ligar <categoria> mudam à mão. Um aviso silenciado nem entra na fila (não chega em canal nenhum).

Os avisos também podem trazer botões de AÇÃO (ex.: "✉️ Preparar e-mail de renovação"): no Telegram, no app e no Bit do
PC. A ação só PREPARA (o e-mail continua sendo enviado por você).
"""
from __future__ import annotations

import time
import unicodedata
from typing import Any, Dict, List, Optional

CATEGORIES = {
    "clientes": "Clientes (renovações e sem contato)",
    "rotina": "Rotina (chuva, hora de sair, repetições)",
    "gatilhos": "Gatilhos (compromisso chegando, e-mail novo, tarefa parada)",
    "iniciativa": "Iniciativas (quando eu puxo assunto)",
    "resumos": "Resumos (manhã e semana)",
}
SETTING = "avisos.retorno"
WINDOW_S = 30 * 86400


def fold(text: Any) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", str(text or "").lower()) if not unicodedata.combining(c))


def category_of(trigger_type: str, brain_used: str = "") -> Optional[str]:
    t, b = str(trigger_type or ""), str(brain_used or "")
    if t == "clientes":
        return "clientes"
    if t == "rotina":
        return "rotina"
    if t == "trigger":
        return "gatilhos"
    if t.startswith("resumo") or b == "RESUMO":
        return "resumos"
    if b.startswith("AUTONOMIA"):
        return "iniciativa"
    return None


def find_category(text: str) -> Optional[str]:
    low = fold(text).strip()
    if not low:
        return None
    for key, label in CATEGORIES.items():
        if low in (key, fold(label)) or fold(label).startswith(low) or key.startswith(low[:5]):
            return key
    return None


class Notices:
    def __init__(self, db: Any, clock: Any = time.time):
        self.db, self.clock = db, clock

    def _data(self) -> Dict[str, Any]:
        data = self.db.get_setting(SETTING, {}) or {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("votos", {})
        data.setdefault("silencio", {})
        return data

    def _save(self, data: Dict[str, Any]) -> None:
        cutoff = self.clock() - 120 * 86400
        data["votos"] = {k: [v for v in vs if float(v[0]) > cutoff][-50:] for k, vs in data["votos"].items()}
        self.db.set_setting(SETTING, data)

    def muted_until(self, category: str) -> float:
        return float(self._data()["silencio"].get(category) or 0)

    def is_muted(self, category: Optional[str]) -> bool:
        return bool(category) and self.muted_until(category) > self.clock()  # type: ignore[arg-type]

    def keep(self, trigger_type: str, message: str, brain_used: str) -> bool:
        """Filtro da fila de avisos: False = silenciado (o aviso nem entra)."""
        return not self.is_muted(category_of(trigger_type, brain_used))

    def vote(self, category: str, good: bool) -> Dict[str, Any]:
        """Registra 👍/👎. Devolve {"text", "offer_mute"}."""
        if category not in CATEGORIES:
            return {"text": "Categoria de aviso desconhecida.", "offer_mute": False}
        data = self._data()
        votes = data["votos"].setdefault(category, [])
        votes.append([self.clock(), 1 if good else -1])
        self._save(data)
        if good:
            return {"text": f"👍 Anotado: continuo com os avisos de {CATEGORIES[category].split(' (')[0].lower()}.",
                    "offer_mute": False}
        recent = [v for v in votes if self.clock() - float(v[0]) < WINDOW_S]
        streak = 0
        for _, sign in reversed(recent):
            if sign > 0:
                break
            streak += 1
        if streak >= 2:
            return {"text": f"Entendi: os avisos de {CATEGORIES[category].lower()} não estão ajudando. Quer que eu os silencie "
                            "por 30 dias?", "offer_mute": True}
        return {"text": "👎 Anotado. Se outro aviso desse tipo não ajudar, eu ofereço silenciá-los.", "offer_mute": False}

    def mute(self, category: str, days: int = 30) -> str:
        if category not in CATEGORIES:
            raise ValueError("Categoria desconhecida. Veja /avisos.")
        days = max(1, min(365, int(days)))
        data = self._data()
        data["silencio"][category] = self.clock() + days * 86400
        self._save(data)
        return (f"🔕 Avisos de {CATEGORIES[category].lower()} silenciados por {days} dias. Para voltar: "
                f"/avisos ligar {category}")

    def unmute(self, category: str) -> str:
        if category not in CATEGORIES:
            raise ValueError("Categoria desconhecida. Veja /avisos.")
        data = self._data()
        data["silencio"].pop(category, None)
        self._save(data)
        return f"🔔 Avisos de {CATEGORIES[category].lower()} ligados de novo."

    def status_text(self) -> str:
        data = self._data()
        lines = ["🔔 Avisos (👍/👎 nos últimos 30 dias):"]
        for key, label in CATEGORIES.items():
            votes = [v for v in data["votos"].get(key, []) if self.clock() - float(v[0]) < WINDOW_S]
            ups, downs = sum(1 for v in votes if v[1] > 0), sum(1 for v in votes if v[1] < 0)
            until = float(data["silencio"].get(key) or 0)
            state = (f"🔕 silenciado até {time.strftime('%d/%m', time.localtime(until))}" if until > self.clock()
                     else "ligado")
            lines.append(f"• {key} — {label}: {state} · 👍 {ups} · 👎 {downs}")
        lines.append("\n/avisos silenciar <categoria> [dias] · /avisos ligar <categoria>")
        return "\n".join(lines)

    def command(self, arg: str) -> str:
        words = str(arg or "").split()
        if not words:
            return self.status_text()
        verb = fold(words[0])
        cat = find_category(" ".join(words[1:2])) if len(words) > 1 else None
        if verb.startswith("silenc") or verb in ("desligar", "mudo"):
            if cat is None:
                raise ValueError("Diga a categoria: " + ", ".join(CATEGORIES))
            days = int(words[2]) if len(words) > 2 and words[2].isdigit() else 30
            return self.mute(cat, days)
        if verb in ("ligar", "reativar", "voltar"):
            if cat is None:
                raise ValueError("Diga a categoria: " + ", ".join(CATEGORIES))
            return self.unmute(cat)
        return "Use: /avisos · /avisos silenciar <categoria> [dias] · /avisos ligar <categoria>"


def keyboard(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Botões do aviso: as ações (um por linha) e, nas categorias com retorno, 👍/👎."""
    rows: List[List[Dict[str, str]]] = [[{"text": a["label"], "callback_data": a["data"]}]
                                        for a in item.get("actions") or [] if a.get("data")]
    cat = category_of(str(item.get("trigger_type") or ""), str(item.get("brain_used") or ""))
    if cat:
        rows.append([{"text": "👍 Ajudou", "callback_data": f"fb:+:{cat}"},
                     {"text": "👎 Não ajudou", "callback_data": f"fb:-:{cat}"}])
    return {"inline_keyboard": rows} if rows else None


def install(service: Any) -> Notices:
    notices = Notices(service.db)
    service.notices = notices
    filters = list(getattr(service.db, "proactive_filters", []) or [])
    service.db.proactive_filters = filters + [notices.keep]
    service.extra_actions["notices_command"] = lambda p: {"text": notices.command(str(p.get("arg") or ""))}
    return notices
