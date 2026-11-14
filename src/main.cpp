#include <Arduino.h>
#include <Wire.h>
#include <Preferences.h>
#include <SPI.h>
#include <SD.h>
#include <U8g2lib.h>
#include "DFRobot_BloodOxygen_S.h"
#include "AudioFileSourceSD.h"
#include "AudioFileSourceBuffer.h"
#include "AudioGeneratorMP3.h"
#include "AudioOutputI2S.h"

#define SDA_PIN 21
#define SCL_PIN 32
#define I2C_ADDRESS 0x57

#define OLED_SDA_PIN 16
#define OLED_SCL_PIN 17

#define I2S_BCLK 26
#define I2S_LRC 25
#define I2S_DIN 22

#define SD_SCK 18
#define SD_MISO 19
#define SD_MOSI 23
#define SD_CS 5

#define SENSOR_REPORT_MS 1000
#define ALERT_TIMEOUT_MS 3000
#define MAX_ALERT_LEVEL 3
#define AUDIO_GAIN 0.1
const uint32_t REPEAT_GAP_MS[MAX_ALERT_LEVEL + 1] = {0, 0, 8000, 1500};

#define POLL_INTERVAL_MS 500
#define MODULE_UPDATE_MS 4000
#define WARMUP_READINGS 1
#define SAMPLES_PER_SESSION 3
#define MAX_MISSED_READINGS 1
#define SESSION_TIMEOUT_MS 40000

#define MAX_HR_JUMP 25
#define MAX_SPO2_JUMP 4
#define MAX_HR_SPREAD 15
#define MAX_SPO2_SPREAD 3
#define MAX_CONSECUTIVE_REJECTS 3

DFRobot_BloodOxygen_S_I2C MAX30102(&Wire, I2C_ADDRESS);

U8G2_SH1106_128X64_NONAME_F_2ND_HW_I2C display(U8G2_R0, U8X8_PIN_NONE);

Preferences prefs;

enum State {
  STATE_IDLE,
  STATE_WARMUP,
  STATE_MEASURING,
  STATE_DONE,
  STATE_FAILED
};

struct Session {
  uint32_t id;
  uint32_t startMs;
  int warmupLeft;
  int hr[SAMPLES_PER_SESSION];
  int spo2[SAMPLES_PER_SESSION];
  int count;
  int rejected;
  int consecutiveRejects;
  int liveHr;
  int liveSpo2;
  int resultHr;
  int resultSpo2;
  uint32_t durationSec;
};

State state = STATE_IDLE;
Session session;
bool hasLastResult = false;
int missedReadings = 0;
uint32_t lastReadMs = 0;

int lastRawHr = 0;
int lastRawSpo2 = 0;
uint32_t lastUpdateMs = 0;

int alertLevel = 0;
int driverAge = 0;
uint32_t lastAlertMsgMs = 0;
char rxLine[32];
int rxLen = 0;

bool sdReady = false;
bool sensorReady = false;
uint32_t lastSensorRetryMs = 0;
volatile int audioRequest = 0;
volatile bool audioPlaying = false;
uint32_t lastAudioEndMs = 0;

struct Status {
  const char *shortText;
  const char *fullText;
  bool normal;
};

struct HeartRange {
  int minAge;
  int low;
  int high;
};
const HeartRange HEART_RANGES[] = {
  {1, 80, 130},
  {3, 80, 120},
  {5, 75, 115},
  {7, 70, 110},
  {10, 60, 100},
};

HeartRange heartRangeForAge(int age) {
  HeartRange range = HEART_RANGES[sizeof(HEART_RANGES) / sizeof(HEART_RANGES[0]) - 1];
  if (age <= 0) return range;
  for (const HeartRange &r : HEART_RANGES) {
    if (age >= r.minAge) range = r;
  }
  return range;
}

int maxHeartRate(int age) {
  return 220 - (age > 0 ? age : 30);
}

