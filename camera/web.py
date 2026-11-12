"""Giao dien web de demo: video camera (MJPEG) + bang trang thai cap nhat lien tuc.

Khong can cai them goi nao: dung http.server co san cua Python.
  /         trang demo
  /stream   video MJPEG
  /status   trang thai hien tai (JSON)
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2


class WebDashboard:
    def __init__(self, host="127.0.0.1", port=8000):
        self.cond = threading.Condition()
        self.frame = None
        self.status = {}
        self.running = True
        dashboard = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # Khong in log moi request ra terminal

            def do_GET(self):
                if self.path == "/":
                    self._send(200, "text/html; charset=utf-8", PAGE.encode())
                elif self.path == "/status":
                    with dashboard.cond:
                        body = json.dumps(dashboard.status).encode()
                    self._send(200, "application/json", body)
                elif self.path == "/stream":
                    self._stream()
                else:
                    self._send(404, "text/plain", b"not found")

            def _send(self, code, ctype, body):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _stream(self):
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    while dashboard.running:
                        with dashboard.cond:
                            dashboard.cond.wait(timeout=1.0)
                            frame = dashboard.frame
                        if frame is None:
                            continue
                        ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                        if not ok:
                            continue
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n")
                        self.wfile.write(jpg.tobytes())
                        self.wfile.write(b"\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass  # Trinh duyet dong tab

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def update(self, frame, status):
        with self.cond:
            self.frame = frame
            self.status = status
            self.cond.notify_all()

    def close(self):
        self.running = False
        self.server.shutdown()


PAGE = """<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cảnh báo ngủ gật</title>
<style>
  :root { --bg:#0f1820; --card:#18242f; --line:#263644; --text:#eef2f5; --muted:#93a3b1;
          --ok:#3fae7a; --l1:#e2b93b; --l2:#e8833a; --l3:#e0503a; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font-family:-apple-system, "Segoe UI", Roboto, Arial, sans-serif; }
  header { display:flex; align-items:center; justify-content:space-between;
           padding:16px 24px; border-bottom:1px solid var(--line); }
  h1 { margin:0; font-size:20px; font-weight:600; }
  .dot { width:10px; height:10px; border-radius:50%; background:var(--muted);
         display:inline-block; margin-right:8px; }
  .dot.live { background:var(--ok); }
  main { display:grid; grid-template-columns:minmax(0, 1fr) 360px; gap:20px; padding:20px 24px; }
  .video { background:#000; border-radius:14px; overflow:hidden; align-self:start; }
  .video img { display:block; width:100%; height:auto; }
  aside { display:flex; flex-direction:column; gap:14px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:16px 18px; }
  .label { color:var(--muted); font-size:13px; text-transform:uppercase; letter-spacing:.06em; }
  .big { font-size:34px; font-weight:700; margin-top:4px; }
  .row { display:flex; justify-content:space-between; padding:6px 0;
         border-top:1px solid var(--line); font-size:15px; }
  .row:first-of-type { border-top:none; }
  .row span:last-child { font-weight:600; font-variant-numeric:tabular-nums; }
  #level { transition:background .2s; }
  #level.l0 { background:#173326; border-color:var(--ok); }
  #level.l1 { background:#3a3218; border-color:var(--l1); }
  #level.l2 { background:#3d2616; border-color:var(--l2); }
  #level.l3 { background:#431b16; border-color:var(--l3); animation:blink 1s infinite; }
  @keyframes blink { 50% { background:#5e2119; } }
  #reasons { color:var(--muted); margin-top:6px; min-height:20px; }
  .bad { color:#ff8f7a; }
  .good { color:#7fd6a9; }
  @media (max-width: 900px) { main { grid-template-columns:1fr; padding:16px; } }
</style>
</head>
<body>
<header>
  <h1>Hệ thống cảnh báo ngủ gật</h1>
  <div><span id="dot" class="dot"></span><span id="conn">Đang kết nối…</span></div>
</header>
<main>
  <div class="video"><img src="/stream" alt="Hình ảnh camera theo dõi tài xế"></div>
  <aside>
    <div id="level" class="card">
      <div class="label">Trạng thái</div>
      <div id="levelText" class="big">--</div>
      <div id="reasons"></div>
    </div>
    <div class="card">
      <div class="label">Cảm biến MAX30102</div>
      <div class="row"><span>Trạng thái</span><span id="sensor">--</span></div>
      <div class="row"><span>Nhịp tim</span><span id="hr">--</span></div>
      <div class="row"><span>SpO2</span><span id="spo2">--</span></div>
    </div>
    <div class="card">
      <div class="label">Mắt</div>
      <div class="row"><span>Độ mở (EAR / ngưỡng)</span><span id="ear">--</span></div>
      <div class="row"><span>Đang nhắm</span><span id="closed">--</span></div>
      <div class="row"><span>PERCLOS (30 giây)</span><span id="perclos">--</span></div>
      <div class="row"><span>Số lần chớp</span><span id="blinks">--</span></div>
    </div>
    <div class="card">
      <div class="label">Miệng và đầu</div>
      <div class="row"><span>Ngáp (1 phút)</span><span id="yawns">--</span></div>
      <div class="row"><span>Cúi đầu</span><span id="head">--</span></div>
    </div>
  </aside>
</main>
<script>
const NAMES = ["Tỉnh táo", "Mức 1 · Nhắc nhở", "Mức 2 · Cảnh báo", "Mức 3 · Nguy hiểm"];
const VI = {"Binh thuong": "Bình thường", "Cham": "Chậm", "Nhanh": "Nhanh", "Rat nhanh": "Rất nhanh",
            "Thap": "Thấp", "Thieu oxy": "Thiếu oxy", "Nguy hiem": "Nguy hiểm"};
const SENSOR = {"-1": "Mất kết nối cảm biến", "0": "Chưa đặt ngón tay", "1": "Đang khởi động…",
                "2": "Đang đo", "3": "Đã có kết quả", "4": "Tín hiệu nhiễu, nhấc tay đo lại"};
const $ = id => document.getElementById(id);
function setText(id, text, cls) { const el = $(id); el.textContent = text; el.className = cls || ""; }
async function tick() {
  try {
    const s = await (await fetch("/status", {cache: "no-store"})).json();
    $("dot").className = "dot live"; $("conn").textContent = "Đang chạy";
    $("level").className = "card l" + (s.level || 0);
    $("levelText").textContent = s.calibrating ? "Đang hiệu chỉnh…" : NAMES[s.level || 0];
    $("reasons").textContent = s.calibrating ? "Mở mắt, nhìn thẳng vào camera"
                              : (s.reasons ? "Lý do: " + s.reasons : "");
    setText("sensor", s.sensor == null ? (s.esp32 ? "Mất kết nối ESP32" : "Chưa nối ESP32")
                      : SENSOR[s.sensor] + (s.sensor === 2 ? " (" + s.samples + "/3 mẫu)" : ""),
            s.sensor === -1 || s.sensor === 4 ? "bad" : "");
    setText("hr", s.hr ? s.hr + " BPM · " + VI[s.hr_state] : "--", s.hr && s.hr_state !== "Binh thuong" ? "bad" : "good");
    setText("spo2", s.spo2 ? s.spo2 + "% · " + VI[s.spo2_state] : "--", s.spo2 && s.spo2_state !== "Binh thuong" ? "bad" : "good");
    setText("ear", s.ear != null ? s.ear.toFixed(2) + " / " + s.threshold.toFixed(2) : "Không thấy mặt");
    setText("closed", s.closed_for > 0 ? s.closed_for.toFixed(1) + " giây" : "Không", s.closed_for > 0 ? "bad" : "");
    setText("perclos", Math.round(s.perclos * 100) + "%");
    setText("blinks", s.blinks);
    setText("yawns", s.yawns + " lần" + (s.yawning ? " · đang ngáp" : ""), s.yawns ? "bad" : "");
    setText("head", s.head_down ? "Có" : "Không", s.head_down ? "bad" : "");
  } catch (e) {
    $("dot").className = "dot"; $("conn").textContent = "Mất kết nối";
  }
}
setInterval(tick, 300); tick();
</script>
</body>
</html>
"""
