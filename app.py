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
  }

  .card-live {
    display: none;
    background: var(--surface);
    border: 1px solid var(--borda-forte);
    border-radius: 20px;
    padding: 14px;
    margin-top: 14px;
  }

  .card-topo {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 10px;
    font-weight: 700;
  }

  .ponto {
    display: inline-block;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: #ff5a4d;
    margin-right: 8px;
  }

  .tempo {
    color: var(--mutado);
    font-variant-numeric: tabular-nums;
  }

  .player {
    width: 100%;
    aspect-ratio: 16 / 9;
    border: 0;
    border-radius: 12px;
    background: #000;
  }

  .player-aviso {
    margin: 8px 0 0;
    font-size: 0.76rem;
    color: var(--mutado);
  }

  .onda-box {
    position: relative;
    margin-top: 28px;
  }

  .rotulos {
    position: absolute;
    top: -20px;
    left: 0;
    right: 0;
    height: 18px;
  }

  .rotulos span {
    position: absolute;
    font-size: 0.75rem;
    font-weight: 700;
    color: var(--roxo-brilho);
  }

  .onda {
    display: flex;
    align-items: center;
    gap: 2px;
    height: 56px;
  }

  .onda i {
    flex: 1;
    border-radius: 2px;
    background: rgba(168, 130, 255, 0.28);
  }

  .onda i.on {
    background: var(--roxo-brilho);
  }

  .onda i.rec {
    background: var(--roxo);
    animation: piscar 1s infinite;
  }

  @keyframes piscar {
    50% { opacity: 0.35; }
  }
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

    <section class="card-live" id="card-live">
      <div class="card-topo">
        <span><span class="ponto"></span><span id="plat-nome">Live</span></span>
        <span class="tempo" id="tempo">AO VIVO</span>
      </div>
      <iframe id="player" class="player" allowfullscreen allow="autoplay; fullscreen"></iframe>
      <p class="player-aviso">Se o vídeo não aparecer aqui, a gravação ainda pode funcionar.</p>
      <div class="onda-box">
        <div class="rotulos" id="rotulos"></div>
        <div class="onda" id="onda"></div>
      </div>
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
let totalMin = 5;
let tIni = null;
let timerId = null;
let ultimoEmbed = "";
const NB = 60;

function montarOnda() {
  let h = "";
  for (let i = 0; i < NB; i++) {
    const alt = 20 + Math.abs(Math.sin(i * 1.7) * Math.cos(i * 0.6)) * 80;
    h += '<i style="height:' + alt.toFixed(0) + '%"></i>';
  }
  document.getElementById('onda').innerHTML = h;
}

function desenhar(clipes, gravando) {
  const bars = document.querySelectorAll('#onda i');
  const porClipe = NB / totalMin;
  const ate = Math.min(NB, Math.round(clipes * porClipe));
  const rec = gravando ? Math.min(NB, Math.round((clipes + 1) * porClipe)) : ate;
  bars.forEach(function (b, i) {
    b.className = i < ate ? 'on' : (i < rec ? 'rec' : '');
  });
  const passo = Math.max(1, Math.ceil(totalMin / 6));
  let r = '';
  for (let c = 1; c <= clipes && c <= totalMin; c++) {
    if (c === 1 || c % passo === 0) {
      r += '<span style="left:' + (((c - 1) / totalMin) * 100) + '%">#' + c + '</span>';
    }
  }
  document.getElementById('rotulos').innerHTML = r;
}

function fmt(s) {
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const x = s % 60;
  const mm = h > 0 ? h + ':' + String(m).padStart(2, '0') : String(m);
  return mm + ':' + String(x).padStart(2, '0');
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
    if (host === 'youtube.com' || host === 'm.youtube.com' || host === 'youtu.be') {
      let id = u.searchParams.get('v');
      if (!id && host === 'youtu.be') { id = partes[0]; }
      if (!id && partes[0] === 'live') { id = partes[1]; }
      if (id) {
        return {nome: 'YouTube', embed: 'https://www.youtube.com/embed/' + id + '?autoplay=1&mute=1'};
      }
    }
  } catch (e) {}
  return null;
}

function mostrarPreview() {
  const url = document.getElementById('url').value.trim();
  const card = document.getElementById('card-live');
  const p = plataforma(url);
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
    totalMin = parseInt(min);
    tIni = Date.now();
    if (timerId) { clearInterval(timerId); }
    timerId = setInterval(function () {
      document.getElementById('tempo').innerText = fmt(Math.floor((Date.now() - tIni) / 1000));
    }, 1000);
    desenhar(0, true);
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
  const terminou = (d.estado === 'fim' || d.estado === 'erro');
  desenhar(d.clipes, !terminou);
  if (!terminou) {
    setTimeout(checar, 3000);
  } else {
    if (timerId) { clearInterval(timerId); }
    document.getElementById('btn-gravar').disabled = false;
  }
}

montarOnda();
document.getElementById('url').addEventListener('input', mostrarPreview);
</script>
</body>
</html>
"""


def ler_fim(caminho, limite=700):
    try:
        with open(caminho, "r", errors="ignore") as f:
            texto = f.read()
        texto = texto.replace("\r", "\n")
        return texto[-limite:]
    except Exception:
        return ""


def tamanho_pasta(pasta):
    total = 0
    for f in os.listdir(pasta):
        if f.startswith("clipe_"):
            total += os.path.getsize(os.path.join(pasta, f))
    return total


def gravar(job_id, url, minutos):
    job = JOBS[job_id]
    pasta = os.path.join(BASE, job_id)
    os.makedirs(pasta, exist_ok=True)
    job["pasta"] = pasta
    padrao = os.path.join(pasta, "clipe_%03d.mp4")
    log_ffmpeg = os.path.join(pasta, "ffmpeg.log")

    job["estado"] = "abrindo"
    job["msg"] = "A abrir a live..."
    job["diag"] = "A executar yt-dlp..."
    cmd_url = ["yt-dlp", "-g", "-f", "best[height<=720]/best", url]
    try:
        r = subprocess.run(cmd_url, capture_output=True, text=True, timeout=90)
    except Exception as e:
        job["estado"] = "erro"
        job["msg"] = "yt-dlp demorou demasiado ou falhou."
        job["diag"] = str(e)[-500:]
        return
    links = [l for l in r.stdout.strip().split("\n") if l.startswith("http")]
    if not links:
        job["estado"] = "erro"
        job["
