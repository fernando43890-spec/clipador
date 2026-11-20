import json
import os
import subprocess
import threading
import time
import uuid
import zipfile

from flask import Flask, jsonify, render_template_string, request, send_file

# ==============================================================================
# CONFIGURAÇÃO INICIAL
# ==============================================================================
app = Flask(__name__)

BASE = "/tmp/clipador" if os.path.exists("/tmp") else "."
os.makedirs(BASE, exist_ok=True)

SEG = 60
JOBS = {}
JOBS_LOCK = threading.Lock()


# ==============================================================================
# TEMPLATES HTML
# ==============================================================================
PAGINA = """
<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Clipador de lives</title>
<style>
  :root {
    --bg: #07060b;
    --surface: #12101a;
    --surface-2: #191624;
    --input: #1f1b2e;
    --borda: rgba(168, 130, 255, 0.16);
    --borda-forte: rgba(168, 130, 255, 0.34);
    --kick-verde: #00ff38;
    --kick-brilho: #66ff88;
    --texto: #f5f3ff;
    --mutado: #a69ec2;
    --vivo: #ef4444;
  }

  * {
    box-sizing: border-box;
  }

  html {
    scroll-behavior: smooth;
  }

  body {
    margin: 0;
    min-height: 100vh;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, sans-serif;
    color: var(--texto);
    background:
      radial-gradient(ellipse at 50% -8%, rgba(0, 255, 56, 0.15), transparent 55%),
      var(--bg);
    padding-bottom: 40px;
  }

  .topbar {
    position: sticky;
    top: 0;
    z-index: 20;
    backdrop-filter: blur(14px);
    -webkit-backdrop-filter: blur(14px);
    background: rgba(7, 6, 11, 0.85);
    border-bottom: 1px solid var(--borda);
  }

  .topbar-inner {
    max-width: 540px;
    margin: 0 auto;
    padding: 12px 16px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }

  .brand {
    display: flex;
    align-items: center;
    gap: 10px;
    text-decoration: none;
    color: var(--texto);
    font-weight: 700;
    font-size: 1rem;
  }

  .logo-box {
    width: 38px;
    height: 38px;
    border-radius: 12px;
    background: #12101a;
    display: flex;
    align-items: center;
    justify-content: center;
    box-shadow: 0 6px 18px rgba(0, 0, 0, 0.5);
    border: 1px solid rgba(0, 255, 56, 0.3);
  }

  .logo-box svg {
    width: 26px;
    height: 26px;
  }

  .btn-topo {
    background: var(--kick-verde);
    color: #0d0a16;
    font-weight: 700;
    font-size: 0.85rem;
    padding: 8px 14px;
    border-radius: 999px;
    text-decoration: none;
    border: none;
    cursor: pointer;
  }

  .wrap {
    max-width: 540px;
    margin: 0 auto;
    padding: 18px 16px 0;
  }

  .hero-card {
    background: var(--surface);
    border: 1px solid var(--borda-forte);
    border-radius: 20px;
    padding: 20px 18px;
    box-shadow: 0 24px 60px rgba(0, 0, 0, 0.6);
  }

  .hero-titulo {
    margin: 0;
    font-size: 1.45rem;
    font-weight: 800;
    letter-spacing: -0.02em;
    line-height: 1.2;
  }

  .hero-sub {
    margin: 8px 0 16px;
    color: var(--mutado);
    font-size: 0.92rem;
    line-height: 1.48;
  }

  .hero-sub strong {
    color: var(--texto);
  }

  .campo {
    margin-bottom: 12px;
  }

  label {
    display: block;
    font-size: 0.78rem;
    font-weight: 600;
    color: var(--mutado);
    margin-bottom: 6px;
  }

  input, select {
    width: 100%;
    padding: 14px;
    font-size: 0.96rem;
    border-radius: 12px;
    border: 1px solid var(--borda);
    background: var(--input);
    color: var(--texto);
    outline: none;
    transition: border-color 0.2s ease, box-shadow 0.2s ease;
  }

  input::placeholder {
    color: #8c84a8;
  }

  input:focus, select:focus {
    border-color: var(--kick-brilho);
    box-shadow: 0 0 0 3px rgba(0, 255, 56, 0.22);
  }

  select {
    appearance: none;
    -webkit-appearance: none;
    background-image:
      linear-gradient(45deg, transparent 50%, var(--mutado) 50%),
      linear-gradient(135deg, var(--mutado) 50%, transparent 50%);
    background-position:
      calc(100% - 18px) calc(50% - 2px),
      calc(100% - 13px) calc(50% - 2px);
    background-size: 5px 5px, 5px 5px;
    background-repeat: no-repeat;
    padding-right: 36px;
  }

  .btn-principal {
    width: 100%;
    padding: 15px 18px;
    margin-top: 4px;
    font-size: 1rem;
    font-weight: 700;
    border-radius: 12px;
    border: none;
    background: var(--kick-verde);
    color: #0d0a16;
    cursor: pointer;
    box-shadow: 0 10px 26px rgba(0, 255, 56, 0.35);
    transition: transform 0.15s ease, background 0.2s ease;
  }

  .btn-principal:hover {
    background: var(--kick-brilho);
    transform: translateY(-1px);
  }

  .btn-principal:active {
    transform: translateY(0);
  }

  .btn-principal:disabled {
    opacity: 0.55;
    cursor: not-allowed;
    transform: none;
  }

  .caixa {
    background: var(--surface);
    border: 1px solid var(--borda);
    border-radius: 16px;
    padding: 14px 16px;
    margin-top: 14px;
    font-size: 0.92rem;
  }

  #status {
    font-weight: 600;
    color: var(--texto);
  }

  #lista a {
    display: block;
    color: var(--texto);
    text-decoration: none;
    padding: 10px 12px;
    border-radius: 10px;
    background: var(--surface-2);
    border: 1px solid var(--borda);
    margin-top: 8px;
    font-size: 0.9rem;
    font-weight: 500;
  }

  #lista a:first-child {
    margin-top: 0;
    background: rgba(0, 255, 56, 0.22);
    border-color: var(--kick-brilho);
    color: var(--kick-brilho);
    font-weight: 700;
  }

  .diag-topo {
    font-size: 0.76rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--mutado);
    font-weight: 700;
  }

  pre {
    white-space: pre-wrap;
    word-break: break-word;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 0.78rem;
    color: #c4b5fd;
    margin: 8px 0 0;
    line-height: 1.45;
  }

  .card-live {
    background: var(--surface);
    border: 1px solid var(--borda-forte);
    border-radius: 20px;
    padding: 14px;
    margin-top: 14px;
  }

  .card-topo {
    display: flex;
    justify-content: space-between;
    margin-bottom: 10px;
    font-weight: 700;
  }

  .ponto {
    display: inline-block;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: var(--vivo);
    margin-right: 8px;
  }

  .player {
    width: 100%;
    aspect-ratio: 16/9;
    border: 0;
    border-radius: 12px;
    background: #000;
  }

  .player-espera {
    width: 100%;
    aspect-ratio: 16/9;
    border-radius: 12px;
    background: #09080e;
    border: 1px solid var(--borda);
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 10px;
    cursor: pointer;
    color: var(--mutado);
    font-size: 0.85rem;
    transition: border-color 0.2s ease, background 0.2s ease;
  }

  .player-espera:hover {
    border-color: var(--kick-verde);
    background: #0e0c17;
    color: var(--texto);
  }

  .btn-play-live {
    background: var(--kick-verde);
    color: #0d0a16;
    border: 0;
    border-radius: 999px;
    padding: 9px 16px;
    font-weight: 800;
    font-size: 0.82rem;
    cursor: pointer;
  }

  .player-aviso {
    margin: 8px 0 0;
    font-size: .76rem;
    color: var(--mutado);
  }

  .live-item {
    margin-top: 12px;
  }

  .live-item.destaque {
    background: var(--surface-2);
    border: 2px solid var(--kick-verde);
    border-radius: 16px;
    padding: 12px;
    margin-bottom: 14px;
    box-shadow: 0 0 0 1px rgba(0, 255, 56, 0.25), 0 12px 30px rgba(0, 255, 56, 0.22);
  }

  .live-item.destaque .player {
    border: 1px solid rgba(0, 255, 56, 0.35);
  }

  .badge-top {
    display: inline-flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 4px;
    background: var(--kick-verde);
    color: #0d0a16;
    font-size: 0.78rem;
    font-weight: 800;
    padding: 6px 12px;
    border-radius: 12px;
    margin-bottom: 10px;
    box-shadow: 0 4px 14px rgba(0, 255, 56, 0.35);
    width: 100%;
  }

  .badge-main-text {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 0.82rem;
  }

  .badge-sub-text {
    font-size: 0.74rem;
    opacity: 0.9;
    font-weight: 700;
  }

  .viewers-tag {
    font-size: 0.78rem;
    color: var(--mutado);
    font-weight: 500;
    margin-left: 6px;
  }

  .live-topo {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 8px;
    font-weight: 700;
  }

  .live-acoes {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .btn-mini {
    background: var(--kick-verde);
    color: #0d0a16;
    border: 0;
    border-radius: 10px;
    padding: 8px 12px;
    font-weight: 700;
    font-size: .8rem;
    cursor: pointer;
    transition: filter 0.2s ease, transform 0.15s ease;
  }

  .btn-mini.secundario {
    background: var(--surface-2);
    color: var(--texto);
    border: 1px solid var(--borda-forte);
  }

  .live-item.destaque .btn-mini {
    background: var(--kick-brilho);
    padding: 9px 14px;
    box-shadow: 0 4px 14px rgba(0, 255, 56, 0.35);
  }

  .btn-mini:hover {
    filter: brightness(1.1);
    transform: translateY(-1px);
  }

  .aviso {
    color: #7c7496;
    font-size: 0.78rem;
    text-align: center;
    margin: 18px 0 0;
    line-height: 1.45;
  }

  .aviso a {
    color: var(--mutado);
    text-decoration: underline;
  }
</style>
</head>
<body>
  <header class="topbar">
    <div class="topbar-inner">
      <a class="brand" href="#">
        <span class="logo-box">
          <svg viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
            <path d="M15 20H35L55 50L35 80H15L35 50L15 20Z" fill="#00ff38"/>
            <path d="M45 20H65L85 50L65 80H45L65 50L45 20Z" fill="#00ff38" opacity="0.7"/>
          </svg>
        </span>
        <span>Clipador de lives</span>
      </a>
      <a class="btn-topo" href="#url">Colar link</a>
    </div>
  </header>

  <main class="wrap">
    <section class="hero-card">
      <h1 class="hero-titulo">Transforme lives ao vivo em clipes de 1 minuto</h1>
      <p class="hero-sub">Cole o link da transmissão <strong>AO VIVO</strong> da Kick, escolha a duração e baixe os cortes prontos em MP4 ou ZIP.</p>

      <div class="campo">
        <label for="url">Link da live</label>
        <input id="url" type="url" placeholder="Cole o link da Kick...">
      </div>

      <div class="campo">
        <label for="min">Duração da captura</label>
        <select id="min">
          <option value="5">Gravar 5 minutos (teste)</option>
          <option value="15">Gravar 15 minutos</option>
          <option value="30">Gravar 30 minutos</option>
          <option value="60">Gravar 60 minutos</option>
        </select>
      </div>

      <button id="btn-gravar" class="btn-principal" onclick="iniciar()">Cortar meu vídeo</button>
    </section>

    <section class="card-live" id="card-live" style="display:none">
      <div class="card-topo">
        <span><span class="ponto"></span><span id="plat-nome">Live</span></span>
        <span>AO VIVO</span>
      </div>
      <iframe id="player" class="player" allowfullscreen allow="autoplay; fullscreen"></iframe>
      <p class="player-aviso">Se o vídeo não aparecer, a gravação ainda pode funcionar.</p>
    </section>

    <div class="caixa" id="status">Aguardando link.</div>
    <div class="caixa" id="lista" style="display:none"></div>
    <div class="caixa">
      <span class="diag-topo">Diagnóstico</span>
      <pre id="diag">-</pre>
    </div>

    <section class="caixa" id="lives-box">
      <span class="diag-topo">Lives agora na Kick</span>
      <div id="lives"></div>
    </section>

    <p class="aviso">Usa apenas com autorização do streamer ou nas tuas próprias lives.<br><a href="/terms">Termos de Serviço</a> · <a href="/privacy">Política de Privacidade</a></p>
  </main>

<script>
let job = null;
let ultimoClipesRenderizado = -1;
let ultimoJobRenderizado = null;
let ultimoEmbed = '';
const livesCarregadas = new Set();

const CANAIS = [
  {nome: 'Bak', plat: 'kick', canal: 'bak'},
  {nome: 'Renato Vicente', plat: 'kick', canal: 'orenatovicente'},
  {nome: 'Brabox', plat: 'kick', canal: 'brabox'},
  {nome: 'Gabe Peixe', plat: 'kick', canal: 'gabepeixe'},
  {nome: 'Coringa', plat: 'kick', canal: 'coringa'}
];

function plataforma(url) {
  try {
    const u = new URL(url);
    const host = u.hostname.replace('www.', '');
    const partes = u.pathname.split('/').filter(Boolean);
    if ((host === 'kick.com' || host === 'kzl.ink') && partes[0]) {
      return {nome: 'Kick', embed: 'https://player.kick.com/' + partes[0] + '?autoplay=true'};
    }
  } catch (e) {}
  return null;
}

function mostrarPreview() {
  const p = plataforma(document.getElementById('url').value.trim());
  const card = document.getElementById('card-live');
  if (!p) {
    card.style.display = 'none';
    document.getElementById('player').src = 'about:blank';
    ultimoEmbed = '';
    return;
  }
  card.style.display = 'block';
  document.getElementById('plat-nome').innerText = p.nome;
  if (p.embed !== ultimoEmbed) {
    document.getElementById('player').src = p.embed;
    ultimoEmbed = p.embed;
  }
}
document.getElementById('url').addEventListener('input', mostrarPreview);

function embedDe(c, autoplay) {
  return 'https://player.kick.com/' + c.canal + '?autoplay=' + (autoplay ? 'true' : 'false');
}

function linkDe(c) {
  return 'https://kick.com/' + c.canal;
}

function formatarViewers(n) {
  if (!n || n <= 0) { return ''; }
  if (n >= 1000) {
    return (n / 1000).toFixed(1).replace('.0', '') + 'k';
  }
  return n.toString();
}

function clipar(i) {
  document.getElementById('url').value = linkDe(CANAIS[i]);
  mostrarPreview();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function darPlayLive(canal) {
  livesCarregadas.add(canal);
  const slot = document.getElementById('player-slot-' + canal);
  if (!slot) { return; }
  slot.innerHTML = '<iframe class="player" src="https://player.kick.com/' + canal + '?autoplay=true" allowfullscreen allow="autoplay; fullscreen"></iframe>';
  const btn = document.getElementById('btn-play-' + canal);
  if (btn) { btn.style.display = 'none'; }
}

function montarLives(lista, comDestaque) {
  let h = '';
  lista.forEach(function (c, pos) {
    const idx = CANAIS.findIndex(function (item) { return item.canal === c.canal; });
    const viewers = c.viewers || 0;
    const isTop = comDestaque && pos === 0 && viewers > 0;
    const textoViewers = formatarViewers(viewers);
    const deveCarregar = isTop || livesCarregadas.has(c.canal);

    h += '<div class="live-item' + (isTop ? ' destaque' : '') + '">';
    if (isTop) {
      h += '<div class="badge-top">' +
           '<div class="badge-main-text">🔥 Assistindo agora</div>' +
           '<div class="badge-sub-text">Live mais popular com ' + textoViewers + ' espectadores</div>' +
           '</div>';
    }
    h += '<div class="live-topo"><span><span class="ponto"></span>' + c.nome +
      (!isTop && textoViewers ? '<span class="viewers-tag">(' + textoViewers + ' espectadores)</span>' : '') +
      '</span><div class="live-acoes">';
    if (!deveCarregar) {
      h += '<button id="btn-play-' + c.canal + '" class="btn-mini secundario" onclick="darPlayLive(\'' + c.canal + '\')">Play</button>';
    }
    h += '<button class="btn-mini" onclick="clipar(' + idx + ')">Usar esta live</button></div></div>' +
      '<div id="player-slot-' + c.canal + '">';
    if (deveCarregar) {
      h += '<iframe class="player" src="' + embedDe(c, isTop) + '" allowfullscreen allow="autoplay; fullscreen"></iframe>';
    } else {
      h += '<div class="player-espera" onclick="darPlayLive(\'' + c.canal + '\')"><button type="button" class="btn-play-live">▶ Dar play na live</button><span>Carrega somente ao clicar</span></div>';
    }
    h += '</div></div>';
  });
  document.getElementById('lives').innerHTML = h;
}
montarLives(CANAIS, false);

async function filtrarOnline() {
  try {
    const resultados = await Promise.all(
      CANAIS.map(async function (c) {
        try {
          const r = await fetch('https://kick.com/api/v2/channels/' + c.canal);
          if (!r.ok) { return {canal: c, status: 'incerto', viewers: 0}; }
          const d = await r.json();
          if (d && 'livestream' in d) {
            const online = Boolean(d.livestream);
            const viewers = online ? Number(d.livestream.viewer_count || d.livestream.viewers || 0) : 0;
            return {
              canal: Object.assign({}, c, {viewers: viewers}),
              status: online ? 'online' : 'offline',
              viewers: viewers
            };
          }
          return {canal: c, status: 'incerto', viewers: 0};
        } catch (e) {
          return {canal: c, status: 'incerto', viewers: 0};
        }
      })
    );
    const confirmadosOnline = resultados
      .filter(function (item) { return item.status === 'online'; })
      .sort(function (a, b) { return b.viewers - a.viewers; })
      .map(function (item) { return item.canal; });

    if (confirmadosOnline.length > 0) {
      montarLives(confirmadosOnline, true);
    } else {
      montarLives(CANAIS, false);
    }
  } catch (e) {}
}
filtrarOnline();
setInterval(filtrarOnline, 30000);

async function iniciar() {
  const url = document.getElementById('url').value.trim();
  const min = document.getElementById('min').value;
  const btn = document.getElementById('btn-gravar');
  if (!url) { return; }
  btn.disabled = true;
  ultimoClipesRenderizado = -1;
  document.getElementById('lista').style.display = 'none';
  document.getElementById('lista').innerHTML = '';
  document.getElementById('status').innerText = 'Iniciando...';
  try {
    const r = await fetch('/iniciar', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({url: url, min: min})
    });
    const d = await r.json();
    if (!r.ok || !d.id) {
      document.getElementById('status').innerText = d.erro || 'Link inválido.';
      btn.disabled = false;
      return;
    }
    job = d.id;
    ultimoJobRenderizado = job;
    checar();
  } catch (e) {
    document.getElementById('status').innerText = 'Erro ao iniciar.';
    btn.disabled = false;
  }
}

function atualizarListaClipes(clipesQtd, jobId) {
  const lista = document.getElementById('lista');
  if (clipesQtd <= 0) {
    lista.style.display = 'none';
    return;
  }
  if (clipesQtd === ultimoClipesRenderizado && jobId === ultimoJobRenderizado) {
    return;
  }
  ultimoClipesRenderizado = clipesQtd;
  ultimoJobRenderizado = jobId;
  lista.style.display = 'block';
  let h = '<a href="/zip/' + jobId + '">Baixar todos em ZIP (' + clipesQtd + ' clipes)</a>';
  for (let i = 1; i <= clipesQtd; i++) {
    h += '<a href="/clipe/' + jobId + '/' + i + '">Clipe ' + i + '</a>';
  }
  lista.innerHTML = h;
}

async function checar() {
  if (!job) { return; }
  try {
    const r = await fetch('/status/' + job);
    const d = await r.json();
    document.getElementById('status').innerText = d.msg || '';
    document.getElementById('diag').innerText = d.diag || '-';
    atualizarListaClipes(Number(d.clipes || 0), job);

    if (d.estado !== 'fim' && d.estado !== 'erro') {
      setTimeout(checar, 3000);
    } else {
      document.getElementById('btn-gravar').disabled = false;
    }
  } catch (e) {
    setTimeout(checar, 3000);
  }
}
</script>
</body>
</html>
"""

