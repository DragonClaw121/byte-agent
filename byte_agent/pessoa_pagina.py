"""Painel pessoal de um convidado (v13.42): <endereço>/u/<agente>#<código>. Conversa e comandos em botões.

O código vai depois do "#": o navegador não o envia em nenhum pedido (não aparece em registros de servidor); a página
o guarda neste aparelho e tira da barra de endereço. As chamadas usam Authorization: Bearer <código>.
"""
from __future__ import annotations

import html
import json

_PAGE = r"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="referrer" content="no-referrer">
<title>__TITLE__</title>
<style>
:root { --bg:#f6f7fb; --card:#fff; --ink:#1d2330; --muted:#677086; --line:#e3e6ee; --pri:#3b5bdb; --pri-ink:#fff;
        --me:#e7ecff; --bot:#fff; --warn:#b4231b; }
@media (prefers-color-scheme: dark) { :root { --bg:#12151c; --card:#1b1f29; --ink:#e7e9ef; --muted:#9aa3b5; --line:#2a3040;
        --pri:#6d8cff; --pri-ink:#0d1020; --me:#26304d; --bot:#1b1f29; --warn:#ff8a80; } }
* { box-sizing:border-box; }
html, body { margin:0; height:100%; background:var(--bg); color:var(--ink);
  font:15px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
.wrap { max-width:760px; margin:0 auto; height:100%; display:flex; flex-direction:column; padding:0 16px; }
header { padding:14px 0 8px; display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; }
header h1 { font-size:1.15rem; margin:0; }
header .sub { color:var(--muted); font-size:0.85rem; }
nav { display:flex; gap:6px; margin-bottom:8px; }
nav button { flex:1; border:1px solid var(--line); background:var(--card); color:var(--ink); border-radius:10px; padding:8px;
  font:inherit; cursor:pointer; }
nav button.on { background:var(--pri); color:var(--pri-ink); border-color:var(--pri); }
#chat, #cmds { flex:1; overflow-y:auto; padding:4px 0 12px; }
.msg { max-width:88%; padding:9px 12px; border-radius:14px; margin:6px 0; white-space:pre-wrap; word-wrap:break-word;
  border:1px solid var(--line); }
.me { margin-left:auto; background:var(--me); border-bottom-right-radius:4px; }
.bot { background:var(--bot); border-bottom-left-radius:4px; }
.msg .btns { display:flex; gap:6px; flex-wrap:wrap; margin-top:8px; }
.msg .btns button, .chip { border:1px solid var(--pri); color:var(--pri); background:transparent; border-radius:999px;
  padding:6px 12px; font:inherit; font-size:0.9rem; cursor:pointer; }
.group h2 { font-size:0.8rem; text-transform:uppercase; letter-spacing:0.05em; color:var(--muted); margin:14px 0 6px; }
.chips { display:flex; flex-wrap:wrap; gap:8px; }
form { display:flex; gap:8px; padding:10px 0 calc(12px + env(safe-area-inset-bottom)); border-top:1px solid var(--line); }
textarea { flex:1; resize:none; border:1px solid var(--line); border-radius:12px; padding:10px; font:inherit; color:var(--ink);
  background:var(--card); min-height:44px; max-height:140px; }
form button { border:0; background:var(--pri); color:var(--pri-ink); border-radius:12px; padding:0 16px; font:inherit; cursor:pointer; }
.note { color:var(--muted); font-size:0.85rem; text-align:center; margin:18px 0; }
.err { color:var(--warn); }
.hidden { display:none !important; }
</style></head>
<body><div class="wrap">
<header><h1 id="title">__TITLE__</h1><span class="sub" id="who">conectando…</span></header>
<nav><button id="t-chat" class="on" onclick="tab('chat')">💬 Conversa</button><button id="t-cmds" onclick="tab('cmds')">⚡ Comandos</button></nav>
<div id="chat"><p class="note">Seu espaço pessoal: o que você conversa aqui é só seu.</p></div>
<div id="cmds" class="hidden"></div>
<form id="f" onsubmit="return enviar(event)"><textarea id="t" rows="1" placeholder="Escreva uma mensagem…"
  onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();enviar(event);}"></textarea><button>Enviar</button></form>
</div>
<script>
const AGENT = __AGENT__;
const BASE = location.pathname.replace(/\/+$/, '');
const KEY = 'byte_pessoa:' + BASE;
let token = '';
try { token = localStorage.getItem(KEY) || ''; } catch (e) {}
if (location.hash.length > 20) {
  token = decodeURIComponent(location.hash.slice(1));
  try { localStorage.setItem(KEY, token); } catch (e) {}
  history.replaceState(null, '', location.pathname);
}
const $ = id => document.getElementById(id);
function tab(name) {
  $('chat').classList.toggle('hidden', name !== 'chat'); $('cmds').classList.toggle('hidden', name !== 'cmds');
  $('t-chat').classList.toggle('on', name === 'chat'); $('t-cmds').classList.toggle('on', name === 'cmds');
}
async function api(path, body) {
  const r = await fetch(BASE + '/api/' + path, { method: body ? 'POST' : 'GET',
    headers: Object.assign({ 'Authorization': 'Bearer ' + token }, body ? { 'Content-Type': 'application/json' } : {}),
    body: body ? JSON.stringify(body) : undefined });
  const data = await r.json().catch(() => ({ error: 'Resposta inválida do servidor.' }));
  if (!r.ok && !data.error) data.error = 'Erro ' + r.status;
  return data;
}
const HKEY = 'byte_pessoa_hist:' + BASE;
let hist = [];
try { hist = JSON.parse(localStorage.getItem(HKEY) || '[]'); } catch (e) { hist = []; }
function guardar(text, mine) {
  hist.push([text, mine ? 1 : 0]); hist = hist.slice(-60);
  try { localStorage.setItem(HKEY, JSON.stringify(hist)); } catch (e) {}
}
function bubble(text, mine, buttons, keep) {
  if (keep) guardar(text, mine);
  const d = document.createElement('div'); d.className = 'msg ' + (mine ? 'me' : 'bot'); d.textContent = text;
  if (buttons && buttons.length) {
    const b = document.createElement('div'); b.className = 'btns';
    buttons.forEach(x => { const e = document.createElement('button'); e.type = 'button'; e.textContent = x.label;
      e.onclick = () => { b.querySelectorAll('button').forEach(y => y.disabled = true); mandar('', x.data, x.label); }; b.appendChild(e); });
    d.appendChild(b);
  }
  $('chat').appendChild(d); $('chat').scrollTop = $('chat').scrollHeight; return d;
}
async function mandar(text, callback, label) {
  tab('chat'); bubble(label || text, true, null, true);
  const wait = bubble('…', false);
  const res = await api('command', { text: text || '', callback: callback || '' });
  wait.remove();
  if (res.error) { const e = bubble('⚠️ ' + res.error, false); e.classList.add('err'); return; }
  (res.messages || []).forEach(m => bubble(m.text, false, m.buttons, true));
}
function enviar(ev) {
  if (ev) ev.preventDefault();
  const v = $('t').value.trim(); if (!v) return false;
  $('t').value = ''; mandar(v); return false;
}
async function carregar() {
  if (!token) { $('who').textContent = 'link sem código'; bubble('Este link está incompleto. Peça /painel no Telegram ou um link novo para quem te convidou.', false); $('f').classList.add('hidden'); return; }
  const eu = await api('eu');
  if (eu.error) { $('who').textContent = 'sem acesso'; const e = bubble('⛔ ' + eu.error, false); e.classList.add('err'); $('f').classList.add('hidden'); return; }
  document.title = AGENT + ' · ' + eu.nome;
  hist.forEach(h => bubble(h[0], !!h[1]));
  $('who').textContent = eu.nome.split(' ')[0] + ' · hoje ' + eu.uso + '/' + eu.limite + ' mensagens';
  const c = await api('commands');
  const groups = {};
  (c.commands || []).forEach(x => (groups[x.group] = groups[x.group] || []).push(x));
  const box = $('cmds'); box.innerHTML = '';
  Object.keys(groups).forEach(g => {
    const sec = document.createElement('div'); sec.className = 'group';
    const h = document.createElement('h2'); h.textContent = g; sec.appendChild(h);
    const chips = document.createElement('div'); chips.className = 'chips';
    groups[g].forEach(x => { const b = document.createElement('button'); b.className = 'chip'; b.type = 'button'; b.textContent = x.label;
      b.onclick = () => { if (x.ask) { tab('chat'); $('t').value = x.command + ' '; $('t').placeholder = x.ask + (x.example ? ' — ex.: ' + x.example : ''); $('t').focus(); } else mandar(x.command, '', x.label); };
      chips.appendChild(b); });
    sec.appendChild(chips); box.appendChild(sec);
  });
}
carregar();
</script></body></html>"""


def page(agent_name: str) -> str:
    title = html.escape(f"{agent_name} · seu espaço")
    return _PAGE.replace("__TITLE__", title).replace("__AGENT__", json.dumps(str(agent_name)))
