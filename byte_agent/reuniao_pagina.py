"""Página /reuniao — grava reuniões ONLINE no notebook (Teams, Zoom, Meet) — v13.37.

Abra no Chrome ou no Edge do notebook: https://<endereço do agente>/reuniao. A página captura:
  - o ÁUDIO DA REUNIÃO: o navegador pede para compartilhar a tela; marque "Compartilhar áudio do sistema" (Windows:
    tela inteira, pega o app do Teams/Zoom) ou "Compartilhar áudio da aba" (Teams/Zoom/Meet no navegador);
  - o SEU MICROFONE (opcional);
mistura os dois a 16 kHz, corta trechos de 20–30 s numa pausa e manda para o mesmo transcritor do app
(/api/v1/reuniao). O texto aparece na página ao vivo; no fim, a ata chega aqui, no app e no Telegram.
Sem instalar nada. O acesso é o mesmo da página /voz (código do /parear, guardado só neste navegador).
"""

MEETING_PAGE_HTML = r"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Reunião</title>
<style>
:root{--bg:#0b0f17;--card:#181f2d;--ink:#eef2f7;--muted:#97a1b2;--blue:#2563eb;--red:#dc2626;--line:#334155}
@media (prefers-color-scheme: light){:root:not([data-theme="dark"]){--bg:#f6f7fb;--card:#fff;--ink:#111827;--muted:#5b6475;--line:#d6dbe4}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:880px;margin:0 auto;padding:20px 16px 40px}h1{font-size:1.4rem;margin:0 0 4px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin:14px 0}
.muted{color:var(--muted);font-size:.92rem}input[type=text]{width:100%;padding:10px 12px;border-radius:10px;border:1px solid var(--line);
background:transparent;color:var(--ink);font-size:1rem}label{display:block;margin:8px 0}
button{border:0;border-radius:12px;padding:12px 18px;font-size:1rem;font-weight:600;cursor:pointer;background:var(--blue);color:#fff}
button.stop{background:var(--red)}button:disabled{opacity:.5;cursor:default}
#live{white-space:pre-wrap;max-height:50vh;overflow:auto;font-size:.98rem}#ata{white-space:pre-wrap}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.meter{height:8px;border-radius:4px;background:var(--line);flex:1;min-width:120px}
.meter>div{height:100%;width:0;border-radius:4px;background:#22c55e;transition:width .1s}
</style></head><body><main>
<h1>📝 Gravar reunião</h1>
<div class="muted">Teams, Zoom ou Meet no notebook: o texto aparece aqui enquanto a reunião acontece e, no fim, a ata chega aqui, no app e no Telegram.</div>

<div class="card" id="setup" hidden>
  <b>Conectar este navegador</b>
  <p class="muted">Mande <b>/parear</b> no Telegram do agente e digite o código de 6 dígitos.</p>
  <div class="row"><input type="text" id="code" inputmode="numeric" maxlength="6" placeholder="123456" style="max-width:160px">
  <button id="pairBtn">Conectar</button></div>
</div>

<div class="card" id="main" hidden>
  <input type="text" id="title" placeholder="Título (opcional), ex.: Reunião de orçamento">
  <label><input type="checkbox" id="useSystem" checked> Áudio da reunião (das outras pessoas)</label>
  <label><input type="checkbox" id="useMic" checked> Meu microfone (a minha voz)</label>
  <div class="row"><button id="rec">🔴 Gravar</button><span id="clock" class="muted"></span><div class="meter"><div id="lvl"></div></div></div>
  <p class="muted" id="howto">Ao gravar, o navegador pede para compartilhar a tela. <b>Windows</b> (app do Teams/Zoom): escolha
  <b>Tela inteira</b> e ligue <b>Compartilhar áudio do sistema</b>. <b>Reunião no navegador</b>: escolha a <b>aba</b> da reunião e ligue
  <b>Compartilhar áudio da aba</b>. Use fone de ouvido para a sua voz não sair duas vezes. Avise os participantes que a reunião está sendo gravada.</p>
  <div id="state" class="muted"></div>
</div>

<div class="card" id="liveCard" hidden><b>Ao vivo</b><div id="live"></div></div>
<div class="card" id="ataCard" hidden><b>Ata</b><div id="ata"></div></div>
</main>
<script>
(() => {
  const $ = id => document.getElementById(id);
  const store = { get(k) { try { return localStorage.getItem(k) || ''; } catch (e) { return ''; } },
                  set(k, v) { try { v ? localStorage.setItem(k, v) : localStorage.removeItem(k); } catch (e) {} } };
  const RATE = 16000, MIN_S = 20, MAX_S = 30;
  let token = store.get('byte_voice_token');
  const hash = new URLSearchParams(location.hash.slice(1));
  if (hash.get('parear')) { const c = hash.get('parear'); history.replaceState(null, '', location.pathname); pair(c); }

  function state(t) { $('state').textContent = t; }
  async function api(path, opts) {
    const r = await fetch(path, Object.assign({ cache: 'no-store' }, opts || {}, {
      headers: Object.assign({ 'Authorization': 'Bearer ' + token }, (opts || {}).headers || {}) }));
    let data = {}; try { data = await r.json(); } catch (e) {}
    if (r.status === 401) { token = ''; store.set('byte_voice_token', ''); show(); }
    if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
    return data;
  }
  async function pair(code) {
    try {
      const r = await fetch('/api/v1/pair', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code, device: 'Notebook (reuniões) ' + new Date().toLocaleDateString('pt-BR') }) });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || !d.token) throw new Error(d.error || ('HTTP ' + r.status));
      token = d.token; store.set('byte_voice_token', token); show();
    } catch (e) { alert('Não conectou: ' + e.message); }
  }
  function show() { $('setup').hidden = !!token; $('main').hidden = !token; }
  $('pairBtn').onclick = () => { const c = $('code').value.trim(); if (/^\d{6}$/.test(c)) pair(c); };
  show();

  // ---------------- gravação
  let ctx = null, streams = [], proc = null, pcm = new Int16Array(RATE * 40), size = 0, seq = 0, sent = 0;
  let meetingId = 0, recording = false, queue = [], uploading = false, started = 0, timer = null;

  function wav(samples) {
    const b = new DataView(new ArrayBuffer(44 + samples.length * 2));
    const w = (o, s) => { for (let i = 0; i < s.length; i++) b.setUint8(o + i, s.charCodeAt(i)); };
    w(0, 'RIFF'); b.setUint32(4, 36 + samples.length * 2, true); w(8, 'WAVE'); w(12, 'fmt ');
    b.setUint32(16, 16, true); b.setUint16(20, 1, true); b.setUint16(22, 1, true); b.setUint32(24, RATE, true);
    b.setUint32(28, RATE * 2, true); b.setUint16(32, 2, true); b.setUint16(34, 16, true); w(36, 'data');
    b.setUint32(40, samples.length * 2, true);
    for (let i = 0; i < samples.length; i++) b.setInt16(44 + i * 2, samples[i], true);
    return new Blob([b.buffer], { type: 'audio/wav' });
  }
  function quietCut(from) {                       // pausa de 300 ms bem baixa depois de `from` amostras
    const win = RATE * 0.3 | 0; let best = 1e9, at = -1;
    for (let i = from; i + win <= size; i += RATE / 20) {
      let s = 0; for (let j = i; j < i + win; j += 4) s += Math.abs(pcm[j]);
      const avg = s / (win / 4); if (avg < best) { best = avg; at = i + (win / 2 | 0); }
    }
    return best < 400 ? at : -1;
  }
  function push(cut) {
    queue.push({ seq: seq++, t: sent / RATE, blob: wav(pcm.slice(0, cut)) });
    sent += cut; pcm.copyWithin(0, cut, size); size -= cut; pump();
  }
  async function pump() {
    if (uploading) return; uploading = true;
    while (queue.length) {
      const item = queue[0];
      try {
        const res = await api(`/api/v1/reuniao/${meetingId}/audio?seq=${item.seq}&t=${item.t.toFixed(1)}`,
                              { method: 'POST', headers: { 'Content-Type': 'audio/wav' }, body: item.blob });
        queue.shift();
        if (res.texto) { $('live').textContent += res.texto + '\n'; $('live').scrollTop = $('live').scrollHeight; }
        state(recording ? '🔴 Gravando' : 'Enviando o final...');
      } catch (e) { state('⚠️ Sem rede? Tentando de novo... (' + e.message + ')'); await new Promise(r => setTimeout(r, 4000)); }
    }
    uploading = false;
  }
  async function start() {
    const wantSys = $('useSystem').checked, wantMic = $('useMic').checked;
    if (!wantSys && !wantMic) { state('Escolha pelo menos uma fonte de áudio.'); return; }
    try {
      ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: RATE });
      const mix = ctx.createGain();
      if (wantSys) {
        const sys = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: { echoCancellation: false, noiseSuppression: false },
                                                                    systemAudio: 'include', selfBrowserSurface: 'exclude' });
        sys.getVideoTracks().forEach(t => t.stop());       // só o som interessa
        if (!sys.getAudioTracks().length) {
          sys.getTracks().forEach(t => t.stop());
          throw new Error('O compartilhamento veio sem áudio: grave de novo e ligue "Compartilhar áudio do sistema" (ou da aba).');
        }
        streams.push(sys); ctx.createMediaStreamSource(sys).connect(mix);
      }
      if (wantMic) {
        const mic = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
        streams.push(mic); ctx.createMediaStreamSource(mic).connect(mix);
      }
      const res = await api('/api/v1/reuniao', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                                 body: JSON.stringify({ titulo: $('title').value.trim() }) });
      meetingId = res.id;
      proc = ctx.createScriptProcessor(4096, 1, 1);
      proc.onaudioprocess = ev => {
        if (!recording) return;
        const x = ev.inputBuffer.getChannelData(0); let peak = 0;
        if (size + x.length > pcm.length) { const bigger = new Int16Array(pcm.length * 2); bigger.set(pcm.subarray(0, size)); pcm = bigger; }
        for (let i = 0; i < x.length; i++) { const v = Math.max(-1, Math.min(1, x[i])); pcm[size++] = v * 32767; peak = Math.max(peak, Math.abs(v)); }
        $('lvl').style.width = Math.min(100, peak * 140) + '%';
        const secs = size / RATE;
        if (secs >= MAX_S) push(size); else if (secs >= MIN_S) { const c = quietCut(MIN_S * RATE); if (c > 0) push(c); }
      };
      const silent = ctx.createGain(); silent.gain.value = 0;    // saída muda: o ScriptProcessor só roda conectado
      mix.connect(proc); proc.connect(silent); silent.connect(ctx.destination);
      recording = true; started = Date.now(); $('live').textContent = ''; $('liveCard').hidden = false; $('ataCard').hidden = true;
      $('rec').textContent = '⏹️ Encerrar e gerar ata'; $('rec').className = 'stop'; state('🔴 Gravando reunião #' + meetingId);
      timer = setInterval(() => { const s = (Date.now() - started) / 1000 | 0; $('clock').textContent = `${String(s / 60 | 0).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`; }, 1000);
      streams.forEach(s => s.getAudioTracks().forEach(t => t.onended = () => recording && stop()));   // parou de compartilhar
    } catch (e) {
      cleanup(); state('⚠️ ' + (e.name === 'NotAllowedError' ? 'Permissão negada (compartilhamento ou microfone).' : e.message));
    }
  }
  function cleanup() {
    streams.forEach(s => s.getTracks().forEach(t => t.stop())); streams = [];
    if (proc) { proc.onaudioprocess = null; try { proc.disconnect(); } catch (e) {} proc = null; }
    if (ctx) { ctx.close().catch(() => {}); ctx = null; }
    clearInterval(timer);
  }
  async function stop() {
    recording = false; $('rec').disabled = true;
    if (size > RATE) push(size); else size = 0;
    cleanup();
    state('Enviando o final...');
    while (queue.length || uploading) await new Promise(r => setTimeout(r, 500));
    try { await api(`/api/v1/reuniao/${meetingId}/fim`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }); }
    catch (e) { state('⚠️ ' + e.message); }
    state('📝 Preparando a ata...');
    $('rec').textContent = '🔴 Gravar'; $('rec').className = ''; $('rec').disabled = false;
    for (let i = 0; i < 120; i++) {
      await new Promise(r => setTimeout(r, 5000));
      try {
        const m = await api(`/api/v1/reuniao/${meetingId}?desde=999999999`);
        if (m.status === 'pronta') { $('ata').textContent = m.ata || ''; $('ataCard').hidden = false; state('✅ Ata pronta (o .docx chegou no Telegram).'); return; }
        if (m.status === 'erro') { state('⚠️ ' + (m.error || 'A ata falhou.')); return; }
      } catch (e) {}
    }
  }
  $('rec').onclick = () => recording ? stop() : start();
  window.addEventListener('beforeunload', e => { if (recording) { e.preventDefault(); e.returnValue = ''; } });
})();
</script></body></html>"""