PAGINA_LEGAL = """
<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ titulo }} - Clipador de lives</title>
<style>
body {
  margin: 0;
  min-height: 100vh;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, sans-serif;
  background: #07060b;
  color: #f5f3ff;
  padding: 24px 16px;
}
.box {
  max-width: 640px;
  margin: 0 auto;
  background: #12101a;
  border: 1px solid rgba(168, 130, 255, 0.25);
  border-radius: 18px;
  padding: 22px;
  line-height: 1.55;
}
h1 { margin-top: 0; font-size: 1.4rem; }
p { color: #c7c1de; font-size: 0.95rem; }
a { color: #00ff38; text-decoration: none; font-weight: 600; }
</style>
</head>
<body>
<div class="box">
  <h1>{{ titulo }}</h1>
  <p>{{ texto }}</p>
  <p><a href="/">← Voltar para o Clipador de lives</a></p>
</div>
</body>
</html>
"""


# ==============================================================================
# FUNÇÕES AUXILIARES DE ARQUIVOS E JOBS
# ==============================================================================
def ler_fim(caminho, limite=700):
    try:
        if not os.path.exists(caminho):
            return ""
        with open(caminho, "r", errors="ignore") as f:
            texto = f.read()
        texto = texto.replace("\r", "\n")
        return texto[-limite:]
    except Exception:
        return ""


