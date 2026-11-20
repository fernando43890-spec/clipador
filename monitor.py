import json
import os
import subprocess
import time

BASE_DIR = "/tmp/clipador" if os.path.exists("/tmp") else "."
os.makedirs(BASE_DIR, exist_ok=True)
LIVES_JSON = os.path.join(BASE_DIR, "lives.json")

# Substitui ou ajusta os URLs dos canais que queres monitorizar
CANAIS = [
    {"nome": "Coringa", "url": "https://kick.com/coringa"},
    {"nome": "Paulinho o Loko", "url": "https://kick.com/paulinholoko"},
]

INTERVALO_SEGUNDOS = 120


def verificar_canal(canal):
    cmd = ["yt-dlp", "--skip-download", "-g", "-f", "best[height<=720]/best", canal["url"]]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        links = [linha.strip() for linha in r.stdout.splitlines() if linha.strip().startswith("http")]
        if links:
            return {
                "nome": canal["nome"],
                "url": canal["url"],
                "stream_url": links[0],
                "online": True,
                "atualizado_em": int(time.time()),
            }
    except Exception:
        pass

    return {
        "nome": canal["nome"],
        "url": canal["url"],
        "stream_url": "",
        "online": False,
        "atualizado_em": int(time.time()),
    }


def guardar_lives(dados):
    temp_path = LIVES_JSON + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, LIVES_JSON)


def loop_monitor():
    while True:
        resultados = [verificar_canal(c) for c in CANAIS]
        guardar_lives(resultados)
        time.sleep(INTERVALO_SEGUNDOS)


if __name__ == "__main__":
    loop_monitor()