Status classifyHeartRate(int bpm) {
  static char fullText[64];
  HeartRange r = heartRangeForAge(driverAge);
  int danger = maxHeartRate(driverAge) * 85 / 100;
  char ageText[16];
  snprintf(ageText, sizeof(ageText), driverAge > 0 ? "tuoi %d" : "nguoi lon",
           driverAge);

  if (bpm < r.low) {
    snprintf(fullText, sizeof(fullText), "Nhip tim cham (< %d BPM, %s)", r.low,
             ageText);
    return {"Cham", fullText, false};
  }
  if (bpm <= r.high) {
    snprintf(fullText, sizeof(fullText), "Binh thuong (%d-%d BPM, %s)", r.low,
             r.high, ageText);
    return {"Binh thuong", fullText, true};
  }
  if (bpm < danger) {
    snprintf(fullText, sizeof(fullText), "Nhip tim nhanh (> %d BPM, %s)", r.high,
             ageText);
    return {"Nhanh", fullText, false};
  }
  snprintf(fullText, sizeof(fullText),
           "Rat nhanh (>= %d BPM = 85%% nhip toi da, %s)", danger, ageText);
  return {"Rat nhanh", fullText, false};
}

Status classifySpO2(int spo2) {
  if (spo2 >= 95) {
    return {"Binh thuong", "Binh thuong (95-100%)", true};
  }
  if (spo2 >= 91) {
    return {"Thap", "Thap nhe (91-94%) - theo doi them", false};
  }
  if (spo2 >= 86) {
    return {"Thieu oxy", "Thieu oxy (86-90%) - can kham bac si", false};
  }
  return {"Nguy hiem", "Thieu oxy nang (<= 85%) - can cap cuu", false};
}

int median(const int *values, int n) {
  int sorted[SAMPLES_PER_SESSION];
  memcpy(sorted, values, n * sizeof(int));
  for (int i = 1; i < n; i++) {
    int key = sorted[i];
    int j = i - 1;
    while (j >= 0 && sorted[j] > key) {
      sorted[j + 1] = sorted[j];
      j--;
    }
    sorted[j + 1] = key;
  }
  return sorted[n / 2];
}

int spread(const int *values, int n) {
  int lo = values[0], hi = values[0];
  for (int i = 1; i < n; i++) {
    lo = min(lo, values[i]);
    hi = max(hi, values[i]);
  }
  return hi - lo;
}

void pushSample(int hr, int spo2) {
  if (session.count == SAMPLES_PER_SESSION) {
    memmove(session.hr, session.hr + 1, (SAMPLES_PER_SESSION - 1) * sizeof(int));
    memmove(session.spo2, session.spo2 + 1, (SAMPLES_PER_SESSION - 1) * sizeof(int));
    session.count--;
  }
  session.hr[session.count] = hr;
  session.spo2[session.count] = spo2;
  session.count++;
}

void restartSensor() {
  MAX30102.sensorEndCollect();
  delay(100);
  MAX30102.sensorStartCollect();
  missedReadings = 0;
}

void startSession() {
  uint32_t id = prefs.getUInt("session", 0) + 1;
  prefs.putUInt("session", id);

  memset(&session, 0, sizeof(session));
  session.id = id;
  session.startMs = millis();
  session.warmupLeft = WARMUP_READINGS;
  state = session.warmupLeft > 0 ? STATE_WARMUP : STATE_MEASURING;
  hasLastResult = false;

  Serial.println("===========================");
  Serial.printf("[Luot #%lu] BAT DAU - giu yen ngon tay\n", session.id);
}

void endSession(State newState) {
  restartSensor();
  state = newState;
}

void finishSession() {
  session.resultHr = median(session.hr, session.count);
  session.resultSpo2 = median(session.spo2, session.count);
  session.durationSec = (millis() - session.startMs) / 1000;
  hasLastResult = true;
  state = STATE_DONE;

  Serial.printf("[Luot #%lu] KET QUA (%lus, loai %d mau nhieu):\n",
                session.id, session.durationSec, session.rejected);
  Serial.printf("  Nhip tim: %d BPM -> %s\n", session.resultHr,
                classifyHeartRate(session.resultHr).fullText);
  Serial.printf("  SpO2: %d %% -> %s\n", session.resultSpo2,
                classifySpO2(session.resultSpo2).fullText);
  Serial.println("  Nhac tay ra de bat dau luot moi.");
}