def listar_clipes(pasta):
    if not os.path.isdir(pasta):
        return []
    arquivos = []
    for f in sorted(os.listdir(pasta)):
        if f.startswith("clipe_") and f.endswith(".mp4"):
            caminho = os.path.join(pasta, f)
            if os.path.isfile(caminho) and os.path.getsize(caminho) > 0:
                arquivos.append(f)
    return arquivos


def tamanho_pasta(pasta):
    return sum(os.path.getsize(os.path.join(pasta, f)) for f in listar_clipes(pasta))


def atualizar_job(job_id, campos):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(campos)


def obter_pasta_job(job_id):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        return j.get("pasta") if j else ""


# ==============================================================================
# CAPTURA E GRAVAÇÃO (YT-DLP + FFMPEG)
# ==============================================================================
def extrair_stream(url):
    cmd_json = ["yt-dlp", "--no-warnings", "-j", "-f", "best[height<=720]/best", url]
    r = subprocess.run(cmd_json, capture_output=True, text=True, timeout=90)
    if r.returncode == 0 and r.stdout.strip():
        for linha in r.stdout.strip().splitlines():
            try:
                info = json.loads(linha)
                stream_url = info.get("url")
                if stream_url:
                    headers = info.get("http_headers") or {}
                    return stream_url, headers, ""
            except json.JSONDecodeError:
                continue

    cmd_url = ["yt-dlp", "--no-warnings", "-g", "-f", "best[height<=720]/best", url]
    r2 = subprocess.run(cmd_url, capture_output=True, text=True, timeout=90)
    links = [l.strip() for l in r2.stdout.strip().splitlines() if l.strip().startswith("http")]
    if links:
        return links[0], {}, ""

    erro_msg = (r.stderr or r2.stderr or "sem mensagem").strip()
    return None, {}, erro_msg[-700:]


