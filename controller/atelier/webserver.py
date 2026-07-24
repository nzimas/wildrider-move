"""Wildrider web UI — manage & download performance recordings.

A tiny self-contained stdlib HTTP server on move.local:<port>. Recordings are
long-form master captures stored PER PROJECT (8 slots each):

    performances/perf_NN/recordings/rec_MM.wav      (a saved project)
    performances/_scratch/recordings/rec_MM.wav     (the unsaved 'scratch' project)

Runs in a daemon thread; never blocks the controller. Stdlib only.
"""
from __future__ import annotations

import html
import re
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

_PROJ_RE = re.compile(r"^(perf_\d{2}|scratch)$")
_NAME_RE = re.compile(r"^rec_\d{2}\.wav$")

PAGE = """<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wildrider — Recordings</title>
<style>
  :root {{ --bg:#0b0d10; --panel:#14171d; --line:#242a33; --txt:#e8edf2; --dim:#7d8895;
           --hot:#e0483c; --cy:#39c2b8; --grn:#57c977; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--txt);
          font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
          -webkit-font-smoothing:antialiased; }}
  .wrap {{ max-width:860px; margin:0 auto; padding:26px 18px 72px; }}
  header {{ display:flex; align-items:baseline; justify-content:space-between; margin:0 0 6px; }}
  .brand {{ font-size:24px; font-weight:800; letter-spacing:.02em; }}
  .brand span {{ color:var(--hot); }}
  .tag {{ color:var(--dim); font-size:12px; letter-spacing:.22em; text-transform:uppercase; }}
  a.refresh {{ color:var(--cy); text-decoration:none; font-size:13px; font-weight:700; letter-spacing:.08em; }}
  .proj {{ margin:24px 0 8px; padding-bottom:6px; border-bottom:1px solid var(--line);
           font-size:14px; letter-spacing:.16em; text-transform:uppercase; color:var(--dim); font-weight:700; }}
  .rec {{ display:flex; align-items:center; gap:13px; background:var(--panel);
          border:1px solid var(--line); border-radius:11px; padding:11px 15px; margin:9px 0; }}
  .rec .n {{ font-size:22px; font-weight:800; color:var(--cy); min-width:30px; text-align:center; }}
  .rec .meta {{ flex:1; min-width:0; }}
  .rec .meta .name {{ font-weight:600; font-size:15px; }}
  .rec .meta .sub {{ color:var(--dim); font-size:12.5px; margin-top:2px; }}
  .btn {{ font:inherit; cursor:pointer; border:none; text-decoration:none; font-weight:700;
          font-size:12.5px; letter-spacing:.04em; padding:9px 15px; border-radius:7px; white-space:nowrap; }}
  .play {{ background:var(--grn); color:#06210f; }}
  a.dl {{ background:var(--cy); color:#06201d; }}
  a.dl:hover {{ background:#4ee0d5; }}
  .del {{ background:transparent; color:var(--dim); border:1px solid var(--line); }}
  .del:hover {{ color:var(--hot); border-color:var(--hot); }}
  .empty {{ display:none; }}
  .none {{ color:var(--dim); margin:40px 0; text-align:center; letter-spacing:.06em; }}
  footer {{ color:var(--dim); font-size:11.5px; letter-spacing:.1em; margin-top:34px;
            border-top:1px solid var(--line); padding-top:14px; }}
</style></head>
<body><div class="wrap">
<header><div><div class="brand">WILD<span>RIDER</span></div>
<div class="tag">Performance Recordings</div></div>
<a class="refresh" href="/" onclick="location.reload();return false;">&#8635; REFRESH</a></header>
{body}
<footer>stereo WAV · 8 recordings per project · up to 10 min each</footer>
</div>
<script>
  var cur=null, curBtn=null;
  function reset(b){{ if(b){{ b.textContent='▶ PLAY'; }} }}
  document.addEventListener('click', function(e){{
    var b = e.target.closest('.play'); if(!b) return;
    if(curBtn===b && cur){{
      if(cur.paused){{ cur.play(); b.textContent='⏸ STOP'; }} else {{ cur.pause(); reset(b); }}
      return;
    }}
    if(cur){{ cur.pause(); }} reset(curBtn);
    cur = new Audio(b.dataset.src); cur.onended=function(){{ reset(b); }};
    cur.play(); curBtn=b; b.textContent='⏸ STOP';
  }});
</script>
</body></html>"""


