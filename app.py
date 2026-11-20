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
    --bg: #09090c;
    --card: #121218;
    --input: #1b1b24;
    --border: rgba(255, 255, 255, 0.08);
    --roxo: #9146ff;
    --roxo-escuro: #7328e6;
    --roxo-claro: #c084fc;
    --texto: #f4f4f6;
    --mutado: #a1a1aa;
  }

  * {
    box-sizing: border-box;
  }

  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, sans-serif;
    background:
      radial-gradient(circle at 50% -10%, rgba(145, 70, 255, 0.22), transparent 50%),
      var(--bg);
    color: var(--texto);
    min-height: 100vh;
    margin: 0;
    padding: 24px 16px;
    display: flex;
    justify-content: center;
    align-items: flex-start;
  }

  .container {
    width: 100%;
    max-width: 500px;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 18px;
    padding: 24px 20px;
    box-shadow: 0 20px 50px rgba(0, 0, 0, 0.55);
  }

  .topo {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }

  h2 {
    margin: 0;
    font-size: 1.5rem;
    font-weight: 700;
    letter-spacing: -0.02em;
  }

  .badge {
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--roxo-claro);
    background: rgba(145, 70, 255, 0.14);
    border: 1px solid rgba(145, 70, 255, 0.35);
    padding: 4px 9px;
    border-radius: 999px;
    white-space: nowrap;
  }

  .descricao {
    color: var(--mutado);
    font-size: 0.94rem;
    line-height: 1.5;
    margin: 10px 0 18px;
  }

  .descricao strong {
    color: var(--texto);
    font-weight: 600;
  }

  .campo {
    margin-bottom: 12px;
  }

  label {
    display: block;
    font-size: 0.8rem;
    font-weight: 600;
    color: var(--mutado);
    margin-bottom: 6px;
  }

  input, select {
    width: 100%;
    padding: 13px 14px;
    font-size: 0.96rem;
    border-radius: 11px;
    border: 1px solid var(--border);
    background: var(--input);
    color: var(--texto);
    outline: none;
    transition: border-color 0.2s ease, box-shadow 0.2s ease;
  }

  input::placeholder {
    color: #71717a;
  }

  input:focus, select:focus {
    border-color: var(--roxo);
    box-shadow: 0 0 0 3px rgba(145, 70, 255, 0.22);
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

  button {
    width: 100%;
    padding: 14px 16px;
    margin-top: 6px;
    font-size: 1rem;
    font-weight: 700;
    border-radius: 11px;
    border: none;
    background: linear-gradient(135deg, var(--roxo), var(--roxo-escuro));
    color: #fff;
    cursor: pointer;
    box-shadow: 0 8px 24px rgba(145, 70, 255, 0.32);
    transition: transform 0.15s ease, filter 0.2s ease;
  }

  button:hover {
    filter: brightness(1.08);
    transform: translateY(-1px);
  }

  button:active {
    transform: translateY(0);
  }

  button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
    transform: none;
  }

  .caixa {
    background: #0d0d12;
    border: 1px solid var(--border);
    padding: 13px 14px;
    border-radius: 12px;
    margin-top: 14px;
    font-size: 0.92rem;
    line-height: 1.45;
  }

  #status {
    color: var(--texto);
    font-weight: 500;
  }

  #lista a {
    display: block;
    color: var(--roxo-claro);
    text-decoration: none;
    padding: 8px 10px;
    border-radius: 8px;
    background: rgba(145, 70, 255, 0.08);
    border: 1px solid rgba(145, 70, 255, 0.18);
    margin-top: 6px;
    font-size: 0.88rem;
    font-weight: 500;
    transition: background 0.15s ease;
  }

  #lista a:first-child {
    margin-top: 0;
    background: rgba(145, 70, 255, 0.18);
    font-weight: 700;
  }

  #lista a:hover {
    background: rgba(145, 70, 255, 0.24);
  }

  .diag-titulo {
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--mutado);
    font-weight: 700;
  }

  pre {
    white-space: pre-wrap;
    word-break: break-word;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 0.78rem;
    color: #93c5fd;
    margin: 8px 0 0;
  }

  .aviso {
    color: #71717a;
    font-size: 0.8rem;
    text-align: center;
    margin: 16px 0 0;
    line-height: 1.4;
  }
</style>
</head>
<body>
<div class="container">
  <div class="topo">
    <h2>Clipador de lives</h2>
    <span class="badge">Clipes 1 min</span>
  </div>
  <p class="descricao">Cole o link de uma live <strong>AO VIVO</strong>. Ela será gravada e cortada automaticamente em clipes de 1 minuto.</p>

  <div class="campo">
    <label for="url">Link da transmissão</label>
    <input id="url" type="url" placeholder="https://...">
  </div>

  <div class="campo">
    <label for="min">Tempo máximo de gravação</label>
    <select id="min">
      <option value="5">Gravar 5 minutos (teste)</option>
      <option value="15">Gravar 15 minutos</option>
      <option value="30">Gravar 30 minutos</option>
      <option value="60">Gravar 60 minutos</option>
    </select>
  </div>

  <button id="btn-gravar" onclick="iniciar()">Começar a gravar</button>

  <div class="caixa" id="status">Aguardando link.</div>
  <div class="caixa" id="lista" style="display:none"></div>
  <div class="caixa">
    <span class="diag-titulo">Diagnóstico</span>
    <pre id="diag">-</pre>
  </div>

  <p class="aviso">Use só com autorização do streamer ou nas suas próprias lives.</p>
</div>

<script>
let job = null;
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