bool acceptSample(int hr, int spo2) {
  if (session.count >= 2) {
    int medHr = median(session.hr, session.count);
    int medSpo2 = median(session.spo2, session.count);
    if (abs(hr - medHr) > MAX_HR_JUMP || abs(spo2 - medSpo2) > MAX_SPO2_JUMP) {
      session.rejected++;
      session.consecutiveRejects++;
      if (session.consecutiveRejects >= MAX_CONSECUTIVE_REJECTS) {
        Serial.printf("[Luot #%lu] Tin hieu thay doi, lam moi cua so mau\n",
                      session.id);
        session.count = 0;
        session.consecutiveRejects = 0;
        pushSample(hr, spo2);
        return true;
      }
      return false;
    }
  }
  session.consecutiveRejects = 0;
  pushSample(hr, spo2);
  return true;
}

void processReading(int hr, int spo2) {
  bool hrValid = hr > 30 && hr < 220;
  bool spo2Valid = spo2 >= 70 && spo2 <= 100;
  bool fullReading = hrValid && spo2Valid;

  if (hrValid || spo2Valid) {
    missedReadings = 0;
  } else {
    missedReadings++;
  }
  bool fingerRemoved = missedReadings >= MAX_MISSED_READINGS;

  switch (state) {
    case STATE_IDLE:
      if (!hrValid && !spo2Valid) {
        break;
      }
      startSession();
      [[fallthrough]];

    case STATE_WARMUP:
    case STATE_MEASURING:
      if (fingerRemoved) {
        Serial.printf("[Luot #%lu] HUY - nhac tay truoc khi co ket qua, "
                      "bo du lieu luot nay\n", session.id);
        endSession(STATE_IDLE);
        break;
      }
      if (millis() - session.startMs > SESSION_TIMEOUT_MS) {
        Serial.printf("[Luot #%lu] LOI - tin hieu khong on dinh sau %ds\n",
                      session.id, SESSION_TIMEOUT_MS / 1000);
        endSession(STATE_FAILED);
        break;
      }
      if (hrValid) session.liveHr = hr;
      if (spo2Valid) session.liveSpo2 = spo2;
      if (!fullReading) {
        Serial.printf("[Luot #%lu] Mau thieu (HR=%d, SpO2=%d), bo qua\n",
                      session.id, hr, spo2);
        break;
      }
      if (state == STATE_WARMUP) {
        session.warmupLeft--;
        Serial.printf("[Luot #%lu] Khoi dong: HR=%d SpO2=%d (bo qua)\n",
                      session.id, hr, spo2);
        if (session.warmupLeft <= 0) {
          state = STATE_MEASURING;
        }
        break;
      }
      if (!acceptSample(hr, spo2)) {
        Serial.printf("[Luot #%lu] Loai mau nhieu: HR=%d SpO2=%d\n",
                      session.id, hr, spo2);
        break;
      }
      Serial.printf("[Luot #%lu] Mau %d/%d: HR=%d SpO2=%d\n", session.id,
                    session.count, SAMPLES_PER_SESSION, hr, spo2);
      if (session.count == SAMPLES_PER_SESSION &&
          spread(session.hr, session.count) <= MAX_HR_SPREAD &&
          spread(session.spo2, session.count) <= MAX_SPO2_SPREAD) {
        finishSession();
      }
      break;

    case STATE_DONE:
    case STATE_FAILED:
      if (fingerRemoved) {
        Serial.printf("[Luot #%lu] Da nhac tay, san sang luot moi\n",
                      session.id);
        endSession(STATE_IDLE);
      }
      break;
  }
}

void showMessage(const char *line1, const char *line2) {
  display.clearBuffer();
  display.setFont(u8g2_font_6x10_tf);
  display.drawStr((128 - display.getStrWidth(line1)) / 2, 28, line1);
  display.drawStr((128 - display.getStrWidth(line2)) / 2, 42, line2);
  display.sendBuffer();
}

