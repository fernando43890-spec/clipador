import json
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
JOBS_LOCK = threading.Lock()
SEG = 60

PAGINA = """
<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Clipador Minimalista</title>
<style>
  :root {
    --bg: #09090b;
    --surface: #18181b;
    --surface-hover: #27272a;
    --texto: #fafafa;
    --mutado: #a1a1aa;
    --accent: #22c55e;
    --accent-glow: rgba(34, 197, 94, 0.2);
    --borda: #27272a;
  }

  * { box-sizing: border-box; }
  body {
    margin: 0;
    min-height: 100vh;
    font-family: system-ui, -apple-system, sans-serif;
    color: var(--texto);
    background-color: var(--bg);
    padding: 20px 10px;
  }

  .wrap { max-width: 800px; margin: 0 auto; }
  header { margin-bottom: 24px; text-align: center; }
  h1 { font-size: 1.75rem; font-weight: 800; margin: 0 0 8px 0; letter-spacing: -0.02em; }
  p.sub { color: var(--mutado); margin: 0; font-size: 0.95rem; }

  .destaque-container {
    background: var(--surface);
    border: 1px solid var(--borda);
    border-radius: 16px;
    padding: 16px;
    margin-bottom: 24px;
  }

  .player-wrapper {
    position: relative;
    width: 100%;
    aspect-ratio: 16/9;
    border-radius: 12px;
    overflow: hidden;
    background: #000;
  }

  .placeholder-img {
    position: absolute;
    top: 0; left: 0; width: 100%; height: 100%;
    object-fit: cover;
    cursor: pointer;
  }

  .play-btn {
    position: absolute;
    top: 50%; left: 50%;
    transform: translate(-50%, -50%);
    width: 64px; height: 64px;
    background: var(--accent);
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    box-shadow: 0 0 20px var(--accent-glow);
    transition: transform 0.1s;
    border: none;
  }
  .play-btn:hover { transform: translate(-50%, -50%) scale(1.05); }
  .play-btn svg { width: 32px; height: 32px; fill: #000; margin-left: 4px; }

  iframe { width: 100%; height: 100%; border: none; }

  .controles {
    background: var(--surface);
    border: 1px solid var(--borda);
    border-radius: 16px;
    padding: 20px;
    margin-bottom: 24px;
  }
  .form-group { margin-bottom: 16px; }
  label { display: block; font-size: 0.85rem; color: var(--mutado); margin-bottom: 8px; font-weight: 500; }
  input, select {
    width: 100%; padding: 12px 16px; font-size: 1rem;
    border-radius: 8px; border: 1px solid var(--borda);
    background: #09090b; color: var(--texto); outline: none;
  }
  input:focus, select:focus { border-color: var(--accent); }
  button.principal {
    width: 100%; padding: 14px; background: var(--accent);
    color: #000; font-weight: 700; border-radius: 8px; border: none;
    cursor: pointer; font-size: 1rem;
  }
  button.principal:disabled { opacity: 0.5; cursor: not-allowed; }

  .grade-lives {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 16px;
    margin-top: 16px;
  }

  .card-min {
    background: var(--surface);
    border: 1px solid var(--borda);
    border-radius: 12px;
    padding: 12px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .card-min .preview {
    position: relative;
    width: 100%;
    aspect-ratio: 16/9;
    border-radius: 8px;
    overflow: hidden;
    background: #000;
  }
  .card-min h3 { margin: 0; font-size: 0.95rem; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .card-min .play-btn { width: 40px; height: 40px; }
  .card-min .play-btn svg { width: 20px; height: 20px; }

  .status-box {
    background: var(--surface); border: 1px solid var(--borda);
    border-radius: 12px; padding: 16px; margin-bottom: 16px; font-size: 0.9rem;
  }
  .lista-clipes { display: flex; flex-direction: column; gap: 8px; }
  .lista-clipes a {
    display: flex; align-items: center; justify-content: space-between;
    padding: 12px 16px; background: var(--surface-hover); border-radius: 8px;
    color: var(--texto); text-decoration: none; font-weight: 500; font-size: 0.9rem;
    border: 1px solid var(--borda);
  }
  .lista-clipes a:first-child { border-color: var(--accent); color: var(--accent); }
  pre {
    background: #09090b; padding: 12px; border-radius: 8px; font-size: 0.75rem;
    color: var(--mutado); overflow-x: auto; margin-top: 8px; border: 1px solid var(--borda);
  }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Clipador Minimalista</h1>
    <p class="sub">Transmissões ao vivo cortadas sob demanda</p>
  </header>

  <div class="destaque-container">
    <div id="player-destaque" class="player-wrapper">
      <img id="capa-destaque" class="placeholder-img" src="" alt="Capa">
      <button class="play-btn" onclick="carregarPlayerDestaque()">
        <svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
      </button>
    </div>
  </div>

  <div class="controles">
    <div class="form-group">
      <label for="url">URL da Live</label>
      <input id="url" type="url" placeholder="https://kick.com/canal">
    </div>
    <div class="form-group">
      <label for="min">Duração</label>
      <select id="min">
        <option value="5">5 minutos</option>
        <option value="15">15 minutos</option>
        <option value="30">30 minutos</option>
      </select>
    </div>
    <button id="btn-iniciar" class="principal" onclick="iniciarGravacao()">Iniciar Corte</button>
  </div>

  <div id="status-area" class="status-box" style="display:none;"></div>
  <div id="lista-clipes" class="lista-clipes" style="margin-bottom: 24px;"></div>

  <div>
    <h2 style="font-size: 1.2rem; margin-bottom: 8px;">Lives em Destaque</h2>
    <div id="grade-canais" class="grade-lives"></div>
  </div>
</div>

<script>
const CANAIS = [
  {nome: 'Coringa', canal: 'coringa', capa: 'https://static-cdn.jtvnw.net/previews-ttv/live_user_coringa-1280x720.jpg'},
  {nome: 'Gabe Peixe', canal: 'gabepeixe', capa: 'https://static-cdn.jtvnw.net/previews-ttv/live_user_gabepeixe-1280x720.jpg'},
  {nome: 'Bak', canal: 'bak', capa: 'https://static-cdn.jtvnw.net/previews-ttv/live_user_bak-1280x720.jpg'},
  {nome: 'Renato Vicente', canal: 'orenatovicente', capa: 'https://static-cdn.jtvnw.net/previews-ttv/live_user_orenatovicente-1280x720.jpg'}
];

let jobAtivo = null;
let ultimoQtdClipes = -1;

function inicializarInterface() {
  const principal = CANAIS[0];
  document.getElementById('url').value = 'https://kick.com/' + principal.canal;
  document.getElementById('capa-destaque').src = principal.capa;
  
  let gridHtml = '';
  CANAIS.forEach((c, idx) => {
    if(idx === 0) return;
    gridHtml += `
      <div class="card-min">
        <div class="preview" id="preview-${idx}">
          <img class="placeholder-img" src="${c.capa}" alt="${c.nome}">
          <button class="play-btn" onclick="carregarMiniPlayer(${idx})">
            <svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
          </button>
        </div>
        <h3>${c.nome}</h3>
      </div>
    `;
  });
  document.getElementById('grade-canais').innerHTML = gridHtml;
}

function carregarPlayerDestaque() {
  const urlInput = document.getElementById('url').value;
  const canal = extrairCanalKick(urlInput) || CANAIS[0].canal;
  const wrapper = document.getElementById('player-destaque');
  wrapper.innerHTML = `<iframe src="https://player.kick.com/${canal}?autoplay=true" allowfullscreen allow="autoplay"></iframe>`;
}

function carregarMiniPlayer(idx) {
  const c = CANAIS[idx];
  document.getElementById('url').value = 'https://kick.com/' + c.canal;
  const container = document.getElementById(`preview-${idx}`);
  container.innerHTML = `<iframe src="https://player.kick.com/${c.canal}?autoplay=true" allowfullscreen allow="autoplay"></iframe>`;
}

function extrairCanalKick(url) {
  try {
    const u = new URL(url);
    const partes = u.pathname.split('/').filter(Boolean);
    return partes[0] || null;
  } catch(e) { return null; }
}

async function iniciarGravacao() {
  const url = document.getElementById('url').value.trim();
  const min = document.getElementById('min').value;
  const btn = document.getElementById('btn-iniciar');
  const statusArea = document.getElementById('status-area');
  
  if(!url) return;
  btn.disabled = true;
  statusArea.style.display = 'block';
  statusArea.innerHTML = 'Inicializando...');
  ultimoQtdClipes = -1;
  document.getElementById('lista-clipes').innerHTML = '';

  try {
    const res = await fetch('/iniciar', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url, min})
    });
    const data = await res.json();
    if(!res.ok || !data.id) {
      statusArea.innerHTML = `Erro: ${data.erro || 'Falha ao iniciar'}`;
      btn.disabled = false;
      return;
    }
    jobAtivo = data.id;
    verificarStatus();
  } catch(e) {
    statusArea.innerHTML = 'Erro de conexão com o servidor.';
    btn.disabled = false;
  }
}

async function verificarStatus() {
  if(!jobAtivo) return;
  try {
    const res = await fetch('/status/' + jobAtivo);
    const d = await res.json();
    const statusArea = document.getElementById('status-area');
    
    statusArea.innerHTML = `
      <div><strong>Estado:</strong> ${d.msg}</div>
      <pre>${d.diag || 'Sem diagnóstico.'}</pre>
    `;
    
    renderizarListaClipes(Number(d.clipes || 0));

    if(d.estado !== 'fim' && d.estado !== 'erro') {
      setTimeout(verificarStatus, 3000);
    } else {
      document.getElementById('btn-iniciar').disabled = false;
    }
  } catch(e) {
    setTimeout(verificarStatus, 3000);
  }
}

function renderizarListaClipes(qtd) {
  if(qtd <= 0 || qtd === ultimoQtdClipes) return;
  ultimoQtdClipes = qtd;
  const container = document.getElementById('lista-clipes');
  let html = `<a href="/zip/${jobAtivo}" style="background: var(--surface); border-color: var(--accent); color: var(--accent);">Baixar Pacote Completo (${qtd} clipes) ⬇️</a>`;
  for(let i = 1; i <= qtd; i++) {
    html += `<a href="/clipe/${jobAtivo}/${i}">Clipe ${i} <span>▶️</span></a>`;
  }
  container.innerHTML = html;
}

document.addEventListener('DOMContentLoaded', inicializarInterface);
</script>
</body>
</html>
"""

