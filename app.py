import os
import subprocess
import tempfile
import glob
from flask import Flask, request, send_file, render_template_string

app = Flask(__name__)

PAGINA = """
<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Clipador</title>
<style>
 body { font-family: sans-serif; background:#111; color:#eee; padding:20px; max-width:500px; margin:auto; }
 input, button { width:100%; padding:14px; margin-top:12px; font-size:16px; border-radius:8px; border:none; box-sizing:border-box; }
 button { background:#9146ff; color:white; font-weight:bold; }
 .aviso { color:#aaa; font-size:13px; margin-top:16px; }
</style>
</head>
<body>
<h2>Clipador de lives</h2>
<p>Cole o link da live (Twitch, YouTube ou Kick). Vai gerar o último minuto.</p>
<form method="post" action="/clipar" onsubmit="document.getElementById('b').innerText='Gerando... aguarde';">
 <input name="url" type="url" placeholder="https://..." required>
 <button id="b" type="submit">Gerar clipe de 1 minuto</button>
</form>
<p class="aviso">Use só com autorização do streamer ou nas suas próprias lives.</p>
</body>
</html>
"""

@app.route("/")
def inicio():
    return render_template_string(PAGINA)

@app.route("/clipar", methods=["POST"])
def clipar():
    url = request.form.get("url", "").strip()
    if not url.startswith("http"):
        return "Link inválido.", 400

    pasta = tempfile.mkdtemp()
    saida = os.path.join(pasta, "clipe.mp4")

    comando = [
        "yt-dlp",
        "--live-from-start",
        "--download-sections", "*-60-inf",
        "-f", "best[height<=720]/best",
        "--merge-output-format", "mp4",
        "-o", saida,
        url,
    ]
    try:
        subprocess.run(comando, check=True, timeout=240)
    except Exception:
        return "Não consegui gerar o clipe. A plataforma pode ter bloqueado o servidor.", 500

    arquivos = glob.glob(os.path.join(pasta, "clipe*"))
    if not arquivos:
        return "Nenhum arquivo foi gerado.", 500

    return send_file(arquivos[0], as_attachment=True, download_name="clipe.mp4")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