void drawHeader(const char *text) {
  display.setFont(u8g2_font_5x8_tf);
  display.drawStr((128 - display.getStrWidth(text)) / 2, 8, text);
  display.drawHLine(0, 10, 128);
}

void drawColumn(int x, const char *label, int value, Status status) {
  char text[8];
  snprintf(text, sizeof(text), "%d", value);

  display.setFont(u8g2_font_5x8_tf);
  display.drawStr(x + (64 - display.getStrWidth(label)) / 2, 20, label);

  display.setFont(u8g2_font_logisoso20_tn);
  display.drawStr(x + (64 - display.getStrWidth(text)) / 2, 43, text);

  display.setFont(u8g2_font_5x8_tf);
  int textX = x + (64 - display.getStrWidth(status.shortText)) / 2;
  if (!status.normal) {
    display.drawRBox(x + 2, 48, 60, 14, 2);
    display.setDrawColor(0);
    display.drawStr(textX, 58, status.shortText);
    display.setDrawColor(1);
  } else {
    display.drawStr(textX, 58, status.shortText);
  }
}

void showResult(const char *header) {
  display.clearBuffer();
  drawHeader(header);
  char hrLabel[16];
  HeartRange r = heartRangeForAge(driverAge);
  snprintf(hrLabel, sizeof(hrLabel), "HR %d-%d", r.low, r.high);
  drawColumn(0, hrLabel, session.resultHr, classifyHeartRate(session.resultHr));
  drawColumn(64, "SpO2 %", session.resultSpo2,
             classifySpO2(session.resultSpo2));
  display.drawVLine(64, 11, 53);
  display.drawHLine(0, 45, 128);
  display.sendBuffer();
}

void showProgress() {
  char header[24];
  char line[24];
  snprintf(header, sizeof(header), "Luot #%lu - Dang do", session.id);

  int done = (WARMUP_READINGS - session.warmupLeft) + session.count;
  int total = WARMUP_READINGS + SAMPLES_PER_SESSION;

  if (state == STATE_WARMUP) {
    snprintf(line, sizeof(line), "Khoi dong...");
  } else if (session.count < SAMPLES_PER_SESSION) {
    snprintf(line, sizeof(line), "Mau %d/%d", session.count,
             SAMPLES_PER_SESSION);
  } else {
    snprintf(line, sizeof(line), "Cho on dinh...");
  }

  char live[24];
  snprintf(live, sizeof(live), "HR %s  SpO2 %s",
           session.liveHr ? String(session.liveHr).c_str() : "--",
           session.liveSpo2 ? String(session.liveSpo2).c_str() : "--");

  display.clearBuffer();
  drawHeader(header);
  display.setFont(u8g2_font_6x10_tf);
  display.drawStr((128 - display.getStrWidth(live)) / 2, 26, live);
  display.drawStr((128 - display.getStrWidth(line)) / 2, 41, line);
  display.drawFrame(4, 50, 120, 10);
  display.drawBox(6, 52, 116 * min(done, total) / total, 6);
  display.sendBuffer();
}

void updateDisplay() {
  char header[28];
  switch (state) {
    case STATE_IDLE:
      if (hasLastResult) {
        snprintf(header, sizeof(header), "#%lu | Dat tay de do moi",
                 session.id);
        showResult(header);
      } else {
        showMessage("Dat ngon tay", "len cam bien");
      }
      break;
    case STATE_WARMUP:
    case STATE_MEASURING:
      showProgress();
      break;
    case STATE_DONE:
      snprintf(header, sizeof(header), "Luot #%lu - Ket qua", session.id);
      showResult(header);
      break;
    case STATE_FAILED:
      showMessage("Tin hieu nhieu!", "Nhac tay, do lai");
      break;
  }
}

bool findWarningFile(int level, char *path, size_t len) {
  snprintf(path, len, "/AUDIO/warning%d.mp3", level);
  if (SD.exists(path)) return true;
  snprintf(path, len, "/warning%d.mp3", level);
  return SD.exists(path);
}

