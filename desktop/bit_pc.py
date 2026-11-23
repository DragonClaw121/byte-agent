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
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

APP_NAME = "Bit no PC"
VERSION = "1.2"
IS_WINDOWS = sys.platform.startswith("win")
BLOCKED_WORDS = ("senha", "password", "1password", "bitwarden", "keepass", "lastpass", "inprivate", "incognito",
                 "anônima", "anonima", "private browsing", "navegação privada", "internet banking", "banco do brasil",
                 "itaú", "itau", "bradesco", "santander", "caixa econômica", "nubank", "banco inter", "token de segurança")
LIVE_CHECK_S = 2.0             # olha a tela a cada 2 s (no PC; não manda nada se não mudou)
LIVE_MIN_SEND_S = 6.0          # o assistente observa: no máximo 1 quadro a cada 6 s (perguntas vão na hora)
LIVE_IDLE_STOP_S = 1200        # desliga sozinho depois de 20 min sem mudança na tela (parado, não gasta nada)
DOCS_DIR = os.path.join(os.path.expanduser("~"), "Documents", "Bit")       # onde ficam planilhas e apresentações
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

    def download(self, sub: str, timeout: float = 120.0) -> bytes:
        req = urllib.request.Request(f"{self.base}/{sub}", headers={"User-Agent": f"BitPC/{VERSION}",
                                                                    "Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read(80 * 1024 * 1024)
        except urllib.error.HTTPError as err:
            raise ApiError(f"Não consegui baixar o arquivo (HTTP {err.code}).") from None
        except (urllib.error.URLError, OSError):
            raise ApiError("Sem conexão com o Bit agora (internet ou servidor fora do ar).") from None


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
    if IS_WINDOWS:
        # Sem CAPTUREBLT: com ele, cada captura obriga o Windows a recompor a tela inteira, e o sistema "engasga"
        # (o Outlook travava por instantes). As janelas do Bit já ficam fora da captura de outro jeito.
        import mss.windows
        mss.windows.CAPTUREBLT = 0
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


def open_path(path: str) -> None:
    """Abre o arquivo no programa padrão (Excel, PowerPoint, Word...)."""
    try:
        if IS_WINDOWS:
            os.startfile(path)                                    # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def mail_link(to: str, subject: str, body: str) -> str:
    q = urllib.parse.urlencode({"subject": subject, "body": body[:1800]}, quote_via=urllib.parse.quote)
    return f"mailto:{urllib.parse.quote(to.strip(), safe='@,;')}?{q}"


def open_mail(to: str, subject: str, body: str) -> None:
    """E-mail novo no programa de e-mail padrão (o Outlook, no Windows da empresa), já com assunto e texto."""
    open_path(mail_link(to, subject, body))


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


def hwnd_of(win: Any) -> int:
    """A janela "de verdade" do Windows (a moldura). Antes usávamos a interna e os estilos não pegavam."""
    import ctypes
    try:
        return int(win.wm_frame(), 16)
    except Exception:
        return ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()


def _ex_style(win: Any, add: int) -> None:
    if not IS_WINDOWS:
        return
    import ctypes
    try:
        hwnd = hwnd_of(win)
        style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
        ctypes.windll.user32.SetWindowLongW(hwnd, -20, style | add)
    except Exception:
        pass


WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_LAYERED, WS_EX_TRANSPARENT = 0x08000000, 0x00000080, 0x00080000, 0x00000020


def exclude_from_capture(win: Any) -> None:
    """As janelas do Bit não aparecem nas capturas (Windows 10 2004+). Assim o Bit nunca vê a si mesmo."""
    if not IS_WINDOWS:
        return
    import ctypes
    try:
        ctypes.windll.user32.SetWindowDisplayAffinity(hwnd_of(win), 0x11)       # WDA_EXCLUDEFROMCAPTURE
    except Exception:
        pass


def no_activate(win: Any) -> None:
    """Avisos, balões e marcações aparecem SEM tirar o foco de quem está digitando (no Outlook, por exemplo)."""
    _ex_style(win, WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)


def click_through(win: Any) -> None:
    """A marcação vermelha não atrapalha: o clique passa para o programa embaixo."""
    _ex_style(win, WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)


def foreground_hwnd() -> int:
    if not IS_WINDOWS:
        return 0
    import ctypes
    return int(ctypes.windll.user32.GetForegroundWindow() or 0)


def give_focus_back(hwnd: int) -> None:
    """Se uma janelinha do Bit pegou o foco ao aparecer, devolve para onde estava (o Outlook continua digitando)."""
    if not IS_WINDOWS or not hwnd:
        return
    import ctypes
    from ctypes import wintypes
    try:
        now = ctypes.windll.user32.GetForegroundWindow()
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(now, ctypes.byref(pid))
        if now != hwnd and pid.value == os.getpid():
            ctypes.windll.user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


# ================================================================== o app
# O Tk 8.6 (o das janelas do Python) não desenha emojis fora do "plano básico" do Unicode (🔔 📸 👉 ...): saem
# quebrados. Tudo que vai para a tela passa por tk_safe, que troca os mais comuns por símbolos simples.
EMOJI_TK = {"🔔": "●", "📸": "◉", "🔴": "●", "👉": "➜", "🙈": "⊘", "📋": "☰", "💬": "✎", "📬": "✉", "📥": "✉",
            "📝": "✎", "📅": "▸", "🗓️": "▸", "🗓": "▸", "⏰": "⏰", "🤖": "", "💻": "", "🧠": "", "🎙️": "", "🔊": "♪",
            "🔗": "", "📊": "", "📈": "", "📎": "", "🚗": "", "☀️": "☀", "🌤️": "☀", "🔑": "", "🧩": "", "🎯": "",
            "🔐": "", "⚡": "", "🩺": "", "📶": "", "🧹": "", "👥": "", "✉️": "✉", "🗂️": "", "📁": "", "📑": ""}


def tk_safe(text: Any) -> str:
    out = str(text or "")
    for k, v in EMOJI_TK.items():
        out = out.replace(k, v)
    out = "".join(ch for ch in out if ord(ch) <= 0xFFFF and ch != "️")
    return "\n".join(" ".join(line.split()) if line.strip() else "" for line in out.split("\n")).strip()


ICON_FONT = "Segoe MDL2 Assets"           # ícones do próprio Windows 10/11 (sem baixar nada)
ICONS = {"preparar": ("\uE70F", "✎"), "chat": ("", "✉"), "olhar": ("", "◉"), "vivo": ("", "●"), "parar": ("", "■"),
         "voz": ("", "♪"), "mudo": ("", "♪"), "sino": ("", "!"), "abrir": ("", "⤢"),
         "fechar": ("", "✕"), "arrastar": ("", "⋮"), "enviar": ("", "➜")}


class App:
    BG, CARD, HOVER, INK, MUTED, PRI, RED = "#12151c", "#1b1f29", "#2a3142", "#e7e9ef", "#9aa3b5", "#6d8cff", "#ff5a52"

    def __init__(self, api_factory: Callable[[str, str], Any] = Api, config_path: Optional[str] = None,
                 test_mode: bool = False):
        import tkinter as tk
        import tkinter.font as tkfont
        self.tk = tk
        self.api_factory, self.config_path, self.test_mode = api_factory, config_path, test_mode
        self.cfg = load_config(config_path)
        self.events: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self.voice = Voice()
        self.api: Optional[Any] = None
        self.last_foreign: Optional[Dict[str, Any]] = None      # a última janela que NÃO é do Bit (o que você está fazendo)
        self.live: Optional[Dict[str, Any]] = None
        self.overlay = self.badge = self.bar = self.bubble = None
        self.result_windows: List[Any] = []
        self.suggest_box = self.bar_suggest = None
        self.bar_xy: Tuple[int, int] = (0, 0)
        self.bubble_xy: Tuple[int, int] = (0, 0)
        self.toasts: List[Any] = []
        self.unread: List[str] = []
        self.bar_text, self.bar_hint_text = "", ""
        self.entry = self.chat = None
        self.my_pid = os.getpid()
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.configure(bg=self.BG)
        # Escala do Windows (125%, 150%...): as fontes crescem com ela, então as janelas também precisam crescer.
        dpi = self.root.winfo_fpixels("1i")
        try:
            if float(os.environ.get("BITPC_ESCALA_TESTE") or 0) > 0:
                dpi = 96.0 * float(os.environ["BITPC_ESCALA_TESTE"])
        except ValueError:
            pass
        self.scale = max(1.0, dpi / 96.0)
        try:
            self.root.tk.call("tk", "scaling", dpi / 72.0)
        except Exception:
            pass
        self.has_icons = ICON_FONT in tkfont.families(self.root)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w, h = min(self.px(480), sw - self.px(40)), min(self.px(720), sh - self.px(90))
        self.root.geometry(f"{w}x{h}+{max(0, sw - w - self.px(40))}+{max(0, (sh - h) // 2 - self.px(20))}")
        self.root.minsize(min(self.px(380), w), min(self.px(480), h))
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.speak_var = tk.BooleanVar(value=bool(self.cfg.get("voz", False)))
        self.text_var = tk.BooleanVar(value=bool(self.cfg.get("texto", True)))
        self.frame: Optional[Any] = None
        token = unprotect(str(self.cfg.get("token") or ""))
        if self.cfg.get("base") and token:
            self.api = self.api_factory(self.cfg["base"], token)
            self.show_main()
            if self.cfg.get("modo") == "compacto":
                self.root.after(50, self.show_companion)
        else:
            self.show_pairing()
        self.root.after(150, self.pump)
        self.root.after(400, lambda: exclude_from_capture(self.root))
        if not test_mode:
            threading.Thread(target=hotkeys, args=(self.events,), daemon=True, name="atalhos").start()
            threading.Thread(target=self.track_foreground, daemon=True, name="janela-em-uso").start()
            threading.Thread(target=self.poll_notices, daemon=True, name="avisos").start()

    # ------------------------------------------------------------ utilidades de interface
    def px(self, value: float) -> int:
        return int(round(value * self.scale))

    def screen(self) -> Tuple[int, int]:
        return self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def label(self, parent: Any, text: str, size: int = 10, color: Optional[str] = None, bold: bool = False, **kw: Any) -> Any:
        return self.tk.Label(parent, text=tk_safe(text), bg=kw.pop("bg", self.BG), fg=color or self.INK, justify="left",
                             font=("Segoe UI", size, "bold" if bold else "normal"), **kw)

    def button(self, parent: Any, text: str, cmd: Callable[[], None], primary: bool = False) -> Any:
        return self.tk.Button(parent, text=tk_safe(text), command=cmd, relief="flat", cursor="hand2", padx=self.px(10),
                              pady=self.px(4), bg=self.PRI if primary else self.CARD, fg="#0d1020" if primary else self.INK,
                              activebackground=self.PRI, font=("Segoe UI", 10), borderwidth=0)

    def clear(self) -> None:
        if self.frame is not None:
            self.frame.destroy()
        self.frame = self.tk.Frame(self.root, bg=self.BG)
        self.frame.pack(fill="both", expand=True, padx=self.px(12), pady=self.px(10))

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
                    self.hotkey_open()
                elif kind == "ao_vivo":
                    self.toggle_live()
                elif kind == "aviso":
                    self.notice(str(payload))
        except queue.Empty:
            pass
        except Exception as err:                                  # um erro numa janela não pode parar o app
            self.add_message(f"⚠ {type(err).__name__}: {err}", "aviso")
        self.root.after(150, self.pump)

    def bring_up(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(300, lambda: self.root.attributes("-topmost", False))
        if self.entry is not None:
            self.entry.focus_force()

    def hotkey_open(self) -> None:
        if self.bar is not None:
            self.bar.deiconify()
            self.bar.lift()
            self.bar_open_ask(True)
        else:
            self.bring_up()

    def error(self, msg: str) -> None:
        if self.chat is not None:
            self.add_message("⚠ " + msg, "aviso")
            if self.bar is not None:
                self.show_bubble([{"text": "⚠ " + msg}])
        elif getattr(self, "pair_status", None) is not None:
            self.pair_status.configure(text=tk_safe(msg), fg=self.RED)

    def quit(self) -> None:
        try:
            self.stop_live(save=False)
        finally:
            self.root.destroy()

    # ------------------------------------------------------------ conectar
    def show_pairing(self) -> None:
        self.chat = None
        self.entry = None
        self.clear()
        f = self.frame
        self.label(f, "Bit no PC", 18, bold=True).pack(anchor="w")
        self.label(f, "1. No painel do Bit, aba Comandos → \"Gerar link de conexão\" (ou /pc no Telegram).\n"
                      "2. Copie o link (https://...#123456).\n3. Cole aqui e clique em Conectar.", 10, self.MUTED,
                   wraplength=self.px(420)).pack(anchor="w", pady=(self.px(8), self.px(10)))
        self.link_entry = self.tk.Entry(f, bg=self.CARD, fg=self.INK, insertbackground=self.INK, relief="flat",
                                        font=("Segoe UI", 10))
        self.link_entry.pack(fill="x", ipady=self.px(5))
        self.label(f, "Nome deste computador:", 9, self.MUTED).pack(anchor="w", pady=(self.px(10), self.px(2)))
        self.name_entry = self.tk.Entry(f, bg=self.CARD, fg=self.INK, insertbackground=self.INK, relief="flat",
                                        font=("Segoe UI", 10))
        self.name_entry.insert(0, os.environ.get("COMPUTERNAME", "Meu PC")[:40])
        self.name_entry.pack(fill="x", ipady=self.px(5))
        self.button(f, "Conectar", self.pair, primary=True).pack(anchor="w", pady=self.px(12))
        self.pair_status = self.label(f, "", 10, self.MUTED, wraplength=self.px(420))
        self.pair_status.pack(anchor="w")

    def pair(self) -> None:
        parsed = parse_link(self.link_entry.get())
        if not parsed:
            self.pair_status.configure(text="Esse não parece o link de conexão (precisa ter https://, /pc/ e #123456).",
                                       fg=self.RED)
            return
        base, code = parsed
        name = self.name_entry.get().strip() or "Meu PC"
        self.pair_status.configure(text="Conectando…", fg=self.MUTED)

        def done(res: Dict[str, Any]) -> None:
            self.cfg.update({"base": base, "token": protect(res["token"]), "agente": res.get("agente", "Bit")})
            save_config(self.cfg, self.config_path)
            self.api = self.api_factory(base, res["token"])
            self.show_main()
            self.add_message(f"✔ Conectado ao {res.get('agente', 'Bit')}. Atalhos: Ctrl+Alt+B abre o Bit · "
                             "Ctrl+Alt+L liga a ajuda ao vivo. Experimente o modo compacto (botão no topo).", "aviso")
        self.run_bg(lambda: self.api_factory(base, "").call("POST", "parear", {"codigo": code, "nome": name}), done,
                    lambda m: self.pair_status.configure(text=tk_safe(m), fg=self.RED))

    def disconnect(self) -> None:
        self.stop_live(save=False)
        self.close_bar()
        self.cfg.pop("token", None)
        self.cfg["modo"] = "janela"
        save_config(self.cfg, self.config_path)
        self.api = None
        self.root.deiconify()
        self.show_pairing()

    # ------------------------------------------------------------ conversa (janela completa)
    def show_main(self) -> None:
        tk = self.tk
        self.clear()
        f = self.frame
        top = tk.Frame(f, bg=self.BG)
        top.pack(fill="x")
        self.label(top, self.cfg.get("agente") or "Bit", 14, bold=True).pack(side="left")
        self.button(top, "Desconectar", self.disconnect).pack(side="right")
        self.button(top, "▭ Modo compacto", self.show_companion, primary=True).pack(side="right", padx=self.px(6))
        # de baixo para cima: o que precisa sempre aparecer entra primeiro (a conversa fica com o espaço que sobrar)
        self.label(f, "Ctrl+Alt+B abre o Bit · Ctrl+Alt+L liga/desliga a ajuda ao vivo", 8, self.MUTED,
                   wraplength=self.px(420)).pack(
            side="bottom", anchor="w", pady=(self.px(4), 0))
        opts = tk.Frame(f, bg=self.BG)
        opts.pack(side="bottom", fill="x", pady=(self.px(6), 0))
        for text, var in (("Orientação em texto", self.text_var), ("Orientação por voz", self.speak_var)):
            tk.Checkbutton(opts, text=text, variable=var, command=self.save_options, bg=self.BG, fg=self.MUTED,
                           selectcolor=self.CARD, activebackground=self.BG, activeforeground=self.INK,
                           font=("Segoe UI", 9)).pack(side="left")
        prep = tk.Frame(f, bg=self.BG)
        prep.pack(side="bottom", fill="x", pady=(self.px(6), 0))
        self.label(prep, "Preparar com o que está na tela:", 8, self.MUTED).pack(anchor="w")
        kinds = tk.Frame(prep, bg=self.BG)
        kinds.pack(fill="x")
        for kind, text in self.KINDS:
            tk.Button(kinds, text=tk_safe(text), command=lambda k=kind: self.prepare(k), relief="flat", cursor="hand2",
                      padx=self.px(7), pady=self.px(3), bg=self.HOVER, fg=self.INK, activebackground=self.PRI,
                      font=("Segoe UI", 9), borderwidth=0).pack(side="left", padx=(0, self.px(4)), pady=(self.px(2), 0))
        self.suggest_box = tk.Frame(prep, bg=self.BG)
        self.suggest_box.pack(fill="x")
        tools = tk.Frame(f, bg=self.BG)
        tools.pack(side="bottom", fill="x", pady=(self.px(8), 0))
        self.button(tools, "◉ Olhar minha tela", self.look).pack(side="left")
        self.live_btn = self.button(tools, "● Assistente ao vivo", self.toggle_live)
        self.live_btn.pack(side="left", padx=self.px(6))
        row = tk.Frame(f, bg=self.BG)
        row.pack(side="bottom", fill="x")
        self.entry = tk.Entry(row, bg=self.CARD, fg=self.INK, insertbackground=self.INK, relief="flat", font=("Segoe UI", 10))
        self.entry.pack(side="left", fill="x", expand=True, ipady=self.px(5))
        self.entry.bind("<Return>", lambda e: self.send())
        self.button(row, "Enviar", self.send, primary=True).pack(side="left", padx=(self.px(6), 0))
        box = tk.Frame(f, bg=self.CARD)
        box.pack(fill="both", expand=True, pady=self.px(8))
        self.chat = tk.Text(box, bg=self.CARD, fg=self.INK, wrap="word", relief="flat", font=("Segoe UI", 10),
                            padx=self.px(8), pady=self.px(8), state="disabled", cursor="arrow", borderwidth=0,
                            width=1, height=1)                     # ocupa o espaço que sobrar (não pede 80 colunas)
        scroll = tk.Scrollbar(box, command=self.chat.yview)
        self.chat.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.chat.pack(side="left", fill="both", expand=True)
        self.chat.tag_configure("eu", foreground=self.PRI, spacing1=self.px(6))
        self.chat.tag_configure("bit", foreground=self.INK, spacing1=self.px(6))
        self.chat.tag_configure("aviso", foreground=self.MUTED, spacing1=self.px(6))

    def save_options(self) -> None:
        self.cfg["voz"], self.cfg["texto"] = bool(self.speak_var.get()), bool(self.text_var.get())
        save_config(self.cfg, self.config_path)
        if not self.speak_var.get():
            self.voice.stop()
        self.update_bar()

    def add_message(self, text: str, who: str = "bit", buttons: Optional[List[Dict[str, str]]] = None) -> None:
        if self.chat is None:
            return
        tk = self.tk
        self.chat.configure(state="normal")
        prefix = {"eu": "Você: ", "bit": f"{self.cfg.get('agente') or 'Bit'}: ", "aviso": ""}.get(who, "")
        self.chat.insert("end", prefix + tk_safe(text) + "\n", who)
        for b in buttons or []:
            btn = tk.Button(self.chat, text=tk_safe(b.get("label", "?")), relief="flat", bg=self.PRI, fg="#0d1020",
                            cursor="hand2", font=("Segoe UI", 9), borderwidth=0, padx=self.px(8))
            btn.configure(command=lambda data=b.get("data", ""), label=b.get("label", ""), w=btn: self.press(data, label, w))
            self.chat.window_create("end", window=btn, padx=self.px(3), pady=self.px(3))
        if buttons:
            self.chat.insert("end", "\n")
        self.chat.configure(state="disabled")
        self.chat.see("end")

    def on_reply(self, res: Dict[str, Any]) -> None:
        msgs = res.get("mensagens") or []
        for m in msgs:
            self.add_message(str(m.get("text") or ""), "bit", m.get("buttons") or [])
        if self.bar is not None and msgs:
            self.show_bubble(msgs)

    def ask(self, text: str) -> None:
        text = text.strip()
        if not text or self.api is None:
            return
        self.add_message(text, "eu")
        if self.live is not None:                               # durante a ajuda ao vivo, a pergunta vai com a tela
            self.live["pergunta"] = text
            return
        if self.bar is not None:
            self.show_bubble([{"text": "…"}], wait=True)
        self.run_bg(lambda: self.api.call("POST", "conversa", {"texto": text}, timeout=180), self.on_reply)

    def send(self) -> None:
        text = self.entry.get()
        self.entry.delete(0, "end")
        self.ask(text)

    def press(self, data: str, label: str, widget: Any) -> None:
        try:
            widget.configure(state="disabled")
        except Exception:
            pass
        self.add_message(label, "eu")
        self.run_bg(lambda: self.api.call("POST", "conversa", {"botao": data}, timeout=120), self.on_reply)

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

    def look(self, question: Optional[str] = None) -> None:
        if self.api is None:
            return
        if question is None and self.entry is not None:
            question = self.entry.get()
            self.entry.delete(0, "end")
        question = (question or "").strip()
        image, _, _, title = self.grab()
        if image is None:
            msg = "⊘ A janela em uso parece privada (senha, banco ou navegação anônima): não vou olhar."
            self.add_message(msg, "aviso")
            if self.bar is not None:
                self.show_bubble([{"text": msg}])
            return
        self.add_message(("◉ " + question) if question else "◉ Olhe minha tela" + (f" ({title[:60]})" if title else ""), "eu")
        if self.bar is not None:
            self.show_bubble([{"text": "Olhando a sua tela…"}], wait=True)
        self.run_bg(lambda: self.api.call("POST", "conversa", {"texto": question, "tela": image}, timeout=120), self.on_reply)

    # ------------------------------------------------------------ assistente ao vivo (observa e PREPARA o trabalho)
    def toggle_live(self) -> None:
        if self.live is not None:
            self.stop_live()
        else:
            self.start_live()

    def start_live(self, goal: Optional[str] = None) -> None:
        if self.api is None:
            self.bring_up()
            return
        if goal is None:                                        # sem caixa de diálogo: nada rouba o foco
            source = self.bar_entry if self.bar is not None else self.entry
            goal = source.get().strip() if source is not None else ""
            if goal:
                source.delete(0, "end")
        self.live = {"sessao": uuid.uuid4().hex[:16], "objetivo": goal.strip(), "sig": None, "sent": 0.0,
                     "changed": time.time(), "pergunta": "", "busy": False, "vendo": "", "sugestoes": [], "notas": 0}
        self.add_message("● Assistente ao vivo ligado: trabalhe normalmente. Eu observo a tela e anoto o que importa; "
                         "quando quiser, peça um e-mail, uma planilha, uma apresentação ou um documento prontos "
                         "(botões \"Preparar\"). Ctrl+Alt+L ou o aviso vermelho para parar.", "aviso")
        if getattr(self, "live_btn", None) is not None:
            self.live_btn.configure(text="■ Parar o assistente", bg=self.RED)
        self.show_badge()
        self.set_bar_text("Observando a sua tela…")
        self.update_bar()
        self.draw_suggestions()
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
                    self.events.put(("call", lambda: self.set_badge("‖ Janela privada: não estou olhando")))
                    continue
                img, used = capture(region or None)
                sig = signature(img)
                now = time.time()
                moved = difference(sig, live["sig"]) >= CHANGE_THRESHOLD
                if moved:
                    live["changed"] = now
                if now - live["changed"] > LIVE_IDLE_STOP_S and not live["pergunta"]:
                    self.events.put(("call", lambda: self.stop_live(reason="Desliguei o assistente: 20 minutos sem mudança na tela.")))
                    return
                if not (live["pergunta"] or live["sig"] is None or (moved and now - live["sent"] >= LIVE_MIN_SEND_S)):
                    continue
                live["busy"], live["sent"], live["sig"] = True, now, sig
                question, live["pergunta"] = live["pergunta"], ""
                body = {"sessao": live["sessao"], "objetivo": live["objetivo"], "tela": encode(img), "pergunta": question}
                res = self.api.call("POST", "ao-vivo", body, timeout=90)
                self.events.put(("call", lambda r=res, u=used, t=title: self.show_observation(r, u, t)))
            except Exception as err:
                msg = str(err)
                self.events.put(("call", lambda m=msg: self.set_badge("⚠ " + m[:70])))
                time.sleep(10)
            finally:
                live["busy"] = False

    def show_observation(self, obs: Dict[str, Any], region: Dict[str, int], title: str = "") -> None:
        if self.live is None or obs.get("espere"):
            return
        if obs.get("vendo"):
            self.live["vendo"] = obs["vendo"]
            self.set_badge("● Bit observando" + (f" — {title[:40]}" if title else ""))
            self.set_bar_text("◉ " + obs["vendo"])
        self.live["notas"] = int(obs.get("notas_total") or self.live["notas"])
        if obs.get("sugestoes") and obs["sugestoes"] != self.live["sugestoes"]:
            self.live["sugestoes"] = list(obs["sugestoes"])
            self.draw_suggestions()
        if obs.get("resposta"):
            self.add_message(obs["resposta"], "bit")
            if self.bar is not None:
                self.show_bubble([{"text": obs["resposta"]}])
            if self.speak_var.get():
                self.voice.say(obs["resposta"])
        if obs.get("alvo"):
            self.show_overlay(to_screen(obs["alvo"], region))

    def stop_live(self, reason: str = "", save: bool = False) -> None:
        live, self.live = self.live, None
        for name in ("overlay", "badge"):
            win = getattr(self, name)
            if win is not None:
                try:
                    win.destroy()
                except Exception:
                    pass
                setattr(self, name, None)
        self.voice.stop()
        if getattr(self, "live_btn", None) is not None:
            try:
                self.live_btn.configure(text="● Assistente ao vivo", bg=self.CARD)
            except Exception:
                pass
        self.set_bar_text("")
        self.update_bar()
        self.draw_suggestions()
        if live is None or self.api is None:
            return
        if reason:
            self.add_message(reason, "aviso")

        def done(r: Dict[str, Any]) -> None:
            self.add_message(str(r.get("texto") or "Assistente ao vivo encerrado."), "aviso")
        self.run_bg(lambda: self.api.call("POST", "ao-vivo/fim", {"sessao": live["sessao"], "guardar": bool(save)}), done)

    # ------------------------------------------------------------ preparar o trabalho (e-mail, planilha, slides...)
    KINDS = (("email", "✉ E-mail"), ("planilha", "▦ Planilha"), ("apresentacao", "▭ Slides"), ("documento", "☰ Documento"),
             ("texto", "✎ Texto"))
    KIND_NAMES = {"email": "o e-mail", "planilha": "a planilha", "apresentacao": "a apresentação", "documento": "o documento",
                  "texto": "o texto"}

    def take_request_text(self) -> str:
        source = self.bar_entry if self.bar is not None else self.entry
        if source is None:
            return ""
        text = source.get().strip()
        source.delete(0, "end")
        return text

    def prepare(self, kind: str, request: Optional[str] = None) -> None:
        """Com o assistente ligado, usa o que ele anotou da tela; desligado, manda a tela atual junto."""
        if self.api is None:
            return
        request = self.take_request_text() if request is None else request
        body: Dict[str, Any] = {"tipo": kind, "pedido": request}
        if self.live is not None:
            body["sessao"] = self.live["sessao"]
        else:
            image, _, _, _ = self.grab()
            if image is None:
                self.error("A janela em uso parece privada: não vou olhar.")
                return
            body["tela"] = image
        what = self.KIND_NAMES.get(kind, kind)
        self.add_message(f"Preparar {what}" + (f": {request}" if request else ""), "eu")
        if self.bar is not None:
            self.show_bubble([{"text": f"Preparando {what}…"}], wait=True)
        self.run_bg(lambda: self.api.call("POST", "preparar", body, timeout=300), self.on_prepared)

    def on_prepared(self, res: Dict[str, Any]) -> None:
        kind = res.get("tipo")
        if kind in ("email", "texto"):
            self.add_message(f"✔ {res.get('titulo') or 'Pronto'}: abri numa janela para você revisar e copiar.", "bit")
            if self.bar is not None:
                self.show_bubble([{"text": f"✔ {res.get('titulo') or 'Pronto'} (abri numa janela)"}])
            self.open_result(res)
            return
        files = list(res.get("arquivos") or [])
        self.run_bg(lambda: self.save_files(files), self.on_saved)

    def save_files(self, names: List[str]) -> List[str]:
        os.makedirs(DOCS_DIR, exist_ok=True)
        paths = []
        for name in names:
            safe = os.path.basename(str(name))
            data = self.api.download("arquivo/" + urllib.parse.quote(safe))
            path = os.path.join(DOCS_DIR, safe)
            with open(path, "wb") as fh:
                fh.write(data)
            paths.append(path)
        return paths

    def on_saved(self, paths: List[str]) -> None:
        if not paths:
            self.error("O Bit não gerou nenhum arquivo agora; tente de novo.")
            return
        names = ", ".join(os.path.basename(p) for p in paths)
        self.add_message(f"✔ Pronto: {names} (em Documentos\\Bit). Abri para você revisar.", "bit")
        if self.bar is not None:
            self.show_bubble([{"text": f"✔ Pronto: {names}\nSalvo em Documentos\\Bit e aberto."}])
        if not self.test_mode:
            for path in paths:
                open_path(path)
        self.saved_paths = paths

    def open_result(self, res: Dict[str, Any]) -> None:
        """Janela normal (essa pode ter foco: você vai editar/copiar), com o texto pronto."""
        tk = self.tk
        win = tk.Toplevel(self.root)
        win.title(tk_safe(res.get("titulo") or "Pronto") + " — Bit")
        win.configure(bg=self.BG)
        sw, sh = self.screen()
        w, h = min(self.px(600), sw - self.px(40)), min(self.px(560), sh - self.px(90))
        win.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 2)}")
        win.after(200, lambda: exclude_from_capture(win))
        frame = tk.Frame(win, bg=self.BG)
        frame.pack(fill="both", expand=True, padx=self.px(12), pady=self.px(10))
        fields: Dict[str, Any] = {}
        if res.get("tipo") == "email":
            for key, label in (("para", "Para"), ("assunto", "Assunto")):
                row = tk.Frame(frame, bg=self.BG)
                row.pack(fill="x", pady=(0, self.px(6)))
                self.label(row, label, 9, self.MUTED, width=8, anchor="w").pack(side="left")
                entry = tk.Entry(row, bg=self.CARD, fg=self.INK, insertbackground=self.INK, relief="flat", font=("Segoe UI", 10))
                entry.insert(0, str(res.get(key) or ""))
                entry.pack(side="left", fill="x", expand=True, ipady=self.px(4))
                fields[key] = entry
        buttons = tk.Frame(frame, bg=self.BG)
        buttons.pack(side="bottom", fill="x", pady=(self.px(8), 0))
        body = tk.Text(frame, bg=self.CARD, fg=self.INK, insertbackground=self.INK, wrap="word", relief="flat",
                       font=("Segoe UI", 10), padx=self.px(8), pady=self.px(8), width=1, height=1, borderwidth=0)
        body.insert("1.0", str(res.get("corpo") or res.get("texto") or ""))
        body.pack(fill="both", expand=True)
        fields["corpo"] = body

        def text_of() -> str:
            return body.get("1.0", "end").strip()

        def copy(value: str, what: str) -> None:
            win.clipboard_clear()
            win.clipboard_append(value)
            status.configure(text=f"✔ {what} copiado.")

        def mail() -> None:
            copy(text_of(), "Texto")
            open_mail(fields["para"].get() if "para" in fields else "", fields["assunto"].get() if "assunto" in fields else "",
                      text_of())
            status.configure(text="✔ Abri um e-mail novo. Se o texto não aparecer inteiro, cole (Ctrl+V): já está copiado.")
        self.button(buttons, "Copiar o texto", lambda: copy(text_of(), "Texto"), primary=True).pack(side="left")
        if res.get("tipo") == "email":
            self.button(buttons, "Copiar o assunto", lambda: copy(fields["assunto"].get(), "Assunto")).pack(
                side="left", padx=self.px(6))
            self.button(buttons, "Abrir no e-mail", mail).pack(side="left")
        self.button(buttons, "Fechar", win.destroy).pack(side="right")
        status = self.label(frame, "Revise e ajuste o texto à vontade antes de copiar.", 9, self.MUTED)
        status.pack(side="bottom", anchor="w", pady=(self.px(6), 0))
        win.lift()
        win.focus_force()
        self.result_windows.append(win)

    def draw_suggestions(self) -> None:
        """Os botões "Preparar" (na janela e na barra) + as sugestões do que o Bit viu."""
        for holder in (getattr(self, "suggest_box", None), getattr(self, "bar_suggest", None)):
            if holder is None:
                continue
            for child in holder.winfo_children():
                child.destroy()
            suggestions = list((self.live or {}).get("sugestoes") or [])
            if not suggestions:
                continue
            tk = self.tk
            self.label(holder, "Sugestões do que vi:", 8, self.MUTED, bg=holder.cget("bg")).pack(anchor="w")
            for sug in suggestions:
                icon = dict(self.KINDS).get(sug["tipo"], "✎").split()[0]
                btn = tk.Button(holder, text=tk_safe(f"{icon} {sug['pedido']}")[:70], relief="flat", cursor="hand2",
                                bg=self.HOVER, fg=self.INK, activebackground=self.PRI, font=("Segoe UI", 9), anchor="w",
                                borderwidth=0, padx=self.px(8), pady=self.px(2),
                                command=lambda s=sug: self.prepare(s["tipo"], s["pedido"]))
                btn.pack(fill="x", pady=(self.px(2), 0))
        if self.bar is not None:
            self.relayout_bar()

    # ------------------------------------------------------------ janelinhas sobre a tela (tamanho pelo conteúdo)
    def popup(self, bg: str, border: Optional[str] = None, activate: bool = False) -> Tuple[Any, Any]:
        """Janela sem moldura, sempre por cima, fora das capturas. Nasce escondida e só aparece em place(), já
        posicionada e SEM roubar o foco (activate=True só para a barra, que tem campo de digitação)."""
        win = self.tk.Toplevel(self.root)
        win.withdraw()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=border or bg)
        win._bit_activate = activate
        inner = self.tk.Frame(win, bg=bg)
        inner.pack(fill="both", expand=True, padx=1 if border else 0, pady=1 if border else 0)
        return win, inner

    def reveal(self, win: Any) -> None:
        activate = getattr(win, "_bit_activate", False)
        before = foreground_hwnd()
        win.update_idletasks()
        if not activate:
            no_activate(win)
        win.deiconify()
        win.update_idletasks()
        if not activate:
            no_activate(win)                                      # de novo: a moldura do Windows só existe depois de mostrar
            for delay in (10, 120):
                win.after(delay, lambda: give_focus_back(before))
        exclude_from_capture(win)
        round_corners(win)

    def place(self, win: Any, x: int, y: int) -> None:
        win.update_idletasks()
        sw, sh = self.screen()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        x, y = max(0, min(x, sw - w)), max(0, min(y, sh - h))
        win.geometry(f"{w}x{h}+{x}+{y}")
        if win.state() == "withdrawn":
            self.reveal(win)
        if win is self.bar:
            self.bar_xy = (x, y)                                  # a posição real só existe depois de desenhada
        elif win is self.bubble:
            self.bubble_xy = (x, y)

    def show_badge(self) -> None:
        self.badge, inner = self.popup(self.RED)
        self.badge_label = self.tk.Label(inner, text=tk_safe("● Bit observando · clique para parar"), bg=self.RED,
                                         fg="white", font=("Segoe UI", 10, "bold"), cursor="hand2",
                                         padx=self.px(14), pady=self.px(4))
        self.badge_label.pack(fill="both", expand=True)
        self.badge_label.bind("<Button-1>", lambda e: self.stop_live())
        self.center_badge()

    def center_badge(self) -> None:
        if self.badge is not None:
            self.badge.update_idletasks()
            self.place(self.badge, (self.screen()[0] - self.badge.winfo_reqwidth()) // 2, self.px(6))

    def set_badge(self, text: str) -> None:
        if self.badge is not None:
            self.badge_label.configure(text=tk_safe(text + " · clique para parar"))
            self.center_badge()

    def show_overlay(self, rect: Tuple[int, int, int, int]) -> None:
        if self.overlay is not None:
            self.overlay.destroy()
        pad = self.px(6)
        x0, y0, x1, y1 = rect[0] - pad, rect[1] - pad, rect[2] + pad, rect[3] + pad
        w, h = max(20, x1 - x0), max(20, y1 - y0)
        win, inner = self.popup("#ff00ff")
        try:
            win.attributes("-transparentcolor", "#ff00ff")
        except Exception:
            pass
        canvas = self.tk.Canvas(inner, width=w, height=h, bg="#ff00ff", highlightthickness=0)
        canvas.pack()
        canvas.create_rectangle(2, 2, w - 2, h - 2, outline=self.RED, width=max(3, self.px(3)))
        win.geometry(f"{w}x{h}+{x0}+{y0}")
        self.reveal(win)
        click_through(win)                                        # depois de mostrar: aí a moldura existe de verdade
        self.overlay = win
        self.root.after(8000, lambda: (win.destroy() if self.overlay is win else None))

    # ------------------------------------------------------------ avisos do Bit (no canto, empilhados)
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
        self.add_message("● " + text, "aviso")
        self.unread.append(text)
        self.update_bar()
        tk = self.tk
        toast, inner = self.popup(self.CARD, border=self.PRI)
        head = tk.Frame(inner, bg=self.CARD)
        head.pack(fill="x", padx=self.px(10), pady=(self.px(8), 0))
        tk.Label(head, text=tk_safe(f"● {self.cfg.get('agente') or 'Bit'}"), bg=self.CARD, fg=self.PRI,
                 font=("Segoe UI", 9, "bold")).pack(side="left")
        close = tk.Label(head, text="✕", bg=self.CARD, fg=self.MUTED, font=("Segoe UI", 10), cursor="hand2")
        close.pack(side="right")
        body = tk.Label(inner, text=tk_safe(text)[:420], bg=self.CARD, fg=self.INK, wraplength=self.px(330),
                        justify="left", font=("Segoe UI", 10), cursor="hand2")
        body.pack(fill="x", padx=self.px(10), pady=(self.px(4), self.px(10)))
        close.bind("<Button-1>", lambda e: self.close_toast(toast))
        body.bind("<Button-1>", lambda e: (self.close_toast(toast), self.hotkey_open()))
        self.toasts.append(toast)
        while len(self.toasts) > 3:
            self.close_toast(self.toasts[0])
        self.layout_toasts()
        self.root.after(12000, lambda: self.close_toast(toast))

    def close_toast(self, toast: Any) -> None:
        if toast in self.toasts:
            self.toasts.remove(toast)
            try:
                toast.destroy()
            except Exception:
                pass
            self.layout_toasts()

    def layout_toasts(self) -> None:
        sw, sh = self.screen()
        bottom = sh - self.px(70)
        if self.bar is not None and self.bar_xy[1] > sh // 2:     # não cobre a barra compacta nem o balão dela
            top = self.bar_xy[1]
            if self.bubble is not None and self.bubble_xy[1] < top:
                top = self.bubble_xy[1]
            bottom = min(bottom, top - self.px(8))
        for toast in reversed(self.toasts):
            toast.update_idletasks()
            h = toast.winfo_reqheight()
            bottom -= h
            self.place(toast, sw - toast.winfo_reqwidth() - self.px(24), bottom)
            bottom -= self.px(8)

    # ------------------------------------------------------------ modo compacto (barra flutuante, tipo miniplayer)
    def icon(self, parent: Any, key: str, cmd: Optional[Callable[[], None]], tip: str = "") -> Any:
        glyph, fallback = ICONS[key]
        lbl = self.tk.Label(parent, text=glyph if self.has_icons else fallback,
                            font=(ICON_FONT, 12) if self.has_icons else ("Segoe UI Symbol", 12),
                            bg=self.CARD, fg=self.INK, padx=self.px(8), pady=self.px(5), cursor="hand2")
        if cmd is not None:
            lbl.bind("<Button-1>", lambda e: cmd())
        lbl.bind("<Enter>", lambda e: (lbl.configure(bg=self.HOVER), self.bar_hint(tip)))
        lbl.bind("<Leave>", lambda e: (lbl.configure(bg=self.CARD), self.bar_hint("")))
        return lbl

    def show_companion(self) -> None:
        if self.api is None:
            return
        self.cfg["modo"] = "compacto"
        save_config(self.cfg, self.config_path)
        if self.bar is not None:
            self.bar.deiconify()
            return
        tk = self.tk
        self.root.withdraw()
        self.bar, inner = self.popup(self.CARD, border=self.PRI)
        try:
            self.bar.attributes("-alpha", 0.97)
        except Exception:
            pass
        row = tk.Frame(inner, bg=self.CARD)
        row.pack(fill="x", padx=self.px(4), pady=self.px(3))
        grip = self.icon(row, "arrastar", None, "Arraste para mover")
        grip.configure(cursor="fleur", fg=self.MUTED)
        grip.pack(side="left")
        name = tk.Label(row, text=tk_safe(self.cfg.get("agente") or "Bit"), bg=self.CARD, fg=self.INK,
                        font=("Segoe UI", 10, "bold"), padx=self.px(2), cursor="fleur")
        name.pack(side="left", padx=(0, self.px(6)))
        for w in (grip, name):
            w.bind("<ButtonPress-1>", self.drag_start)
            w.bind("<B1-Motion>", self.drag_move)
            w.bind("<ButtonRelease-1>", self.drag_end)
        self.bar_buttons = {}
        for key, cmd, tip in (("chat", lambda: self.bar_open_ask(), "Perguntar ao Bit (Ctrl+Alt+B)"),
                              ("olhar", lambda: self.look(self.bar_take_text()), "Olhar minha tela"),
                              ("vivo", self.toggle_live, "Assistente ao vivo: observa e prepara (Ctrl+Alt+L)"),
                              ("preparar", lambda: self.bar_open_prepare(), "Preparar e-mail, planilha, slides, documento"),
                              ("voz", self.toggle_voice, "Orientação por voz: liga/desliga"),
                              ("sino", self.show_notices, "Avisos"),
                              ("abrir", self.show_window, "Abrir a janela completa"),
                              ("fechar", self.quit, "Fechar o Bit")):
            self.bar_buttons[key] = self.icon(row, key, cmd, tip)
            self.bar_buttons[key].pack(side="left")
            if key == "sino":
                self.bell_count = tk.Label(row, text="", bg=self.CARD, fg=self.RED, font=("Segoe UI", 8, "bold"))
                self.bell_count.pack(side="left")
        self.bar_ask = tk.Frame(inner, bg=self.CARD)
        self.bar_entry = tk.Entry(self.bar_ask, bg=self.BG, fg=self.INK, insertbackground=self.INK, relief="flat",
                                  font=("Segoe UI", 10))
        self.bar_entry.pack(side="left", fill="x", expand=True, ipady=self.px(4), padx=(self.px(8), self.px(4)))
        self.bar_entry.bind("<Return>", lambda e: self.ask(self.bar_take_text()))
        self.bar_entry.bind("<Escape>", lambda e: self.bar_open_ask(False))
        send = self.icon(self.bar_ask, "enviar", lambda: self.ask(self.bar_take_text()), "Enviar")
        send.pack(side="left", padx=(0, self.px(6)))
        self.bar_prep = tk.Frame(inner, bg=self.CARD)
        row1 = tk.Frame(self.bar_prep, bg=self.CARD)
        row1.pack(fill="x")
        for kind, text in self.KINDS:
            tk.Button(row1, text=tk_safe(text), relief="flat", cursor="hand2", bg=self.HOVER, fg=self.INK,
                      activebackground=self.PRI, font=("Segoe UI", 9), borderwidth=0, padx=self.px(6), pady=self.px(2),
                      command=lambda k=kind: self.prepare(k)).pack(side="left", padx=(0, self.px(3)))
        self.bar_suggest = tk.Frame(self.bar_prep, bg=self.CARD)
        self.bar_suggest.pack(fill="x", pady=(self.px(4), 0))
        self.bar_status = tk.Label(inner, text="", bg=self.CARD, fg=self.MUTED, wraplength=self.px(320), justify="left", anchor="w",
                                   font=("Segoe UI", 9), padx=self.px(10))
        self.bar.update_idletasks()
        sw, sh = self.screen()
        xy = self.cfg.get("barra_xy")
        if isinstance(xy, list) and len(xy) == 2:
            x, y = int(xy[0]), int(xy[1])
        else:
            x, y = sw - self.bar.winfo_reqwidth() - self.px(24), sh - self.bar.winfo_reqheight() - self.px(80)
        self.place(self.bar, x, y)
        self.update_bar()
        self.layout_toasts()

    def close_bar(self) -> None:
        for name in ("bubble", "bar"):
            win = getattr(self, name)
            if win is not None:
                try:
                    win.destroy()
                except Exception:
                    pass
                setattr(self, name, None)

    def show_window(self) -> None:
        self.cfg["modo"] = "janela"
        save_config(self.cfg, self.config_path)
        self.close_bar()
        self.bring_up()

    def drag_start(self, e: Any) -> None:
        self._drag = (e.x_root - self.bar.winfo_x(), e.y_root - self.bar.winfo_y())

    def drag_move(self, e: Any) -> None:
        dx, dy = getattr(self, "_drag", (0, 0))
        self.bar_xy = (e.x_root - dx, e.y_root - dy)
        self.bar.geometry(f"+{self.bar_xy[0]}+{self.bar_xy[1]}")
        if self.bubble is not None:
            self.position_bubble()

    def drag_end(self, e: Any) -> None:
        self.cfg["barra_xy"] = list(self.bar_xy)
        save_config(self.cfg, self.config_path)
        self.layout_toasts()

    def relayout_bar(self) -> None:
        if self.bar is None:
            return
        self.bar.update_idletasks()
        self.place(self.bar, *self.bar_xy)
        if self.bubble is not None:
            self.position_bubble()
        self.layout_toasts()

    def bar_open_ask(self, show: Optional[bool] = None) -> None:
        if self.bar is None:
            return
        visible = bool(self.bar_ask.winfo_manager())
        show = (not visible) if show is None else show
        if show and not visible:
            self.bar_ask.pack(fill="x", pady=(0, self.px(6)), before=self.bar_status if self.bar_status.winfo_manager() else None)
        elif not show and visible:
            self.bar_ask.pack_forget()
        self.relayout_bar()
        if show:
            self.bar.focus_force()
            self.bar_entry.focus_set()

    def bar_open_prepare(self, show: Optional[bool] = None) -> None:
        if self.bar is None:
            return
        visible = bool(self.bar_prep.winfo_manager())
        show = (not visible) if show is None else show
        if show and not visible:
            self.bar_prep.pack(fill="x", padx=self.px(8), pady=(0, self.px(6)))
            self.bar_hint("Escreva no campo ✉ o que quer (opcional) e escolha o que preparar")
            self.root.after(5000, lambda: self.bar_hint(""))
        elif not show and visible:
            self.bar_prep.pack_forget()
        self.draw_suggestions()

    def bar_take_text(self) -> str:
        if self.bar is None:
            return ""
        text = self.bar_entry.get()
        self.bar_entry.delete(0, "end")
        return text

    def set_bar_text(self, text: str) -> None:
        self.bar_text = tk_safe(text)
        self.refresh_bar_status()

    def bar_hint(self, tip: str) -> None:
        self.bar_hint_text = tk_safe(tip)
        self.refresh_bar_status()

    def refresh_bar_status(self) -> None:
        if self.bar is None:
            return
        text = self.bar_hint_text or self.bar_text
        if text:
            self.bar_status.configure(text=text, fg=self.MUTED if self.bar_hint_text else self.INK)
            if not self.bar_status.winfo_manager():
                self.bar_status.pack(fill="x", pady=(0, self.px(6)))
        elif self.bar_status.winfo_manager():
            self.bar_status.pack_forget()
        self.relayout_bar()

    def update_bar(self) -> None:
        if self.bar is None:
            return
        live = self.live is not None
        glyph, fallback = ICONS["parar" if live else "vivo"]
        self.bar_buttons["vivo"].configure(text=glyph if self.has_icons else fallback, fg=self.RED if live else self.INK)
        glyph, fallback = ICONS["voz" if self.speak_var.get() else "mudo"]
        self.bar_buttons["voz"].configure(text=glyph if self.has_icons else fallback,
                                          fg=self.INK if self.speak_var.get() else self.MUTED)
        self.bell_count.configure(text=str(len(self.unread)) if self.unread else "")

    def toggle_voice(self) -> None:
        self.speak_var.set(not self.speak_var.get())
        self.save_options()
        self.bar_hint("Orientação por voz: " + ("ligada" if self.speak_var.get() else "desligada"))

    def show_notices(self) -> None:
        if not self.unread:
            self.show_bubble([{"text": "Nenhum aviso novo."}])
            return
        items, self.unread = self.unread[-5:], []
        self.update_bar()
        self.show_bubble([{"text": "● " + t} for t in items])

    def show_bubble(self, messages: List[Dict[str, Any]], wait: bool = False) -> None:
        """Balão da resposta, colado na barra (em cima ou embaixo, onde couber)."""
        if self.bar is None:
            return
        if self.bubble is not None:
            self.bubble.destroy()
        tk = self.tk
        self.bubble, inner = self.popup(self.CARD, border=self.PRI)
        bubble = self.bubble
        for m in messages[-4:]:
            tk.Label(inner, text=tk_safe(m.get("text") or "")[:700], bg=self.CARD, fg=self.MUTED if wait else self.INK,
                     wraplength=self.px(360), justify="left", anchor="w", font=("Segoe UI", 10)).pack(
                anchor="w", fill="x", padx=self.px(12), pady=(self.px(8), 0))
            buttons = m.get("buttons") or []
            if buttons:
                row = tk.Frame(inner, bg=self.CARD)
                row.pack(anchor="w", padx=self.px(12), pady=(self.px(4), 0))
                for b in buttons:
                    btn = tk.Button(row, text=tk_safe(b.get("label", "?")), relief="flat", bg=self.PRI, fg="#0d1020",
                                    cursor="hand2", font=("Segoe UI", 9), borderwidth=0, padx=self.px(8))
                    btn.configure(command=lambda data=b.get("data", ""), label=b.get("label", ""), w=btn:
                                  self.press(data, label, w))
                    btn.pack(side="left", padx=(0, self.px(4)))
        foot = tk.Frame(inner, bg=self.CARD)
        foot.pack(fill="x", padx=self.px(12), pady=self.px(8))
        more = tk.Label(foot, text="Abrir a conversa", bg=self.CARD, fg=self.PRI, font=("Segoe UI", 9, "underline"),
                        cursor="hand2")
        more.pack(side="left")
        more.bind("<Button-1>", lambda e: self.show_window())
        close = tk.Label(foot, text="✕", bg=self.CARD, fg=self.MUTED, font=("Segoe UI", 10), cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: self.close_bubble(bubble))
        self.position_bubble()
        if not wait:
            self.root.after(30000, lambda: self.close_bubble(bubble))

    def close_bubble(self, bubble: Any) -> None:
        try:
            bubble.destroy()
        except Exception:
            pass
        if self.bubble is bubble:
            self.bubble = None
            self.layout_toasts()

    def position_bubble(self) -> None:
        if self.bubble is None or self.bar is None:
            return
        self.bubble.update_idletasks()
        bh, bw = self.bubble.winfo_reqheight(), self.bubble.winfo_reqwidth()
        x, y = self.bar_xy
        y = y - bh - self.px(6) if y - bh - self.px(6) > 0 else y + self.bar.winfo_reqheight() + self.px(6)
        self.place(self.bubble, x + self.bar.winfo_reqwidth() - bw, y)
        self.layout_toasts()

    def run(self) -> None:
        self.root.mainloop()


def round_corners(widget: Any) -> None:
    """Cantos arredondados no Windows 11 (no 10 não faz nada)."""
    if not IS_WINDOWS:
        return
    import ctypes
    try:
        hwnd = hwnd_of(widget)
        pref = ctypes.c_int(2)                                   # DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
    except Exception:
        pass


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
            return {"vendo": "Outlook: e-mail da Ana pedindo o orçamento de 3 notebooks 📧", "notas": "Ana pede orçamento",
                    "sugestoes": [{"tipo": "email", "pedido": "Responder à Ana confirmando o orçamento e o prazo de 12/12"},
                                  {"tipo": "planilha", "pedido": "Planilha do orçamento dos 3 notebooks"}],
                    "resposta": "", "alvo": [0.1, 0.1, 0.2, 0.15], "notas_total": 1}
        if sub == "preparar":
            if (body or {}).get("tipo") == "email":
                return {"tipo": "email", "titulo": "Resposta à Ana", "para": "ana@empresa.com", "assunto": "Re: Orçamento",
                        "corpo": "Olá, Ana!\n\nSegue o orçamento dos 3 notebooks, com entrega até 12/12. 👍\n\nAbraços,\n[SEU NOME]",
                        "arquivos": []}
            return {"tipo": "planilha", "titulo": "Orçamento", "arquivos": ["planilha_orcamento.xlsx"]}
        if sub == "ao-vivo/fim":
            return {"texto": "Encerrado."}
        return {"avisos": []}

    def download(self, sub: str, timeout: float = 120.0) -> bytes:
        return b"PK\x03\x04 planilha de teste"


def autotest(out_path: str) -> int:
    """Teste de verdade no Windows do GitHub: captura a tela, abre a janela, os avisos, a barra compacta, o balão e a
    ajuda ao vivo; confere que o texto CABE em cada janela (escala do Windows) e salva fotos para conferência."""
    result: Dict[str, Any] = {"ok": False, "platform": sys.platform, "escala": os.environ.get("BITPC_ESCALA_TESTE", "1")}
    prefix = os.path.splitext(os.path.abspath(out_path))[0]
    try:
        dpi_aware()
        img, region = capture(None)
        b64 = encode(img)
        sig = signature(img)
        result.update(capture=list(img.size), jpeg_kb=len(b64) * 3 // 4 // 1024, region=region,
                      same=difference(sig, sig), link=parse_link("veja: https://x.azurecontainerapps.io/pc/bit#123456"),
                      protect_roundtrip=unprotect(protect("segredo")) == "segredo", foreground=bool(foreground() or not IS_WINDOWS))
        cfg_path = os.path.join(config_dir(), "autoteste.json")
        if os.path.exists(cfg_path):
            os.remove(cfg_path)
        global DOCS_DIR
        DOCS_DIR = os.path.join(os.path.dirname(prefix), "documentos-teste")
        app = App(api_factory=FakeApi, config_path=cfg_path, test_mode=True)
        result["icones_windows"] = app.has_icons
        app.link_entry.insert(0, "https://x.azurecontainerapps.io/pc/bit#123456")
        app.pair()
        problems: List[str] = []

        def fits(win: Any, name: str) -> None:
            win.update_idletasks()
            if win.winfo_height() + 2 < win.winfo_reqheight() or win.winfo_width() + 2 < win.winfo_reqwidth():
                problems.append(f"{name}: {win.winfo_width()}x{win.winfo_height()} < {win.winfo_reqwidth()}x{win.winfo_reqheight()}")

        def texts(widget: Any) -> List[str]:
            out = []
            try:
                out.append(str(widget.cget("text")))
            except Exception:
                pass
            for child in widget.winfo_children():
                out.extend(texts(child))
            return out

        def shot(name: str) -> None:
            try:
                im, _ = capture(None)
                im.save(f"{prefix}-{name}.png")
            except Exception as err:
                problems.append(f"foto {name}: {err}")

        def step1() -> None:
            app.pump()
            fits(app.root, "janela")
            app.add_message("📅 Reunião com o time às 15h 👉 sala 2", "bit", [{"label": "✅ Aprovar", "data": "ok:1"}])
            app.notice("⏰ Lembrete: enviar o relatório mensal para a diretoria antes das 18h 📊 (planilha na pasta Vendas)")
            app.notice("🤖 Resumo da manhã: 3 reuniões, 5 e-mails importantes e chuva às 17h ☔")
            for t in app.toasts:
                fits(t, "aviso")
            app.root.update()
            shot("janela")
            app.show_companion()
            app.bar_open_ask(True)
            app.bar_entry.insert(0, "Qual o próximo compromisso?")
            app.ask(app.bar_take_text())
            app.root.after(1200, step2)

        def step2() -> None:
            app.pump()
            app.start_live("responder e-mails")
            app.show_observation(FakeApi("", "").call("POST", "ao-vivo"), region, "Outlook")
            app.bar_open_prepare(True)
            app.prepare("email", "Responder à Ana")
            app.prepare("planilha", "")
            app.root.after(1500, step2b)

        def step2b() -> None:
            app.pump()
            app.root.update()
            result["resultado_email"] = len(app.result_windows)
            result["arquivos_salvos"] = [os.path.basename(p) for p in getattr(app, "saved_paths", [])]
            if not app.result_windows:
                problems.append("janela do e-mail pronto: não abriu")
            else:
                fits(app.result_windows[-1], "e-mail pronto")
            if result["arquivos_salvos"] != ["planilha_orcamento.xlsx"]:
                problems.append(f"planilha não foi salva: {result['arquivos_salvos']}")
            if IS_WINDOWS:
                import ctypes
                for win, name in ((app.bubble, "balão"), (app.badge, "aviso vermelho")):
                    if win is not None and not ctypes.windll.user32.GetWindowLongW(hwnd_of(win), -20) & WS_EX_NOACTIVATE:
                        problems.append(f"{name}: pode roubar o foco (sem WS_EX_NOACTIVATE)")
                if app.overlay is not None and not ctypes.windll.user32.GetWindowLongW(hwnd_of(app.overlay), -20) & WS_EX_TRANSPARENT:
                    problems.append("marcação vermelha: não deixa o clique passar")
            for win, name in ((app.bar, "barra"), (app.bubble, "balao"), (app.badge, "aviso vermelho")):
                if win is None:
                    problems.append(f"{name}: não abriu")
                else:
                    fits(win, name)
            bad = [t for w in [app.root] + list(app.root.winfo_children()) for t in texts(w) if any(ord(c) > 0xFFFF for c in t)]
            if bad:
                problems.append(f"emoji que o Tk não desenha: {bad[:3]}")
            result["janelas"] = sum(1 for w in (app.overlay, app.bar, app.badge, app.bubble) if w is not None)
            app.root.after(400, step3)

        def step3() -> None:
            shot("compacto")
            for w in app.result_windows:
                w.destroy()
            app.stop_live()
            app.show_window()
            result["problemas"] = problems
            result["ok"] = (result["janelas"] == 4 and not problems and result["protect_roundtrip"]
                            and result["link"] is not None)
            app.root.after(300, app.root.destroy)
        app.root.after(900, step1)
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
