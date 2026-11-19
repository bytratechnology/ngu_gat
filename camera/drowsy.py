import argparse
import collections
import os
import statistics
import threading
import time

import cv2
import mediapipe as mp
import numpy as np
import serial


MODEL_DIR = os.path.join(os.path.dirname(__file__), "models")
AGE_MODEL = os.path.join(MODEL_DIR, "genderage.onnx")
AGE_MODEL_DIR = os.path.join(MODEL_DIR, "AgeModel")

LEFT_EYE = [362, 385, 387, 263, 373, 380]
RIGHT_EYE = [33, 160, 158, 133, 153, 144]
EAR_DEFAULT_THRESHOLD = 0.21
CALIB_FRAMES = 60
EAR_HISTORY = 300

MOUTH = [13, 14, 78, 308]
FOREHEAD, CHIN = 10, 152
MAR_YAWN = 0.6
YAWN_MIN_S = 1.0          # mieng mo rong >= 1 s la 1 lan ngap -> bao ngay
YAWN_WINDOW_S = 60
YAWN_RECENT_S = 10
HEAD_DOWN_DEG = 45        # cui dau >= 45 do so voi tu the binh thuong -> canh bao
HEAD_DOWN_MIN_S = 0.5
HEAD_LOST_MAX_S = 10      # mat mat khi dang cui dau = guc dau, van canh bao toi da 10 s
ALERT_HOLD_S = 5
BLINK_MAX_S = 0.5
CLOSED_ALERT_S = 2.0      # nham mat >= 2 s moi canh bao

PERCLOS_WINDOW_S = 30
HR_BASELINE_N = 20
NO_FACE_GRACE_S = 1.0     # mat mat qua 1 s = khong co nguoi -> dung nhan dien, tat canh bao


HEART_RANGES = [(1, 80, 130), (3, 80, 120), (5, 75, 115), (7, 70, 110), (10, 60, 100)]


def heart_range_for_age(age):
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
    return "Nhanh" if hr < (220 - (age or 30)) * 0.85 else "Rat nhanh"


def classify_spo2(spo2):
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
    if age is None or age < 50:
        return {"closed_s": CLOSED_ALERT_S, "danger_s": 4.0, "hr_drop": 0.88}
    return {"closed_s": CLOSED_ALERT_S, "danger_s": 3.0, "hr_drop": 0.92}


class HeartRateLink:

    def __init__(self, port, baud=115200):
        self.ser = serial.Serial(port, baud, timeout=1) if port else None
        self.hr = None
        self.spo2 = None
        self.hr_time = 0.0
        self.history = []
        self.sensor = None
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
        return self.hr if time.time() - self.hr_time < 10 else None

    def current_spo2(self):
        return self.spo2 if time.time() - self.hr_time < 10 else None

    def sensor_status(self):
        return self.sensor if time.time() - self.sensor_time < 3 else None

    def baseline(self):
        if len(self.history) < HR_BASELINE_N:
            return None
        return statistics.median(self.history[:HR_BASELINE_N])

    def send(self, msg):
        if self.ser:
            self.ser.write((msg + "\n").encode())


