import json
import os
import subprocess
import threading
import time
import uuid
import zipfile
from urllib.parse import urlparse
from flask import Flask, request, send_file, render_template_string, jsonify

app = Flask(__name__)
BASE = "/tmp/clipador" if os.path.exists("/tmp") else "."
os.makedirs(BASE, exist_ok=True)
JOBS = {}
JOBS_LOCK = threading.Lock()
SEG = 60

TIKTOK = [
    {"nome": "Isaffonti", "usuario": "isaffonti"},
    {"nome": "Fluxo Rajah", "usuario": "fluxo_rajah"},
    {"nome": "Uriel", "usuario": "uriel.wq"},
    {"nome": "Levelup", "usuario": "levelup_007"},
]
TT_CACHE = {"hora": 0, "dados": [], "rodando": False}
TT_LOCK = threading.Lock()
TT_TTL = 60


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
  .player-aviso {
    margin: 8px 0 0;
    font-size: .76rem;
    color: var(--mutado);
  }

  .live-item {
    margin-top: 12px;
  }
  .live-topo {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 8px;
    font-weight: 700;
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
    transition: filter 0.2s ease;
  }
  .btn-mini:hover {
    filter: brightness(1.1);
  }

  .placeholder {
    width: 100%;
    aspect-ratio: 16/9;
    border-radius: 12px;
    background: #000;
    border: 1px solid var(--borda);
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 8px;
    cursor: pointer;
    color: var(--texto);
  }
  .placeholder .play {
    width: 56px;
    height: 56px;
    border-radius: 50%;
    background: var(--kick-verde);
    color: #0d0a16;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 1.4rem;
    padding-left: 4px;
  }
  .placeholder small {
    color: var(--mutado);
    font-size: .78rem;
  }
  .viewers {
    font-weight: 500;
    font-size: .8rem;
    color: var(--mutado);
    margin-left: 8px;
  }

  .tt-card {
    background: var(--surface-2);
    border: 1px solid var(--borda);
    border-radius: 12px;
    padding: 12px;
  }
  .tt-card .live-topo {
    margin-bottom: 6px;
  }
  .tt-link {
    color: var(--kick-brilho);
    font-size: .82rem;
    text-decoration: none;
    font-weight: 600;
  }
  .tt-vazio {
    margin: 10px 0 0;
    color: var(--mutado);
    font-size: .85rem;
  }

  .aviso {
    color: #7c7496;
    font-size: 0.78rem;
    text-align: center;
    margin: 18px 0 0;
    line-height: 1.45;
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
      <p class="player-aviso" id="player-aviso">Se o vídeo não aparecer, a gravação ainda pode funcionar.</p>
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

    <section class="caixa" id="tt-box">
      <span class="diag-topo">Lives agora no TikTok</span>
      <div id="lives-tt"><p class="tt-vazio">Verificando...</p></div>
    </section>

    <p class="aviso">Usa apenas com autorização do streamer ou nas tuas próprias lives.</p>
  </main>

<script>
let job = null;
let ultimoClipesRenderizado = -1;
let ultimoJobRenderizado = null;

function plataforma(url) {
  try {
    const u = new URL(url);
    const host = u.hostname.replace('www.', '');
    const partes = u.pathname.split('/').filter(Boolean);
    if ((host === 'kick.com' || host === 'kzl.ink') && partes[0]) {
      return {nome: 'Kick', embed: 'https://player.kick.com/' + partes[0] + '?autoplay=true'};
    }
    if ((host === 'tiktok.com' || host.endsWith('.tiktok.com')) && partes[0] && partes[0].charAt(0) === '@') {
      return {nome: 'TikTok', embed: ''};
    }
  } catch (e) {}
  return null;
}

let ultimoEmbed = '';
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
  const playerEl = document.getElementById('player');
  const avisoEl = document.getElementById('player-aviso');
  if (!p.embed) {
    playerEl.style.display = 'none';
    playerEl.src = 'about:blank';
    ultimoEmbed = '';
    avisoEl.innerText = 'O TikTok não permite player aqui, mas a gravação funciona normalmente.';
    return;
  }
  playerEl.style.display = '';
  avisoEl.innerText = 'Se o vídeo não aparecer, a gravação ainda pode funcionar.';
  if (p.embed !== ultimoEmbed) {
    document.getElementById('player').src = p.embed;
    ultimoEmbed = p.embed;
  }
}
document.getElementById('url').addEventListener('input', mostrarPreview);

const CANAIS = [
  {nome: 'Bak', plat: 'kick', canal: 'bak'},
  {nome: 'Renato Vicente', plat: 'kick', canal: 'orenatovicente'},
  {nome: 'Brabox', plat: 'kick', canal: 'brabox'},
  {nome: 'Gabe Peixe', plat: 'kick', canal: 'gabepeixe'},
  {nome: 'Coringa', plat: 'kick', canal: 'coringa'}
];

function embedDe(c) {
  return 'https://player.kick.com/' + c.canal + '?autoplay=true';
}