def ler_fim(caminho, limite=500):
    try:
        if not os.path.exists(caminho): return ""
        with open(caminho, "r", errors="ignore") as f:
            texto = f.read().replace("\r", "\n")
        return texto[-limite:]
    except: return ""

def listar_clipes(pasta):
    if not os.path.isdir(pasta): return []
    return sorted([f for f in os.listdir(pasta) if f.startswith("clipe_") and f.endswith(".mp4") and os.path.getsize(os.path.join(pasta, f)) > 0])

def tamanho_pasta(pasta):
    return sum(os.path.getsize(os.path.join(pasta, f)) for f in listar_clipes(pasta))

def atualizar_job(job_id, **campos):
    with JOBS_LOCK:
        if job_id in JOBS: JOBS[job_id].update(campos)

def extrair_stream(url):
    cmd_json = ["yt-dlp", "--no-warnings", "-j", "-f", "best[height<=720]/best", url]
    r = subprocess.run(cmd_json, capture_output=True, text=True, timeout=60)
    if r.returncode == 0 and r.stdout.strip():
        for linha in r.stdout.strip().splitlines():
            try:
                info = json.loads(linha)
                stream_url = info.get("url")
                if stream_url: return stream_url, info.get("http_headers") or {}, ""
            except: continue
    return None, {}, (r.stderr or "Falha ao extrair stream").strip()

