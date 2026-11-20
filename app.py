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
</style>
</head>
<body>
<h2>Clipador de lives</h2>
<p>Cole o link de uma live que está AO VIVO agora. Ela será gravada e cortada em clipes de 1 minuto.</p>
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
<p class="aviso">Use só com autorização do streamer ou nas suas próprias lives.</p>
