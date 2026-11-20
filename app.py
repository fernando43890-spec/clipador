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
<title>Clipador</title>
<style>
 body { font-family: sans-serif; background:#111; color:#eee; padding:20px; max-width:500px; margin:auto; }
 input, button, select { width:100%; padding:14px; margin-top:12px; font-size:16px; border-radius:8px; border:none; box-sizing:border-box; }
 button { background:#9146ff; color:white; font-weight:bold; }
 .caixa { background:#1c1c24; padding:14px; border-radius:8px; margin-top:16px; }
 .aviso { color:#aaa; font-size:13px; margin-top:16px; }
 pre { white-space:pre-wrap; word-break:break-all; font-size:11px; color:#9fd; margin:0; }
</style>
</head>
<body>
<h2>Clipador de lives</h2>
<p>Cole o link de uma live AO VIVO. Ela será gravada e cortada em clipes de 1 minuto.</p>
<input id="url" type="url" placeholder="https://...">
<select id="min">
 <option value="5">Gravar 5 minutos (teste)</option>
 <option value="15">Gravar 15 minutos</option>
 <option value="30">Gravar 30 minutos</option>
 <option value="60">Gravar 60 minutos</option>
</select>
<button onclick="iniciar()">Começar a gravar</button>
<div class="caixa" id="status">Aguardando link.</div>
<div class="caixa" id="lista" style="display:none"></div>
<div class="caixa"><b>Diagnóstico</b><br><pre id="diag">-</pre></div>
<p class="aviso">Use só com autorização do streamer ou nas suas próprias lives.</p>
<script>
let job = null;
async function iniciar() {
  const url = document.getElementById('url').value.trim();
  const min = document.getElementById('min').value;
  if (!url) { return; }
  document.getElementById('status').innerText = 'Iniciando...';
  const r = await fetch('/iniciar', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({url:url, min:min})});
  const d = await r.json();
  job = d.id;
  checar();
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
    let h = '<a style="color:#bf94ff" href="/zip/' + job + '">Baixar todos em ZIP (' + d.clipes + ' clipes)</a><br><br>';
    for (let i = 1; i <= d.clipes; i++) {
      h += '<a style="color:#bf94ff" href="/clipe/' + job + '/' + i + '">Clipe ' + i + '</a><br>';
    }
    lista.innerHTML = h;
  }
  if (d.estado !== 'fim' && d.estado !== 'erro') { setTimeout(checar, 3000); }
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
