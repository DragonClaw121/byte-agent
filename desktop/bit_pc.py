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
import re
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
VERSION = "1.18"
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


CLIENT_ASK_RE = __import__("re").compile(
    r"\b(pesquis\w*|levant\w*|analis\w*|investig\w*|dossie|plano de (conta|abordagem)|conhecer melhor)\b.{0,60}"
    r"\b(cliente|empresa|prospect\w*|cnpj)\b|\b(cliente|empresa)\b.{0,40}\b(pesquis\w*|levant\w*|analis\w*)\b")


def fold(text: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", str(text or "").lower()) if not unicodedata.combining(c))


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


def print_window(hwnd: int) -> Optional[Tuple[Any, Dict[str, int]]]:
    """Fotografa a JANELA em uso pelo próprio Windows (PrintWindow): o que estiver por cima dela, como a barra do Bit,
    não aparece, e nada pisca na tela. Devolve (imagem, região na tela) ou None (janela que não deixa: aí vale o plano B)."""
    if not IS_WINDOWS or not hwnd:
        return None
    import ctypes
    from ctypes import wintypes
    from PIL import Image
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    # identificadores do Windows têm 64 bits: sem declarar, o ctypes os cortaria para 32
    user32.GetDC.restype = gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.CreateCompatibleBitmap.restype = gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.DeleteObject.argtypes = gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.PrintWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
    user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p,
                                ctypes.c_void_p, ctypes.c_uint]
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w < 80 or h < 60 or w * h > 80_000_000:
        return None
    screen_dc = user32.GetDC(0)
    mem_dc = gdi32.CreateCompatibleDC(screen_dc)
    bmp = gdi32.CreateCompatibleBitmap(screen_dc, w, h)
    old = gdi32.SelectObject(mem_dc, bmp)
    try:
        if not user32.PrintWindow(hwnd, mem_dc, 2):                     # PW_RENDERFULLCONTENT (Chrome, Edge, Office)
            return None

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]
        header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        if not gdi32.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(header), 0):
            return None
        img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
    finally:
        gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(0, screen_dc)
    lo, hi = img.convert("L").getextrema()
    if hi - lo < 10:                                                    # tudo preto/branco: a janela não deixou
        return None
    region = {"left": rect.left, "top": rect.top, "width": w, "height": h}
    info = window_info(hwnd)                                            # tira a borda invisível do Windows 10/11
    if info:
        l, t, r, b = info["rect"]
        box = (max(0, l - rect.left), max(0, t - rect.top), min(w, r - rect.left), min(h, b - rect.top))
        if box[2] - box[0] > 80 and box[3] - box[1] > 60:
            img = img.crop(box)
            region = {"left": rect.left + box[0], "top": rect.top + box[1], "width": box[2] - box[0], "height": box[3] - box[1]}
    return img, region


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


def html_to_text(html: str) -> str:
    import html as html_lib
    import re
    text = re.sub(r"(?is)<(style|script|head)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_lib.unescape(text).replace("\xa0", " ")
    lines = [" ".join(line.split()) for line in text.split("\n")]
    out, blank = [], False
    for line in lines:
        if line or not blank:
            out.append(line)
        blank = not line
    return "\n".join(out).strip()


# ---------------- .oft/.msg sem o Outlook clássico: o corpo costuma vir em RTF comprimido com o HTML "encapsulado"
RTF_DICT = (b"{\\rtf1\\ansi\\mac\\deff0\\deftab720{\\fonttbl;}{\\f0\\fnil \\froman \\fswiss \\fmodern \\fscript "
            b"\\fdecor MS Sans SerifSymbolArialTimes New RomanCourier{\\colortbl\\red0\\green0\\blue0\r\n\\par "
            b"\\pard\\plain\\f0\\fs20\\b\\i\\u\\tab\\tx")


def decompress_rtf(data: bytes) -> bytes:
    """PR_RTF_COMPRESSED (MS-OXRTFCP, "LZFu"/"MELA") -> RTF."""
    import struct
    if len(data) < 16:
        return b""
    comp_size, raw_size, magic, _crc = struct.unpack("<IIII", data[:16])
    if magic == 0x414C454D:                                   # "MELA": sem compressão
        return data[16:16 + raw_size]
    if magic != 0x75465A4C:                                   # "LZFu"
        return b""
    window = bytearray(4096)
    window[:len(RTF_DICT)] = RTF_DICT
    pos, out, i, end = len(RTF_DICT), bytearray(), 16, min(len(data), comp_size + 4)
    while i < end:
        flags = data[i]
        i += 1
        for bit in range(8):
            if i >= end:
                break
            if flags & (1 << bit):
                if i + 1 >= end:
                    return bytes(out)
                ref = (data[i] << 8) | data[i + 1]
                i += 2
                offset, length = ref >> 4, (ref & 0xF) + 2
                if offset == pos:
                    return bytes(out)
                for k in range(length):
                    c = window[(offset + k) % 4096]
                    out.append(c)
                    window[pos] = c
                    pos = (pos + 1) % 4096
            else:
                c = data[i]
                i += 1
                out.append(c)
                window[pos] = c
                pos = (pos + 1) % 4096
    return bytes(out)


RTF_SKIP = {"fonttbl", "colortbl", "stylesheet", "info", "pict", "header", "footer", "listtable", "listoverridetable",
            "rsidtbl", "generator", "xmlnstbl", "mmathPr", "themedata", "colorschememapping", "latentstyles", "datastore"}


def rtf_to_html(rtf: str) -> str:
    """O HTML original de dentro do RTF (\\fromhtml1, MS-OXRTFEX). "" se o RTF não carrega HTML."""
    if "\\fromhtml" not in rtf[:2000]:
        return ""
    cp = re.search(r"\\ansicpg(\d+)", rtf[:2000])
    codepage = f"cp{cp.group(1)}" if cp else "cp1252"
    out: List[str] = []
    pending = bytearray()

    def flush() -> None:
        if pending:
            try:
                out.append(pending.decode(codepage, "replace"))
            except LookupError:
                out.append(pending.decode("cp1252", "replace"))
            pending.clear()
    # pilha: (suprimir por \htmlrtf, pular grupo, dentro de htmltag, \ucN)
    stack: List[List[Any]] = []
    state: List[Any] = [False, False, False, 1]
    skip_chars = 0
    i, n = 0, len(rtf)
    token = re.compile(r"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\(.)|([{}])|([^\\{}\r\n]+)|[\r\n]+", re.S)
    while i < n:
        m = token.match(rtf, i)
        if not m:
            i += 1
            continue
        i = m.end()
        word, arg, hexbyte, sym, brace, text = m.groups()
        if brace == "{":
            stack.append(state[:])
            if rtf.startswith("\\*", i):                       # destino opcional: só \*\htmltag interessa
                tag = re.match(r"\\\*\\([a-zA-Z]+)", rtf[i:i + 40])
                if tag and tag.group(1) == "htmltag":
                    state[2] = True
                elif tag and tag.group(1) in ("mhtmltag",):
                    state[1] = True
                else:
                    state[1] = True
            else:
                nxt = re.match(r"\\([a-zA-Z]+)", rtf[i:i + 40])
                if nxt and nxt.group(1) in RTF_SKIP:
                    state[1] = True
            continue
        if brace == "}":
            flush()
            state = stack.pop() if stack else [False, False, False, 1]
            continue
        if state[1]:
            continue
        if word:
            if word == "htmlrtf":
                flush()
                state[0] = arg != "0"
            elif word == "uc":
                state[3] = int(arg or 1)
            elif state[0]:
                continue
            elif word == "u":
                flush()
                code = int(arg or 0)
                out.append(chr(code + 65536 if code < 0 else code))
                skip_chars = state[3]
            elif word == "par":
                flush()
                out.append("\r\n")
            elif word == "tab":
                flush()
                out.append("\t")
            elif word == "line":
                flush()
                out.append("<br>")
            elif word == "htmltag" or word == "fromhtml":
                pass
            continue
        if state[0]:
            continue
        if hexbyte:
            if skip_chars:
                skip_chars -= 1
                continue
            pending.append(int(hexbyte, 16))
            continue
        flush()
        if sym:
            if skip_chars:
                skip_chars -= 1
                continue
            out.append({"~": "\xa0", "_": "-"}.get(sym, sym if sym in "\\{}" else ""))
        elif text:
            if skip_chars:
                cut = min(skip_chars, len(text))
                text, skip_chars = text[cut:], skip_chars - cut
            out.append(text)
    flush()
    html = "".join(out)
    return html if "<" in html else ""


def read_outlook_template(path: str) -> Optional[Dict[str, str]]:
    """Um .oft/.msg lido direto do arquivo (sem o Outlook clássico): assunto, texto, HTML (do PR_HTML ou de dentro do RTF
    comprimido) e as imagens internas (anexos cid:)."""
    try:
        import olefile                                        # pip install olefile (o instalador já instala)
    except ImportError:
        return None
    try:
        ole = olefile.OleFileIO(path)
    except Exception:
        return None
    try:
        def text(prop: str, prefix: str = "") -> str:
            for suffix, enc in (("001F", "utf-16-le"), ("001E", "cp1252")):
                name = f"{prefix}__substg1.0_{prop}{suffix}"
                if ole.exists(name):
                    return ole.openstream(name).read().decode(enc, "replace").rstrip("\x00").strip()
            return ""
        subject, body, html = text("0037"), text("1000"), ""
        if ole.exists("__substg1.0_10130102"):                # PR_HTML
            raw = ole.openstream("__substg1.0_10130102").read()
            charset = re.search(rb"(?i)charset=[\"']?([\w-]+)", raw[:3000])
            try:
                html = raw.decode(charset.group(1).decode() if charset else "utf-8")
            except (LookupError, UnicodeDecodeError):
                html = raw.decode("cp1252", "replace")
            html = html.rstrip("\x00")
        if "<" not in html and ole.exists("__substg1.0_10090102"):    # PR_RTF_COMPRESSED com o HTML dentro
            try:
                rtf = decompress_rtf(ole.openstream("__substg1.0_10090102").read()).decode("latin-1")
                html = rtf_to_html(rtf)
            except Exception:
                html = ""
        images = []
        for entry in ole.listdir(streams=False, storages=True):
            folder = entry[0] if entry else ""
            if not folder.startswith("__attach_version1.0_#") or len(entry) != 1:
                continue
            cid = text("3712", folder + "/")
            data_name = f"{folder}/__substg1.0_37010102"
            if cid and ole.exists(data_name):
                images.append({"cid": cid, "nome": text("3707", folder + "/") or text("3704", folder + "/"),
                               "mime": text("370E", folder + "/"),
                               "dados": base64.b64encode(ole.openstream(data_name).read()).decode()})
        body = body or html_to_text(html)
    finally:
        ole.close()
    if len(body.strip()) < 10:
        return None
    name = os.path.splitext(os.path.basename(path))[0]
    if "<" in html:
        html = cid_to_data(html, [dict(img, mime=img["mime"] if str(img["mime"]).startswith("image/") else "") for img in images])
    return {"nome": name[:80], "assunto": subject[:200], "corpo": body.replace("\r\n", "\n")[:8000], "uso": "",
            "html": html if "<" in html else ""}


OLD_SERVER_MSG = ("⚠ O servidor do Bit ainda está na versão antiga e guardou só o texto. Ele se atualiza sozinho em "
                  "alguns minutos (ou mande /atualizar para o Bit); depois, importe ou cole de novo.")


# ---------------- imagens do e-mail (logo, banner): embutidas no modelo, viram anexos "cid:" ao abrir no Outlook
IMG_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".bmp": "image/bmp"}
DATA_IMG_RE = re.compile(r"data:(image/[\w.+-]+);base64,([A-Za-z0-9+/=\s]+)")


def inline_local_images(html: str, base_dir: str = "") -> str:
    """Imagens que apontam para arquivos do PC (o Outlook copia assim; a assinatura .htm usa a pasta *_files) viram
    data: embutido, para o modelo continuar com elas depois."""
    def repl(m: "re.Match[str]") -> str:
        src = urllib.parse.unquote(m.group(2))
        if src.lower().startswith("file:"):
            path = urllib.request.url2pathname(src[5:].lstrip("/") if IS_WINDOWS else src[5:].replace("//", "", 1))
        elif re.match(r"^[a-zA-Z]:[\\/]", src) or (base_dir and not re.match(r"^[a-z]+:", src, re.I)):
            path = src if re.match(r"^[a-zA-Z]:[\\/]", src) else os.path.join(base_dir, src)
        else:
            return m.group(0)
        mime = IMG_TYPES.get(os.path.splitext(path)[1].lower())
        try:
            if not mime or os.path.getsize(path) > 1_500_000:
                return m.group(0)
            with open(path, "rb") as fh:
                data = base64.b64encode(fh.read()).decode()
        except OSError:
            return m.group(0)
        return f'{m.group(1)}"data:{mime};base64,{data}"'
    return re.sub(r"""(?i)(\bsrc=)["']([^"']+)["']""", repl, str(html or ""))


def split_images(html: str, prefix: str = "bitimg") -> Tuple[str, List[Dict[str, Any]]]:
    """data: embutido -> cid:..., com a lista das imagens (para virarem anexos internos do e-mail)."""
    images: List[Dict[str, Any]] = []

    def repl(m: "re.Match[str]") -> str:
        mime = m.group(1)
        ext = {"image/jpeg": ".jpg", "image/gif": ".gif", "image/bmp": ".bmp"}.get(mime, ".png")
        cid = f"{prefix}{len(images) + 1}"
        images.append({"cid": cid, "mime": mime, "nome": cid + ext, "dados": base64.b64decode(re.sub(r"\s", "", m.group(2)))})
        return "cid:" + cid
    return DATA_IMG_RE.sub(repl, str(html or "")), images


# ---------------- imagens transparentes (v1.11): o Outlook novo (e vários outros) pinta o transparente de branco
_NAMED = {"white": (255, 255, 255), "black": (0, 0, 0), "transparent": None}