void audioTask(void *) {
  AudioOutputI2S *out = new AudioOutputI2S();
  out->SetPinout(I2S_BCLK, I2S_LRC, I2S_DIN);
  out->SetGain(AUDIO_GAIN);
  AudioGeneratorMP3 *mp3 = nullptr;
  AudioFileSourceSD *file = nullptr;
  AudioFileSourceBuffer *buffer = nullptr;

  auto stop = [&]() {
    if (mp3) mp3->stop();
    delete mp3;    mp3 = nullptr;
    delete buffer; buffer = nullptr;
    delete file;   file = nullptr;
    if (audioPlaying) lastAudioEndMs = millis();
    audioPlaying = false;
  };

  while (true) {
    int request = audioRequest;
    if (request != 0) {
      stop();
      audioPlaying = request > 0;
      audioRequest = 0;
      char path[32];
      if (request > 0 && findWarningFile(request, path, sizeof(path))) {
        file = new AudioFileSourceSD(path);
        buffer = new AudioFileSourceBuffer(file, 4096);
        mp3 = new AudioGeneratorMP3();
        audioPlaying = mp3->begin(buffer, out);
        Serial.printf("Phat %s%s\n", path, audioPlaying ? "" : " - LOI giai ma");
        if (!audioPlaying) stop();
      } else if (request > 0) {
        Serial.printf("Khong tim thay warning%d.mp3 tren the nho\n", request);
      }
    }
    if (mp3 && mp3->isRunning()) {
      if (!mp3->loop()) stop();
      vTaskDelay(1);
    } else {
      vTaskDelay(pdMS_TO_TICKS(10));
    }
  }
}

void playWarning(int level) {
  if (sdReady) audioRequest = level;
}

void updateAudio() {
  if (alertLevel > 0 && millis() - lastAlertMsgMs > ALERT_TIMEOUT_MS) {
    Serial.println("Mat ket noi PC, tat canh bao");
    alertLevel = 0;
    playWarning(-1);
    updateDisplay();
    return;
  }
  uint32_t gap = REPEAT_GAP_MS[alertLevel];
  if (gap > 0 && !audioPlaying && audioRequest == 0 &&
      millis() - lastAudioEndMs >= gap) {
    playWarning(alertLevel);
  }
}

void showAlert() {
  static const char *titles[] = {"", "Nhac nho", "CANH BAO", "NGUY HIEM!"};
  static const char *hints[] = {"", "Dau hieu buon ngu", "Met moi, mat tap trung",
                                "Dung xe nghi ngay"};
  char line[24];
  display.clearBuffer();
  if (alertLevel >= 2) {
    display.drawBox(0, 0, 128, 64);
    display.setDrawColor(0);
  }
  display.setFont(u8g2_font_logisoso16_tr);
  display.drawStr((128 - display.getStrWidth(titles[alertLevel])) / 2, 26,
                  titles[alertLevel]);
  display.setFont(u8g2_font_6x10_tf);
  display.drawStr((128 - display.getStrWidth(hints[alertLevel])) / 2, 44,
                  hints[alertLevel]);
  snprintf(line, sizeof(line), "HR %s  Tuoi %s",
           session.liveHr ? String(session.liveHr).c_str() : "--",
           driverAge ? String(driverAge).c_str() : "--");
  display.drawStr((128 - display.getStrWidth(line)) / 2, 60, line);
  display.setDrawColor(1);
  display.sendBuffer();
}

void handleCommand(const char *cmd) {
  int value;
  if (sscanf(cmd, "@ALERT,%d", &value) == 1) {
    lastAlertMsgMs = millis();
    value = constrain(value, 0, MAX_ALERT_LEVEL);
    if (value != alertLevel) {
      int previous = alertLevel;
      alertLevel = value;
      if (alertLevel == 0) {
        playWarning(-1);
        updateDisplay();
      } else {
        if (alertLevel > previous) playWarning(alertLevel);
        showAlert();
      }
    }
  } else if (sscanf(cmd, "@AGE,%d", &value) == 1) {
    if (value != driverAge) {
      driverAge = value;
      Serial.printf("Tuoi nguoi lai: %d -> nhip tim binh thuong %d-%d BPM\n",
                    driverAge, heartRangeForAge(driverAge).low,
                    heartRangeForAge(driverAge).high);
      if (alertLevel == 0 && sensorReady) updateDisplay();
    }
  }
}

