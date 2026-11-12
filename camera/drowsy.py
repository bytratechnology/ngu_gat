"""Canh bao ngu gat: camera (OpenCV) + nhip tim tu ESP32 qua Serial.

- Khuon mat + 2 mat: MediaPipe Face Mesh (478 diem moc), do do mo mat EAR
- Tuoi: MiVOLO v2 (mat + than nguoi, Hugging Face iitolstykh/mivolo_v2) chay o luong rieng;
  --age-model insightface de dung genderage.onnx nhe hon (tai bang download_models.py)
- Ngap: do mo mieng MAR tren Face Mesh; cui dau: mui tut xuong so voi tu the binh thuong
- Ket hop: thoi gian nham mat, PERCLOS, ngap, cui dau va nhip tim -> canh bao 3 muc,
  ESP32 phat warning1/2/3.mp3 tuong ung
Chay: python drowsy.py --port /dev/cu.usbserial-XXXX
"""
import argparse
import collections
import os
import statistics
import threading
import time
import webbrowser

import cv2
import mediapipe as mp
import numpy as np
import serial

from web import WebDashboard

MODEL_DIR = os.path.join(os.path.dirname(__file__), "models")
AGE_MODEL = os.path.join(MODEL_DIR, "genderage.onnx")
MIVOLO_REPO = "iitolstykh/mivolo_v2"
MIVOLO_REVISION = "e8bdd8aec21ae468cbfe73b41f52550105ad8fbf"  # Ghim ban ma da kiem tra

# 6 diem moc moi mat tren Face Mesh: goc ngoai, 2 diem mi tren, goc trong, 2 diem mi duoi
LEFT_EYE = [362, 385, 387, 263, 373, 380]
RIGHT_EYE = [33, 160, 158, 133, 153, 144]
EAR_DEFAULT_THRESHOLD = 0.21  # Dung tam khi chua hieu chinh xong
CALIB_FRAMES = 60             # So khung dau truoc khi bat dau xet nham mat
EAR_HISTORY = 300             # ~10s EAR gan nhat de cap nhat nguong lien tuc

# Mieng: moi tren/duoi trong (13, 14), 2 khoe mieng (78, 308); mui (1)
MOUTH = [13, 14, 78, 308]
NOSE_TIP = 1
MAR_YAWN = 0.6                # Mo mieng > 60% be rong mieng = dang ha mieng rong
YAWN_MIN_S = 1.5              # Ha mieng lau hon the nay moi tinh la ngap (noi chuyen ngan hon)
YAWN_WINDOW_S = 60            # Dem so lan ngap trong 1 phut gan nhat
HEAD_DOWN_RATIO = 0.25        # Mui tut xuong > 25% chieu cao mat so voi binh thuong = cui dau
ALERT_HOLD_S = 5              # Giu muc canh bao toi thieu 5s de am thanh khong bi ngat quang
YAWN_RECENT_S = 10            # 1 lan ngap chi giu muc 1 trong 10s (2 lan/phut moi len muc 2)
BLINK_MAX_S = 0.5             # Nham ngan hon the nay la chop mat binh thuong, khong tinh PERCLOS

PERCLOS_WINDOW_S = 30      # Cua so tinh ti le nham mat
HR_BASELINE_N = 20         # So mau HR dau tien lam muc nen
NO_FACE_WARN_S = 3         # Khong thay mat lau -> canh bao nhe (cui dau/quay di)


# Nhip tim luc nghi binh thuong theo tuoi (MedlinePlus/NIH) - giong bang trong firmware
HEART_RANGES = [(1, 80, 130), (3, 80, 120), (5, 75, 115), (7, 70, 110), (10, 60, 100)]


def heart_range_for_age(age):
    """Tra ve (thap, cao) BPM binh thuong; chua biet tuoi thi dung nguong nguoi lon."""
    low, high = HEART_RANGES[-1][1:]
    if age:
        for min_age, lo, hi in HEART_RANGES:
            if age >= min_age:
                low, high = lo, hi
    return low, high


def classify_heart_rate(hr, age):
    low, high = heart_range_for_age(age)
    if hr < low:
        return "Cham"
    if hr <= high:
        return "Binh thuong"
    # Luc nghi ma vuot 85% nhip tim toi da (220 - tuoi) la bat thuong
    return "Nhanh" if hr < (220 - (age or 30)) * 0.85 else "Rat nhanh"