def parse_color(value: str) -> Optional[Tuple[int, int, int]]:
    v = str(value or "").strip().lower()
    if v in _NAMED:
        return _NAMED[v]
    m = re.search(r"#([0-9a-f]{6}|[0-9a-f]{3})\b", v)
    if m:
        h = m.group(1)
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    m = re.search(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", v)
    if m:
        return tuple(min(255, int(x)) for x in m.groups())  # type: ignore[return-value]
    return None


def css_backgrounds(html: str) -> Dict[str, Tuple[int, int, int]]:
    """Cores de fundo definidas em <style> por seletores simples: '.classe', 'td', 'td.classe', '#id'."""
    out: Dict[str, Tuple[int, int, int]] = {}
    for block in re.findall(r"(?is)<style[^>]*>(.*?)</style>", str(html or "")):
        block = re.sub(r"(?s)/\*.*?\*/", "", block)
        for sels, body in re.findall(r"([^{}@]+)\{([^{}]*)\}", block):
            m = re.search(r"(?i)background(?:-color)?\s*:\s*([^;]+)", body)
            color = parse_color(m.group(1)) if m else None
            if color is None:
                continue
            for sel in sels.split(","):
                sel = sel.strip().lower()
                if re.fullmatch(r"[a-z0-9]*(?:[.#][\w-]+)?", sel) and sel:
                    out[sel] = color
    return out


IMG_SRC_RE = re.compile(r"""(?i)(<img\b[^>]*?\bsrc\s*=\s*)(["'])([^"']+)\2""")


def image_backgrounds(html: str) -> List[Optional[Tuple[int, int, int]]]:
    """Para cada <img> (na ordem do HTML), a cor de fundo do bloco onde ela está: bgcolor, style background e as
    classes/etiquetas/ids do <style> (v1.13)."""
    from html.parser import HTMLParser
    voids = {"img", "br", "hr", "meta", "link", "input", "col", "area", "base", "source", "wbr"}
    css = css_backgrounds(html)

    class P(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.stack: List[Tuple[str, Optional[Tuple[int, int, int]]]] = []
            self.found: List[Optional[Tuple[int, int, int]]] = []

        def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
            a = {k.lower(): (v or "") for k, v in attrs}
            color = None
            for cls in a.get("class", "").lower().split():
                color = css.get(f"{tag}.{cls}") or css.get(f".{cls}") or color
            if a.get("id"):
                color = css.get(f"#{a['id'].lower()}") or color
            color = color or css.get(tag)
            color = parse_color(a.get("bgcolor", "")) or color
            m = re.search(r"(?i)background(?:-color)?\s*:\s*([^;]+)", a.get("style", ""))
            if m and parse_color(m.group(1)) is not None:
                color = parse_color(m.group(1))
            if tag == "img":
                self.found.append(next((c for _, c in reversed(self.stack) if c is not None), None))
                return
            # v1.16: etiquetas que o HTML do Outlook deixa sem fechar (<td>, <tr>, <p>, <li>): uma nova fecha a anterior,
            # como no navegador — senão a cor de uma célula antiga "vazava" para a imagem seguinte
            limits = {"td": ("td", "th"), "th": ("td", "th"), "tr": ("tr",), "p": ("p",), "li": ("li",)}
            if tag in limits:
                walls = {"td": ("tr", "table"), "th": ("tr", "table"), "tr": ("table",), "p": ("td", "th", "div", "table"),
                         "li": ("ul", "ol")}[tag]
                for k in range(len(self.stack) - 1, -1, -1):
                    name = self.stack[k][0]
                    if name in walls:
                        break
                    if name in limits[tag]:                           # (para <tr>, sobe até o <tr> anterior)
                        del self.stack[k:]
                        break
            if tag not in voids:
                self.stack.append((tag, color))

        def handle_endtag(self, tag: str) -> None:
            for i in range(len(self.stack) - 1, -1, -1):
                if self.stack[i][0] == tag:
                    del self.stack[i:]
                    break
    p = P()
    try:
        p.feed(str(html or ""))
    except Exception:
        return []
    return p.found


def _light(color: Tuple[int, int, int]) -> bool:
    r, g, b = color
    return 0.299 * r + 0.587 * g + 0.114 * b >= 200


def _dist(a: Tuple[int, ...], b: Tuple[int, ...]) -> int:
    return max(abs(int(x) - int(y)) for x, y in zip(a[:3], b[:3]))


def border_color(im: Any) -> Optional[Tuple[int, int, int]]:
    """A cor da moldura da imagem, se ela for (quase) toda de uma cor só — senão None."""
    rgb = im.convert("RGB")
    w, h = rgb.size
    if w < 4 or h < 4:
        return None
    px = rgb.load()
    border = [px[x, y] for x in range(w) for y in (0, h - 1)] + [px[x, y] for y in range(h) for x in (0, w - 1)]
    counts: Dict[Tuple[int, int, int], int] = {}
    for c in border:
        key = (c[0] // 8 * 8, c[1] // 8 * 8, c[2] // 8 * 8)
        counts[key] = counts.get(key, 0) + 1
    key = max(counts, key=counts.get)                              # type: ignore[arg-type]
    near = [c for c in border if _dist(c, key) <= 14]
    if len(near) < 0.85 * len(border):
        return None
    return tuple(sum(c[i] for c in near) // len(near) for i in range(3))  # type: ignore[return-value]


def looks_like_logo(im: Any) -> bool:
    """Logo, ícone ou botão (poucas cores) — e não foto ou print de tela, que nunca têm o fundo removido."""
    small = im.convert("RGB").resize((96, 96))
    return len(set(small.getdata())) <= 900                        # type: ignore[arg-type]


def _alpha_from(c: Tuple[int, ...], bg: Tuple[int, int, int]) -> float:
    """Quanto do pixel NÃO é o fundo ("cor para alfa", como no GIMP)."""
    out = 0.0
    for ci, bi in zip(c[:3], bg):
        if ci > bi:
            out = max(out, (ci - bi) / float(255 - bi))
        elif ci < bi:
            out = max(out, (bi - ci) / float(bi))
    return out


def remove_background(im: Any, bg: Tuple[int, int, int], low: int = 24, rings: int = 2) -> Any:
    """v1.17: remove o fundo sólido (ligado às bordas) de uma imagem SEM transparência: vira transparente de verdade.
    A borda suavizada do desenho (até 2 px) fica semitransparente, com a cor do fundo antigo descontada (sem halo).
    O que é da cor do fundo mas fica DENTRO do desenho (o miolo branco de um "o") não muda."""
    rgba = im.convert("RGBA")
    w, h = rgba.size
    px = rgba.load()
    removed = bytearray(w * h)
    seen = bytearray(w * h)
    stack = [(x, y) for x in range(w) for y in (0, h - 1)] + [(x, y) for y in range(h) for x in (0, w - 1)]
    edge: List[Tuple[int, int]] = []
    while stack:
        x, y = stack.pop()
        k = y * w + x
        if seen[k]:
            continue
        seen[k] = 1
        p = px[x, y]
        if _dist(p, bg) > low:
            edge.append((x, y))                                     # já é o desenho (ou a borda suavizada dele)
            continue
        removed[k] = 1
        px[x, y] = (p[0], p[1], p[2], 0)
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < w and 0 <= ny < h and not seen[ny * w + nx]:
                stack.append((nx, ny))
    for _ in range(rings):                                           # a borda suavizada, de fora para dentro
        nxt: List[Tuple[int, int]] = []
        for x, y in edge:
            k = y * w + x
            if removed[k] == 2 or not any(0 <= nx < w and 0 <= ny < h and removed[ny * w + nx] for nx, ny in
                                          ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1))):
                continue                                             # só o que encosta no fundo removido
            p = px[x, y]
            a = _alpha_from(p, bg)
            removed[k] = 2
            if a >= 0.85:
                continue                                             # o desenho de verdade: intocado
            a = max(a, 1 / 255.0)
            px[x, y] = tuple(max(0, min(255, int(round((c - b) / a + b)))) for c, b in zip(p[:3], bg)) + (
                int(round(a * 255)),)                                # type: ignore[assignment]
            for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if 0 <= nx < w and 0 <= ny < h and not removed[ny * w + nx]:
                    nxt.append((nx, ny))
        edge = nxt
    return rgba


MAX_EMAIL_IMAGE = 1200


def fit_for_email(im: Any, limit: int = MAX_EMAIL_IMAGE) -> Any:
    """Imagem gigante (o logo do modelo tinha 8000 px) diminuída para o dobro do tamanho em que aparece no e-mail
    (nítida em tela de alta resolução), no máximo 1200 px, com o alfa pré-multiplicado (sem franja na borda)."""
    w, h = im.size
    if max(w, h) <= limit:
        return im
    scale = limit / float(max(w, h))
    from PIL import Image
    size = (max(1, int(w * scale)), max(1, int(h * scale)))
    return im.convert("RGBa").resize(size, Image.LANCZOS).convert("RGBA")


def display_limits(html: str) -> List[int]:
    """Para cada <img> com src (na ordem), o maior lado aceitável: 2x a largura em que ela aparece, se o modelo disser."""
    out: List[int] = []
    for tag in re.findall(r"(?is)<img\b[^>]*>", str(html or "")):
        if not IMG_SRC_RE.search(tag):
            continue
        m = re.search(r"""(?i)\bwidth\s*=\s*["']?(\d{2,4})(?:px)?["'\s>/]""", tag) or \
            re.search(r"(?i)[\s;\"']width\s*:\s*(\d{2,4})(?:\.\d+)?px", tag)
        out.append(min(MAX_EMAIL_IMAGE, 2 * int(m.group(1))) if m and int(m.group(1)) >= 16 else MAX_EMAIL_IMAGE)
    return out


LAST_IMAGES: List[Dict[str, Any]] = []


def _fetch_image(url: str) -> Optional[bytes]:
    """Imagem que o modelo busca na internet (logo hospedado): baixa para poder ajustar o fundo (até 2 MB)."""
    if not url.lower().startswith("https://"):
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": f"BitPC/{VERSION}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            if not str(resp.headers.get("Content-Type") or "").lower().startswith("image/"):
                return None
            data = resp.read(2_000_001)
        return data if len(data) <= 2_000_000 else None
    except Exception:
        return None


BROWSER_PROBE_JS = """<script>(function(){var out=[];var imgs=document.getElementsByTagName('img');
for(var i=0;i<imgs.length;i++){var el=imgs[i].parentElement,c='';while(el){var s=getComputedStyle(el).backgroundColor;
if(s&&s!=='transparent'&&!/rgba\\([^)]*,\\s*0\\)$/.test(s)){c=s;break;}el=el.parentElement;}out.push(c);}
var p=document.createElement('pre');p.id='bit-fundos';p.textContent='BITFUNDOS'+JSON.stringify(out)+'FIMFUNDOS';
document.documentElement.appendChild(p);})();</script>"""
PIXEL_GIF = "data:image/gif;base64,R0lGODlhAQABAAAAACw="


def find_browser() -> str:
    """O Edge (vem em todo Windows 10/11) ou o Chrome, para medir as cores como o navegador desenha."""
    if not IS_WINDOWS:
        return ""
    bases = [os.environ.get(k, "") for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    for base in bases:
        for rel in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
            path = os.path.join(base, rel) if base else ""
            if path and os.path.isfile(path):
                return path
    return ""


def browser_backgrounds(html: str, browser: str = "") -> Optional[List[Optional[Tuple[int, int, int]]]]:
    """v1.16: a cor de fundo REAL atrás de cada <img>, medida pelo navegador (Edge sem janela): vale para estilos,
    classes, <table bgcolor> e o HTML "torto" do Outlook (etiquetas sem fechar), que a leitura simples erra. As imagens
    viram um pixel vazio na cópia medida (nada sai do PC). None se não houver navegador ou ele não responder."""
    import tempfile
    browser = browser or find_browser()
    if not browser:
        return None
    probe = IMG_SRC_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{PIXEL_GIF}{m.group(2)}", str(html or ""))
    probe = re.sub(r"(?is)<script.*?</script>", "", probe)
    probe = re.sub(r"(?i)</body>", lambda _m: BROWSER_PROBE_JS + "</body>", probe, count=1) if re.search(r"(?i)</body>", probe) \
        else probe + BROWSER_PROBE_JS
    folder = tempfile.mkdtemp(prefix="bitpc-fundo-")
    path = os.path.join(folder, "email.html")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(probe if re.search(r"(?i)<meta[^>]+charset", probe) else '<meta charset="utf-8">' + probe)
        extra = ["--no-sandbox"] if not sys.platform.startswith("win") and hasattr(os, "geteuid") and os.geteuid() == 0 else []
        proc = subprocess.run([browser, "--headless", "--disable-gpu", "--no-first-run", "--disable-extensions",
                               "--disable-background-networking", "--disable-component-update", "--disable-sync",
                               "--no-default-browser-check", "--mute-audio"] + extra + [
                               f"--user-data-dir={os.path.join(folder, 'perfil')}", "--dump-dom",
                               "file:///" + path.replace("\\", "/").lstrip("/")],
                              capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (proc.stdout or b"").decode("utf-8", "replace")
        m = re.search(r"BITFUNDOS(\[.*?\])FIMFUNDOS", out, re.S)
        if not m:
            return None
        return [parse_color(c) if c else None for c in json.loads(m.group(1).replace("&quot;", '"'))]
    except Exception:
        return None
    finally:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)


def transparent_images(html: str, fetch: Optional[Callable[[str], Optional[bytes]]] = None,
                       backgrounds: Optional[Callable[[str], Optional[List[Optional[Tuple[int, int, int]]]]]] = None) -> str:
    """v1.17: as imagens do e-mail ficam com o fundo TRANSPARENTE — em qualquer programa e no modo claro ou escuro, o que
    aparece atrás delas é o fundo do próprio e-mail:
      - PNG/GIF que já têm transparência: a transparência é mantida (nada é pintado por cima);
      - imagem com fundo sólido (a caixa branca de um logo "achatado" pelo Outlook/Word, ou um fundo da cor do bloco):
        o fundo ligado às bordas é removido. Só em logos, ícones e botões; foto e print de tela não mudam. Um botão
        de cor diferente do bloco onde está também não muda (o fundo dele é o próprio botão);
      - imagens gigantes são diminuídas para 1200 px (PNG com alfa).
    O diagnóstico (sem as imagens) fica em LAST_IMAGES e em %APPDATA%\\BitPC\\imagens.log."""
    try:
        from PIL import Image
    except ImportError:
        return html
    fetch = fetch or _fetch_image
    n_imgs = len(IMG_SRC_RE.findall(str(html or "")))
    limits = display_limits(html)
    blocks: Dict[str, Any] = {}

    def block_color(i: int) -> Optional[Tuple[int, int, int]]:
        if "cores" not in blocks:                                  # só mede (no Edge) se alguma imagem precisar
            measured = (backgrounds or browser_backgrounds)(html)
            blocks["cores"] = measured if measured is not None and len(measured) == n_imgs else image_backgrounds(html)
            blocks["como"] = "navegador" if blocks["cores"] is measured else "leitura do HTML"
        cores = blocks["cores"]
        return cores[i] if i < len(cores) else None
    count = [0]
    report: List[Dict[str, Any]] = []

    def encoded(prefix: str, quote: str, im: Any) -> str:
        out = io.BytesIO()
        im.save(out, "PNG", optimize=True)
        return f"{prefix}{quote}data:image/png;base64,{base64.b64encode(out.getvalue()).decode('ascii')}{quote}"

    def repl(m: "re.Match[str]") -> str:
        i = count[0]
        count[0] += 1
        prefix, quote, src = m.group(1), m.group(2), m.group(3)
        info: Dict[str, Any] = {"n": i + 1, "origem": "embutida" if src.startswith("data:") else
                                ("internet" if src.lower().startswith("http") else "outra")}
        report.append(info)
        try:
            if src.startswith("data:"):
                dm = DATA_IMG_RE.match(src)
                raw = base64.b64decode(re.sub(r"\s", "", dm.group(2))) if dm else b""
            else:
                raw = fetch(src) or b""
            if not raw:
                info["resultado"] = "não consegui ler a imagem: mantida"
                return m.group(0)
            im = Image.open(io.BytesIO(raw))
            info["formato"], info["tamanho"] = im.format, list(im.size)
            if getattr(im, "n_frames", 1) > 1:
                info["resultado"] = "GIF animado: mantida"
                return m.group(0)
            rgba = im.convert("RGBA")
            limit = limits[i] if i < len(limits) else MAX_EMAIL_IMAGE
            big = max(rgba.size) > limit
            fit = lambda x: fit_for_email(x, limit)                # noqa: E731
            if rgba.getchannel("A").getextrema()[0] < 255:
                info["resultado"] = "já transparente: mantida" + (" (diminuída)" if big else "")
                if not big and im.format in ("PNG", "GIF"):
                    return m.group(0)
                return encoded(prefix, quote, fit(rgba))
            bg = border_color(rgba)
            if bg is None and looks_like_logo(rgba):               # botão arredondado que perdeu a transparência:
                corners = [rgba.getpixel(p)[:3] for p in ((0, 0), (rgba.width - 1, 0), (0, rgba.height - 1),
                                                          (rgba.width - 1, rgba.height - 1))]
                if all(min(c) >= 235 for c in corners):           # só os cantos brancos saem
                    white = tuple(sum(c[k] for c in corners) // 4 for k in range(3))
                    info["resultado"] = "cantos brancos removidos (transparente)"
                    return encoded(prefix, quote, remove_background(fit(rgba), white))  # type: ignore[arg-type]
            if bg is None or not looks_like_logo(rgba):
                info["resultado"] = "foto ou sem fundo liso: mantida"
                return m.group(0) if not big else encoded(prefix, quote, fit(rgba))
            info["fundo_da_imagem"] = list(bg)
            near_white = min(bg) >= 235                            # a caixa branca típica: sai sempre
            block = None if near_white else block_color(i)
            if not near_white and (block is None or _dist(bg, block) > 40):
                info["resultado"] = "botão/fundo próprio (cor diferente do bloco): mantida"
                info["bloco"], info["medido_por"] = block, blocks.get("como")
                return m.group(0) if not big else encoded(prefix, quote, fit(rgba))
            info["resultado"] = "fundo removido (transparente)"
            return encoded(prefix, quote, remove_background(fit(rgba), bg))
        except Exception as err:
            info["resultado"] = f"erro: {type(err).__name__}"
            return m.group(0)
    out_html = IMG_SRC_RE.sub(repl, str(html or ""))
    LAST_IMAGES.clear()
    LAST_IMAGES.extend(report)
    if report:
        try:
            with open(os.path.join(config_dir(), "imagens.log"), "a", encoding="utf-8") as fh:
                fh.write(time.strftime("%Y-%m-%d %H:%M:%S v") + VERSION + " " + json.dumps(report, ensure_ascii=False) + "\n")
        except OSError:
            pass
    return out_html


def images_summary() -> str:
    """Uma linha para a janela do e-mail: o que foi feito com as imagens."""
    if not LAST_IMAGES:
        return ""
    removed = sum(1 for i in LAST_IMAGES if "removido" in str(i.get("resultado")))
    kept = sum(1 for i in LAST_IMAGES if "já transparente" in str(i.get("resultado")))
    text = f"Imagens: {len(LAST_IMAGES)}"
    if kept:
        text += f", {kept} com a transparência original"
    if removed:
        text += f", {removed} com o fundo removido"
    if removed or any("foto ou sem fundo" in str(i.get("resultado")) for i in LAST_IMAGES):
        text += " Se alguma imagem perdeu a transparência no modelo, troque pela original em Modelos → Imagens do modelo"
    return text + "."


def _decode_src(src: str, fetch: Optional[Callable[[str], Optional[bytes]]] = None) -> bytes:
    if src.startswith("data:"):
        dm = DATA_IMG_RE.match(src)
        return base64.b64decode(re.sub(r"\s", "", dm.group(2))) if dm else b""
    return (fetch or _fetch_image)(src) or b"" if src.lower().startswith("https://") else b""


def template_images(html: str, fetch: Optional[Callable[[str], Optional[bytes]]] = None) -> List[Dict[str, Any]]:
    """v1.18: as imagens de um modelo, com o diagnóstico de cada uma (para trocar a "achatada" pela original)."""
    from PIL import Image
    out: List[Dict[str, Any]] = []
    for i, m in enumerate(IMG_SRC_RE.finditer(str(html or ""))):
        src = m.group(3)
        item: Dict[str, Any] = {"n": i + 1, "origem": "embutida" if src.startswith("data:") else
                                ("internet" if src.lower().startswith("http") else "outra"), "png": b""}
        try:
            raw = _decode_src(src, fetch)
            if not raw:
                item["estado"] = "não consegui ler a imagem"
                out.append(item)
                continue
            im = Image.open(io.BytesIO(raw))
            item["formato"], item["tamanho"] = im.format, list(im.size)
            rgba = im.convert("RGBA")
            if rgba.getchannel("A").getextrema()[0] < 255:
                item["estado"], item["ok"] = "✔ fundo transparente", True
            elif border_color(rgba) is not None and looks_like_logo(rgba):
                item["estado"], item["ok"] = "⚠ SEM transparência (fundo liso): troque pela original", False
            else:
                corners = [rgba.getpixel(p) for p in ((0, 0), (rgba.width - 1, 0), (0, rgba.height - 1),
                                                     (rgba.width - 1, rgba.height - 1))]
                flat_corners = all(min(c[:3]) >= 235 for c in corners) and looks_like_logo(rgba)
                item["estado"] = ("⚠ cantos brancos (perdeu a transparência?): troque pela original" if flat_corners
                                  else "sem transparência (foto ou imagem cheia)")
                item["ok"] = not flat_corners
            thumb = rgba.copy()
            thumb.thumbnail((360, 90))
            board = Image.new("RGBA", thumb.size, (32, 32, 32, 255))     # fundo escuro: o "quadro branco" aparece
            board.alpha_composite(thumb)
            buf = io.BytesIO()
            board.convert("RGB").save(buf, "PNG")
            item["png"] = buf.getvalue()
        except Exception as err:
            item["estado"] = f"erro: {type(err).__name__}"
        out.append(item)
    return out


def replace_image(html: str, index: int, raw: bytes) -> str:
    """Troca a imagem nº index (0, 1, ...) do modelo por outra (PNG, WebP, GIF ou JPG), salva como PNG com transparência,
    no tamanho certo para e-mail. O resto do HTML não muda."""
    from PIL import Image
    im = Image.open(io.BytesIO(raw))
    im.load()
    limits = display_limits(html)
    rgba = fit_for_email(im.convert("RGBA"), limits[index] if index < len(limits) else MAX_EMAIL_IMAGE)
    buf = io.BytesIO()
    rgba.save(buf, "PNG", optimize=True)
    new_src = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    count = [0]

    def repl(m: "re.Match[str]") -> str:
        k = count[0]
        count[0] += 1
        return f"{m.group(1)}{m.group(2)}{new_src}{m.group(2)}" if k == index else m.group(0)
    out = IMG_SRC_RE.sub(repl, str(html or ""))
    if count[0] <= index:
        raise ValueError("Essa imagem não existe no modelo.")
    return out


def images_to_files(html: str) -> str:
    """data: embutido -> arquivos temporários (file:///...): é assim que o Outlook aceita imagens ao colar."""
    import tempfile
    out, images = split_images(html)
    if not images:
        return out
    folder = os.path.join(tempfile.gettempdir(), "BitPC", f"img-{int(time.time())}")
    os.makedirs(folder, exist_ok=True)
    for img in reversed(images):                          # do fim: bitimg10 antes de bitimg1
        path = os.path.join(folder, img["nome"])
        with open(path, "wb") as fh:
            fh.write(img["dados"])
        out = out.replace("cid:" + img["cid"], "file:///" + path.replace("\\", "/"))
    return out


# ---------------- área de transferência com formato (HTML), como o Outlook copia e cola
def _clip_api() -> Tuple[Any, Any, int]:
    import ctypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.restype = kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = kernel32.GlobalUnlock.argtypes = kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
    kernel32.GlobalSize.restype = ctypes.c_size_t
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    return user32, kernel32, user32.RegisterClipboardFormatW("HTML Format")


def clipboard_get_html() -> str:
    """O HTML que o Outlook (ou o navegador) colocou na área de transferência ao copiar; "" se só houver texto."""
    if not IS_WINDOWS:
        return ""
    import ctypes
    import re
    user32, kernel32, fmt = _clip_api()
    for _ in range(5):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return ""
    try:
        handle = user32.GetClipboardData(fmt)
        if not handle:
            return ""
        ptr = kernel32.GlobalLock(handle)
        data = ctypes.string_at(ptr, kernel32.GlobalSize(handle))
        kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()
    head = data[:400].decode("ascii", "replace")
    start, end = re.search(r"StartHTML:(-?\d+)", head), re.search(r"EndHTML:(-?\d+)", head)
    if start and end and int(start.group(1)) >= 0:
        chunk = data[int(start.group(1)):int(end.group(1))]
    else:
        fs, fe = re.search(r"StartFragment:(\d+)", head), re.search(r"EndFragment:(\d+)", head)
        chunk = data[int(fs.group(1)):int(fe.group(1))] if fs and fe else b""
    return chunk.split(b"\0", 1)[0].decode("utf-8", "replace").strip()


def clipboard_set_html(html: str, text: str) -> bool:
    """Copia COM formato: colar (Ctrl+V) no Outlook mantém tabelas, cores e imagens. Leva o texto simples junto."""
    if not IS_WINDOWS:
        return False
    import ctypes
    import re
    user32, kernel32, fmt = _clip_api()
    if re.search(r"(?i)<body[^>]*>", html):
        doc = re.sub(r"(?i)(<body[^>]*>)", r"\1<!--StartFragment-->", html, count=1)
        doc = re.sub(r"(?i)</body>", "<!--EndFragment--></body>", doc, count=1) if re.search(r"(?i)</body>", doc) \
            else doc + "<!--EndFragment-->"
    else:
        doc = f"<html><body><!--StartFragment-->{html}<!--EndFragment--></body></html>"
    template = "Version:0.9\r\nStartHTML:{:010d}\r\nEndHTML:{:010d}\r\nStartFragment:{:010d}\r\nEndFragment:{:010d}\r\n"
    header_len = len(template.format(0, 0, 0, 0).encode("ascii"))
    body = doc.encode("utf-8")
    frag_start = header_len + body.index(b"<!--StartFragment-->") + len(b"<!--StartFragment-->")
    frag_end = header_len + body.index(b"<!--EndFragment-->")
    payload = template.format(header_len, header_len + len(body), frag_start, frag_end).encode("ascii") + body + b"\0"

    def put(fmt_id: int, raw: bytes) -> None:
        handle = kernel32.GlobalAlloc(0x0002, len(raw))
        ptr = kernel32.GlobalLock(handle)
        ctypes.memmove(ptr, raw, len(raw))
        kernel32.GlobalUnlock(handle)
        user32.SetClipboardData(fmt_id, handle)
    for _ in range(5):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return False
    try:
        user32.EmptyClipboard()
        put(fmt, payload)
        put(13, (text or "").encode("utf-16-le") + b"\0\0")              # CF_UNICODETEXT
    finally:
        user32.CloseClipboard()
    return True


# ---------------- o Outlook do próprio PC (clássico), pelo PowerShell: lê .oft/.msg com o HTML e abre e-mail novo
def _powershell(script: str, data: Any, timeout: float = 90.0) -> Any:
    import tempfile
    folder = tempfile.mkdtemp(prefix="bitpc-")
    src, out, ps1 = (os.path.join(folder, n) for n in ("in.json", "out.json", "run.ps1"))
    with open(src, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    with open(ps1, "w", encoding="utf-8-sig") as fh:
        fh.write(script)
    try:
        proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", ps1,
                               "-src", src, "-out", out], capture_output=True, timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if proc.returncode != 0 or not os.path.exists(out):
            raise RuntimeError((proc.stderr or proc.stdout or b"").decode("utf-8", "replace")[-300:] or "Outlook indisponível")
        with open(out, encoding="utf-8-sig") as fh:
            return json.load(fh)
    finally:
        for f in (src, out, ps1):
            try:
                os.remove(f)
            except OSError:
                pass
        try:
            os.rmdir(folder)
        except OSError:
            pass


OUTLOOK_READ_PS = r"""param($src, $out)
$ErrorActionPreference = 'Stop'
$o = New-Object -ComObject Outlook.Application
$dir = Split-Path $out
$cidProp = 'http://schemas.microsoft.com/mapi/proptag/0x3712001F'
$res = @()
foreach ($p in @(Get-Content -Raw -Encoding UTF8 $src | ConvertFrom-Json)) {
  try {
    if ($p.ToLower().EndsWith('.oft')) { $m = $o.CreateItemFromTemplate($p) } else { $m = $o.Session.OpenSharedItem($p) }
    $html = $m.HTMLBody
    $imgs = @()
    foreach ($a in $m.Attachments) {
      $cid = ''
      try { $cid = $a.PropertyAccessor.GetProperty($cidProp) } catch { }
      if ($cid -and $html.Contains('cid:' + $cid)) {
        $f = Join-Path $dir ([guid]::NewGuid().ToString('N') + [IO.Path]::GetExtension($a.FileName))
        $a.SaveAsFile($f)
        $imgs += [pscustomobject]@{cid = $cid; nome = $a.FileName; dados = [Convert]::ToBase64String([IO.File]::ReadAllBytes($f))}
        Remove-Item $f -Force
      }
    }
    $res += [pscustomobject]@{arquivo = $p; assunto = $m.Subject; html = $html; texto = $m.Body; imagens = @($imgs)}
    $m.Close(1)
  } catch { $res += [pscustomobject]@{arquivo = $p; erro = $_.Exception.Message} }
}
ConvertTo-Json -InputObject @($res) -Depth 4 -Compress | Set-Content -Encoding UTF8 $out
"""

OUTLOOK_NEW_PS = r"""param($src, $out)
$ErrorActionPreference = 'Stop'
$d = Get-Content -Raw -Encoding UTF8 $src | ConvertFrom-Json
$o = New-Object -ComObject Outlook.Application
$m = $o.CreateItem(0)
if ($d.para) { $m.To = $d.para }
$m.Subject = $d.assunto
foreach ($i in @($d.imagens)) {
  if (-not $i) { continue }
  $a = $m.Attachments.Add($i.arquivo, 1, 0)
  $a.PropertyAccessor.SetProperty('http://schemas.microsoft.com/mapi/proptag/0x3712001F', $i.cid)
  $a.PropertyAccessor.SetProperty('http://schemas.microsoft.com/mapi/proptag/0x7FFE000B', $true)
}
$m.HTMLBody = $d.html
$m.Display()
'{"ok": true}' | Set-Content -Encoding UTF8 $out
"""


def cid_to_data(html: str, images: List[Dict[str, Any]]) -> str:
    """As imagens internas (anexos cid:) do .oft/.msg/.eml voltam para dentro do HTML (data:)."""
    for img in images or []:
        cid, data = str(img.get("cid") or "").strip("<>"), str(img.get("dados") or "")
        if not cid or not data:
            continue
        mime = img.get("mime") or IMG_TYPES.get(os.path.splitext(str(img.get("nome") or ""))[1].lower(), "image/png")
        html = re.sub(r"(?i)cid:" + re.escape(cid) + r"(?=[\"'\s)>])", f"data:{mime};base64,{data}", html)
    return html


def outlook_read(paths: List[str]) -> List[Dict[str, str]]:
    """Abre .oft/.msg pelo Outlook clássico (o HTML completo, com tabelas e imagens). Sem Outlook: exceção."""
    if not IS_WINDOWS or not paths:
        return []
    items = _powershell(OUTLOOK_READ_PS, list(paths))
    out = []
    for item in items if isinstance(items, list) else [items]:
        if not isinstance(item, dict) or item.get("erro") or not (item.get("html") or item.get("texto")):
            continue
        imgs = item.get("imagens") or []
        out.append({"nome": os.path.splitext(os.path.basename(str(item.get("arquivo") or "")))[0][:80],
                    "assunto": str(item.get("assunto") or "")[:200],
                    "html": cid_to_data(str(item.get("html") or ""), imgs if isinstance(imgs, list) else [imgs]),
                    "corpo": str(item.get("texto") or "").replace("\r\n", "\n")[:8000], "uso": ""})
    return out


WINDOWS_LOCATION_PS = r"""param($src, $out)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Device
$w = New-Object System.Device.Location.GeoCoordinateWatcher([System.Device.Location.GeoPositionAccuracy]::Default)
$w.Start()
$deadline = (Get-Date).AddSeconds(15)
while (($w.Status -ne 'Ready') -and ((Get-Date) -lt $deadline)) { Start-Sleep -Milliseconds 300 }
$c = $w.Position.Location
$w.Stop()
if ($c.IsUnknown) { '{"ok": false}' | Set-Content -Encoding UTF8 $out; exit 0 }
ConvertTo-Json @{ ok = $true; lat = $c.Latitude; lon = $c.Longitude; acc = $c.HorizontalAccuracy } | Set-Content -Encoding UTF8 $out
"""


def windows_location() -> Optional[Dict[str, float]]:
    """Posição aproximada do PC pelo serviço de localização do Windows (Wi-Fi). None se desligado/indisponível."""
    if not IS_WINDOWS:
        return None
    try:
        res = _powershell(WINDOWS_LOCATION_PS, {}, timeout=40)
    except Exception:
        return None
    if not isinstance(res, dict) or not res.get("ok"):
        return None
    try:
        lat, lon = float(res["lat"]), float(res["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    return {"lat": round(lat, 4), "lon": round(lon, 4), "acc": float(res.get("acc") or 0)}


def write_eml(to: str, subject: str, text: str, html: str) -> str:
    """Rascunho .eml (abre como e-mail NOVO no Outlook, com o formato) — plano B quando não dá para usar o Outlook."""
    import tempfile
    from email.message import EmailMessage
    msg = EmailMessage()
    if to.strip():
        msg["To"] = to.strip()
    msg["Subject"] = subject
    msg["X-Unsent"] = "1"
    msg.set_content(text or " ")
    html, images = split_images(html)
    msg.add_alternative(html, subtype="html")
    if images:
        part = msg.get_payload()[1]
        for img in images:
            maintype, subtype = img["mime"].split("/", 1)
            part.add_related(img["dados"], maintype=maintype, subtype=subtype, cid=f"<{img['cid']}>", filename=img["nome"])
    folder = os.path.join(tempfile.gettempdir(), "BitPC")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"email-{int(time.time())}.eml")
    with open(path, "wb") as fh:
        fh.write(bytes(msg))
    return path


# ---------------- Outlook NOVO (olk.exe): sem COM. Abre o e-mail novo pelo mailto: (para + assunto) e cola o corpo com
# formato no campo certo, achado pela acessibilidade do Windows (UI Automation) — só no e-mail com o MESMO assunto.
NEW_OUTLOOK_PASTE_PS = r"""param($src, $out)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Windows.Forms
Add-Type @"
using System; using System.Runtime.InteropServices;
public static class BitWin {
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
}
"@
$d = Get-Content -Raw -Encoding UTF8 $src | ConvertFrom-Json
$A = [System.Windows.Automation.AutomationElement]
$T = [System.Windows.Automation.TreeScope]
$CT = [System.Windows.Automation.ControlType]
$cond = New-Object System.Windows.Automation.OrCondition(
  (New-Object System.Windows.Automation.PropertyCondition($A::ControlTypeProperty, $CT::Edit)),
  (New-Object System.Windows.Automation.PropertyCondition($A::ControlTypeProperty, $CT::Document)))
function Norm($t) { return ((([string]$t) -replace '\s+', ' ').Trim()).ToLower() }
$want = Norm $d.assunto
$seen = @{}
$state = @{ janela = $false; assunto = '' }
function Done($ok, $why) {
  $info = @{ ok = $ok; motivo = $why; janela = $state.janela; assunto_visto = $state.assunto; campos = @($seen.Keys | Select-Object -First 15) }
  ConvertTo-Json -InputObject $info -Depth 3 | Set-Content -Encoding UTF8 $out
  exit 0
}
$deadline = (Get-Date).AddSeconds([double]$d.espera)
while ((Get-Date) -lt $deadline) {
  $ids = @(Get-Process olk -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
  if ($ids.Count -gt 0) {
    foreach ($w in $A::RootElement.FindAll($T::Children, [System.Windows.Automation.Condition]::TrueCondition)) {
      if ($ids -notcontains $w.Current.ProcessId) { continue }
      $state.janela = $true
      $subjectOk = $false; $body = $null
      foreach ($e in $w.FindAll($T::Descendants, $cond)) {
        $n = [string]$e.Current.Name
        if ($n) { $seen[$n.Substring(0, [Math]::Min(60, $n.Length))] = 1 }
        if ($n -match 'assunto|subject') {
          $v = ''
          try { $v = $e.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).Current.Value } catch { }
          $nv = Norm $v
          if ($nv) { $state.assunto = $nv }
          if ($nv -eq $want -or ($want.Length -ge 12 -and $nv.StartsWith($want.Substring(0, 12)))) { $subjectOk = $true }
        } elseif (-not $body -and $n -match 'corpo|body|mensagem|message') { $body = $e }
      }
      if ($subjectOk -and $body) {
        try { [BitWin]::SetForegroundWindow([IntPtr]$w.Current.NativeWindowHandle) | Out-Null } catch { }
        Start-Sleep -Milliseconds 150
        $body.SetFocus()
        Start-Sleep -Milliseconds 250
        # o corpo é uma página web (processo msedgewebview2): o que vale é a JANELA da frente ser do Outlook
        $fpid = [uint32]0
        [BitWin]::GetWindowThreadProcessId([BitWin]::GetForegroundWindow(), [ref]$fpid) | Out-Null
        if ($ids -notcontains [int]$fpid) { Done $false 'foco' }
        [System.Windows.Forms.SendKeys]::SendWait('^v')
        Start-Sleep -Milliseconds 400
        Done $true 'colado'
      }
    }
  }
  Start-Sleep -Milliseconds 300
}
if (-not $state.janela) { Done $false 'sem_janela' }
if ($state.assunto) { Done $false 'assunto_diferente' }
Done $false 'sem_campos'
"""


def running_outlooks() -> set:
    """Quais Outlooks estão abertos: "novo" (olk.exe) e/ou "classico" (OUTLOOK.EXE)."""
    if not IS_WINDOWS:
        return set()
    try:
        out = subprocess.run(["tasklist", "/NH", "/FO", "CSV"], capture_output=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.decode("utf-8", "replace").lower()
    except Exception:
        return set()
    found = set()
    if '"olk.exe"' in out:
        found.add("novo")
    if '"outlook.exe"' in out:
        found.add("classico")
    return found


def mailto_handler() -> str:
    """O app de e-mail padrão do Windows (ProgId do mailto:)."""
    if not IS_WINDOWS:
        return ""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\mailto\UserChoice")
        return str(winreg.QueryValueEx(key, "ProgId")[0])
    except OSError:
        return ""


def outlook_flavor(preferred: str = "") -> str:
    """'novo' ou 'classico': a preferência do usuário; senão o que está aberto; senão o app de e-mail padrão."""
    if preferred in ("novo", "classico"):
        return preferred
    running = running_outlooks()
    if running == {"novo"}:
        return "novo"
    if running == {"classico"}:
        return "classico"
    handler = mailto_handler().lower()
    if "outlook.url" in handler:
        return "classico"
    new_exe = os.path.join(os.environ.get("LOCALAPPDATA") or "", "Microsoft", "WindowsApps", "olk.exe")
    return "novo" if (handler and os.path.exists(new_exe)) or "novo" in running else "classico"


def copy_formatted(html: str, text: str, flavor: str) -> bool:
    """O Outlook novo (e o navegador) aceita as imagens embutidas; o clássico (Word) prefere arquivos."""
    return clipboard_set_html(html if flavor == "novo" else images_to_files(html), text)


OUTLOOK_REASONS = {"foco": "o Outlook não ficou na frente", "sem_janela": "não achei a janela do Outlook novo",
                   "assunto_diferente": "achei um e-mail, mas com outro assunto",
                   "sem_campos": "não achei os campos do e-mail", "erro": "o PowerShell falhou"}
LAST_OUTLOOK: Dict[str, Any] = {}


def outlook_log(info: Dict[str, Any]) -> None:
    """Diagnóstico do último "Abrir no Outlook" (novo): %APPDATA%\\BitPC\\outlook-novo.log, sem o texto do e-mail."""
    LAST_OUTLOOK.clear()
    LAST_OUTLOOK.update(info)
    try:
        with open(os.path.join(config_dir(), "outlook-novo.log"), "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + json.dumps(info, ensure_ascii=False) + "\n")
    except OSError:
        pass


def open_new_outlook(to: str, subject: str, text: str, html: str) -> str:
    """E-mail novo no Outlook novo: 'novo' (já colado com formato) ou 'novo_colar' (aberto; o formato está copiado).
    v1.10: o motivo de não colar fica em LAST_OUTLOOK e no outlook-novo.log."""
    copy_formatted(html, text, "novo")
    os.startfile("mailto:" + urllib.parse.quote(to.strip(), safe="@,;") + "?subject=" + urllib.parse.quote(subject))  # type: ignore[attr-defined]
    if not subject.strip():
        outlook_log({"ok": False, "motivo": "sem_assunto"})
        return "novo_colar"                                 # sem assunto não dá para ter certeza de qual e-mail é
    try:
        res = _powershell(NEW_OUTLOOK_PASTE_PS, {"assunto": subject, "espera": 25}, timeout=50)
    except Exception as err:
        outlook_log({"ok": False, "motivo": "erro", "erro": f"{type(err).__name__}: {str(err)[:200]}"})
        return "novo_colar"
    info = res if isinstance(res, dict) else {"ok": False, "motivo": "erro"}
    outlook_log(info)
    return "novo" if info.get("ok") else "novo_colar"


def open_html_mail(to: str, subject: str, text: str, html: str, flavor: str = "") -> str:
    """E-mail novo JÁ FORMATADO: no Outlook novo (mailto + colar) ou no clássico (COM); senão, um rascunho .eml."""
    if IS_WINDOWS and (flavor or outlook_flavor()) == "novo":
        try:
            return open_new_outlook(to, subject, text, html)
        except Exception:
            pass
    if IS_WINDOWS:
        import shutil
        import tempfile
        folder = tempfile.mkdtemp(prefix="bitpc-img-")
        try:
            body, images = split_images(html)
            files = []
            for img in images:
                path = os.path.join(folder, img["nome"])
                with open(path, "wb") as fh:
                    fh.write(img["dados"])
                files.append({"arquivo": path, "cid": img["cid"]})
            _powershell(OUTLOOK_NEW_PS, {"para": to, "assunto": subject, "html": body, "imagens": files}, timeout=60)
            return "outlook"
        except Exception:
            pass
        finally:
            shutil.rmtree(folder, ignore_errors=True)
    open_path(write_eml(to, subject, text, html))
    return "eml"


def read_template_file(path: str) -> Optional[Dict[str, str]]:
    """Um modelo a partir de arquivo: .oft/.msg (pelo Outlook; reserva: leitura direta), .eml ou .html/.htm."""
    low = path.lower()
    name = os.path.splitext(os.path.basename(path))[0][:80]
    if low.endswith((".oft", ".msg")):
        try:
            found = outlook_read([path])
            if found:
                return found[0]
        except Exception:
            pass
        return read_outlook_template(path)
    if low.endswith(".eml"):
        import email
        from email import policy
        with open(path, "rb") as fh:
            msg = email.message_from_binary_file(fh, policy=policy.default)
        html_part, text_part = msg.get_body(("html",)), msg.get_body(("plain",))
        html = html_part.get_content() if html_part else ""
        text = text_part.get_content() if text_part else html_to_text(html)
        html = cid_to_data(html, [{"cid": p.get("Content-ID", ""), "mime": p.get_content_type(),
                                   "dados": base64.b64encode(p.get_content()).decode()}
                                  for p in msg.walk() if p.get("Content-ID") and p.get_content_maintype() == "image"])
        return {"nome": name, "assunto": str(msg.get("Subject") or "")[:200], "html": html, "corpo": text[:8000], "uso": ""}
    if low.endswith((".htm", ".html")):
        raw = open(path, "rb").read()
        for enc in ("utf-8", "cp1252"):
            try:
                html = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        html = inline_local_images(html, os.path.dirname(path))
        return {"nome": name, "assunto": "", "html": html, "corpo": html_to_text(html)[:8000], "uso": ""}
    return None


def outlook_files() -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """Modelos (.oft, com o HTML completo pelo Outlook) e assinaturas (HTML e texto) que o Outlook clássico guarda no PC."""
    appdata = os.environ.get("APPDATA") or ""
    paths: List[str] = []
    for folder in (os.path.join(appdata, "Microsoft", "Templates"),
                   os.path.join(os.path.expanduser("~"), "Documents", "Custom Office Templates"),
                   os.path.join(os.path.expanduser("~"), "Documents", "Modelos Personalizados do Office")):
        if os.path.isdir(folder):
            paths += [os.path.join(folder, n) for n in sorted(os.listdir(folder)) if n.lower().endswith(".oft")]
    templates: List[Dict[str, str]] = []
    try:
        templates = outlook_read(paths)
    except Exception:
        templates = []
    if len(templates) < len(paths):                       # sem Outlook clássico: leitura direta (pode vir só o texto)
        got = {t["nome"] for t in templates}
        for path in paths:
            if os.path.splitext(os.path.basename(path))[0][:80] not in got:
                item = read_outlook_template(path)
                if item:
                    templates.append(item)
    signatures: List[Dict[str, str]] = []
    sig_dir = os.path.join(appdata, "Microsoft", "Signatures")
    if os.path.isdir(sig_dir):
        by_name: Dict[str, Dict[str, Any]] = {}
        for n in os.listdir(sig_dir):
            stem, ext = os.path.splitext(n)
            if ext.lower() not in (".txt", ".htm"):
                continue
            path = os.path.join(sig_dir, n)
            raw = open(path, "rb").read()
            text = ""
            for enc in (("utf-16",) if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else ()) + ("utf-8", "cp1252"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            entry = by_name.setdefault(stem, {"nome": stem, "texto": "", "html": "", "mtime": 0.0})
            entry["mtime"] = max(entry["mtime"], os.path.getmtime(path))
            if ext.lower() == ".htm":
                entry["html"] = inline_local_images(text, sig_dir)
                entry["texto"] = entry["texto"] or html_to_text(text)
            else:
                entry["texto"] = text.strip()
        signatures = sorted(by_name.values(), key=lambda e: -e["mtime"])
    return templates, signatures


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


# ================================================================== reunião: transcrição ao vivo (v1.8)
# O som do PC (os outros participantes: Teams, Meet, Zoom) vem do "loopback" do WASAPI e a sua voz do microfone. Os
# dois viram um áudio mono de 16 kHz, cortado em pedaços de ~25 s num silêncio, enviados ao Bit (transcrição na nuvem).
# Conversões em Python puro (sem audioop, que saiu do Python 3.13).
from array import array as _array

MEET_RATE = 16000
MEET_CHUNK_S = 25.0
MEET_SILENCE_RMS = 120                 # abaixo disso o pedaço é silêncio: não é enviado (a transcrição inventaria frases)


def to_mono16k(raw: bytes, channels: int, rate: int, state: Optional[Dict[str, Any]] = None) -> _array:
    """PCM 16 bits (1 ou 2+ canais, qualquer taxa) -> mono 16 kHz. `state` guarda a fração que sobrou entre blocos."""
    data = _array("h")
    data.frombytes(raw[: len(raw) - len(raw) % (2 * max(1, channels))])
    if channels > 1:
        chans = [data[i::channels] for i in range(channels)]
        data = _array("h", [sum(v) // channels for v in zip(*chans)])
    if rate == MEET_RATE or not data:
        return data
    step = rate / MEET_RATE
    state = state if state is not None else {}
    pos = float(state.get("pos", 0.0))
    width = max(1, int(round(step)))                             # média dos vizinhos: filtro passa-baixa simples
    out = _array("h")
    n = len(data)
    while pos < n:
        i = int(pos)
        seg = data[i:i + width]
        out.append(sum(seg) // len(seg))
        pos += step
    state["pos"] = pos - n
    return out


def mix(a: _array, b: _array) -> _array:
    return _array("h", [max(-32768, min(32767, x + y)) for x, y in zip(a, b)])


def rms(samples: Any) -> float:
    if not samples:
        return 0.0
    return (sum(x * x for x in samples) / len(samples)) ** 0.5


def quiet_cut(samples: _array, search_s: float = 6.0, window_s: float = 0.25) -> int:
    """Onde cortar o pedaço: a janela mais silenciosa dos últimos `search_s` segundos (para não cortar uma palavra)."""
    win = int(MEET_RATE * window_s)
    start = max(0, len(samples) - int(MEET_RATE * search_s))
    best, best_rms = len(samples), float("inf")
    for i in range(start, len(samples) - win, win):
        r = rms(samples[i:i + win:4])
        if r < best_rms:
            best, best_rms = i + win // 2, r
    return best


def wav_bytes(samples: _array) -> bytes:
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(MEET_RATE)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


MEETING_APPS = (
    (re.compile(r"(?i)\b(reuni[aã]o|meeting|chamada|call)\b.*\bteams\b|\bteams\b.*\b(reuni[aã]o|meeting|chamada|call)\b"), "Teams"),
    (re.compile(r"(?i)zoom (meeting|workplace)|reuni[aã]o (do|no) zoom|\bzoom\b.*\b(reuni[aã]o|meeting)\b"), "Zoom"),
    (re.compile(r"(?i)^meet\s*[-–—:]|google meet|\bmeet\.google\.com\b"), "Google Meet"),
    (re.compile(r"(?i)\bwebex\b"), "Webex"),
)


def meeting_app(title: str) -> str:
    """Pelo título da janela em uso: 'Teams', 'Zoom', 'Google Meet', 'Webex' ou ''."""
    for rx, name in MEETING_APPS:
        if rx.search(str(title or "")):
            return name
    return ""


class AudioSource:
    """Um fluxo (som do PC ou microfone) já convertido para mono 16 kHz, esperando o misturador."""

    def __init__(self, name: str, channels: int, rate: int):
        self.name, self.channels, self.rate = name, max(1, int(channels)), int(rate)
        self.buf = _array("h")
        self.state: Dict[str, Any] = {}
        self.lock = threading.Lock()
        self.got = 0

    def feed(self, raw: bytes) -> None:
        conv = to_mono16k(raw, self.channels, self.rate, self.state)
        with self.lock:
            self.buf.extend(conv)
            self.got += len(conv)
            if len(self.buf) > MEET_RATE * 3:                    # atrasou demais (o PC travou?): volta ao tempo real
                del self.buf[: len(self.buf) - MEET_RATE]

    def take(self, n: int) -> _array:
        with self.lock:
            out = self.buf[:n]
            del self.buf[:n]
        if len(out) < n:                                         # o loopback não manda nada quando o PC está mudo
            out.extend(_array("h", bytes(2 * (n - len(out)))))
        return out


ECHO_SYS_RMS = 300                    # o som do PC está "falando"
ECHO_RATIO = 1.5                       # o microfone só vale se for bem mais alto que o som do PC (senão é eco)


class SplitMeeting:
    """v1.13: "eu × os outros". O microfone e o som do PC viram DOIS fluxos transcritos à parte, com quem falou. Sem fone
    de ouvido o microfone também capta o alto-falante: enquanto o som do PC fala e o microfone não está bem mais alto,
    o microfone é silenciado (filtro de eco simples), para a fala dos outros não sair duplicada como sua."""

    def __init__(self, system: AudioSource, mic: AudioSource, on_chunk: Callable[[bytes, int, float, str], None]):
        self.system, self.mic = system, mic
        self.rec = {"outros": MeetingRecorder([system], lambda w, q, t: on_chunk(w, q, t, "outros")),
                    "voce": MeetingRecorder([mic], lambda w, q, t: on_chunk(w, q, t, "voce"))}
        self.echo_blocks = 0

    def tick(self, seconds: float = 0.5) -> None:
        n = int(MEET_RATE * seconds)
        sys_blk, mic_blk = self.system.take(n), self.mic.take(n)
        r_sys, r_mic = rms(sys_blk[::4]), rms(mic_blk[::4])
        if r_sys >= ECHO_SYS_RMS and r_mic < ECHO_RATIO * r_sys:
            mic_blk = _array("h", bytes(2 * n))
            self.echo_blocks += 1
        self.rec["outros"].tick(seconds, sys_blk)
        self.rec["voce"].tick(seconds, mic_blk)

    def flush(self) -> None:
        for rec in self.rec.values():
            rec.flush()

    @property
    def skipped(self) -> int:
        return sum(r.skipped for r in self.rec.values())


class MeetingRecorder:
    """Mistura as fontes a cada `tick` (0,5 s) e entrega pedaços WAV de ~25 s: on_chunk(wav, seq, inicio_s)."""

    def __init__(self, sources: List[AudioSource], on_chunk: Callable[[bytes, int, float], None]):
        self.sources, self.on_chunk = sources, on_chunk
        self.pending = _array("h")
        self.seq, self.offset_s, self.skipped = 0, 0.0, 0
        self.elapsed_s = 0.0

    def tick(self, seconds: float = 0.5, block: Optional[_array] = None) -> None:
        """`block` (v1.13): o pedaço já tirado das fontes (modo "eu × os outros", com o filtro de eco)."""
        n = int(MEET_RATE * seconds)
        if block is None:
            for src in self.sources:
                part = src.take(n)
                block = part if block is None else mix(block, part)
        if block is None:
            return
        self.pending.extend(block)
        self.elapsed_s += seconds
        if len(self.pending) >= MEET_RATE * MEET_CHUNK_S:
            cut = quiet_cut(self.pending)
            self._emit(self.pending[:cut])
            del self.pending[:cut]

    def flush(self) -> None:
        if len(self.pending) > MEET_RATE * 0.5:
            self._emit(self.pending[:])
        self.pending = _array("h")

    def _emit(self, samples: _array) -> None:
        dur = len(samples) / MEET_RATE
        if rms(samples[::8]) >= MEET_SILENCE_RMS:
            self.on_chunk(wav_bytes(samples), self.seq, round(self.offset_s, 2))
            self.seq += 1
        else:
            self.skipped += 1
        self.offset_s += dur


def open_meeting_sources(system: bool = True, mic: bool = True) -> Tuple[List[AudioSource], Callable[[], None], List[str]]:
    """Abre o som do PC (WASAPI loopback do alto-falante padrão) e/ou o microfone padrão. Devolve (fontes, fechar,
    avisos). Precisa do PyAudioWPatch (o instalador instala)."""
    try:
        import pyaudiowpatch as pyaudio                       # pip install PyAudioWPatch
    except ImportError:
        raise RuntimeError("Falta o componente de áudio: rode o instalador do Bit de novo (ele instala o PyAudioWPatch).")
    pa = pyaudio.PyAudio()
    sources, streams, notes = [], [], []

    def open_stream(info: Dict[str, Any], name: str) -> None:
        channels = max(1, min(2, int(info.get("maxInputChannels") or 1)))
        rate = int(info.get("defaultSampleRate") or 48000)
        src = AudioSource(name, channels, rate)

        def cb(in_data: bytes, frames: int, t: Any, status: Any) -> Tuple[None, int]:
            src.feed(in_data)
            return (None, pyaudio.paContinue)
        st = pa.open(format=pyaudio.paInt16, channels=channels, rate=rate, input=True, input_device_index=info["index"],
                     frames_per_buffer=int(rate / 10), stream_callback=cb)
        st.start_stream()
        streams.append(st)
        sources.append(src)
    if system:
        try:
            open_stream(pa.get_default_wasapi_loopback(), "som do PC")
        except Exception as err:
            notes.append(f"Som do PC indisponível ({type(err).__name__}): só o microfone.")
    if mic:
        try:
            open_stream(pa.get_default_input_device_info(), "microfone")
        except Exception as err:
            notes.append(f"Microfone indisponível ({type(err).__name__}).")

    def close() -> None:
        for st in streams:
            try:
                st.stop_stream()
                st.close()
            except Exception:
                pass
        try:
            pa.terminate()
        except Exception:
            pass
    if not sources:
        close()
        raise RuntimeError("Não achei nem o som do PC nem o microfone. " + " ".join(notes))
    return sources, close, notes


ICON_FONT = "Segoe MDL2 Assets"           # ícones do próprio Windows 10/11 (sem baixar nada)
ICONS = {"preparar": ("\uE70F", "✎"), "chat": ("", "✉"), "olhar": ("", "◉"), "vivo": ("", "●"), "parar": ("", "■"),
         "voz": ("", "♪"), "mudo": ("", "♪"), "sino": ("", "!"), "abrir": ("", "⤢"),
         "fechar": ("", "✕"), "arrastar": ("", "⋮"), "enviar": ("", "➜"), "reuniao": ("\uE720", "⏺")}


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
        self.loc_var = tk.BooleanVar(value=bool(self.cfg.get("localizacao")))     # v1.15: localização do Windows
        self.frame: Optional[Any] = None
        self.meeting: Optional[Dict[str, Any]] = None
        token = unprotect(str(self.cfg.get("token") or ""))
        if self.cfg.get("base") and token:
            self.api = self.api_factory(self.cfg["base"], token)
            self.show_main()
            if self.cfg.get("modo") == "compacto":
                self.root.after(50, self.show_companion)
        else:
            self.show_pairing()
        self.root.after(150, self.pump)
        self.root.after(400, exclude_from_capture, self.root)
        if not test_mode:
            threading.Thread(target=hotkeys, args=(self.events,), daemon=True, name="atalhos").start()
            threading.Thread(target=self.track_foreground, daemon=True, name="janela-em-uso").start()
            threading.Thread(target=self.poll_notices, daemon=True, name="avisos").start()
            threading.Thread(target=self.location_loop, daemon=True, name="localizacao").start()

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
                    if isinstance(payload, tuple):                    # v1.12: aviso com botões (ação e 👍/👎)
                        text, buttons = payload
                        self.notice(text, buttons=buttons)
                    else:
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
        self.root.after(300, self.root.attributes, "-topmost", False)
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
            if self.meeting is not None:                          # fechar o Bit no meio da reunião: para de ouvir
                self.meeting["stop"].set()
                self.meeting["close"]()
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
        for text, var in (("Orientação em texto", self.text_var), ("Orientação por voz", self.speak_var),
                          ("Localização (clima)", self.loc_var)):
            tk.Checkbutton(opts, text=text, variable=var, command=self.save_options, bg=self.BG, fg=self.MUTED,
                           selectcolor=self.CARD, activebackground=self.BG, activeforeground=self.INK,
                           font=("Segoe UI", 9)).pack(side="left")
        prep = tk.Frame(f, bg=self.BG)
        prep.pack(side="bottom", fill="x", pady=(self.px(6), 0))
        self.label(prep, "Preparar com o que está na tela:", 8, self.MUTED).pack(anchor="w")
        kinds = tk.Frame(prep, bg=self.BG)
        kinds.pack(fill="x")
        for i, (kind, text) in enumerate(self.KINDS):
            tk.Button(kinds, text=tk_safe(text), command=lambda k=kind: self.prepare(k), relief="flat", cursor="hand2",
                      padx=self.px(7), pady=self.px(3), bg=self.HOVER, fg=self.INK, activebackground=self.PRI,
                      font=("Segoe UI", 9), borderwidth=0).grid(row=i // 3, column=i % 3, sticky="ew",
                                                               padx=(0, self.px(4)), pady=(self.px(3), 0))
        for col in range(3):
            kinds.grid_columnconfigure(col, weight=1)
        tk.Button(prep, text=tk_safe("☰ Meus modelos de e-mail e assinatura"), command=self.open_templates, relief="flat",
                  cursor="hand2", bg=self.BG, fg=self.PRI, activebackground=self.BG, font=("Segoe UI", 9, "underline"),
                  borderwidth=0, anchor="w").pack(anchor="w", pady=(self.px(3), 0))
        self.suggest_box = tk.Frame(prep, bg=self.BG)
        self.suggest_box.pack(fill="x")
        tools = tk.Frame(f, bg=self.BG)
        tools.pack(side="bottom", fill="x", pady=(self.px(8), 0))
        self.button(tools, "◉ Olhar minha tela", self.look).pack(side="left")
        self.live_btn = self.button(tools, "● Assistente ao vivo", self.toggle_live)
        self.live_btn.pack(side="left", padx=self.px(6))
        self.meet_btn = self.button(tools, "⏺ Reunião", self.toggle_meeting)
        self.meet_btn.pack(side="left")
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
        turned_on = bool(self.loc_var.get()) and not self.cfg.get("localizacao")
        self.cfg["localizacao"] = bool(self.loc_var.get())
        save_config(self.cfg, self.config_path)
        if turned_on:
            self.location_now()
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
        if isinstance(res.get("preparado"), dict):
            self.on_prepared(res["preparado"])
            return
        msgs = res.get("mensagens") or []
        for m in msgs:
            self.add_message(str(m.get("text") or ""), "bit", m.get("buttons") or [])
        if self.bar is not None and msgs:
            self.show_bubble(msgs)

    def ask(self, text: str) -> None:
        text = text.strip()
        if not text or self.api is None:
            return
        if CLIENT_ASK_RE.search(fold(text)):                      # "pesquise esse cliente": usa a tela (e as compras nela)
            self.prepare("cliente", text)
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
                    self.maybe_suggest_meeting(str(info.get("title") or ""))
            except Exception:
                pass
            time.sleep(0.5)

    def maybe_suggest_meeting(self, title: str, now: Optional[float] = None) -> Optional[str]:
        """v1.9: janela de reunião em uso (Teams, Zoom, Meet, Webex) e nada sendo transcrito -> oferece transcrever, no
        máximo uma vez a cada 30 min por aplicativo. Desliga com "sugerir_reuniao": false na configuração."""
        app = meeting_app(title)
        if not app or self.meeting is not None or self.cfg.get("sugerir_reuniao") is False:
            return None
        now = time.time() if now is None else now
        last = getattr(self, "meeting_hints", {})
        if now - last.get(app, 0.0) < 1800:
            return None
        last[app] = now
        self.meeting_hints = last
        self.events.put(("call", lambda: self.notice(f"Parece uma reunião no {app}. Quer que eu transcreva e faça a ata?",
                                                     action=("⏺ Transcrever", self.ask_meeting))))
        return app

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

    def clean_capture(self, region: Optional[Dict[str, int]]) -> Tuple[Any, Dict[str, int]]:
        """Foto da tela SEM o Bit: primeiro a janela em uso direto (PrintWindow); se ela não deixar, as janelas do Bit
        ficam invisíveis por um instante (sem fechar nem roubar o foco) durante a foto."""
        info = self.last_foreign
        if info and region:
            shot = print_window(info["hwnd"])
            if shot is not None:
                return shot
        if threading.current_thread() is threading.main_thread():
            return self.hidden_capture(region)
        box: Dict[str, Any] = {}
        done = threading.Event()

        def job() -> None:
            try:
                box["r"] = self.hidden_capture(region)
            except Exception as err:
                box["e"] = err
            finally:
                done.set()
        self.events.put(("call", job))
        if not done.wait(6.0):
            raise RuntimeError("a tela está ocupada agora")
        if "e" in box:
            raise box["e"]
        return box["r"]

    def our_windows(self) -> List[Any]:
        return [w for w in [self.root, self.bar, self.bubble, self.badge, self.overlay] + list(self.toasts)
                + list(self.result_windows) + list(getattr(self, "extra_windows", [])) if alive(w)]

    def hidden_capture(self, region: Optional[Dict[str, int]]) -> Tuple[Any, Dict[str, int]]:
        saved = []
        for win in self.our_windows():
            try:
                if win.winfo_ismapped():
                    saved.append((win, float(win.attributes("-alpha"))))
                    win.attributes("-alpha", 0.0)
            except Exception:
                continue
        self.last_hidden = len(saved)
        try:
            self.root.update_idletasks()
            time.sleep(0.15)                                  # o Windows redesenha o que estava atrás
            return capture(region or None)
        finally:
            for win, alpha in saved:
                try:
                    win.attributes("-alpha", alpha)
                except Exception:
                    pass

    def grab(self) -> Tuple[Optional[str], Optional[Dict[str, int]], Optional[List[int]], str]:
        region, title = self.target_region()
        if region is None:
            return None, None, None, title
        img, used = self.clean_capture(region)
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
                direct = print_window(self.last_foreign["hwnd"]) if self.last_foreign and region else None
                img, used = direct or capture(region or None)        # só para ver se mudou: não esconde nada
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
                if direct is None:                                # a foto que vai para o Bit é sempre sem o Bit
                    img, used = self.clean_capture(region)
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
             ("texto", "✎ Texto"), ("cliente", "⌕ Cliente"))
    KIND_NAMES = {"email": "o e-mail", "planilha": "a planilha", "apresentacao": "a apresentação", "documento": "o documento",
                  "texto": "o texto", "cliente": "a pesquisa do cliente (cadastro, web e plano de abordagem; 1 a 3 min)"}

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
        if kind == "cliente":                                     # plano de conta (.docx) + e-mail de abordagem
            self.add_message(str(res.get("texto") or res.get("resumo") or "Pesquisa do cliente pronta."), "bit")
            if res.get("corpo"):
                self.open_result(dict(res, tipo="email", titulo="E-mail de abordagem"))
            if res.get("arquivos"):
                self.run_bg(lambda: self.save_files(list(res["arquivos"])), self.on_saved)
            elif self.bar is not None:
                self.show_bubble([{"text": str(res.get("resumo") or "Pesquisa do cliente pronta.")}])
            return
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
        w, h = min(self.px(720), sw - self.px(40)), min(self.px(560), sh - self.px(90))
        win.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 2)}")
        self.root.after(200, self.later, exclude_from_capture, win)
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
        html = str(res.get("corpo_html") or "")
        # v1.16: as imagens são ajustadas em segundo plano (medir as cores no navegador leva alguns segundos)
        html_box: Dict[str, Any] = {"html": html, "ready": threading.Event()}

        def final_html() -> str:
            html_box["ready"].wait(45)
            return html_box["html"]

        def flattened(h: str) -> None:
            html_box["html"] = h
            html_box["ready"].set()
            if alive(status) and images_summary():
                status.configure(text=tk_safe(str(status.cget("text")) + " " + images_summary()))
        if html:
            self.run_bg(lambda: transparent_images(html), flattened, lambda m: html_box["ready"].set())
        else:
            html_box["ready"].set()

        def flavor() -> str:
            return outlook_flavor(str(self.cfg.get("outlook") or ""))

        def copy_formatted_now() -> None:
            text_now = text_of()

            def work() -> bool:
                if copy_formatted(final_html(), text_now, flavor()):
                    return True
                self.events.put(("call", lambda: copy(text_now, "Texto")))
                return False
            self.run_bg(work, lambda ok: status.configure(
                text="✔ Copiado COM o formato do modelo: no Outlook, clique no corpo do e-mail e Ctrl+V." if ok else
                "⚠ Não consegui copiar com formato; copiei só o texto."))

        def outlook() -> None:
            kind = flavor()
            status.configure(text="Abrindo um e-mail novo no Outlook" + (" novo" if kind == "novo" else "")
                             + ", já formatado… (não mexa no teclado por alguns segundos)")
            to = fields["para"].get() if "para" in fields else ""
            subject = fields["assunto"].get() if "assunto" in fields else ""
            messages = {
                "novo": "✔ Abri no Outlook novo e colei o e-mail com o formato do modelo. Revise e envie de lá.",
                "novo_colar": "✔ Abri o e-mail novo no Outlook, mas não consegui colar sozinho{motivo}. O corpo formatado "
                              "JÁ ESTÁ COPIADO: clique no corpo do e-mail e Ctrl+V. (Se abriu no Outlook errado: "
                              "Configurações do Windows → Apps → Apps padrão → Outlook (new) → MAILTO.)",
                "outlook": "✔ Abri no Outlook, com o formato do modelo. Revise e envie de lá.",
                "eml": "✔ Abri um rascunho já formatado (.eml). Se abrir como e-mail recebido, use \"Copiar formatado\"."}
            def opened(how: str) -> None:
                why = OUTLOOK_REASONS.get(str(LAST_OUTLOOK.get("motivo") or ""), "")
                status.configure(text=messages.get(how, messages["eml"]).replace("{motivo}", f" ({why})" if why else ""))
                if how == "novo_colar":                          # o aviso também aparece fora da janela (no Outlook)
                    self.notice("No Outlook: clique no corpo do e-mail e aperte Ctrl+V — o e-mail formatado já está copiado.")
            self.run_bg(lambda: open_html_mail(to, subject, text_of(), final_html(), kind), opened)

        def switch_outlook() -> None:
            self.cfg["outlook"] = "classico" if flavor() == "novo" else "novo"
            save_config(self.cfg, self.config_path)
            which.configure(text=tk_safe(f"Outlook: {'novo' if self.cfg['outlook'] == 'novo' else 'clássico'} (trocar)"))

        def preview() -> None:
            import tempfile
            path = os.path.join(tempfile.gettempdir(), "BitPC", f"previa-{int(time.time())}.html")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                shown = html_box["html"]
                fh.write(shown if re.search(r"(?i)<meta[^>]+charset", shown) else '<meta charset="utf-8">' + shown)
            open_path(path)
        if html:
            self.button(buttons, "Copiar formatado", copy_formatted_now, primary=True).pack(side="left")
            self.button(buttons, "Abrir no Outlook", outlook).pack(side="left", padx=self.px(6))
            self.button(buttons, "Ver", preview).pack(side="left")
            self.button(buttons, "Só o texto", lambda: copy(text_of(), "Texto")).pack(side="left", padx=self.px(6))
            which = self.label(buttons, "Outlook: … (trocar)", 8, self.MUTED, cursor="hand2")
            which.pack(side="left", padx=(self.px(4), 0))
            which.bind("<Button-1>", lambda _e: switch_outlook())
            self.run_bg(flavor, lambda k: alive(which) and which.configure(
                text=f"Outlook: {'novo' if k == 'novo' else 'clássico'} (trocar)"))
        else:
            self.button(buttons, "Copiar o texto", lambda: copy(text_of(), "Texto"), primary=True).pack(side="left")
        if res.get("tipo") == "email" and not html:
            self.button(buttons, "Copiar o assunto", lambda: copy(fields["assunto"].get(), "Assunto")).pack(
                side="left", padx=self.px(6))
            self.button(buttons, "Abrir no e-mail", mail).pack(side="left")
        def save_as_template() -> None:
            from tkinter import simpledialog
            name = simpledialog.askstring(APP_NAME, "Nome do modelo (ex.: Follow-up de orçamento):", parent=win)
            if not name:
                return
            use = simpledialog.askstring(APP_NAME, "Quando usar? (opcional, ex.: 3 dias depois da proposta)", parent=win) or ""
            body = {"acao": "salvar", "nome": name, "uso": use, "corpo": text_of(), "origem": "pc",
                    "assunto": fields["assunto"].get() if "assunto" in fields else ""}
            self.run_bg(lambda: self.api.call("POST", "modelos", body),
                        lambda r: status.configure(text=f"✔ Salvo como o modelo \"{name}\". Troque nomes e números por {{{{campos}}}} se quiser."))
        if res.get("tipo") == "email" and not html:
            self.button(buttons, "Salvar como modelo", save_as_template).pack(side="left", padx=self.px(6))
        self.button(buttons, "Fechar", win.destroy).pack(side="right")
        hint = (f"Baseado no seu modelo \"{res['modelo']}\". " if res.get("modelo") else "")
        if html:
            hint += ("No formato do modelo (◆). \"Copiar formatado\"/\"Abrir no Outlook\" levam o visual; o que você "
                     "editar na caixa vale para \"Só o texto\".")
        elif res.get("formato") in ("grande", "estrutura"):
            hint += "Desta vez saiu só o texto: abra o modelo no Outlook e cole por cima (Ctrl+V)."
        else:
            hint += "Revise e ajuste o texto à vontade antes de copiar."
        status = self.label(frame, hint, 9, self.MUTED, wraplength=w - self.px(40))
        status.pack(side="bottom", anchor="w", pady=(self.px(6), 0))
        win.lift()
        win.focus_force()
        self.result_windows.append(win)

    def template_images_window(self, parent: Any, res: Dict[str, Any], entries: Dict[str, Any], text: Any, status: Any,
                               state: Dict[str, Any], save: Callable[[], None]) -> None:
        tk = self.tk
        box: Dict[str, Any] = {"html": str(res.get("html") or ""), "changed": False, "photos": []}
        win = tk.Toplevel(parent)
        win.title("Imagens do modelo — Bit")
        win.configure(bg=self.BG)
        self.extra_windows = [x for x in getattr(self, "extra_windows", []) if alive(x)] + [win]
        self.label(win, f"Imagens de \"{res.get('nome') or ''}\"", 12, bold=True).pack(anchor="w", padx=self.px(12),
                                                                                     pady=(self.px(10), 0))
        self.label(win, "Mostradas sobre fundo escuro: se aparecer um quadro branco, a imagem perdeu a transparência. "
                        "Troque pela original (PNG ou WebP, como veio do design).", 9, self.MUTED,
                   wraplength=self.px(560)).pack(anchor="w", padx=self.px(12))
        rows = tk.Frame(win, bg=self.BG)
        rows.pack(fill="both", expand=True, padx=self.px(12), pady=self.px(8))
        info = self.label(win, "", 9, self.MUTED, wraplength=self.px(560))
        info.pack(anchor="w", padx=self.px(12))

        def draw() -> None:
            for child in rows.winfo_children():
                child.destroy()
            box["photos"] = []
            found = template_images(box["html"])
            box["found"] = found
            if not found:
                self.label(rows, "Este modelo não tem imagens.", 10).pack(anchor="w")
            for item in found:
                row = tk.Frame(rows, bg=self.CARD)
                row.pack(fill="x", pady=(0, self.px(6)))
                if item.get("png"):
                    try:
                        photo = tk.PhotoImage(data=base64.b64encode(item["png"]).decode("ascii"))
                        box["photos"].append(photo)
                        tk.Label(row, image=photo, bg=self.CARD).pack(side="left", padx=self.px(6), pady=self.px(6))
                    except Exception:
                        pass
                size = "×".join(str(v) for v in item.get("tamanho") or [])
                desc = f"Imagem {item['n']} · {item.get('formato') or '?'} {size}\n{item.get('estado') or ''}"
                self.label(row, desc, 9, "#7ddc9a" if item.get("ok") else "#ffb86b", bg=self.CARD,
                           wraplength=self.px(230)).pack(side="left", padx=self.px(6))
                self.button(row, "Trocar pela original…", lambda k=item["n"] - 1: swap(k)).pack(side="right",
                                                                                               padx=self.px(6))

        def swap(index: int) -> None:
            from tkinter import filedialog
            path = filedialog.askopenfilename(parent=win, title=f"Imagem {index + 1}: a original", filetypes=[
                ("Imagens", "*.png *.webp *.gif *.jpg *.jpeg"), ("Todos os arquivos", "*.*")])
            if not path:
                return
            try:
                with open(path, "rb") as fh:
                    box["html"] = replace_image(box["html"], index, fh.read())
            except Exception as err:
                info.configure(text=tk_safe(f"⚠ Não consegui usar esse arquivo: {err}"))
                return
            box["changed"] = True
            info.configure(text=f"✔ Imagem {index + 1} trocada. Clique em \"Salvar no modelo\" para valer nos próximos e-mails.")
            draw()

        def keep() -> None:
            if not box["changed"]:
                win.destroy()
                return
            state["html"] = box["html"]
            save()
            win.destroy()
            status.configure(text="✔ Imagens trocadas e modelo salvo com o formato.")
        bar = tk.Frame(win, bg=self.BG)
        bar.pack(fill="x", padx=self.px(12), pady=(self.px(4), self.px(10)))
        self.button(bar, "Salvar no modelo", keep, primary=True).pack(side="left")
        self.button(bar, "Fechar", win.destroy).pack(side="left", padx=self.px(6))
        self.template_images_win = win
        self.template_images_swap = swap
        draw()
        status.configure(text="")

    def open_templates(self) -> None:
        """Modelos de e-mail e assinatura: o Bit usa o que combina com cada situação."""
        if self.api is None:
            return
        tk = self.tk
        win = tk.Toplevel(self.root)
        win.title("Modelos de e-mail — Bit")
        win.configure(bg=self.BG)
        sw, sh = self.screen()
        w, h = min(self.px(820), sw - self.px(40)), min(self.px(600), sh - self.px(90))
        win.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 2)}")
        self.extra_windows = [x for x in getattr(self, "extra_windows", []) if alive(x)] + [win]
        self.root.after(200, self.later, exclude_from_capture, win)
        state: Dict[str, Any] = {"items": [], "n": None, "html": None}      # html: None = mantém o formato salvo
        top = tk.Frame(win, bg=self.BG)
        top.pack(fill="x", padx=self.px(12), pady=(self.px(10), 0))
        self.label(top, "Seus modelos de e-mail", 13, bold=True).pack(side="left")
        status = self.label(win, "O Bit usa o modelo que combina com a situação e mantém o seu texto e tom.", 9, self.MUTED,
                            wraplength=w - self.px(40))
        body = tk.Frame(win, bg=self.BG)
        left = tk.Frame(body, bg=self.BG)
        right = tk.Frame(body, bg=self.BG)
        listbox = tk.Listbox(left, bg=self.CARD, fg=self.INK, selectbackground=self.PRI, relief="flat", borderwidth=0,
                             font=("Segoe UI", 10), activestyle="none", exportselection=False, height=1)
        listbox.pack(fill="both", expand=True)
        entries: Dict[str, Any] = {}
        form = tk.Frame(right, bg=self.BG)
        form.pack(fill="x")
        form.grid_columnconfigure(1, weight=1)
        for i, (key, label) in enumerate((("nome", "Nome"), ("uso", "Quando usar"), ("assunto", "Assunto"))):
            self.label(form, label, 9, self.MUTED).grid(row=i, column=0, sticky="w", padx=(0, self.px(6)))
            e = tk.Entry(form, bg=self.CARD, fg=self.INK, insertbackground=self.INK, relief="flat", font=("Segoe UI", 10))
            e.grid(row=i, column=1, sticky="ew", ipady=self.px(2), pady=(0, self.px(3)))
            entries[key] = e
        text_head = tk.Frame(right, bg=self.BG)
        text_head.pack(fill="x")
        self.label(text_head, "Texto (use {{nome}}, {{pedido}}... onde muda)", 9, self.MUTED).pack(anchor="w")
        fmt = self.label(right, "", 9, "#7ddc9a", wraplength=int(w * 0.5))
        text = tk.Text(right, bg=self.CARD, fg=self.INK, insertbackground=self.INK, wrap="word", relief="flat",
                       font=("Segoe UI", 10), height=1, width=1, borderwidth=0, padx=self.px(6), pady=self.px(6))
        sig_box = tk.Frame(win, bg=self.BG)
        self.label(sig_box, "Assinatura (vai no fim de todo e-mail)", 9, self.MUTED).pack(anchor="w")
        sig_row = tk.Frame(sig_box, bg=self.BG)
        sig_row.pack(fill="x")
        sig = tk.Text(sig_row, bg=self.CARD, fg=self.INK, insertbackground=self.INK, wrap="word", relief="flat",
                      font=("Segoe UI", 10), height=2, width=1, borderwidth=0, padx=self.px(6), pady=self.px(4))

        def show(res: Dict[str, Any]) -> None:
            state["items"] = list(res.get("modelos") or [])
            listbox.delete(0, "end")
            for t in state["items"]:
                listbox.insert("end", tk_safe(("◆ " if t.get("tem_html") else "") + f"{t['nome']}"
                                              + (f" — {t['uso']}" if t.get("uso") else "")))
            if "assinatura" in res and not sig.get("1.0", "end").strip():
                sig.insert("1.0", res.get("assinatura") or "")
            if res.get("importados") is not None:
                sent = state.pop("sent", None) or {}
                if sent.get("html") and not res.get("formato"):
                    status.configure(text=OLD_SERVER_MSG)
                    return
                msg = f"✔ {res['importados']} modelo(s) importado(s)"
                if sent.get("html"):
                    msg += f", {sent['html']} com o formato (◆)"
                if sent.get("so_texto"):
                    msg += (". ⚠ Só o texto de: " + ", ".join(sent["so_texto"][:4]) + " — abra no Outlook, clique no "
                            "corpo, Ctrl+A, Ctrl+C e use \"Colar do Outlook\"")
                status.configure(text=msg + ".")

        def set_format(has_html: bool, fresh: bool = False) -> None:
            if has_html:
                fmt.configure(text=("✔ Formato copiado (tabelas, cores e imagens). Dê um nome e salve." if fresh else
                                    "◆ Com o formato do Outlook: o e-mail sai igual ao modelo. O texto abaixo é a referência "
                                    "do conteúdo; para mudar o visual, cole de novo do Outlook."))
                fmt.pack(anchor="w", before=text, pady=(0, self.px(3)))
            else:
                fmt.pack_forget()

        def pick(_e: Any = None) -> None:
            sel = listbox.curselection()
            if not sel:
                return
            t = state["items"][sel[0]]
            state["n"], state["html"] = t["n"], None
            set_format(bool(t.get("tem_html")))
            for key in entries:
                entries[key].delete(0, "end")
                entries[key].insert(0, t.get(key) or "")
            text.delete("1.0", "end")
            text.insert("1.0", t.get("corpo") or "")
        listbox.bind("<<ListboxSelect>>", pick)

        def new() -> None:
            state["n"], state["html"] = None, None
            set_format(False)
            listbox.selection_clear(0, "end")
            for e in entries.values():
                e.delete(0, "end")
            text.delete("1.0", "end")
            entries["nome"].focus_set()

        def call(payload: Dict[str, Any], msg: str) -> None:
            def done(res: Dict[str, Any]) -> None:
                show(res)
                status.configure(text=OLD_SERVER_MSG if payload.get("html") and not res.get("formato") else msg)
            self.run_bg(lambda: self.api.call("POST", "modelos", payload), done,
                        lambda m: status.configure(text="⚠ " + tk_safe(m)))

        def save() -> None:
            payload = {"acao": "salvar", "origem": "pc", "corpo": text.get("1.0", "end").strip(),
                       **{k: e.get().strip() for k, e in entries.items()}}
            if state["html"] is not None:
                payload["html"] = state["html"]
            call(payload, "✔ Modelo salvo" + (" com o formato do Outlook." if state["html"] else "."))

        def paste_outlook() -> None:
            """No Outlook: abra o modelo, Ctrl+A e Ctrl+C no corpo do e-mail; aqui, este botão. Vem com o formato."""
            html = clipboard_get_html()
            if not html:
                try:
                    plain = win.clipboard_get()
                except Exception:
                    plain = ""
                if not plain.strip():
                    status.configure(text="⚠ A área de transferência está vazia: no Outlook, abra o modelo, clique no corpo, "
                                          "Ctrl+A e Ctrl+C.")
                    return
                text.delete("1.0", "end")
                text.insert("1.0", plain.strip())
                status.configure(text="Colei só o texto (o que foi copiado não tinha formato).")
                return
            state["html"] = inline_local_images(html)
            text.delete("1.0", "end")
            text.insert("1.0", html_to_text(state["html"]))
            set_format(True, fresh=True)
            lost = len(re.findall(r"""(?i)\bsrc=["']?(?:blob:|cid:|file:)""", state["html"]))
            status.configure(text="✔ Colei com o formato. Campos como [Nome] ou {{nome}} são trocados a cada e-mail."
                             + (f" ⚠ {lost} imagem(ns) não vieram pela cópia: para elas, use \"Importar arquivo…\" com o "
                                ".oft (Arquivo → Salvar como no Outlook)." if lost else ""))

        def paste_key(_e: Any = None) -> Optional[str]:
            if IS_WINDOWS and clipboard_get_html():          # Ctrl+V com formato no campo do texto = Colar do Outlook
                paste_outlook()
                return "break"
            return None

        def import_file() -> None:
            from tkinter import filedialog
            paths = filedialog.askopenfilenames(parent=win, title="Modelos de e-mail", filetypes=[
                ("Modelos e e-mails", "*.oft *.msg *.eml *.htm *.html"), ("Todos os arquivos", "*.*")])
            if not paths:
                return
            status.configure(text="Lendo os arquivos (com o formato)…")

            def work() -> Dict[str, Any]:
                found = [t for t in (read_template_file(p) for p in paths) if t]
                if not found:
                    raise ApiError("Não consegui ler esses arquivos como e-mail (.oft, .msg, .eml ou .htm).")
                state["sent"] = {"html": sum(1 for t in found if t.get("html")),
                                 "so_texto": [t["nome"] for t in found if not t.get("html")]}
                return self.api.call("POST", "modelos", {"acao": "importar", "modelos": found}, timeout=120)
            self.run_bg(work, show, lambda m: status.configure(text="⚠ " + tk_safe(m)))

        def images() -> None:
            """v1.18: ver as imagens do modelo e trocar a que perdeu a transparência pela original."""
            if state["n"] is None or not any(t.get("n") == state["n"] and t.get("tem_html") for t in state["items"]):
                status.configure(text="Escolha na lista um modelo com formato (◆) para ver as imagens dele.")
                return
            status.configure(text="Abrindo as imagens do modelo…")
            self.run_bg(lambda: self.api.call("POST", "modelos", {"acao": "html", "n": state["n"]}),
                        lambda res: self.template_images_window(win, res, entries, text, status, state, save),
                        lambda m: status.configure(text="⚠ " + tk_safe(m)))

        def delete() -> None:
            if state["n"] is not None:
                call({"acao": "apagar", "n": state["n"]}, "✔ Modelo apagado.")
                new()

        def import_outlook() -> None:
            status.configure(text="Procurando os modelos (.oft) e a assinatura do Outlook…")

            def work() -> Dict[str, Any]:
                found, signatures = outlook_files()
                if not found and not signatures:
                    raise ApiError("Não achei modelos (.oft) nem assinaturas do Outlook neste PC. No Outlook novo, os "
                                   "modelos ficam na nuvem: abra cada um, copie e cole aqui.")
                sig_info = signatures[0] if signatures else {}
                state["sent"] = {"html": sum(1 for t in found if t.get("html")),
                                 "so_texto": [t["nome"] for t in found if not t.get("html")]}
                return self.api.call("POST", "modelos", {"acao": "importar", "modelos": found,
                                                         "assinatura": sig_info.get("texto", ""),
                                                         "assinatura_html": sig_info.get("html", "")}, timeout=180)

            def done(res: Dict[str, Any]) -> None:
                sig.delete("1.0", "end")
                show(res)
            self.run_bg(work, done, lambda m: status.configure(text="⚠ " + tk_safe(m)))

        actions = tk.Frame(top, bg=self.BG)
        actions.pack(side="right")
        self.button(actions, "Importar do Outlook", import_outlook, primary=True).pack(side="left")
        self.button(actions, "Importar arquivo…", import_file).pack(side="left", padx=(self.px(6), 0))
        self.button(text_head, "Colar do Outlook (com formato)", paste_outlook).pack(anchor="w", pady=(self.px(2), self.px(4)))
        self.button(left, "Imagens do modelo", images).pack(side="bottom", fill="x", pady=(self.px(6), 0), before=listbox)
        text.bind("<<Paste>>", paste_key)
        buttons = tk.Frame(right, bg=self.BG)
        buttons.pack(side="bottom", fill="x", pady=(self.px(6), 0))
        self.button(buttons, "Salvar modelo", save, primary=True).pack(side="left")
        self.button(buttons, "Novo", new).pack(side="left", padx=self.px(6))
        self.button(buttons, "Apagar", delete).pack(side="left")
        text.pack(fill="both", expand=True)
        self.button(sig_row, "Salvar assinatura",
                    lambda: call({"acao": "assinatura", "assinatura": sig.get("1.0", "end").strip()}, "✔ Assinatura salva.")
                    ).pack(side="right", padx=(self.px(6), 0))
        sig.pack(side="left", fill="x", expand=True)
        sig_box.pack(side="bottom", fill="x", padx=self.px(12), pady=(0, self.px(10)))
        status.pack(side="bottom", anchor="w", padx=self.px(12), pady=(self.px(4), self.px(4)))
        body.pack(fill="both", expand=True, padx=self.px(12), pady=self.px(8))
        left.pack(side="left", fill="both", expand=False, ipadx=self.px(80))
        right.pack(side="left", fill="both", expand=True, padx=(self.px(10), 0))
        self.templates_window, self.templates_list = win, listbox
        self.templates_actions = {"colar": paste_outlook, "salvar": save, "novo": new, "estado": state, "texto": text,
                                  "campos": entries, "imagens": images, "pick": pick}
        self.run_bg(lambda: self.api.call("GET", "modelos"), show, lambda m: status.configure(text="⚠ " + tk_safe(m)))

    def draw_suggestions(self) -> None:
        """Os botões "Preparar" (na janela e na barra) + as sugestões do que o Bit viu."""
        for holder in (getattr(self, "suggest_box", None), getattr(self, "bar_suggest", None)):
            if not alive(holder):
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
    def later(self, func: Callable[..., Any], win: Any, *args: Any) -> None:
        """Agendamentos ficam SEMPRE na janela principal (que nunca fecha) e conferem se a janela alvo ainda existe.
        Agendar na própria janelinha e fechá-la antes deixava o agendamento órfão: o Tk podia disparar outra função
        no lugar (foi o que fechava a barra compacta)."""
        if alive(win):
            func(win, *args)

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
                self.root.after(delay, give_focus_back, before)   # na janela principal: nunca "órfão"
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
        self.root.after(8000, self.drop_overlay, win)

    def drop_overlay(self, win: Any) -> None:
        if self.overlay is win:
            self.overlay = None
        try:
            win.destroy()
        except Exception:
            pass

    # ------------------------------------------------------------ avisos do Bit (no canto, empilhados)
    def poll_notices(self) -> None:
        while True:
            time.sleep(60)
            if self.api is None:
                continue
            try:
                for it in self.api.call("GET", "avisos", timeout=30).get("avisos") or []:
                    self.events.put(("aviso", (str(it.get("text") or ""), list(it.get("botoes") or []))))
            except Exception:
                pass

    # ------------------------------------------------------------ localização do Windows (v1.15)
    def location_loop(self) -> None:
        """Com a sua permissão, manda ao Bit a posição APROXIMADA do PC (Wi-Fi, pelo serviço de localização do Windows) a
        cada 45 min: o clima, o resumo e a rotina passam a saber a cidade sem cadastro. Pergunta uma vez."""
        time.sleep(20)
        if self.cfg.get("localizacao") is None and not self.cfg.get("localizacao_perguntada"):
            self.cfg["localizacao_perguntada"] = True
            save_config(self.cfg, self.config_path)
            self.events.put(("call", lambda: self.notice(
                "Quer que eu use a localização do Windows (aproximada, pelo Wi-Fi) para o clima e a rotina saberem a sua "
                "cidade sozinhos? Dá para mudar depois em \"Localização (clima)\".",
                action=("Usar a localização", lambda: (self.loc_var.set(True), self.save_options())))))
        while True:
            if self.cfg.get("localizacao") and self.api is not None:
                self.location_now(background=False)
            time.sleep(45 * 60)

    def location_now(self, background: bool = True) -> None:
        def work() -> Dict[str, Any]:
            pos = windows_location()
            if not pos:
                raise ApiError("A localização do Windows está desligada ou não respondeu (Configurações → Privacidade e "
                               "segurança → Localização).")
            return self.api.call("POST", "local", pos, timeout=30)

        def done(res: Dict[str, Any]) -> None:
            if res.get("onde"):
                self.cfg["ultima_cidade"] = str(res.get("cidade") or res["onde"])
                save_config(self.cfg, self.config_path)
        if background:
            self.run_bg(work, done, lambda msg: self.add_message("⚠ " + msg, "aviso"))
        else:
            try:
                done(work())
            except Exception:
                pass

    def press_data(self, data: str, label: str) -> None:
        """Botão de um aviso (v1.12): manda para o Bit como se fosse um botão da conversa."""
        if self.api is None:
            return
        self.add_message(label, "eu")
        self.run_bg(lambda: self.api.call("POST", "conversa", {"botao": data}, timeout=300), self.on_reply)

    def notice(self, text: str, action: Optional[Tuple[str, Callable[[], None]]] = None,
               buttons: Optional[List[Dict[str, str]]] = None) -> None:
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
        if action is not None:                                   # v1.9: aviso com um botão (ex.: transcrever a reunião)
            label, cmd = action
            tk.Button(inner, text=tk_safe(label), command=lambda t=toast: (self.close_toast(t), cmd()), relief="flat",
                      cursor="hand2", bg=self.PRI, fg="#0d1020", font=("Segoe UI", 9, "bold"), borderwidth=0,
                      padx=self.px(10), pady=self.px(3)).pack(anchor="w", padx=self.px(10), pady=(0, self.px(10)))
        feedback = [b for b in buttons or [] if str(b.get("data") or "").startswith("fb:")]
        for b in [b for b in buttons or [] if b not in feedback][:3]:   # v1.12: "✉️ Renovação — EMS"
            tk.Button(inner, text=tk_safe(b.get("label") or "Abrir"), relief="flat", cursor="hand2", bg=self.PRI,
                      fg="#0d1020", font=("Segoe UI", 9, "bold"), borderwidth=0, padx=self.px(10), pady=self.px(3),
                      command=lambda t=toast, d=str(b.get("data")), lb=str(b.get("label")): (self.close_toast(t),
                                                                                             self.press_data(d, lb))
                      ).pack(anchor="w", padx=self.px(10), pady=(0, self.px(6)))
        if feedback:
            row = tk.Frame(inner, bg=self.CARD)
            row.pack(anchor="w", padx=self.px(10), pady=(0, self.px(8)))
            for b in feedback[:2]:
                tk.Button(row, text=tk_safe(b.get("label") or "?"), relief="flat", cursor="hand2", bg=self.HOVER,
                          fg=self.INK, font=("Segoe UI", 8), borderwidth=0, padx=self.px(8), pady=self.px(2),
                          command=lambda t=toast, d=str(b.get("data")), lb=str(b.get("label")): (self.close_toast(t),
                                                                                                 self.press_data(d, lb))
                          ).pack(side="left", padx=(0, self.px(6)))
        close.bind("<Button-1>", lambda e=None, t=toast: self.close_toast(t))
        body.bind("<Button-1>", lambda e=None, t=toast: (self.close_toast(t), self.hotkey_open()))
        self.toasts.append(toast)
        while len(self.toasts) > 3:
            self.close_toast(self.toasts[0])
        self.layout_toasts()
        toast._bit_timer = self.root.after(12000, self.close_toast, toast)

    def close_toast(self, toast: Any) -> None:
        timer = getattr(toast, "_bit_timer", None)
        if timer:
            try:
                self.root.after_cancel(timer)
            except Exception:
                pass
            toast._bit_timer = None
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
                              ("reuniao", self.toggle_meeting, "Transcrever reunião (som do PC + microfone) e fazer a ata"),
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
        for i, (kind, text) in enumerate(self.KINDS):
            tk.Button(row1, text=tk_safe(text), relief="flat", cursor="hand2", bg=self.HOVER, fg=self.INK,
                      activebackground=self.PRI, font=("Segoe UI", 9), borderwidth=0, padx=self.px(6), pady=self.px(2),
                      command=lambda k=kind: self.prepare(k)).grid(row=i // 3, column=i % 3, sticky="ew",
                                                                   padx=(0, self.px(3)), pady=(0, self.px(3)))
        for col in range(3):
            row1.grid_columnconfigure(col, weight=1)
        tk.Button(self.bar_prep, text=tk_safe("☰ Meus modelos de e-mail"), command=self.open_templates, relief="flat",
                  cursor="hand2", bg=self.CARD, fg=self.PRI, activebackground=self.CARD, font=("Segoe UI", 9, "underline"),
                  borderwidth=0).pack(anchor="w")
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
        self.bar_suggest = None
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
            self.root.after(5000, self.bar_hint, "")
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
        if "reuniao" in self.bar_buttons:
            self.bar_buttons["reuniao"].configure(fg=self.RED if self.meeting is not None else self.INK)

    # ------------------------------------------------------------ reunião (v1.8)
    def toggle_meeting(self) -> None:
        if self.meeting is not None:
            self.stop_meeting()
        else:
            self.ask_meeting()

    def ask_meeting(self) -> None:
        """Antes de começar: nome, o que ouvir e o aviso aos participantes."""
        if self.api is None:
            return
        tk = self.tk
        win = tk.Toplevel(self.root)
        win.title("Transcrever reunião — Bit")
        win.configure(bg=self.BG)
        win.resizable(False, False)
        self.root.after(200, self.later, exclude_from_capture, win)
        box = tk.Frame(win, bg=self.BG)
        box.pack(fill="both", expand=True, padx=self.px(14), pady=self.px(12))
        self.label(box, "Transcrever reunião", 13, bold=True).pack(anchor="w")
        self.label(box, "Nome (opcional)", 9, self.MUTED).pack(anchor="w", pady=(self.px(8), 0))
        name = tk.Entry(box, bg=self.CARD, fg=self.INK, insertbackground=self.INK, relief="flat", font=("Segoe UI", 10),
                        width=36)
        name.pack(fill="x", ipady=self.px(4))
        system_var, mic_var = tk.BooleanVar(value=True), tk.BooleanVar(value=True)
        split_var = tk.BooleanVar(value=bool(self.cfg.get("reuniao_separar", True)))
        for text, var in (("Som do PC (os outros participantes: Teams, Meet, Zoom)", system_var),
                          ("Meu microfone (a minha voz)", mic_var),
                          ("Separar quem fala: eu × os outros (melhor com fone de ouvido)", split_var)):
            tk.Checkbutton(box, text=text, variable=var, bg=self.BG, fg=self.INK, selectcolor=self.CARD,
                           activebackground=self.BG, activeforeground=self.INK, font=("Segoe UI", 9),
                           anchor="w").pack(fill="x", pady=(self.px(4), 0))
        self.label(box, "Avise os participantes de que a reunião será transcrita. O áudio vai, em pedaços, para a "
                        "transcrição do Bit e não fica guardado; ficam o texto e a ata (com as tarefas para você aprovar).",
                   8, self.MUTED, wraplength=self.px(380)).pack(anchor="w", pady=(self.px(8), 0))
        row = tk.Frame(box, bg=self.BG)
        row.pack(fill="x", pady=(self.px(10), 0))

        def go() -> None:
            title, system, mic = name.get().strip(), system_var.get(), mic_var.get()
            self.cfg["reuniao_separar"] = bool(split_var.get())
            save_config(self.cfg, self.config_path)
            win.destroy()
            if not (system or mic):
                self.error("Escolha o som do PC, o microfone ou os dois.")
                return
            self.start_meeting(title, system, mic)
        self.button(row, "Começar", go, primary=True).pack(side="left")
        self.button(row, "Cancelar", win.destroy).pack(side="left", padx=self.px(6))
        name.bind("<Return>", lambda e: go())
        win.update_idletasks()
        sw, sh = self.screen()
        win.geometry(f"+{max(0, (sw - win.winfo_reqwidth()) // 2)}+{max(0, (sh - win.winfo_reqheight()) // 3)}")
        win.lift()
        name.focus_force()
        self.meeting_dialog = win

    def start_meeting(self, title: str = "", system: bool = True, mic: bool = True,
                      sources: Optional[List[AudioSource]] = None, split: Optional[bool] = None) -> None:
        if self.meeting is not None or self.api is None:
            return

        def work() -> Tuple[List[AudioSource], Callable[[], None], List[str], Dict[str, Any]]:
            if sources is not None:
                srcs, close, notes = sources, (lambda: None), []
            else:
                srcs, close, notes = open_meeting_sources(system, mic)
            try:
                res = self.api.call("POST", "reuniao", {"titulo": title})
            except Exception:
                close()
                raise
            return srcs, close, notes, res

        def done(out: Tuple[List[AudioSource], Callable[[], None], List[str], Dict[str, Any]]) -> None:
            srcs, close, notes, res = out
            m: Dict[str, Any] = {"id": int(res["id"]), "title": str(res.get("title") or title or "Reunião"), "close": close,
                                 "queue": queue.Queue(), "stop": threading.Event(), "t0": time.time(), "sent": 0,
                                 "lost": 0, "lines": [], "notes": notes, "sources": [s.name for s in srcs]}
            want_split = (self.cfg.get("reuniao_separar", True) if split is None else split)
            by_name = {s.name: s for s in srcs}
            if want_split and "som do PC" in by_name and "microfone" in by_name:   # v1.13: "Você" × "Participantes"
                m["recorder"] = SplitMeeting(by_name["som do PC"], by_name["microfone"],
                                             lambda wav, seq, t, who: m["queue"].put((wav, seq, t, who)))
                m["split"] = True
            else:
                m["recorder"] = MeetingRecorder(srcs, lambda wav, seq, t: m["queue"].put((wav, seq, t, "")))
            self.meeting = m
            self.open_meeting_window(m)
            threading.Thread(target=self.meeting_ticker, args=(m,), daemon=True, name="BitReuniaoAudio").start()
            threading.Thread(target=self.meeting_uploader, args=(m,), daemon=True, name="BitReuniaoEnvio").start()
            self.add_message(f"⏺ Transcrevendo \"{m['title']}\" ({' + '.join(m['sources'])})."
                             + (" " + " ".join(notes) if notes else ""), "bit")
            self.update_bar()
        self.run_bg(work, done, lambda msg: self.error(f"Não consegui começar a transcrição: {msg}"))

    def meeting_ticker(self, m: Dict[str, Any]) -> None:
        step, nxt = 0.5, time.monotonic()
        while not m["stop"].is_set():
            nxt += step
            m["stop"].wait(max(0.0, nxt - time.monotonic()))
            try:
                m["recorder"].tick(step)
            except Exception as err:
                m["notes"].append(f"áudio: {type(err).__name__}")
        try:
            m["recorder"].flush()
        finally:
            m["close"]()
            m["ticker_done"] = True

    def meeting_uploader(self, m: Dict[str, Any]) -> None:
        while True:
            try:
                wav, _seq, t, who = m["queue"].get(timeout=0.5)
            except queue.Empty:
                if m.get("ticker_done"):
                    break
                continue
            m["upload_seq"] = m.get("upload_seq", -1) + 1           # na ordem de envio (os 2 fluxos numa fila só)
            body = {"audio": base64.b64encode(wav).decode("ascii"), "seq": m["upload_seq"], "t": t}
            if who:
                body["quem"] = who
            for attempt in range(4):
                try:
                    res = self.api.call("POST", f"reuniao/{m['id']}/audio", body, timeout=120)
                    m["sent"] += 1
                    line = str(res.get("texto") or "").strip()
                    if line:
                        m["lines"].append(line)
                        self.events.put(("call", lambda ln=line: self.meeting_line(m, ln)))
                    break
                except Exception as err:
                    if attempt == 3:
                        m["lost"] += 1
                        msg = str(err)
                        self.events.put(("call", lambda: self.meeting_status(m, f"⚠ Um trecho não foi transcrito: {msg[:120]}")))
                    else:
                        time.sleep(2 * (attempt + 1))
        self.events.put(("call", lambda: self.meeting_status(m, "Fazendo a ata…")))
        try:
            self.api.call("POST", f"reuniao/{m['id']}/fim", {})
            for _ in range(150):                                   # até ~20 min (reuniões longas)
                time.sleep(8 if not self.test_mode else 0.2)
                st = self.api.call("GET", f"reuniao/{m['id']}")
                if st.get("status") in ("pronta", "erro"):
                    self.events.put(("call", lambda: self.meeting_done(m, st)))
                    return
            self.events.put(("call", lambda: self.meeting_status(m, "A ata está demorando: ela chega nos avisos do Bit.")))
        except Exception as err:
            msg = str(err)
            self.events.put(("call", lambda: self.meeting_status(m, f"⚠ Não consegui pedir a ata: {msg[:160]}")))

    def stop_meeting(self) -> None:
        m = self.meeting
        if m is None:
            return
        m["stop"].set()
        self.meeting = None
        self.meeting_status(m, "Encerrando: enviando o último trecho…")
        if alive(m.get("stop_btn")):
            m["stop_btn"].configure(state="disabled")
        self.add_message(f"■ Reunião \"{m['title']}\" encerrada. Faço a ata e aviso aqui.", "bit")
        self.update_bar()

    def open_meeting_window(self, m: Dict[str, Any]) -> None:
        tk = self.tk
        win = tk.Toplevel(self.root)
        win.title(tk_safe(f"Reunião — {m['title']}"))
        win.configure(bg=self.BG)
        sw, sh = self.screen()
        w, h = min(self.px(460), sw - self.px(40)), min(self.px(420), sh - self.px(90))
        win.geometry(f"{w}x{h}+{max(0, sw - w - self.px(30))}+{self.px(60)}")
        self.root.after(200, self.later, exclude_from_capture, win)
        win.protocol("WM_DELETE_WINDOW", win.withdraw)                 # fechar a janela não para a transcrição
        head = tk.Frame(win, bg=self.BG)
        head.pack(fill="x", padx=self.px(10), pady=(self.px(8), 0))
        m["clock"] = self.label(head, "● Transcrevendo 00:00", 10, self.RED, bold=True)
        m["clock"].pack(side="left")
        m["status"] = self.label(win, "Os trechos aparecem aqui a cada ~25 segundos.", 8, self.MUTED,
                                 wraplength=w - self.px(30))
        buttons = tk.Frame(win, bg=self.BG)
        buttons.pack(side="bottom", fill="x", padx=self.px(10), pady=self.px(8))
        m["stop_btn"] = self.button(buttons, "■ Parar e fazer a ata", self.stop_meeting, primary=True)
        m["stop_btn"].pack(side="left")
        self.button(buttons, "Esconder", win.withdraw).pack(side="right")
        m["status"].pack(side="bottom", anchor="w", padx=self.px(10))
        text = tk.Text(win, bg=self.CARD, fg=self.INK, wrap="word", relief="flat", font=("Segoe UI", 10), width=1, height=1,
                       padx=self.px(8), pady=self.px(8), borderwidth=0, state="disabled")
        text.pack(fill="both", expand=True, padx=self.px(10), pady=(self.px(6), 0))
        m["text"], m["window"] = text, win
        self.meeting_windows = [x for x in getattr(self, "meeting_windows", []) if alive(x)] + [win]
        self.root.after(1000, self.meeting_clock, m)

    def meeting_clock(self, m: Dict[str, Any]) -> None:
        if not alive(m.get("clock")) or m["stop"].is_set():
            return
        s = int(time.time() - m["t0"])
        m["clock"].configure(text=tk_safe(f"● Transcrevendo {s // 3600:d}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600
                                          else f"● Transcrevendo {s // 60:02d}:{s % 60:02d}"))
        self.root.after(1000, self.meeting_clock, m)

    def meeting_line(self, m: Dict[str, Any], line: str) -> None:
        text = m.get("text")
        if alive(text):
            text.configure(state="normal")
            text.insert("end", tk_safe(line) + "\n")
            text.see("end")
            text.configure(state="disabled")

    def meeting_status(self, m: Dict[str, Any], msg: str) -> None:
        if alive(m.get("status")):
            m["status"].configure(text=tk_safe(msg))
        if m["stop"].is_set() and alive(m.get("clock")):
            m["clock"].configure(text=tk_safe("■ Encerrada"), fg=self.MUTED)

    def meeting_done(self, m: Dict[str, Any], st: Dict[str, Any]) -> None:
        if st.get("status") == "erro":
            self.meeting_status(m, f"⚠ A ata falhou: {str(st.get('erro') or '')[:160]}")
            self.error(f"A ata da reunião falhou: {str(st.get('erro') or '')[:160]}")
            return
        self.meeting_status(m, "✔ Ata pronta.")
        if alive(m.get("window")):
            m["window"].destroy()
        self.open_result({"tipo": "texto", "titulo": f"Ata — {m['title']}", "texto": str(st.get("ata") or "")})
        if st.get("arquivo"):
            self.run_bg(lambda: self.save_files([str(st["arquivo"])]), self.on_saved)
        self.meeting_result = st

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
        more.bind("<Button-1>", lambda e=None: self.show_window())
        close = tk.Label(foot, text="✕", bg=self.CARD, fg=self.MUTED, font=("Segoe UI", 10), cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda e=None, b=bubble: self.close_bubble(b))
        self.position_bubble()
        if not wait:
            bubble._bit_timer = self.root.after(30000, self.close_bubble, bubble)

    def close_bubble(self, bubble: Any) -> None:
        timer = getattr(bubble, "_bit_timer", None)
        if timer:
            try:
                self.root.after_cancel(timer)
            except Exception:
                pass
            bubble._bit_timer = None
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


def alive(widget: Any) -> bool:
    """A janela ainda existe? (mexer numa janela fechada derruba o callback do Tk)"""
    try:
        return widget is not None and bool(widget.winfo_exists())
    except Exception:
        return False


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
TEST_PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
TEST_HTML = ('<html><body><table style="border:2px solid #0a5"><tr><td style="color:#c00;font-family:Arial">Olá, Ana!</td>'
             f'</tr><tr><td>Segue o orçamento.</td></tr></table><img src="data:image/png;base64,{TEST_PNG}"></body></html>')


class FakeApi:
    saved: Optional[Dict[str, Any]] = None
    meeting: Dict[str, Any] = {}

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
            if (body or {}).get("tipo") == "cliente":
                return {"tipo": "cliente", "titulo": "Plano de conta — ACME", "texto": "🏢 Plano de conta — ACME\n\nResumo...",
                        "resumo": "Cliente recorrente, sem compras há 4 meses: reativar com oferta.", "arquivos": [],
                        "assunto": "Novidades para a ACME", "corpo": "Olá, Maria! ..."}
            if (body or {}).get("tipo") == "email":
                return {"tipo": "email", "titulo": "Resposta à Ana", "para": "ana@empresa.com", "assunto": "Re: Orçamento",
                        "modelo": "Follow-up de orçamento",
                        "corpo": "Olá, Ana!\n\nSegue o orçamento dos 3 notebooks, com entrega até 12/12. 👍\n\nAbraços,\n[SEU NOME]",
                        "arquivos": [], **({"corpo_html": TEST_HTML, "formato": "campos"}
                                           if "formato" in str((body or {}).get("pedido")) else {})}
            return {"tipo": "planilha", "titulo": "Orçamento", "arquivos": ["planilha_orcamento.xlsx"]}
        if sub == "ao-vivo/fim":
            return {"texto": "Encerrado."}
        if sub == "reuniao":
            FakeApi.meeting = {"chunks": 0, "fim": False}
            return {"id": 9, "title": (body or {}).get("titulo") or "Reunião"}
        if sub == "reuniao/9/audio":
            FakeApi.meeting["chunks"] += 1
            assert str((body or {}).get("audio") or "").startswith("UklGR")             # WAV em base64
            return {"texto": f"[00:{int((body or {}).get('t') or 0):02d}] Trecho {FakeApi.meeting['chunks']} da reunião"}
        if sub == "reuniao/9/fim":
            FakeApi.meeting["fim"] = True
            return {"id": 9, "status": "processando"}
        if sub == "reuniao/9":
            return {"id": 9, "status": "pronta", "ata": "📝 Ata — Reunião de teste\nResumo: tudo certo.", "arquivo": ""}
        if sub == "modelos":
            items = [{"n": 1, "nome": "Follow-up de orçamento", "uso": "3 dias depois da proposta", "assunto": "Proposta {{pedido}}",
                      "corpo": "Olá, {{nome}}!\n\nConseguiu avaliar a proposta?\n\nAbraço,", "usos": 2},
                     {"n": 2, "nome": "Boas-vindas", "uso": "cliente novo", "assunto": "", "corpo": "Olá! Seja bem-vindo.",
                      "usos": 0, "tem_html": True, "origem": "outlook"}]
            if (body or {}).get("acao") == "salvar":
                self.saved = body
            if (body or {}).get("acao") == "html":
                return {"n": 2, "nome": "Boas-vindas", "html": TEST_HTML}
            return {"modelos": items, "assinatura": "Gabriel · Comercial", "formato": True}
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
            app.notice("🔁 EMS S/A: renovação de Figma Enterprise em 30 dias (10/03/2027).",       # v1.12: botões
                       buttons=[{"label": "✉️ Renovação — EMS S/A", "data": "cs:ren:1:20270310"},
                                {"label": "👍 Ajudou", "data": "fb:+:clientes"},
                                {"label": "👎 Não ajudou", "data": "fb:-:clientes"}])
            labels = [t for toast in app.toasts for t in texts(toast)]
            result["aviso_botoes"] = [x for x in labels if "Renovação" in x or "ajudou" in x.lower()]
            if len(result["aviso_botoes"]) < 3:
                problems.append(f"aviso com botões: {result['aviso_botoes']}")
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
            app.prepare("email", "Responder à Ana no formato do modelo")
            app.prepare("planilha", "")
            app.ask("pesquise esse cliente pra mim")
            app.root.after(1500, step2b)

        def step2b() -> None:
            app.pump()
            app.root.update()
            result["resultado_email"] = len(app.result_windows)
            formatted = [w for w in app.result_windows if alive(w) and any("Copiar formatado" in t for t in texts(w))]
            result["email_formatado"] = len(formatted)
            if not formatted:
                problems.append("e-mail no formato do modelo: sem o botão \"Copiar formatado\"")
            else:
                fits(formatted[0], "e-mail formatado")
            if len(app.result_windows) < 3:
                problems.append("pesquisa do cliente: a janela do e-mail de abordagem não abriu")
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
            result["vivas_no_fim"] = {"barra": alive(app.bar), "balao": alive(app.bubble), "sugestoes_barra": alive(app.bar_suggest)}
            app.open_templates()
            app.root.after(800, step3b)

        def step3b() -> None:
            app.pump()
            win = getattr(app, "templates_window", None)
            if not alive(win):
                problems.append("janela de modelos: não abriu")
            else:
                fits(win, "modelos de e-mail")
                result["modelos_na_lista"] = app.templates_list.size()
                if result["modelos_na_lista"] != 2:
                    problems.append("janela de modelos: a lista não carregou")
                elif not app.templates_list.get(1).startswith("◆"):
                    problems.append("janela de modelos: sem a marca de formato")
            # formato do Outlook: copiar com HTML -> "Colar do Outlook" guarda o HTML -> salvar manda o HTML
            result["eml_ida_e_volta"] = False
            eml = read_template_file(write_eml("ana@x.com", "Orçamento", "Olá, Ana!", TEST_HTML))
            result["eml_ida_e_volta"] = bool(eml and "<table" in eml["html"] and "data:image/png" in eml["html"]
                                             and eml["assunto"] == "Orçamento")
            if not result["eml_ida_e_volta"]:
                problems.append("rascunho .eml: perdeu o formato ou a imagem")
            if IS_WINDOWS and alive(win):
                ok_set = clipboard_set_html(images_to_files(TEST_HTML), "Olá, Ana!")
                got = clipboard_get_html()
                result["area_transferencia_html"] = bool(ok_set and "<table" in got and "color:#c00" in got)
                if not result["area_transferencia_html"]:
                    problems.append(f"área de transferência com formato: não voltou igual ({got[:120]!r})")
                try:                                                    # Outlook novo: o script de colar roda e,
                    uia = _powershell(NEW_OUTLOOK_PASTE_PS, {"assunto": "teste-bit", "espera": 2}, timeout=40)
                    result["outlook_novo_colar"] = uia.get("motivo") if isinstance(uia, dict) else str(uia)
                    result["localizacao_windows"] = bool(windows_location())     # diagnóstico (o CI não tem GPS/Wi-Fi)
                    probe_html = ('<style>.hero{background-color:rgb(31,42,26)}</style><body bgcolor="#ffffff"><table>'
                                  '<tr><td style="background:#ffffff"><p>Olá<td>x<tr><td class="hero"><p><span>'
                                  '<img src="data:image/png;base64,AAAA"></span><tr><td><img src="https://x/y.png"></table>')
                    result["navegador"] = os.path.basename(find_browser())
                    measured = browser_backgrounds(probe_html)                  # v1.16: o Edge mede as cores de fundo
                    result["fundos_navegador"] = measured
                    if result["navegador"] and measured != [(31, 42, 26), (255, 255, 255)]:
                        problems.append(f"cores de fundo pelo navegador: {measured}")
                    result["outlook_tipo"] = outlook_flavor()           # sem Outlook novo aberto, não cola nada
                    if result["outlook_novo_colar"] not in ("sem_janela", "colado"):
                        problems.append(f"Outlook novo: script de colar respondeu {result['outlook_novo_colar']}")
                except Exception as err:
                    problems.append(f"Outlook novo: script de colar falhou: {err}")
                acts = app.templates_actions
                acts["novo"]()
                acts["colar"]()
                acts["campos"]["nome"].insert(0, "Orçamento formatado")
                acts["salvar"]()
                html_kept = str(acts["estado"].get("html") or "")
                result["colar_do_outlook"] = "<table" in html_kept and "data:image/png" in html_kept
                if not result["colar_do_outlook"]:
                    problems.append("Colar do Outlook: o formato (ou a imagem) não ficou no modelo")
            before = [float(w.attributes("-alpha")) for w in app.our_windows()]
            app.hidden_capture(None)                                      # o Bit some na foto e volta igual
            after = [float(w.attributes("-alpha")) for w in app.our_windows()]
            result["janelas_escondidas_na_foto"] = getattr(app, "last_hidden", 0)
            if before != after or not result["janelas_escondidas_na_foto"]:
                problems.append(f"foto sem o Bit: transparência não voltou ({before} -> {after})")
            if IS_WINDOWS:
                probe = app.tk.Toplevel(app.root)                       # janela comum (as do Bit saem pretas de propósito)
                probe.geometry("420x260+40+40")
                app.tk.Label(probe, text="janela de teste", bg="#2266cc", fg="white", font=("Segoe UI", 20)).pack(
                    fill="both", expand=True)
                probe.update()
                time.sleep(0.3)
                direct_shot = print_window(hwnd_of(probe))
                probe.destroy()
                result["printwindow"] = list(direct_shot[0].size) if direct_shot else None
                if direct_shot is None:
                    problems.append("PrintWindow não fotografou a janela")
            shot("modelos")
            try:                                                  # v1.18: "Imagens do modelo" (ver e trocar a original)
                app.templates_list.selection_clear(0, "end")
                app.templates_list.selection_set(1)
                app.templates_actions["pick"]()
                app.templates_actions["imagens"]()
            except Exception as err:
                problems.append(f"imagens do modelo: {type(err).__name__}: {err}")
            app.root.after(1500, step_images)

        def step_images() -> None:
            app.pump()
            box = getattr(app, "template_images_win", None)
            result["imagens_do_modelo"] = bool(box is not None and alive(box))
            if not result["imagens_do_modelo"]:
                problems.append("a janela \"Imagens do modelo\" não abriu")
            else:
                shot("imagens_modelo")
                box.destroy()
            if alive(getattr(app, "templates_window", None)):
                app.templates_window.destroy()
            app.root.after(100, step_meet)

        def step_meet() -> None:                                  # v1.8: transcrever reunião (com um som de teste)
            global MEET_CHUNK_S
            MEET_CHUNK_S = 1.0
            if IS_WINDOWS:                                        # diagnóstico: o Windows do GitHub costuma não ter áudio
                try:
                    srcs, close, notes = open_meeting_sources()
                    result["audio_real"] = [x.name for x in srcs] + notes
                    close()
                except Exception as err:
                    result["audio_real"] = f"{type(err).__name__}: {str(err)[:160]}"
            src = AudioSource("teste", 2, 48000)
            import math

            def feed() -> None:
                for k in range(30):                               # 3 s de um "lá" (440 Hz) em estéreo, 48 kHz
                    block = _array("h")
                    for i in range(4800):
                        v = int(6000 * math.sin(2 * math.pi * 440 * (k * 4800 + i) / 48000))
                        block.extend((v, v))
                    src.feed(block.tobytes())
                    time.sleep(0.1)
            threading.Thread(target=feed, daemon=True).start()
            app.start_meeting("Reunião de teste", sources=[src])
            app.root.after(2600, step_meet2)

        def step_meet2() -> None:
            app.pump()
            m = app.meeting
            if m is None or not alive(m.get("window")):
                problems.append("reunião: a janela da transcrição não abriu")
            else:
                fits(m["window"], "reunião")
                shot("reuniao")
                app.stop_meeting()
            app.root.after(2500, step_meet3)

        def step_meet3() -> None:
            app.pump()
            got = getattr(FakeApi, "meeting", {}) or {}
            result["reuniao"] = dict(got)
            if not got.get("chunks"):
                problems.append("reunião: nenhum trecho de áudio foi enviado")
            if not got.get("fim"):
                problems.append("reunião: a ata não foi pedida no fim")
            atas = [w for w in app.result_windows if alive(w) and "Ata" in str(w.title())]
            if not atas:
                problems.append("reunião: a ata não abriu")
            first = app.maybe_suggest_meeting("Reunião semanal | Microsoft Teams")    # v1.9: oferece transcrever
            again = app.maybe_suggest_meeting("Reunião semanal | Microsoft Teams")
            app.pump()
            buttons = [t for toast in app.toasts for t in texts(toast) if "Transcrever" in t]
            result["sugestao_reuniao"] = [first, again, len(buttons)]
            if first != "Teams" or again is not None or not buttons:
                problems.append(f"reunião: sugestão de transcrever falhou ({result['sugestao_reuniao']})")
            app.root.after(100, step3c)

        def step3c() -> None:
            shot("compacto")
            for w in app.result_windows:
                w.destroy()
            app.stop_live()
            app.show_window()
            result["problemas"] = problems
            result["ok"] = (result["janelas"] == 4 and not problems and result["protect_roundtrip"]
                            and result["link"] is not None)
            app.root.after(300, app.root.destroy)
        def guarded(step: Callable[[], None]) -> Callable[[], None]:
            def run() -> None:
                try:
                    step()
                except Exception as err:                      # um erro no teste não pode travar o build
                    import traceback
                    problems.append(f"{step.__name__}: {type(err).__name__}: {err}")
                    result["trace"] = traceback.format_exc()[-1500:]
                    result["problemas"] = problems
                    app.root.after(100, app.root.destroy)
            run.__name__ = step.__name__
            return run
        step1, step2, step2b, step3 = guarded(step1), guarded(step2), guarded(step2b), guarded(step3)
        step3b, step3c = guarded(step3b), guarded(step3c)
        step_meet, step_meet2, step_meet3 = guarded(step_meet), guarded(step_meet2), guarded(step_meet3)
        app.root.after(900, step1)
        app.root.after(150000, lambda: (problems.append("tempo esgotado (150 s)"), app.root.destroy()))
        app.run()
        result.setdefault("problemas", problems)
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
