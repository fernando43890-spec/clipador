"""Clipador de lives - Kick (somente Kick).

Desenvolvimento:
    pip install -r requirements.txt
    python app.py

Produção (use 1 worker: os jobs ficam em memória, então vários workers não se enxergam):
    gunicorn app:app --workers 1 --threads 8 --timeout 120

Arquivo único: o HTML da página está embutido na variável HTML.
Precisa do ffmpeg (com ffprobe) instalado no sistema. Variáveis de ambiente opcionais: veja o LEIAME.md.

Variáveis novas de segurança:
    CLIPADOR_PROXY=1|0        1 = confia no X-Forwarded-For (use só atrás de proxy/nginx/Render...). 0 = nunca.
                              Sem definir: liga sozinho se detectar Render, Railway, Fly, Heroku ou Cloud Run.
    CLIPADOR_PROXY_SALTOS=1   quantos proxies existem na frente (ex.: Cloudflare + Render = 2).
    CLIPADOR_MAX_DISCO_MB=4096  para de aceitar tarefas quando a pasta de clipes passa desse tamanho (0 = sem limite).
    CLIPADOR_TESTE=1          liga a rota /api/vod/teste e o botão de diagnóstico (deixe desligado em produção).
    CLIPADOR_ADMIN_SENHA=...  liga a página /admin/feedbacks (login pelo navegador: qualquer usuário + esta senha).
                              Sem definir, a página não existe (404). Use uma senha longa e só acesse por HTTPS.
    CLIPADOR_DADOS_DIR=...    pasta dos feedbacks, separada dos clipes. Aponte para um disco persistente da hospedagem,
                              senão os feedbacks somem quando o servidor reinicia. Sem definir, usa CLIPADOR_DIR.
"""
import functools
import json
import logging
import math
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html import escape
from urllib.error import HTTPError
from urllib.parse import quote, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from flask import Flask, Response, g, jsonify, request, send_file

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
MAX_DISCO_MB = _env_int("CLIPADOR_MAX_DISCO_MB", 4096)      # teto de espaço usado pelos clipes (0 = sem limite)
TESTE_ATIVO = os.environ.get("CLIPADOR_TESTE", "0") == "1"  # rota de diagnóstico /api/vod/teste
ADMIN_SENHA = os.environ.get("CLIPADOR_ADMIN_SENHA", "").strip()   # senha da página /admin/feedbacks ("" = página desligada)
DADOS = os.environ.get("CLIPADOR_DADOS_DIR") or BASE        # onde ficam os feedbacks (use um disco persistente)
os.makedirs(DADOS, exist_ok=True)
ARQ_FEEDBACK = os.path.join(DADOS, "feedback.jsonl")
FEEDBACK_MAX_BYTES = 5 * 1024 * 1024                        # a caixa de feedbacks para de aceitar acima disso

HOSTS_KICK = {"kick.com", "kzl.ink"}
HOSTS_ENCURTADOR = {"kzl.ink"}          # só estes são resolvidos; o destino final TEM de ser kick.com
ESTADOS_FINAIS = ("fim", "erro")
FORMATOS = ("original", "vertical_blur")
RE_SLUG = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
# ids novos têm 22 caracteres (128 bits, impossível de adivinhar); o formato antigo de 8 hex segue aceito só na limpeza
RE_JOB_ID = re.compile(r"^[A-Za-z0-9_-]{22}$")
RE_PASTA_JOB = re.compile(r"^([0-9a-f]{8}|[A-Za-z0-9_-]{22})$")

# Canais exibidos em "Lives agora na Kick". Edite aqui: o site pega esta lista automaticamente.
CANAIS = [
    {"nome": "Bak", "canal": "bak"},
    {"nome": "Renato Vicente", "canal": "orenatovicente"},
    {"nome": "Brabox", "canal": "brabox"},
    {"nome": "Gabe Peixe", "canal": "gabepeixe"},
    {"nome": "Coringa", "canal": "coringa"},
    {"nome": "Super Xandão", "canal": "superxandao"},
    {"nome": "Mitsuke", "canal": "mitsuke"},
    {"nome": "BRKK", "canal": "brkk"},
    {"nome": "NaraFPS", "canal": "narafps"},
    {"nome": "ThiagoSemT", "canal": "thiagosemt"},
    {"nome": "Renato Garcia", "canal": "renatogarcia"},
    {"nome": "Yoda", "canal": "yoda"},
    {"nome": "Yuri22", "canal": "yuri22"},
    {"nome": "Dona", "canal": "dona"},
    {"nome": "JonVlogs", "canal": "jonvlogs"},
    {"nome": "BahiaQZ", "canal": "bahiaqz"},
    {"nome": "Gaules", "canal": "gaules"},
]

# vídeo vertical: 720x1280 por padrão (leve). Se o servidor tiver folga de memória, CLIPADOR_VERTICAL_ALTURA=1920 usa 1080x1920.
VERT_H, VERT_W = (1920, 1080) if _env_int("CLIPADOR_VERTICAL_ALTURA", 1280) >= 1600 else (1280, 720)
# O ffmpeg em container enxerga todos os núcleos do servidor e abre threads demais (isso estoura a memória). Limitamos.
THREADS_FFMPEG = max(1, _env_int("CLIPADOR_FFMPEG_THREADS", 2))

UA_NAV = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024   # os pedidos são JSONs minúsculos; recusa o resto (HTTP 413)


def _proxy_ativo():
    """Só confia no X-Forwarded-For quando há mesmo um proxy na frente; senão qualquer um forja o IP e burla os limites."""
    valor = os.environ.get("CLIPADOR_PROXY", "").strip()
    if valor in ("0", "1"):
        return valor == "1"
    plataformas = ("RENDER", "RAILWAY_ENVIRONMENT", "FLY_APP_NAME", "DYNO", "K_SERVICE", "KOYEB_APP_NAME")
    return any(os.environ.get(k) for k in plataformas)


if _proxy_ativo():
    from werkzeug.middleware.proxy_fix import ProxyFix
    _saltos = max(1, _env_int("CLIPADOR_PROXY_SALTOS", 1))
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=_saltos, x_proto=_saltos, x_host=_saltos)
    log.info("Proxy reverso: confiando no X-Forwarded-For (%d salto/s).", _saltos)
else:
    log.info("Sem proxy reverso: usando o IP da conexão. Atrás de nginx etc., defina CLIPADOR_PROXY=1.")