def _wav_info(path: Path):
    """(duration_seconds or None, size_bytes) from the WAV header."""
    size = path.stat().st_size
    try:
        with path.open("rb") as f:
            head = f.read(4096)
        if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            return None, size
        i, ch, sr, bits, data = 12, 2, 44100, 16, None
        while i + 8 <= len(head):
            cid, csz = head[i:i + 4], struct.unpack("<I", head[i + 4:i + 8])[0]
            if cid == b"fmt ":
                ch = struct.unpack("<H", head[i + 10:i + 12])[0]
                sr = struct.unpack("<I", head[i + 12:i + 16])[0]
                bits = struct.unpack("<H", head[i + 22:i + 24])[0]
            elif cid == b"data":
                data = csz if csz not in (0, 0xFFFFFFFF) else (size - (i + 8))
                break
            i += 8 + csz + (csz & 1)
        if data and ch and sr and bits:
            return data / (sr * ch * (bits // 8)), size
    except (OSError, struct.error):
        pass
    return None, size


def _fmt_dur(sec):
    if sec is None:
        return "—"
    m, s = divmod(int(round(sec)), 60)
    return f"{m}:{s:02d}"


def _fmt_size(b):
    return f"{b / 1_048_576:.1f} MB" if b >= 1_048_576 else f"{b / 1024:.0f} KB"


class _Handler(BaseHTTPRequestHandler):
    perf_dir: Path = Path(".")
    n_slots: int = 8

    def log_message(self, *_a):
        pass

    def _send(self, code, ctype, body: bytes, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _rec_file(self, path: str):
        """Resolve /rec|del/<proj>/<name> to a real file, guarding traversal."""
        parts = [p for p in path.split("/") if p]     # e.g. ['rec','perf_03','rec_00.wav']
        if len(parts) != 3:
            return None
        proj, name = unquote(parts[1]), unquote(parts[2])
        if not _PROJ_RE.match(proj) or not _NAME_RE.match(name):
            return None
        d = self.perf_dir / ("_scratch" if proj == "scratch" else proj) / "recordings"
        f = d / name
        try:
            f.resolve().relative_to(self.perf_dir.resolve())
        except (ValueError, OSError):
            return None
        return f

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._send(200, "text/html; charset=utf-8", self._index().encode())
        if path.startswith("/rec/"):
            f = self._rec_file(path)
            if f and f.is_file():
                return self._send(200, "audio/wav", f.read_bytes(),
                                  {"Content-Disposition": f'attachment; filename="{f.name}"'})
            return self._send(404, "text/plain", b"not found")
        return self._send(404, "text/plain", b"not found")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/del/"):
            f = self._rec_file(path)
            if f is not None:
                try:
                    f.unlink()
                except OSError:
                    pass
            return self._send(303, "text/plain", b"", {"Location": "/"})
        return self._send(404, "text/plain", b"not found")

    def _projects(self):
        """[(label, url_proj, [(slot, path) ...]), ...] for projects that hold takes,
        scratch first, then perf_NN ascending."""
        out = []
        base = self.perf_dir
        dirs = []
        sc = base / "_scratch"
        if sc.is_dir():
            dirs.append(("Unsaved (scratch)", "scratch", sc))
        for d in sorted(base.glob("perf_*")):
            if d.is_dir():
                n = d.name.split("_")[1]
                dirs.append((f"Project {int(n) + 1}", d.name, d))
        for label, urlp, d in dirs:
            recs = []
            for slot in range(self.n_slots):
                f = d / "recordings" / f"rec_{slot:02d}.wav"
                if f.is_file() and f.stat().st_size > 44:
                    recs.append((slot, f))
            if recs:
                out.append((label, urlp, recs))
        return out

    def _index(self) -> str:
        blocks = []
        for label, urlp, recs in self._projects():
            rows = [f'<div class="proj">{html.escape(label)}</div>']
            for slot, f in recs:
                dur, size = _wav_info(f)
                url = f"/rec/{urlp}/{f.name}"
                rows.append(
                    f'<div class="rec"><div class="n">{slot + 1}</div>'
                    f'<div class="meta"><div class="name">{html.escape(f.name)}</div>'
                    f'<div class="sub">{_fmt_dur(dur)} &nbsp;·&nbsp; {_fmt_size(size)}</div></div>'
                    f'<button class="btn play" data-src="{url}">▶ PLAY</button>'
                    f'<a class="btn dl" href="{url}" download>DOWNLOAD</a>'
                    f'<form method="post" action="/del/{urlp}/{f.name}" style="margin:0" '
                    f'onsubmit="return confirm(\'Delete {html.escape(f.name)}?\')">'
                    f'<button class="btn del" type="submit">DEL</button></form></div>')
            blocks.append("\n".join(rows))
        body = "\n".join(blocks) if blocks else '<div class="none">No recordings yet.</div>'
        return PAGE.format(body=body)


def serve(port: int, perf_dir, n_slots: int) -> None:
    """Start the web UI in a daemon thread. Never raises into the caller."""
    _Handler.perf_dir = Path(perf_dir)
    _Handler.n_slots = int(n_slots)
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", int(port)), _Handler)
    except OSError as e:
        print(f"[wildrider] web UI could not bind :{port} ({e})", flush=True)
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[wildrider] recordings web UI on http://0.0.0.0:{port}", flush=True)
