"""Clipador de lives - Kick.

Desenvolvimento:
    pip install -r requirements.txt
    python app.py

Produção (use 1 worker: os jobs ficam em memória, então vários workers não se enxergam):
    gunicorn app:app --workers 1 --threads 8 --timeout 120

Arquivo único: o HTML da página está embutido na variável HTML.
Precisa do ffmpeg (com ffprobe) instalado no sistema. Variáveis de ambiente opcionais: veja o LEIAME.md.
"""
import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html import escape
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from flask import Flask, Response, jsonify, request, send_file

try:
    from curl_cffi import requests as curl_requests  # passa melhor na Cloudflare
except ImportError:
    curl_requests = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("clipador")


# ============================== CONFIGURAÇÃO ==============================

def _env_int(nome, padrao):
    try:
        return int(os.environ.get(nome, padrao))
    except ValueError:
        return padrao


HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("CLIPADOR_DIR") or os.path.join(tempfile.gettempdir(), "clipador")
os.makedirs(BASE, exist_ok=True)

SEG = 60                                                    # duração padrão do clipe (s)
MAX_CAPTURA_MIN = 60                                        # máximo de captura ao vivo
MAX_SIMULTANEOS = _env_int("CLIPADOR_MAX_SIMULTANEOS", 3)   # ffmpeg/análises rodando ao mesmo tempo
MAX_ATIVOS_POR_IP = _env_int("CLIPADOR_MAX_POR_IP", 2)      # tarefas ativas por visitante
MAX_ATIVOS_TOTAL = _env_int("CLIPADOR_MAX_FILA", 20)        # tarefas ativas no servidor todo (fila inclusa)
VALIDADE_S = _env_int("CLIPADOR_VALIDADE_MIN", 120) * 60    # quanto tempo os arquivos ficam no servidor
CONTATO = os.environ.get("CLIPADOR_CONTATO", "").strip()    # e-mail para pedidos de remoção (aparece nos termos)

HOSTS_KICK = {"kick.com", "kzl.ink"}
ESTADOS_FINAIS = ("fim", "erro")
FORMATOS = ("original", "vertical_blur", "vertical_crop")
RE_SLUG = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
RE_PASTA_JOB = re.compile(r"^[0-9a-f]{8}$")

# Canais exibidos em "Lives agora na Kick". Edite aqui: o site pega esta lista automaticamente.
CANAIS = [
    {"nome": "Bak", "canal": "bak"},
    {"nome": "Renato Vicente", "canal": "orenatovicente"},
    {"nome": "Brabox", "canal": "brabox"},
    {"nome": "Gabe Peixe", "canal": "gabepeixe"},
    {"nome": "Coringa", "canal": "coringa"},
]

# vídeo vertical: 720x1280 por padrão (leve). Se o servidor tiver folga de memória, CLIPADOR_VERTICAL_ALTURA=1920 usa 1080x1920.
VERT_H, VERT_W = (1920, 1080) if _env_int("CLIPADOR_VERTICAL_ALTURA", 1280) >= 1600 else (1280, 720)
# O ffmpeg em container enxerga todos os núcleos do servidor e abre threads demais (isso estoura a memória). Limitamos.
THREADS_FFMPEG = max(1, _env_int("CLIPADOR_FFMPEG_THREADS", 2))

UA_NAV = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

app = Flask(__name__)
if os.environ.get("CLIPADOR_PROXY", "1") == "1":
    # atrás de proxy (Render, Railway, nginx...) o IP real vem em X-Forwarded-For.
    # Se rodar direto na internet sem proxy, use CLIPADOR_PROXY=0.
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