def gravar(job_id, url, minutos):
    pasta = os.path.join(BASE, job_id)
    os.makedirs(pasta, exist_ok=True)
    atualizar_job(job_id, pasta=pasta)
    log_ffmpeg = os.path.join(pasta, "ffmpeg.log")
    padrao = os.path.join(pasta, "clipe_%03d.mp4")

    atualizar_job(job_id, estado="abrindo", msg="Resolvendo link...", diag="Executando yt-dlp...")
    stream, headers, erro = extrair_stream(url)

    if not stream:
        atualizar_job(job_id, estado="erro", msg="Erro ao obter link da live.", diag=erro)
        return

    user_agent = headers.get("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
    extra_headers = [item for kv in [["-headers", f"{k}: {v}"] for k, v in headers.items() if k.lower() != "user-agent" and v] for item in kv]

    atualizar_job(job_id, estado="gravando", msg="Gravando Kick...", diag="Iniciando FFmpeg...")

    cmd = [
        "ffmpeg", "-y", "-hide_banner",
        "-user_agent", user_agent,
        *extra_headers,
        "-rw_timeout", "15000000",
        "-i", stream,
        "-t", str(minutos * 60),
        "-c", "copy",
        "-f", "segment",
        "-segment_time", str(SEG),
        "-reset_timestamps", "1",
        padrao
    ]

    proc = None
    try:
        with open(log_ffmpeg, "w") as logf:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=logf)
            inicio = time.time()

            while proc.poll() is None:
                feitos = listar_clipes(pasta)
                clipes_prontos = max(0, len(feitos) - 1)
                tam = tamanho_pasta(pasta)
                
                atualizar_job(
                    job_id, clipes=clipes_prontos,
                    msg=f"Gravando ({clipes_prontos} clipes fechados)...",
                    diag=f"Tempo: {int(time.time() - inicio)}s | Espaço: {tam // 1024} KB\nLast Log:\n{ler_fim(log_ffmpeg)}"
                )

                if time.time() - inicio > 60 and tam == 0:
                    break
                time.sleep(3)

        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    except Exception as e:
        if proc and proc.poll() is None:
            proc.kill()
        atualizar_job(job_id, estado="erro", msg="Falha crítica no FFmpeg.", diag=str(e))
        return

    feitos_finais = listar_clipes(pasta)
    clipes_finais = len(feitos_finais)
    if clipes_finais > 0:
        atualizar_job(job_id, estado="fim", clipes=clipes_finais, msg="Processamento concluído com sucesso!", diag="Todos os segmentos foram salvos.")
    else:
        atualizar_job(job_id, estado="erro", msg="Nenhum clipe válido foi gerado.", diag=ler_fim(log_ffmpeg, 1000))

