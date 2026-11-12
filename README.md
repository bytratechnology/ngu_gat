# Hệ thống cảnh báo ngủ gật

Phát hiện tài xế buồn ngủ bằng webcam (mắt, ngáp, cúi đầu) kết hợp cảm biến nhịp tim/SpO2, rồi phát cảnh báo bằng giọng nói qua loa gắn trên ESP32.

- **Máy tính (Python, thư mục `camera/`)**: MediaPipe Face Mesh theo dõi độ mở mắt (EAR), PERCLOS, ngáp (MAR), cúi đầu; quyết định 3 mức cảnh báo; hiển thị bằng cửa sổ hoặc trang web.
- **ESP32 (PlatformIO, thư mục `src/`)**: đọc MAX30102, hiển thị OLED, phát `warning1/2/3.mp3` từ thẻ nhớ qua MAX98357A.
- Hai bên nói chuyện qua cáp USB (Serial, 115200 baud).

## Phần cứng và sơ đồ chân

| Module | Giao tiếp | Chân ESP32 | Nguồn |
|---|---|---|---|
| MAX30102 (DFRobot SEN0344) | I2C bus 1 | SDA 21 · SCL 32 | 3.3V |
| OLED 1.3" SH1106 | I2C bus 2 | SDA 16 · SCL 17 | 3.3V |
| MAX98357A + loa | I2S | BCLK 26 · LRC 25 · DIN 22 | 5V |
| Micro SD | SPI | SCK 18 · MISO 19 · MOSI 23 · CS 5 | 3.3V |

Mọi GND nối chung. Thẻ nhớ định dạng FAT32, chứa `warning1.mp3`, `warning2.mp3`, `warning3.mp3` trong thư mục `/AUDIO` (hoặc ở gốc thẻ).

## Nạp firmware ESP32

```bash
pio run -e esp32dev -t upload                   # chương trình chính
pio run -e audio_test -t upload -t monitor      # test riêng loa và thẻ nhớ
```

## Chạy chương trình camera

Thử trên macOS (Intel), Python 3.10:

```bash
conda create -n drowsy-env python=3.10 && conda activate drowsy-env
cd camera
pip install -r requirements.txt
pip install --no-deps --no-build-isolation "git+https://github.com/WildChlamydia/MiVOLO.git"
python download_models.py

python drowsy.py --port /dev/cu.usbserial-XXXX          # cửa sổ OpenCV, nhấn q để thoát
python drowsy.py --port /dev/cu.usbserial-XXXX --web    # trang web http://localhost:8000
```

Bỏ `--port` để chạy riêng camera. Khi chương trình camera đang chạy, phải tắt nó trước khi nạp lại ESP32 (cả hai cùng dùng cổng Serial).

Mô hình MiVOLO (CC BY-NC 4.0) và InsightFace chỉ được dùng cho mục đích phi thương mại; file mô hình không nằm trong repo.

## Mức cảnh báo

| Mức | Điều kiện (chỉ cần một) | Âm thanh |
|---|---|---|
| 1 · Nhắc nhở | Vừa ngáp · PERCLOS ≥ 15% · nhịp tim thấp · cúi đầu · không thấy mặt > 3 s | `warning1.mp3`, 1 lần |
| 2 · Cảnh báo | Nhắm mắt ≥ 1,5 s · ngáp ≥ 2 lần/phút · PERCLOS ≥ 30% | `warning2.mp3`, lặp sau 8 s |
| 3 · Nguy hiểm | Nhắm mắt ≥ 3 s · cúi đầu khi nhắm mắt · nhắm lâu + nhịp tim thấp · không thấy mặt > 6 s | `warning3.mp3`, lặp sau 1,5 s |

PERCLOS là tỉ lệ thời gian nhắm mắt trong 30 giây gần nhất (không tính chớp mắt dưới 0,5 s). Nhịp tim thấp: giảm ≥ 12% so với nhịp nền (trung vị 20 lần đo đầu) hoặc dưới ngưỡng bình thường.

## Giao thức Serial

| Hướng | Dòng lệnh | Ý nghĩa |
|---|---|---|
| ESP32 → PC | `@HR,<bpm>,<spo2>` | Mỗi lần cảm biến có số đo mới |
| ESP32 → PC | `@SENSOR,<trạng thái>,<HR tạm>,<SpO2 tạm>,<HR kết quả>,<SpO2 kết quả>,<số mẫu>` | Mỗi giây |
| PC → ESP32 | `@ALERT,<0..3>` | Mức cảnh báo, gửi lại mỗi giây (mất 3 s thì ESP32 tự tắt cảnh báo) |
| PC → ESP32 | `@AGE,<tuổi>` | Tuổi ước lượng, dùng để chọn ngưỡng nhịp tim |
