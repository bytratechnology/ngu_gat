#include <Arduino.h>
#include <Wire.h>
#include "DFRobot_BloodOxygen_S.h"
#include "sensor_link.h"

#define SDA_PIN 6
#define SCL_PIN 7
#define I2C_ADDRESS 0x57

#define SEND_INTERVAL_MS 500
#define SENSOR_RETRY_MS 2000

DFRobot_BloodOxygen_S_I2C MAX30102(&Wire, I2C_ADDRESS);

bool sensorOk = false;
uint32_t lastSensorRetryMs = 0;
uint32_t lastSendMs = 0;
uint32_t seq = 0;
volatile bool restartRequested = false;

void onLinkRecv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
  if (linkValid(data, len, LINK_RESTART)) restartRequested = true;
}

void restartSensor() {
  MAX30102.sensorEndCollect();
  delay(100);
  MAX30102.sensorStartCollect();
}

void connectSensor() {
  lastSensorRetryMs = millis();
  Wire.beginTransmission(I2C_ADDRESS);
  sensorOk = Wire.endTransmission() == 0;
  if (!sensorOk) {
    Serial.printf("Khong tim thay MAX30102 (SDA=%d, SCL=%d)\n", SDA_PIN, SCL_PIN);
    return;
  }
  Serial.println("Da ket noi MAX30102");
  restartSensor();
}

void setup() {
  Serial.begin(115200);
  delay(500);
  Wire.begin(SDA_PIN, SCL_PIN);

  if (!linkBegin(onLinkRecv)) {
    Serial.println("Khoi dong ESP-NOW that bai");
  }
  WiFi.setTxPower(WIFI_POWER_8_5dBm);
  Serial.printf("Kenh ESP-NOW: %d\n", linkChannel());
  Serial.print("ESP32-C3 MAC: ");
  Serial.println(linkMac());

  connectSensor();
}

void loop() {
  if (!sensorOk && millis() - lastSensorRetryMs >= SENSOR_RETRY_MS) {
    connectSensor();
  }

  if (restartRequested) {
    restartRequested = false;
    if (sensorOk) restartSensor();
    Serial.println("Master yeu cau khoi dong lai cam bien");
  }

  if (millis() - lastSendMs < SEND_INTERVAL_MS) {
    delay(5);
    return;
  }
  lastSendMs = millis();

  LinkPacket packet = {};
  packet.magic = LINK_MAGIC;
  packet.type = LINK_READING;
  packet.sensorOk = sensorOk;
  packet.hr = -1;
  packet.spo2 = -1;
  packet.seq = seq++;

  if (sensorOk) {
    MAX30102.getHeartbeatSPO2();
    packet.hr = MAX30102._sHeartbeatSPO2.Heartbeat;
    packet.spo2 = MAX30102._sHeartbeatSPO2.SPO2;
  }

  bool sent = linkSend(packet);
  Serial.printf("#%lu HR=%d SpO2=%d %s\n", packet.seq, packet.hr, packet.spo2,
                sent ? "" : "(gui loi)");
}
