#!/usr/bin/env python3
"""Студия голоса Джарвиса — живой подбор JARVIS-DSP под Silero (eugene).

Локальный веб-UI (stdlib http.server, без зависимостей). Открывает страницу с
«ползунками» каждого параметра обработки: тон, темп, эквалайзер, компрессия, реверб.
Двигаешь ползунок — СРАЗУ слышишь результат и сравниваешь варианты A/B.

КЛЮЧ МГНОВЕННОСТИ (synth-once / DSP-many): фразу Silero синтезирует ОДИН раз (torch
грузится раз за сессию, «сухой» PCM кэшируется в памяти). При движении ползунков
пере-применяется только быстрая ffmpeg-цепочка (jarvis.tts_dsp, ~150 мс) — ре-синтеза
нет, отклик мгновенный. Кнопка «Сохранить пресет» пишет значения в settings.yaml → voice.dsp.

Запуск:   python tools/voice_studio.py            (откроется http://127.0.0.1:8770)
          python tools/voice_studio.py --port 9000 --no-browser

Безопасно при работающем Джарвисе: тулза не трогает MQTT, играет звук сама (через браузер).
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import threading
import wave
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from jarvis import config, tts_dsp                  # noqa: E402
from jarvis.tts_engine import SileroEngine          # noqa: E402

# Описание ползунков: ключ, подпись, группа, тип и границы. HTML строится из этой схемы.
PARAM_SCHEMA = [
    ("pitch_cents",   "Тон (центы)",        "Тон и темп",  "range", -400, 100, 5),
    ("tempo",         "Темп",               "Тон и темп",  "range", 0.7, 1.3, 0.01),
    ("bass_gain",     "Низ, дБ",            "Эквалайзер",  "range", -6, 10, 0.5),
    ("bass_freq",     "Низ, Гц",            "Эквалайзер",  "range", 60, 250, 5),
    ("presence_gain", "Присутствие, дБ",    "Эквалайзер",  "range", -6, 8, 0.5),
    ("presence_freq", "Присутствие, Гц",    "Эквалайзер",  "range", 1500, 5000, 50),
    ("treble_gain",   "Верх, дБ",           "Эквалайзер",  "range", -8, 6, 0.5),
    ("treble_freq",   "Верх, Гц",           "Эквалайзер",  "range", 5000, 12000, 100),
    ("comp_ratio",    "Компрессия (ratio)", "Компрессия",  "range", 1, 6, 0.1),
    ("comp_threshold","Порог, дБ",          "Компрессия",  "range", -40, 0, 1),
    ("comp_makeup",   "Компенсация, дБ",     "Компрессия",  "range", 0, 8, 0.5),
    ("reverb",        "Реверб (эфир)",       "Реверб",      "bool", 0, 1, 1),
    ("reverb_in",     "Реверб вход",         "Реверб",      "range", 0, 1, 0.05),
    ("reverb_out",    "Реверб выход",        "Реверб",      "range", 0, 1, 0.05),
    ("reverb_delays", "Задержки, мс",        "Реверб",      "text", 0, 0, 0),
    ("reverb_decays", "Затухания",           "Реверб",      "text", 0, 0, 0),
    ("limit",         "Лимитер",             "Финал",       "range", 0, 1, 0.01),
    ("trim_silence",  "Срез тишины",         "Финал",       "bool", 0, 1, 1),
]

SAMPLE_PHRASES = [
    "Системы в норме, сэр. Рад снова быть в вашем распоряжении.",
    "Слушаю, сэр.",
    "Боюсь, сэр, это уже не отыграть назад.",
    "Доброе утро, сэр. Сейчас семь часов утра. Пора просыпаться.",
    "Сэр, вы за работой без передышки уже изрядно. Пять минут паузы — и дальше пойдёт легче.",
    "Прибавляю звук, сэр.",
]

_engine = SileroEngine()
_dry_cache: dict[str, bytes] = {}   # sha1(text) -> сырой PCM (синтез один раз на текст)
_synth_lock = threading.Lock()


def _dry_pcm(text: str) -> bytes:
    """Синтезировать «сухой» PCM для текста ОДИН раз (потокобезопасно, кэш в памяти)."""
    key = hashlib.sha1(text.encode("utf-8")).hexdigest()
    cached = _dry_cache.get(key)
    if cached is not None:
        return cached
    with _synth_lock:
        cached = _dry_cache.get(key)
        if cached is None:
            cached = _engine.synth(text)
            _dry_cache[key] = cached
        return cached


def _pcm_to_wav(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _current_params() -> dict:
    """Текущие значения = дефолт DSP, перекрытый тем, что уже в settings.yaml (voice.dsp)."""
    return {**tts_dsp.DEFAULT_PARAMS, **(config.DSP_PARAMS or {})}


def _save_dsp_to_settings(params: dict) -> str:
    """Записать значения DSP в settings.yaml → voice.dsp, СОХРАНЯЯ комментарии.

    Построчно: в блоке `  dsp:` (отступ 2 под voice) у каждой дочерней строки `    ключ: знач`
    меняем значение, не трогая хвостовой комментарий. Отсутствующие ключи дописываем в конец
    блока. Перед записью — резервная копия settings.yaml.bak."""
    path = Path(config.SETTINGS_FILE)
    text = path.read_text(encoding="utf-8")
    path.with_suffix(path.suffix + ".bak").write_text(text, encoding="utf-8")
    lines = text.splitlines(keepends=True)

    def fmt(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, float):
            return f"{v:g}"
        if isinstance(v, str):
            return f'"{v}"'
        return str(v)

    # Найти строку `  dsp:` (ровно 2 пробела отступа).
    start = next((i for i, ln in enumerate(lines) if ln.rstrip("\n") == "  dsp:"), None)
    if start is None:
        raise RuntimeError("в settings.yaml не найден блок voice.dsp — добавьте его вручную")
    # Конец блока — первая последующая строка с отступом ≤ 2 непробельная.
    end = len(lines)
    for i in range(start + 1, len(lines)):
        s = lines[i]
        if s.strip() and not s.startswith("    "):
            end = i
            break
    remaining = dict(params)
    for i in range(start + 1, end):
        raw = lines[i]
        stripped = raw.strip()
        if not stripped.startswith("#") and ":" in stripped:
            key = stripped.split(":", 1)[0].strip()
            if key in remaining:
                indent = raw[: len(raw) - len(raw.lstrip())]
                after = raw.split(":", 1)[1]
                comment = ""
                if "#" in after:
                    comment = "  #" + after.split("#", 1)[1].rstrip("\n")
                lines[i] = f"{indent}{key}: {fmt(remaining.pop(key))}{comment}\n"
    # Дописать недостающие ключи в конец блока.
    if remaining:
        addition = "".join(f"    {k}: {fmt(v)}\n" for k, v in remaining.items())
        lines.insert(end, addition)
    path.write_text("".join(lines), encoding="utf-8")
    return str(path)


PAGE = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<title>Студия голоса Джарвиса</title>
<style>
 body{background:#11151a;color:#cdd6e0;font:14px/1.5 system-ui,sans-serif;margin:0;padding:24px}
 h1{font-size:18px;color:#7fd1ff;margin:0 0 4px} .sub{color:#7a8694;margin-bottom:18px}
 .wrap{max-width:920px;margin:0 auto}
 textarea{width:100%;box-sizing:border-box;background:#0c0f13;color:#e6edf3;border:1px solid #2a323c;
   border-radius:8px;padding:10px;font-size:14px}
 .groups{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px}
 .group{background:#161b22;border:1px solid #232b35;border-radius:10px;padding:12px 14px}
 .group h3{margin:0 0 8px;font-size:13px;color:#9fb0c0;text-transform:uppercase;letter-spacing:.04em}
 .row{display:grid;grid-template-columns:140px 1fr 56px;align-items:center;gap:8px;margin:6px 0}
 .row label{color:#aebac7;font-size:13px} input[type=range]{width:100%}
 .row .val{text-align:right;color:#7fd1ff;font-variant-numeric:tabular-nums}
 input[type=text]{background:#0c0f13;color:#e6edf3;border:1px solid #2a323c;border-radius:6px;padding:4px 6px}
 .bar{position:sticky;top:0;background:#11151a;padding:10px 0;display:flex;gap:8px;flex-wrap:wrap;
   align-items:center;z-index:5}
 button{background:#1f6feb;color:#fff;border:0;border-radius:8px;padding:9px 14px;font-size:14px;cursor:pointer}
 button.ghost{background:#222b36;color:#cdd6e0} button:disabled{opacity:.5;cursor:wait}
 select{background:#0c0f13;color:#e6edf3;border:1px solid #2a323c;border-radius:6px;padding:7px}
 #status{color:#8a97a5;margin-left:auto}
</style></head><body><div class=wrap>
 <h1>Студия голоса Джарвиса</h1>
 <div class=sub>Silero · eugene + JARVIS-DSP. Двигай ползунки — слышишь сразу (синтез один раз, обработка мгновенно).</div>
 <select id=sample></select>
 <textarea id=text rows=2></textarea>
 <div class=bar>
   <button id=play>▶ Играть</button>
   <button class=ghost id=storeA>Запомнить A</button>
   <button class=ghost id=playA>▶ A</button>
   <button class=ghost id=storeB>Запомнить B</button>
   <button class=ghost id=playB>▶ B</button>
   <button class=ghost id=reset>Сброс</button>
   <button id=save>💾 Сохранить пресет</button>
   <span id=status>—</span>
 </div>
 <div class=groups id=groups></div>
</div>
<audio id=au></audio>
<script>
const SCHEMA=__SCHEMA__, INIT=__INIT__, SAMPLES=__SAMPLES__;
const params=Object.assign({},INIT); let A=null,B=null,timer=null;
const groups={}; const g=document.getElementById('groups');
for(const [key,label,grp,type,mn,mx,st] of SCHEMA){
  if(!groups[grp]){const d=document.createElement('div');d.className='group';
    d.innerHTML='<h3>'+grp+'</h3>';g.appendChild(d);groups[grp]=d;}
  const row=document.createElement('div');row.className='row';
  if(type==='bool'){
    row.innerHTML='<label>'+label+'</label><input type=checkbox '+(params[key]?'checked':'')+
      '><span class=val></span>';
    row.querySelector('input').onchange=e=>{params[key]=e.target.checked;schedule();};
  }else if(type==='text'){
    row.innerHTML='<label>'+label+'</label><input type=text value="'+(params[key]??'')+'"><span class=val></span>';
    row.querySelector('input').onchange=e=>{params[key]=e.target.value;schedule();};
  }else{
    row.innerHTML='<label>'+label+'</label><input type=range min='+mn+' max='+mx+' step='+st+
      ' value='+params[key]+'><span class=val>'+params[key]+'</span>';
    const inp=row.querySelector('input'),val=row.querySelector('.val');
    inp.oninput=e=>{params[key]=parseFloat(e.target.value);val.textContent=e.target.value;schedule();};
  }
  groups[grp].appendChild(row);
}
SAMPLES.forEach((s,i)=>{const o=document.createElement('option');o.value=i;o.textContent=s.slice(0,48);
  document.getElementById('sample').appendChild(o);});
const text=document.getElementById('text');text.value=SAMPLES[0];
document.getElementById('sample').onchange=e=>{text.value=SAMPLES[e.target.value];};
const status=m=>document.getElementById('status').textContent=m;
async function render(p){
  status('синтез…');
  const r=await fetch('/render',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({text:text.value,params:p})});
  if(!r.ok){status('ошибка: '+await r.text());return null;}
  status('готово');return await r.blob();
}
async function play(p){const b=await render(p);if(!b)return;const au=document.getElementById('au');
  au.src=URL.createObjectURL(b);au.play();}
function schedule(){clearTimeout(timer);timer=setTimeout(()=>play(params),140);}
document.getElementById('play').onclick=()=>play(params);
document.getElementById('storeA').onclick=()=>{A=Object.assign({},params);status('A запомнен');};
document.getElementById('storeB').onclick=()=>{B=Object.assign({},params);status('B запомнен');};
document.getElementById('playA').onclick=()=>A&&play(A);
document.getElementById('playB').onclick=()=>B&&play(B);
document.getElementById('reset').onclick=()=>location.reload();
document.getElementById('save').onclick=async()=>{
  const r=await fetch('/save',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(params)});
  status(r.ok?('сохранено → '+await r.text()):('ошибка сохранения: '+await r.text()));};
play(params);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # тихо

    def _json_body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.path != "/":
            self.send_error(404)
            return
        html = (PAGE
                .replace("__SCHEMA__", json.dumps(PARAM_SCHEMA, ensure_ascii=False))
                .replace("__INIT__", json.dumps(_current_params(), ensure_ascii=False))
                .replace("__SAMPLES__", json.dumps(SAMPLE_PHRASES, ensure_ascii=False)))
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            data = self._json_body()
            if self.path == "/render":
                dry = _dry_pcm((data.get("text") or "").strip())
                if not dry:
                    self.send_error(400, "пустой текст")
                    return
                wet = tts_dsp.apply_dsp(dry, _engine.sample_rate, data.get("params") or {},
                                        _engine.sample_rate)
                body = _pcm_to_wav(wet, _engine.sample_rate)
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/save":
                path = _save_dsp_to_settings(data)
                self._text(200, path)
            else:
                self.send_error(404)
        except Exception as exc:
            self._text(500, f"{type(exc).__name__}: {exc}")

    def _text(self, code: int, msg: str):
        body = msg.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    ap = argparse.ArgumentParser(description="Студия подбора голоса Джарвиса (Silero + JARVIS-DSP)")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    url = f"http://{args.host}:{args.port}"
    print(f"Студия голоса: {url}  (Ctrl+C — выход)")
    print(f"Модель: {config.SILERO_MODEL} · спикер {config.SILERO_SPEAKER} · {_engine.sample_rate} Гц")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nВыход.")
        srv.shutdown()


if __name__ == "__main__":
    main()
