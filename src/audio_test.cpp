// Test loa MAX98357A (I2S) + the nho Micro SD: phat lan luot cac file MP3.
// Lenh qua Serial Monitor: n = bai tiep, b = bai truoc, 1..9 = chon bai,
//                          p = tam dung/tiep tuc, + / - = am luong, l = liet ke
#include <Arduino.h>
#include <SPI.h>
#include <SD.h>
#include "AudioFileSourceSD.h"
#include "AudioFileSourceBuffer.h"
#include "AudioGeneratorMP3.h"
#include "AudioOutputI2S.h"

// MAX98357A
#define I2S_BCLK 26
#define I2S_LRC 25
#define I2S_DIN 22

// Micro SD (SPI)
#define SD_SCK 18
#define SD_MISO 19
#define SD_MOSI 23
#define SD_CS 5

#define MUSIC_DIR "/AUDIO"   // Neu khong co thu muc nay thi tim o goc the
#define MAX_TRACKS 20

AudioGeneratorMP3 *mp3 = nullptr;
AudioFileSourceSD *file = nullptr;
AudioFileSourceBuffer *buffer = nullptr;
AudioOutputI2S *out = nullptr;

String tracks[MAX_TRACKS];
int trackCount = 0;
int current = 0;
float volume = 0.3;          // 0.0 - 4.0; MAX98357A kha to, bat dau nho
bool paused = false;

bool isMp3(const String &name) {
  String lower = name;
  lower.toLowerCase();
  // Bo file an "._xxx.mp3" do macOS tao ra khi chep vao the
  return lower.endsWith(".mp3") && !lower.startsWith("._");
}

void scanDir(const char *dir) {
  File root = SD.open(dir);
  if (!root || !root.isDirectory()) {
    return;
  }
  for (File f = root.openNextFile(); f && trackCount < MAX_TRACKS;
       f = root.openNextFile()) {
    String name = f.name();
    if (!f.isDirectory() && isMp3(name)) {
      tracks[trackCount++] = String(strcmp(dir, "/") == 0 ? "" : dir) + "/" + name;
    }
  }
}

void listTracks() {
  Serial.printf("Co %d bai:\n", trackCount);
  for (int i = 0; i < trackCount; i++) {
    Serial.printf("  %d. %s%s\n", i + 1, tracks[i].c_str(),
                  i == current ? "  <- dang phat" : "");
  }
}

void stopTrack() {
  if (mp3 && mp3->isRunning()) mp3->stop();
  delete mp3;    mp3 = nullptr;
  delete buffer; buffer = nullptr;
  delete file;   file = nullptr;
}

void playTrack(int index) {
  stopTrack();
  current = (index + trackCount) % trackCount;
  paused = false;
  Serial.printf("> Phat %d/%d: %s\n", current + 1, trackCount,
                tracks[current].c_str());
  file = new AudioFileSourceSD(tracks[current].c_str());
  buffer = new AudioFileSourceBuffer(file, 4096);  // Dem tranh vap tieng khi doc SD
  mp3 = new AudioGeneratorMP3();
  if (!mp3->begin(buffer, out)) {
    Serial.println("  Loi giai ma file, chuyen bai");
    playTrack(current + 1);
  }
}

void handleSerial() {
  if (!Serial.available()) return;
  char c = Serial.read();
  switch (c) {
    case 'n': playTrack(current + 1); break;
    case 'b': playTrack(current - 1); break;
    case 'p':
      paused = !paused;
      Serial.println(paused ? "Tam dung" : "Tiep tuc");
      break;
    case '+':
    case '-':
      volume = constrain(volume + (c == '+' ? 0.1f : -0.1f), 0.0f, 4.0f);
      out->SetGain(volume);
      Serial.printf("Am luong: %.1f\n", volume);
      break;
    case 'l': listTracks(); break;
    default:
      if (c >= '1' && c <= '9' && c - '1' < trackCount) playTrack(c - '1');
  }
}

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n=== TEST LOA MAX98357A + THE NHO ===");

  SPI.begin(SD_SCK, SD_MISO, SD_MOSI, SD_CS);
  while (!SD.begin(SD_CS, SPI, 4000000)) {
    Serial.println("Khong doc duoc the nho! Kiem tra day, the FAT32. Thu lai...");
    delay(2000);
  }
  const char *types[] = {"NONE", "MMC", "SD", "SDHC", "UNKNOWN"};
  Serial.printf("The nho OK: %s, %llu MB\n", types[min((int)SD.cardType(), 4)],
                SD.cardSize() / (1024 * 1024));

  scanDir(MUSIC_DIR);
  if (trackCount == 0) scanDir("/");
  if (trackCount == 0) {
    Serial.println("Khong tim thay file .mp3 trong " MUSIC_DIR " hoac thu muc goc!");
    while (true) delay(1000);
  }
  listTracks();

  out = new AudioOutputI2S();
  out->SetPinout(I2S_BCLK, I2S_LRC, I2S_DIN);
  out->SetGain(volume);

  Serial.println("Lenh: n/b = bai tiep/truoc, 1-9 = chon bai, p = dung, +/- = am luong, l = danh sach");
  playTrack(0);
}

void loop() {
  handleSerial();
  if (paused || !mp3) return;
  if (mp3->isRunning()) {
    if (!mp3->loop()) {
      Serial.println("  Het bai");
      playTrack(current + 1);
    }
  }
}