@app.route("/")
def inicio(): return render_template_string(PAGINA)

@app.route("/iniciar", methods=["POST"])
def iniciar():
    dados = request.get_json(silent=True) or {}
    url = str(dados.get("url", "")).strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return jsonify({"id": "", "erro": "URL inválida."}), 400
    try:
        minutos = min(max(int(dados.get("min", 5)), 1), 180)
    except: minutos = 5

    job_id = uuid.uuid4().hex[:8]
    with JOBS_LOCK:
        JOBS[job_id] = {"estado": "iniciando", "msg": "Iniciando...", "clipes": 0, "diag": "", "pasta": ""}

    threading.Thread(target=gravar, args=(job_id, url, minutos), daemon=True).start()
    return jsonify({"id": job_id})

@app.route("/status/<job_id>")
def status(job_id):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j: return jsonify({"estado": "erro", "msg": "Job não encontrado", "clipes": 0, "diag": ""})
        return jsonify({"estado": j["estado"], "msg": j["msg"], "clipes": j["clipes"], "diag": j.get("diag", "")})

@app.route("/clipe/<job_id>/<int:n>")
def clipe(job_id, n):
    if n < 1: return "Inválido", 400
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        pasta = j.get("pasta") if j else ""
    if not pasta: return "Não encontrado", 404
    arq = os.path.join(pasta, f"clipe_{n - 1:03d}.mp4")
    if not os.path.isfile(arq): return "Não encontrado", 404
    return send_file(arq, as_attachment=True, download_name=f"clipe_{n}.mp4")

@app.route("/zip/<job_id>")
def baixar_zip(job_id):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        pasta = j.get("pasta") if j else ""
    if not pasta: return "Não encontrado", 404
    arquivos = listar_clipes(pasta)
    if not arquivos: return "Vazio", 404
    caminho_zip = os.path.join(BASE, f"{job_id}.zip")
    with zipfile.ZipFile(caminho_zip, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for f in arquivos: z.write(os.path.join(pasta, f), f)
    return send_file(caminho_zip, as_attachment=True, download_name="clipes.zip")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