class AgeModel:

    def __init__(self):
        from agemodel import load_age_model
        self.model = load_age_model(AGE_MODEL_DIR)
        self.ages = collections.deque(maxlen=15)
        self.busy = False

    def update(self, frame, box):
        if self.busy:
            return
        x, y, w, h = box
        fh, fw = frame.shape[:2]
        m = int(0.15 * max(w, h))
        face = frame[max(0, y - m):min(fh, y + h + m), max(0, x - m):min(fw, x + w + m)].copy()
        cx = x + w // 2
        body = frame[max(0, y - h // 3):fh, max(0, cx - int(1.5 * w)):min(fw, cx + int(1.5 * w))].copy()
        self.busy = True
        threading.Thread(target=self._predict, args=(face, body), daemon=True).start()

    def _predict(self, face, body):
        try:
            self.ages.append(self.model.predict_age(face, body))
        finally:
            self.busy = False

    def age(self):
        if len(self.ages) < 3:
            return None
        return int(round(statistics.median(self.ages)))


class AgeEstimator:

    def __init__(self):
        self.net = cv2.dnn.readNetFromONNX(AGE_MODEL)
        self.ages = collections.deque(maxlen=30)

    def update(self, frame, box):
        x, y, w, h = box
        scale = 96 / (max(w, h) * 1.5)
        cx, cy = x + w / 2, y + h / 2
        m = np.float32([[scale, 0, 48 - cx * scale], [0, scale, 48 - cy * scale]])
        face = cv2.warpAffine(frame, m, (96, 96))
        self.net.setInput(cv2.dnn.blobFromImage(face, 1.0, (96, 96), (0, 0, 0), swapRB=True))
        self.ages.append(float(self.net.forward()[0][2]) * 100)

    def age(self):
        if len(self.ages) < 5:
            return None
        return int(round(statistics.median(self.ages)))


def eye_aspect_ratio(pts):
    p1, p2, p3, p4, p5, p6 = pts
    vertical = np.linalg.norm(p2 - p6) + np.linalg.norm(p3 - p5)
    return vertical / (2.0 * np.linalg.norm(p1 - p4))


class EyeTracker:

    def __init__(self):
        self.mesh = mp.solutions.face_mesh.FaceMesh(
            max_num_faces=1, refine_landmarks=True,
            min_detection_confidence=0.5, min_tracking_confidence=0.5)
        self.calib = collections.deque(maxlen=EAR_HISTORY)
        self.frames = 0
        self.threshold = EAR_DEFAULT_THRESHOLD
        self.blinks = 0
        self.was_closed = False
        self.pitch_history = collections.deque(maxlen=EAR_HISTORY)
        self.pitch = None

    def process(self, frame):
        h, w = frame.shape[:2]
        res = self.mesh.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        if not res.multi_face_landmarks:
            self.pitch = None
            return None
        lm3 = np.array([(p.x * w, p.y * h, p.z * w) for p in res.multi_face_landmarks[0].landmark])
        lm = lm3[:, :2]
        left, right = lm[LEFT_EYE], lm[RIGHT_EYE]
        ear = (eye_aspect_ratio(left) + eye_aspect_ratio(right)) / 2

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

        # Goc cui dau: truc tran -> cam nghieng ve phia sau (z tang) khi cui xuong.
        # Tru di goc nen (tu the binh thuong) de khong phu thuoc vi tri dat camera.
        _, fy, fz = lm3[FOREHEAD]
        _, cy, cz = lm3[CHIN]
        raw = float(np.degrees(np.arctan2(cz - fz, cy - fy)))
        self.pitch = raw if self.pitch is None else 0.7 * self.pitch + 0.3 * raw
        pitch = 0.0
        if len(self.pitch_history) >= CALIB_FRAMES:
            pitch = self.pitch - float(np.median(self.pitch_history))
        if pitch < HEAD_DOWN_DEG / 2:
            self.pitch_history.append(self.pitch)

        return {"box": (x0, y0, x1 - x0, face_h), "left": left, "right": right,
                "mouth": lm[MOUTH], "ear": ear, "closed": closed, "mar": mar,
                "pitch": pitch, "head_down": pitch >= HEAD_DOWN_DEG}

    @property
    def calibrating(self):
        return self.frames < CALIB_FRAMES


ESP32_USB_VIDS = {0x10C4, 0x1A86, 0x0403, 0x303A}


def find_esp32_port():
    from serial.tools import list_ports
    ports = list_ports.comports()
    for p in ports:
        if p.vid in ESP32_USB_VIDS:
            return p.device
    for p in ports:
        if any(k in p.device for k in ("usbserial", "SLAB", "ttyUSB", "ttyACM")):
            return p.device
    return None


def draw_sensor_panel(frame, link, age, port):
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
    ap.add_argument("--port", help="cong Serial ESP32, 'auto' = tu do (bo trong = chi chay camera)")
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument("--age-model", choices=["agemodel", "insightface"], default="agemodel")
    args = ap.parse_args()

    eyes_tracker = EyeTracker()
    print("Dang tai mo hinh tuoi", args.age_model, "...")
    ager = AgeModel() if args.age_model == "agemodel" else AgeEstimator()
    if args.port == "auto":
        args.port = find_esp32_port()
        print("ESP32:", args.port or "khong tim thay - chi chay camera")
    link = HeartRateLink(args.port)
    cap = cv2.VideoCapture(args.camera)

    eye_log = collections.deque()
    closed_since = None
    yawn_since, yawn_counted = None, False
    yawn_times = collections.deque()
    head_down, head_down_since, lost_head_down = False, None, False
    held_level, held_until = 0, 0.0
    last_face = 0.0
    level_sent, last_send = -1, 0.0
    frame_no = 0

    while True:
        ok, frame = cap.read()
        if not ok:
            print("Khong doc duoc hinh tu camera (may ngu, ung dung khac dang dung camera?) - dung chuong trinh")
            break
        frame = cv2.flip(frame, 1)
        frame_no += 1
        now = time.time()
        found = eyes_tracker.process(frame)

        eyes_closed, ear, mar, yawning, pitch, new_yawn = None, None, 0.0, False, None, False
        if found:
            last_face, lost_head_down = now, False
            box, ear, eyes_closed = found["box"], found["ear"], found["closed"]
            left, right, mar, pitch = found["left"], found["right"], found["mar"], found["pitch"]
            head_down_since = (head_down_since or now) if found["head_down"] else None
            head_down = head_down_since is not None and now - head_down_since >= HEAD_DOWN_MIN_S

            if mar > MAR_YAWN:
                yawn_since = yawn_since or now
                yawning = now - yawn_since >= YAWN_MIN_S
                if yawning and not yawn_counted:
                    yawn_times.append(now)
                    yawn_counted, new_yawn = True, True
            else:
                yawn_since, yawn_counted = None, False
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
                eyes_closed = None
        elif head_down:
            lost_head_down = True

        no_face_for = now - last_face
        head_lost = lost_head_down and no_face_for <= HEAD_LOST_MAX_S
        present = found is not None or no_face_for <= NO_FACE_GRACE_S or head_lost
        if not present:
            # Khong co nguoi: dung nhan dien, xoa trang thai, khong canh bao
            eye_log.clear()
            closed_since, yawn_since, yawn_counted = None, None, False
            head_down, head_down_since, lost_head_down = False, None, False
            held_level, held_until = 0, 0.0

        if eyes_closed is not None:
            closed_since = (closed_since or now) if eyes_closed else None
            eye_log.append((now, eyes_closed and now - closed_since >= BLINK_MAX_S))
        while eye_log and now - eye_log[0][0] > PERCLOS_WINDOW_S:
            eye_log.popleft()
        perclos = sum(c for _, c in eye_log) / len(eye_log) if eye_log else 0.0
        closed_for = now - closed_since if closed_since else 0.0
        while yawn_times and now - yawn_times[0] > YAWN_WINDOW_S:
            yawn_times.popleft()
        yawns = len(yawn_times)
        yawn_recent = bool(yawn_times) and now - yawn_times[-1] < YAWN_RECENT_S

        age = ager.age()
        th = thresholds_for_age(age)
        hr, base = link.current(), link.baseline()
        spo2 = link.current_spo2()
        hr_low = hr is not None and (
            (base is not None and hr < base * th["hr_drop"])
            or hr < heart_range_for_age(age)[0])

        conditions = [
            (3, closed_for >= th["danger_s"], f"nham mat {closed_for:.1f}s"),
            (3, head_lost, "guc dau (mat khoi camera)"),
            (3, head_down and bool(eyes_closed), "cui dau + nham mat"),
            (3, closed_for >= th["closed_s"] and hr_low, "nham mat + nhip tim thap"),
            (2, closed_for >= th["closed_s"], f"nham mat {closed_for:.1f}s"),
            (2, head_down and not head_lost, f"cui dau {pitch or 0:.0f} do"),
            (2, yawns >= 2, f"ngap {yawns} lan/phut"),
            (1, yawn_recent, "vua ngap"),
            (1, hr_low, f"nhip tim thap ({hr})"),
        ] if present else []
        active = [(lv, why) for lv, ok, why in conditions if ok]
        level = max((lv for lv, _ in active), default=0)
        if level:
            reasons = ", ".join(why for lv, why in active if lv == level)
        elif held_level and now <= held_until:
            reasons = f"giu canh bao them {held_until - now:.0f}s"
        else:
            reasons = ""
        if level >= held_level or now > held_until:
            if level > 0:
                held_until = now + ALERT_HOLD_S
            held_level = level
        level = held_level

        if new_yawn and level == 1 and level_sent == 1:
            link.send("@ALERT,0")   # dang o muc 1 -> ha xuong roi gui lai de ESP32 phat lai cho lan ngap moi
            level_sent = 0
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
            f"  Cui dau: {f'{pitch:.0f}' if pitch is not None else '--'}/{HEAD_DOWN_DEG} do"
            f"{'  CUI DAU' if head_down else ''}",
            "Khong co nguoi - tam dung nhan dien"
            if not present
            else "Dang hieu chinh - mo mat nhin thang"
            if eyes_tracker.calibrating
            else ["Tinh tao", "Muc 1: Nhac nho", "Muc 2: Canh bao", "Muc 3: NGUY HIEM"][level]
            + (f" - Ly do: {reasons}" if reasons else ""),
        ]
        for i, text in enumerate(info):
            cv2.putText(frame, text, (10, 25 + 25 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
        draw_sensor_panel(frame, link, age, args.port)
        cv2.imshow("Canh bao ngu gat", frame)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

    link.send("@ALERT,0")
    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
