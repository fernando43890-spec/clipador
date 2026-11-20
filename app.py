import os
import subprocess
import threading
import time
import uuid
import zipfile
from flask import Flask, request, send_file, render_template_string, jsonify

app = Flask(__name__)
BASE = "/tmp/clipador" if os.path.exists("/tmp") else "."
os.makedirs(BASE, exist_ok=True)
JOBS = {}
SEG = 60


PAGINA = """
<!doctype html>
<html lang="pt">
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
    --roxo: #8b6cf6;
    --roxo-brilho: #a488ff;
    --texto: #f5f3ff;
    --mutado: #a69ec2;
  }

  * {
    box-sizing: border-box;
  }

  body {
    margin: 0;
    min-height: 100vh;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, sans-serif;
    color: var(--texto);
    background:
      radial-gradient(ellipse at 50% -8%, rgba(139, 108, 246, 0.28), transparent 55%),
      var(--bg);
    padding-bottom: 40px;
  }

  .wrap {
    max-width: 540px;
    margin: 0 auto;
    padding: 20px 16px 0;
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
    font-size: 1.4rem;
    font-weight: 800;
    line-height: 1.2;
  }

  .hero-sub {
    margin: 8px 0 16px;
    color: var(--mutado);
    font-size: 0.9rem;
    line-height: 1.48;
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
  }

  .btn-principal {
    width: 100%;
    padding: 15px 18px;
    margin-top: 4px;
    font-size: 1rem;
    font-weight: 700;
    border-radius: 12px;
    border: none;
    background: var(--roxo);
    color: #0d0a16;
    cursor: pointer;
  }

  .btn-principal:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }

  .caixa {
    background: var(--surface);
    border: 1px solid var(--borda);
    border-radius: 16px;
    padding: 14px 16px;
    margin-top: 14px;
    font-size: 0.92rem;
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
  }

  #lista a:first-child {
    margin-top: 0;
    background: rgba(139, 108, 246, 0.22);
    border-color: var(--borda-forte);
    color: var(--roxo-brilho);
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
  }  .card-live{background:var(--surface);border:1px solid var(--borda-forte);border-radius:20px;padding:14px;margin-top:14px}
  .card-topo{display:flex;justify-content:space-between;margin-bottom:10px;font-weight:700}
  .ponto{display:inline-block;width:10px;height:10px;border-radius:50%;background:#ff5a4d;margin-right:8px}
  .player{width:100%;aspect-ratio:16/9;border:0;border-radius:12px;background:#000}
  .player-aviso{margin:8px 0 0;font-size:.76rem;color:var(--mutado)}
  .live-item{margin-top:12px}
  .live-topo{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;font-weight:700}
  .btn-mini{background:var(--roxo);color:#0d0a16;border:0;border-radius:10px;padding:8px 12px;font-weight:700;font-size:.8rem}

</style>
</head>
<body>
  <main class="wrap">
    <section class="hero-card">
      <h1 class="hero-titulo">Clipador de lives</h1>
      <p class="hero-sub">Cole o link da live, escolha a duração e baixe os clipes de 1 minuto.</p>

      <div class="campo">
        <label for="url">Link da live</label>
        <input id="url" type="url" placeholder="Cole o link da live (YouTube, Kick, Twitch...)">
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
    <section class="caixa" id="lives-box">
      <span class="diag-topo">Lives agora</span>
      <div id="lives"></div>
    </section>

    <div class="caixa" id="status">Aguardando link.</div>
    <div class="caixa" id="lista" style="display:none"></div>
    <div class="caixa">
      <span class="diag-topo">Diagnóstico</span>
      <pre id="diag">-</pre>
    </div>
  </main>

<script>
let job = null;

async function iniciar() {
  const url = document.getElementById('url').value.trim();
  const min = document.getElementById('min').value;
  const btn = document.getElementById('btn-gravar');
  if (!url) { return; }
  btn.disabled = true;
  document.getElementById('status').innerText = 'A iniciar...';
  try {
    const r = await fetch('/iniciar', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url: url, min: min})
    });
    const d = await r.json();
    if (!d.id) {
      document.getElementById('status').innerText = 'Link inválido.';
      btn.disabled = false;
      return;
    }
    job = d.id;
    checar();
  } catch (e) {
    document.getElementById('status').innerText = 'Erro ao iniciar.';
    btn.disabled = false;
  }
}

async function checar() {
  if (!job) { return; }
  const r = await fetch('/status/' + job);
  const d = await r.json();
  document.getElementById('status').innerText = d.msg;
  document.getElementById('diag').innerText = d.diag || '-';
  const lista = document.getElementById('lista');
  if (d.clipes > 0) {
    lista.style.display = 'block';
    let h = '<a href="/zip/' + job + '">Baixar todos em ZIP (' + d.clipes + ' clipes)</a>';
    for (let i = 1; i <= d.clipes; i++) {
      h += '<a href="/clipe/' + job + '/' + i + '">Clipe ' + i + '</a>';
    }
    lista.innerHTML = h;
  }
  if (d.estado !== 'fim' && d.estado !== 'erro') {
    setTimeout(checar, 3000);
  } else {
    document.getElementById('btn-gravar').disabled = false;
  }
}
function plataforma(url) {
  try {
    const u = new URL(url);
    const host = u.hostname.replace('www.', '');
    const partes = u.pathname.split('/').filter(Boolean);
    if (host === 'kick.com' && partes[0]) {
      return {nome: 'Kick', embed: 'https://player.kick.com/' + partes[0] + '?autoplay=true&muted=true'};
    }
    if (host === 'twitch.tv' && partes[0]) {
      return {nome: 'Twitch', embed: 'https://player.twitch.tv/?channel=' + partes[0] + '&parent=' + location.hostname + '&muted=true'};
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
  if (p.embed !== ultimoEmbed) {
    document.getElementById('player').src = p.embed;
    ultimoEmbed = p.embed;
  }
}
document.getElementById('url').addEventListener('input', mostrarPreview);
const CANAIS = [
  {nome: 'Coringa', plat: 'kick', canal: 'coringa'},
  {nome: 'Gabepeixe', plat: 'kick', canal: 'gabepeixe'}
];

function embedDe(c) {
  if (c.plat === 'twitch') {
    return 'https://player.twitch.tv/?channel=' + c.canal + '&parent=' + location.hostname + '&muted=true';
  }
  return 'https://player.kick.com/' + c.canal + '?autoplay=true&muted=true';
}

function linkDe(c) {
  return (c.plat === 'twitch' ? 'https://twitch.tv/' : 'https://kick.com/') + c.canal;
}

function clipar(i) {
  document.getElementById('url').value = linkDe(CANAIS[i]);
  mostrarPreview();
  window.scrollTo(0, 0);
}

function montarLives() {
  let h = '';
  CANAIS.forEach(function (c, i) {
    h += '<div class="live-item"><div class="live-topo"><span><span class="ponto"></span>' + c.nome +
      '</span><button class="btn-mini" onclick="clipar(' + i + ')">Usar esta live</button></div>' +
      '<iframe class="player" src="' + embedDe(c) + '" allowfullscreen allow="autoplay; fullscreen"></iframe></div>';
  });
  document.getElementById('lives').innerHTML = h;
}
montarLives();

</script>
</body>
</html>
 