def montar_comando_ffmpeg(stream, headers, minutos, padrao):
    user_agent = headers.get(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    )

    extra_headers = []
    for chave, valor in headers.items():
        if chave.lower() != "user-agent" and valor:
            extra_headers.extend(["-headers", f"{chave}: {valor}"])

    return [
        "ffmpeg", "-y",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-user_agent", user_agent,
        *extra_headers,
        "-rw_timeout", "15000000",
        "-i", stream,
        "-t", str(minutos * 60),
        "-c", "copy",
        "-f", "segment",
        "-segment_time", str(SEG),
        "-reset_timestamps", "1",
        padrao,
    ]


def gravar(job_id, url, minutos):
    pasta = os.path.join(BASE, job_id)
    os.makedirs(pasta, exist_ok=True)
    atualizar_job(job_id, pasta=pasta)

    padrao = os.path.join(pasta, "clipe_%03d.mp4")
    log_ffmpeg = os.path.join(pasta, "ffmpeg.log")

    atualizar_job(
        job_id,
        estado="abrindo",
        msg="Abrindo a live...",
        diag="Rodando yt-dlp...",
    )

    try:
        stream, headers, erro_yt = extrair_stream(url)
    except subprocess.TimeoutExpired:
        atualizar_job(
            job_id,
            estado="erro",
            msg="O yt-dlp demorou demais para responder.",
            diag="Timeout ao buscar o stream da live.",
        )
        return
    except Exception as e:
        atualizar_job(
            job_id,
            estado="erro",
            msg="Falha ao executar o yt-dlp.",
            diag=str(e)[-500:],
        )
        return

    if not stream:
        atualizar_job(
            job_id,
            estado="erro",
            msg="O yt-dlp não conseguiu o link da live.",
            diag=erro_yt or "sem mensagem",
        )
        return

    atualizar_job(
        job_id,
        estado="gravando",
        msg="Gravando... 0 clipes prontos",
        diag="yt-dlp OK. Iniciando ffmpeg...",
    )

    cmd = montar_comando_ffmpeg(stream, headers, minutos, padrao)

    proc = None
    try:
        with open(log_ffmpeg, "w") as logf:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=logf)
            inicio = time.time()

            while proc.poll() is None:
                feitos = listar_clipes(pasta)
                clipes_prontos = max(0, len(feitos) - 1)
                tam = tamanho_pasta(pasta)

                atualizar_job(
                    job_id,
                    clipes=clipes_prontos,
                    msg=f"Gravando... {clipes_prontos} clipes prontos",
                    diag=(
                        f"Tempo: {int(time.time() - inicio)}s | Gravado: {tam // 1024} KB\n"
                        + ler_fim(log_ffmpeg, 500)
                    ),
                )

                if time.time() - inicio > 60 and tam == 0:
                    proc.kill()
                    proc.wait()
                    break

                time.sleep(3)
    except Exception as e:
        if proc and proc.poll() is None:
            proc.kill()
        atualizar_job(
            job_id,
            estado="erro",
            msg="Erro durante a gravação com ffmpeg.",
            diag=str(e)[-500:],
        )
        return

    feitos_finais = listar_clipes(pasta)
    tam_final = tamanho_pasta(pasta)
    clipes_finais = len(feitos_finais)

    if clipes_finais > 0 and tam_final > 0:
        atualizar_job(
            job_id,
            estado="fim",
            clipes=clipes_finais,
            msg=f"Pronto! {clipes_finais} clipes gerados.",
            diag=f"Total gravado: {tam_final // 1024} KB\n" + ler_fim(log_ffmpeg, 400),
        )
    else:
        atualizar_job(
            job_id,
            estado="erro",
            clipes=0,
            msg="Nenhum dado chegou em 60 segundos ou a live não está ativa.",
            diag=ler_fim(log_ffmpeg, 800) or "Sem saída do ffmpeg.",
        )