HTML = r"""
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
    border: 1px solid rgba(37, 244, 238, 0.4);
  }
  .logo-box svg { width: 26px; height: 26px; }
  .wrap { max-width: 540px; margin: 0 auto; padding: 18px 16px 0; }
  .hero-card {
    background: var(--surface); border: 1px solid var(--borda-forte);
    border-radius: 20px; padding: 20px 18px;
    box-shadow: 0 24px 60px rgba(0, 0, 0, 0.6);
  }
  .hero-titulo {
    margin: 0; font-size: 1.45rem; font-weight: 800;
    letter-spacing: -0.02em; line-height: 1.2;
    background: var(--grad); -webkit-background-clip: text; background-clip: text;
    color: transparent; -webkit-text-fill-color: transparent;
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
    background: var(--grad); color: #0d0a16; cursor: pointer;
    box-shadow: 0 10px 26px rgba(0, 255, 56, 0.35);
    transition: transform 0.15s ease, filter 0.2s ease;
  }
  .btn-principal:hover { filter: brightness(1.1); transform: translateY(-1px); }
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

  /* lista de clipes, player e gerar mais */
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
  button:focus-visible, a:focus-visible { outline: 2px solid var(--tt-azul); outline-offset: 2px; }


  /* ===== Redesign mobile: inspirado na referência enviada ===== */
  :root {
    --bg: #08090e;
    --surface: #14141d;
    --surface-2: #1b1b27;
    --input: #171923;
    --borda: rgba(180, 184, 210, .20);
    --borda-forte: rgba(180, 184, 210, .30);
    --kick-verde: #58e879;
    --kick-brilho: #76f59a;
    --texto: #f7f7fb;
    --mutado: #aaaebe;
    --tt-rosa: #ff3264;
    --tt-azul: #29dce8;
    --grad: linear-gradient(110deg, #27dfe5 0%, #34d9ca 45%, #ff3264 100%);
  }
  body {
    background:
      radial-gradient(ellipse at 10% 0%, rgba(44, 225, 151, .09), transparent 42%),
      radial-gradient(ellipse at 95% 12%, rgba(255, 50, 100, .08), transparent 40%),
      #08090e;
  }
  .topbar { background: rgba(8,9,14,.92); border: 0; }
  .topbar-inner { max-width: 560px; padding: 20px 18px 10px; }
  .brand { font-size: 1.55rem; letter-spacing: -.045em; line-height: 1.05; }
  .logo-box { display:none; }
  .brand span:last-child::after {
    content: ' • de lives'; display: block; color: var(--kick-verde);
    font-size: .88rem; letter-spacing: 0; margin-top: 3px;
  }
  .wrap { max-width: 560px; padding: 18px 16px 30px; }
  .hero-card {
    display: grid; grid-template-columns: minmax(0,1fr) minmax(0,1fr);
    column-gap: 12px; row-gap: 0; padding: 22px 18px;
    border: 1px solid rgba(190,190,220,.25); border-radius: 17px;
    background: linear-gradient(145deg, rgba(26,26,38,.98), rgba(17,17,26,.98));
    box-shadow: 0 18px 48px rgba(0,0,0,.34);
  }
  .hero-titulo, .hero-sub, .hero-card > .campo:nth-of-type(1),
  .hero-card > .campo:nth-of-type(2), .hero-card > .campo:nth-of-type(5),
  .hero-card > .campo:nth-of-type(6), .hero-card > .btn-principal { grid-column: 1 / -1; }
  .hero-titulo { font-size: clamp(1.7rem, 7vw, 2.2rem); line-height: 1.12; }
  .hero-sub { color: #d1d2dc; font-size: .98rem; margin: 10px 0 18px; }
  .campo { min-width: 0; margin-bottom: 13px; }
  label { color: #f0f0f5; font-size: .88rem; margin-bottom: 8px; }
  input, select {
    border-radius: 11px; min-height: 50px; background-color: #151923;
    border-color: rgba(118,245,154,.55); font-size: .95rem;
  }
  #formato { border-color: rgba(41,220,232,.7); }
  .btn-principal {
    min-height: 56px; margin-top: 3px; border-radius: 12px;
    font-size: 1.02rem; box-shadow: 0 9px 24px rgba(255,50,100,.14);
  }
  .caixa, .card-live {
    background: rgba(22,22,32,.96); border: 1px solid rgba(180,184,210,.22);
    border-radius: 14px; padding: 15px 16px; margin-top: 14px;
  }
  #status { position: relative; padding-left: 38px; min-height: 54px; }
  #status::before {
    content: ''; position: absolute; left: 16px; top: 19px; width: 10px; height: 10px;
    border-radius: 50%; background: var(--kick-verde); box-shadow: 0 0 12px rgba(88,232,121,.6);
  }
  .diag-topo { color: #f0f0f5; font-size: .84rem; }
  pre { color: #d0d3df; }
  #lives-box { overflow: hidden; }
  #lives-box > .diag-topo { display:block; font-size: 1rem; margin-bottom: 12px; }
  #lives {
    display: grid; grid-auto-flow: column; grid-auto-columns: minmax(220px, 78%);
    grid-template-rows: auto; gap: 12px; overflow-x: auto; padding: 2px 2px 12px;
    scroll-snap-type: x mandatory; scrollbar-width: thin;
  }
  .live-item {
    min-width: 0; margin: 0; padding: 8px; border: 1px solid rgba(88,232,121,.42);
    border-radius: 13px; background: #10131a; scroll-snap-align: start;
  }
  .live-topo { display:flex; flex-direction:column; align-items:stretch; gap:8px; margin: 0 0 8px; }
  .live-topo > span { line-height: 1.35; }
  .live-topo .btn-mini { width:100%; min-height: 38px; background: transparent; color: var(--kick-verde); border:1px solid var(--kick-verde); }
  .placeholder, .player { border-radius: 9px; border: 1px solid rgba(88,232,121,.75); }
  .placeholder { aspect-ratio: 16/9; }
  .placeholder .play { width:42px; height:42px; font-size:1.1rem; }
  .viewers { display:block; margin:4px 0 0; }
  .ponto { width:9px; height:9px; background:var(--kick-verde); box-shadow:0 0 9px rgba(88,232,121,.6); }
  .ponto.off { background:#777; box-shadow:none; }
  .clipe-item { background:#171923; border-color:rgba(180,184,210,.2); }
  .zip-link { background:rgba(88,232,121,.12); border-color:var(--kick-verde); color:var(--kick-brilho); }
  .aviso { color:#9298a9; }
  @media (max-width: 390px) {
    .hero-card { padding:19px 14px; column-gap:9px; }
    .hero-titulo { font-size:1.65rem; }
    input, select { padding:12px 10px; font-size:.88rem; }
    #lives { grid-auto-columns: 86%; }
  }
  @media (min-width: 700px) {
    .wrap, .topbar-inner { max-width: 680px; }
    #lives { grid-auto-columns: 245px; }
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

      <!-- formato do vídeo -->
      <div class="campo">
        <label for="formato">Formato do vídeo</label>
        <select id="formato">
          <option value="original" selected>Original (horizontal, mais rápido)</option>
          <option value="vertical_blur">Vertical 9:16 com fundo desfocado</option>
          <option value="vertical_crop">Vertical 9:16 com recorte central</option>
        </select>
        <p class="player-aviso">Vertical reencoda o vídeo: na live ao vivo o servidor grava primeiro e converte clipe por clipe depois, então demora mais pra ficar pronto. No formato original o corte é feito no keyframe mais próximo, então o início pode variar alguns segundos.</p>
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
      <iframe id="player" class="player" title="Prévia da live" allowfullscreen allow="autoplay; fullscreen"></iframe>
      <p class="player-aviso" id="player-aviso">Se o vídeo não aparecer, a gravação ainda pode funcionar.</p>
    </section>

    <div class="caixa" id="status" role="status" aria-live="polite">Aguardando link.</div>
    <div class="caixa" id="lista" style="display:none"></div>

    <!-- gerar mais clipes -->
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
    <p class="rodape"><a href="#" onclick="abrirTermos(); return false;">Termos de uso</a></p>
  </main>

  <div class="modal-fundo" id="termos" role="dialog" aria-modal="true" aria-labelledby="termos-titulo" onclick="if(event.target===this){fecharTermos()}">
    <div class="modal">
      <h2 id="termos-titulo">Termos de uso</h2>
      <p>1. Use o Clipador de lives apenas com autorização do streamer ou em transmissões suas.</p>
      <p>2. O conteúdo das lives e dos clipes continua sendo do criador original. Este site não reivindica direitos sobre ele.</p>
      <p>3. Este site não tem ligação com a Kick nem com nenhum streamer. Kick é marca de seus respectivos donos.</p>
      <p>4. Os clipes são gerados de forma automática e podem conter erros ou ficar fora do ponto esperado.</p>
      <p>5. Você é o único responsável pelo uso e pela publicação dos clipes, inclusive em outras plataformas.</p>
      <p>6. Os arquivos ficam no servidor só por um tempo e podem ser apagados a qualquer momento. Baixe o que quiser guardar.</p>
      <p>7. O serviço é oferecido como está, sem garantia de funcionamento ou disponibilidade.</p>
      <p>8. Se o dono de um conteúdo pedir a remoção, o pedido será atendido.<!--CONTATO--></p>
      <button class="btn-mini" onclick="fecharTermos()">Fechar</button>
    </div>
  </div>

<script>
let job = null;
let timerChecar = null;

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

function abrirTermos() {
  document.getElementById('termos').style.display = 'flex';
  document.querySelector('#termos .btn-mini').focus();
}
function fecharTermos() { document.getElementById('termos').style.display = 'none'; }
document.addEventListener('keydown', function (e) { if (e.key === 'Escape') { fecharTermos(); } });

function esc(s) {
  return String(s).replace(/[&<>"']/g, function (c) {
    return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
  });
}

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

const CANAIS = /*__CANAIS__*/ [];

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
  return '<iframe class="player" title="Live ao vivo" src="' + embedDe(c) + '" allowfullscreen allow="autoplay; fullscreen"></iframe>';
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
      h += '<div class="live-item"><div class="live-topo"><span><span class="ponto off"></span>' + esc(c.nome) +
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
    h += '<div class="live-item"><div class="live-topo"><span><span class="ponto"></span>' + esc(c.nome) + info +
      '</span><button class="btn-mini" onclick="clipar(' + idx + ')">Usar esta live</button></div>' +
      '<div id="slot-' + c.canal + '">' + corpo + '</div></div>';
  });
  document.getElementById('lives').innerHTML = h;
}
montarLives(CANAIS.map(function (c) { return {canal: c.canal, nome: c.nome, viewers: null}; }));

async function filtrarOnline() {
  if (document.hidden) { return; }
  try {
    const r = await fetch('/api/lives');
    if (!r.ok) { return; }
    const dados = await r.json();
    const resultados = dados.canais || [];

    resultados.forEach(function (x) { estadoCanal[x.canal] = x.status; });
    atualizarStatusPreview();

    const online = resultados.filter(function (x) { return x.status === 'online'; });
    const offlineLista = resultados.filter(function (x) { return x.status === 'offline'; });

    if (online.length > 0 || offlineLista.length > 0) {
      online.sort(function (a, b) { return (b.viewers || 0) - (a.viewers || 0); });
      montarLives(online.concat(offlineLista).map(function (x) {
        return {canal: x.canal, nome: x.nome, viewers: x.viewers, offline: x.status === 'offline'};
      }));
    }
  } catch (e) {}
}
filtrarOnline();
setInterval(filtrarOnline, 30000);
document.addEventListener('visibilitychange', function () { if (!document.hidden) { filtrarOnline(); } });


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
    if (!r.ok) {
      document.getElementById('status').innerText = d.erro || 'Erro ao testar.';
      return;
    }
    document.getElementById('diag').innerText = JSON.stringify(d, null, 2);
    const ok = d.lista_vods && d.lista_vods.ok && d.chat && d.chat.ok;
    document.getElementById('status').innerText = ok
      ? 'Funcionou: o servidor lê as lives antigas e o chat.'
      : 'Não deu: veja o diagnóstico abaixo.';
  } catch (e) {
    document.getElementById('status').innerText = 'Erro ao testar.';
  }
}

// envia o formato e zera a lista e o bloco de mais clipes
async function iniciar() {
  const url = document.getElementById('url').value.trim();
  const min = document.getElementById('min').value;
  const seg = document.getElementById('seg').value;
  const formato = document.getElementById('formato').value;
  const btn = document.getElementById('btn-gravar');
  if (!url) { return; }
  btn.disabled = true;
  clearTimeout(timerChecar);
  document.getElementById('lista').dataset.job = '';
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
    checar();
  } catch (e) {
    document.getElementById('status').innerText = 'Erro ao iniciar.';
    btn.disabled = false;
  }
}

// H:MM:SS no navegador
function fmtTempo(s) {
  s = Math.max(0, Math.floor(s));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const mm = String(m).padStart(2, '0');
  const ss = String(sec).padStart(2, '0');
  return h > 0 ? h + ':' + mm + ':' + ss : m + ':' + ss;
}

// lista incremental: só acrescenta clipes novos, sem apagar o vídeo que a pessoa está assistindo
function criarItemClipe(i, jobId) {
  const el = document.createElement('div');
  el.className = 'clipe-item';
  el.id = 'clipe-' + i;
  el.innerHTML =
    '<div class="clipe-topo"><div><div class="clipe-nome">Clipe ' + i + '</div>' +
    '<div class="clipe-tempo"></div></div>' +
    '<div class="clipe-acoes">' +
    '<button type="button" class="sec" onclick="assistir(' + i + ')">Assistir</button>' +
    '<a href="/clipe/' + jobId + '/' + i + '">Baixar</a>' +
    '</div></div><div id="video-' + i + '"></div>';
  return el;
}

function atualizarListaClipes(clipesQtd, jobId, info) {
  const lista = document.getElementById('lista');
  if (clipesQtd <= 0) {
    lista.style.display = 'none';
    return;
  }
  lista.style.display = 'block';
  if (lista.dataset.job !== jobId) {
    lista.innerHTML = '<a class="zip-link" id="zip-link"></a><div id="clipes-itens"></div>';
    lista.dataset.job = jobId;
  }
  const zip = document.getElementById('zip-link');
  zip.href = '/zip/' + jobId;
  zip.textContent = 'Baixar todos em ZIP (' + clipesQtd + ' clipes)';

  const itens = document.getElementById('clipes-itens');
  const porNumero = {};
  (info || []).forEach(function (x) { porNumero[x.n] = x; });

  for (let i = 1; i <= clipesQtd; i++) {
    let el = document.getElementById('clipe-' + i);
    if (!el) {
      el = criarItemClipe(i, jobId);
      itens.appendChild(el);
    }
    const x = porNumero[i];
    if (x) {
      el.querySelector('.clipe-tempo').textContent =
        fmtTempo(x.ini) + ' até ' + fmtTempo(x.fim) + ' da live · ' + x.msgs + ' msgs no chat';
    }
  }
}

// pré-visualização do clipe na própria página
function assistir(n) {
  const alvo = document.getElementById('video-' + n);
  if (!alvo || !job) { return; }
  if (alvo.innerHTML) {
    alvo.innerHTML = '';
    return;
  }
  alvo.innerHTML = '<video class="clipe-video" controls autoplay playsinline src="/ver/' + job + '/' + n + '"></video>';
}

// mostra o bloco de gerar mais quando termina
async function checar() {
  if (!job) { return; }
  const meuJob = job;
  try {
    const r = await fetch('/status/' + meuJob);
    const d = await r.json();
    if (meuJob !== job) { return; }  // a pessoa iniciou outra tarefa enquanto isso
    document.getElementById('status').innerText = d.msg || '';
    document.getElementById('diag').innerText = d.diag || '-';
    atualizarListaClipes(Number(d.clipes || 0), meuJob, d.info || []);

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
      timerChecar = setTimeout(checar, 3000);
    } else {
      document.getElementById('btn-gravar').disabled = false;
      document.getElementById('btn-mais').disabled = false;
    }
  } catch (e) {
    if (meuJob === job) { timerChecar = setTimeout(checar, 3000); }
  }
}

// pede mais clipes do mesmo job
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
    clearTimeout(timerChecar);
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


def _montar_pagina():
    html = HTML
    canais_json = json.dumps(CANAIS, ensure_ascii=False).replace("</", "<\\/")
    html = html.replace("/*__CANAIS__*/ []", canais_json)
    contato = ""
    if CONTATO:
        c = escape(CONTATO, quote=True)
        contato = ' Contato: <a href="mailto:%s">%s</a>.' % (c, c)
    return html.replace("<!--CONTATO-->", contato)


PAGINA = _montar_pagina()

JOBS = {}
JOBS_LOCK = threading.Lock()
VAGAS = threading.BoundedSemaphore(MAX_SIMULTANEOS)


@app.after_request
def cabecalhos(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    return resp


# ============================== UTILIDADES ==============================

def ler_fim(caminho, limite=700):
    try:
        if not os.path.exists(caminho):
            return ""
        with open(caminho, "r", errors="ignore") as f:
            texto = f.read()
        return texto.replace("\r", "\n")[-limite:]
    except Exception:
        return ""


def listar_clipes(pasta, prefixo="clipe_"):
    if not pasta or not os.path.isdir(pasta):
        return []
    arquivos = []
    for f in sorted(os.listdir(pasta)):
        if f.startswith(prefixo) and f.endswith(".mp4"):
            caminho = os.path.join(pasta, f)
            if os.path.isfile(caminho) and os.path.getsize(caminho) > 0:
                arquivos.append(f)
    return arquivos


def tamanho_pasta(pasta, prefixo="clipe_"):
    return sum(os.path.getsize(os.path.join(pasta, f)) for f in listar_clipes(pasta, prefixo))


def clipe_valido(caminho, minimo_s=1.0):
    """False para MP4 corrompido (sem moov, sem vídeo, quase vazio)."""
    try:
        if os.path.getsize(caminho) < 2048:
            return False
    except OSError:
        return False
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_type:format=duration", "-of", "json", caminho],
            capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return True  # sem ffprobe só vale o teste de tamanho
    except subprocess.TimeoutExpired:
        return False
    if r.returncode != 0:
        return False
    try:
        d = json.loads(r.stdout or "{}")
        return bool(d.get("streams")) and float((d.get("format") or {}).get("duration") or 0) >= minimo_s
    except (ValueError, TypeError):
        return False


def resumo_erro(texto, codigo=None, limite=400):
    """Resume o erro do ffmpeg: código de saída + linhas que parecem erro (ou o final do log)."""
    partes = []
    if codigo is not None and codigo < 0:
        partes.append("ffmpeg foi encerrado pelo sistema (sinal %d) - provável falta de memória ou CPU no servidor." % -codigo)
    elif codigo:
        partes.append("ffmpeg terminou com código %d." % codigo)
    linhas = [l.strip() for l in (texto or "").replace("\r", "\n").splitlines() if l.strip()]
    suspeitas = [l for l in linhas if re.search(r"error|invalid|fail|unable|unknown|no such|too many|timed out|denied", l, re.I)]
    partes.append("\n".join((suspeitas or linhas)[-4:]))
    return "\n".join(p for p in partes if p)[-limite:]


def fmt_tempo(s):
    """Segundos -> H:MM:SS (ou M:SS)."""
    s = int(max(0, s))
    h, resto = divmod(s, 3600)
    m, sec = divmod(resto, 60)
    if h:
        return "%d:%02d:%02d" % (h, m, sec)
    return "%d:%02d" % (m, sec)


def intervalo_clipe(ini, tamanho):
    return "%s até %s" % (fmt_tempo(ini), fmt_tempo(ini + tamanho))


def ler_numero(valor, padrao, tipo=int):
    try:
        n = tipo(valor)
    except (TypeError, ValueError, OverflowError):
        return padrao
    if tipo is float and not math.isfinite(n):
        return padrao
    return n


def ler_seg(dados):
    seg = ler_numero(dados.get("seg", SEG), SEG)
    return seg if seg in (30, 60) else SEG


def ler_formato(dados):
    formato = str(dados.get("formato", "original"))
    return formato if formato in FORMATOS else "original"


def ip_cliente():
    return request.remote_addr or "?"


def url_permitida(url):
    """Só aceita links http(s) da Kick. Evita que o servidor seja usado pra baixar de qualquer lugar (SSRF)."""
    try:
        u = urlparse(url)
        host = (u.hostname or "").lower()
    except ValueError:
        return False
    if host.startswith("www."):
        host = host[4:]
    return u.scheme in ("http", "https") and host in HOSTS_KICK


def slug_da_url(url):
    if not url_permitida(url):
        return ""
    partes = [p for p in urlparse(url).path.split("/") if p]
    if partes and RE_SLUG.match(partes[0]):
        return partes[0]
    return ""


# ============================== JOBS ==============================

def atualizar_job(job_id, **campos):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if j is not None:
            j.update(campos)
            j["atualizado"] = time.time()


def job_ativo(j):
    return j["estado"] not in ESTADOS_FINAIS


def _checar_limites_locked(ip):
    """Chamar com JOBS_LOCK. Retorna mensagem de erro ou ''."""
    ativos = [j for j in JOBS.values() if job_ativo(j)]
    if len(ativos) >= MAX_ATIVOS_TOTAL:
        return "O servidor está cheio agora. Tente de novo em alguns minutos."
    if sum(1 for j in ativos if j.get("ip") == ip) >= MAX_ATIVOS_POR_IP:
        return "Você já tem %d tarefas em andamento. Espere uma terminar." % MAX_ATIVOS_POR_IP
    return ""


def criar_job(**extra):
    """Cria o job se couber nos limites. Retorna (job_id, erro)."""
    ip = ip_cliente()
    with JOBS_LOCK:
        erro = _checar_limites_locked(ip)
        if erro:
            return None, erro
        job_id = uuid.uuid4().hex[:8]
        while job_id in JOBS:
            job_id = uuid.uuid4().hex[:8]
        job = {
            "estado": "iniciando", "msg": "Iniciando...", "clipes": 0, "diag": "", "pasta": "",
            "info": [], "picos": [], "usados": 0, "stream": "", "headers": {},
            "tamanho_clipe": SEG, "formato": "original",
            "ip": ip, "atualizado": time.time(),
        }
        job.update(extra)
        JOBS[job_id] = job
    return job_id, ""


def rodar_job(job_id, alvo, *args):
    """Espera uma vaga, roda o trabalho e garante que o job nunca fique preso em 'gravando'."""
    atualizar_job(job_id, estado="fila", msg="Na fila... esperando uma vaga.")
    try:
        with VAGAS:
            alvo(job_id, *args)
    except Exception as e:
        log.exception("job %s falhou", job_id)
        atualizar_job(job_id, estado="erro", msg="Erro inesperado no servidor.", diag=str(e)[-300:])


def iniciar_thread(job_id, alvo, *args):
    threading.Thread(target=rodar_job, args=(job_id, alvo) + args, daemon=True).start()


def dados_do_job(job_id):
    """(pasta, clipes prontos) e renova a validade dos arquivos."""
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            return "", 0
        j["atualizado"] = time.time()
        return j.get("pasta") or "", int(j.get("clipes") or 0)


# ============================== LIMPEZA ==============================

def _apagar_pasta_job(pasta):
    """Só apaga pastas de job dentro de BASE (nunca outro lugar)."""
    p = os.path.abspath(pasta or "")
    if os.path.dirname(p) == os.path.abspath(BASE) and RE_PASTA_JOB.match(os.path.basename(p)):
        shutil.rmtree(p, ignore_errors=True)


def _remover_arquivo(caminho):
    try:
        os.remove(caminho)
    except OSError:
        pass


def limpar_antigos():
    agora = time.time()
    pastas_velhas = []
    with JOBS_LOCK:
        for job_id, j in list(JOBS.items()):
            parado = agora - j.get("atualizado", agora)
            if job_ativo(j) and parado > 3 * 3600:
                # nada se mexeu em 3h: travou
                j.update(estado="erro", msg="A tarefa travou e foi cancelada.", atualizado=agora)
            elif not job_ativo(j) and parado > VALIDADE_S:
                pastas_velhas.append(j.get("pasta"))
                del JOBS[job_id]
        conhecidos = set(JOBS)
    for pasta in pastas_velhas:
        _apagar_pasta_job(pasta)

    # sobras: pastas de jobs de antes de um reinício e zips temporários esquecidos
    try:
        for nome in os.listdir(BASE):
            caminho = os.path.join(BASE, nome)
            idade = agora - os.path.getmtime(caminho)
            if nome in conhecidos:
                continue
            if RE_PASTA_JOB.match(nome) and os.path.isdir(caminho) and idade > VALIDADE_S:
                _apagar_pasta_job(caminho)
            elif nome.endswith(".zip") and os.path.isfile(caminho) and idade > 3600:
                _remover_arquivo(caminho)
    except OSError:
        pass


def _loop_limpeza():
    while True:
        time.sleep(600)
        try:
            limpar_antigos()
        except Exception:
            log.exception("a limpeza falhou")


# ============================== YT-DLP / FFMPEG ==============================

def extrair_stream(url):
    cmd_json = ["yt-dlp", "--no-warnings", "--no-playlist", "-j", "-f", "best[height<=720]/best", url]
    r = subprocess.run(cmd_json, capture_output=True, text=True, timeout=90)
    if r.returncode == 0 and r.stdout.strip():
        for linha in r.stdout.strip().splitlines():
            try:
                info = json.loads(linha)
                stream_url = info.get("url")
                if stream_url:
                    return stream_url, info.get("http_headers") or {}, ""
            except json.JSONDecodeError:
                continue

    cmd_url = ["yt-dlp", "--no-warnings", "--no-playlist", "-g", "-f", "best[height<=720]/best", url]
    r2 = subprocess.run(cmd_url, capture_output=True, text=True, timeout=90)
    links = [l.strip() for l in r2.stdout.strip().splitlines() if l.strip().startswith("http")]
    if links:
        return links[0], {}, ""

    erro_msg = (r.stderr or r2.stderr or "sem mensagem").strip()
    return None, {}, erro_msg[-700:]


def args_formato(formato, leve=False):
    """Argumentos de vídeo do ffmpeg. 'original' só copia; os verticais reencodam em 9:16 a 30 fps.
    leve=True: 540x960, preset ultrafast e 1 thread (plano B quando o servidor não aguenta)."""
    if formato not in ("vertical_blur", "vertical_crop"):
        return ["-c", "copy"]
    w, h = (540, 960) if leve else (VERT_W, VERT_H)
    threads = 1 if leve else THREADS_FFMPEG
    codec = ["-c:v", "libx264", "-preset", "ultrafast" if leve else "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
             "-threads", str(threads), "-filter_threads", "1", "-filter_complex_threads", "1"]
    if formato == "vertical_blur":
        pw, ph = w // 5, h // 5  # o fundo é desfocado em miniatura e depois ampliado: muito mais leve
        filtro = (
            "fps=30,split=2[fundo][frente];"
            "[fundo]scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,boxblur=4:1,"
            "scale=%d:%d:flags=bilinear[bg];"
            "[frente]scale=%d:-2[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p"
        ) % (pw, ph, pw, ph, w, h, w)
        return ["-filter_complex", filtro, *codec]
    filtro = "fps=30,crop=ih*9/16:ih,scale=%d:%d,setsar=1,format=yuv420p" % (w, h)
    return ["-vf", filtro, *codec]


def converter_vertical(entrada, saida, formato):
    """Converte um trecho já gravado para vertical (fora do tempo real, então não depende da velocidade do servidor).
    Se o ffmpeg for morto por falta de memória, tenta de novo em qualidade reduzida."""
    erro = ""
    for leve in (False, True):
        threads = 1 if leve else THREADS_FFMPEG
        cmd = ["ffmpeg", "-nostdin", "-y", "-threads", str(threads), "-i", entrada,
               *args_formato(formato, leve), saida]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        except subprocess.TimeoutExpired:
            _remover_arquivo(saida)
            erro = "demorou demais (mais de 15 min)"
            continue
        if r.returncode == 0 and clipe_valido(saida):
            return True, ""
        _remover_arquivo(saida)
        erro = resumo_erro(r.stderr, r.returncode) or "o arquivo convertido saiu corrompido"
    return False, erro + "\n(tentei também em qualidade reduzida)"


# sem "file": o ffmpeg só precisa de rede aqui (um m3u8 malicioso não consegue ler arquivos do servidor)
PROTOCOLOS = "http,https,tcp,tls,crypto"


def cabecalhos_ffmpeg(headers):
    user_agent = headers.get("User-Agent", UA_NAV)
    hdr = "".join("%s: %s\r\n" % (k, v) for k, v in headers.items()
                  if k.lower() != "user-agent" and v)
    return user_agent, (["-headers", hdr] if hdr else [])


def gravar(job_id, url, minutos, seg=SEG, formato="original"):
    """Captura uma live ao vivo e divide em clipes.
    Sempre grava com -c copy (leve, aguenta tempo real em qualquer servidor). Nos formatos verticais,
    os trechos gravados são convertidos um a um depois da captura, em vez de reencodar ao vivo."""
    pasta = os.path.join(BASE, job_id)
    os.makedirs(pasta, exist_ok=True)
    atualizar_job(job_id, pasta=pasta, estado="abrindo", msg="Abrindo a live...", diag="Rodando yt-dlp...")

    vertical = formato != "original"
    prefixo = "bruto_" if vertical else "clipe_"
    padrao = os.path.join(pasta, prefixo + "%03d.mp4")
    log_ffmpeg = os.path.join(pasta, "ffmpeg.log")

    try:
        stream, headers, erro_yt = extrair_stream(url)
    except subprocess.TimeoutExpired:
        atualizar_job(job_id, estado="erro", msg="O yt-dlp demorou demais para responder.",
                      diag="Timeout ao buscar o stream da live.")
        return
    except Exception as e:
        atualizar_job(job_id, estado="erro", msg="Falha ao executar o yt-dlp.", diag=str(e)[-500:])
        return

    if not stream:
        atualizar_job(job_id, estado="erro", msg="O yt-dlp não conseguiu o link da live.",
                      diag=erro_yt or "sem mensagem")
        return

    user_agent, extra_headers = cabecalhos_ffmpeg(headers)
    atualizar_job(job_id, estado="gravando", msg="Gravando... 0 clipes prontos",
                  diag="yt-dlp OK. Iniciando ffmpeg...")

    cmd = [
        "ffmpeg", "-nostdin", "-y",
        "-user_agent", user_agent,
        *extra_headers,
        "-protocol_whitelist", PROTOCOLOS,
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-rw_timeout", "15000000",
        "-i", stream,
        "-t", str(int(minutos * 60)),
        "-c", "copy",
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
                feitos = listar_clipes(pasta, prefixo)
                # o último arquivo ainda está sendo gravado; só os anteriores estão fechados
                fechados = max(0, len(feitos) - 1)
                tam = tamanho_pasta(pasta, prefixo)

                atualizar_job(
                    job_id,
                    clipes=0 if vertical else fechados,  # no vertical os clipes só aparecem depois de convertidos
                    msg=("Gravando... %d trechos capturados" if vertical else "Gravando... %d clipes prontos") % fechados,
                    diag="Tempo: %ds | Gravado: %d KB\n" % (time.time() - inicio, tam // 1024)
                         + ler_fim(log_ffmpeg, 500),
                )

                if time.time() - inicio > 60 and tam == 0:
                    proc.kill()
                    proc.wait()
                    break

                time.sleep(3)
    except Exception as e:
        if proc and proc.poll() is None:
            proc.kill()
        atualizar_job(job_id, estado="erro", msg="Erro durante a gravação com ffmpeg.", diag=str(e)[-500:])
        return

    codigo = proc.returncode
    brutos = listar_clipes(pasta, prefixo)
    if not brutos or tamanho_pasta(pasta, prefixo) == 0:
        atualizar_job(
            job_id, estado="erro", clipes=0,
            msg="Nenhum dado chegou em 60 segundos ou a live não está ativa.",
            diag=resumo_erro(ler_fim(log_ffmpeg, 20000), codigo, 800) or "Sem saída do ffmpeg.",
        )
        return

    notas = []
    if vertical:
        convertidos = 0
        for i, nome in enumerate(brutos):
            atualizar_job(job_id, estado="convertendo", clipes=convertidos,
                          msg="Convertendo para vertical... %d de %d" % (i + 1, len(brutos)),
                          diag="\n".join(notas)[-600:])
            entrada = os.path.join(pasta, nome)
            saida = os.path.join(pasta, "clipe_%03d.mp4" % convertidos)
            ok, nota = converter_vertical(entrada, saida, formato)
            _remover_arquivo(entrada)
            if ok:
                convertidos += 1
            else:
                notas.append("Trecho %d falhou: %s" % (i + 1, nota))
            atualizar_job(job_id, clipes=convertidos)
        total = convertidos
    else:
        # se o ffmpeg foi encerrado no meio, o último arquivo fica incompleto: descarta
        finais = listar_clipes(pasta)
        while finais and not clipe_valido(os.path.join(pasta, finais[-1])):
            _remover_arquivo(os.path.join(pasta, finais.pop()))
            notas.append("Um clipe incompleto no final foi descartado.")
        total = len(finais)

    if total > 0:
        atualizar_job(job_id, estado="fim", clipes=total, msg="Pronto! %d clipes gerados." % total,
                      diag=("\n".join(notas) + "\n" if notas else "") + "Total gravado: %d KB" % (tamanho_pasta(pasta) // 1024))
    else:
        atualizar_job(job_id, estado="erro", clipes=0, msg="Nenhum clipe saiu inteiro.",
                      diag=("\n".join(notas) + "\n" if notas else "") + resumo_erro(ler_fim(log_ffmpeg, 20000), codigo, 400))


# ============================== LIVES ANTIGAS (VOD) ==============================

def kick_get_json(url, timeout=20):
    """GET em JSON na Kick. Retorna (dados, erro)."""
    try:
        if curl_requests is not None:
            r = curl_requests.get(url, impersonate="chrome", timeout=timeout,
                                  headers={"Accept": "application/json"})
            if r.status_code != 200:
                return None, "HTTP %s" % r.status_code
            return r.json(), ""
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


def iso_para_epoch(txt):
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
    """Percorre o chat de trás pra frente a partir do fim da VOD. Retorna (segundos relativos, erro)."""
    canal, erro = kick_get_json("https://kick.com/api/v2/channels/%s" % quote(canal_slug))
    if not canal or "id" not in canal:
        return [], "não consegui o id do canal (%s)" % erro
    cid = canal["id"]

    fim_epoch = inicio_epoch + duracao_s
    cursor = str(int(fim_epoch * 1_000_000))
    tempos, vistos = [], set()
    paginas, sem_novas = 0, 0
    while paginas < 4000:
        url = "https://kick.com/api/v2/channels/%s/messages?cursor=%s" % (cid, cursor)
        dados, erro = kick_get_json(url)
        # se a Kick pedir calma (429), espera e tenta de novo
        tentativas = 0
        while dados is None and "429" in (erro or "") and tentativas < 5:
            tentativas += 1
            time.sleep(5 * tentativas)
            dados, erro = kick_get_json(url)
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
            t = iso_para_epoch(m.get("created_at"))
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


def achar_picos(tempos, duracao_s, quantidade, tamanho_clipe):
    """Janela deslizante de 30s; pega os maiores picos sem sobrepor.
    Retorna [(inicio_s, msgs)] do pico mais forte ao mais fraco."""
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
    return extrair_stream("https://kick.com/%s/videos/%s" % (slug, uuid_))


def cortar_clipe(job_id, idx, ini, formato):
    """Corta um clipe da VOD usando os dados guardados no job. idx começa em 0."""
    with JOBS_LOCK:
        j = JOBS[job_id]
        pasta, stream = j["pasta"], j["stream"]
        headers, tam = j["headers"], j["tamanho_clipe"]

    user_agent, extra = cabecalhos_ffmpeg(headers)
    saida = os.path.join(pasta, "clipe_%03d.mp4" % idx)
    cmd = ["ffmpeg", "-nostdin", "-y", "-user_agent", user_agent, *extra,
           "-protocol_whitelist", PROTOCOLOS,
           "-rw_timeout", "20000000",
           *(["-threads", str(THREADS_FFMPEG)] if formato != "original" else []),
           "-ss", str(int(ini)), "-i", stream, "-t", str(tam),
           *args_formato(formato),
           "-avoid_negative_ts", "make_zero", saida]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=240 if formato == "original" else 600)
    except subprocess.TimeoutExpired:
        _remover_arquivo(saida)
        return False, "demorou demais"
    if r.returncode == 0 and clipe_valido(saida):
        return True, ""
    _remover_arquivo(saida)  # não deixa arquivo pela metade entrar no ZIP
    return False, resumo_erro(r.stderr, r.returncode, 250)


def cortar_lote(job_id, lote, formato):
    """Corta uma lista de (inicio_s, msgs). Os números dos clipes seguem sempre em sequência,
    mesmo se algum corte falhar (o próximo reaproveita o número)."""
    with JOBS_LOCK:
        tam = JOBS[job_id]["tamanho_clipe"]
        info = list(JOBS[job_id]["info"])
        feitos = JOBS[job_id]["clipes"]
    notas = []
    novos = 0
    for k, (ini, score) in enumerate(lote):
        ok, nota = cortar_clipe(job_id, feitos, ini, formato)
        if ok:
            feitos += 1
            novos += 1
            info.append({"n": feitos, "ini": int(ini), "fim": int(ini + tam), "msgs": score})
            notas.append("Clipe %d: %s (%d msgs)" % (feitos, intervalo_clipe(ini, tam), score))
        else:
            notas.append("Corte em %s falhou: %s" % (fmt_tempo(ini), nota))
        atualizar_job(job_id, clipes=feitos, info=info,
                      msg="Cortando... %d de %d" % (k + 1, len(lote)),
                      diag="\n".join(notas)[-900:])

    diag = "\n".join(notas)[-900:]
    if novos:
        atualizar_job(job_id, estado="fim", clipes=feitos, info=info,
                      msg="Pronto! %d clipes dos melhores momentos." % feitos, diag=diag)
    elif feitos:
        atualizar_job(job_id, estado="fim", msg="Não consegui cortar os novos clipes. Os anteriores continuam aqui.",
                      diag=diag)
    else:
        atualizar_job(job_id, estado="erro", clipes=0, msg="Não consegui cortar nenhum clipe.", diag=diag)


def gravar_vod(job_id, url, tamanho_clipe, quantidade, formato="original"):
    pasta = os.path.join(BASE, job_id)
    os.makedirs(pasta, exist_ok=True)
    atualizar_job(job_id, pasta=pasta, estado="abrindo", msg="Procurando a live antiga...", diag="")

    slug = slug_da_url(url)
    m_video = re.search(r"/videos/([0-9a-fA-F-]{36})", url)
    if not slug:
        atualizar_job(job_id, estado="erro", msg="Link inválido.", diag="Use o link do canal da Kick.")
        return

    vods, erro = listar_vods(slug)
    if not vods:
        atualizar_job(job_id, estado="erro", msg="Não consegui listar as lives antigas desse canal.",
                      diag=erro or "O canal não tem lives salvas.")
        return
    escolhida = None
    if m_video:
        escolhida = next((v for v in vods if v["uuid"] == m_video.group(1)), None)
    if not escolhida:
        escolhida = escolher_vod(vods) or vods[0]

    inicio_epoch = iso_para_epoch(escolhida["inicio"])
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

    # guarda até 60 picos, do mais forte ao mais fraco
    ranking = achar_picos(tempos, dur, 60, tamanho_clipe)
    if not ranking:
        atualizar_job(job_id, estado="erro", msg="Não achei momentos de destaque no chat.", diag="")
        return

    # os N primeiros do ranking, em ordem de horário
    primeiros = sorted(ranking[:quantidade])

    # usa o link do vídeo que a lista já trouxe (evita o 429 do yt-dlp)
    src = escolhida.get("source") or ""
    if src.startswith("https://"):
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

    cortar_lote(job_id, primeiros, formato)


# ============================== LIVES NA HOME (com cache) ==============================

_LIVES_CACHE = {"ate": 0.0, "dados": None}
_LIVES_LOCK = threading.Lock()


def _consultar_canal(c):
    item = {"canal": c["canal"], "nome": c["nome"], "status": "incerto", "viewers": None}
    d, _ = kick_get_json("https://kick.com/api/v2/channels/%s" % quote(c["canal"]), timeout=10)
    if isinstance(d, dict) and "livestream" in d:
        ls = d["livestream"]
        if not ls:
            item["status"] = "offline"
        else:
            v = ls.get("viewer_count")
            if v is None:
                v = ls.get("viewers")
            item["status"] = "online"
            item["viewers"] = v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    return item


# ============================== ROTAS ==============================

@app.route("/")
def inicio():
    return Response(PAGINA, mimetype="text/html")


@app.route("/api/lives")
def api_lives():
    """Quem está online. Uma consulta por 30s para todos os visitantes (e não uma por visitante)."""
    with _LIVES_LOCK:
        if _LIVES_CACHE["dados"] is None or time.time() > _LIVES_CACHE["ate"]:
            with ThreadPoolExecutor(max_workers=len(CANAIS)) as ex:
                dados = list(ex.map(_consultar_canal, CANAIS))
            ok = any(x["status"] != "incerto" for x in dados)
            _LIVES_CACHE["dados"] = dados
            _LIVES_CACHE["ate"] = time.time() + (30 if ok else 10)
        return jsonify({"canais": _LIVES_CACHE["dados"]})


@app.route("/iniciar", methods=["POST"])
def iniciar():
    dados = request.get_json(silent=True) or {}
    url = str(dados.get("url", "")).strip()
    if not url_permitida(url):
        return jsonify({"id": "", "erro": "Use um link da Kick (kick.com)."}), 400

    minutos = ler_numero(dados.get("min", 5), 5.0, float)
    minutos = max(0.5, min(minutos, MAX_CAPTURA_MIN))
    seg = ler_seg(dados)
    formato = ler_formato(dados)

    job_id, erro = criar_job(tamanho_clipe=seg, formato=formato)
    if erro:
        return jsonify({"id": "", "erro": erro}), 429
    iniciar_thread(job_id, gravar, url, minutos, seg, formato)
    return jsonify({"id": job_id})


@app.route("/iniciar_vod", methods=["POST"])
def iniciar_vod():
    dados = request.get_json(silent=True) or {}
    url = str(dados.get("url", "")).strip()
    if not slug_da_url(url):
        return jsonify({"id": "", "erro": "Cole o link do canal da Kick."}), 400

    seg = ler_seg(dados)
    qtd = max(1, min(ler_numero(dados.get("qtd", 5), 5), 15))
    formato = ler_formato(dados)

    job_id, erro = criar_job(tamanho_clipe=seg, formato=formato)
    if erro:
        return jsonify({"id": "", "erro": erro}), 429
    iniciar_thread(job_id, gravar_vod, url, seg, qtd, formato)
    return jsonify({"id": job_id})


@app.route("/mais_clipes/<job_id>", methods=["POST"])
def mais_clipes(job_id):
    """Gera mais clipes do mesmo job, sem reler o chat."""
    dados = request.get_json(silent=True) or {}
    qtd = max(1, min(ler_numero(dados.get("qtd", 5), 5), 15))
    ip = ip_cliente()

    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j or not j.get("picos"):
            return jsonify({"erro": "Esse job não tem lista de picos. Rode os melhores momentos antes."}), 404
        if job_ativo(j):
            return jsonify({"erro": "Ainda tem clipes sendo cortados. Espere terminar."}), 409
        usados = j.get("usados", 0)
        restantes = j["picos"][usados:usados + qtd]
        if not restantes:
            return jsonify({"erro": "Acabaram os momentos de destaque dessa live."}), 404
        erro = _checar_limites_locked(ip)
        if erro:
            return jsonify({"erro": erro}), 429
        j["usados"] = usados + len(restantes)
        j["estado"] = "fila"
        j["msg"] = "Cortando mais %d clipes..." % len(restantes)
        j["ip"] = ip
        j["atualizado"] = time.time()
        formato = j["formato"]

    iniciar_thread(job_id, cortar_lote, sorted(restantes), formato)
    return jsonify({"id": job_id, "novos": len(restantes)})


_TESTE_ULTIMO = {}
_TESTE_LOCK = threading.Lock()


@app.route("/api/vod/teste")
def api_vod_teste():
    """Diagnóstico: o servidor consegue ler a Kick? Limitado a 1 teste a cada 15s por visitante."""
    agora = time.time()
    ip = ip_cliente()
    with _TESTE_LOCK:
        for k in [k for k, t in _TESTE_ULTIMO.items() if agora - t > 60]:
            del _TESTE_ULTIMO[k]
        if agora - _TESTE_ULTIMO.get(ip, 0) < 15:
            return jsonify({"erro": "Espere alguns segundos para testar de novo."}), 429
        _TESTE_ULTIMO[ip] = agora

    slug = request.args.get("canal", "bak").strip() or "bak"
    if not RE_SLUG.match(slug):
        return jsonify({"erro": "Nome de canal inválido."}), 400

    info = {"canal": slug, "curl_cffi": curl_requests is not None}
    vods, erro = listar_vods(slug)
    info["lista_vods"] = {"ok": bool(vods), "quantidade": len(vods), "erro": erro}
    if vods:
        v = escolher_vod(vods) or vods[0]
        info["primeira_vod"] = {"titulo": v["titulo"][:60], "inicio": v["inicio"], "duracao_s": v["duracao_s"]}
        canal, e2 = kick_get_json("https://kick.com/api/v2/channels/%s" % quote(slug))
        info["canal_id"] = {"ok": bool(canal and "id" in canal), "erro": e2}
        ini = iso_para_epoch(v["inicio"])
        if canal and "id" in canal and ini and v["duracao_s"] > 0:
            cur = str(int((ini + v["duracao_s"]) * 1_000_000))
            d, e3 = kick_get_json("https://kick.com/api/v2/channels/%s/messages?cursor=%s" % (canal["id"], cur))
            msgs = ((d or {}).get("data") or {}).get("messages") or []
            info["chat"] = {"ok": bool(msgs), "mensagens_na_pagina": len(msgs), "erro": e3}
    return jsonify(info)


@app.route("/status/<job_id>")
def status(job_id):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            return jsonify({"estado": "erro", "msg": "Tarefa não encontrada (pode ter expirado).",
                            "clipes": 0, "diag": ""})
        return jsonify({
            "estado": j["estado"],
            "msg": j["msg"],
            "clipes": j["clipes"],
            "diag": j.get("diag", ""),
            "info": j.get("info", []),
            "restantes": max(0, len(j.get("picos", [])) - j.get("usados", 0)),
        })


def _arquivo_do_clipe(job_id, n):
    """Caminho do clipe n (1-based) se ele já estiver pronto; senão ''."""
    pasta, prontos = dados_do_job(job_id)
    if not pasta or n < 1 or n > prontos:  # o clipe que ainda está sendo gravado não entra
        return ""
    arq = os.path.join(pasta, "clipe_%03d.mp4" % (n - 1))
    return arq if os.path.isfile(arq) else ""


@app.route("/clipe/<job_id>/<int:n>")
def clipe(job_id, n):
    arq = _arquivo_do_clipe(job_id, n)
    if not arq:
        return "Não encontrado", 404
    return send_file(arq, as_attachment=True, download_name="clipe_%d.mp4" % n)


@app.route("/ver/<job_id>/<int:n>")
def ver_clipe(job_id, n):
    """Serve o clipe sem forçar download, para a pré-visualização."""
    arq = _arquivo_do_clipe(job_id, n)
    if not arq:
        return "Não encontrado", 404
    return send_file(arq, mimetype="video/mp4", conditional=True)


def _enviar_e_apagar(caminho, tam_bloco=1 << 20):
    """Envia o arquivo aos poucos e apaga no fim (ou se o visitante desistir do download).
    (send_file + call_on_close não serve: o Werkzeug não chama o close em respostas de arquivo.)"""
    try:
        with open(caminho, "rb") as f:
            while True:
                bloco = f.read(tam_bloco)
                if not bloco:
                    break
                yield bloco
    finally:
        _remover_arquivo(caminho)


@app.route("/zip/<job_id>")
def baixar_zip(job_id):
    pasta, prontos = dados_do_job(job_id)
    if not pasta:
        return "Não encontrado", 404
    arquivos = listar_clipes(pasta)[:prontos]
    if not arquivos:
        return "Nenhum clipe disponível", 404

    # arquivo temporário único (downloads simultâneos não se pisam) e apagado depois de enviar.
    # MP4 já é comprimido, então ZIP_STORED é bem mais rápido que deflate.
    fd, caminho_zip = tempfile.mkstemp(suffix=".zip", dir=BASE)
    os.close(fd)
    try:
        with zipfile.ZipFile(caminho_zip, "w", compression=zipfile.ZIP_STORED) as z:
            for f in arquivos:
                z.write(os.path.join(pasta, f), f)
    except Exception:
        _remover_arquivo(caminho_zip)
        raise

    resp = Response(_enviar_e_apagar(caminho_zip), mimetype="application/zip")
    resp.headers["Content-Length"] = str(os.path.getsize(caminho_zip))
    resp.headers["Content-Disposition"] = 'attachment; filename="clipes.zip"'
    return resp


# limpeza periódica dos jobs e arquivos antigos (também roda sob gunicorn)
limpar_antigos()
threading.Thread(target=_loop_limpeza, daemon=True).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), threaded=True)
