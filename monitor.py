import json
import os
import subprocess
import time

BASE_DIR = "/tmp/clipador" if os.path.exists("/tmp") else "."
os.makedirs(BASE_DIR, exist_ok=True)
LIVES_JSON = os.path.join(BASE_DIR, "lives.json")

CANAIS = [
    {"nome": "yuri22ofc", "url": "https://youtube.com/@yuri22ofc?si=IWHTykazXTRE_TUf"},
    {"nome": "brabox", "url": "https://kick.com/brabox"},
    {"nome": "coringa", "url": "https://kick.com/coringa"},
    {"nome": "gabepeixe", "url": "https://kick.com/gabepeixe"},
]

INTERVALO_SEGUNDOS = 90


def verificar_canal(canal):
    cmd = [
        "yt-dlp",
        "--skip-download",
        "--no-warnings",
        "--no-playlist",
        "-g",
        "-f",
        "best[height<=720]/best",
        canal["url"],
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        links = [
            linha.strip()
            for linha in r.stdout.splitlines()
            if linha.strip().startswith("http")
        ]
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
        try:
            resultados = [verificar_canal(c) for c in CANAIS]
            guardar_lives(resultados)
        except Exception:
            pass
        time.sleep(INTERVALO_SEGUNDOS)


if __name__ == "__main__":
    loop_monitor()