# ==============================================================================
# ROTAS FLASK
# ==============================================================================
@app.route("/")
def inicio():
    return render_template_string(PAGINA)


@app.route("/iniciar", methods=["POST"])
def iniciar():
    dados = request.get_json(silent=True) or {}
    url = str(dados.get("url", "")).strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return jsonify({"id": "", "erro": "Link inválido."}), 400

    try:
        minutos = int(dados.get("min", 5))
    except (TypeError, ValueError):
        minutos = 5
    minutos = max(1, min(minutos, 180))

    job_id = uuid.uuid4().hex[:8]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "estado": "iniciando",
            "msg": "Iniciando...",
            "clipes": 0,
            "diag": "",
            "pasta": "",
        }

    threading.Thread(target=gravar, args=(job_id, url, minutos), daemon=True).start()
    return jsonify({"id": job_id})


@app.route("/status/<job_id>")
def status(job_id):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            return jsonify({"estado": "erro", "msg": "Tarefa não encontrada", "clipes": 0, "diag": ""})
        return jsonify({
            "estado": j["estado"],
            "msg": j["msg"],
            "clipes": j["clipes"],
            "diag": j.get("diag", ""),
        })


@app.route("/clipe/<job_id>/<int:n>")
def clipe(job_id, n):
    if n < 1:
        return "Clipe inválido", 400

    pasta = obter_pasta_job(job_id)
    if not pasta:
        return "Não encontrado", 404

    arq = os.path.join(pasta, f"clipe_{n - 1:03d}.mp4")
    if not os.path.isfile(arq):
        return "Não encontrado", 404
    return send_file(arq, as_attachment=True, download_name=f"clipe_{n}.mp4")


