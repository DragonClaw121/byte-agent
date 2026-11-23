"""Bit no PC (Windows) — app de mesa do Bit, etapa 1 (v13.47).

O que faz:
  - conversa com o Bit (a mesma conversa e memória do Telegram), com os botões de aprovação;
  - "📸 Olhar minha tela": manda UMA captura da janela em uso e o Bit responde sobre ela;
  - "🔴 Ajuda ao vivo": acompanha a tela enquanto você trabalha (como o OBS, mas só manda um quadro quando a tela
    muda) e orienta passo a passo; marca na tela onde clicar; orientação em texto e/ou voz, você escolhe;
  - avisos do Bit (lembretes, resumos...) como notificação no canto da tela;
  - atalhos: Ctrl+Alt+B abre o Bit, Ctrl+Alt+L liga/desliga a ajuda ao vivo.
Privacidade: as janelas do próprio Bit não entram nas capturas; janelas com senha, banco ou navegação anônima são
puladas; nada da tela é gravado em disco; a ajuda ao vivo mostra um aviso vermelho e para sozinha sem atividade.

Conectar: no Telegram, /pc → cole o link (https://.../pc/bit#123456) no app.
Rodar sem instalar: python bit_pc.py   (precisa de: pip install mss pillow)
"""
from __future__ import annotations

import base64
import io
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

APP_NAME = "Bit no PC"
VERSION = "1.0"
IS_WINDOWS = sys.platform.startswith("win")
BLOCKED_WORDS = ("senha", "password", "1password", "bitwarden", "keepass", "lastpass", "inprivate", "incognito",
                 "anônima", "anonima", "private browsing", "navegação privada", "internet banking", "banco do brasil",
                 "itaú", "itau", "bradesco", "santander", "caixa econômica", "nubank", "banco inter", "token de segurança")
LIVE_CHECK_S = 1.0             # olha a tela a cada 1 s (no PC; não manda nada se não mudou)
LIVE_MIN_SEND_S = 2.5          # manda no máximo 1 quadro a cada 2,5 s
LIVE_IDLE_STOP_S = 180         # para sozinho depois de 3 min sem mudança na tela
CHANGE_THRESHOLD = 4.0         # diferença média (0-255) na miniatura para contar como "mudou"


# ================================================================== configuração (token protegido pelo Windows)
def config_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    path = os.path.join(base, "BitPC")
    os.makedirs(path, exist_ok=True)
    return path


def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    buf = ctypes.create_string_buffer(data, len(data))
    inp = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = Blob()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("DPAPI falhou")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def protect(text: str) -> str:
    """No Windows, o código do PC fica cifrado com a conta do usuário (DPAPI): copiar o arquivo para outro PC não serve."""
    if IS_WINDOWS:
        try:
            return "dpapi:" + base64.b64encode(_dpapi(text.encode("utf-8"), True)).decode()
        except Exception:
            pass
    return "plain:" + text


