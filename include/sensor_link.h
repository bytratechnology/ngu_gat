#pragma once

#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_mac.h>
#include <esp_wifi.h>

#define LINK_MAGIC 0x4E474154
#define LINK_CHANNEL 1

enum : uint8_t {
  LINK_READING = 1,
  LINK_RESTART = 2,
};

struct __attribute__((packed)) LinkPacket {
  uint32_t magic;
  uint8_t type;
  uint8_t sensorOk;
  int16_t hr;
  int16_t spo2;
  uint32_t seq;
};

inline bool linkBegin(esp_now_recv_cb_t onRecv) {
  WiFi.mode(WIFI_STA);
  esp_wifi_set_channel(LINK_CHANNEL, WIFI_SECOND_CHAN_NONE);
  if (esp_now_init() != ESP_OK) return false;
  esp_now_register_recv_cb(onRecv);
  esp_now_peer_info_t peer = {};
  memset(peer.peer_addr, 0xFF, 6);
  peer.channel = LINK_CHANNEL;
  peer.encrypt = false;
  return esp_now_add_peer(&peer) == ESP_OK;
}

inline String linkMac() {
  uint8_t mac[6];
  esp_read_mac(mac, ESP_MAC_WIFI_STA);
  char text[18];
  snprintf(text, sizeof(text), "%02X:%02X:%02X:%02X:%02X:%02X", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
  return String(text);
}

inline uint8_t linkChannel() {
  uint8_t primary = 0;
  wifi_second_chan_t second;
  esp_wifi_get_channel(&primary, &second);
  return primary;
}

inline bool linkSend(const LinkPacket &packet) {
  static const uint8_t broadcast[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
  return esp_now_send(broadcast, (const uint8_t *)&packet, sizeof(packet)) == ESP_OK;
}

inline bool linkValid(const uint8_t *data, int len, uint8_t type) {
  if (len != sizeof(LinkPacket)) return false;
  const LinkPacket *p = (const LinkPacket *)data;
  return p->magic == LINK_MAGIC && p->type == type;
}
