import json
import os
import subprocess
import threading
import time
import uuid
import zipfile
from urllib.parse import urlparse, quote
from urllib.request import Request, urlopen
from flask import Flask, request, send_file, render_template_string, jsonify

app = Flask(__name__)
BASE = "/tmp/clipador" if os.path.exists("/tmp") else "."
os.makedirs(BASE, exist_ok=True)
JOBS = {}
JOBS_LOCK = threading.Lock()
SEG = 60

PAGINA = """
<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Clipador de lives – Kick</title>
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
    --tt-rosa: #fe2c55;
    --tt-azul: #25f4ee;
    --grad: linear-gradient(135deg, #00ff38 0%, #25f4ee 52%, #fe2c55 100%);
  }

  * { box-sizing: border-box; }
  html { scroll-behavior: smooth; }

  body {
    margin: 0;
    min-height: 100vh;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, sans-serif;
    color: var(--texto);
    background:
      radial-gradient(ellipse at 18% -8%, rgba(0, 255, 56, 0.15), transparent 55%),
      radial-gradient(ellipse at 85% -8%, rgba(254, 44, 85, 0.14), transparent 55%),
      radial-gradient(ellipse at 52% 0%, rgba(37, 244, 238, 0.07), transparent 50%),
      var(--bg);
    padding-bottom: 40px;
  }

  .topbar {
    position: sticky; top: 0; z-index: 20;
    backdrop-filter: blur(14px);
    -webkit-backdrop-filter: blur(14px);
    background: rgba(7, 6, 11, 0.85);
    border-bottom: 1px solid var(--borda);
  }
  .topbar-inner {
    max-width: 540px; margin: 0 auto; padding: 12px 16px;
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
  }
  .brand {
    display: flex; align-items: center; gap: 10px;
    text-decoration: none; color: var(--texto); font-weight: 700; font-size: 1rem;
  }
  .logo-box {
    width: 38px; height: 38px; border-radius: 12px; background: #12101a;
    display: flex; align-items: center; justify-content: center;
    box-shadow: 0 6px 18px rgba(0, 0, 0, 0.5);
    border: 1px solid rgba(0, 255, 56, 0.3);
  }
  .logo-box svg { width: 26px; height: 26px; }
  .btn-topo {
    background: var(--kick-verde); color: #0d0a16; font-weight: 700;
    font-size: 0.85rem; padding: 8px 14px; border-radius: 999px;
    text-decoration: none; border: none; cursor: pointer;
  }
  .wrap { max-width: 540px; margin: 0 auto; padding: 18px 16px 0; }
  .hero-card {
    background: var(--surface); border: 1px solid var(--borda-forte);
    border-radius: 20px; padding: 20px 18px;
    box-shadow: 0 24px 60px rgba(0, 0, 0, 0.6);
  }
  .hero-titulo {
    margin: 0; font-size: 1.45rem; font-weight: 800;
    letter-spacing: -0.02em; line-height: 1.2;
  }
  .hero-sub { margin: 8px 0 16px; color: var(--mutado); font-size: 0.92rem; line-height: 1.48; }
  .hero-sub strong { color: var(--texto); }
  .campo { margin-bottom: 12px; }
  label { display: block; font-size: 0.78rem; font-weight: 600; color: var(--mutado); margin-bottom: 6px; }
  input, select {
    width: 100%; padding: 14px; font-size: 0.96rem; border-radius: 12px;
    border: 1px solid var(--borda); background: var(--input); color: var(--texto);
    outline: none; transition: border-color 0.2s ease, box-shadow 0.2s ease;
  }
  input::placeholder { color: #8c84a8; }
  input:focus, select:focus {
    border-color: var(--kick-brilho);
    box-shadow: 0 0 0 3px rgba(0, 255, 56, 0.22);
  }
  select {
    appearance: none; -webkit-appearance: none;
    background-image:
      linear-gradient(45deg, transparent 50%, var(--mutado) 50%),
      linear-gradient(135deg, var(--mutado) 50%, transparent 50%);
    background-position: calc(100% - 18px) calc(50% - 2px), calc(100% - 13px) calc(50% - 2px);
    background-size: 5px 5px, 5px 5px;
    background-repeat: no-repeat;
    padding-right: 36px;
  }
  .btn-principal {
    width: 100%; padding: 15px 18px; margin-top: 4px; font-size: 1rem;
    font-weight: 700; border-radius: 12px; border: none;
    background: var(--kick-verde); color: #0d0a16; cursor: pointer;
    box-shadow: 0 10px 26px rgba(0, 255, 56, 0.35);
    transition: transform 0.15s ease, background 0.2s ease;
  }
  .btn-principal:hover { background: var(--kick-brilho); transform: translateY(-1px); }
  .btn-principal:active { transform: translateY(0); }
  .btn-principal:disabled { opacity: 0.55; cursor: not-allowed; transform: none; }
  .caixa {
    background: var(--surface); border: 1px solid var(--borda);
    border-radius: 16px; padding: 14px 16px; margin-top: 14px; font-size: 0.92rem;
  }
  #status { font-weight: 600; color: var(--texto); }
  .diag-topo {
    font-size: 0.76rem; text-transform: uppercase; letter-spacing: 0.06em;
    color: var(--mutado); font-weight: 700;
  }
  pre {
    white-space: pre-wrap; word-break: break-word;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 0.78rem; color: #c4b5fd; margin: 8px 0 0; line-height: 1.45;
  }
  .card-live {
    background: var(--surface); border: 1px solid var(--borda-forte);
    border-radius: 20px; padding: 14px; margin-top: 14px;
  }
  .card-topo { display: flex; justify-content: space-between; margin-bottom: 10px; font-weight: 700; }
  .ponto {
    display: inline-block; width: 10px; height: 10px; border-radius: 50%;
    background: var(--vivo); margin-right: 8px;
  }
  .player { width: 100%; aspect-ratio: 16/9; border: 0; border-radius: 12px; background: #000; }
  .player-aviso { margin: 8px 0 0; font-size: .76rem; color: var(--mutado); }
  .live-item { margin-top: 12px; }
  .live-topo {
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 8px; font-weight: 700;
  }
  .btn-mini {
    background: var(--kick-verde); color: #0d0a16; border: 0; border-radius: 10px;
    padding: 8px 12px; font-weight: 700; font-size: .8rem; cursor: pointer;
    transition: filter 0.2s ease;
  }
  .btn-mini:hover { filter: brightness(1.1); }
  .btn-mini:disabled { opacity: 0.55; cursor: not-allowed; }
  .placeholder {
    width: 100%; aspect-ratio: 16/9; border-radius: 12px; background: #000;
    border: 1px solid var(--borda); display: flex; flex-direction: column;
    align-items: center; justify-content: center; gap: 8px; cursor: pointer;
    color: var(--texto);
  }
  .placeholder .play {
    width: 56px; height: 56px; border-radius: 50%; background: var(--kick-verde);
    color: #0d0a16; display: flex; align-items: center; justify-content: center;
    font-size: 1.4rem; padding-left: 4px;
  }
  .placeholder small { color: var(--mutado); font-size: .78rem; }
  .viewers { font-weight: 500; font-size: .8rem; color: var(--mutado); margin-left: 8px; }
  .hero-titulo {
    background: var(--grad); -webkit-background-clip: text; background-clip: text;
    color: transparent; -webkit-text-fill-color: transparent;
  }
  .btn-principal { background: var(--grad); }
  .btn-principal:hover { background: var(--grad); filter: brightness(1.1); }
  .btn-topo { background: var(--grad); }
  .logo-box { border-color: rgba(37, 244, 238, 0.4); }
  .ponto.off { background: #6b6585; }
  .btn-mini.off { background: #2a2540; color: var(--texto); }
  .rodape { text-align: center; margin: 14px 0 0; }
  .rodape a { color: var(--mutado); font-size: 0.78rem; text-decoration: underline; cursor: pointer; }
  .modal-fundo { position: fixed; inset: 0; background: rgba(0,0,0,0.7); z-index: 50; display: none; align-items: center; justify-content: center; padding: 16px; }
  .modal { background: var(--surface); border: 1px solid var(--borda-forte); border-radius: 16px; max-width: 480px; width: 100%; max-height: 80vh; overflow-y: auto; padding: 18px; }
  .modal h2 { margin: 0 0 10px; font-size: 1.1rem; }
  .modal p { color: var(--mutado); font-size: 0.86rem; line-height: 1.5; margin: 8px 0; }
  .modal .btn-mini { margin-top: 10px; width: 100%; }
  .aviso { color: #7c7496; font-size: 0.78rem; text-align: center; margin: 18px 0 0; line-height: 1.45; }

  /* NOVO: lista de clipes, player e gerar mais */
  .clipe-item { margin-top: 10px; background: var(--surface-2); border: 1px solid var(--borda); border-radius: 12px; padding: 10px 12px; }
  .clipe-topo { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
  .clipe-nome { font-weight: 700; font-size: 0.9rem; }
  .clipe-tempo { font-size: 0.78rem; color: var(--mutado); margin-top: 2px; }
  .clipe-acoes { display: flex; gap: 6px; }
  .clipe-acoes a, .clipe-acoes button {
    background: var(--kick-verde); color: #0d0a16; border: 0; border-radius: 10px;
    padding: 7px 11px; font-weight: 700; font-size: .78rem; cursor: pointer; text-decoration: none;
  }
  .clipe-acoes button.sec { background: #2a2540; color: var(--texto); }
  .clipe-video { width: 100%; max-height: 70vh; border-radius: 10px; background: #000; margin-top: 10px; }
  .zip-link { display: block; text-align: center; padding: 12px; border-radius: 10px; font-weight: 700; text-decoration: none;
    background: rgba(0,255,56,0.22); border: 1px solid var(--kick-brilho); color: var(--kick-brilho); }
  .mais-box { margin-top: 12px; display: flex; gap: 8px; align-items: center; }
  .mais-box select { flex: 1; }
  .mais-box button { white-space: nowrap; }
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
    </div>
  </header>

  <main class="wrap">
    <section class="hero-card">
      <h1 class="hero-titulo">Transforme lives em clipes de 1 minuto</h1>
      <p class="hero-sub">Cole o link da Kick e corte uma live <strong>ao vivo</strong> ou uma <strong>live antiga</strong> (com os melhores momentos). Baixe os cortes prontos em MP4 ou ZIP.</p>

      <div class="campo">
        <label for="url">Link da live</label>
        <input id="url" type="url" placeholder="Cole o link da Kick...">
      </div>

      <div class="campo">
        <label for="modo">O que você quer clipar?</label>
        <select id="modo" onchange="trocarModo()">
          <option value="ao_vivo" selected>Live ao vivo</option>
          <option value="vod">Live antiga (melhores momentos)</option>
        </select>
      </div>

      <div class="campo" id="grupo-min">
        <label for="min">Duração da captura</label>
        <select id="min">
          <option value="0.5">Gravar 30 segundos</option>
          <option value="1">Gravar 1 minuto</option>
          <option value="5">Gravar 5 minutos (teste)</option>
          <option value="15">Gravar 15 minutos</option>
          <option value="30">Gravar 30 minutos</option>
          <option value="60">Gravar 60 minutos</option>
        </select>
      </div>

      <div class="campo">
        <label for="seg">Duração de cada clipe</label>
        <select id="seg">
          <option value="60" selected>1 minuto</option>
          <option value="30">30 segundos</option>
        </select>
      </div>

      <!-- NOVO: formato do vídeo -->
      <div class="campo">
        <label for="formato">Formato do vídeo</label>
        <select id="formato">
          <option value="original" selected>Original (horizontal, mais rápido)</option>
          <option value="vertical_blur">Vertical 9:16 com fundo desfocado</option>
          <option value="vertical_crop">Vertical 9:16 com recorte central</option>
        </select>
        <p class="player-aviso">Vertical reencoda o vídeo, então demora mais pra ficar pronto.</p>
      </div>

      <div class="campo" id="grupo-qtd" style="display:none">
        <label for="qtd">Quantos clipes dos melhores momentos</label>
        <select id="qtd">
          <option value="3">3 clipes</option>
          <option value="5" selected>5 clipes</option>
          <option value="8">8 clipes</option>
          <option value="10">10 clipes</option>
        </select>
        <p class="player-aviso">Usa o link do canal (ou de uma live antiga). Pega a live mais recente e acha os picos do chat.</p>
        <button type="button" class="btn-mini" style="margin-top:8px" onclick="testarVod()">Testar se o servidor lê a Kick</button>
      </div>

      <button id="btn-gravar" class="btn-principal" onclick="iniciar()">Cortar meu vídeo</button>
    </section>

    <section class="card-live" id="card-live" style="display:none">
      <div class="card-topo">
        <span><span class="ponto"></span><span id="plat-nome">Live</span></span>
        <span id="plat-status">AO VIVO</span>
      </div>
      <iframe id="player" class="player" allowfullscreen allow="autoplay; fullscreen"></iframe>
      <p class="player-aviso" id="player-aviso">Se o vídeo não aparecer, a gravação ainda pode funcionar.</p>
    </section>

    <div class="caixa" id="status">Aguardando link.</div>
    <div class="caixa" id="lista" style="display:none"></div>

    <!-- NOVO: gerar mais clipes -->
    <div class="caixa" id="mais" style="display:none">
      <span class="diag-topo">Quer mais momentos dessa live?</span>
      <div class="mais-box">
        <select id="mais-qtd">
          <option value="3">Mais 3 clipes</option>
          <option value="5" selected>Mais 5 clipes</option>
          <option value="10">Mais 10 clipes</option>
          <option value="15">Mais 15 clipes</option>
        </select>
        <button type="button" class="btn-mini" id="btn-mais" onclick="maisClipes()">Gerar mais</button>
      </div>
      <p class="player-aviso" id="mais-aviso"></p>
    </div>

    <div class="caixa">
      <span class="diag-topo">Diagnóstico</span>
      <pre id="diag">-</pre>
    </div>

    <section class="caixa" id="lives-box">
      <span class="diag-topo">Lives agora na Kick</span>
      <div id="lives"></div>
    </section>

    <p class="aviso">Usa apenas com autorização do streamer ou nas tuas próprias lives.</p>
    <p class="rodape"><a onclick="abrirTermos()">Termos de uso</a></p>
  </main>

  <div class="modal-fundo" id="termos" onclick="if(event.target===this){fecharTermos()}">
    <div class="modal">
      <h2>Termos de uso</h2>
      <p>1. Use o Clipador de lives apenas com autorização do streamer ou em transmissões suas.</p>
      <p>2. O conteúdo das lives e dos clipes continua sendo do criador original. Este site não reivindica direitos sobre ele.</p>
      <p>3. Este site não tem ligação com a Kick nem com nenhum streamer. Kick é marca de seus respectivos donos.</p>
      <p>4. Os clipes são gerados de forma automática e podem conter erros ou ficar fora do ponto esperado.</p>
      <p>5. Você é o único responsável pelo uso e pela publicação dos clipes, inclusive em outras plataformas.</p>
      <p>6. Os arquivos ficam no servidor só por um tempo e podem ser apagados a qualquer momento. Baixe o que quiser guardar.</p>
      <p>7. O serviço é oferecido como está, sem garantia de funcionamento ou disponibilidade.</p>
      <p>8. Se o dono de um conteúdo pedir a remoção, o pedido será atendido.</p>
      <button class="btn-mini" onclick="fecharTermos()">Fechar</button>
    </div>
  </div>

<script>
let job = null;
let ultimoClipesRenderizado = -1;
let ultimoJobRenderizado = null;
let ultimaAssinaturaLista = '';

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

const estadoCanal = {};  // canal -> 'online' | 'offline' | 'incerto'

function abrirTermos() { document.getElementById('termos').style.display = 'flex'; }
function fecharTermos() { document.getElementById('termos').style.display = 'none'; }

function atualizarStatusPreview() {
  const p = plataforma(document.getElementById('url').value.trim());
  const txt = document.getElementById('plat-status');
  const ponto = document.querySelector('#card-live .ponto');
  if (!txt || !ponto) { return; }
  let canal = '';
  if (p) {
    try { canal = new URL(document.getElementById('url').value.trim()).pathname.split('/').filter(Boolean)[0] || ''; } catch (e) {}
  }
  const off = estadoCanal[canal] === 'offline';
  txt.innerText = off ? 'OFFLINE' : 'AO VIVO';
  if (off) { ponto.classList.add('off'); } else { ponto.classList.remove('off'); }
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
  playerEl.style.display = '';
  avisoEl.innerText = 'Se o vídeo não aparecer, a gravação ainda pode funcionar.';
  if (p.embed !== ultimoEmbed) {
    document.getElementById('player').src = p.embed;
    ultimoEmbed = p.embed;
  }
  atualizarStatusPreview();
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
  // lista: [{canal, nome, viewers, offline}] online primeiro (maior público), offline depois
  const assinatura = lista.map(function (c) { return c.canal + ':' + c.viewers + ':' + (c.offline ? 'off' : 'on'); }).join('|');
  if (assinatura === assinaturaLives) { return; }
  assinaturaLives = assinatura;

  const temPublico = lista.length > 0 && lista[0].viewers !== null && !lista[0].offline;
  let h = '';
  lista.forEach(function (c, pos) {
    const idx = CANAIS.findIndex(function (item) { return item.canal === c.canal; });
    if (c.offline) {
      h += '<div class="live-item"><div class="live-topo"><span><span class="ponto off"></span>' + c.nome +
        '<span class="viewers">Offline</span></span>' +
        '<button class="btn-mini off" onclick="clipar(' + idx + ')">Usar última live</button></div></div>';
      return;
    }
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

    resultados.forEach(function (x) { estadoCanal[x.c.canal] = x.status; });
    atualizarStatusPreview();

    const online = resultados.filter(function (x) { return x.status === 'online'; });
    const offlineLista = resultados.filter(function (x) { return x.status === 'offline'; });

    if (online.length > 0 || offlineLista.length > 0) {
      online.sort(function (a, b) { return (b.viewers || 0) - (a.viewers || 0); });
      const lista = online.map(function (x) {
        return {canal: x.c.canal, nome: x.c.nome, viewers: x.viewers, offline: false};
      }).concat(offlineLista.map(function (x) {
        return {canal: x.c.canal, nome: x.c.nome, viewers: null, offline: true};
      }));
      montarLives(lista);
    }
  } catch (e) {}
}
filtrarOnline();
setInterval(filtrarOnline, 30000);


function trocarModo() {
  const vod = document.getElementById('modo').value === 'vod';
  document.getElementById('grupo-qtd').style.display = vod ? 'block' : 'none';
  document.getElementById('grupo-min').style.display = vod ? 'none' : 'block';
  document.getElementById('btn-gravar').innerText = vod ? 'Achar melhores momentos' : 'Cortar meu vídeo';
}

async function testarVod() {
  const url = document.getElementById('url').value.trim();
  let canal = 'bak';
  try {
    const partes = new URL(url).pathname.split('/').filter(Boolean);
    if (partes[0]) { canal = partes[0]; }
  } catch (e) {}
  document.getElementById('status').innerText = 'Testando a leitura da Kick...';
  try {
    const r = await fetch('/api/vod/teste?canal=' + encodeURIComponent(canal));
    const d = await r.json();
    document.getElementById('diag').innerText = JSON.stringify(d, null, 2);
    const ok = d.lista_vods && d.lista_vods.ok && d.chat && d.chat.ok;
    document.getElementById('status').innerText = ok
      ? 'Funcionou: o servidor lê as lives antigas e o chat.'
      : 'Não deu: veja o diagnóstico abaixo.';
  } catch (e) {
    document.getElementById('status').innerText = 'Erro ao testar.';
  }
}

// MUDANÇA: envia o formato e zera a lista e o bloco de mais clipes
async function iniciar() {
  const url = document.getElementById('url').value.trim();
  const min = document.getElementById('min').value;
  const seg = document.getElementById('seg').value;
  const formato = document.getElementById('formato').value;
  const btn = document.getElementById('btn-gravar');
  if (!url) { return; }
  btn.disabled = true;
  ultimoClipesRenderizado = -1;
  ultimaAssinaturaLista = '';
  document.getElementById('lista').style.display = 'none';
  document.getElementById('lista').innerHTML = '';
  document.getElementById('mais').style.display = 'none';
  document.getElementById('status').innerText = 'Iniciando...';
  try {
    const vod = document.getElementById('modo').value === 'vod';
    const r = await fetch(vod ? '/iniciar_vod' : '/iniciar', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(vod
        ? {url: url, seg: seg, qtd: document.getElementById('qtd').value, formato: formato}
        : {url: url, min: min, seg: seg, formato: formato})
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

// NOVO: H:MM:SS no navegador
function fmtTempo(s) {
  s = Math.max(0, Math.floor(s));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const mm = String(m).padStart(2, '0');
  const ss = String(sec).padStart(2, '0');
  return h > 0 ? h + ':' + mm + ':' + ss : m + ':' + ss;
}

// MUDANÇA: lista com intervalo, Assistir e Baixar
function atualizarListaClipes(clipesQtd, jobId, info) {
  const lista = document.getElementById('lista');
  if (clipesQtd <= 0) {
    lista.style.display = 'none';
    return;
  }
  const assinatura = jobId + ':' + clipesQtd;
  if (assinatura === ultimaAssinaturaLista) {
    return;
  }
  ultimaAssinaturaLista = assinatura;
  ultimoClipesRenderizado = clipesQtd;
  ultimoJobRenderizado = jobId;
  lista.style.display = 'block';

  const porNumero = {};
  (info || []).forEach(function (x) { porNumero[x.n] = x; });

  let h = '<a class="zip-link" href="/zip/' + jobId + '">Baixar todos em ZIP (' + clipesQtd + ' clipes)</a>';
  for (let i = 1; i <= clipesQtd; i++) {
    const x = porNumero[i];
    const tempo = x
      ? '<div class="clipe-tempo">' + fmtTempo(x.ini) + ' até ' + fmtTempo(x.fim) +
        ' da live &middot; ' + x.msgs + ' msgs no chat</div>'
      : '';
    h += '<div class="clipe-item" id="clipe-' + i + '">' +
      '<div class="clipe-topo"><div><div class="clipe-nome">Clipe ' + i + '</div>' + tempo + '</div>' +
      '<div class="clipe-acoes">' +
      '<button type="button" class="sec" onclick="assistir(' + i + ')">Assistir</button>' +
      '<a href="/clipe/' + jobId + '/' + i + '">Baixar</a>' +
      '</div></div>' +
      '<div id="video-' + i + '"></div></div>';
  }
  lista.innerHTML = h;
}

// NOVO: pré-visualização do clipe na própria página
function assistir(n) {
  const alvo = document.getElementById('video-' + n);
  if (!alvo || !job) { return; }
  if (alvo.innerHTML) {
    alvo.innerHTML = '';
    return;
  }
  alvo.innerHTML = '<video class="clipe-video" controls autoplay playsinline src="/ver/' + job + '/' + n + '"></video>';
}

// MUDANÇA: mostra o bloco de gerar mais quando termina
async function checar() {
  if (!job) { return; }
  try {
    const r = await fetch('/status/' + job);
    const d = await r.json();
    document.getElementById('status').innerText = d.msg || '';
    document.getElementById('diag').innerText = d.diag || '-';
    atualizarListaClipes(Number(d.clipes || 0), job, d.info || []);

    const terminou = d.estado === 'fim' || d.estado === 'erro';
    const maisBox = document.getElementById('mais');
    const temMais = (d.restantes || 0) > 0;
    if (d.estado === 'fim' && temMais) {
      maisBox.style.display = 'block';
      document.getElementById('mais-aviso').innerText =
        'Ainda restam ' + d.restantes + ' momentos de destaque nessa live.';
    } else {
      maisBox.style.display = 'none';
    }

    if (!terminou) {
      setTimeout(checar, 3000);
    } else {
      document.getElementById('btn-gravar').disabled = false;
      document.getElementById('btn-mais').disabled = false;
    }
  } catch (e) {
    setTimeout(checar, 3000);
  }
}

// NOVO: pede mais clipes do mesmo job
async function maisClipes() {
  if (!job) { return; }
  const btn = document.getElementById('btn-mais');
  btn.disabled = true;
  try {
    const r = await fetch('/mais_clipes/' + job, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({qtd: document.getElementById('mais-qtd').value})
    });
    const d = await r.json();
    if (!r.ok) {
      document.getElementById('mais-aviso').innerText = d.erro || 'Não deu pra gerar mais.';
      btn.disabled = false;
      return;
    }
    document.getElementById('mais').style.display = 'none';
    document.getElementById('btn-gravar').disabled = true;
    checar();
  } catch (e) {
    document.getElementById('mais-aviso').innerText = 'Erro ao pedir mais clipes.';
    btn.disabled = false;
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


# NOVO: formata segundos como H:MM:SS (ou M:SS)
def fmt_tempo(s):
    s = int(max(0, s))
    h, resto = divmod(s, 3600)
    m, sec = divmod(resto, 60)
    if h:
        return "%d:%02d:%02d" % (h, m, sec)
    return "%d:%02d" % (m, sec)


# NOVO: texto do intervalo de um clipe
def intervalo_clipe(ini, tamanho):
    return "%s até %s" % (fmt_tempo(ini), fmt_tempo(ini + tamanho))


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
    return url


# NOVO: formatos de saída
FORMATOS = ("original", "vertical_blur", "vertical_crop")


# NOVO: argumentos do ffmpeg para cada formato
def args_formato(formato):
    if formato == "vertical_blur":
        filtro = (
            "split=2[fundo][frente];"
            "[fundo]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,boxblur=25:5[bg];"
            "[frente]scale=1080:-2[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1"
        )
        return ["-filter_complex", filtro,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k"]
    if formato == "vertical_crop":
        filtro = "crop=ih*9/16:ih,scale=1080:1920,setsar=1"
        return ["-vf", filtro,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k"]
    return ["-c", "copy"]


# MUDANÇA: recebe o formato
def gravar(job_id, url, minutos, seg=SEG, formato="original"):
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

    hdr = "".join(
        f"{k}: {v}\r\n" for k, v in headers.items()
        if k.lower() != "user-agent" and v
    )
    extra_headers = ["-headers", hdr] if hdr else []

    atualizar_job(
        job_id,
        estado="gravando",
        msg="Gravando... 0 clipes prontos",
        diag="yt-dlp OK. Iniciando ffmpeg...",
    )

    # NOVO: vídeo conforme o formato escolhido
    extra_video = args_formato(formato)
    if formato != "original":
        extra_video += ["-force_key_frames", f"expr:gte(t,n_forced*{seg})"]

    cmd = [
        "ffmpeg", "-y",
        "-user_agent", user_agent,
        *extra_headers,
        "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-rw_timeout", "15000000",
        "-i", stream,
        "-t", str(int(minutos * 60)),
        *extra_video,  # MUDANÇA: no lugar de "-c", "copy"
        "-f", "segment",
        "-segment_time", str(seg),
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


# ======================= LIVES ANTIGAS (VOD) =======================
import re
from datetime import datetime, timezone

UA_NAV = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def kick_get_json(url, timeout=20):
    """GET em JSON na Kick. Retorna (dados, erro). Usa curl_cffi se existir (passa melhor na Cloudflare)."""
    try:
        try:
            from curl_cffi import requests as cr
            r = cr.get(url, impersonate="chrome", timeout=timeout,
                       headers={"Accept": "application/json"})
            if r.status_code != 200:
                return None, "HTTP %s" % r.status_code
            return r.json(), ""
        except ImportError:
            pass
        req = Request(url, headers={
            "User-Agent": UA_NAV,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
            "Referer": "https://kick.com/",
        })
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="ignore")), ""
    except Exception as e:
        return None, str(e)[-160:]


def slug_da_url(url):
    try:
        u = urlparse(url)
        partes = [p for p in u.path.split("/") if p]
        if (u.hostname or "").replace("www.", "") in ("kick.com", "kzl.ink") and partes:
            return partes[0]
    except Exception:
        pass
    return ""


def listar_vods(slug):
    """Retorna (lista, erro). Cada item: uuid, titulo, inicio(iso), duracao_s, views, source."""
    dados, erro = kick_get_json("https://kick.com/api/v2/channels/%s/videos" % quote(slug))
    if dados is None:
        return [], erro
    if isinstance(dados, dict):
        dados = dados.get("data") or dados.get("videos") or []
    out = []
    for v in dados:
        vid = v.get("video") or {}
        uuid_ = vid.get("uuid") or v.get("uuid")
        if not uuid_ or vid.get("is_private"):
            continue
        out.append({
            "uuid": uuid_,
            "titulo": v.get("session_title") or "Live sem título",
            "inicio": v.get("start_time") or v.get("created_at") or "",
            "duracao_s": int((v.get("duration") or 0) / 1000),
            "views": v.get("views") or 0,
            "source": v.get("source") or "",
            "ao_vivo": bool(v.get("is_live")),
        })
    return out, ""


def escolher_vod(vods):
    """Primeira live já encerrada (a em andamento vem com duração 0)."""
    for v in vods:
        if v["duracao_s"] > 0 and not v.get("ao_vivo"):
            return v
    return None


def _iso_para_epoch(txt):
    txt = (txt or "").strip().replace(" ", "T")
    if not txt:
        return None
    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def baixar_chat_vod(canal_slug, inicio_epoch, duracao_s, progresso=None):
    """Percorre o chat de trás pra frente a partir do fim da VOD. Retorna (lista de segundos relativos, erro)."""
    canal, erro = kick_get_json("https://kick.com/api/v2/channels/%s" % quote(canal_slug))
    if not canal or "id" not in canal:
        return [], "não consegui o id do canal (%s)" % erro
    cid = canal["id"]

    fim_epoch = inicio_epoch + duracao_s
    cursor = str(int(fim_epoch * 1_000_000))
    tempos, vistos = [], set()
    paginas, sem_novas = 0, 0
    while paginas < 4000:
        dados, erro = kick_get_json(
            "https://kick.com/api/v2/channels/%s/messages?cursor=%s" % (cid, cursor))
        # se a Kick pedir calma (429), espera e tenta de novo
        tentativas = 0
        while dados is None and "429" in (erro or "") and tentativas < 5:
            tentativas += 1
            time.sleep(5 * tentativas)
            dados, erro = kick_get_json(
                "https://kick.com/api/v2/channels/%s/messages?cursor=%s" % (cid, cursor))
        if dados is None:
            return tempos, ("falha na página %d: %s" % (paginas, erro)) if not tempos else ""
        bloco = dados.get("data") or {}
        msgs = bloco.get("messages") or []
        novas = 0
        menor = None
        for m in msgs:
            mid = m.get("id")
            if mid in vistos:
                continue
            vistos.add(mid)
            t = _iso_para_epoch(m.get("created_at"))
            if t is None:
                continue
            menor = t if menor is None else min(menor, t)
            if inicio_epoch <= t <= fim_epoch:
                tempos.append(t - inicio_epoch)
                novas += 1
        paginas += 1
        proximo = bloco.get("cursor")
        if progresso and (paginas == 1 or paginas % 10 == 0):
            progresso(paginas, len(tempos), menor)
        if not proximo or not msgs:
            break
        if menor is not None and menor < inicio_epoch:
            break
        sem_novas = sem_novas + 1 if novas == 0 else 0
        if sem_novas >= 30:
            break
        cursor = str(proximo)
        time.sleep(0.1)
    return tempos, ""


# MUDANÇA: devolve o ranking do pico mais forte ao mais fraco (sem ordenar por horário)
def achar_picos(tempos, duracao_s, quantidade, tamanho_clipe):
    """Janela deslizante de 30s; pega os maiores picos sem sobrepor. Retorna lista de (inicio_s, msgs)."""
    if not tempos or duracao_s <= 0:
        return []
    janela = 30
    n = int(duracao_s) + 1
    por_seg = [0] * (n + 1)
    for t in tempos:
        i = int(t)
        if 0 <= i < n:
            por_seg[i] += 1
    soma = [0] * (n + 2)
    for i in range(n + 1):
        soma[i + 1] = soma[i] + por_seg[i]
    pontos = []
    for i in range(n):
        j = min(n, i + janela)
        pontos.append((soma[j] - soma[i], i))
    pontos.sort(reverse=True)
    escolhidos = []
    for score, i in pontos:
        if score <= 0 or len(escolhidos) >= quantidade:
            break
        centro = i + janela // 2
        ini = max(0, centro - tamanho_clipe // 2)
        if ini + tamanho_clipe > duracao_s:
            ini = max(0, duracao_s - tamanho_clipe)
        if all(abs(ini - e[0]) >= max(tamanho_clipe, 90) for e in escolhidos):
            escolhidos.append((ini, score))
    return escolhidos


def stream_da_vod(slug, uuid_):
    url = "https://kick.com/%s/videos/%s" % (slug, uuid_)
    return extrair_stream(url)


# NOVO: corta um clipe da VOD usando os dados guardados no job
def cortar_clipe(job_id, idx, ini, formato):
    with JOBS_LOCK:
        j = JOBS[job_id]
        pasta, stream = j["pasta"], j["stream"]
        headers, tam = j["headers"], j["tamanho_clipe"]

    user_agent = headers.get("User-Agent", UA_NAV)
    hdr = "".join("%s: %s\r\n" % (k, v) for k, v in headers.items()
                  if k.lower() != "user-agent" and v)
    extra = ["-headers", hdr] if hdr else []

    saida = os.path.join(pasta, "clipe_%03d.mp4" % idx)
    cmd = ["ffmpeg", "-y", "-user_agent", user_agent, *extra,
           "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
           "-rw_timeout", "20000000",
           "-ss", str(int(ini)), "-i", stream, "-t", str(tam),
           *args_formato(formato),
           "-avoid_negative_ts", "make_zero", saida]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=240 if formato == "original" else 600)
    except subprocess.TimeoutExpired:
        return False, "demorou demais"
    if r.returncode == 0 and os.path.isfile(saida) and os.path.getsize(saida) > 0:
        return True, ""
    return False, (r.stderr or "")[-120:].strip()


# NOVO: corta uma lista de (inicio_s, msgs) e guarda os intervalos exatos
def _cortar_lote(job_id, lote, formato, primeiro_idx):
    with JOBS_LOCK:
        tam = JOBS[job_id]["tamanho_clipe"]
        info = list(JOBS[job_id]["info"])
        feitos = JOBS[job_id]["clipes"]
    notas = []
    for k, (ini, score) in enumerate(lote):
        idx = primeiro_idx + k
        ok, nota = cortar_clipe(job_id, idx, ini, formato)
        if ok:
            feitos += 1
            info.append({"n": idx + 1, "ini": int(ini), "fim": int(ini + tam), "msgs": score})
            notas.append("Clipe %d: %s (%d msgs)" % (idx + 1, intervalo_clipe(ini, tam), score))
        else:
            notas.append("Clipe %d falhou: %s" % (idx + 1, nota))
        atualizar_job(job_id, clipes=feitos, info=info,
                      msg="Cortando... %d de %d" % (k + 1, len(lote)),
                      diag="\n".join(notas)[-900:])

    if feitos:
        atualizar_job(job_id, estado="fim", clipes=feitos, info=info, ocupado=False,
                      msg="Pronto! %d clipes dos melhores momentos." % feitos,
                      diag="\n".join(notas)[-900:])
    else:
        atualizar_job(job_id, estado="erro", clipes=0, ocupado=False,
                      msg="Não consegui cortar nenhum clipe.",
                      diag="\n".join(notas)[-900:])


# MUDANÇA: usa o ranking completo e guarda tudo no job
def gravar_vod(job_id, url, tamanho_clipe, quantidade, formato="original"):
    pasta = os.path.join(BASE, job_id)
    os.makedirs(pasta, exist_ok=True)
    atualizar_job(job_id, pasta=pasta, estado="abrindo", msg="Procurando a live antiga...", diag="")

    slug = slug_da_url(url)
    m = re.search(r"/videos/([0-9a-fA-F-]{36})", url)
    if not slug:
        atualizar_job(job_id, estado="erro", msg="Link inválido.", diag="Use o link do canal da Kick.")
        return

    vods, erro = listar_vods(slug)
    if not vods:
        atualizar_job(job_id, estado="erro",
                      msg="Não consegui listar as lives antigas desse canal.",
                      diag=erro or "O canal não tem lives salvas.")
        return
    escolhida = None
    if m:
        escolhida = next((v for v in vods if v["uuid"] == m.group(1)), None)
    if not escolhida:
        escolhida = escolher_vod(vods) or vods[0]

    inicio_epoch = _iso_para_epoch(escolhida["inicio"])
    dur = escolhida["duracao_s"]
    if inicio_epoch is None or dur <= 0:
        atualizar_job(job_id, estado="erro", msg="Essa live não tem horário ou duração válidos.",
                      diag=json.dumps(escolhida)[:400])
        return

    t_chat = time.time()

    def prog(pags, msgs, menor=None):
        resto = ""
        try:
            fim_epoch = inicio_epoch + dur
            if menor is not None and dur > 0:
                feito = max(0.0, fim_epoch - max(menor, inicio_epoch))
                frac = min(1.0, feito / float(dur))
                gasto = time.time() - t_chat
                if frac > 0.01 and gasto > 5:
                    falta = int(gasto * (1 - frac) / frac)
                    if falta >= 60:
                        resto = "Faltam uns %d min" % ((falta + 59) // 60)
                    else:
                        resto = "Faltam menos de 1 min"
        except Exception:
            resto = ""
        atualizar_job(job_id, estado="analisando",
                      msg="Lendo o chat da live... %d mensagens" % msgs,
                      diag=(resto + "\n" if resto else "") +
                           "Página %d do chat | live: %s" % (pags, escolhida["titulo"][:60]))

    tempos, erro = baixar_chat_vod(slug, inicio_epoch, dur, prog)
    if not tempos:
        atualizar_job(job_id, estado="erro", msg="Não consegui ler o chat dessa live.",
                      diag=erro or "Nenhuma mensagem encontrada no período.")
        return

    # NOVO: guarda até 60 picos, do mais forte ao mais fraco
    ranking = achar_picos(tempos, dur, 60, tamanho_clipe)
    if not ranking:
        atualizar_job(job_id, estado="erro", msg="Não achei momentos de destaque no chat.", diag="")
        return

    # os N primeiros do ranking, em ordem de horário
    primeiros = sorted(ranking[:quantidade])

    # usa o link do vídeo que a lista já trouxe (evita o 429 do yt-dlp)
    src = escolhida.get("source") or ""
    if src.startswith("http"):
        stream, headers, erro_yt = src, {}, ""
    else:
        stream, headers, erro_yt = stream_da_vod(slug, escolhida["uuid"])
    if not stream:
        atualizar_job(job_id, estado="erro", msg="Não consegui o vídeo dessa live.",
                      diag=erro_yt or "sem mensagem")
        return

    atualizar_job(job_id, estado="gravando",
                  msg="Achei %d momentos. Cortando..." % len(primeiros),
                  diag="Chat: %d mensagens analisadas" % len(tempos),
                  picos=ranking, usados=len(primeiros), stream=stream, headers=headers)

    with JOBS_LOCK:
        formato_job = JOBS[job_id]["formato"]

    _cortar_lote(job_id, primeiros, formato_job, primeiro_idx=0)


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
        minutos = float(dados.get("min", 5))
    except (TypeError, ValueError):
        minutos = 5
    minutos = max(0.5, min(minutos, 180))

    try:
        seg = int(dados.get("seg", 60))
    except (TypeError, ValueError):
        seg = 60
    if seg not in (30, 60):
        seg = 60

    # o clipe segue a captura. 30s -> clipe de 30s; o resto -> clipes de 1 minuto
    if minutos <= 0.5:
        seg = 30
    else:
        seg = 60

    # NOVO: formato
    formato = str(dados.get("formato", "original"))
    if formato not in FORMATOS:
        formato = "original"

    job_id = uuid.uuid4().hex[:8]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "estado": "iniciando",
            "msg": "Iniciando...",
            "clipes": 0,
            "diag": "",
            "pasta": "",
        }

    threading.Thread(target=gravar, args=(job_id, url, minutos, seg, formato), daemon=True).start()
    return jsonify({"id": job_id})


@app.route("/iniciar_vod", methods=["POST"])
def iniciar_vod():
    dados = request.get_json(silent=True) or {}
    url = str(dados.get("url", "")).strip()
    if not (url.startswith("http://") or url.startswith("https://")) or not slug_da_url(url):
        return jsonify({"id": "", "erro": "Cole o link do canal da Kick."}), 400
    try:
        seg = int(dados.get("seg", 60))
    except (TypeError, ValueError):
        seg = 60
    if seg not in (30, 60):
        seg = 60
    try:
        qtd = int(dados.get("qtd", 5))
    except (TypeError, ValueError):
        qtd = 5
    qtd = max(1, min(qtd, 15))

    # NOVO: formato
    formato = str(dados.get("formato", "original"))
    if formato not in FORMATOS:
        formato = "original"

    job_id = uuid.uuid4().hex[:8]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "estado": "iniciando", "msg": "Iniciando...", "clipes": 0,
            "diag": "", "pasta": "",
            # NOVO: dados que o gerar-mais precisa
            "info": [],
            "picos": [],
            "usados": 0,
            "stream": "",
            "headers": {},
            "tamanho_clipe": seg,
            "formato": formato,
            "ocupado": False,
        }
    threading.Thread(target=gravar_vod, args=(job_id, url, seg, qtd, formato), daemon=True).start()
    return jsonify({"id": job_id})


# NOVO: gera mais clipes do mesmo job, sem reler o chat
@app.route("/mais_clipes/<job_id>", methods=["POST"])
def mais_clipes(job_id):
    dados = request.get_json(silent=True) or {}
    try:
        qtd = max(1, min(int(dados.get("qtd", 5)), 15))
    except (TypeError, ValueError):
        qtd = 5

    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j or not j.get("picos"):
            return jsonify({"erro": "Esse job não tem lista de picos. Rode os melhores momentos antes."}), 404
        if j.get("ocupado") or j["estado"] in ("abrindo", "analisando", "gravando"):
            return jsonify({"erro": "Ainda tem clipes sendo cortados. Espere terminar."}), 409
        usados = j.get("usados", 0)
        restantes = j["picos"][usados:usados + qtd]
        if not restantes:
            return jsonify({"erro": "Acabaram os momentos de destaque dessa live."}), 404
        j["usados"] = usados + len(restantes)
        j["ocupado"] = True
        j["estado"] = "gravando"
        j["msg"] = "Cortando mais %d clipes..." % len(restantes)
        formato = j["formato"]
        primeiro_idx = len(listar_clipes(j["pasta"]))

    lote = sorted(restantes)
    threading.Thread(target=_cortar_lote, args=(job_id, lote, formato, primeiro_idx), daemon=True).start()
    return jsonify({"id": job_id, "novos": len(restantes)})


@app.route("/api/vod/teste")
def api_vod_teste():
    slug = request.args.get("canal", "bak").strip() or "bak"
    info = {"canal": slug, "curl_cffi": False}
    try:
        import curl_cffi  # noqa: F401
        info["curl_cffi"] = True
    except ImportError:
        pass
    vods, erro = listar_vods(slug)
    info["lista_vods"] = {"ok": bool(vods), "quantidade": len(vods), "erro": erro}
    if vods:
        v = escolher_vod(vods) or vods[0]
        info["primeira_vod"] = {"titulo": v["titulo"][:60], "inicio": v["inicio"], "duracao_s": v["duracao_s"]}
        canal, e2 = kick_get_json("https://kick.com/api/v2/channels/%s" % quote(slug))
        info["canal_id"] = {"ok": bool(canal and "id" in canal), "erro": e2}
        ini = _iso_para_epoch(v["inicio"])
        if canal and "id" in canal and ini and v["duracao_s"] > 0:
            cur = str(int((ini + v["duracao_s"]) * 1_000_000))
            d, e3 = kick_get_json("https://kick.com/api/v2/channels/%s/messages?cursor=%s" % (canal["id"], cur))
            msgs = ((d or {}).get("data") or {}).get("messages") or []
            info["chat"] = {"ok": bool(msgs), "mensagens_na_pagina": len(msgs), "erro": e3}
    return jsonify(info)


# MUDANÇA: devolve também os intervalos e quantos picos ainda restam
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
            "info": j.get("info", []),
            "restantes": max(0, len(j.get("picos", [])) - j.get("usados", 0)),
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


# NOVO: serve o clipe sem forçar download, para a pré-visualização
@app.route("/ver/<job_id>/<int:n>")
def ver_clipe(job_id, n):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        pasta = j.get("pasta") if j else ""
    if not pasta or n < 1:
        return "Não encontrado", 404
    arq = os.path.join(pasta, f"clipe_{n - 1:03d}.mp4")
    if not os.path.isfile(arq):
        return "Não encontrado", 404
    return send_file(arq, mimetype="video/mp4", conditional=True)


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
