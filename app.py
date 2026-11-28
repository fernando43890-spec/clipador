"""Clipador de lives - Kick (somente Kick).

Desenvolvimento:
    pip install -r requirements.txt
    python app.py

Produção (use 1 worker: os jobs ficam em memória, então vários workers não se enxergam):
    gunicorn app:app --workers 1 --threads 4 --timeout 120

Arquivo único: o HTML da página está embutido na variável HTML.
Precisa do ffmpeg (com ffprobe) instalado no sistema. Variáveis de ambiente opcionais: veja o LEIAME.md.

Plano grátis do Railway (0,5 GB de RAM, 1 GB de disco): os padrões abaixo já vêm ajustados para isso.
    CLIPADOR_MAX_CAPTURA_MIN=15   máximo de minutos da captura ao vivo.
    CLIPADOR_COTA_CLIPES=15       clipes por visitante dentro da janela abaixo (0 = sem limite).
    CLIPADOR_COTA_JANELA_MIN=60   tamanho da janela da cota, em minutos.
    CLIPADOR_MAX_JOBS_DIA=100     tarefas aceitas por dia no site todo (0 = sem limite). Protege o crédito do mês.
    CLIPADOR_MAX_SIMULTANEOS=2    tarefas de VOD/cortes ao mesmo tempo.
    CLIPADOR_MAX_CAPTURAS=2       capturas ao vivo ao mesmo tempo (usam pouca CPU, só copiam o vídeo).
    CLIPADOR_MAX_VERTICAL=1       conversões para vertical ao mesmo tempo (é o que mais gasta CPU e memória).
    CLIPADOR_CACHE_CHAT_MIN=60    por quanto tempo guardar a análise do chat de uma live.
    Estatísticas em /admin/estatisticas (mesma senha de /admin/feedbacks). Guardam só contagens e um código
    irreversível do visitante, nunca o IP. Para não zerarem a cada reinício, aponte CLIPADOR_DADOS_DIR para um volume.

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
import contextlib
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
import textwrap
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

# Transcrição local e gratuita com whisper.cpp (não usa chave nem envia áudio a terceiros).
WHISPER_CPP_BIN = os.environ.get("CLIPADOR_WHISPER_BIN", "whisper-cli").strip() or "whisper-cli"
WHISPER_CPP_MODELO = os.environ.get(
    "CLIPADOR_WHISPER_MODELO", "/app/models/ggml-tiny-q5_1.bin"
).strip()


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
MAX_CAPTURA_MIN = max(1, _env_int("CLIPADOR_MAX_CAPTURA_MIN", 15))   # máximo de captura ao vivo
MAX_SIMULTANEOS = max(1, _env_int("CLIPADOR_MAX_SIMULTANEOS", 2))    # análises de VOD/cortes rodando ao mesmo tempo
MAX_CAPTURAS = max(1, _env_int("CLIPADOR_MAX_CAPTURAS", 2))          # capturas ao vivo (só copiam o vídeo: leves)
MAX_VERTICAL = max(1, _env_int("CLIPADOR_MAX_VERTICAL", 1))          # conversões para vertical (as que mais pesam)
MAX_ATIVOS_POR_IP = _env_int("CLIPADOR_MAX_POR_IP", 2)      # tarefas ativas por visitante
MAX_ATIVOS_TOTAL = _env_int("CLIPADOR_MAX_FILA", 10)        # tarefas ativas no servidor todo (fila inclusa)
VALIDADE_S = _env_int("CLIPADOR_VALIDADE_MIN", 30) * 60     # quanto tempo os arquivos ficam no servidor
COTA_CLIPES = _env_int("CLIPADOR_COTA_CLIPES", 15)          # clipes por visitante dentro da janela (0 = sem limite)
COTA_JANELA_S = max(1, _env_int("CLIPADOR_COTA_JANELA_MIN", 60)) * 60
MAX_JOBS_DIA = _env_int("CLIPADOR_MAX_JOBS_DIA", 100)       # tarefas por dia no site todo (0 = sem limite)
CACHE_CHAT_S = _env_int("CLIPADOR_CACHE_CHAT_MIN", 60) * 60 # quanto tempo vale a análise do chat guardada
CONTATO = os.environ.get("CLIPADOR_CONTATO", "").strip()    # e-mail para pedidos de remoção (aparece nos termos)
MAX_DISCO_MB = _env_int("CLIPADOR_MAX_DISCO_MB", 700)      # teto de espaço usado pelos clipes (0 = sem limite)
TESTE_ATIVO = os.environ.get("CLIPADOR_TESTE", "0") == "1"  # rota de diagnóstico /api/vod/teste
ADMIN_SENHA = os.environ.get("CLIPADOR_ADMIN_SENHA", "").strip()   # senha da página /admin/feedbacks ("" = página desligada)
DADOS = os.environ.get("CLIPADOR_DADOS_DIR") or BASE        # onde ficam os feedbacks (use um disco persistente)
os.makedirs(DADOS, exist_ok=True)
ARQ_FEEDBACK = os.path.join(DADOS, "feedback.jsonl")
FEEDBACK_MAX_BYTES = 5 * 1024 * 1024                        # a caixa de feedbacks para de aceitar acima disso

HOSTS_KICK = {"kick.com", "kzl.ink"}
HOSTS_ENCURTADOR = {"kzl.ink"}          # só estes são resolvidos; o destino final TEM de ser kick.com
ESTADOS_FINAIS = ("fim", "erro")
FORMATOS = ("original", "vertical_blur", "vertical_webcam")
RE_SLUG = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
# ids novos têm 22 caracteres (128 bits, impossível de adivinhar); o formato antigo de 8 hex segue aceito só na limpeza
RE_JOB_ID = re.compile(r"^[A-Za-z0-9_-]{22}$")
RE_PASTA_JOB = re.compile(r"^([0-9a-f]{8}|[A-Za-z0-9_-]{22})$")

# ---------- layout vertical com webcam: webcam de cada canal ----------
# A posição da webcam é diferente em cada streamer, por isso fica numa lista por canal:
# (x, y, largura, altura) como fração do quadro (0 a 1). Para incluir um canal sem mexer no código, defina
# CLIPADOR_WEBCAMS='{"outrocanal": [0.75, 0.15, 0.23, 0.22]}'. Canais fora da lista saem no vertical desfocado.
WEBCAMS = {
    "gabepeixe": (0.757, 0.152, 0.233, 0.223),   # webcam no canto superior direito
}
try:
    for _c, _v in json.loads(os.environ.get("CLIPADOR_WEBCAMS", "") or "{}").items():
        _x, _y, _w, _h = [float(n) for n in _v]
        if RE_SLUG.match(str(_c)) and 0 <= _x < 1 and 0 <= _y < 1 and 0 < _w <= 1 and 0 < _h <= 1:
            WEBCAMS[str(_c).lower()] = (_x, _y, _w, _h)
except (ValueError, TypeError):
    pass


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
THREADS_FFMPEG = max(1, _env_int("CLIPADOR_FFMPEG_THREADS", 1))

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

# Fundo do site (1280x720, já desfocado e escurecido para não competir com o conteúdo).
FUNDO_JPG = base64.b64decode(
    "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"
    "QrEAAg0DaYJoTAAAAAAAAAAABgAACGIRgQAAAAFCYqGIhpQEAAAABaAABIMYaGmcukueTJm9PoOze8H0quNdjI6KsyYjPj7OQ4ps"
    "zno6cdfN5sOfbPp016cLxzq+bSTpzMc4d82u975PLOZlLr110xWc6c85b6NJ9Oumue3PlVIxz+iLPneSVcmHF38vXXm83o4ev08M"
    "dePbtirnW5AtGgvbnvOe3Tivlx6p54NcJjp1EG9gFoBINMYORDBNMGmjacjESNCCWrqVS1pAUAKAABIAA0DaAAAAABMAAAAGmAEg"
    "ACaoAUAGAACAyBiBCoBKAAAAAAWgEgAO4qN9ubTnz2yWcIi+u+3s4+m6bjStrVQZ1jWKlHRjWBzpac+fRarzeXDPbLpupaWNFory"
    "3M55dNrtwz7FJwLtz3vkjfHe80102UnGumOmOetRtz5fSorwc5nWM4xz3m8+Xn7sOl87n7ub0+jjy6cfR6MlS30TGAyRNKmkKAWg"
    "AAABI2mNpyCaAAY3MpskSFaCLQEomqQ0oBaAAAAAASDQMAAAAAEMAAAYAAgACaUAAAYgYCMSKSFBMQwQwQwQAAAAA0DRVEkUkD3x"
    "6Tr6Mtibym3qnmxk7J47t2WUyXzVEkbrfly0bfHhktk1FNDrNG5zI63xi9q4yTrz551Xy649uuab6bTqpJ1NcYrono4efr6PF6L6"
    "PYfn9HHGsNcPNnh0Za5cfN38/frw4dnP6PTzzrPXtFOhGhnOE7Za3AzWkNABaAAADTkbljaqROnMptSEk2ktXQCpiFaAE0AFoAAA"
    "AAAAEg0DEwBAADTAAAAAAAAQ0ANAxMAAQAANpgCAAAAAENAAAFoAAAAF9fJud65szXPCTozhyVc65yVV88RVLGKvCZOmeSNXrjkn"
    "W+qOdb1usC3VZFupkGpkzR5tKQQrmk00jo58lpWnLk9405cfMeJ6vd19Pn3l6mvlbcefpHLrw8zw6M3Pg5vQw9Hfhntz69uV6zrS"
    "aCctZ1rIt63mtpMlc60gLQAAchSodJ5zRMlTKpiFAATVomgAAABghoALQAAAAAAAJAAAAABoGIAAAAAAAAAAAAAAaBgAAAMQIaAa"
    "BQCgAAAAAAd5uTSZQ2rHrN88aXjOc7zzxb0Rgt61nMtpItYADBDCRghoGmOlcy6vTnz530kmXSt+fKtHXDgrdZz83WVfR+n0PKsZ"
    "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"
)
FUNDO_ETAG = hashlib.md5(FUNDO_JPG).hexdigest()[:16]

# ---- Pix (botão "Ajude a manter o site no ar") ----
PIX_CHAVE = "d0643ebf-af79-43ca-9da1-6036593d011b"      # chave aleatória
PIX_NOME = "CLIPES DAS LIVES"         # nome que aparece no Pix (até 25 caracteres, sem acento)
PIX_CIDADE = "BRASIL"                  # cidade (até 15 caracteres, sem acento)
# QR Code desse Pix, gerado já pronto (SVG). Se você mudar a chave, o nome ou a cidade, o QR abaixo deixa de valer:
# o site detecta isso sozinho e mostra só o "copia e cola" (que é sempre recalculado) até você trocar o QR.
PIX_QR_PARA = "00020126580014br.gov.bcb.pix0136d0643ebf-af79-43ca-9da1-6036593d011b5204000053039865802BR5916CLIPES DAS LIVES6006BRASIL62070503***63045A42"
PIX_QR_SVG = (
    '<svg viewBox="0 0 57 57" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="QR Code Pix" shape-renderin'
    'g="crispEdges"><rect width="57" height="57" fill="#fff"/><path fill="#000" d="M6 6h7v1h-7zM14 6h2v1h-2zM17 6h3'
    'v1h-3zM21 6h1v1h-1zM24 6h2v1h-2zM27 6h1v1h-1zM33 6h2v1h-2zM36 6h1v1h-1zM39 6h1v1h-1zM42 6h1v1h-1zM44 6h7v1h-7z'
    'M6 7h1v1h-1zM12 7h1v1h-1zM16 7h1v1h-1zM18 7h4v1h-4zM23 7h1v1h-1zM25 7h4v1h-4zM33 7h1v1h-1zM36 7h1v1h-1zM38 7h2'
    'v1h-2zM41 7h1v1h-1zM44 7h1v1h-1zM50 7h1v1h-1zM6 8h1v1h-1zM8 8h3v1h-3zM12 8h1v1h-1zM14 8h1v1h-1zM16 8h1v1h-1zM1'
    '8 8h1v1h-1zM22 8h1v1h-1zM28 8h5v1h-5zM34 8h2v1h-2zM41 8h1v1h-1zM44 8h1v1h-1zM46 8h3v1h-3zM50 8h1v1h-1zM6 9h1v1'
    'h-1zM8 9h3v1h-3zM12 9h1v1h-1zM14 9h5v1h-5zM22 9h1v1h-1zM24 9h5v1h-5zM30 9h2v1h-2zM37 9h1v1h-1zM41 9h2v1h-2zM44'
    ' 9h1v1h-1zM46 9h3v1h-3zM50 9h1v1h-1zM6 10h1v1h-1zM8 10h3v1h-3zM12 10h1v1h-1zM14 10h1v1h-1zM17 10h3v1h-3zM24 10'
    'h1v1h-1zM26 10h9v1h-9zM37 10h2v1h-2zM40 10h3v1h-3zM44 10h1v1h-1zM46 10h3v1h-3zM50 10h1v1h-1zM6 11h1v1h-1zM12 1'
    '1h1v1h-1zM17 11h1v1h-1zM20 11h1v1h-1zM25 11h2v1h-2zM30 11h2v1h-2zM34 11h1v1h-1zM37 11h3v1h-3zM44 11h1v1h-1zM50'
    ' 11h1v1h-1zM6 12h7v1h-7zM14 12h1v1h-1zM16 12h1v1h-1zM18 12h1v1h-1zM20 12h1v1h-1zM22 12h1v1h-1zM24 12h1v1h-1zM2'
    '6 12h1v1h-1zM28 12h1v1h-1zM30 12h1v1h-1zM32 12h1v1h-1zM34 12h1v1h-1zM36 12h1v1h-1zM38 12h1v1h-1zM40 12h1v1h-1z'
    'M42 12h1v1h-1zM44 12h7v1h-7zM26 13h1v1h-1zM30 13h2v1h-2zM34 13h2v1h-2zM37 13h1v1h-1zM39 13h4v1h-4zM6 14h4v1h-4'
    'zM12 14h1v1h-1zM14 14h3v1h-3zM18 14h1v1h-1zM20 14h1v1h-1zM22 14h1v1h-1zM26 14h5v1h-5zM33 14h3v1h-3zM37 14h1v1h'
    '-1zM39 14h1v1h-1zM42 14h2v1h-2zM46 14h3v1h-3zM50 14h1v1h-1zM6 15h2v1h-2zM10 15h2v1h-2zM13 15h3v1h-3zM17 15h2v1'
    'h-2zM22 15h2v1h-2zM25 15h1v1h-1zM27 15h1v1h-1zM29 15h1v1h-1zM31 15h1v1h-1zM33 15h2v1h-2zM36 15h1v1h-1zM41 15h2'
    'v1h-2zM45 15h1v1h-1zM49 15h2v1h-2zM9 16h1v1h-1zM12 16h5v1h-5zM18 16h1v1h-1zM20 16h1v1h-1zM22 16h2v1h-2zM25 16h'
    '1v1h-1zM27 16h1v1h-1zM29 16h1v1h-1zM36 16h1v1h-1zM39 16h2v1h-2zM42 16h2v1h-2zM50 16h1v1h-1zM8 17h1v1h-1zM10 17'
    'h2v1h-2zM13 17h2v1h-2zM16 17h1v1h-1zM18 17h4v1h-4zM23 17h3v1h-3zM28 17h1v1h-1zM32 17h4v1h-4zM37 17h1v1h-1zM40 '
    '17h2v1h-2zM43 17h1v1h-1zM45 17h2v1h-2zM49 17h1v1h-1zM6 18h2v1h-2zM12 18h3v1h-3zM16 18h2v1h-2zM21 18h2v1h-2zM24'
    ' 18h1v1h-1zM27 18h2v1h-2zM30 18h1v1h-1zM35 18h3v1h-3zM39 18h1v1h-1zM42 18h1v1h-1zM44 18h1v1h-1zM47 18h1v1h-1zM'
    '6 19h4v1h-4zM11 19h1v1h-1zM13 19h7v1h-7zM21 19h1v1h-1zM24 19h3v1h-3zM28 19h3v1h-3zM32 19h2v1h-2zM38 19h2v1h-2z'
    'M44 19h1v1h-1zM46 19h2v1h-2zM49 19h1v1h-1zM7 20h1v1h-1zM11 20h2v1h-2zM14 20h6v1h-6zM21 20h3v1h-3zM26 20h1v1h-1'
    'zM29 20h1v1h-1zM32 20h1v1h-1zM34 20h2v1h-2zM38 20h1v1h-1zM40 20h1v1h-1zM44 20h1v1h-1zM46 20h1v1h-1zM48 20h2v1h'
    '-2zM8 21h1v1h-1zM10 21h1v1h-1zM15 21h1v1h-1zM18 21h2v1h-2zM21 21h1v1h-1zM24 21h1v1h-1zM27 21h1v1h-1zM29 21h1v1'
    'h-1zM33 21h2v1h-2zM36 21h3v1h-3zM41 21h1v1h-1zM46 21h1v1h-1zM48 21h3v1h-3zM6 22h1v1h-1zM8 22h3v1h-3zM12 22h4v1'
    'h-4zM17 22h2v1h-2zM20 22h2v1h-2zM23 22h1v1h-1zM25 22h2v1h-2zM28 22h2v1h-2zM31 22h3v1h-3zM36 22h3v1h-3zM40 22h3'
    'v1h-3zM44 22h1v1h-1zM48 22h1v1h-1zM50 22h1v1h-1zM8 23h2v1h-2zM14 23h1v1h-1zM16 23h1v1h-1zM22 23h3v1h-3zM26 23h'
    '2v1h-2zM29 23h2v1h-2zM33 23h2v1h-2zM40 23h2v1h-2zM43 23h1v1h-1zM46 23h1v1h-1zM48 23h1v1h-1zM50 23h1v1h-1zM6 24'
    'h7v1h-7zM15 24h1v1h-1zM17 24h1v1h-1zM23 24h4v1h-4zM30 24h1v1h-1zM32 24h1v1h-1zM35 24h1v1h-1zM38 24h3v1h-3zM43 '
    '24h4v1h-4zM48 24h1v1h-1zM6 25h5v1h-5zM13 25h6v1h-6zM20 25h1v1h-1zM23 25h2v1h-2zM26 25h1v1h-1zM31 25h2v1h-2zM35'
    ' 25h1v1h-1zM38 25h2v1h-2zM42 25h3v1h-3zM47 25h1v1h-1zM50 25h1v1h-1zM6 26h3v1h-3zM10 26h7v1h-7zM19 26h1v1h-1zM2'
    '1 26h1v1h-1zM25 26h12v1h-12zM39 26h2v1h-2zM42 26h8v1h-8zM6 27h1v1h-1zM8 27h1v1h-1zM10 27h1v1h-1zM14 27h2v1h-2z'
    'M18 27h4v1h-4zM23 27h1v1h-1zM26 27h1v1h-1zM30 27h1v1h-1zM33 27h2v1h-2zM36 27h1v1h-1zM42 27h1v1h-1zM46 27h1v1h-'
    '1zM48 27h3v1h-3zM6 28h1v1h-1zM8 28h1v1h-1zM10 28h1v1h-1zM12 28h1v1h-1zM14 28h4v1h-4zM19 28h1v1h-1zM21 28h1v1h-'
    '1zM23 28h1v1h-1zM26 28h1v1h-1zM28 28h1v1h-1zM30 28h2v1h-2zM36 28h1v1h-1zM38 28h2v1h-2zM42 28h1v1h-1zM44 28h1v1'
    'h-1zM46 28h2v1h-2zM50 28h1v1h-1zM7 29h1v1h-1zM10 29h1v1h-1zM14 29h4v1h-4zM19 29h2v1h-2zM22 29h1v1h-1zM24 29h3v'
    '1h-3zM30 29h1v1h-1zM35 29h1v1h-1zM37 29h3v1h-3zM42 29h1v1h-1zM46 29h1v1h-1zM49 29h1v1h-1zM6 30h1v1h-1zM9 30h6v'
    '1h-6zM17 30h5v1h-5zM24 30h1v1h-1zM26 30h5v1h-5zM32 30h1v1h-1zM37 30h1v1h-1zM39 30h9v1h-9zM6 31h2v1h-2zM10 31h1'
    'v1h-1zM16 31h2v1h-2zM20 31h4v1h-4zM25 31h4v1h-4zM32 31h3v1h-3zM38 31h2v1h-2zM43 31h3v1h-3zM49 31h1v1h-1zM6 32h'
    '1v1h-1zM9 32h1v1h-1zM12 32h1v1h-1zM14 32h1v1h-1zM17 32h1v1h-1zM23 32h1v1h-1zM25 32h2v1h-2zM29 32h2v1h-2zM32 32'
    'h1v1h-1zM34 32h1v1h-1zM36 32h2v1h-2zM43 32h1v1h-1zM48 32h2v1h-2zM11 33h1v1h-1zM13 33h1v1h-1zM15 33h1v1h-1zM18 '
    '33h1v1h-1zM22 33h3v1h-3zM27 33h2v1h-2zM30 33h1v1h-1zM35 33h2v1h-2zM38 33h2v1h-2zM41 33h5v1h-5zM48 33h1v1h-1zM6'
    ' 34h1v1h-1zM11 34h2v1h-2zM14 34h1v1h-1zM16 34h1v1h-1zM18 34h1v1h-1zM20 34h4v1h-4zM29 34h1v1h-1zM33 34h1v1h-1zM'
    '35 34h2v1h-2zM38 34h4v1h-4zM44 34h3v1h-3zM48 34h1v1h-1zM50 34h1v1h-1zM6 35h2v1h-2zM9 35h1v1h-1zM13 35h2v1h-2zM'
    '16 35h1v1h-1zM20 35h1v1h-1zM22 35h3v1h-3zM29 35h1v1h-1zM31 35h1v1h-1zM38 35h1v1h-1zM40 35h2v1h-2zM46 35h1v1h-1'
    'zM50 35h1v1h-1zM6 36h1v1h-1zM8 36h1v1h-1zM12 36h1v1h-1zM15 36h1v1h-1zM18 36h1v1h-1zM21 36h1v1h-1zM23 36h1v1h-1'
    'zM26 36h4v1h-4zM31 36h3v1h-3zM35 36h1v1h-1zM39 36h2v1h-2zM42 36h1v1h-1zM47 36h2v1h-2zM8 37h3v1h-3zM13 37h1v1h-'
    '1zM15 37h1v1h-1zM17 37h1v1h-1zM19 37h2v1h-2zM23 37h5v1h-5zM29 37h1v1h-1zM31 37h4v1h-4zM37 37h1v1h-1zM41 37h3v1'
    'h-3zM45 37h1v1h-1zM47 37h1v1h-1zM49 37h1v1h-1zM6 38h2v1h-2zM9 38h2v1h-2zM12 38h3v1h-3zM19 38h1v1h-1zM21 38h8v1'
    'h-8zM32 38h1v1h-1zM34 38h1v1h-1zM43 38h3v1h-3zM47 38h3v1h-3zM6 39h1v1h-1zM9 39h2v1h-2zM17 39h1v1h-1zM19 39h1v1'
    'h-1zM21 39h1v1h-1zM24 39h2v1h-2zM27 39h2v1h-2zM30 39h1v1h-1zM33 39h1v1h-1zM36 39h1v1h-1zM38 39h1v1h-1zM41 39h1'
    'v1h-1zM43 39h1v1h-1zM45 39h1v1h-1zM47 39h4v1h-4zM10 40h1v1h-1zM12 40h1v1h-1zM16 40h3v1h-3zM20 40h2v1h-2zM23 40'
    'h2v1h-2zM28 40h1v1h-1zM30 40h2v1h-2zM33 40h1v1h-1zM36 40h2v1h-2zM40 40h3v1h-3zM44 40h1v1h-1zM50 40h1v1h-1zM7 4'
    '1h4v1h-4zM13 41h2v1h-2zM16 41h4v1h-4zM22 41h2v1h-2zM25 41h1v1h-1zM31 41h1v1h-1zM34 41h1v1h-1zM40 41h3v1h-3zM44'
    ' 41h1v1h-1zM46 41h1v1h-1zM49 41h2v1h-2zM6 42h1v1h-1zM9 42h2v1h-2zM12 42h1v1h-1zM14 42h4v1h-4zM22 42h1v1h-1zM24'
    ' 42h1v1h-1zM26 42h5v1h-5zM36 42h2v1h-2zM39 42h1v1h-1zM42 42h6v1h-6zM50 42h1v1h-1zM14 43h1v1h-1zM16 43h2v1h-2zM'
    '19 43h1v1h-1zM24 43h3v1h-3zM30 43h1v1h-1zM32 43h1v1h-1zM34 43h2v1h-2zM37 43h3v1h-3zM41 43h2v1h-2zM46 43h2v1h-2'
    'zM49 43h1v1h-1zM6 44h7v1h-7zM16 44h1v1h-1zM20 44h1v1h-1zM22 44h1v1h-1zM26 44h1v1h-1zM28 44h1v1h-1zM30 44h1v1h-'
    '1zM32 44h6v1h-6zM40 44h3v1h-3zM44 44h1v1h-1zM46 44h1v1h-1zM48 44h2v1h-2zM6 45h1v1h-1zM12 45h1v1h-1zM15 45h2v1h'
    '-2zM18 45h1v1h-1zM20 45h1v1h-1zM23 45h2v1h-2zM26 45h1v1h-1zM30 45h1v1h-1zM32 45h1v1h-1zM34 45h1v1h-1zM36 45h1v'
    '1h-1zM38 45h2v1h-2zM42 45h1v1h-1zM46 45h1v1h-1zM48 45h2v1h-2zM6 46h1v1h-1zM8 46h3v1h-3zM12 46h1v1h-1zM16 46h1v'
    '1h-1zM19 46h1v1h-1zM23 46h1v1h-1zM26 46h7v1h-7zM35 46h1v1h-1zM38 46h9v1h-9zM48 46h3v1h-3zM6 47h1v1h-1zM8 47h3v'
    '1h-3zM12 47h1v1h-1zM14 47h3v1h-3zM19 47h1v1h-1zM21 47h1v1h-1zM23 47h1v1h-1zM25 47h2v1h-2zM28 47h2v1h-2zM31 47h'
    '1v1h-1zM37 47h6v1h-6zM45 47h1v1h-1zM48 47h2v1h-2zM6 48h1v1h-1zM8 48h3v1h-3zM12 48h1v1h-1zM14 48h2v1h-2zM17 48h'
    '2v1h-2zM21 48h2v1h-2zM24 48h2v1h-2zM28 48h2v1h-2zM32 48h1v1h-1zM35 48h1v1h-1zM39 48h2v1h-2zM42 48h1v1h-1zM45 4'
    '8h1v1h-1zM48 48h1v1h-1zM50 48h1v1h-1zM6 49h1v1h-1zM12 49h1v1h-1zM14 49h4v1h-4zM20 49h3v1h-3zM24 49h1v1h-1zM27 '
    '49h1v1h-1zM30 49h6v1h-6zM38 49h2v1h-2zM43 49h2v1h-2zM46 49h2v1h-2zM50 49h1v1h-1zM6 50h7v1h-7zM14 50h2v1h-2zM17'
    ' 50h3v1h-3zM23 50h1v1h-1zM28 50h1v1h-1zM32 50h1v1h-1zM34 50h1v1h-1zM40 50h1v1h-1zM43 50h6v1h-6z"/></svg>'
)


def _tlv(id_, valor):
    return "%s%02d%s" % (id_, len(valor), valor)


def _crc16(texto):
    crc = 0xFFFF
    for b in texto.encode():
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return "%04X" % crc


def pix_copia_e_cola(chave=PIX_CHAVE, nome=PIX_NOME, cidade=PIX_CIDADE):
    """Código "Pix copia e cola" (BR Code estático, sem valor: a pessoa escolhe quanto doar)."""
    p = (_tlv("00", "01") + _tlv("26", _tlv("00", "br.gov.bcb.pix") + _tlv("01", chave)) + _tlv("52", "0000")
         + _tlv("53", "986") + _tlv("58", "BR") + _tlv("59", nome[:25]) + _tlv("60", cidade[:15])
         + _tlv("62", _tlv("05", "***")) + "6304")
    return p + _crc16(p)


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
  /* fundo desfocado e fixo; a opacidade controla o quanto ele aparece (0 = some, 1 = total) */
  body::before {
    content: ''; position: fixed; inset: 0; z-index: -1; pointer-events: none;
    background: url('/fundo.jpg') center / cover no-repeat; opacity: .38;
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

  .topbar-inner { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
  .btn-pix {
    display: inline-flex; align-items: center; gap: 8px; max-width: 200px; padding: 11px 16px;
    border: 0; border-radius: 99px; cursor: pointer; text-align: left; line-height: 1.2;
    background: var(--grad); color: #0b0c12; font-weight: 800; font-size: .84rem;
    box-shadow: 0 0 22px rgba(88, 232, 121, .35); transition: transform .12s ease, filter .15s ease;
  }
  .btn-pix:hover { filter: brightness(1.08); transform: translateY(-1px); }
  .btn-pix i { font-style: normal; font-size: 1.05rem; }
  .pix-modal { text-align: center; }
  .pix-qr { width: 230px; max-width: 100%; margin: 14px auto 6px; padding: 10px; border-radius: 14px; background: #fff; }
  .pix-qr svg { display: block; width: 100%; height: auto; }
  .pix-chave { margin: 10px 0 0; padding: 10px 12px; border: 1px dashed var(--borda-forte); border-radius: 10px; font-family: ui-monospace, Menlo, Consolas, monospace; font-size: .8rem; color: var(--texto); overflow-wrap: anywhere; user-select: all; }
  .pix-rotulo { margin: 14px 0 0; font-size: .8rem; font-weight: 700; color: var(--mutado); }
  .pix-botoes { display: grid; gap: 8px; margin-top: 12px; }
  .pix-botoes .btn-mini { margin-top: 0; }

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
  .fmt-cel[data-fmt="vertical_webcam"] .fmt-prev { width: 34px; height: 60px; background: linear-gradient(180deg, rgba(88, 232, 121, .45) 0 36%, rgba(88, 232, 121, .95) 36% 52%, rgba(24, 136, 63, .5) 52%); }
  .fmt-cel[data-fmt="vertical_webcam"] .fmt-prev i { display: none; }

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
      <button type="button" class="btn-pix" id="abrir-pix"><i aria-hidden="true">♥</i><span>Ajude a manter o site no ar</span></button>
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
                <option value="vertical_webcam">Vertical com webcam e frase do chat</option>
              </select>
            </div>
          </div>
          <div class="campo fmt-linha">
            <div class="fmt-cel" id="fmt-cel" data-fmt="original" aria-hidden="true"><div class="fmt-prev"><i></i></div></div>
            <p class="dica">Horizontal é o mais rápido, mas o início do corte pode variar alguns segundos. Vertical 9:16 reencoda o vídeo e demora mais; o modo desfocado preenche as laterais sem cortar a imagem. O modo com webcam põe a câmera em cima, um título no meio e o jogo embaixo (só em canais já configurados: __WEBCAMS__; nos outros sai o desfocado).</p>
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
  <div class="modal-fundo" id="pix" role="dialog" aria-modal="true" aria-labelledby="pix-titulo">
    <div class="modal pix-modal">
      <h2 id="pix-titulo">Ajude a manter o site no ar</h2>
      <p>O Clipador é gratuito, mas o servidor tem custo. Qualquer valor ajuda. Obrigado! 💚</p>
      <div class="pix-qr"><!--PIX_QR--></div>
      <p class="pix-rotulo">Chave Pix (aleatória)</p>
      <div class="pix-chave">__PIXCHAVE__</div>
      <div class="pix-botoes">
        <button type="button" class="btn-mini" id="copiar-pix-chave" data-copiar="__PIXCHAVE__">Copiar chave Pix</button>
        <button type="button" class="btn-mini sec" id="copiar-pix-codigo" data-copiar="__PIXCOPIA__">Copiar Pix copia e cola</button>
        <button type="button" class="btn-mini sec" id="fechar-pix">Fechar</button>
      </div>
    </div>
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

// ---------- Pix ----------
async function copiarTexto(texto, aviso) {
  try {
    await navigator.clipboard.writeText(texto);
  } catch (e) {
    const ta = document.createElement('textarea');
    ta.value = texto;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (e2) {}
    document.body.removeChild(ta);
    if (!ok) { toast('Não consegui copiar. Segure a chave para copiar na mão.'); return; }
  }
  toast(aviso);
}
let focoPix = null;
function abrirPix() { focoPix = document.activeElement; $('pix').style.display = 'flex'; $('copiar-pix-chave').focus(); }
function fecharPix() {
  $('pix').style.display = 'none';
  if (focoPix && focoPix.focus) { focoPix.focus(); }
}
$('abrir-pix').addEventListener('click', abrirPix);
$('fechar-pix').addEventListener('click', fecharPix);
$('pix').addEventListener('click', function (e) { if (e.target === this) { fecharPix(); } });
document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && $('pix').style.display === 'flex') { fecharPix(); } });
$('copiar-pix-chave').addEventListener('click', function () { copiarTexto(this.dataset.copiar, 'Chave Pix copiada. Obrigado pelo apoio!'); });
$('copiar-pix-codigo').addEventListener('click', function () { copiarTexto(this.dataset.copiar, 'Pix copia e cola copiado. Obrigado pelo apoio!'); });

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
    html = html.replace("__WEBCAMS__", escape(", ".join(sorted(WEBCAMS))))
    contato = ""
    if CONTATO:
        c = escape(CONTATO, quote=True)
        contato = ' Contato: <a href="mailto:%s">%s</a>.' % (c, c)
    teste = ('<button type="button" class="btn-mini sec" id="btn-testar">'
             'Testar se o servidor lê a Kick</button>') if TESTE_ATIVO else ""
    pix = pix_copia_e_cola()
    if PIX_QR_PARA == pix:
        qr = PIX_QR_SVG
    else:
        qr = ""
        log.warning("O QR do Pix não bate com a chave/nome/cidade atuais: mostrando só o copia e cola. Gere o QR de novo.")
    html = html.replace("<!--PIX_QR-->", qr).replace("__PIXCOPIA__", escape(pix, quote=True))
    html = html.replace("__PIXCHAVE__", escape(PIX_CHAVE, quote=True))
    html = html.replace("<!--TESTE-->", teste).replace("__MAXMIN__", str(MAX_CAPTURA_MIN))
    return html.replace("<!--CONTATO-->", contato)


PAGINA = _montar_pagina()

JOBS = {}
JOBS_LOCK = threading.Lock()
VAGAS = threading.BoundedSemaphore(MAX_SIMULTANEOS)           # análise de VOD e cortes
VAGAS_CAPTURA = threading.BoundedSemaphore(MAX_CAPTURAS)      # captura ao vivo (copia o vídeo, quase não usa CPU)
VAGAS_VERTICAL = threading.BoundedSemaphore(MAX_VERTICAL)     # cada reencode para vertical, um de cada vez


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
    if request.path in ("/logo.jpg", "/fundo.jpg"):
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


_DISCO_LOCK = threading.Lock()


def _bytes_em_uso():
    agora = time.time()
    if agora < _DISCO["ate"]:
        return _DISCO["bytes"]
    if not _DISCO_LOCK.acquire(blocking=False):   # outra requisição já está somando: usa o último valor
        return _DISCO["bytes"]
    try:
        total = 0
        for raiz, _, arquivos in os.walk(BASE):
            for nome in arquivos:
                try:
                    total += os.path.getsize(os.path.join(raiz, nome))
                except OSError:
                    pass
        _DISCO["bytes"], _DISCO["ate"] = total, time.time() + 10   # recalcula no máximo a cada 10 s
        return total
    finally:
        _DISCO_LOCK.release()


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

# ============================== COTA, ORÇAMENTO, ESTATÍSTICAS E CACHE ==============================

def _txt_janela():
    m = COTA_JANELA_S // 60
    return "por hora" if m == 60 else "a cada %d min" % m


# ---------- cota de clipes por visitante (janela deslizante, em memória) ----------
_COTA = {}   # ip -> [[instante, quantidade], ...] em ordem cronológica
_COTA_LOCK = threading.Lock()


def _cota_limpar_locked(ip, agora):
    lista = [e for e in _COTA.get(ip, []) if agora - e[0] < COTA_JANELA_S]
    if lista:
        _COTA[ip] = lista
    else:
        _COTA.pop(ip, None)
    return lista


def cota_reservar(ip, qtd):
    """Reserva `qtd` clipes para o visitante. Retorna (ok, usados_na_janela, segundos_até_liberar)."""
    if COTA_CLIPES <= 0:
        return True, 0, 0
    agora = time.time()
    with _COTA_LOCK:
        if len(_COTA) > 20000:
            for k in list(_COTA):
                _cota_limpar_locked(k, agora)
        lista = _cota_limpar_locked(ip, agora)
        usados = sum(e[1] for e in lista)
        if qtd > COTA_CLIPES:
            return False, usados, 0
        if usados + qtd > COTA_CLIPES:
            precisa = usados + qtd - COTA_CLIPES   # quantos clipes antigos precisam "sair" da janela
            acum, espera = 0, COTA_JANELA_S
            for t, q in lista:
                acum += q
                if acum >= precisa:
                    espera = int(t + COTA_JANELA_S - agora) + 1
                    break
            return False, usados, max(1, espera)
        _COTA.setdefault(ip, []).append([agora, qtd])
    return True, usados, 0


def cota_devolver(ip, qtd):
    """Devolve clipes reservados que não chegaram a ser gerados (falha ou menos clipes que o pedido)."""
    if qtd <= 0 or COTA_CLIPES <= 0:
        return
    with _COTA_LOCK:
        lista = _COTA.get(ip, [])
        for e in reversed(lista):
            if qtd <= 0:
                break
            tira = min(e[1], qtd)
            e[1] -= tira
            qtd -= tira
        lista = [e for e in lista if e[1] > 0]
        if lista:
            _COTA[ip] = lista
        else:
            _COTA.pop(ip, None)


def erro_cota(qtd, usados, espera):
    if qtd > COTA_CLIPES:
        return ("Esse pedido geraria %d clipes, mas o limite é de %d clipes %s por pessoa. Peça menos clipes."
                % (qtd, COTA_CLIPES, _txt_janela()))
    quando = "daqui a uns %d min" % max(1, (espera + 59) // 60)
    livres = max(0, COTA_CLIPES - usados)
    if livres > 0:
        return ("Limite de %d clipes %s. Você ainda pode gerar %d agora; para pedir %d, tente %s."
                % (COTA_CLIPES, _txt_janela(), livres, qtd, quando))
    return "Você já gerou os %d clipes permitidos %s. Tente de novo %s." % (COTA_CLIPES, _txt_janela(), quando)


# ---------- estatísticas simples (só contagens; o visitante vira um código irreversível) ----------
ARQ_STATS = os.path.join(DADOS, "estatisticas.json")
ARQ_SAL = os.path.join(DADOS, "estatisticas.sal")
CAMPOS_STATS = ("visitas", "jobs_live", "jobs_vod", "jobs_mais", "clipes", "recusados")
_STATS = {}                    # "AAAA-MM-DD" -> {campo: n, "unicos": set(códigos)}
_STATS_LOCK = threading.Lock()
_STATS_ARQ_LOCK = threading.Lock()
_STATS_SALVO = [0.0]


def _carregar_sal():
    try:
        with open(ARQ_SAL, encoding="utf-8") as f:
            sal = f.read().strip()
        if len(sal) >= 16:
            return sal
    except OSError:
        pass
    sal = secrets.token_hex(16)
    try:
        with open(ARQ_SAL, "w", encoding="utf-8") as f:
            f.write(sal)
    except OSError:
        pass
    return sal


_SAL = _carregar_sal()


def _dia_hoje():
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() - 3 * 3600))   # dia no horário de Brasília


def _stats_dia_locked():
    dia = _dia_hoje()
    d = _STATS.get(dia)
    if d is None:
        d = {c: 0 for c in CAMPOS_STATS}
        d["unicos"] = set()
        _STATS[dia] = d
        for velho in sorted(_STATS)[:-30]:   # guarda só os últimos 30 dias
            del _STATS[velho]
    return d


def _stats_salvar(forcar=False):
    agora = time.time()
    with _STATS_LOCK:
        if not forcar and agora - _STATS_SALVO[0] < 30:
            return
        _STATS_SALVO[0] = agora
        copia = {dia: {**{k: v for k, v in d.items() if k != "unicos"}, "unicos": sorted(d["unicos"])}
                 for dia, d in _STATS.items()}
    with _STATS_ARQ_LOCK:
        tmp = ARQ_STATS + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(copia, f)
            os.replace(tmp, ARQ_STATS)
        except OSError:
            pass


def _stats_carregar():
    try:
        with open(ARQ_STATS, encoding="utf-8") as f:
            bruto = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(bruto, dict):
        return
    with _STATS_LOCK:
        for dia, d in bruto.items():
            try:
                novo = {c: int(d.get(c, 0) or 0) for c in CAMPOS_STATS}
                novo["unicos"] = set(str(x) for x in (d.get("unicos") or []))
                _STATS[str(dia)] = novo
            except Exception:
                continue


def stat_inc(campo, n=1):
    if n <= 0:
        return
    with _STATS_LOCK:
        _stats_dia_locked()[campo] += n
    _stats_salvar()


def stat_visita(ip):
    codigo = hashlib.sha256(("%s|%s|%s" % (_SAL, _dia_hoje(), ip)).encode("utf-8")).hexdigest()[:12]
    with _STATS_LOCK:
        d = _stats_dia_locked()
        d["visitas"] += 1
        if len(d["unicos"]) < 50000:
            d["unicos"].add(codigo)
    _stats_salvar()


def jobs_hoje():
    with _STATS_LOCK:
        d = _stats_dia_locked()
        return d["jobs_live"] + d["jobs_vod"] + d["jobs_mais"]


def orcamento_estourado():
    """Teto diário de tarefas no site todo: evita gastar o crédito do mês em poucos dias."""
    return MAX_JOBS_DIA > 0 and jobs_hoje() >= MAX_JOBS_DIA


_stats_carregar()


# ---------- cache da análise do chat (a parte mais cara de uma VOD) ----------
_CACHE_CHAT = {}   # (canal, uuid, duração) -> (expira, mensagens com tempo e texto)
_CACHE_CHAT_LOCK = threading.Lock()
CACHE_CHAT_MAX = 4


def cache_chat_get(chave):
    agora = time.time()
    with _CACHE_CHAT_LOCK:
        item = _CACHE_CHAT.get(chave)
        if item and item[0] > agora:
            return [dict(m) for m in item[1]]
        _CACHE_CHAT.pop(chave, None)
    return None


def cache_chat_set(chave, mensagens):
    if CACHE_CHAT_S <= 0 or not mensagens:
        return
    # Guarda só o necessário para montar as legendas dos clipes.
    compactas = [{"t": float(m["t"]), "texto": str(m.get("texto") or "")[:250]}
                 for m in mensagens if isinstance(m, dict) and m.get("texto")]
    with _CACHE_CHAT_LOCK:
        _CACHE_CHAT[chave] = (time.time() + CACHE_CHAT_S, compactas)
        while len(_CACHE_CHAT) > CACHE_CHAT_MAX:
            mais_velha = min(_CACHE_CHAT, key=lambda k: _CACHE_CHAT[k][0])
            del _CACHE_CHAT[mais_velha]


def atualizar_job(job_id, **campos):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if j is not None:
            j.update(campos)
            j["atualizado"] = time.time()


def job_ativo(j):
    return j["estado"] not in ESTADOS_FINAIS


def _checar_limites_locked(ip, disco_cheio_agora=False):
    """Chamar com JOBS_LOCK. Retorna mensagem de erro ou ''. (O disco é medido antes, fora do lock.)"""
    ativos = [j for j in JOBS.values() if job_ativo(j)]
    if len(ativos) >= MAX_ATIVOS_TOTAL:
        return "O servidor está cheio agora. Tente de novo em alguns minutos."
    if sum(1 for j in ativos if j.get("ip") == ip) >= MAX_ATIVOS_POR_IP:
        return "Você já tem %d tarefas em andamento. Espere uma terminar." % MAX_ATIVOS_POR_IP
    if disco_cheio_agora:
        return "O servidor está sem espaço no momento. Tente de novo em alguns minutos."
    return ""


def criar_job(tipo, cota, **extra):
    """Cria o job se couber nos limites e na cota de clipes do visitante. Retorna (job_id, erro).
    tipo: "live" ou "vod". cota: quantos clipes este pedido deve gerar."""
    ip = ip_cliente()
    if orcamento_estourado():
        stat_inc("recusados")
        return None, "O site atingiu o limite de uso de hoje. Volte amanhã."
    cheio = disco_cheio()   # percorre a pasta de clipes: fica fora do lock para não travar o resto do site
    erro, job_id = "", None
    with JOBS_LOCK:
        erro = _checar_limites_locked(ip, cheio)
        if not erro:
            ok, usados, espera = cota_reservar(ip, cota)
            if not ok:
                erro = erro_cota(cota, usados, espera)
        if not erro:
            job_id = secrets.token_urlsafe(16)   # 22 caracteres, 128 bits: quem não tem o link não acha o clipe
            while job_id in JOBS:
                job_id = secrets.token_urlsafe(16)
            job = {
                "estado": "iniciando", "msg": "Iniciando...", "clipes": 0, "diag": "", "pasta": "",
                "info": [], "picos": [], "usados": 0, "stream": "", "headers": {},
                "tamanho_clipe": SEG, "formato": "original",
                "ip": ip, "atualizado": time.time(),
                "cota": cota, "clipes_antes": 0,
            }
            job.update(extra)
            JOBS[job_id] = job
    if erro:
        stat_inc("recusados")
        return None, erro
    stat_inc("jobs_" + tipo)
    return job_id, ""


def _fechar_cota(job_id):
    """No fim do job, devolve à cota do visitante os clipes que ele pediu e não recebeu; conta os gerados."""
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            return
        cota = int(j.pop("cota", 0) or 0)
        ip = j.get("ip")
        novos = max(0, int(j.get("clipes") or 0) - int(j.get("clipes_antes") or 0))
    if cota:
        cota_devolver(ip, max(0, cota - novos))
    stat_inc("clipes", novos)


def rodar_job(job_id, alvo, *args):
    """Espera uma vaga, roda o trabalho e garante que o job nunca fique preso em 'gravando'.
    A captura ao vivo usa vagas próprias (ela só copia o vídeo e quase não gasta CPU)."""
    atualizar_job(job_id, estado="fila", msg="Na fila... esperando uma vaga.")
    if alvo is gravar:
        vaga = VAGAS_CAPTURA
    else:
        vaga = VAGAS
    try:
        with vaga:
            alvo(job_id, *args)
    except Exception as e:
        log.exception("job %s falhou", job_id)
        atualizar_job(job_id, estado="erro", msg="Erro inesperado no servidor.", diag=str(e)[-300:])
    finally:
        _fechar_cota(job_id)


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
            _stats_salvar(True)
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


# ============================== LAYOUT VERTICAL COM WEBCAM ==============================
# Webcam em cima, faixa verde com texto branco no meio e a tela do jogo embaixo (720x1280), com a marca do site.
LV_W, LV_H = 720, 1280
LV_CAM, LV_BANDA = 400, 150
LV_JOGO = LV_H - LV_CAM - LV_BANDA
COR_BANDA = "0x58E879"  # verde principal do site

# Fonte Poppins Bold (SIL Open Font License 1.1), reduzida às letras latinas, e a marca do site em círculo.
_FONTE_B64 = (
    "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"
    "fmUoLzEwFzAlcwMnVB8AAgA8AJYCBQJgABsAJwAAAAcXBycGIyInByc3JjU0Nyc3FzYzMhc3FwcWFQY2NTQmIyIGFRQWMwHy"
    "IzZCQCs3NyxAQjchIjhCQy00Mi1CQjYjsisqIB8qKh8BPS42Q0ASEkBDNy48Pi04Q0MREEJDNi4/UikoKCoqKCgpAAEABwAA"
    "ApgCvgAWAAABMxUHBzMVIxUjNSM1MycnNTMDMxMTMwIJZKAgwMiryMAgoGSPwoiHwAGpcwE9coaGcj0BcwEV/skBNwAAAgBp"
    "/38BFAMvAAMABwAAEzMRIxMjETNpq6urq6sDL/5m/eoBmgACAB7/IAItAsgALwA7AAAkFRQGBiMiJiczFjMyNTQmJicmJjU0"
    "NjcmNTQ2NjMyFhcjJiMiFRQWFhcWFhUUBgcmNjU0JiMiBhUUFjMCCTdoR2V4BKkENjgbPz9SVDM4RzdoR2V4BKkENjgbPz9S"
    "VDM4di0tJiYtLSYjUDNSLmdWRTEVICIbI1k9Mk8VO1AzUi5nVkUxFSAiGyNZPTFLGkgtISEtLSEhLQACAAoCbAFlAv8ACwAX"
    "AAASJjU0NjMyFhUUBiMyJjU0NjMyFhUUBiM0KiofICorH6kqKh8gKisfAmwqHx8rKiAfKiofHysqIB8qAAMAJv/5AuYCyAAP"
    "AB8AOgAAABYWFRQGBiMiJiY1NDY2Mw4CFRQWFjMyNjY1NCYmIw4CFRQWFjMyNjcjBiMiJjU0NjMyFhczJiYjAe2gWVmgZ2eg"
    "WVmgZ1mER0eEWVmER0eEWT9lOTllQE9yEJMRMiUqKiUZIweTEHFQAshbpGhopFxcpGhopFs4SolcW4pLS4pbXIlKTjdmRERm"
    "N1pKMTg2NjgaF0xYAAIAGQFdAbcCxwAQABwAABI2NjMyFzUzESM1BiMiJiY1FhYzMjY1NCYjIgYVGSxMLk4pgYEnTTBNLIUs"
    "ICAsLCAgLAJJUiwxLP6gKi8sUjcmKislJSorJQACAC0AeAIAAdYABQALAAA3JzczBxczJzczBxeBVFSeV1dDVFSeV1d4r6+v"
    "r6+vr68AAAEALwC0ApYBugAFAAABESM1ITUClqv+RAG6/vqEggD//wA0ARgB7AGmAAIADgAAAAQAJwEZAdoCyQAPABsAKQAw"
    "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"
    "BaoFtwXTBewF+wYXBi0GWQZ9Bq8G2AcWBygHSQddB3sHlQerB8AH0gfhB/MIBQgSCB8ITwh/CKcI1wkJCSkJaQmLCagJzgnl"
    "CfIKJgpHCnIKogrYCvALLwtMC28LgQufC7kLzwvjDCgMNAx5DKAMoAy9DOwNIQ1fDYQNlw3rDhAOZA6QDqkOuQ7BDwgPFQ89"
    "D1YPeg+vD7wP3w/8EAUQIhAxEF0QdhChENcRKBFfEWsRdxGDEY8RmxGnEcoSChIWEiISLhI6EkYSUhJeEmoSlhKiEq4SuhLG"
    "EtIS3hL4EzsTRxNTE18TaxN3E50T7RP5FAUUERQdFCkUNRSMFMgU1BTgFOwU9xUCFQ0VGBUjFWEVbRV5FYUVkRWcFagV1RYR"
    "Fh0WKRY1FkEWTRaAFowWmRapFs8W8xcAAAEAAAAEAQaISBYiXw889QADA+gAAAAA2KSpyAAAAADbFjbJ/c79ogmtBFQAAQAH"
    "AAIAAAAAAAAB9AAAANQAAAGIAF8BmwAoA4gAFAKSACwDZgAfAx4AJADdACgB3QBXAd0ABwIWAEoCdAA7AR8AGgJEADQBGgAo"
    "AcUACgKMAC0BeAAbAjsAJQJdACoCpQAxAooAVAJ9AD4CFwAcAogANgJnADQBHAAoAVsAMgInADkCuABEAh0APwIaABoEOABJ"
    "AuEAEAKTAD4C+gAhAtcAPgIdAD4CIwA+AvoAIQLbAD4BJwA+AkIAGgK5AD4B3QA+A5YAPgLwAD4DEgAhAnAAPgMUACECjAA+"
    "AmcAKgJPABgCwQA7AtoACQQcABkCywAYAp8ABwJUADIB/gB4AyIAtwH+AFoCxQAeAwwAVQEjAAoCpwAcAqcAPgJdABwCpwAc"
    "AmgAHAFoABECpwAcAqIAPgEnAC4BJv/hAmoAPgEnAD4EIwA+AqIAPgJ9ABwCpwA+AqcAHAGsAD4CLgAgAZYAFQKiADkCcgAJ"
    "A2IABAJMAAUCeP//AfAAHwHzAFsBIwA8AfMARwJ8ABwA1AAAAYgAXwJ3ACoClwAkAkEAPAKfAAcBfQBpAksAHgFvAAoDCwAm"
    "AdUAGQItAC0CzQAvAkQANAICACcBpAAKAc8AGQJ1ADwBfwAiAXYADwEFAAoCwQBLAsQAHAErADIBOQAKAPkAFAHKABkCLQAy"
    "AxIAKAM1ACgDewAyAhoAIgLhABAC4QAQAuEAEALhABAC4QAQAuEAEAO7AAYC+gAhAh0APgIdAD4CHQA+Ah0APgEnABcBJwAX"
    "ASf/+QEn/+YC3AAEAvAAPgMSACEDEgAhAxIAIQMSACEDEgAhAn0ARQMSABYCwQA7AsEAOwLBADsCwQA7Ap8ABwJwAD4C+AAx"
    "AqcAHAKnABwCpwAcAqcAHAKnABwCpwAcBC0AHAJdABwCaAAcAmgAHAJoABwCaAAcAUEAJAFBACQBQQAGAUH/8wKAABwCogA+"
    "An0AHAJ9ABwCfQAcAn0AHAJ9ABwCYwA1AnUADQKiADkCogA5AqIAOQKiADkCeP//AqcAPgJ4//8BQQBLAUoACgEGAAkBmgAF"
    "Ah8ANAABAAAEGv6iAGQJnf3O+RgJrQABAAAAAAAAAAAAAAAAAAAAxQAEA2kCvAAFAAACigJYAAAASwKKAlgAAAFeADIBTgAA"
    "AAAIAAAAAAAAAAAAAAMAAAAAAAAAAAAAAABJVEZPAKAAIAD/BBr+ogBkBG8CcwAAAAEAAAAAAi4CwQAAACAABAAAAAIAAAAD"
    "AAAAFAADAAEAAAAUAAQAKAAAAAYABAABAAIAfgD///8AAAAgAKD////h/8AAAQAAAAAAAAAAAAcAWgADAAEECQAAAKIAAAAD"
    "AAEECQABAA4AogADAAEECQACAAgAsAADAAEECQADADYAuAADAAEECQAEABgA7gADAAEECQAFAAoBBgADAAEECQAGABgBEABD"
    "AG8AcAB5AHIAaQBnAGgAdAAgADIAMAAyADAAIABUAGgAZQAgAFAAbwBwAHAAaQBuAHMAIABQAHIAbwBqAGUAYwB0ACAAQQB1"
    "AHQAaABvAHIAcwAgACgAaAB0AHQAcABzADoALwAvAGcAaQB0AGgAdQBiAC4AYwBvAG0ALwBpAHQAZgBvAHUAbgBkAHIAeQAv"
    "AFAAbwBwAHAAaQBuAHMAKQBQAG8AcABwAGkAbgBzAEIAbwBsAGQASQBUAEYATwA7ACAAUABvAHAAcABpAG4AcwAgAEIAbwBs"
    "AGQAOwAgADQALgAwADAANABiADgAUABvAHAAcABpAG4AcwAgAEIAbwBsAGQANAAuADAAMAA0AFAAbwBwAHAAaQBuAHMALQBC"
    "AG8AbABkAAAAAwAAAAAAAP+1ADIAAAAAAAAAAAAAAAAAAAAAAAAAAAABAAAACgAcAB4AAURGTFQACAAEAAAAAP//AAAAAAAA"
    "AAEAAAAKACwALgADREZMVAAUZGV2MgAeZGV2YQAeAAQAAAAA//8AAAAAAAAAAAAA"
)
_MARCA_B64 = (
    "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"
)

# frases da faixa: (título, gancho). {N} vira o nome do canal.
_TITULOS = (
    ("{N} SOLTOU ESSA E O CHAT EXPLODIU", "VOCÊ CONCORDA COM O CHAT?"),
    ("O CHAT NÃO ACREDITOU NO QUE {N} FEZ", "VOCÊ TAMBÉM FICOU CHOCADO?"),
    ("{N} FEZ ISSO E O CHAT ENLOUQUECEU", "COMENTA O QUE VOCÊ ACHOU"),
    ("NINGUÉM ESPERAVA ESSA DE {N}", "MARCA QUEM PRECISA VER ISSO"),
    ("O CHAT EXPLODIU COM ESSE MOMENTO", "FOI EXAGERO OU MERECIDO?"),
)

FONTE_TITULO = os.path.join(DADOS, "fonte_titulo.ttf")
MARCA_PNG = os.path.join(DADOS, "marca_site.png")
TEM_DRAWTEXT = False


def _preparar_layout_webcam():
    """Grava a fonte e a marca em disco e confere se este ffmpeg sabe escrever texto (drawtext)."""
    global TEM_DRAWTEXT
    try:
        for caminho, b64 in ((FONTE_TITULO, _FONTE_B64), (MARCA_PNG, _MARCA_B64)):
            dados = base64.b64decode(b64)
            if not os.path.isfile(caminho) or os.path.getsize(caminho) != len(dados):
                with open(caminho, "wb") as f:
                    f.write(dados)
        r = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True, timeout=15)
        TEM_DRAWTEXT = "drawtext" in r.stdout
    except Exception as e:
        log.warning("layout com webcam desativado: %s", str(e)[:200])
        TEM_DRAWTEXT = False
    if not TEM_DRAWTEXT:
        log.warning("Este ffmpeg não tem o filtro drawtext: o formato 'vertical com webcam' vai sair como vertical desfocado.")


def webcam_do_canal(canal):
    return WEBCAMS.get(str(canal or "").lower())


def formato_efetivo(formato, canal):
    """O vertical com webcam só existe para canais configurados e com drawtext; senão cai no desfocado."""
    if formato == "vertical_webcam" and not (TEM_DRAWTEXT and webcam_do_canal(canal)):
        return "vertical_blur"
    return formato


def _esc_filtro(txt):
    """Escapa um caminho/texto para dentro de aspas simples de um filtro do ffmpeg."""
    return str(txt).replace("\\", "\\\\").replace("'", "'\\''").replace(":", "\\:")


def _texto_faixa(canal, idx, frase=None):
    # A legenda principal vem do chat do intervalo do clipe; não inventa chamadas genéricas.
    if frase:
        return str(frase).strip()[:100], ""
    return "MENSAGEM DO CHAT", ""


def _filtros_faixa(canal, idx, frase=None):
    """Desenha a frase real do chat em até duas linhas, centralizada na faixa verde."""
    titulo, gancho = _texto_faixa(canal, idx, frase)
    for tam in (42, 36, 30, 26):
        linhas = textwrap.wrap(titulo, max(8, int(650 / (0.70 * tam)))) or [titulo]
        if len(linhas) <= 2:
            break
    linhas = linhas[:2]
    alt_titulo = len(linhas) * tam * 1.2
    topo = max(6, (LV_BANDA - alt_titulo) / 2)
    fonte = _esc_filtro(FONTE_TITULO)
    comuns = "fontfile='%s':expansion=none:x=(w-text_w)/2" % fonte
    filtros = []
    for k, linha in enumerate(linhas):
        filtros.append("drawtext=%s:text='%s':fontsize=%d:fontcolor=white:borderw=3:bordercolor=black:y=%.0f"
                       % (comuns, _esc_filtro(linha), tam, topo + k * tam * 1.2))
    return ",".join(filtros)


def _grafo_webcam(canal, idx, frase=None):
    cx, cy, cw, ch = webcam_do_canal(canal)
    cam = ("crop=iw*%.4f:ih*%.4f:iw*%.4f:ih*%.4f,scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d"
           % (cw, ch, cx, cy, LV_W, LV_CAM, LV_W, LV_CAM))
    larg = "ih*%d/%d" % (LV_W, LV_JOGO)       # largura do recorte do jogo, na proporção do espaço de baixo
    if cx + cw / 2 > 0.5:                       # webcam à direita: o recorte do jogo recua para não pegar a borda dela
        x = "max(0,min((iw-%s)/2,iw*%.4f-%s))" % (larg, cx, larg)
    else:                                       # webcam à esquerda
        x = "min(iw-%s,max((iw-%s)/2,iw*%.4f))" % (larg, larg, cx + cw)
    jogo = "crop=w='%s':h=ih:x='%s':y=0,scale=%d:%d" % (larg, x, LV_W, LV_JOGO)
    banda = ("scale=16:16,scale=%d:%d,drawbox=x=0:y=0:w=%d:h=%d:color=%s:t=fill,%s"
             % (LV_W, LV_BANDA, LV_W, LV_BANDA, COR_BANDA, _filtros_faixa(canal, idx, frase)))
    return (
        "[0:v]fps=30,split=3[s1][s2][s3];"
        "[s1]%s[cam];[s2]%s[banda];[s3]%s[jogo];"
        "[cam][banda][jogo]vstack=inputs=3[emp];"
        "movie='%s',format=rgba[wm];"
        "[emp][wm]overlay=W-w-16:16,setsar=1,format=yuv420p[outv]"
    ) % (cam, banda, jogo, _esc_filtro(MARCA_PNG))


_preparar_layout_webcam()


def args_formato(formato, leve=False, canal=None, idx=0, frase=None):
    """Argumentos de saída. Os formatos verticais geram MP4 H.264/AAC em 9:16."""
    formato = formato_efetivo(formato, canal)
    if formato == "vertical_webcam" and not leve:
        return ["-filter_complex", _grafo_webcam(canal, idx, frase),
                "-map", "[outv]", "-map", "0:a:0?",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-pix_fmt", "yuv420p", "-r", "30", "-vsync", "cfr",
                "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
                "-max_muxing_queue_size", "1024", "-movflags", "+faststart",
                "-threads", str(THREADS_FFMPEG), "-filter_threads", "1",
                "-filter_complex_threads", "1", "-f", "mp4"]
    if formato == "vertical_webcam":
        # Plano B quando a composição com webcam não cabe na memória.
        formato = "vertical_blur"
    if formato != "vertical_blur":
        return ["-c", "copy"]

    w, h = (540, 960) if leve else (VERT_W, VERT_H)
    threads = 1 if leve else THREADS_FFMPEG
    pw, ph = w // 5, h // 5
    filtro = (
        "fps=30,split=2[fundo][frente];"
        "[fundo]scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,boxblur=4:1,"
        "scale=%d:%d:flags=bilinear[bg];"
        "[frente]scale=%d:-2[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2:shortest=1,setsar=1,"
        "drawbox=x=0:y=(h-%d)/2:w=w:h=%d:color=%s:t=fill,%s,"
        "setsar=1,format=yuv420p[outv]"
    ) % (pw, ph, pw, ph, w, h, w, LV_BANDA, LV_BANDA, COR_BANDA,
         _filtros_faixa(canal, idx, frase))
    return ["-filter_complex", filtro, "-map", "[outv]", "-map", "0:a:0?",
            "-c:v", "libx264", "-preset", "ultrafast" if leve else "veryfast",
            "-crf", "25" if leve else "23", "-pix_fmt", "yuv420p",
            "-r", "30", "-vsync", "cfr", "-c:a", "aac", "-b:a", "128k",
            "-ar", "48000", "-max_muxing_queue_size", "1024",
            "-movflags", "+faststart", "-threads", str(threads),
            "-filter_threads", "1", "-filter_complex_threads", "1", "-f", "mp4"]


def converter_vertical(entrada, saida, formato, canal=None, idx=0):
    """Converte um trecho já gravado para vertical (fora do tempo real, então não depende da velocidade do servidor).
    Se o ffmpeg for morto por falta de memória, tenta de novo em qualidade reduzida."""
    erro = ""
    for leve in (False, True):
        threads = 1 if leve else THREADS_FFMPEG
        cmd = ["ffmpeg", "-nostdin", "-y", "-threads", str(threads), "-i", entrada,
               *args_formato(formato, leve, canal, idx), saida]
        try:
            with VAGAS_VERTICAL:
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
    canal_slug = slug_da_url(url) or ""
    atualizar_job(job_id, canal=canal_slug)
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
            ok, nota = converter_vertical(entrada, saida, formato, canal_slug, convertidos)
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
    """Percorre o chat da VOD. Retorna mensagens com segundo relativo e texto."""
    canal, erro = kick_get_json("https://kick.com/api/v2/channels/%s" % quote(canal_slug))
    if not canal or "id" not in canal:
        return [], "não consegui o id do canal (%s)" % erro
    cid = canal["id"]

    fim_epoch = inicio_epoch + duracao_s
    cursor = str(int(fim_epoch * 1_000_000))
    mensagens, vistos = [], set()
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
            return mensagens, ("falha na página %d: %s" % (paginas, erro)) if not mensagens else ""
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
                conteudo = m.get("content") or m.get("message") or m.get("text") or ""
                if isinstance(conteudo, dict):
                    conteudo = conteudo.get("content") or conteudo.get("text") or ""
                conteudo = re.sub(r"\s+", " ", str(conteudo)).strip()[:250]
                mensagens.append({"t": round(t - inicio_epoch, 3), "texto": conteudo})
                novas += 1
        paginas += 1
        proximo = bloco.get("cursor")
        if progresso and (paginas == 1 or paginas % 10 == 0):
            progresso(paginas, len(mensagens), menor)
        if not proximo or not msgs:
            break
        if menor is not None and menor < inicio_epoch:
            break
        sem_novas = sem_novas + 1 if novas == 0 else 0
        if sem_novas >= 30:
            break
        cursor = str(proximo)
        time.sleep(0.1)
    return mensagens, ""


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
    todos = {}
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

        mensagens, erro = baixar_chat_vod(canal_slug, inicio_epoch + a, b - a, prog)
        for m in mensagens:
            registro = {"t": round(a + float(m.get("t", 0)), 3), "texto": str(m.get("texto") or "")[:250]}
            chave = (registro["t"], registro["texto"])
            todos[chave] = registro
        paginas_antes += ultimas["pags"]
        lido_antes += b - a
        if erro:
            erros.append(erro)
    resultado = sorted(todos.values(), key=lambda m: m["t"])
    return resultado, ("; ".join(erros) if erros and not resultado else "")



def frase_mais_repetida_chat(mensagens, inicio, fim):
    """Escolhe a mensagem mais repetida dentro do intervalo do clipe."""
    grupos = {}
    for m in mensagens or []:
        try:
            t = float(m.get("t", -1))
        except (TypeError, ValueError):
            continue
        if not (inicio <= t <= fim):
            continue
        original = re.sub(r"\s+", " ", str(m.get("texto") or "")).strip()
        # Evita exibir links, mensagens vazias e spam de um único caractere.
        limpo = re.sub(r"https?://\S+", "", original, flags=re.I).strip()
        chave = re.sub(r"[^\wÀ-ÿ]+", " ", limpo.lower(), flags=re.UNICODE).strip()
        if len(chave) < 3:
            continue
        if chave not in grupos:
            grupos[chave] = {"qtd": 0, "texto": limpo}
        grupos[chave]["qtd"] += 1
    if not grupos:
        return "SEM MENSAGENS NO CHAT"
    # Mais repetida primeiro; em empate, prefere uma frase que comunique algo.
    melhor = max(grupos.values(), key=lambda g: (g["qtd"], min(len(g["texto"]), 90)))
    frase = melhor["texto"]
    if len(frase) > 100:
        frase = frase[:97].rsplit(" ", 1)[0] + "..."
    return frase.upper()

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


def cortar_clipe(job_id, idx, ini, formato, frase=None):
    """Corta um clipe da VOD usando os dados guardados no job. idx começa em 0."""
    with JOBS_LOCK:
        j = JOBS[job_id]
        pasta, stream = j["pasta"], j["stream"]
        headers, tam = j["headers"], j["tamanho_clipe"]
        canal = j.get("canal", "")

    user_agent, extra = cabecalhos_ffmpeg(headers)
    saida = os.path.join(pasta, "clipe_%03d.mp4" % idx)
    cmd = ["ffmpeg", "-nostdin", "-y", "-user_agent", user_agent, *extra,
           "-protocol_whitelist", PROTOCOLOS,
           "-rw_timeout", "20000000",
           *(["-threads", str(THREADS_FFMPEG)] if formato != "original" else []),
           "-ss", str(int(ini)), "-i", stream, "-t", str(tam),
           *args_formato(formato, canal=canal, idx=idx, frase=frase),
           "-avoid_negative_ts", "make_zero", saida]
    try:
        with (VAGAS_VERTICAL if formato != "original" else contextlib.nullcontext()):
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=240 if formato == "original" else 600)
    except subprocess.TimeoutExpired:
        _remover_arquivo(saida)
        return False, "demorou demais"
    if r.returncode == 0 and clipe_valido(saida):
        return True, ""
    _remover_arquivo(saida)  # não deixa arquivo pela metade entrar no ZIP
    return False, resumo_erro(r.stderr, r.returncode, 250)


def _pontuar_fala_streamer(texto):
    """Heurística conservadora para preferir falas completas e expressivas."""
    t = re.sub(r"\s+", " ", str(texto or "")).strip()
    if len(t) < 12:
        return -1
    palavras = re.findall(r"\w+", t, flags=re.UNICODE)
    if len(palavras) < 3:
        return -1
    score = min(len(palavras), 18) * 0.25
    score += min(len(t), 120) / 120
    score += sum(1.4 for c in "!?" if c in t)
    termos = ("caralho", "inacreditável", "impossível", "nunca", "agora", "ganhei",
              "perdi", "mentira", "sério", "olha", "calma", "meu deus", "não é possível",
              "que isso", "acabou", "consegui", "vingança", " absurdo")
    tl = t.lower()
    score += sum(1.0 for termo in termos if termo in tl)
    # Penaliza transcrições muito longas: a tarja precisa ser curta e legível.
    if len(t) > 115:
        score -= (len(t) - 115) / 35
    return score


def frase_impactante_streamer(job, ini, duracao):
    """Extrai o áudio do clipe e transcreve localmente com whisper.cpp.

    Não usa OPENAI_API_KEY, não envia áudio para serviços externos e não gera
    cobrança por transcrição. Se o binário/modelo não estiver instalado ou não
    houver fala aproveitável, retorna string vazia para usar o chat como fallback.
    """
    pasta, stream = job.get("pasta"), job.get("stream")
    headers = job.get("headers") or {}
    if not pasta or not stream:
        return ""
    if not WHISPER_CPP_MODELO or not os.path.isfile(WHISPER_CPP_MODELO):
        log.info("Modelo local do Whisper não encontrado; usando frase do chat.")
        return ""

    fd, audio_path = tempfile.mkstemp(prefix="fala_", suffix=".wav", dir=pasta)
    os.close(fd)
    prefixo_json = os.path.join(pasta, "transcricao_" + secrets.token_hex(5))
    json_path = prefixo_json + ".json"
    try:
        user_agent, extra = cabecalhos_ffmpeg(headers)
        cmd_audio = ["ffmpeg", "-nostdin", "-y", "-user_agent", user_agent, *extra,
                     "-protocol_whitelist", PROTOCOLOS, "-rw_timeout", "20000000",
                     "-ss", str(max(0, int(ini))), "-i", stream, "-t", str(int(duracao)),
                     "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", audio_path]
        r = subprocess.run(cmd_audio, capture_output=True, text=True, timeout=120)
        if r.returncode != 0 or not os.path.exists(audio_path) or os.path.getsize(audio_path) < 1000:
            log.warning("Não foi possível extrair áudio para transcrição: %s",
                        resumo_erro(r.stderr, r.returncode, 180))
            return ""

        cmd_whisper = [WHISPER_CPP_BIN, "-m", WHISPER_CPP_MODELO, "-f", audio_path,
                       "-l", "pt", "-oj", "-of", prefixo_json, "-nt", "-np"]
        r = subprocess.run(cmd_whisper, capture_output=True, text=True, timeout=180)
        if r.returncode != 0 or not os.path.isfile(json_path):
            log.warning("whisper.cpp indisponível ou falhou: %s",
                        resumo_erro(r.stderr, r.returncode, 180))
            return ""

        with open(json_path, "r", encoding="utf-8") as arq:
            dados = json.load(arq)

        segmentos = dados.get("transcription") or dados.get("segments") or []
        candidatos = []
        for seg in segmentos:
            texto = re.sub(r"\s+", " ", str(seg.get("text") or "")).strip(" .,-")
            score = _pontuar_fala_streamer(texto)
            if score > 0:
                candidatos.append((score, texto))

        if not candidatos:
            texto = re.sub(r"\s+", " ", str(dados.get("text") or "")).strip()
            score = _pontuar_fala_streamer(texto)
            if score > 0:
                candidatos.append((score, texto))
        if not candidatos:
            return ""

        frase = max(candidatos, key=lambda x: x[0])[1]
        if len(frase) > 100:
            frase = frase[:97].rsplit(" ", 1)[0] + "..."
        return frase.upper()
    except subprocess.TimeoutExpired:
        log.warning("A transcrição local do streamer excedeu o tempo limite.")
        return ""
    except Exception as exc:
        log.warning("Transcrição local indisponível; usando chat como alternativa: %s", exc)
        return ""
    finally:
        _remover_arquivo(audio_path)
        _remover_arquivo(json_path)


def cortar_lote(job_id, lote, formato):
    """Corta uma lista de (inicio_s, msgs). Os números dos clipes seguem sempre em sequência,
    mesmo se algum corte falhar (o próximo reaproveita o número)."""
    with JOBS_LOCK:
        tam = JOBS[job_id]["tamanho_clipe"]
        mensagens_chat = list(JOBS[job_id].get("chat_mensagens") or [])
        info = list(JOBS[job_id]["info"])
        feitos = JOBS[job_id]["clipes"]
    notas = []
    novos = 0
    with JOBS_LOCK:
        job_transcricao = dict(JOBS[job_id])
    for k, (ini, score) in enumerate(lote):
        # Prioridade: fala impactante do streamer; fallback: frase mais repetida no chat.
        frase = frase_impactante_streamer(job_transcricao, ini, tam) if formato != "original" else ""
        if not frase and formato != "original":
            frase = frase_mais_repetida_chat(mensagens_chat, ini, ini + tam)
        ok, nota = cortar_clipe(job_id, feitos, ini, formato, frase=frase)
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
    atualizar_job(job_id, canal=slug or "")
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

    chave_chat = (slug, escolhida["uuid"], int(dur))
    mensagens_chat = cache_chat_get(chave_chat)
    erro = ""
    if mensagens_chat is not None:
        atualizar_job(job_id, estado="analisando", msg="Esse chat já foi analisado há pouco. Aproveitando...",
                      diag="Mensagens e horários recuperados do cache.")
    else:
        mensagens_chat, erro = baixar_chat_vod_amostrado(slug, inicio_epoch, dur, prog)
        if mensagens_chat:
            cache_chat_set(chave_chat, mensagens_chat)
    if not mensagens_chat:
        atualizar_job(job_id, estado="erro", msg="Não consegui ler o chat dessa live.",
                      diag=erro or "Nenhuma mensagem encontrada no período.")
        return

    # guarda até 60 picos, do mais forte ao mais fraco
    tempos = [m["t"] for m in mensagens_chat if isinstance(m, dict) and "t" in m]
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
                  diag="Chat: %d mensagens analisadas; texto disponível para as legendas." % len(mensagens_chat),
                  picos=ranking, usados=len(primeiros), stream=stream, headers=headers,
                  chat_mensagens=mensagens_chat)

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
    stat_visita(ip_cliente())
    g.nonce = secrets.token_urlsafe(16)   # novo a cada resposta: é ele que autoriza o script e o CSS da página
    pagina = PAGINA.replace("__NONCE__", g.nonce).replace("__OGIMG__", request.url_root + "logo.jpg")
    return Response(pagina, mimetype="text/html")


@app.route("/logo.jpg")
def logo():
    resp = Response(LOGO_JPG, mimetype="image/jpeg")
    resp.set_etag(LOGO_ETAG)
    return resp.make_conditional(request)


@app.route("/fundo.jpg")
def fundo():
    resp = Response(FUNDO_JPG, mimetype="image/jpeg")
    resp.set_etag(FUNDO_ETAG)
    return resp.make_conditional(request)


_LIVES_REFRESH = threading.Lock()


def _atualizar_lives():
    with ThreadPoolExecutor(max_workers=min(6, len(CANAIS))) as ex:
        dados = list(ex.map(_consultar_canal, CANAIS))
    ok = any(x["status"] != "incerto" for x in dados)
    with _LIVES_LOCK:
        _LIVES_CACHE["dados"] = dados
        _LIVES_CACHE["ate"] = time.time() + (30 if ok else 10)


@app.route("/api/lives")
@limitar("lives", 30)
def api_lives():
    """Quem está online. Uma consulta por 30s para todos os visitantes. Com o cache vencido, devolve o dado
    anterior na hora e atualiza em segundo plano (ninguém fica esperando as consultas à Kick)."""
    agora = time.time()
    with _LIVES_LOCK:
        dados, ate = _LIVES_CACHE["dados"], _LIVES_CACHE["ate"]
    if dados is None or agora > ate + 300:
        # primeira vez (ou cache muito velho, como depois de o site acordar): espera a consulta
        with _LIVES_REFRESH:
            with _LIVES_LOCK:
                dados, ate = _LIVES_CACHE["dados"], _LIVES_CACHE["ate"]
            if dados is None or time.time() > ate + 300:
                try:
                    _atualizar_lives()
                except Exception:
                    log.exception("falha ao consultar as lives")
                with _LIVES_LOCK:
                    dados = _LIVES_CACHE["dados"]
    elif agora > ate and _LIVES_REFRESH.acquire(blocking=False):
        def tarefa():
            try:
                _atualizar_lives()
            except Exception:
                log.exception("falha ao atualizar as lives")
            finally:
                _LIVES_REFRESH.release()
        threading.Thread(target=tarefa, daemon=True).start()
    return jsonify({"canais": dados or []})


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


_STATS_HTML = """<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow"><meta name="color-scheme" content="dark">
<title>Estatísticas – Clipador</title>
<style nonce="__NONCE__">
  body { margin: 0; padding: 16px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
         background: linear-gradient(180deg, #0a2412 0, #050906 480px) #050906; color: #f7f7fb; line-height: 1.45; }
  main { max-width: 760px; margin: 0 auto; }
  h1 { font-size: 1.3rem; margin: 6px 0 10px; }
  .card { background: #0c120e; border: 1px solid rgba(110,220,145,.18); border-radius: 12px; padding: 12px 14px; margin-bottom: 12px; }
  .card b { color: #58e879; }
  .rolar { overflow-x: auto; }
  table { border-collapse: collapse; width: 100%; font-size: .85rem; white-space: nowrap; }
  th, td { padding: 6px 10px; text-align: right; border-bottom: 1px solid rgba(255,255,255,.08); }
  th:first-child, td:first-child { text-align: left; }
  th { color: #a9bbae; font-weight: 600; }
  .peq { color: #a9bbae; font-size: .8rem; margin-top: 8px; }
</style></head><body><main>
<h1>Estatísticas</h1>
<div class="card">__RESUMO__</div>
<div class="card rolar"><table>
<tr><th>Dia</th><th>Visitas</th><th>Visitantes</th><th>Ao vivo</th><th>Melhores mom.</th><th>Mais clipes</th><th>Clipes</th><th>Recusados</th></tr>
__LINHAS__
</table></div>
<p class="peq">Dias no horário de Brasília. "Visitantes" conta pessoas diferentes por dia, sem guardar o IP. "Recusados" são pedidos barrados por limite.</p>
</main></body></html>"""


def _exigir_admin():
    """None se a senha estiver certa; senão, a resposta a devolver."""
    if not ADMIN_SENHA:
        return Response("Não encontrado", status=404)
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
    return None


@app.route("/admin/estatisticas")
def admin_estatisticas():
    bloqueio = _exigir_admin()
    if bloqueio is not None:
        return bloqueio
    with _STATS_LOCK:
        _stats_dia_locked()
        dias = sorted(_STATS, reverse=True)[:14]
        tabela = [(dia, dict(_STATS[dia]), len(_STATS[dia]["unicos"])) for dia in dias]
    with JOBS_LOCK:
        ativos = sum(1 for j in JOBS.values() if job_ativo(j))
    mb = _bytes_em_uso() // (1024 * 1024)
    hoje = tabela[0][1] if tabela else {}
    jobs = hoje.get("jobs_live", 0) + hoje.get("jobs_vod", 0) + hoje.get("jobs_mais", 0)
    limite_dia = str(MAX_JOBS_DIA) if MAX_JOBS_DIA > 0 else "sem limite"
    limite_disco = ("%d MB" % MAX_DISCO_MB) if MAX_DISCO_MB > 0 else "sem limite"
    resumo = ("Hoje: <b>%d</b> visitas, <b>%d</b> visitantes, <b>%d</b> tarefas (limite do dia: %s).<br>"
              "Agora: <b>%d</b> tarefas ativas, disco em uso: <b>%d MB</b> de %s."
              % (hoje.get("visitas", 0), tabela[0][2] if tabela else 0, jobs, limite_dia, ativos, mb, limite_disco))
    linhas = []
    for dia, d, unicos in tabela:
        linhas.append("<tr><td>%s</td><td>%d</td><td>%d</td><td>%d</td><td>%d</td><td>%d</td><td>%d</td><td>%d</td></tr>"
                      % (escape(dia[8:10] + "/" + dia[5:7]), d["visitas"], unicos, d["jobs_live"], d["jobs_vod"],
                         d["jobs_mais"], d["clipes"], d["recusados"]))
    g.nonce = secrets.token_urlsafe(16)
    html = (_STATS_HTML.replace("__NONCE__", g.nonce).replace("__RESUMO__", resumo)
            .replace("__LINHAS__", "\n".join(linhas)))
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

    cota = int(math.ceil(minutos * 60 / seg))   # 1 clipe por trecho de `seg` segundos
    job_id, erro = criar_job("live", cota, tamanho_clipe=seg, formato=formato)
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

    job_id, erro = criar_job("vod", qtd, tamanho_clipe=seg, formato=formato)
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
    if orcamento_estourado():
        stat_inc("recusados")
        return jsonify({"erro": "O site atingiu o limite de uso de hoje. Volte amanhã."}), 429
    cheio = disco_cheio()   # fora do lock

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
        erro = _checar_limites_locked(ip, cheio)
        if erro:
            return jsonify({"erro": erro}), 429
        ok, usados_cota, espera = cota_reservar(ip, len(restantes))
        if not ok:
            return jsonify({"erro": erro_cota(len(restantes), usados_cota, espera)}), 429
        j["cota"] = len(restantes)
        j["clipes_antes"] = int(j.get("clipes") or 0)
        j["usados"] = usados + len(restantes)
        j["estado"] = "fila"
        j["msg"] = "Cortando mais %d clipes..." % len(restantes)
        j["ip"] = ip
        j["atualizado"] = time.time()
        formato = j["formato"]

    stat_inc("jobs_mais")
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