def unprotect(value: str) -> str:
    if value.startswith("dpapi:"):
        try:
            return _dpapi(base64.b64decode(value[6:]), False).decode("utf-8")
        except Exception:
            return ""
    return value[6:] if value.startswith("plain:") else ""


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    path = path or os.path.join(config_dir(), "config.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(cfg: Dict[str, Any], path: Optional[str] = None) -> None:
    path = path or os.path.join(config_dir(), "config.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def parse_link(raw: str) -> Optional[Tuple[str, str]]:
    """"https://servidor/pc/bit#123456" -> ("https://servidor/pc/bit", "123456")."""
    text = (raw or "").strip()
    i = text.find("https://")
    if i < 0:
        return None
    text = text[i:].split()[0]
    if "#" not in text:
        return None
    base, code = text.split("#", 1)
    base = base.rstrip("/")
    code = code.strip()
    if "/pc/" not in base or not code.isdigit() or len(code) != 6:
        return None
    return base, code


# ================================================================== servidor
class ApiError(RuntimeError):
    pass


class Api:
    def __init__(self, base: str, token: str = ""):
        self.base, self.token = base.rstrip("/"), token

    def call(self, method: str, sub: str, body: Optional[Dict[str, Any]] = None, timeout: float = 60.0) -> Dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(f"{self.base}/{sub}", data=data, method=method,
                                     headers={"Content-Type": "application/json", "User-Agent": f"BitPC/{VERSION}",
                                              **({"Authorization": f"Bearer {self.token}"} if self.token else {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                out = json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as err:
            try:
                out = json.loads(err.read().decode("utf-8") or "{}")
            except ValueError:
                out = {}
            raise ApiError(out.get("error") or f"O servidor recusou (HTTP {err.code}).") from None
        except (urllib.error.URLError, OSError):
            raise ApiError("Sem conexão com o Bit agora (internet ou servidor fora do ar).") from None
        if isinstance(out, dict) and out.get("error"):
            raise ApiError(str(out["error"]))
        return out if isinstance(out, dict) else {}


# ================================================================== tela
def dpi_aware() -> None:
    if not IS_WINDOWS:
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)        # coordenadas reais, iguais às da captura
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def window_info(hwnd: int) -> Optional[Dict[str, Any]]:
    """Título, processo e retângulo (sem a borda invisível do Windows 10/11) de uma janela."""
    if not IS_WINDOWS or not hwnd:
        return None
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    rect = wintypes.RECT()
    try:
        ok = ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect)) == 0
    except Exception:
        ok = False
    if not ok:
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return {"hwnd": hwnd, "title": buf.value, "pid": pid.value,
            "rect": (rect.left, rect.top, rect.right, rect.bottom), "minimized": bool(user32.IsIconic(hwnd))}


def foreground() -> Optional[Dict[str, Any]]:
    if not IS_WINDOWS:
        return None
    import ctypes
    return window_info(ctypes.windll.user32.GetForegroundWindow())


def blocked_title(title: str) -> bool:
    low = (title or "").lower()
    return any(w in low for w in BLOCKED_WORDS)


def clip_region(rect: Tuple[int, int, int, int], screen: Dict[str, int]) -> Optional[Dict[str, int]]:
    left, top = max(rect[0], screen["left"]), max(rect[1], screen["top"])
    right = min(rect[2], screen["left"] + screen["width"])
    bottom = min(rect[3], screen["top"] + screen["height"])
    if right - left < 80 or bottom - top < 60:
        return None
    return {"left": left, "top": top, "width": right - left, "height": bottom - top}


def capture(region: Optional[Dict[str, int]] = None) -> Tuple[Any, Dict[str, int]]:
    """Captura uma região da tela (ou o monitor principal). Devolve (imagem PIL, região)."""
    import mss
    from PIL import Image
    with mss.mss() as sct:
        area = region or dict(sct.monitors[1])
        shot = sct.grab({k: area[k] for k in ("left", "top", "width", "height")})
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    return img, {k: int(area[k]) for k in ("left", "top", "width", "height")}


def virtual_screen() -> Dict[str, int]:
    import mss
    with mss.mss() as sct:
        return {k: int(sct.monitors[0][k]) for k in ("left", "top", "width", "height")}


def encode(img: Any, max_width: int = 1280, quality: int = 60) -> str:
    """JPEG reduzido em base64 (~100-250 KB): o suficiente para ler botões e menus."""
    from PIL import Image
    im = img
    if im.width > max_width:
        im = im.resize((max_width, int(im.height * max_width / im.width)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def signature(img: Any) -> List[int]:
    """Miniatura 64x36 em tons de cinza: compara telas sem guardar a imagem."""
    return list(img.convert("L").resize((64, 36)).getdata())


def difference(a: Optional[List[int]], b: Optional[List[int]]) -> float:
    if not a or not b or len(a) != len(b):
        return 255.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def to_screen(box: List[float], region: Dict[str, int]) -> Tuple[int, int, int, int]:
    """Região do alvo (frações 0..1 da imagem enviada) -> pixels da tela."""
    x0, y0, x1, y1 = box
    return (int(region["left"] + x0 * region["width"]), int(region["top"] + y0 * region["height"]),
            int(region["left"] + x1 * region["width"]), int(region["top"] + y1 * region["height"]))


# ================================================================== voz (opcional)
class Voice:
    """Voz do próprio Windows (System.Speech). Uma fala por vez: a nova interrompe a anterior."""

    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None

    def say(self, text: str) -> None:
        if not IS_WINDOWS or not text.strip():
            return
        self.stop()
        script = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                  "$v = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like 'pt*' } | Select-Object -First 1; "
                  "if ($v) { $s.SelectVoice($v.VoiceInfo.Name) }; $s.Speak([Console]::In.ReadToEnd())")
        try:
            self.proc = subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                         stdin=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.proc.stdin.write(text[:600].encode("utf-8"))
            self.proc.stdin.close()
        except Exception:
            self.proc = None

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None


# ================================================================== atalhos globais (Windows)
def hotkeys(events: "queue.Queue[Tuple[str, Any]]") -> None:
    """Ctrl+Alt+B (abrir) e Ctrl+Alt+L (ajuda ao vivo), mesmo com o Bit minimizado."""
    if not IS_WINDOWS:
        return
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    mods = 0x0001 | 0x0002 | 0x4000                    # Alt + Ctrl + sem repetição
    keys = {1: (ord("B"), "abrir"), 2: (ord("L"), "ao_vivo")}
    for hk_id, (vk, _) in keys.items():
        if not user32.RegisterHotKey(None, hk_id, mods, vk):
            events.put(("aviso", "Outro programa já usa um dos atalhos do Bit (Ctrl+Alt+B / Ctrl+Alt+L)."))
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        if msg.message == 0x0312 and msg.wParam in keys:        # WM_HOTKEY
            events.put((keys[msg.wParam][1], None))


def exclude_from_capture(widget: Any) -> None:
    """As janelas do Bit não aparecem nas capturas (Windows 10 2004+). Assim o Bit nunca vê a si mesmo."""
    if not IS_WINDOWS:
        return
    import ctypes
    try:
        hwnd = ctypes.windll.user32.GetParent(widget.winfo_id()) or widget.winfo_id()
        ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, 0x11)       # WDA_EXCLUDEFROMCAPTURE
    except Exception:
        pass


def click_through(widget: Any) -> None:
    """A marcação vermelha não atrapalha: o clique passa para o programa embaixo."""
    if not IS_WINDOWS:
        return
    import ctypes
    try:
        hwnd = ctypes.windll.user32.GetParent(widget.winfo_id()) or widget.winfo_id()
        style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
        ctypes.windll.user32.SetWindowLongW(hwnd, -20, style | 0x00080000 | 0x00000020)   # LAYERED | TRANSPARENT
    except Exception:
        pass


# ================================================================== o app
class App:
    BG, CARD, INK, MUTED, PRI, RED = "#12151c", "#1b1f29", "#e7e9ef", "#9aa3b5", "#6d8cff", "#ff5a52"

    def __init__(self, api_factory: Callable[[str, str], Any] = Api, config_path: Optional[str] = None,
                 test_mode: bool = False):
        import tkinter as tk
        self.tk = tk
        self.api_factory, self.config_path, self.test_mode = api_factory, config_path, test_mode
        self.cfg = load_config(config_path)
        self.events: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self.voice = Voice()
        self.api: Optional[Any] = None
        self.last_foreign: Optional[Dict[str, Any]] = None      # a última janela que NÃO é do Bit (o que você está fazendo)
        self.live: Optional[Dict[str, Any]] = None
        self.overlay = self.guide = self.badge = None
        self.my_pid = os.getpid()
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.configure(bg=self.BG)
        self.root.geometry("440x660")
        self.root.minsize(360, 480)
        try:
            self.root.tk.call("tk", "scaling", self.root.winfo_fpixels("1i") / 72.0)
        except Exception:
            pass
        self.speak_var = tk.BooleanVar(value=bool(self.cfg.get("voz", False)))
        self.text_var = tk.BooleanVar(value=bool(self.cfg.get("texto", True)))
        self.frame: Optional[Any] = None
        token = unprotect(str(self.cfg.get("token") or ""))
        if self.cfg.get("base") and token:
            self.api = self.api_factory(self.cfg["base"], token)
            self.show_main()
        else:
            self.show_pairing()
        self.root.after(150, self.pump)
        self.root.after(400, lambda: exclude_from_capture(self.root))
        if not test_mode:
            threading.Thread(target=hotkeys, args=(self.events,), daemon=True, name="atalhos").start()
            threading.Thread(target=self.track_foreground, daemon=True, name="janela-em-uso").start()
            threading.Thread(target=self.poll_notices, daemon=True, name="avisos").start()

    # ------------------------------------------------------------ utilidades de interface
    def label(self, parent: Any, text: str, size: int = 10, color: Optional[str] = None, bold: bool = False, **kw: Any) -> Any:
        return self.tk.Label(parent, text=text, bg=kw.pop("bg", self.BG), fg=color or self.INK, justify="left",
                             font=("Segoe UI", size, "bold" if bold else "normal"), **kw)

    def button(self, parent: Any, text: str, cmd: Callable[[], None], primary: bool = False) -> Any:
        return self.tk.Button(parent, text=text, command=cmd, relief="flat", cursor="hand2", padx=10, pady=5,
                              bg=self.PRI if primary else self.CARD, fg="#0d1020" if primary else self.INK,
                              activebackground=self.PRI, font=("Segoe UI", 10))

    def clear(self) -> None:
        if self.frame is not None:
            self.frame.destroy()
        self.frame = self.tk.Frame(self.root, bg=self.BG)
        self.frame.pack(fill="both", expand=True, padx=14, pady=12)

    def run_bg(self, work: Callable[[], Any], done: Callable[[Any], None], fail: Optional[Callable[[str], None]] = None) -> None:
        def target() -> None:
            try:
                res = work()
                self.events.put(("call", lambda: done(res)))
            except Exception as err:
                msg = str(err) if isinstance(err, ApiError) else f"{type(err).__name__}: {err}"
                self.events.put(("call", lambda: (fail or self.error)(msg)))
        threading.Thread(target=target, daemon=True).start()

    def pump(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "call":
                    payload()
                elif kind == "abrir":
                    self.bring_up()
                elif kind == "ao_vivo":
                    self.toggle_live()
                elif kind == "aviso":
                    self.notice(str(payload))
        except queue.Empty:
            pass
        self.root.after(150, self.pump)

    def bring_up(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(300, lambda: self.root.attributes("-topmost", False))
        if getattr(self, "entry", None) is not None:
            self.entry.focus_force()

    def error(self, msg: str) -> None:
        if getattr(self, "chat", None) is not None:
            self.add_message("⚠️ " + msg, "aviso")
        elif getattr(self, "pair_status", None) is not None:
            self.pair_status.configure(text=msg, fg=self.RED)

    # ------------------------------------------------------------ conectar
    def show_pairing(self) -> None:
        self.chat = None
        self.entry = None
        self.clear()
        f = self.frame
        self.label(f, "Bit no PC", 18, bold=True).pack(anchor="w")
        self.label(f, "1. No Telegram, mande /pc para o Bit.\n2. Copie o link que ele responder (https://...#123456)\n"
                      "3. Cole aqui e clique em Conectar.", 10, self.MUTED).pack(anchor="w", pady=(8, 10))
        self.link_entry = self.tk.Entry(f, bg=self.CARD, fg=self.INK, insertbackground=self.INK, relief="flat",
                                        font=("Segoe UI", 10))
        self.link_entry.pack(fill="x", ipady=6)
        self.label(f, "Nome deste computador:", 9, self.MUTED).pack(anchor="w", pady=(10, 2))
        self.name_entry = self.tk.Entry(f, bg=self.CARD, fg=self.INK, insertbackground=self.INK, relief="flat",
                                        font=("Segoe UI", 10))
        self.name_entry.insert(0, os.environ.get("COMPUTERNAME", "Meu PC")[:40])
        self.name_entry.pack(fill="x", ipady=6)
        self.button(f, "Conectar", self.pair, primary=True).pack(anchor="w", pady=12)
        self.pair_status = self.label(f, "", 10, self.MUTED, wraplength=380)
        self.pair_status.pack(anchor="w")

    def pair(self) -> None:
        parsed = parse_link(self.link_entry.get())
        if not parsed:
            self.pair_status.configure(text="Esse não parece o link do /pc (precisa ter https://, /pc/ e #123456).", fg=self.RED)
            return
        base, code = parsed
        name = self.name_entry.get().strip() or "Meu PC"
        self.pair_status.configure(text="Conectando…", fg=self.MUTED)

        def done(res: Dict[str, Any]) -> None:
            self.cfg.update({"base": base, "token": protect(res["token"]), "agente": res.get("agente", "Bit")})
            save_config(self.cfg, self.config_path)
            self.api = self.api_factory(base, res["token"])
            self.show_main()
            self.add_message(f"✅ Conectado ao {res.get('agente', 'Bit')}. Atalhos: Ctrl+Alt+B abre o Bit · "
                             "Ctrl+Alt+L liga a ajuda ao vivo.", "aviso")
        self.run_bg(lambda: self.api_factory(base, "").call("POST", "parear", {"codigo": code, "nome": name}), done,
                    lambda m: self.pair_status.configure(text=m, fg=self.RED))

    def disconnect(self) -> None:
        self.stop_live(save=False)
        self.cfg.pop("token", None)
        save_config(self.cfg, self.config_path)
        self.api = None
        self.show_pairing()

    # ------------------------------------------------------------ conversa
    def show_main(self) -> None:
        tk = self.tk
        self.clear()
        f = self.frame
        top = tk.Frame(f, bg=self.BG)
        top.pack(fill="x")
        self.label(top, self.cfg.get("agente") or "Bit", 14, bold=True).pack(side="left")
        self.button(top, "Desconectar", self.disconnect).pack(side="right")
        box = tk.Frame(f, bg=self.CARD)
        box.pack(fill="both", expand=True, pady=8)
        self.chat = tk.Text(box, bg=self.CARD, fg=self.INK, wrap="word", relief="flat", font=("Segoe UI", 10),
                            padx=8, pady=8, state="disabled", cursor="arrow")
        bar = tk.Scrollbar(box, command=self.chat.yview)
        self.chat.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.chat.pack(side="left", fill="both", expand=True)
        self.chat.tag_configure("eu", foreground=self.PRI, spacing1=6)
        self.chat.tag_configure("bit", foreground=self.INK, spacing1=6)
        self.chat.tag_configure("aviso", foreground=self.MUTED, spacing1=6)
        row = tk.Frame(f, bg=self.BG)
        row.pack(fill="x")
        self.entry = tk.Entry(row, bg=self.CARD, fg=self.INK, insertbackground=self.INK, relief="flat", font=("Segoe UI", 10))
        self.entry.pack(side="left", fill="x", expand=True, ipady=6)
        self.entry.bind("<Return>", lambda e: self.send())
        self.button(row, "Enviar", self.send, primary=True).pack(side="left", padx=(6, 0))
        tools = tk.Frame(f, bg=self.BG)
        tools.pack(fill="x", pady=(8, 0))
        self.button(tools, "📸 Olhar minha tela", self.look).pack(side="left")
        self.live_btn = self.button(tools, "🔴 Ajuda ao vivo", self.toggle_live)
        self.live_btn.pack(side="left", padx=6)
        opts = tk.Frame(f, bg=self.BG)
        opts.pack(fill="x", pady=(6, 0))
        for text, var in (("Orientação em texto", self.text_var), ("Orientação por voz", self.speak_var)):
            tk.Checkbutton(opts, text=text, variable=var, command=self.save_options, bg=self.BG, fg=self.MUTED,
                           selectcolor=self.CARD, activebackground=self.BG, font=("Segoe UI", 9)).pack(side="left")
        self.label(f, "Ctrl+Alt+B abre o Bit · Ctrl+Alt+L liga/desliga a ajuda ao vivo", 8, self.MUTED).pack(anchor="w", pady=(4, 0))

    def save_options(self) -> None:
        self.cfg["voz"], self.cfg["texto"] = bool(self.speak_var.get()), bool(self.text_var.get())
        save_config(self.cfg, self.config_path)
        if not self.speak_var.get():
            self.voice.stop()

    def add_message(self, text: str, who: str = "bit", buttons: Optional[List[Dict[str, str]]] = None) -> None:
        if getattr(self, "chat", None) is None:
            return
        tk = self.tk
        self.chat.configure(state="normal")
        prefix = {"eu": "Você: ", "bit": f"{self.cfg.get('agente') or 'Bit'}: ", "aviso": ""}.get(who, "")
        self.chat.insert("end", prefix + text.strip() + "\n", who)
        for b in buttons or []:
            btn = tk.Button(self.chat, text=b.get("label", "?"), relief="flat", bg=self.PRI, fg="#0d1020", cursor="hand2",
                            font=("Segoe UI", 9))
            btn.configure(command=lambda data=b.get("data", ""), label=b.get("label", ""), w=btn: self.press(data, label, w))
            self.chat.window_create("end", window=btn, padx=3, pady=3)
        if buttons:
            self.chat.insert("end", "\n")
        self.chat.configure(state="disabled")
        self.chat.see("end")

    def show_reply(self, res: Dict[str, Any]) -> None:
        for m in res.get("mensagens") or []:
            self.add_message(str(m.get("text") or ""), "bit", m.get("buttons") or [])

    def send(self) -> None:
        text = self.entry.get().strip()
        if not text or self.api is None:
            return
        self.entry.delete(0, "end")
        if self.live is not None:                               # durante a ajuda ao vivo, a pergunta vai com a tela
            self.live["pergunta"] = text
            self.add_message(text, "eu")
            return
        self.add_message(text, "eu")
        self.run_bg(lambda: self.api.call("POST", "conversa", {"texto": text}, timeout=180), self.show_reply)

    def press(self, data: str, label: str, widget: Any) -> None:
        widget.configure(state="disabled")
        self.add_message(label, "eu")
        self.run_bg(lambda: self.api.call("POST", "conversa", {"botao": data}, timeout=120), self.show_reply)

    # ------------------------------------------------------------ olhar a tela
    def track_foreground(self) -> None:
        while True:
            try:
                info = foreground()
                if info and info["pid"] != self.my_pid and not info["minimized"]:
                    self.last_foreign = info
            except Exception:
                pass
            time.sleep(0.5)

    def target_region(self) -> Tuple[Optional[Dict[str, int]], str]:
        """A janela em uso (a última que não é do Bit). Sem ela, o monitor principal."""
        info = self.last_foreign
        if info:
            if blocked_title(info["title"]):
                return None, info["title"]
            try:
                region = clip_region(info["rect"], virtual_screen())
            except Exception:
                region = None
            if region:
                return region, info["title"]
        return {}, ""

    def grab(self) -> Tuple[Optional[str], Optional[Dict[str, int]], Optional[List[int]], str]:
        region, title = self.target_region()
        if region is None:
            return None, None, None, title
        img, used = capture(region or None)
        return encode(img), used, signature(img), title

    def look(self) -> None:
        if self.api is None:
            return
        question = self.entry.get().strip()
        self.entry.delete(0, "end")
        image, _, _, title = self.grab()
        if image is None:
            self.add_message("🙈 A janela em uso parece privada (senha, banco ou navegação anônima): não vou olhar.", "aviso")
            return
        self.add_message(("📸 " + question) if question else "📸 Olhe minha tela" + (f" ({title[:60]})" if title else ""), "eu")
        self.run_bg(lambda: self.api.call("POST", "conversa", {"texto": question, "tela": image}, timeout=120), self.show_reply)

    # ------------------------------------------------------------ ajuda ao vivo
    def toggle_live(self) -> None:
        if self.live is not None:
            self.stop_live(save=None)
        else:
            self.start_live()

    def start_live(self, goal: Optional[str] = None) -> None:
        if self.api is None:
            self.bring_up()
            return
        if goal is None:
            goal = self.entry.get().strip()
            if goal:
                self.entry.delete(0, "end")
            else:
                from tkinter import simpledialog
                goal = simpledialog.askstring(APP_NAME, "Com o que você quer ajuda? (ex.: montar uma tabela dinâmica)",
                                              parent=self.root) or ""
        self.live = {"sessao": uuid.uuid4().hex[:16], "objetivo": goal.strip(), "sig": None, "sent": 0.0,
                     "changed": time.time(), "pergunta": "", "busy": False, "last_step": ""}
        self.add_message(f"🔴 Ajuda ao vivo ligada{': ' + goal if goal else ''}. Trabalhe normalmente; eu acompanho e "
                         "oriento. Ctrl+Alt+L ou o aviso vermelho para parar.", "aviso")
        self.live_btn.configure(text="⏹ Parar ajuda ao vivo", bg=self.RED)
        self.show_badge()
        if not self.test_mode:
            threading.Thread(target=self.live_loop, args=(self.live,), daemon=True, name="ao-vivo").start()

    def live_loop(self, live: Dict[str, Any]) -> None:
        while self.live is live:
            time.sleep(LIVE_CHECK_S)
            if live["busy"]:
                continue
            try:
                region, title = self.target_region()
                if region is None:
                    self.events.put(("call", lambda: self.set_badge("⏸ Janela privada: não estou olhando")))
                    continue
                img, used = capture(region or None)
                sig = signature(img)
                now = time.time()
                moved = difference(sig, live["sig"]) >= CHANGE_THRESHOLD
                if moved:
                    live["changed"] = now
                if now - live["changed"] > LIVE_IDLE_STOP_S and not live["pergunta"]:
                    self.events.put(("call", lambda: self.stop_live(save=None, reason="Parei: 3 minutos sem mudança na tela.")))
                    return
                if not (moved or live["pergunta"] or live["sig"] is None) or now - live["sent"] < LIVE_MIN_SEND_S:
                    continue
                live["busy"], live["sent"], live["sig"] = True, now, sig
                question, live["pergunta"] = live["pergunta"], ""
                body = {"sessao": live["sessao"], "objetivo": live["objetivo"], "tela": encode(img), "pergunta": question}
                self.events.put(("call", lambda t=title: self.set_badge("● Bit vendo a tela" + (f" — {t[:40]}" if t else ""))))
                res = self.api.call("POST", "ao-vivo", body, timeout=60)
                self.events.put(("call", lambda r=res, u=used: self.show_step(r, u)))
            except Exception as err:
                msg = str(err)
                self.events.put(("call", lambda m=msg: self.set_badge("⚠️ " + m[:70])))
                time.sleep(5)
            finally:
                live["busy"] = False

    def show_step(self, step: Dict[str, Any], region: Dict[str, int]) -> None:
        if self.live is None or step.get("espere"):
            return
        text = str(step.get("passo") or "").strip()
        if step.get("novo", True) and text and text != self.live.get("last_step"):
            self.live["last_step"] = text
            self.add_message("👉 " + text, "bit")
            if self.text_var.get():
                self.show_guide(text)
            if self.speak_var.get():
                self.voice.say(str(step.get("fala") or text))
        if step.get("alvo"):
            self.show_overlay(to_screen(step["alvo"], region))
        if step.get("concluido"):
            self.stop_live(save=None, reason="✅ Parece concluído!")

    def stop_live(self, save: Optional[bool], reason: str = "") -> None:
        live, self.live = self.live, None
        for name in ("overlay", "guide", "badge"):
            win = getattr(self, name)
            if win is not None:
                try:
                    win.destroy()
                except Exception:
                    pass
                setattr(self, name, None)
        self.voice.stop()
        if getattr(self, "live_btn", None) is not None:
            self.live_btn.configure(text="🔴 Ajuda ao vivo", bg=self.CARD)
        if live is None or self.api is None:
            return
        if reason:
            self.add_message(reason, "aviso")
        if save is None:
            from tkinter import messagebox
            save = bool(live.get("last_step")) and messagebox.askyesno(
                APP_NAME, "Quer que eu guarde esse passo a passo como um processo na minha memória?", parent=self.root)
        self.run_bg(lambda: self.api.call("POST", "ao-vivo/fim", {"sessao": live["sessao"], "guardar": bool(save)}),
                    lambda r: self.add_message(str(r.get("texto") or "Ajuda ao vivo encerrada."), "aviso"))

    # ------------------------------------------------------------ janelinhas sobre a tela
    def topmost_window(self, x: int, y: int, w: int, h: int, bg: str) -> Any:
        win = self.tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=bg)
        win.geometry(f"{w}x{h}+{x}+{y}")
        win.after(100, lambda: exclude_from_capture(win))
        return win

    def show_badge(self) -> None:
        sw = self.root.winfo_screenwidth()
        self.badge = self.topmost_window(sw // 2 - 190, 6, 380, 30, self.RED)
        self.badge_label = self.tk.Label(self.badge, text="● Bit vendo a tela — clique para parar", bg=self.RED, fg="white",
                                         font=("Segoe UI", 10, "bold"), cursor="hand2")
        self.badge_label.pack(fill="both", expand=True)
        self.badge_label.bind("<Button-1>", lambda e: self.stop_live(save=None))

    def set_badge(self, text: str) -> None:
        if self.badge is not None:
            self.badge_label.configure(text=text + " · clique para parar")

    def show_guide(self, text: str) -> None:
        if self.guide is not None:
            self.guide.destroy()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.guide = self.topmost_window(sw - 440, sh - 190, 420, 130, self.CARD)
        lbl = self.tk.Label(self.guide, text="👉 " + text, bg=self.CARD, fg=self.INK, wraplength=400, justify="left",
                            font=("Segoe UI", 11), padx=10, pady=8)
        lbl.pack(fill="both", expand=True)
        click_through(self.guide)

    def show_overlay(self, rect: Tuple[int, int, int, int]) -> None:
        if self.overlay is not None:
            self.overlay.destroy()
        pad = 6
        x0, y0, x1, y1 = rect[0] - pad, rect[1] - pad, rect[2] + pad, rect[3] + pad
        w, h = max(20, x1 - x0), max(20, y1 - y0)
        self.overlay = self.topmost_window(x0, y0, w, h, "#ff00ff")
        try:
            self.overlay.attributes("-transparentcolor", "#ff00ff")
        except Exception:
            pass
        canvas = self.tk.Canvas(self.overlay, width=w, height=h, bg="#ff00ff", highlightthickness=0)
        canvas.pack()
        canvas.create_rectangle(2, 2, w - 2, h - 2, outline=self.RED, width=4)
        click_through(self.overlay)
        ov = self.overlay
        self.root.after(8000, lambda: (ov.destroy() if self.overlay is ov else None))

    # ------------------------------------------------------------ avisos do Bit
    def poll_notices(self) -> None:
        while True:
            time.sleep(60)
            if self.api is None:
                continue
            try:
                for it in self.api.call("GET", "avisos", timeout=30).get("avisos") or []:
                    self.events.put(("aviso", str(it.get("text") or "")))
            except Exception:
                pass

    def notice(self, text: str) -> None:
        if not text.strip():
            return
        self.add_message("🔔 " + text, "aviso")
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        toast = self.topmost_window(sw - 400, sh - 340, 380, 110, self.CARD)
        lbl = self.tk.Label(toast, text=("🔔 " + text)[:300], bg=self.CARD, fg=self.INK, wraplength=360, justify="left",
                            font=("Segoe UI", 10), padx=10, pady=8, cursor="hand2")
        lbl.pack(fill="both", expand=True)
        lbl.bind("<Button-1>", lambda e: (toast.destroy(), self.bring_up()))
        self.root.after(9000, toast.destroy)

    def run(self) -> None:
        self.root.mainloop()


# ================================================================== autoteste (usado no build do GitHub, no Windows)
class FakeApi:
    def __init__(self, base: str, token: str = ""):
        self.base, self.token = base, token

    def call(self, method: str, sub: str, body: Optional[Dict[str, Any]] = None, timeout: float = 60.0) -> Dict[str, Any]:
        if sub == "parear":
            return {"token": "t" * 43, "agente": "Bit"}
        if sub == "conversa":
            return {"mensagens": [{"text": "Feito.", "buttons": [{"label": "✅ Aprovar", "data": "ok:1"}]}]}
        if sub == "ao-vivo":
            return {"passo": "Clique em Inserir", "alvo": [0.1, 0.1, 0.2, 0.15], "concluido": False, "fala": "Clique", "novo": True}
        if sub == "ao-vivo/fim":
            return {"texto": "Encerrada."}
        return {"avisos": []}


def autotest(out_path: str) -> int:
    """Captura a tela, codifica, compara e abre as janelas de verdade (sem servidor). Escreve o resultado em JSON."""
    result: Dict[str, Any] = {"ok": False, "platform": sys.platform}
    try:
        dpi_aware()
        img, region = capture(None)
        b64 = encode(img)
        sig = signature(img)
        result.update(capture=list(img.size), jpeg_kb=len(b64) * 3 // 4 // 1024, region=region,
                      same=difference(sig, sig), link=parse_link("veja: https://x.azurecontainerapps.io/pc/bit#123456"),
                      protect_roundtrip=unprotect(protect("segredo")) == "segredo", foreground=bool(foreground() or not IS_WINDOWS))
        cfg_path = os.path.join(config_dir(), "autoteste.json")
        app = App(api_factory=FakeApi, config_path=cfg_path, test_mode=True)
        app.link_entry.insert(0, "https://x.azurecontainerapps.io/pc/bit#123456")
        app.pair()

        def steps() -> None:
            app.pump()
            app.add_message("teste", "bit", [{"label": "✅ Aprovar", "data": "ok:1"}])
            app.start_live("teste")
            app.show_step(FakeApi("", "").call("POST", "ao-vivo"), region)
            app.notice("Lembrete de teste")
            result["janelas"] = sum(1 for w in (app.overlay, app.guide, app.badge) if w is not None)
            app.stop_live(save=False)
            result["ok"] = result["janelas"] == 3 and result["protect_roundtrip"] and result["link"] is not None
            app.root.after(300, app.root.destroy)
        app.root.after(800, steps)
        app.run()
        try:
            os.remove(cfg_path)
        except OSError:
            pass
    except Exception as err:
        import traceback
        result["error"] = f"{type(err).__name__}: {err}"
        result["trace"] = traceback.format_exc()[-1500:]
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=1, default=str)
    return 0 if result["ok"] else 1


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--autoteste":
        return autotest(sys.argv[2])
    dpi_aware()
    App().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
