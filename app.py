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
import base64
import functools
import hashlib
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

# Logo "Clipes das Lives" (JPEG 384x384) embutida em base64: o app continua sendo um arquivo único.
LOGO_JPG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAQDAwQDAwQEBAQFBQQFBwsHBwYGBw4KCggLEA4RERAOEA8SFBoWEhMYEw8QFh8XGBsb"
    "HR0dERYgIh8cIhocHRz/2wBDAQUFBQcGBw0HBw0cEhASHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwc"
    "HBwcHBwcHBz/wgARCAGAAYADASIAAhEBAxEB/8QAHAAAAQQDAQAAAAAAAAAAAAAABgMEBQcAAggB/8QAGgEAAgMBAQAAAAAAAAAA"
    "AAAAAAQBAwUCBv/aAAwDAQACEAMQAAAB5/zMDMzAzfWbrqjklmhPmZllmZm8R5s5dV1Rerxv12lnvnfeZ7geZt7Ea5v7EaYr6Qli"
    "3sQ3xzgNcW1npLN/JnX3b0NfHuvHDTZZLqczTyet9NvA8zMmczFIhPMyZzMk+eHE01j0M2WF0Mac1zfLmPHCLnniwSWAs3Ax6iBj"
    "sG0nWuyx8xfXmtkAEQ332eWdtVjIlzc+sNrikFFqQWvRz1FCIXEH9wBoFDN54fydSsth9H7a27do+ZE+Po+SBi3WSstz3XeZ0z3c"
    "MPZmfwfP0NmPd3f3eNo2qhVLza5j1xqdLrB6dywi6tYbv0WWiyxa0v7NSpoTs8SWWCrlArJYYGKzKgW++RkRvdxmc3hleK56RGJl"
    "dYsKYF5lIWjKUIzZftMC8I2egVeIjWWSELOR1LOhGMilo6Ef66RtuSzNOutvPNiculMu8xiORuvQbuHGIa+j28zFeuk91POeVT+v"
    "pRVSz3wlO5efAt7eH2pELdaxPbtB3JR5NC9tkAMzxc8RDjR1u64QUFe9fC2w2awDkbfJm25xjlnVk1fM9NckuOp7SyujUA/m7rwV"
    "DnQ5q7qDAw+dYS9qgYkeSet9HTQ8XzrtG0wZFOk9rFN93LJ9tF39+baqW3e7IveeGrhNxzyiv674qXJYQ2zEdDMIGV6uo62VnC+u"
    "tYMRmixF6vKGrWRJ0HOaWrx7r1DzHb3HnNbJB2yvyJ0hMktGXBAByjobAkQvd1EbRF0gbWYVoHCxuG0L9X1vX15ZifPTczG9K1+r"
    "dUZwUBHFQu62n7mt7WvnvnXXm2vobqp788KrpylC6pq5vHKyQQIktU1gwtrZnsbc84PbrdYoqt+wQW2/mNRePkv+2+IrUDoSJhgQ"
    "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"
    "+JRoDBgDaUEqS6jpY4LTrye9oiRU2q5X/wA//9k="
)
LOGO_ETAG = hashlib.md5(LOGO_JPG).hexdigest()[:16]


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
<meta property="og:image" content="__OGIMG__">
<meta name="twitter:card" content="summary">
<meta name="twitter:image" content="__OGIMG__">
<link rel="icon" type="image/jpeg" href="/logo.jpg">
<link rel="apple-touch-icon" href="/logo.jpg">
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
  .brand { display: inline-block; line-height: 0; border-radius: 20px; text-decoration: none; }
  .brand-logo {
    display: block; width: 92px; height: 92px; border-radius: 20px; object-fit: cover;
    box-shadow: 0 0 0 1px rgba(88, 232, 121, .35), 0 0 26px rgba(88, 232, 121, .28);
    transition: transform .15s ease, box-shadow .15s ease;
  }
  .brand:hover .brand-logo { transform: scale(1.04); box-shadow: 0 0 0 1px rgba(88, 232, 121, .6), 0 0 34px rgba(88, 232, 121, .42); }
  @media (min-width: 900px) { .brand-logo { width: 110px; height: 110px; border-radius: 24px; } }

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
        <img class="brand-logo" src="/logo.jpg" width="92" height="92" alt="Clipes das Lives" decoding="async" fetchpriority="high">
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
    if request.path == "/logo.jpg":
        h["Cache-Control"] = "public, max-age=86400"
    elif request.path.startswith(("/clipe/", "/ver/", "/thumb/")):
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
    pagina = PAGINA.replace("__NONCE__", g.nonce).replace("__OGIMG__", request.url_root + "logo.jpg")
    return Response(pagina, mimetype="text/html")


@app.route("/logo.jpg")
def logo():
    resp = Response(LOGO_JPG, mimetype="image/jpeg")
    resp.set_etag(LOGO_ETAG)
    return resp.make_conditional(request)


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