def classify_spo2(spo2):
    """SpO2 (WHO) - giong firmware."""
    if spo2 >= 95:
        return "Binh thuong"
    if spo2 >= 91:
        return "Thap"
    if spo2 >= 86:
        return "Thieu oxy"
    return "Nguy hiem"


SENSOR_STATES = {-1: "Mat ket noi cam bien", 0: "Chua dat ngon tay", 1: "Dang khoi dong...",
                 2: "Dang do", 3: "Da co ket qua", 4: "Tin hieu nhieu - nhac tay do lai"}


def thresholds_for_age(age):
    """Nguoi lon tuoi phan xa cham hon va nhip tim nen thap hon -> nguong chat hon."""
    if age is None or age < 50:
        return {"closed_s": 1.5, "danger_s": 3.0, "perclos": 0.30, "hr_drop": 0.88}
    return {"closed_s": 1.0, "danger_s": 2.0, "perclos": 0.25, "hr_drop": 0.92}


class HeartRateLink:
    """Doc '@HR'/'@SENSOR' tu ESP32 o luong rieng, gui lenh '@ALERT'/'@AGE'."""

    def __init__(self, port, baud=115200):
        self.ser = serial.Serial(port, baud, timeout=1) if port else None
        self.hr = None
        self.spo2 = None
        self.hr_time = 0.0
        self.history = []
        self.sensor = None        # Trang thai '@SENSOR' moi nhat
        self.sensor_time = 0.0
        if self.ser:
            threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        while True:
            line = self.ser.readline().decode(errors="ignore").strip()
            if line.startswith("@SENSOR,"):
                try:
                    st, live_hr, live_spo2, res_hr, res_spo2, count = map(int, line.split(",")[1:7])
                except ValueError:
                    continue
                self.sensor = {"state": st, "live_hr": live_hr, "live_spo2": live_spo2,
                               "result_hr": res_hr, "result_spo2": res_spo2, "count": count}
                self.sensor_time = time.time()
                continue
            if not line.startswith("@HR,"):
                continue
            try:
                _, hr, spo2 = line.split(",")[:3]
                hr, spo2 = int(hr), int(spo2)
            except ValueError:
                continue
            self.hr, self.hr_time = hr, time.time()
            self.spo2 = spo2 if 70 <= spo2 <= 100 else None
            self.history.append(hr)

    def current(self):
        # Nhip tim cu hon 10s coi nhu khong co (nhac tay khoi cam bien)
        return self.hr if time.time() - self.hr_time < 10 else None

    def current_spo2(self):
        return self.spo2 if time.time() - self.hr_time < 10 else None

    def sensor_status(self):
        # Qua 3s khong nhan duoc '@SENSOR' coi nhu mat ket noi ESP32
        return self.sensor if time.time() - self.sensor_time < 3 else None

    def baseline(self):
        if len(self.history) < HR_BASELINE_N:
            return None
        return statistics.median(self.history[:HR_BASELINE_N])

    def send(self, msg):
        if self.ser:
            self.ser.write((msg + "\n").encode())


