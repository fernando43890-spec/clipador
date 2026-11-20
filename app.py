import os
import time
import uuid
import threading
import subprocess
import zipfile
from flask import Flask, request, send_file, render_template_string, jsonify

app = Flask(__name__)
BASE = "/tmp/clipador"
os.makedirs(BASE, exist_ok=True)
JOBS = {}
SEG = 60

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
    --roxo: #8b6cf6;
    --roxo-brilho: #a488ff;
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
      radial-gradient(ellipse at 50% -8%, rgba(139, 108, 246, 0.28), transparent 55%),
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
    width: 34px;
    height: 34px;
    border-radius: 10px;
    background: linear-gradient(135deg, #a855f7, #6366f1);
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 800;
    font-size: 0.9rem;
    color: #fff;
  }

  .btn-topo {
    background: var(--roxo);
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
    font-size: 1.4rem;
    font-weight: 800;
    letter-spacing: -0.02em;
    line-height: 1.2;
  }

  .hero-sub {
    margin: 8px 0 16px;
    color: var(--mutado);
    font-size: 0.9rem;
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
    border-color: var(--roxo-brilho);
    box-shadow: 0 0 0 3px rgba(139, 108, 246, 0.22);
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
    background: var(--roxo);
    color: #0d0a16;
    cursor: pointer;
    box-shadow: 0 10px 26px rgba(139, 108, 246, 0.35);
  }

  .btn-principal:hover {
    background: var(--roxo-brilho);
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

  .lives-box {
    margin-top: 18px;
    background: var(--surface);
    border: 1px solid var(--borda);
    border-radius: 20px;
    padding: 16px;
  }

  .lives-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 0.9rem;
    font-weight: 700;
    margin-bottom: 12px;
  }

  .live-tag {
    display: inline-flex;
    align-items: center;
    gap: 8px;
  }

  .live-tag::before {
    content: "";
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--vivo);
    box-shadow: 0 0 10px var(--vivo);
  }

  .live-tempo {
    color: var(--mutado);
    font-variant-numeric: tabular-nums;
    font-size: 0.85rem;
  }

  .tela-live {
    position: relative;
    border-radius: 14px;
    overflow: hidden;
    background: linear-gradient(135deg, #241942 0%, #110d1d 60%, #1c1333 100%);
    border: 1px solid var(--borda-forte);
    padding: 18px 14px;
    min-height: 150px;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
  }

  .tela-topo-badge {
    align-self: flex-start;
    background: rgba(0, 0, 0, 0.55);
    border: 1px solid rgba(255, 255, 255, 0.14);
    color: #e9d5ff;
    font-size: 0.72rem;
    font-weight: 700;
    padding: 4px 10px;
    border-radius: 999px;
  }

  .tela-centro {
    margin: 14px 0;
  }

  .tela-centro strong {
    display: block;
    font-size: 1rem;
    color: #fff;
  }

  .tela-centro span {
    font-size: 0.82rem;
    color: var(--mutado);
  }

  .onda-marcadores {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 8px;
    margin-top: 12px;
  }

  .marcador {
    border: 1.5px solid var(--roxo-brilho);
    background: rgba(139, 108, 246, 0.14);
    border-radius: 10px;
    padding: 7px 6px;
    text-align: center;
    cursor: pointer;
  }

  .marcador span {
    display: block;
    font-size: 0.78rem;
    font-weight: 700;
    color: #fff;
    font-variant-numeric: tabular-nums;
  }

  .marcador small {
    font-size: 0.68rem;
    color: var(--roxo-brilho);
  }

  .grid-3 {
    margin-top: 12px;
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 10px;
  }

  .card-live {
    background: var(--surface-2);
    border: 1px solid var(--borda);
    border-radius: 14px;
    padding: 12px 10px;
    text-align: left;
    color: var(--texto);
    cursor: pointer;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    min-height: 112px;
    transition: border-color 0.2s ease, transform 0.15s ease;
  }

  .card-live:hover {
    border-color: var(--roxo-brilho);
    transform: translateY(-2px);
  }

  .card-pill {
    display: inline-block;
    font-size: 0.66rem;
    font-weight: 700;
    text-transform: uppercase;
    color: var(--roxo-brilho);
    background: rgba(139, 108, 246, 0.16);
    padding: 2px 7px;
    border-radius: 999px;
    margin-bottom: 6px;
    width: fit-content;
  }

  .card-nome {
    font-size: 0.86rem;
    font-weight: 700;
    line-height: 1.25;
  }

  .card-acao {
    margin-top: 8px;
    font-size: 0.72rem;
    color: var(--mutado);
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
        <span class="logo-box">CL</span>
        <span>Clipador de lives</span>
      </a>
      <a class="btn-topo" href="#url">Colar link</a>
    </div>
  </header>

  <main class="wrap">
    <section class="hero-card">
      <h1 class="hero-titulo">Transforme lives ao vivo em clipes de 1 minuto</h1>
      <p class="hero-sub">Cole o link da transmissão <strong>AO VIVO</strong>, escolha a duração e baixe os cortes prontos em MP4 ou ZIP.</p>

      <div class="campo">
        <label for="url">Link da live</label>
        <input id="url" type="url" placeholder="Cole o link da live (Kick, Twitch, YouTube...)">
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

    <div class="caixa" id="status">Aguardando link.</div>
    <div class="caixa" id="lista" style="display:none"></div>
    <div class="caixa">
      <span class="diag-topo">Diagnóstico</span>
      <pre id="diag">-</pre>
    </div>

    <section class="lives-box" aria-label="Lives e cortes">
      <div class="lives-top">
        <span class="live-tag">Kick • Lives</span>
        <span class="live-tempo">01:00 / clipe</span>
      </div>

      <div class="tela-live">
        <span class="tela-topo-badge">PRÉVIA DE CORTES AUTOMÁTICOS</span>
        <div class="tela-centro">
          <strong id="preview-titulo">Selecione um atalho ou cole o link acima</strong>
          <span id="preview-sub">Cada minuto gravado vira um clipe MP4 separado pronto para baixar.</span>
        </div>

        <div class="onda-marcadores">
          <div class="marcador" onclick="focarUrl()">
            <span>00:00</span>
            <small>Clipe 1</small>
          </div>
          <div class="marcador" onclick="focarUrl()">
            <span>01:00</span>
            <small>Clipe 2</small>
          </div>
          <div class="marcador" onclick="focarUrl()">
            <span>02:00</span>
            <small>Clipe 3</small>
          </div>
        </div>
      </div>

      <div class="grid-3">
        <button type="button" class="card-live" onclick="usarAtalho('Kick Live', 'https://kick.com/')">
          <span class="card-pill">Kick</span>
          <span class="card-nome">Canal Kick</span>
          <span class="card-acao">Preencher link →</span>
        </button>

        <button type="button" class="card-live" onclick="usarAtalho('Twitch Live', 'https://www.twitch.tv/')">
          <span class="card-pill">Twitch</span>
          <span class="card-nome">Canal Twitch</span>
          <span class="card-acao">Preencher link →</span>
        </button>

        <button type="button" class="card-live" onclick="usarAtalho('YouTube Live', 'https://www.youtube.com/')">
          <span class="card-pill">YouTube</span>
          <span class="card-nome">Live YouTube</span>
          <span class="card-acao">Preencher link →</span>
        </button>
      </div>
    </section>

    <p class="aviso">Use só com autorização do streamer ou nas suas próprias lives.</p>
  </main>

<script>
let job = null;

function focarUrl() {
  const input = document.getElementById('url');
  input.scrollIntoView({ behavior: 'smooth', block: 'center' });
  setTimeout(() => input.focus(), 250);
}

function usarAtalho(nome, linkBase) {
  const input = document.getElementById('url');
  if (!input.value.trim() || input.value.trim().endsWith('.com/') || input.value.trim().endsWith('.tv/')) {
    input.value = linkBase;
  }
  document.getElementById('preview-titulo').innerText = nome + ' selecionado';
  document.getElementById('preview-sub').innerText = 'Complete o link do canal ao vivo e clique em "Cortar meu vídeo".';
  focarUrl();
}

async function iniciar() {
  const url = document.getElementById('url').value.trim();
  const min = document.getElementById('min').value;
  const btn = document.getElementById('btn-gravar');
  if (!url) { return; }
  btn.disabled = true;
  document.getElementById('status').innerText = 'Iniciando...';
  try {
    const r = await fetch('/iniciar', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({url:url, min:min})});
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
    job["msg"] = "Abrindo a live..."
    job["diag"] = "Rodando yt-dlp..."
    cmd_url = ["yt-dlp", "-g", "-f", "best[height<=720]/best", url]
    try:
        r = subprocess.run(cmd_url, capture_output=True, text=True, timeout=90)
    except Exception as e:
        job["estado"] = "erro"
        job["msg"] = "yt-dlp demorou demais ou falhou."
        job["diag"] = str(e)[-500:]
        return
    links = [l for l in r.stdout.strip().split("\n") if l.startswith("http")]
    if not links:
        job["estado"] = "erro"
        job["msg"] = "O yt-dlp não conseguiu o link da live."
        job["diag"] = (r.stderr or "sem mensagem")[-700:]
        return
    stream = links[0]
    job["diag"] = "yt-dlp OK. Links: " + str(len(links)) + ". Iniciando ffmpeg..."

    job["estado"] = "gravando"
    job["msg"] = "Gravando... 0 clipes prontos"
    cmd = [
        "ffmpeg", "-y",
        "-user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "-rw_timeout", "15000000",
        "-i", stream,
        "-t", str(int(minutos) * 60),
        "-c", "copy",
        "-f", "segment", "-segment_time", str(SEG),
        "-reset_timestamps", "1",
        padrao,
    ]
    logf = open(log_ffmpeg, "w")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=logf)
    inicio = time.time()
    while proc.poll() is None:
        feitos = sorted(f for f in os.listdir(pasta) if f.startswith("clipe_"))
        job["clipes"] = max(0, len(feitos) - 1)
        tam = tamanho_pasta(pasta)
        job["msg"] = "Gravando... " + str(job["clipes"]) + " clipes prontos"
        job["diag"] = ("Tempo: " + str(int(time.time() - inicio)) + "s | Gravado: "
                       + str(tam // 1024) + " KB\n" + ler_fim(log_ffmpeg, 500))
        if time.time() - inicio > 60 and tam == 0:
            proc.kill()
            break
        time.sleep(3)
    logf.close()
    feitos = sorted(f for f in os.listdir(pasta) if f.startswith("clipe_"))
    job["clipes"] = len(feitos)
    if feitos and tamanho_pasta(pasta) > 0:
        job["estado"] = "fim"
        job["msg"] = "Pronto! " + str(len(feitos)) + " clipes gerados."
    else:
        job["estado"] = "erro"
        job["msg"] = "Nenhum dado chegou em 60 segundos. A Kick pode ter bloqueado o servidor."
        job["diag"] = ler_fim(log_ffmpeg, 800)

@app.route("/")
def inicio():
    return render_template_string(PAGINA)

@app.route("/iniciar", methods=["POST"])
def iniciar():
    dados = request.get_json()
    url = dados.get("url", "").strip()
    minutos = int(dados.get("min", 5))
    if not url.startswith("http"):
        return jsonify({"id": ""}), 400
    job_id = uuid.uuid4().hex[:8]
    JOBS[job_id] = {"estado": "iniciando", "msg": "Iniciando...", "clipes": 0, "diag": ""}
    threading.Thread(target=gravar, args=(job_id, url, minutos), daemon=True).start()
    return jsonify({"id": job_id})

@app.route("/status/<job_id>")
def status(job_id):
    j = JOBS.get(job_id)
    if not j:
        return jsonify({"estado": "erro", "msg": "Tarefa não encontrada", "clipes": 0, "diag": ""})
    return jsonify({"estado": j["estado"], "msg": j["msg"], "clipes": j["clipes"], "diag": j.get("diag", "")})

@app.route("/clipe/<job_id>/<int:n>")
def clipe(job_id, n):
    j = JOBS.get(job_id)
    if not j:
        return "Não encontrado", 404
    arq = os.path.join(j["pasta"], "clipe_%03d.mp4" % (n - 1))
    if not os.path.exists(arq):
        return "Não encontrado", 404
    return send_file(arq, as_attachment=True, download_name="clipe_%d.mp4" % n)

@app.route("/zip/<job_id>")
def baixar_zip(job_id):
    j = JOBS.get(job_id)
    if not j:
        return "Não encontrado", 404
    caminho = os.path.join(BASE, job_id + ".zip")
    with zipfile.ZipFile(caminho, "w") as z:
        for f in sorted(os.listdir(j["pasta"])):
            if f.startswith("clipe_"):
                z.write(os.path.join(j["pasta"], f), f)
    return send_file(caminho, as_attachment=True, download_name="clipes.zip")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