HTML = r"""
<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#050906">
<meta name="color-scheme" content="dark">
<meta name="description" content="Corte lives da Kick em clipes de 30 segundos ou 1 minuto, em MP4 horizontal ou vertical 9:16.">
<meta property="og:type" content="website">
<meta property="og:title" content="Clipador de lives – Kick">
<meta property="og:description" content="Cole o link da Kick e baixe os melhores momentos da live em clipes prontos.">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Crect width='100' height='100' rx='22' fill='%2314151d'/%3E%3Cpath d='M22 24H40L60 50L40 76H22L42 50Z' fill='%2358e879'/%3E%3Cpath d='M48 24H66L86 50L66 76H48L68 50Z' fill='%2358e879' opacity='.65'/%3E%3C/svg%3E">
<title>Clipador de lives – Kick</title>
<style nonce="__NONCE__">
  :root {
    --bg: #050906;
    --surface: #0c120e;
    --surface-2: #121a15;
    --input: #0e1610;
    --borda: rgba(110, 220, 145, .16);
    --borda-forte: rgba(110, 220, 145, .30);
    --verde: #58e879;
    --verde-2: #76f59a;
    --verde-esc: #18883f;
    --texto: #f7f7fb;
    --mutado: #a9bbae;
    --erro: #ff7a93;
    --grad: linear-gradient(100deg, #6df58f 0%, #34c96a 50%, #18883f 100%);
    --grad-titulo: linear-gradient(100deg, #a6ffc0 0%, #58e879 45%, #1f9d4d 100%);
    --fonte: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  }

  * { box-sizing: border-box; }
  html { scroll-behavior: smooth; -webkit-text-size-adjust: 100%; }
  body {
    margin: 0; min-height: 100vh;
    font-family: var(--fonte); color: var(--texto); line-height: 1.45;
    background:
      radial-gradient(ellipse at 8% -6%, rgba(88, 232, 121, .18), transparent 46%),
      radial-gradient(ellipse at 96% 0%, rgba(24, 136, 63, .16), transparent 42%),
      linear-gradient(180deg, #0a2412 0%, rgba(5, 9, 6, 0) 600px),
      var(--bg);
  }
  .oculto { display: none; }

  /* ---------- topo ---------- */
  .topbar { padding-top: env(safe-area-inset-top); }
  .topbar-inner { width: 100%; max-width: none; margin: 0 auto; padding: 22px 16px 4px; }
  .brand { display: inline-block; color: var(--texto); text-decoration: none; }
  .brand-nome { display: block; font-weight: 800; font-size: 1.6rem; letter-spacing: -.04em; line-height: 1; }
  .brand-sub { display: flex; align-items: center; gap: 7px; margin-top: 4px; font-size: 1.05rem; font-weight: 700; color: var(--verde); letter-spacing: -.01em; }
  .brand-sub::before { content: ''; width: 22px; height: 3px; border-radius: 2px; background: var(--verde); }

  /* ---------- estrutura ---------- */
  .wrap { width: 100%; max-width: none; margin: 0 auto; padding: 16px 16px calc(36px + env(safe-area-inset-bottom)); }
  .layout { display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr); gap: 18px; align-items: start; }
  .col { display: grid; gap: 14px; align-content: start; min-width: 0; }
  @media (min-width: 900px) {
    .topbar-inner { padding: 14px 28px; }
    .wrap { padding: 24px 28px 44px; }
    .layout { grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr); gap: 22px; align-items: start; }
  }
  @media (max-width: 899px) {
    .layout { grid-template-columns: minmax(0, 1fr); }
  }

  /* ---------- cartões ---------- */
  .hero-card {
    background: linear-gradient(150deg, #0f1a13, #0a100c);
    border: 1px solid var(--borda-forte); border-radius: 18px; padding: 22px 18px;
    box-shadow: 0 18px 48px rgba(0, 0, 0, .35);
  }
  .hero-titulo {
    margin: 0; font-size: clamp(1.65rem, 6.4vw, 2.1rem); font-weight: 800;
    letter-spacing: -.03em; line-height: 1.12;
    background: var(--grad-titulo); -webkit-background-clip: text; background-clip: text;
    color: transparent; -webkit-text-fill-color: transparent;
  }
  .hero-sub { margin: 10px 0 20px; color: #d3d5df; font-size: .96rem; line-height: 1.5; max-width: 60ch; }
  .hero-sub strong { color: var(--texto); }
  .caixa, .card-live {
    background: var(--surface); border: 1px solid var(--borda); border-radius: 14px;
    padding: 15px 16px;
  }
  .card-live { padding: 14px; }
  .titulo-caixa { display: block; font-size: .95rem; font-weight: 700; color: var(--texto); margin-bottom: 10px; }

  /* ---------- formulário ---------- */
  .campo { margin-bottom: 14px; min-width: 0; }
  label { display: block; font-size: .86rem; font-weight: 600; color: #e9eaf1; margin-bottom: 7px; }
  input, select {
    width: 100%; min-height: 50px; padding: 13px 14px; font: inherit; font-size: .96rem;
    color: var(--texto); background-color: var(--input);
    border: 1px solid var(--borda-forte); border-radius: 11px; outline: none;
    transition: border-color .15s ease, box-shadow .15s ease;
  }
  input::placeholder { color: #8d91a3; }
  input:focus, select:focus { border-color: var(--verde-2); box-shadow: 0 0 0 3px rgba(88, 232, 121, .2); }
  select {
    appearance: none; -webkit-appearance: none; padding-right: 38px;
    background-image:
      linear-gradient(45deg, transparent 50%, var(--mutado) 50%),
      linear-gradient(135deg, var(--mutado) 50%, transparent 50%);
    background-position: calc(100% - 19px) calc(50% - 2px), calc(100% - 14px) calc(50% - 2px);
    background-size: 5px 5px, 5px 5px; background-repeat: no-repeat;
  }
  .dica { margin: 8px 0 0; font-size: .8rem; color: var(--mutado); line-height: 1.5; }

  .fmt-linha { display: flex; gap: 10px; align-items: center; }
  .fmt-linha select { flex: 1; min-width: 0; }
  .fmt-linha .dica { margin: 0; }
  .linha-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  #seg { border-color: rgba(88, 232, 121, .6); }
  #formato { border-color: rgba(88, 232, 121, .65); }
  .fmt-cel {
    flex: none; width: 66px; height: 70px; display: grid; place-items: center;
    background: var(--input); border: 1px solid var(--borda); border-radius: 11px;
  }
  .fmt-prev { position: relative; overflow: hidden; width: 48px; height: 27px; border-radius: 4px; background: #1c2a21; transition: width .2s ease, height .2s ease; }
  .fmt-prev i { position: absolute; inset: 0; background: linear-gradient(110deg, var(--verde-esc), var(--verde)); }
  .fmt-cel[data-fmt="vertical_blur"] .fmt-prev { width: 34px; height: 60px; background: linear-gradient(160deg, rgba(24, 136, 63, .5), rgba(88, 232, 121, .4)); }
  .fmt-cel[data-fmt="vertical_blur"] .fmt-prev i { top: 34%; bottom: 34%; }

  button { font: inherit; }
  .btn-principal {
    width: 100%; min-height: 54px; padding: 14px 18px; margin-top: 4px;
    font-size: 1.02rem; font-weight: 750; border: 0; border-radius: 12px;
    background: var(--grad); color: #0b0c12; cursor: pointer;
    transition: transform .12s ease, filter .15s ease;
  }
  .btn-principal:hover { filter: brightness(1.08); transform: translateY(-1px); }
  .btn-principal:active { transform: none; }
  .btn-principal:disabled { opacity: .55; cursor: not-allowed; transform: none; filter: none; }
  .btn-mini {
    background: var(--verde); color: #0b0c12; border: 1px solid var(--verde); border-radius: 10px;
    padding: 9px 13px; font-weight: 700; font-size: .82rem; cursor: pointer; text-decoration: none;
    transition: filter .15s ease; display: inline-block; text-align: center;
  }
  .btn-mini:hover { filter: brightness(1.08); }
  .btn-mini:disabled { opacity: .55; cursor: not-allowed; }
  .btn-mini.sec { background: transparent; color: var(--texto); border-color: var(--borda-forte); }
  .btn-mini.sec:hover { border-color: var(--verde-2); }
  .btn-mini.off { color: var(--mutado); }
  button:focus-visible, a:focus-visible, summary:focus-visible { outline: 2px solid var(--verde); outline-offset: 2px; }

  /* ---------- status e progresso ---------- */
  .status-topo { display: flex; justify-content: flex-end; margin-bottom: 8px; }
  .rotulo { display: inline-flex; align-items: center; gap: 7px; font-size: .8rem; font-weight: 600; color: var(--mutado); }
  .rotulo::before { content: ''; width: 8px; height: 8px; border-radius: 50%; background: #6d7186; }
  .rotulo[data-estado="trabalhando"], .rotulo[data-estado="fim"] { color: var(--verde-2); }
  .rotulo[data-estado="trabalhando"]::before, .rotulo[data-estado="fim"]::before { background: var(--verde); }
  .rotulo[data-estado="erro"] { color: var(--erro); }
  .rotulo[data-estado="erro"]::before { background: var(--erro); }
  .progresso { height: 6px; border-radius: 99px; background: rgba(180, 184, 210, .14); overflow: hidden; }
  .progresso span { display: block; height: 100%; width: 0; border-radius: 99px; background: linear-gradient(90deg, var(--verde-esc), var(--verde)); transition: width .3s ease; }
  .progresso[data-estado="trabalhando"] span { width: 38%; animation: corre 1.4s ease-in-out infinite; }
  .progresso[data-estado="fim"] span { width: 100%; }
  .progresso[data-estado="erro"] span { width: 100%; background: var(--erro); }
  @keyframes corre { 0% { transform: translateX(-110%); } 100% { transform: translateX(290%); } }
  #status { margin-top: 12px; font-size: .92rem; color: #dfe1ea; min-height: 22px; }
  #status[data-estado="erro"] { color: var(--erro); }

  .vazio { display: grid; gap: 6px; justify-items: center; text-align: center; padding: 26px 16px; color: var(--mutado); font-size: .9rem; }
  .vazio strong { color: var(--texto); font-size: .98rem; }
  .vazio.oculto, details.caixa.oculto { display: none; }
  .vazio svg { width: 44px; height: 44px; margin-bottom: 4px; opacity: .8; }

  details.caixa summary { cursor: pointer; font-size: .86rem; font-weight: 600; color: var(--mutado); list-style-position: inside; }
  pre {
    white-space: pre-wrap; word-break: break-word; margin: 10px 0 0; line-height: 1.45;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: .78rem; color: #d0d3df;
  }

  /* ---------- player da prévia ---------- */
  .card-topo { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; font-weight: 700; }
  .ponto { display: inline-block; width: 9px; height: 9px; border-radius: 50%; background: var(--verde); margin-right: 8px; }
  .ponto.off { background: #777b8d; }
  .player { display: block; width: 100%; aspect-ratio: 16/9; border: 0; border-radius: 10px; background: #000; }
  .player-aviso { margin: 8px 0 0; font-size: .78rem; color: var(--mutado); }

  /* ---------- lives (no topo da página) ---------- */
  .lives-sec { margin-bottom: 20px; }
  .lives-titulo { margin: 0 0 12px; font-size: .95rem; font-weight: 800; letter-spacing: .03em; text-transform: uppercase; }
  .lives-layout { display: grid; gap: 12px; }
  #lives-destaque:empty, #lives-outras:empty { display: none; }
  #lives-outras {
    display: grid; grid-template-columns: minmax(0, 1fr); grid-auto-flow: row; gap: 12px;
    min-width: 0; overflow: visible; padding: 2px 2px 10px; scroll-snap-type: none;
  }
  .live-card {
    min-width: 0; display: flex; flex-direction: column; gap: 8px; padding: 6px;
    border: 1.5px solid rgba(88, 232, 121, .55); border-radius: 14px; background: #0a100c; scroll-snap-align: start;
  }
  .live-card.destaque {
    padding: 8px; border-width: 2px; border-color: var(--verde);
    background: linear-gradient(160deg, #0f1c14, #0a100c);
    box-shadow: 0 0 0 1px rgba(88, 232, 121, .22), 0 0 32px rgba(88, 232, 121, .16);
  }
  .live-card.offline { border-color: var(--borda); opacity: .85; }
  .live-media { position: relative; aspect-ratio: 16/9; border-radius: 9px; overflow: hidden; background: #000; }
  .live-media .player { position: absolute; inset: 0; width: 100%; height: 100%; aspect-ratio: auto; border-radius: 0; }
  .live-thumb { position: absolute; inset: 0; display: grid; place-items: center; background: linear-gradient(135deg, #12201a, #0a100c); }
  .live-letra { font-size: 2rem; font-weight: 800; color: rgba(255, 255, 255, .3); }
  .live-img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; transition: opacity .2s ease; }
  .play-over {
    position: absolute; inset: 0; border: 0; cursor: pointer; display: grid; place-items: center;
    background: linear-gradient(to top, rgba(0, 0, 0, .55), transparent 55%); color: #0b0c12;
  }
  .play-over span { width: 44px; height: 44px; border-radius: 50%; background: rgba(88, 232, 121, .95); display: grid; place-items: center; padding-left: 3px; transition: transform .15s ease; }
  .play-over:hover span { transform: scale(1.08); }
  .live-card:not(.destaque) .play-over span { width: 36px; height: 36px; font-size: .85rem; }
  .destaque .play-over span { width: 62px; height: 62px; font-size: 1.3rem; }
  .badge-live { position: absolute; top: 8px; left: 8px; z-index: 1; background: #e5253d; color: #fff; font-size: .68rem; font-weight: 800; letter-spacing: .04em; padding: 3px 7px; border-radius: 5px; pointer-events: none; }
  .chip-viewers { position: absolute; left: 8px; bottom: 8px; z-index: 1; background: rgba(0, 0, 0, .68); color: #fff; font-size: .74rem; font-weight: 600; padding: 3px 9px; border-radius: 99px; pointer-events: none; }
  .live-info { display: flex; align-items: center; gap: 9px; min-width: 0; padding: 0 2px; }
  .avatar { flex: none; width: 34px; height: 34px; border-radius: 50%; overflow: hidden; background: #16231b; display: grid; place-items: center; font-weight: 800; font-size: .9rem; color: #cfd2de; }
  .avatar img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .live-textos { flex: 1; min-width: 0; }
  .live-nome { display: flex; align-items: center; gap: 6px; min-width: 0; font-size: .92rem; }
  .live-nome b { font-weight: 700; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .live-titulo { font-size: .78rem; color: var(--mutado); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-height: 1.1em; }
  .tag-destaque { display: none; flex: none; background: rgba(88, 232, 121, .16); color: var(--verde-2); font-size: .66rem; font-weight: 700; padding: 2px 7px; border-radius: 99px; }
  .destaque .tag-destaque { display: inline-block; }
  .destaque .live-nome { font-size: 1.08rem; }
  .destaque .avatar { width: 42px; height: 42px; }
  .viewers-info { display: none; flex: none; font-size: .78rem; color: var(--mutado); }
  .tocando .viewers-info { display: inline; }
  .btn-live {
    width: 100%; min-height: 40px; background: transparent; color: var(--verde); cursor: pointer;
    border: 1.5px solid var(--verde); border-radius: 10px; font-weight: 700; font-size: .86rem;
    transition: background .15s ease;
  }
  .btn-live:hover { background: rgba(88, 232, 121, .12); }
  .btn-live.off { border-color: var(--borda-forte); color: var(--mutado); }
  /* Live mais assistida em destaque; demais em grade compacta. */
  #lives-destaque, #lives-outras { width: 100%; min-width: 0; }
  .lives-layout:not(.sem-destaque) { grid-template-columns: minmax(0, 1fr); }
  #lives-destaque .live-card { width: 100%; }
  #lives-outras {
    display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
    grid-auto-flow: row; gap: 10px; min-width: 0; overflow: visible;
    padding: 2px 2px 10px; scroll-snap-type: none;
  }
  #lives-outras .live-card { gap: 5px; padding: 5px; border-radius: 10px; }
  #lives-outras .live-media { aspect-ratio: 1 / 1; }
  #lives-outras .live-info { gap: 5px; }
  #lives-outras .avatar { width: 25px; height: 25px; font-size: .72rem; }
  #lives-outras .live-nome { font-size: .76rem; }
  #lives-outras .live-titulo { font-size: .66rem; }
  #lives-outras .btn-live { min-height: 32px; font-size: .72rem; }
  #lives-outras .chip-viewers { font-size: .65rem; padding: 2px 6px; }
  #lives-outras .play-over span { width: 30px; height: 30px; }
  #lives-outras:not(.expandido) .live-card:nth-child(n+4) { display: none; }
  .btn-mais-lives {
    grid-column: 1 / -1; justify-self: center; min-width: 150px; padding: 10px 24px;
    border: 1.5px solid var(--verde); border-radius: 10px; background: rgba(88,232,121,.10);
    color: var(--verde); font-weight: 800; font-size: .9rem; cursor: pointer;
  }
  .btn-mais-lives.oculto, .caixa.oculto { display: none; }
  .btn-mais-lives:hover { background: rgba(88,232,121,.18); }
  @media (min-width: 700px) {
    #lives-outras { grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
  }

  /* ---------- clipes ---------- */
  .zip-link {
    display: block; text-align: center; padding: 12px; border-radius: 10px; font-weight: 700; text-decoration: none;
    background: rgba(88, 232, 121, .12); border: 1px solid var(--verde); color: var(--verde-2);
  }
  .clipe-item { margin-top: 10px; background: var(--surface-2); border: 1px solid var(--borda); border-radius: 12px; padding: 10px; }
  .clipe-topo { display: grid; grid-template-columns: 72px minmax(0, 1fr); gap: 10px; align-items: center; }
  .clipe-thumb { width: 72px; height: 72px; object-fit: cover; border-radius: 8px; background: #000; display: block; }
  .clipe-nome { font-weight: 700; font-size: .92rem; }
  .clipe-tempo { font-size: .78rem; color: var(--mutado); margin-top: 2px; }
  .clipe-acoes { grid-column: 1 / -1; display: flex; gap: 6px; flex-wrap: wrap; }
  .clipe-acoes .btn-mini { flex: 1 1 auto; padding: 8px 10px; font-size: .78rem; }
  .clipe-video { width: 100%; max-height: 70vh; border-radius: 10px; background: #000; margin-top: 10px; }
  @media (min-width: 520px) {
    .clipe-topo { grid-template-columns: 72px minmax(0, 1fr) auto; }
    .clipe-acoes { grid-column: auto; flex-wrap: nowrap; }
    .clipe-acoes .btn-mini { flex: none; }
  }
  .mais-box { margin-top: 12px; display: flex; gap: 8px; align-items: center; }
  .mais-box select { flex: 1; }
  .mais-box .btn-mini { white-space: nowrap; min-height: 50px; }

  /* ---------- rodapé, modal, aviso ---------- */
  .aviso { color: #9298a9; font-size: .8rem; text-align: center; margin: 22px 0 0; line-height: 1.5; }
  .rodape { text-align: center; margin: 8px 0 0; }
  .link { background: none; border: 0; padding: 6px; color: var(--mutado); font-size: .8rem; text-decoration: underline; cursor: pointer; }
  .modal-fundo { position: fixed; inset: 0; background: rgba(0, 0, 0, .72); z-index: 50; display: none; align-items: center; justify-content: center; padding: 16px; }
  .modal { background: var(--surface); border: 1px solid var(--borda-forte); border-radius: 16px; max-width: 480px; width: 100%; max-height: 80vh; overflow-y: auto; padding: 20px; }
  .modal h2 { margin: 0 0 10px; font-size: 1.1rem; }
  .modal p { color: var(--mutado); font-size: .88rem; line-height: 1.55; margin: 8px 0; }
  .modal .btn-mini { margin-top: 12px; width: 100%; }
  .toast {
    position: fixed; left: 50%; bottom: calc(20px + env(safe-area-inset-bottom)); z-index: 60;
    transform: translate(-50%, 16px); opacity: 0; pointer-events: none; max-width: 90vw;
    background: #16231b; border: 1px solid var(--borda-forte); border-radius: 99px;
    padding: 10px 18px; font-size: .88rem; text-align: center; transition: opacity .2s ease, transform .2s ease;
  }
  .toast.on { opacity: 1; transform: translate(-50%, 0); }

  @media (max-width: 390px) {
    .hero-card { padding: 18px 14px; }
    input, select { padding: 12px 10px; font-size: .9rem; }
    #lives { grid-auto-columns: 88%; }
  }
  @media (prefers-reduced-motion: reduce) {
    html { scroll-behavior: auto; }
    *, *::before, *::after { transition: none !important; }
    .progresso[data-estado="trabalhando"] span { animation: none; width: 100%; opacity: .5; }
  }
</style>
</head>
<body>
  <header class="topbar">
    <div class="topbar-inner">
      <a class="brand" href="/" aria-label="Clipador de lives, página inicial">
        <span class="brand-nome">Clipador<span class="brand-sub">de lives</span></span>
      </a>
    </div>
  </header>

  <main class="wrap">
    <div class="layout">
      <div class="col col-form">
        <section class="hero-card">
          <h1 class="hero-titulo">Transforme lives em clipes de 1 minuto</h1>
          <p class="hero-sub">Corte trechos marcantes de transmissões da <strong>Kick</strong> de forma fácil e rápida. Funciona com lives ao vivo e com lives antigas (melhores momentos).</p>

          <div class="campo">
            <label for="url">Link da live ou vídeo</label>
            <input id="url" type="url" inputmode="url" autocomplete="off" autocapitalize="none" spellcheck="false" placeholder="https://kick.com/canal">
          </div>

          <div class="campo">
            <label for="modo">O que você quer clipar?</label>
            <select id="modo">
              <option value="ao_vivo" selected>Live ao vivo</option>
              <option value="vod">Live antiga (melhores momentos)</option>
            </select>
          </div>

          <div class="campo" id="grupo-min">
            <label for="min">Quantidade de vídeos</label>
            <input id="min" type="number" inputmode="numeric" min="1" max="__MAXMIN__" step="1" value="5">
            <p class="dica" id="min-dica"></p>
          </div>

          <div class="campo oculto" id="grupo-qtd">
            <label for="qtd">Quantos clipes dos melhores momentos</label>
            <select id="qtd">
              <option value="3">3 clipes</option>
              <option value="5" selected>5 clipes</option>
              <option value="8">8 clipes</option>
              <option value="10">10 clipes</option>
            </select>
            <p class="dica">Usa o link do canal (ou de uma live antiga). Pega a live mais recente e acha os picos do chat.</p>
            <!--TESTE-->
          </div>

          <div class="linha-2">
            <div class="campo">
              <label for="seg">Duração de cada clipe</label>
              <select id="seg">
                <option value="60" selected>60 segundos</option>
              </select>
            </div>
            <div class="campo">
              <label for="formato">Formato do vídeo</label>
              <select id="formato">
                <option value="original" selected>Horizontal</option>
                <option value="vertical_blur">Vertical (desfocado)</option>
              </select>
            </div>
          </div>
          <div class="campo fmt-linha">
            <div class="fmt-cel" id="fmt-cel" data-fmt="original" aria-hidden="true"><div class="fmt-prev"><i></i></div></div>
            <p class="dica">Horizontal é o mais rápido, mas o início do corte pode variar alguns segundos. Vertical 9:16 reencoda o vídeo e demora mais; o modo desfocado preenche as laterais sem cortar a imagem.</p>
          </div>

          <button id="btn-gravar" type="button" class="btn-principal">Cortar meu vídeo</button>
        </section>

        <section class="card-live oculto" id="card-live" aria-label="Prévia da live">
          <div class="card-topo">
            <span><span class="ponto"></span><span id="plat-nome">Live</span></span>
            <span id="plat-status">AO VIVO</span>
          </div>
          <iframe id="player" class="player" title="Prévia da live" allow="autoplay; fullscreen" referrerpolicy="strict-origin-when-cross-origin"></iframe>
          <p class="player-aviso">Se o vídeo não aparecer, a gravação ainda pode funcionar.</p>
        </section>
      </div>

      <div class="col col-res">
        <div class="caixa oculto" id="status-box">
          <div class="status-topo"><span class="rotulo" id="status-rotulo" data-estado="ocioso">Aguardando</span></div>
          <div class="progresso" id="progresso" data-estado="ocioso" role="progressbar" aria-label="Andamento"><span></span></div>
          <div id="status" role="status" aria-live="polite" data-estado="ocioso">Cole um link de uma live da Kick para começar.</div>
        </div>

        <div class="caixa vazio oculto" id="vazio">
          <svg viewBox="0 0 48 48" fill="none" aria-hidden="true"><rect x="5" y="9" width="38" height="30" rx="6" stroke="#aeb2c2" stroke-width="2"/><path d="M20 18.5v11l9-5.5z" fill="#58e879"/></svg>
          <strong>Seus clipes vão aparecer aqui</strong>
          <span>Cole o link de uma live da Kick, escolha como cortar e toque em cortar.</span>
        </div>

        <div class="caixa oculto" id="lista"></div>

        <div class="caixa oculto" id="mais">
          <span class="titulo-caixa">Quer mais momentos dessa live?</span>
          <div class="mais-box">
            <select id="mais-qtd" aria-label="Quantidade de clipes extras">
              <option value="3">Mais 3 clipes</option>
              <option value="5" selected>Mais 5 clipes</option>
              <option value="10">Mais 10 clipes</option>
              <option value="15">Mais 15 clipes</option>
            </select>
            <button type="button" class="btn-mini" id="btn-mais">Gerar mais</button>
          </div>
          <p class="player-aviso" id="mais-aviso"></p>
        </div>

        <details class="caixa oculto" id="detalhes">
          <summary>Detalhes técnicos</summary>
          <pre id="diag">-</pre>
        </details>
      </div>
    </div>

    <section class="lives-sec" aria-labelledby="lives-titulo">
      <h2 class="lives-titulo" id="lives-titulo">Lives agora na Kick</h2>
      <div class="lives-layout" id="lives-layout">
        <div id="lives-destaque"></div>
        <div id="lives-outras"></div>
        <button type="button" class="btn-mais-lives oculto" id="btn-mais-lives" aria-expanded="false">Mais lives</button>
      </div>
    </section>

    <p class="aviso">Use apenas com autorização do streamer ou nas suas próprias lives.</p>
    <p class="rodape">
      <button type="button" class="link" id="abrir-termos">Termos de uso</button>
      <span aria-hidden="true"> · </span>
      <button type="button" class="link" id="abrir-feedback">Enviar feedback</button>
    </p>
  </main>

  <div class="modal-fundo" id="termos" role="dialog" aria-modal="true" aria-labelledby="termos-titulo">
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
      <button type="button" class="btn-mini" id="fechar-termos">Fechar</button>
    </div>
  </div>

  <div class="modal-fundo" id="feedback" role="dialog" aria-modal="true" aria-labelledby="feedback-titulo">
    <form class="modal" id="feedback-form">
      <h2 id="feedback-titulo">Enviar feedback</h2>
      <p>Conte uma sugestão, reclamação ou problema que encontrou no Clipador.</p>
      <label for="feedback-tipo">Tipo</label>
      <select id="feedback-tipo" required>
        <option value="sugestao">Sugestão</option>
        <option value="problema">Problema</option>
        <option value="elogio">Elogio</option>
      </select>
      <label for="feedback-mensagem">Mensagem</label>
      <textarea id="feedback-mensagem" maxlength="2000" required rows="5" placeholder="Escreva sua mensagem (até 2.000 caracteres)"></textarea>
      <button class="btn-mini" type="submit" id="feedback-enviar">Enviar</button>
      <button class="btn-mini sec" type="button" id="fechar-feedback">Fechar</button>
      <p id="feedback-status" role="status" aria-live="polite"></p>
    </form>
  </div>
  <div class="toast" id="toast" role="status" aria-live="polite"></div>

<script nonce="__NONCE__">
(function () {
'use strict';

const $ = function (id) { return document.getElementById(id); };
const CANAIS = /*__CANAIS__*/ [];
const RE_SLUG = /^[A-Za-z0-9_-]{1,40}$/;

let job = null;
let timerChecar = null;
const estadoCanal = {};   // canal -> 'online' | 'offline' | 'incerto'

// ---------- utilidades ----------
function el(tag, classe, texto) {
  const e = document.createElement(tag);
  if (classe) { e.className = classe; }
  if (texto !== undefined) { e.textContent = texto; }
  return e;
}

let timerToast = null;
function toast(msg) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.add('on');
  clearTimeout(timerToast);
  timerToast = setTimeout(function () { t.classList.remove('on'); }, 3200);
}

const MAX_MIN = __MAXMIN__;
function lerMinutos() {
  const n = Math.round(Number($('min').value));
  return Number.isFinite(n) ? Math.min(MAX_MIN, Math.max(1, n)) : 1;
}
function atualizarDicaMin() {
  const n = lerMinutos();
  $('min-dica').textContent = (n === 1 ? '1 vídeo' : n + ' vídeos') + ' de 60 segundos (' + n +
    (n === 1 ? ' minuto' : ' minutos') + ' de live). Máximo: ' + MAX_MIN + ' vídeos.';
}
$('min').addEventListener('input', atualizarDicaMin);
$('min').addEventListener('change', function () { $('min').value = lerMinutos(); atualizarDicaMin(); });
atualizarDicaMin();
const ROTULOS = {ocioso: 'Aguardando', trabalhando: 'Em andamento', fim: 'Concluído', erro: 'Erro'};
function definirStatus(msg, estado) {
  estado = ROTULOS[estado] ? estado : 'ocioso';
  const s = $('status');
  s.textContent = msg || '';
  s.dataset.estado = estado;
  $('progresso').dataset.estado = estado;
  const r = $('status-rotulo');
  r.dataset.estado = estado;
  r.textContent = ROTULOS[estado];
}

function msgHttp(codigo) {
  if (codigo === 429) { return 'Muitas requisições seguidas. Espere alguns segundos e tente de novo.'; }
  if (codigo === 413) { return 'Pedido grande demais.'; }
  if (codigo >= 500) { return 'O servidor está com problema. Tente de novo daqui a pouco.'; }
  return 'Não deu certo (erro ' + codigo + ').';
}

async function pedir(url, opcoes) {
  let r;
  try {
    r = await fetch(url, opcoes);
  } catch (e) {
    return {ok: false, status: 0, data: {erro: 'Sem conexão com o servidor. Confira sua internet e tente de novo.'}};
  }
  let data = {};
  try { data = await r.json(); } catch (e) { data = {}; }
  if (!r.ok && !data.erro) { data.erro = msgHttp(r.status); }
  return {ok: r.ok, status: r.status, data: data};
}

function postar(url, corpo) {
  return pedir(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(corpo)
  });
}

// ---------- termos ----------
let focoAntes = null;
function abrirTermos() {
  focoAntes = document.activeElement;
  $('termos').style.display = 'flex';
  $('fechar-termos').focus();
}
function fecharTermos() {
  $('termos').style.display = 'none';
  if (focoAntes && focoAntes.focus) { focoAntes.focus(); }
}
document.addEventListener('keydown', function (e) {
  const aberto = $('termos').style.display === 'flex';
  if (e.key === 'Escape' && aberto) { fecharTermos(); }
  if (e.key === 'Tab' && aberto) { e.preventDefault(); $('fechar-termos').focus(); }
});
$('abrir-termos').addEventListener('click', abrirTermos);
$('fechar-termos').addEventListener('click', fecharTermos);
$('termos').addEventListener('click', function (e) { if (e.target === this) { fecharTermos(); } });

// ---------- feedback ----------
function fecharFeedback() { $('feedback').style.display = 'none'; }
$('abrir-feedback').addEventListener('click', function () {
  $('feedback').style.display = 'flex';
  $('feedback-mensagem').focus();
});
$('fechar-feedback').addEventListener('click', fecharFeedback);
$('feedback').addEventListener('click', function (e) { if (e.target === this) { fecharFeedback(); } });
$('feedback-form').addEventListener('submit', async function (e) {
  e.preventDefault();
  const btn = $('feedback-enviar');
  const mensagem = $('feedback-mensagem').value.trim();
  if (mensagem.length < 5) { $('feedback-status').textContent = 'Escreva uma mensagem um pouco mais detalhada.'; return; }
  btn.disabled = true;
  $('feedback-status').textContent = 'Enviando...';
  const r = await postar('/feedback', {tipo: $('feedback-tipo').value, mensagem: mensagem});
  $('feedback-status').textContent = r.ok ? 'Obrigado! Seu feedback foi recebido.' : (r.data.erro || 'Não consegui enviar. Tente novamente.');
  btn.disabled = false;
  if (r.ok) { $('feedback-mensagem').value = ''; }
});

// ---------- prévia do link colado ----------
function plataforma(url) {
  try {
    const u = new URL(url);
    const host = u.hostname.replace(/^www\./, '');
    const partes = u.pathname.split('/').filter(Boolean);
    if (host === 'kick.com' && partes[0] && RE_SLUG.test(partes[0])) {
      return {nome: 'Kick', canal: partes[0], embed: 'https://player.kick.com/' + partes[0] + '?autoplay=true'};
    }
  } catch (e) {}
  return null;
}

function atualizarStatusPreview() {
  const p = plataforma($('url').value.trim());
  const txt = $('plat-status');
  const ponto = document.querySelector('#card-live .ponto');
  if (!txt || !ponto) { return; }
  const off = !!p && estadoCanal[p.canal] === 'offline';
  txt.textContent = off ? 'OFFLINE' : 'AO VIVO';
  ponto.classList.toggle('off', off);
}

let ultimoEmbed = '';
function mostrarPreview() {
  const p = plataforma($('url').value.trim());
  const card = $('card-live');
  if (!p) {
    card.style.display = 'none';
    $('player').src = 'about:blank';
    ultimoEmbed = '';
    return;
  }
  card.style.display = 'block';
  $('plat-nome').textContent = p.nome;
  if (p.embed !== ultimoEmbed) {
    $('player').src = p.embed;
    ultimoEmbed = p.embed;
  }
  atualizarStatusPreview();
}
$('url').addEventListener('input', mostrarPreview);
$('url').addEventListener('keydown', function (e) {
  if (e.key === 'Enter' && !$('btn-gravar').disabled) { e.preventDefault(); iniciar(); }
});

// ---------- lives agora ----------
function embedDe(c) { return 'https://player.kick.com/' + c.canal + '?autoplay=true'; }
function linkDe(c) { return 'https://kick.com/' + c.canal; }

function criarPlayer(c) {
  const f = document.createElement('iframe');
  f.className = 'player';
  f.title = 'Live de ' + c.nome;
  f.allow = 'autoplay; fullscreen';
  f.referrerPolicy = 'strict-origin-when-cross-origin';
  f.src = embedDe(c);
  return f;
}

function clipar(slug) {
  const c = CANAIS.find(function (x) { return x.canal === slug; });
  if (!c) { return; }
  $('url').value = linkDe(c);
  mostrarPreview();
  document.querySelector('.hero-card').scrollIntoView({behavior: 'smooth', block: 'start'});
}

function fmtViewers(n) {
  if (n >= 1000) {
    return (Math.round(n / 100) / 10).toLocaleString('pt-BR', {maximumFractionDigits: 1}) + ' mil';
  }
  return Number(n).toLocaleString('pt-BR');
}

const cards = {};   // canal -> estado do cartão (o elemento é reaproveitado, para o player não recarregar a cada 30 s)

function inicial(nome) { return String(nome || '?').charAt(0).toUpperCase(); }

function criarCard(c) {
  const k = {c: c, d: null, offline: null, player: null, manual: false, thumbImg: null, chipV: null, avatarUrl: ''};
  k.root = el('article', 'live-card');
  k.media = el('div', 'live-media');

  const info = el('div', 'live-info');
  k.avatar = el('span', 'avatar', inicial(c.nome));
  const textos = el('div', 'live-textos');
  const nome = el('div', 'live-nome');
  nome.appendChild(el('b', '', c.nome));
  nome.appendChild(el('span', 'tag-destaque', 'Maior público'));
  k.titulo = el('div', 'live-titulo');
  textos.appendChild(nome);
  textos.appendChild(k.titulo);
  k.viewersInfo = el('span', 'viewers-info');
  info.appendChild(k.avatar);
  info.appendChild(textos);
  info.appendChild(k.viewersInfo);

  k.btn = el('button', 'btn-live');
  k.btn.type = 'button';
  k.btn.addEventListener('click', function () { clipar(k.c.canal); });

  k.root.appendChild(k.media);
  k.root.appendChild(info);
  k.root.appendChild(k.btn);
  return k;
}

function montarMedia(k) {
  k.media.replaceChildren();
  k.player = null;
  k.thumbImg = null;
  k.chipV = null;
  k.root.classList.remove('tocando');
  const thumb = el('div', 'live-thumb');
  thumb.appendChild(el('span', 'live-letra', inicial(k.c.nome)));
  if (k.offline) {
    thumb.appendChild(el('span', 'chip-viewers', 'Offline'));
  } else {
    const img = document.createElement('img');
    img.className = 'live-img';
    img.alt = '';
    img.loading = 'lazy';
    img.referrerPolicy = 'no-referrer';
    img.style.opacity = '0';
    img.addEventListener('load', function () { img.style.opacity = ''; });
    img.addEventListener('error', function () { img.style.opacity = '0'; });
    k.thumbImg = img;
    thumb.appendChild(img);
    thumb.appendChild(el('span', 'badge-live', 'LIVE'));
    k.chipV = el('span', 'chip-viewers');
    thumb.appendChild(k.chipV);
    const play = el('button', 'play-over');
    play.type = 'button';
    play.setAttribute('aria-label', 'Assistir à live de ' + k.c.nome);
    play.appendChild(el('span', '', '▶'));
    play.addEventListener('click', function () { iniciarPlayer(k, true); });
    thumb.appendChild(play);
  }
  k.media.appendChild(thumb);
}

function aplicarDados(k, d) {
  k.d = d;
  k.titulo.textContent = d.offline ? 'Offline' : (d.titulo || 'Ao vivo agora');
  const v = (d.viewers !== null && d.viewers !== undefined) ? fmtViewers(d.viewers) + ' assistindo' : '';
  k.viewersInfo.textContent = v;
  if (k.chipV) { k.chipV.textContent = v || 'Ao vivo'; }
  if (k.thumbImg && d.thumb && k.thumbImg.getAttribute('src') !== d.thumb) { k.thumbImg.src = d.thumb; }
  if (d.avatar && d.avatar !== k.avatarUrl) {
    k.avatarUrl = d.avatar;
    const a = document.createElement('img');
    a.alt = '';
    a.referrerPolicy = 'no-referrer';
    a.addEventListener('load', function () { k.avatar.replaceChildren(a); });
    a.src = d.avatar;
  }
  k.btn.textContent = d.offline ? 'Usar última live' : 'Usar esta live';
  k.btn.classList.toggle('off', !!d.offline);
}

function iniciarPlayer(k, manual) {
  if (k.offline) { return; }
  if (manual) { k.manual = true; }
  if (k.player) { return; }
  const f = criarPlayer(k.c);
  k.media.replaceChildren(f);
  k.player = f;
  k.root.classList.add('tocando');
}

function pararPlayer(k) {
  montarMedia(k);
  if (k.d) { aplicarDados(k, k.d); }
}

function colocar(cont, nos) {
  // só move o que está fora de ordem (mover um iframe no DOM faz ele recarregar)
  nos.forEach(function (n, i) {
    if (cont.children[i] !== n) { cont.insertBefore(n, cont.children[i] || null); }
  });
  while (cont.children.length > nos.length) { cont.removeChild(cont.lastChild); }
}

let mostrarTodasLives = false;
const btnMaisLives = $('btn-mais-lives');
if (btnMaisLives) {
  btnMaisLives.addEventListener('click', function () {
    mostrarTodasLives = !mostrarTodasLives;
    $('lives-outras').classList.toggle('expandido', mostrarTodasLives);
    btnMaisLives.textContent = mostrarTodasLives ? 'Mostrar menos' : 'Mais lives';
    btnMaisLives.setAttribute('aria-expanded', String(mostrarTodasLives));
  });
}

function montarLives(lista) {
  // lista: [{canal, nome, viewers, offline, titulo, thumb, avatar}] online primeiro (maior público), offline depois
  const temPublico = lista.length > 0 && !lista[0].offline && lista[0].viewers !== null && lista[0].viewers !== undefined;
  const destaque = [];
  const outras = [];
  const vistos = {};
  lista.forEach(function (d, pos) {
    vistos[d.canal] = true;
    let k = cards[d.canal];
    if (!k) { k = cards[d.canal] = criarCard({canal: d.canal, nome: d.nome}); }
    const off = !!d.offline;
    if (k.offline !== off) {
      k.offline = off;
      k.root.classList.toggle('offline', off);
      montarMedia(k);
    }
    const ehDestaque = temPublico && pos === 0;
    k.root.classList.toggle('destaque', ehDestaque);
    aplicarDados(k, d);
    if (ehDestaque && !k.player) { iniciarPlayer(k, false); }          // a mais assistida já abre tocando
    if (!ehDestaque && k.player && !k.manual) { pararPlayer(k); }      // perdeu o 1º lugar: volta pra miniatura
    (ehDestaque ? destaque : outras).push(k.root);
  });
  Object.keys(cards).forEach(function (canal) { if (!vistos[canal]) { delete cards[canal]; } });
  colocar($('lives-destaque'), destaque);
  colocar($('lives-outras'), outras);
  $('lives-layout').classList.toggle('sem-destaque', destaque.length === 0);
  $('lives-outras').classList.toggle('expandido', mostrarTodasLives);
  if (btnMaisLives) {
    btnMaisLives.style.display = outras.length > 3 ? 'inline-flex' : 'none';
    btnMaisLives.textContent = mostrarTodasLives ? 'Mostrar menos' : 'Mais lives';
    btnMaisLives.setAttribute('aria-expanded', String(mostrarTodasLives));
  }
}
montarLives(CANAIS.map(function (c) {
  return {canal: c.canal, nome: c.nome, viewers: null, offline: false, titulo: '', thumb: '', avatar: ''};
}));

async function filtrarOnline() {
  if (document.hidden) { return; }
  const res = await pedir('/api/lives');
  if (!res.ok) { return; }
  const resultados = res.data.canais || [];
  resultados.forEach(function (x) { estadoCanal[x.canal] = x.status; });
  atualizarStatusPreview();

  const online = resultados.filter(function (x) { return x.status === 'online'; });
  const offline = resultados.filter(function (x) { return x.status === 'offline'; });
  if (online.length > 0 || offline.length > 0) {
    online.sort(function (a, b) { return (b.viewers || 0) - (a.viewers || 0); });
    montarLives(online.concat(offline).map(function (x) {
      return {canal: x.canal, nome: x.nome, viewers: x.viewers, offline: x.status === 'offline',
              titulo: x.titulo || '', thumb: x.thumb || '', avatar: x.avatar || ''};
    }));
  }
}
filtrarOnline();
setInterval(filtrarOnline, 30000);
document.addEventListener('visibilitychange', function () { if (!document.hidden) { filtrarOnline(); } });

// ---------- formulário ----------
function trocarModo() {
  const vod = $('modo').value === 'vod';
  $('grupo-qtd').style.display = vod ? 'block' : 'none';
  $('grupo-min').style.display = vod ? 'none' : 'block';
  $('btn-gravar').textContent = vod ? 'Achar melhores momentos' : 'Cortar meu vídeo';
}
$('modo').addEventListener('change', trocarModo);
$('formato').addEventListener('change', function () { $('fmt-cel').dataset.fmt = this.value; });



async function testarVod() {
  const url = $('url').value.trim();
  let canal = 'bak';
  try {
    const partes = new URL(url).pathname.split('/').filter(Boolean);
    if (partes[0] && RE_SLUG.test(partes[0])) { canal = partes[0]; }
  } catch (e) {}
  $('status-box').style.display = 'block';
  $('detalhes').style.display = 'block';
  definirStatus('Testando a leitura da Kick...', 'trabalhando');
  const res = await pedir('/api/vod/teste?canal=' + encodeURIComponent(canal));
  if (!res.ok) { definirStatus(res.data.erro || 'Erro ao testar.', 'erro'); return; }
  const d = res.data;
  $('diag').textContent = JSON.stringify(d, null, 2);
  const ok = d.lista_vods && d.lista_vods.ok && d.chat && d.chat.ok;
  definirStatus(ok ? 'Funcionou: o servidor lê as lives antigas e o chat.'
                   : 'Não deu: veja os detalhes técnicos abaixo.', ok ? 'fim' : 'erro');
}
const btnTestar = $('btn-testar');
if (btnTestar) { btnTestar.addEventListener('click', testarVod); }

function resetarResultados() {
  const lista = $('lista');
  lista.dataset.job = '';
  lista.style.display = 'none';
  lista.replaceChildren();
  $('mais').style.display = 'none';
  $('vazio').style.display = 'grid';
}

async function iniciar() {
  const url = $('url').value.trim();
  const btn = $('btn-gravar');
  $('status-box').style.display = 'block';
  if (!url) {
    definirStatus('Cole o link da Kick primeiro.', 'erro');
    $('url').focus();
    return;
  }
  btn.disabled = true;
  $('status-box').style.display = 'block';
  $('detalhes').style.display = 'block';
  clearTimeout(timerChecar);
  resetarResultados();
  definirStatus('Iniciando...', 'trabalhando');

  const vod = $('modo').value === 'vod';
  const base = {url: url, seg: $('seg').value, formato: $('formato').value};
  const corpo = vod ? Object.assign(base, {qtd: $('qtd').value}) : Object.assign(base, {min: lerMinutos()});
  const res = await postar(vod ? '/iniciar_vod' : '/iniciar', corpo);
  if (!res.ok || !res.data.id) {
    definirStatus(res.data.erro || 'Link inválido.', 'erro');
    btn.disabled = false;
    return;
  }
  job = res.data.id;
  checar();
}
$('btn-gravar').addEventListener('click', iniciar);

// ---------- lista de clipes ----------
function fmtTempo(s) {
  s = Math.max(0, Math.floor(s));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const mm = String(m).padStart(2, '0');
  const ss = String(sec).padStart(2, '0');
  return h > 0 ? h + ':' + mm + ':' + ss : m + ':' + ss;
}

async function copiarLink(jobId, n) {
  const link = location.origin + '/ver/' + jobId + '/' + n;
  try {
    await navigator.clipboard.writeText(link);
  } catch (e) {
    const ta = document.createElement('textarea');
    ta.value = link;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (e2) {}
    document.body.removeChild(ta);
    if (!ok) { toast('Não consegui copiar. Use o botão Baixar.'); return; }
  }
  toast('Link copiado. Vale enquanto o arquivo estiver no servidor.');
}

function assistir(jobId, n) {
  const alvo = $('video-' + n);
  if (!alvo) { return; }
  if (alvo.firstChild) { alvo.replaceChildren(); return; }
  const v = el('video', 'clipe-video');
  v.controls = true;
  v.autoplay = true;
  v.playsInline = true;
  v.src = '/ver/' + jobId + '/' + n;
  alvo.appendChild(v);
}

function criarItemClipe(i, jobId) {
  const item = el('div', 'clipe-item');
  item.id = 'clipe-' + i;

  const topo = el('div', 'clipe-topo');
  const img = el('img', 'clipe-thumb');
  img.alt = '';
  img.width = 72;
  img.height = 72;
  img.loading = 'lazy';
  img.addEventListener('error', function () { img.style.visibility = 'hidden'; });
  img.src = '/thumb/' + jobId + '/' + i;
  topo.appendChild(img);

  const meta = el('div');
  meta.appendChild(el('div', 'clipe-nome', 'Clipe ' + i));
  meta.appendChild(el('div', 'clipe-tempo'));
  topo.appendChild(meta);

  const acoes = el('div', 'clipe-acoes');
  const bAssistir = el('button', 'btn-mini sec', 'Assistir');
  bAssistir.type = 'button';
  bAssistir.addEventListener('click', function () { assistir(jobId, i); });
  const bCopiar = el('button', 'btn-mini sec', 'Copiar link');
  bCopiar.type = 'button';
  bCopiar.addEventListener('click', function () { copiarLink(jobId, i); });
  const aBaixar = el('a', 'btn-mini', 'Baixar');
  aBaixar.href = '/clipe/' + jobId + '/' + i;
  aBaixar.setAttribute('download', '');
  acoes.appendChild(bAssistir);
  acoes.appendChild(bCopiar);
  acoes.appendChild(aBaixar);
  topo.appendChild(acoes);

  item.appendChild(topo);
  const video = el('div');
  video.id = 'video-' + i;
  item.appendChild(video);
  return item;
}

// lista incremental: só acrescenta clipes novos, sem apagar o vídeo que a pessoa está assistindo
function atualizarListaClipes(clipesQtd, jobId, info) {
  const lista = $('lista');
  if (clipesQtd <= 0) {
    lista.style.display = 'none';
    $('vazio').style.display = 'grid';
    return;
  }
  lista.style.display = 'block';
  $('vazio').style.display = 'none';
  if (lista.dataset.job !== jobId) {
    const zip = el('a', 'zip-link');
    zip.id = 'zip-link';
    const itens = el('div');
    itens.id = 'clipes-itens';
    lista.replaceChildren(zip, itens);
    lista.dataset.job = jobId;
  }
  const zip = $('zip-link');
  zip.href = '/zip/' + jobId;
  zip.textContent = 'Baixar todos em ZIP (' + clipesQtd + ' clipes)';

  const itens = $('clipes-itens');
  const porNumero = {};
  (info || []).forEach(function (x) { porNumero[x.n] = x; });

  for (let i = 1; i <= clipesQtd; i++) {
    let item = $('clipe-' + i);
    if (!item) {
      item = criarItemClipe(i, jobId);
      itens.appendChild(item);
    }
    const x = porNumero[i];
    if (x) {
      item.querySelector('.clipe-tempo').textContent =
        fmtTempo(x.ini) + ' até ' + fmtTempo(x.fim) + ' da live · ' + x.msgs + ' msgs no chat';
    }
  }
}

// ---------- acompanhamento do job ----------
async function checar() {
  if (!job) { return; }
  const meuJob = job;
  const res = await pedir('/status/' + encodeURIComponent(meuJob));
  if (meuJob !== job) { return; }   // a pessoa iniciou outra tarefa enquanto isso
  if (!res.ok) {                    // rede, limite ou servidor: tenta de novo
    timerChecar = setTimeout(checar, res.status === 429 ? 6000 : 4000);
    return;
  }
  const d = res.data;
  const terminou = d.estado === 'fim' || d.estado === 'erro';
  definirStatus(d.msg || '', terminou ? d.estado : 'trabalhando');
  $('diag').textContent = d.diag || '-';
  atualizarListaClipes(Number(d.clipes || 0), meuJob, d.info || []);

  const temMais = (d.restantes || 0) > 0;
  if (d.estado === 'fim' && temMais) {
    $('mais').style.display = 'block';
    $('mais-aviso').textContent = 'Ainda restam ' + d.restantes + ' momentos de destaque nessa live.';
  } else {
    $('mais').style.display = 'none';
  }

  if (!terminou) {
    timerChecar = setTimeout(checar, 3000);
  } else {
    $('btn-gravar').disabled = false;
    $('btn-mais').disabled = false;
  }
}

async function maisClipes() {
  if (!job) { return; }
  const btn = $('btn-mais');
  btn.disabled = true;
  const res = await postar('/mais_clipes/' + encodeURIComponent(job), {qtd: $('mais-qtd').value});
  if (!res.ok) {
    $('mais-aviso').textContent = res.data.erro || 'Não deu pra gerar mais.';
    btn.disabled = false;
    return;
  }
  $('mais').style.display = 'none';
  $('btn-gravar').disabled = true;
  definirStatus('Cortando mais clipes...', 'trabalhando');
  clearTimeout(timerChecar);
  checar();
}
$('btn-mais').addEventListener('click', maisClipes);

trocarModo();
})();
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
    teste = ('<button type="button" class="btn-mini sec" id="btn-testar">'
             'Testar se o servidor lê a Kick</button>') if TESTE_ATIVO else ""
    html = html.replace("<!--TESTE-->", teste).replace("__MAXMIN__", str(MAX_CAPTURA_MIN))
    return html.replace("<!--CONTATO-->", contato)


PAGINA = _montar_pagina()

JOBS = {}
JOBS_LOCK = threading.Lock()
VAGAS = threading.BoundedSemaphore(MAX_SIMULTANEOS)


# CSP da página: scripts e estilos só com o nonce desta resposta; único site externo permitido é o player da Kick.
CSP_PAGINA = ("default-src 'self'; script-src 'nonce-{n}'; style-src 'nonce-{n}'; img-src 'self' data: https://*.kick.com; "
              "media-src 'self'; connect-src 'self'; frame-src https://player.kick.com; "
              "object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


@app.after_request
def cabecalhos(resp):
    h = resp.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
    h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    nonce = getattr(g, "nonce", None)
    if nonce:
        h["Content-Security-Policy"] = CSP_PAGINA.replace("{n}", nonce)
    else:
        h.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
    if request.is_secure:
        h.setdefault("Strict-Transport-Security", "max-age=31536000")
    if request.path.startswith(("/clipe/", "/ver/", "/thumb/")):
        h["Cache-Control"] = "private, no-cache"   # arquivos: o navegador revalida (e o seek do vídeo continua funcionando)
    else:
        h["Cache-Control"] = "no-store"
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
    return seg if seg == 60 else SEG


def ler_formato(dados):
    formato = str(dados.get("formato", "original"))
    return formato if formato in FORMATOS else "original"


def ip_cliente():
    return request.remote_addr or "?"


# ---------- limite de requisições por visitante (em memória; o app roda com 1 worker) ----------
_LIMITES = {}
_LIMITES_LOCK = threading.Lock()


def _segundos_de_espera(chave, maximo, janela_s):
    """0 se pode seguir; senão, quantos segundos faltam para liberar. Janela fixa, barata e suficiente aqui."""
    agora = time.time()
    with _LIMITES_LOCK:
        if len(_LIMITES) > 20000:  # não deixa o dicionário crescer sem fim
            for k in [k for k, v in _LIMITES.items() if agora - v[0] >= v[2]]:
                del _LIMITES[k]
        ini, qtd, _ = _LIMITES.get(chave, (agora, 0, janela_s))
        if agora - ini >= janela_s:
            ini, qtd = agora, 0
        qtd += 1
        _LIMITES[chave] = (ini, qtd, janela_s)
        if qtd > maximo:
            return max(1, int(ini + janela_s - agora))
    return 0


def limitar(nome, maximo, janela_s=60):
    """Decorador: no máximo `maximo` chamadas por visitante a cada `janela_s` segundos nesta rota."""
    def deco(f):
        @functools.wraps(f)
        def interno(*args, **kwargs):
            espera = _segundos_de_espera((nome, ip_cliente()), maximo, janela_s)
            if espera:
                resp = jsonify({"erro": "Muitas requisições seguidas. Espere %d s e tente de novo." % espera})
                resp.status_code = 429
                resp.headers["Retry-After"] = str(espera)
                return resp
            return f(*args, **kwargs)
        return interno
    return deco


# ---------- espaço em disco ----------
_DISCO = {"ate": 0.0, "bytes": 0}


def _bytes_em_uso():
    agora = time.time()
    if agora < _DISCO["ate"]:
        return _DISCO["bytes"]
    total = 0
    for raiz, _, arquivos in os.walk(BASE):
        for nome in arquivos:
            try:
                total += os.path.getsize(os.path.join(raiz, nome))
            except OSError:
                pass
    _DISCO["bytes"], _DISCO["ate"] = total, agora + 10   # recalcula no máximo a cada 10 s
    return total


def disco_cheio():
    return MAX_DISCO_MB > 0 and _bytes_em_uso() >= MAX_DISCO_MB * 1024 * 1024


# ---------- validação de links ----------
def _host_limpo(u):
    host = (u.hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def url_permitida(url):
    """Só aceita links http(s) da Kick, sem usuário/senha e sem porta estranha. Evita que o servidor
    seja usado pra baixar de qualquer lugar (SSRF)."""
    if not isinstance(url, str) or len(url) > 300:
        return False
    try:
        u = urlparse(url)
        host = _host_limpo(u)
        porta = u.port
    except ValueError:
        return False
    if u.username or u.password or porta not in (None, 80, 443):
        return False
    return u.scheme in ("http", "https") and host in HOSTS_KICK


class _SemRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None   # não segue: quem decide para onde ir somos nós


_ABRIR_SEM_REDIRECT = build_opener(_SemRedirect)


def _passo_redirect(url):
    """Um único passo de redirecionamento, sem seguir. Retorna (codigo_http, location); (None, '') se falhar."""
    try:
        if curl_requests is not None:
            r = curl_requests.get(url, impersonate="chrome", timeout=10, allow_redirects=False)
            return r.status_code, r.headers.get("location", "") or ""
        req = Request(url, headers={"User-Agent": UA_NAV})
        try:
            with _ABRIR_SEM_REDIRECT.open(req, timeout=10) as resp:
                return resp.status, ""
        except HTTPError as e:
            return e.code, e.headers.get("Location", "") or ""
    except Exception:
        return None, ""


_ENCURTADOS = {}
_ENCURTADOS_LOCK = threading.Lock()


def normalizar_url_kick(url):
    """Devolve o link final em kick.com, ou '' se não for aceitável.
    Links encurtados (kzl.ink) são resolvidos aqui, com no máximo 3 saltos, e CADA destino é validado:
    assim o yt-dlp nunca recebe um link que acabe em outro site."""
    url = (url or "").strip()
    agora = time.time()
    with _ENCURTADOS_LOCK:
        achado = _ENCURTADOS.get(url)
        if achado and achado[1] > agora:
            return achado[0]
    atual = url
    resultado = ""
    for _ in range(4):
        if not url_permitida(atual):
            break
        if _host_limpo(urlparse(atual)) not in HOSTS_ENCURTADOR:
            resultado = atual
            break
        codigo, destino = _passo_redirect(atual)
        if codigo not in (301, 302, 303, 307, 308) or not destino:
            break
        atual = urljoin(atual, destino)
    if resultado:
        with _ENCURTADOS_LOCK:
            if len(_ENCURTADOS) > 500:
                _ENCURTADOS.clear()
            _ENCURTADOS[url] = (resultado, agora + 600)
    return resultado



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
    if disco_cheio():
        return "O servidor está sem espaço no momento. Tente de novo em alguns minutos."
    return ""


def criar_job(**extra):
    """Cria o job se couber nos limites. Retorna (job_id, erro)."""
    ip = ip_cliente()
    with JOBS_LOCK:
        erro = _checar_limites_locked(ip)
        if erro:
            return None, erro
        job_id = secrets.token_urlsafe(16)   # 22 caracteres, 128 bits: quem não tem o link não acha o clipe
        while job_id in JOBS:
            job_id = secrets.token_urlsafe(16)
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
    cmd_json = ["yt-dlp", "--no-warnings", "--no-playlist", "-j", "-f", "best[height<=720]/best", "--", url]
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

    cmd_url = ["yt-dlp", "--no-warnings", "--no-playlist", "-g", "-f", "best[height<=720]/best", "--", url]
    r2 = subprocess.run(cmd_url, capture_output=True, text=True, timeout=90)
    links = [l.strip() for l in r2.stdout.strip().splitlines() if l.strip().startswith("http")]
    if links:
        return links[0], {}, ""

    erro_msg = (r.stderr or r2.stderr or "sem mensagem").strip()
    return None, {}, erro_msg[-700:]


def args_formato(formato, leve=False):
    """Argumentos de vídeo do ffmpeg. 'original' só copia; os verticais reencodam em 9:16 a 30 fps.
    leve=True: 540x960, preset ultrafast e 1 thread (plano B quando o servidor não aguenta)."""
    if formato != "vertical_blur":
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
    return ["-c", "copy"]


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


def _intervalos_amostra(duracao_s):
    """Trechos (em segundos) cujo chat será lido: 1h do início, 2h centrais e 3h finais, já sem sobreposição."""
    inicio = (0, min(3600, duracao_s))
    meio_ini = max(0, (duracao_s - 7200) // 2)
    meio = (meio_ini, min(duracao_s, meio_ini + 7200))
    fim = (max(0, duracao_s - 10800), duracao_s)
    intervalos = []
    for a, b in sorted((inicio, meio, fim)):
        if b <= a:
            continue
        if intervalos and a <= intervalos[-1][1]:
            intervalos[-1] = (intervalos[-1][0], max(intervalos[-1][1], b))
        else:
            intervalos.append((a, b))
    return intervalos


def baixar_chat_vod_amostrado(canal_slug, inicio_epoch, duracao_s, progresso=None):
    """Analisa até 1h do início, 2h centrais e 3h finais da VOD, sem percorrer o restante.
    progresso(paginas, mensagens, fracao): fracao = parte (0 a 1) do total a ler que já foi lida."""
    duracao_s = max(0, int(duracao_s))
    if duracao_s <= 0:
        return [], "duração inválida"
    intervalos = _intervalos_amostra(duracao_s)
    total_s = float(sum(b - a for a, b in intervalos)) or 1.0
    todos = set()
    erros = []
    paginas_antes = 0
    lido_antes = 0
    for a, b in intervalos:
        ultimas = {"pags": 0}

        def prog(pags, msgs, menor=None, a=a, b=b, lido_antes=lido_antes, paginas_antes=paginas_antes, ultimas=ultimas):
            ultimas["pags"] = pags
            if not progresso:
                return
            frac = None
            if menor is not None:
                # dentro do trecho o chat é lido de trás pra frente: já lemos de 'menor' até 'b'
                rel = min(b, max(a, menor - inicio_epoch))
                frac = min(1.0, (lido_antes + (b - rel)) / total_s)
            progresso(paginas_antes + pags, len(todos) + msgs, frac)

        tempos, erro = baixar_chat_vod(canal_slug, inicio_epoch + a, b - a, prog)
        todos.update(round(a + t, 3) for t in tempos)
        paginas_antes += ultimas["pags"]
        lido_antes += b - a
        if erro:
            erros.append(erro)
    return sorted(todos), ("; ".join(erros) if erros and not todos else "")


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

    def prog(pags, msgs, frac=None):
        resto = ""
        try:
            if frac is not None:
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
                           "Páginas consultadas: %d | amostras: início 1h, meio 2h, final 3h | live: %s" % (pags, escolhida["titulo"][:60]))

    tempos, erro = baixar_chat_vod_amostrado(slug, inicio_epoch, dur, prog)
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


def _url_imagem_kick(valor):
    """Só deixa passar imagens https de kick.com ou subdomínios (a CSP da página também só libera isso)."""
    if isinstance(valor, dict):
        valor = valor.get("src") or valor.get("url") or ""
    if not isinstance(valor, str) or len(valor) > 500:
        return ""
    valor = valor.strip()
    try:
        u = urlparse(valor)
        host = (u.hostname or "").lower()
    except ValueError:
        return ""
    if u.scheme != "https" or not (host == "kick.com" or host.endswith(".kick.com")):
        return ""
    return valor


def _consultar_canal(c):
    item = {"canal": c["canal"], "nome": c["nome"], "status": "incerto", "viewers": None,
            "titulo": "", "thumb": "", "avatar": ""}
    d, _ = kick_get_json("https://kick.com/api/v2/channels/%s" % quote(c["canal"]), timeout=10)
    if isinstance(d, dict):
        user = d.get("user") if isinstance(d.get("user"), dict) else {}
        item["avatar"] = _url_imagem_kick(user.get("profile_pic") or d.get("profile_pic") or d.get("profilePic"))
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
            item["titulo"] = str(ls.get("session_title") or "")[:120]
            item["thumb"] = _url_imagem_kick(ls.get("thumbnail"))
    return item


# ============================== ROTAS ==============================

@app.route("/")
def inicio():
    g.nonce = secrets.token_urlsafe(16)   # novo a cada resposta: é ele que autoriza o script e o CSS da página
    return Response(PAGINA.replace("__NONCE__", g.nonce), mimetype="text/html")


@app.route("/api/lives")
@limitar("lives", 30)
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


@app.route("/feedback", methods=["POST"])
@limitar("feedback", 3, 300)
def receber_feedback():
    dados = request.get_json(silent=True)
    dados = dados if isinstance(dados, dict) else {}
    tipo = str(dados.get("tipo", "sugestao"))[:20]
    mensagem = str(dados.get("mensagem", "")).strip()
    if tipo not in ("sugestao", "problema", "elogio") or not 5 <= len(mensagem) <= 2000:
        return jsonify({"erro": "Confira o tipo e escreva uma mensagem entre 5 e 2.000 caracteres."}), 400
    caminho = ARQ_FEEDBACK
    try:
        if os.path.isfile(caminho) and os.path.getsize(caminho) >= FEEDBACK_MAX_BYTES:
            return jsonify({"erro": "A caixa de feedbacks está cheia no momento. Tente novamente mais tarde."}), 503
        with open(caminho, "a", encoding="utf-8") as arq:
            arq.write(json.dumps({"data": datetime.now(timezone.utc).isoformat(), "tipo": tipo,
                                  "mensagem": mensagem}, ensure_ascii=False) + "\n")
    except OSError:
        return jsonify({"erro": "Não foi possível salvar agora. Tente novamente mais tarde."}), 500
    return jsonify({"ok": True})


# ---------- página do dono: ler os feedbacks ----------
try:
    from zoneinfo import ZoneInfo
    _FUSO_BR = ZoneInfo("America/Sao_Paulo")
except Exception:          # sem o banco de fusos no sistema: mostra em UTC
    _FUSO_BR = None

_ADMIN_FALHAS = {}         # ip -> (erros, início da janela)
_ADMIN_LOCK = threading.Lock()
_ADMIN_MAX_ERROS, _ADMIN_JANELA_S = 5, 900


def _admin_espera(ip):
    """Segundos de bloqueio por senha errada (0 = liberado). Vale mesmo se a próxima tentativa estiver certa."""
    agora = time.time()
    with _ADMIN_LOCK:
        erros, ini = _ADMIN_FALHAS.get(ip, (0, agora))
        if agora - ini > _ADMIN_JANELA_S:
            _ADMIN_FALHAS.pop(ip, None)
            return 0
        return int(ini + _ADMIN_JANELA_S - agora) + 1 if erros >= _ADMIN_MAX_ERROS else 0


def _admin_falhou(ip):
    agora = time.time()
    with _ADMIN_LOCK:
        if len(_ADMIN_FALHAS) > 5000:
            _ADMIN_FALHAS.clear()
        erros, ini = _ADMIN_FALHAS.get(ip, (0, agora))
        if agora - ini > _ADMIN_JANELA_S:
            erros, ini = 0, agora
        _ADMIN_FALHAS[ip] = (erros + 1, ini)


def _ler_feedbacks(limite=200):
    """Os feedbacks mais recentes primeiro (lê só o fim do arquivo, no máximo 1 MB)."""
    try:
        with open(ARQ_FEEDBACK, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 1_000_000))
            bruto = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return []
    itens = []
    for linha in bruto.splitlines():
        try:
            d = json.loads(linha)
        except ValueError:
            continue
        if isinstance(d, dict):
            itens.append(d)
    return itens[-limite:][::-1]


def _data_br(txt):
    try:
        dt = datetime.fromisoformat(str(txt))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if _FUSO_BR:
            return dt.astimezone(_FUSO_BR).strftime("%d/%m/%Y %H:%M")
        return dt.astimezone(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    except (ValueError, TypeError):
        return "?"


_TIPOS_FEEDBACK = {"sugestao": "Sugestão", "problema": "Problema", "elogio": "Elogio"}

_ADMIN_HTML = """<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow"><meta name="color-scheme" content="dark">
<title>Feedbacks – Clipador</title>
<style nonce="__NONCE__">
  body { margin: 0; padding: 16px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
         background: linear-gradient(180deg, #0a2412 0, #050906 480px) #050906; color: #f7f7fb; line-height: 1.45; }
  main { max-width: 720px; margin: 0 auto; }
  h1 { font-size: 1.3rem; margin: 6px 0 2px; }
  .sub { color: #a9bbae; font-size: .85rem; margin: 0 0 14px; }
  .item { background: #0c120e; border: 1px solid rgba(110,220,145,.18); border-radius: 12px; padding: 12px 14px; margin-bottom: 10px; }
  .topo { display: flex; justify-content: space-between; gap: 8px; font-size: .78rem; color: #a9bbae; margin-bottom: 6px; }
  .tipo { font-weight: 800; color: #58e879; }
  .tipo.problema { color: #ff7a93; }
  .msg { white-space: pre-wrap; overflow-wrap: anywhere; }
  .vazio { color: #a9bbae; text-align: center; padding: 30px 0; }
</style></head><body><main>
<h1>Feedbacks</h1>
<p class="sub">__SUB__</p>
__ITENS__
</main></body></html>"""


@app.route("/admin/feedbacks")
def admin_feedbacks():
    if not ADMIN_SENHA:
        return "Não encontrado", 404
    ip = ip_cliente()
    espera = _admin_espera(ip)
    if espera:
        resp = Response("Muitas tentativas erradas. Espere %d min." % ((espera + 59) // 60), status=429)
        resp.headers["Retry-After"] = str(espera)
        return resp
    auth = request.authorization
    certo = bool(auth and auth.password and
                 secrets.compare_digest(auth.password.encode("utf-8"), ADMIN_SENHA.encode("utf-8")))
    if not certo:
        _admin_falhou(ip)
        return Response("Senha necessária.", status=401,
                        headers={"WWW-Authenticate": 'Basic realm="Clipador", charset="UTF-8"'})

    itens = _ler_feedbacks()
    if itens:
        blocos = []
        for d in itens:
            tipo = str(d.get("tipo", ""))
            blocos.append(
                '<div class="item"><div class="topo"><span class="tipo %s">%s</span><span>%s</span></div>'
                '<div class="msg">%s</div></div>' % (
                    escape(tipo) if tipo in _TIPOS_FEEDBACK else "",
                    escape(_TIPOS_FEEDBACK.get(tipo, "Outro")),
                    escape(_data_br(d.get("data"))),
                    escape(str(d.get("mensagem", "")))))
        corpo = "\n".join(blocos)
    else:
        corpo = '<p class="vazio">Nenhum feedback ainda.</p>'
    sub = "Mais recentes primeiro (até 200). Horário de Brasília." if _FUSO_BR else "Mais recentes primeiro (até 200). Horário UTC."
    g.nonce = secrets.token_urlsafe(16)
    html = _ADMIN_HTML.replace("__NONCE__", g.nonce).replace("__SUB__", sub).replace("__ITENS__", corpo)
    resp = Response(html, mimetype="text/html")
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


@app.route("/iniciar", methods=["POST"])
@limitar("iniciar", 8)
def iniciar():
    dados = request.get_json(silent=True)
    dados = dados if isinstance(dados, dict) else {}
    url = normalizar_url_kick(str(dados.get("url", "")))
    if not url:
        return jsonify({"id": "", "erro": "Use um link da Kick (kick.com). Se for um link encurtado e não funcionar, cole o link direto do canal."}), 400

    minutos = ler_numero(dados.get("min", 5), 5.0, float)
    minutos = float(max(1, min(int(round(minutos)), MAX_CAPTURA_MIN)))   # minutos inteiros: 1 min = 1 clipe de 60 s
    seg = ler_seg(dados)
    formato = ler_formato(dados)

    job_id, erro = criar_job(tamanho_clipe=seg, formato=formato)
    if erro:
        return jsonify({"id": "", "erro": erro}), 429
    iniciar_thread(job_id, gravar, url, minutos, seg, formato)
    return jsonify({"id": job_id})


@app.route("/iniciar_vod", methods=["POST"])
@limitar("iniciar", 8)
def iniciar_vod():
    dados = request.get_json(silent=True)
    dados = dados if isinstance(dados, dict) else {}
    url = normalizar_url_kick(str(dados.get("url", "")))
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
@limitar("mais", 8)
def mais_clipes(job_id):
    """Gera mais clipes do mesmo job, sem reler o chat."""
    dados = request.get_json(silent=True)
    dados = dados if isinstance(dados, dict) else {}
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
    """Diagnóstico: o servidor consegue ler a Kick? Só existe com CLIPADOR_TESTE=1. Limitado a 1 teste a cada 15s por visitante."""
    if not TESTE_ATIVO:
        return "Não encontrado", 404
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
@limitar("status", 90)
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
@limitar("clipe", 30)
def clipe(job_id, n):
    arq = _arquivo_do_clipe(job_id, n)
    if not arq:
        return "Não encontrado", 404
    return send_file(arq, as_attachment=True, download_name="clipe_%d.mp4" % n)


@app.route("/ver/<job_id>/<int:n>")
@limitar("ver", 120)
def ver_clipe(job_id, n):
    """Serve o clipe sem forçar download, para a pré-visualização."""
    arq = _arquivo_do_clipe(job_id, n)
    if not arq:
        return "Não encontrado", 404
    return send_file(arq, mimetype="video/mp4", conditional=True)


_VAGAS_MINIATURA = threading.BoundedSemaphore(2)


@app.route("/thumb/<job_id>/<int:n>")
@limitar("thumb", 120)
def miniatura(job_id, n):
    """Miniatura (um quadro) do clipe n, gerada na primeira vez e guardada ao lado do clipe."""
    arq = _arquivo_do_clipe(job_id, n)
    if not arq:
        return "Não encontrado", 404
    saida = os.path.join(os.path.dirname(arq), "thumb_%03d.jpg" % (n - 1))
    if not os.path.isfile(saida):
        tmp = "%s.%s.tmp.jpg" % (saida, secrets.token_hex(4))
        cmd = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-ss", "0.5", "-i", arq,
               "-frames:v", "1", "-vf", "scale=240:-2", "-q:v", "6", tmp]
        try:
            with _VAGAS_MINIATURA:
                r = subprocess.run(cmd, capture_output=True, timeout=20)
            if r.returncode != 0 or not os.path.isfile(tmp):
                raise RuntimeError("ffmpeg falhou")
            os.replace(tmp, saida)
        except Exception:
            _remover_arquivo(tmp)
            return "Sem miniatura", 404
    return send_file(saida, mimetype="image/jpeg", conditional=True)


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
@limitar("zip", 10)
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