@app.route("/zip/<job_id>")
def baixar_zip(job_id):
    pasta = obter_pasta_job(job_id)
    if not pasta:
        return "Não encontrado", 404

    arquivos = listar_clipes(pasta)
    if not arquivos:
        return "Nenhum clipe disponível", 404

    caminho_zip = os.path.join(BASE, f"{job_id}.zip")
    with zipfile.ZipFile(caminho_zip, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for f in arquivos:
            z.write(os.path.join(pasta, f), f)

    return send_file(caminho_zip, as_attachment=True, download_name="clipes.zip")


@app.route("/terms")
def terms():
    return render_template_string(
        PAGINA_LEGAL,
        titulo="Termos de Serviço",
        texto=(
            "O Clipador de lives permite gerar clipes de transmissões ao vivo e facilitar "
            "a publicação em plataformas conectadas mediante autorização do usuário. "
            "Você concorda em utilizar o serviço apenas com conteúdo próprio ou devidamente "
            "autorizado e em conformidade com as políticas das plataformas de destino."
        ),
    )


@app.route("/privacy")
def privacy():
    return render_template_string(
        PAGINA_LEGAL,
        titulo="Política de Privacidade",
        texto=(
            "O Clipador de lives processa os links e arquivos de vídeo temporariamente "
            "para gerar os clipes solicitados. Não vendemos dados pessoais a terceiros e "
            "utilizamos credenciais ou autorizações apenas para as ações explicitamente "
            "solicitadas por você na aplicação."
        ),
    )


# ==============================================================================
# ENTRADA DA APLICAÇÃO
# ==============================================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