void readCommands() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (rxLen > 0) {
        rxLine[rxLen] = '\0';
        handleCommand(rxLine);
        rxLen = 0;
      }
    } else if (rxLen < (int)sizeof(rxLine) - 1) {
      rxLine[rxLen++] = c;
    }
  }
}

void reportSensor() {
  static uint32_t lastReportMs = 0;
  if (millis() - lastReportMs < SENSOR_REPORT_MS) return;
  lastReportMs = millis();
  bool measuring = state == STATE_WARMUP || state == STATE_MEASURING;
  bool hasResult = state == STATE_DONE || hasLastResult;
  Serial.printf("@SENSOR,%d,%d,%d,%d,%d,%d\n", sensorReady ? (int)state : -1,
                measuring ? session.liveHr : 0, measuring ? session.liveSpo2 : 0,
                hasResult ? session.resultHr : 0, hasResult ? session.resultSpo2 : 0,
                session.count);
}

#define SENSOR_RETRY_MS 2000
void connectSensor() {
  lastSensorRetryMs = millis();
  sensorReady = MAX30102.begin();
  if (!sensorReady) {
    Serial.println("Khong tim thay cam bien (dia chi 0x57)!");
    Serial.printf("Kiem tra SDA=%d, SCL=%d, VCC va GND.\n", SDA_PIN, SCL_PIN);
    if (alertLevel == 0) showMessage("Loi cam bien!", "Kiem tra day noi");
    return;
  }
  Serial.println("Da ket noi cam bien.");
  restartSensor();
  Serial.println("Dat ngon tay len cam bien.");
  if (alertLevel == 0) updateDisplay();
}

void setup() {
  Serial.begin(115200);
  delay(500);
  Wire.begin(SDA_PIN, SCL_PIN);

  Wire1.begin(OLED_SDA_PIN, OLED_SCL_PIN);
  display.setBusClock(400000);
  display.begin();
  showMessage("Khoi dong...", "MAX30102");

  prefs.begin("oximeter", false);

  SPI.begin(SD_SCK, SD_MISO, SD_MOSI, SD_CS);
  sdReady = SD.begin(SD_CS, SPI, 4000000);
  Serial.println(sdReady ? "The nho OK" : "Khong doc duoc the nho - canh bao khong co tieng");
  xTaskCreatePinnedToCore(audioTask, "audio", 8192, nullptr, 2, nullptr, 0);

  Serial.println("Khoi dong cam bien MAX30102 (DFRobot SEN0344)...");

  connectSensor();
  lastReadMs = millis();
}

void loop() {
  readCommands();
  updateAudio();
  reportSensor();

  if (!sensorReady) {
    if (millis() - lastSensorRetryMs >= SENSOR_RETRY_MS) connectSensor();
    delay(10);
    return;
  }

  if (millis() - lastReadMs < POLL_INTERVAL_MS) {
    delay(10);
    return;
  }
  lastReadMs = millis();

  MAX30102.getHeartbeatSPO2();
  int hr = MAX30102._sHeartbeatSPO2.Heartbeat;
  int spo2 = MAX30102._sHeartbeatSPO2.SPO2;

  bool isNewData = hr != lastRawHr || spo2 != lastRawSpo2 ||
                   millis() - lastUpdateMs >= MODULE_UPDATE_MS;
  if (!isNewData) {
    return;
  }
  lastRawHr = hr;
  lastRawSpo2 = spo2;
  lastUpdateMs = millis();

  if (hr > 30 && hr < 220) {
    Serial.printf("@HR,%d,%d\n", hr, spo2);
  }

  processReading(hr, spo2);
  if (alertLevel > 0) {
    showAlert();
  } else {
    updateDisplay();
  }
}