class MivoloAgeEstimator:
    """MiVOLO v2: doan tuoi tu ca mat va than nguoi, chinh xac hon nhieu nhung cham
    (~0.5-1s/lan tren CPU) nen chay o luong rieng, khong lam giat camera."""

    def __init__(self):
        import torch
        from transformers import AutoImageProcessor, AutoModelForImageClassification
        self.torch = torch
        self.model = AutoModelForImageClassification.from_pretrained(
            MIVOLO_REPO, revision=MIVOLO_REVISION, trust_remote_code=True,
            torch_dtype=torch.float32).eval()
        self.proc = AutoImageProcessor.from_pretrained(
            MIVOLO_REPO, revision=MIVOLO_REVISION, trust_remote_code=True)
        self.ages = collections.deque(maxlen=15)
        self.busy = False

    def update(self, frame, box):
        if self.busy:
            return  # Lan doan truoc chua xong thi bo qua khung nay
        x, y, w, h = box
        fh, fw = frame.shape[:2]
        # Mo rong khung mat (khung MediaPipe sat hon khung YOLO luc huan luyen)
        m = int(0.15 * max(w, h))
        face = frame[max(0, y - m):min(fh, y + h + m), max(0, x - m):min(fw, x + w + m)].copy()
        # Than nguoi: rong 3 lan mat, tu tran xuong het khung hinh
        cx = x + w // 2
        body = frame[max(0, y - h // 3):fh, max(0, cx - int(1.5 * w)):min(fw, cx + int(1.5 * w))].copy()
        self.busy = True
        threading.Thread(target=self._predict, args=(face, body), daemon=True).start()

    def _predict(self, face, body):
        try:
            with self.torch.no_grad():
                faces = self.proc(images=[face])["pixel_values"].to(self.torch.float32)
                bodies = self.proc(images=[body])["pixel_values"].to(self.torch.float32)
                out = self.model(faces_input=faces, body_input=bodies)
            self.ages.append(float(out.age_output[0].item()))
        finally:
            self.busy = False

    def age(self):
        if len(self.ages) < 3:
            return None
        return int(round(statistics.median(self.ages)))


class AgeEstimator:
    """InsightFace genderage: hoi quy tuoi lien tuc, lay trung vi nhieu lan doan."""

    def __init__(self):
        self.net = cv2.dnn.readNetFromONNX(AGE_MODEL)
        self.ages = collections.deque(maxlen=30)

    def update(self, frame, box):
        # Cat vuong quanh mat rong 1.5 lan, dua tam mat ve giua anh 96x96 nhu luc huan luyen
        x, y, w, h = box
        scale = 96 / (max(w, h) * 1.5)
        cx, cy = x + w / 2, y + h / 2
        m = np.float32([[scale, 0, 48 - cx * scale], [0, scale, 48 - cy * scale]])
        face = cv2.warpAffine(frame, m, (96, 96))
        # Mo hinh tu tru/nhan chuan hoa ben trong, chi can doi BGR -> RGB
        self.net.setInput(cv2.dnn.blobFromImage(face, 1.0, (96, 96), (0, 0, 0), swapRB=True))
        self.ages.append(float(self.net.forward()[0][2]) * 100)

    def age(self):
        if len(self.ages) < 5:
            return None
        return int(round(statistics.median(self.ages)))


def eye_aspect_ratio(pts):
    """EAR = tong 2 khoang cach mi tren-duoi / (2 * do rong mat); ~0.3 mo, <0.2 nham."""
    p1, p2, p3, p4, p5, p6 = pts
    vertical = np.linalg.norm(p2 - p6) + np.linalg.norm(p3 - p5)
    return vertical / (2.0 * np.linalg.norm(p1 - p4))


class EyeTracker:
    """Theo doi 2 mat bang MediaPipe Face Mesh, nguong EAR hieu chinh theo tung nguoi."""

    def __init__(self):
        self.mesh = mp.solutions.face_mesh.FaceMesh(
            max_num_faces=1, refine_landmarks=True,
            min_detection_confidence=0.5, min_tracking_confidence=0.5)
        self.calib = collections.deque(maxlen=EAR_HISTORY)
        self.frames = 0
        self.threshold = EAR_DEFAULT_THRESHOLD
        self.blinks = 0
        self.was_closed = False
        self.nose_history = collections.deque(maxlen=EAR_HISTORY)

    def process(self, frame):
        """Tra ve dict thong tin mat/mieng/dau, hoac None neu khong thay mat."""
        h, w = frame.shape[:2]
        res = self.mesh.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        if not res.multi_face_landmarks:
            return None
        lm = np.array([(p.x * w, p.y * h) for p in res.multi_face_landmarks[0].landmark])
        left, right = lm[LEFT_EYE], lm[RIGHT_EYE]
        ear = (eye_aspect_ratio(left) + eye_aspect_ratio(right)) / 2

        # Nguong = 70% EAR luc mo mat (phan vi 90 cua ~10s gan nhat), cap nhat
        # lien tuc de khong bi ket nguong sai neu luc dau dang cui/liec xuong
        self.calib.append(ear)
        self.frames += 1
        if self.frames >= CALIB_FRAMES:
            open_ear = float(np.percentile(self.calib, 90))
            self.threshold = max(0.15, min(0.28, open_ear * 0.7))

        closed = ear < self.threshold
        if self.was_closed and not closed and not self.calibrating:
            self.blinks += 1
        self.was_closed = closed

        x0, y0 = lm.min(axis=0).astype(int)
        x1, y1 = lm.max(axis=0).astype(int)
        face_h = max(1, y1 - y0)

        top, bottom, lcorner, rcorner = lm[MOUTH]
        mar = np.linalg.norm(top - bottom) / max(1e-6, np.linalg.norm(lcorner - rcorner))

        # Vi tri mui so voi trung vi ~10s gan nhat (tu the ngoi binh thuong)
        nose_y = lm[NOSE_TIP][1]
        head_drop = 0.0
        if len(self.nose_history) >= CALIB_FRAMES:
            head_drop = (nose_y - float(np.median(self.nose_history))) / face_h
        if head_drop < HEAD_DOWN_RATIO / 2:
            self.nose_history.append(nose_y)  # Khong lay luc dang cui lam muc chuan

        return {"box": (x0, y0, x1 - x0, face_h), "left": left, "right": right,
                "mouth": lm[MOUTH], "ear": ear, "closed": closed, "mar": mar,
                "head_down": head_drop > HEAD_DOWN_RATIO}

    @property
    def calibrating(self):
        return self.frames < CALIB_FRAMES


def web_status(level, reasons, tracker, ear, closed_for, perclos, yawns, yawning,
               head_down, link, age, port):
    """Trang thai gui cho trang web (/status)."""
    sensor = link.sensor_status()
    hr = spo2 = None
    if sensor:
        measuring = sensor["state"] in (1, 2)
        hr = (sensor["live_hr"] if measuring else sensor["result_hr"]) or None
        spo2 = (sensor["live_spo2"] if measuring else sensor["result_spo2"]) or None
    return {
        "level": level, "reasons": reasons, "calibrating": tracker.calibrating,
        "ear": ear, "threshold": tracker.threshold, "closed_for": closed_for,
        "perclos": perclos, "blinks": tracker.blinks, "yawns": yawns, "yawning": yawning,
        "head_down": bool(head_down), "esp32": bool(port),
        "sensor": sensor["state"] if sensor else None,
        "samples": sensor["count"] if sensor else 0,
        "hr": hr, "hr_state": classify_heart_rate(hr, age) if hr else None,
        "spo2": spo2, "spo2_state": classify_spo2(spo2) if spo2 else None,
    }


def draw_sensor_panel(frame, link, age, port):
    """Khung thong tin cam bien MAX30102 o goc duoi phai, cap nhat moi giay."""
    sensor = link.sensor_status()
    low, high = heart_range_for_age(age)
    lines = [("CAM BIEN MAX30102", (255, 255, 255))]
    if not port:
        lines.append(("Chua ket noi ESP32 (--port)", (160, 160, 160)))
    elif sensor is None:
        lines.append(("Mat ket noi ESP32", (0, 0, 255)))
    else:
        st = sensor["state"]
        lines.append((SENSOR_STATES.get(st, "?") +
                      (f" ({sensor['count']}/3 mau)" if st == 2 else ""),
                      (0, 0, 255) if st in (-1, 4) else (255, 255, 255)))
        # Dang do: hien gia tri tam; con lai: hien ket qua chot gan nhat
        hr = sensor["live_hr"] if st in (1, 2) else sensor["result_hr"]
        spo2 = sensor["live_spo2"] if st in (1, 2) else sensor["result_spo2"]
        tag = "tam" if st in (1, 2) else "ket qua"
        if hr:
            hr_state = classify_heart_rate(hr, age)
            ok = hr_state == "Binh thuong"
            lines.append((f"Nhip tim: {hr} BPM ({tag}) - {hr_state}",
                          (0, 200, 0) if ok else (0, 0, 255)))
        else:
            lines.append(("Nhip tim: --", (200, 200, 200)))
        lines.append((f"  Binh thuong theo tuoi {age or '--'}: {low}-{high} BPM", (200, 200, 200)))
        if spo2:
            spo2_state = classify_spo2(spo2)
            ok = spo2_state == "Binh thuong"
            lines.append((f"SpO2: {spo2}% ({tag}) - {spo2_state}",
                          (0, 200, 0) if ok else (0, 0, 255)))
        else:
            lines.append(("SpO2: --%", (200, 200, 200)))
        lines.append(("  Binh thuong: >= 95%  (91-94 thap, <= 90 thieu oxy)", (200, 200, 200)))

    h, w = frame.shape[:2]
    pw, ph = 500, 14 + 24 * len(lines)
    x0, y0 = w - pw - 10, h - ph - 10
    overlay = frame.copy()
    cv2.rectangle(overlay, (x0, y0), (x0 + pw, y0 + ph), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.6, frame, 0.4, 0, frame)
    for i, (text, col) in enumerate(lines):
        cv2.putText(frame, text, (x0 + 10, y0 + 26 + 24 * i),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, col, 1, cv2.LINE_AA)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", help="cong Serial ESP32 (bo trong = chi chay camera)")
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument("--age-model", choices=["mivolo", "insightface"], default="mivolo")
    ap.add_argument("--web", action="store_true", help="hien thi tren trinh duyet thay cho cua so")
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 de xem tu may khac cung mang")
    ap.add_argument("--web-port", type=int, default=8000)
    args = ap.parse_args()

    eyes_tracker = EyeTracker()
    print("Dang tai mo hinh tuoi", args.age_model, "...")
    ager = MivoloAgeEstimator() if args.age_model == "mivolo" else AgeEstimator()
    link = HeartRateLink(args.port)
    web = None
    if args.web:
        web = WebDashboard(args.host, args.web_port)
        print("Mo trinh duyet tai", web.url, "(Ctrl+C de dung)")
        webbrowser.open(web.url)
    cap = cv2.VideoCapture(args.camera)

    eye_log = collections.deque()   # (thoi diem, nham mat?)
    closed_since = None
    yawn_since, yawn_counted = None, False
    yawn_times = collections.deque()
    head_down = False
    held_level, held_until = 0, 0.0
    last_face = time.time()
    level_sent, last_send = -1, 0.0
    frame_no = 0

    while True:
        ok, frame = cap.read()
        if not ok:
            print("Khong doc duoc hinh tu camera (may ngu, ung dung khac dang dung camera?) - dung chuong trinh")
            break
        # Lat ngang nhu soi guong de hinh hien thi dung chieu voi nguoi lai
        frame = cv2.flip(frame, 1)
        frame_no += 1
        now = time.time()
        found = eyes_tracker.process(frame)

        eyes_closed, ear, mar, yawning = None, None, 0.0, False
        if found:
            last_face = now
            box, ear, eyes_closed = found["box"], found["ear"], found["closed"]
            left, right, mar, head_down = found["left"], found["right"], found["mar"], found["head_down"]

            # Ngap: ha mieng rong lien tuc >= YAWN_MIN_S, moi lan ngap chi dem 1
            if mar > MAR_YAWN:
                yawn_since = yawn_since or now
                yawning = now - yawn_since >= YAWN_MIN_S
                if yawning and not yawn_counted:
                    yawn_times.append(now)
                    yawn_counted = True
            else:
                yawn_since, yawn_counted = None, False
            # Khi ngap mat thuong nheo lai -> khong tinh la nham mat
            if mar > MAR_YAWN:
                eyes_closed = False

            if frame_no % 5 == 0:
                ager.update(frame, box)
            x, y, w, h = box
            cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 1)
            eye_color = (0, 0, 255) if eyes_closed else (255, 200, 0)
            for pts in (left, right):
                cv2.polylines(frame, [pts.astype(np.int32)], True, eye_color, 2)
            mouth_color = (0, 0, 255) if yawning else (0, 255, 255)
            for px, py in found["mouth"].astype(int):
                cv2.circle(frame, (px, py), 3, mouth_color, -1)
            if eyes_tracker.calibrating:
                eyes_closed = None  # Chua tinh nham mat khi dang hieu chinh

        if eyes_closed is not None:
            closed_since = (closed_since or now) if eyes_closed else None
            # Chi tinh nhung lan nham lau hon 1 cai chop mat vao PERCLOS
            eye_log.append((now, eyes_closed and now - closed_since >= BLINK_MAX_S))
        while eye_log and now - eye_log[0][0] > PERCLOS_WINDOW_S:
            eye_log.popleft()
        perclos = sum(c for _, c in eye_log) / len(eye_log) if eye_log else 0.0
        closed_for = now - closed_since if closed_since else 0.0
        while yawn_times and now - yawn_times[0] > YAWN_WINDOW_S:
            yawn_times.popleft()
        yawns = len(yawn_times)
        yawn_recent = bool(yawn_times) and now - yawn_times[-1] < YAWN_RECENT_S
        no_face_for = now - last_face

        age = ager.age()
        th = thresholds_for_age(age)
        hr, base = link.current(), link.baseline()
        spo2 = link.current_spo2()
        # Nhip tim thap: giam so voi muc nen cua chinh nguoi lai, hoac duoi nguong theo tuoi
        hr_low = hr is not None and (
            (base is not None and hr < base * th["hr_drop"])
            or hr < heart_range_for_age(age)[0])

        # Tung dieu kien kem ly do hien len man hinh, muc cao nhat dat duoc se thang
        conditions = [
            # Muc 3 (nguy hiem): nham mat bat thuong lau, cui dau ngu gat
            (3, closed_for >= th["danger_s"], f"nham mat {closed_for:.1f}s"),
            (3, head_down and (eyes_closed or no_face_for > 0), "cui dau + nham mat"),
            (3, closed_for >= th["closed_s"] and hr_low, "nham mat + nhip tim thap"),
            (3, no_face_for > NO_FACE_WARN_S * 2, f"mat khoi camera {no_face_for:.0f}s"),
            # Muc 2 (canh bao): nham mat keo dai, ngap lien tuc, PERCLOS cao
            (2, closed_for >= th["closed_s"], f"nham mat {closed_for:.1f}s"),
            (2, yawns >= 2, f"ngap {yawns} lan/phut"),
            (2, perclos >= th["perclos"], f"PERCLOS {perclos:.0%}"),
            (2, hr_low and perclos >= th["perclos"] / 2, "nhip tim thap + PERCLOS"),
            # Muc 1 (nhac nho): dau hieu nhe
            (1, yawn_recent, "vua ngap"),
            (1, perclos >= th["perclos"] / 2, f"PERCLOS {perclos:.0%}"),
            (1, hr_low, f"nhip tim thap ({hr})"),
            (1, head_down, "cui dau"),
            (1, no_face_for > NO_FACE_WARN_S, f"khong thay mat {no_face_for:.0f}s"),
        ]
        active = [(lv, why) for lv, ok, why in conditions if ok]
        level = max((lv for lv, _ in active), default=0)
        if level:
            reasons = ", ".join(why for lv, why in active if lv == level)
        elif held_level and now <= held_until:
            reasons = f"giu canh bao them {held_until - now:.0f}s"
        else:
            reasons = ""
        # Giu muc cao trong ALERT_HOLD_S de loa phat het cau canh bao
        if level >= held_level or now > held_until:
            if level > 0:
                held_until = now + ALERT_HOLD_S
            held_level = level
        level = held_level

        # Gui lai dinh ky de ESP32 khong tu tat do timeout
        if level != level_sent or now - last_send > 1.0:
            link.send(f"@ALERT,{level}")
            if age:
                link.send(f"@AGE,{age}")
            level_sent, last_send = level, now

        color = [(0, 200, 0), (0, 220, 255), (0, 120, 255), (0, 0, 255)][level]
        info = [
            f"Tuoi: {age or '--'}  HR: {hr or '--'} (nen {base or '--'})"
            + (f" - {classify_heart_rate(hr, age)} "
               f"[{'-'.join(map(str, heart_range_for_age(age)))}]" if hr else "")
            + f"  SpO2: {spo2 or '--'}%" + (f" - {classify_spo2(spo2)}" if spo2 else ""),
            f"EAR: {ear:.2f}/{eyes_tracker.threshold:.2f}  Chop mat: {eyes_tracker.blinks}"
            if ear is not None else "Khong thay mat",
            f"Nham mat: {closed_for:.1f}s  PERCLOS: {perclos:.0%}",
            f"MAR: {mar:.2f}  Ngap: {yawns} lan/phut{'  (DANG NGAP)' if yawning else ''}"
            f"{'  CUI DAU' if head_down else ''}",
            "Dang hieu chinh - mo mat nhin thang"
            if eyes_tracker.calibrating
            else ["Tinh tao", "Muc 1: Nhac nho", "Muc 2: Canh bao", "Muc 3: NGUY HIEM"][level]
            + (f" - Ly do: {reasons}" if reasons else ""),
        ]
        if web:
            # Che do web: thong tin nam o bang ben canh, video chi ve mat/mieng
            web.update(frame, web_status(level, reasons, eyes_tracker, ear, closed_for, perclos,
                                         yawns, yawning, head_down, link, age, args.port))
            continue
        for i, text in enumerate(info):
            cv2.putText(frame, text, (10, 25 + 25 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
        draw_sensor_panel(frame, link, age, args.port)
        cv2.imshow("Canh bao ngu gat", frame)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

    link.send("@ALERT,0")
    cap.release()
    if web:
        web.close()
    else:
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