function linkDe(c) {
  return 'https://kick.com/' + c.canal;
}

function clipar(i) {
  document.getElementById('url').value = linkDe(CANAIS[i]);
  mostrarPreview();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

let assinaturaLives = '';
const tocando = new Set();

function playerHtml(c) {
  return '<iframe class="player" src="' + embedDe(c) + '" allowfullscreen allow="autoplay; fullscreen"></iframe>';
}

function tocar(canal) {
  tocando.add(canal);
  const el = document.getElementById('slot-' + canal);
  const c = CANAIS.find(function (x) { return x.canal === canal; });
  if (el && c) { el.innerHTML = playerHtml(c); }
}

function montarLives(lista) {
  // lista: [{canal, nome, viewers}] já ordenada por público (maior primeiro)
  const assinatura = lista.map(function (c) { return c.canal + ':' + c.viewers; }).join('|');
  if (assinatura === assinaturaLives) { return; }
  assinaturaLives = assinatura;

  const temPublico = lista.length > 0 && lista[0].viewers !== null;
  let h = '';
  lista.forEach(function (c, pos) {
    const idx = CANAIS.findIndex(function (item) { return item.canal === c.canal; });
    const autoplay = temPublico && pos === 0;
    const rodando = autoplay || tocando.has(c.canal);
    const info = c.viewers !== null ? '<span class="viewers">' + c.viewers.toLocaleString('pt-BR') + ' assistindo</span>' : '';
    const corpo = rodando
      ? playerHtml(c)
      : '<div class="placeholder" data-canal="' + c.canal + '" onclick="tocar(this.dataset.canal)"><div class="play">▶</div><small>Clique para assistir</small></div>';
    h += '<div class="live-item"><div class="live-topo"><span><span class="ponto"></span>' + c.nome + info +
      '</span><button class="btn-mini" onclick="clipar(' + idx + ')">Usar esta live</button></div>' +
      '<div id="slot-' + c.canal + '">' + corpo + '</div></div>';
  });
  document.getElementById('lives').innerHTML = h;
}
montarLives(CANAIS.map(function (c) { return {canal: c.canal, nome: c.nome, viewers: null}; }));

async function filtrarOnline() {
  try {
    const resultados = await Promise.all(
      CANAIS.map(async function (c) {
        try {
          const r = await fetch('https://kick.com/api/v2/channels/' + c.canal);
          if (!r.ok) { return {c: c, status: 'incerto', viewers: null}; }
          const d = await r.json();
          if (d && 'livestream' in d) {
            if (!d.livestream) { return {c: c, status: 'offline', viewers: null}; }
            const v = d.livestream.viewer_count != null ? d.livestream.viewer_count : d.livestream.viewers;
            return {c: c, status: 'online', viewers: (typeof v === 'number') ? v : null};
          }
          return {c: c, status: 'incerto', viewers: null};
        } catch (e) {
          return {c: c, status: 'incerto', viewers: null};
        }
      })
    );

    const online = resultados.filter(function (x) { return x.status === 'online'; });
    const offline = resultados.filter(function (x) { return x.status === 'offline'; }).length;

    if (online.length > 0) {
      online.sort(function (a, b) { return (b.viewers || 0) - (a.viewers || 0); });
      montarLives(online.map(function (x) {
        return {canal: x.c.canal, nome: x.c.nome, viewers: x.viewers};
      }));
    } else if (offline === CANAIS.length) {
      assinaturaLives = '';
      montarLives([]);
    }
  } catch (e) {}
}
filtrarOnline();
setInterval(filtrarOnline, 30000);

let assinaturaTT = '';

function esc(t) {
  return String(t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function usarTikTok(usuario) {
  document.getElementById('url').value = 'https://www.tiktok.com/@' + usuario + '/live';
  mostrarPreview();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function montarTikTok(lista) {
  const box = document.getElementById('lives-tt');
  const visiveis = lista.filter(function (c) { return c.online !== false; });
  const assinatura = visiveis.map(function (c) { return c.usuario + ':' + c.online; }).join('|');
  if (assinatura === assinaturaTT) { return; }
  assinaturaTT = assinatura;
  if (visiveis.length === 0) {
    box.innerHTML = '<p class="tt-vazio">Nenhuma live do TikTok no ar agora.</p>';
    return;
  }
  let h = '';
  visiveis.forEach(function (c) {
    const incerto = c.online === null;
    const info = incerto ? '<span class="viewers">status incerto</span>' : '';
    h += '<div class="live-item"><div class="tt-card">' +
      '<div class="live-topo"><span><span class="ponto"' + (incerto ? ' style="background:#7c7496"' : '') + '></span>' + c.nome + info + '</span>' +
      '<button class="btn-mini" data-usuario="' + c.usuario + '" onclick="usarTikTok(this.dataset.usuario)">Usar esta live</button></div>' +
      '<a class="tt-link" href="https://www.tiktok.com/@' + c.usuario + '/live" target="_blank" rel="noopener">Abrir no TikTok ↗</a>' +
      (incerto && c.detalhe ? '<p class="tt-vazio">Motivo: ' + esc(c.detalhe) + '</p>' : '') +
      '</div></div>';
  });
  box.innerHTML = h;
}

async function carregarTikTok() {
  let proximo = 60000;
  try {
    const r = await fetch('/api/tiktok');
    if (!r.ok) {
      proximo = 5000;
    } else {
      const d = await r.json();
      if (!d.pronto) {
        proximo = 3000;
      } else {
        montarTikTok(d.dados);
      }
    }
  } catch (e) {
    proximo = 5000;
  }
  setTimeout(carregarTikTok, proximo);
}
carregarTikTok();

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


def atualizar_job(job_id, **campos):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(campos)


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


def normalizar_url(url):
    try:
        u = urlparse(url)
        host = (u.hostname or "").replace("www.", "")
        partes = [p for p in u.path.split("/") if p]
        if host.endswith("tiktok.com") and partes and partes[0].startswith("@"):
            return f"https://www.tiktok.com/{partes[0]}/live"
    except Exception:
        pass
    return url


def checar_tiktok(usuario):
    """Retorna (estado, detalhe). estado: True = ao vivo, False = offline, None = incerto."""
    url = f"https://www.tiktok.com/@{usuario}/live"
    try:
        r = subprocess.run(
            ["yt-dlp", "--no-warnings", "-g", "-f", "best[height<=720]/best", url],
            capture_output=True, text=True, timeout=25,
        )
    except subprocess.TimeoutExpired:
        return None, "o yt-dlp demorou demais para responder"
    except FileNotFoundError:
        return None, "yt-dlp não encontrado no servidor"
    except Exception as e:
        return None, str(e)[-200:]
    links = [l for l in r.stdout.strip().splitlines() if l.strip().startswith("http")]
    if r.returncode == 0 and links:
        return True, ""
    erro = (r.stderr or "").strip()
    baixo = erro.lower()
    if "not currently live" in baixo or "not live" in baixo or "offline" in baixo:
        return False, ""
    return None, erro[-200:] or "sem mensagem do yt-dlp"


def atualizar_tiktok():
    resultados = [(None, "")] * len(TIKTOK)

    def worker(i, usuario):
        resultados[i] = checar_tiktok(usuario)

    try:
        threads = [
            threading.Thread(target=worker, args=(i, c["usuario"]), daemon=True)
            for i, c in enumerate(TIKTOK)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=40)
        dados = [
            {
                "nome": c["nome"],
                "usuario": c["usuario"],
                "online": resultados[i][0],
                "detalhe": resultados[i][1],
            }
            for i, c in enumerate(TIKTOK)
        ]
        with TT_LOCK:
            TT_CACHE["dados"] = dados
            TT_CACHE["hora"] = time.time()
    finally:
        with TT_LOCK:
            TT_CACHE["rodando"] = False


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

    user_agent = headers.get(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    )

    extra_headers = []
    for chave, valor in headers.items():
        if chave.lower() != "user-agent" and valor:
            extra_headers.extend(["-headers", f"{chave}: {valor}"])

    atualizar_job(
        job_id,
        estado="gravando",
        msg="Gravando... 0 clipes prontos",
        diag="yt-dlp OK. Iniciando ffmpeg...",
    )

    cmd = [
        "ffmpeg", "-y",
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

    proc = None
    try:
        with open(log_ffmpeg, "w") as logf:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=logf)
            inicio = time.time()

            while proc.poll() is None:
                feitos = listar_clipes(pasta)
                # O último arquivo ainda está sendo gravado; só o anterior está fechado
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


@app.route("/")
def inicio():
    return render_template_string(PAGINA)


@app.route("/iniciar", methods=["POST"])
def iniciar():
    dados = request.get_json(silent=True) or {}
    url = str(dados.get("url", "")).strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return jsonify({"id": "", "erro": "Link inválido."}), 400

    url = normalizar_url(url)

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


@app.route("/api/tiktok")
def api_tiktok():
    iniciar_thread = False
    with TT_LOCK:
        velho = time.time() - TT_CACHE["hora"] >= TT_TTL
        if velho and not TT_CACHE["rodando"]:
            TT_CACHE["rodando"] = True
            iniciar_thread = True
        dados = list(TT_CACHE["dados"])
    if iniciar_thread:
        threading.Thread(target=atualizar_tiktok, daemon=True).start()
    return jsonify({"pronto": bool(dados), "dados": dados})


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
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        pasta = j.get("pasta") if j else ""
    if not pasta:
        return "Não encontrado", 404

    arq = os.path.join(pasta, f"clipe_{n - 1:03d}.mp4")
    if not os.path.isfile(arq):
        return "Não encontrado", 404
    return send_file(arq, as_attachment=True, download_name=f"clipe_{n}.mp4")


@app.route("/zip/<job_id>")
def baixar_zip(job_id):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        pasta = j.get("pasta") if j else ""
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
