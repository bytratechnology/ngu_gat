# Hệ thống cảnh báo ngủ gật

Phát hiện tài xế buồn ngủ bằng webcam (mắt, ngáp, cúi đầu) kết hợp cảm biến nhịp tim/SpO2, rồi phát cảnh báo bằng giọng nói qua loa gắn trên ESP32.

- **Máy tính (Python, thư mục `camera/`)**: MediaPipe Face Mesh theo dõi độ mở mắt (EAR), PERCLOS, ngáp (MAR), cúi đầu; quyết định 3 mức cảnh báo; hiển thị bằng cửa sổ OpenCV.
- **ESP32-C3 Super Mini (`src/sensor_c3.cpp`)**: đọc MAX30102, gửi nhịp tim/SpO2 không dây sang ESP32 master bằng ESP-NOW mỗi 0,5 giây.
- **ESP32 master (`src/main.cpp`)**: nhận dữ liệu từ C3, xử lý lượt đo, hiển thị OLED, phát `warning1/2/3.mp3` từ thẻ nhớ qua MAX98357A.
- Máy tính và ESP32 master nói chuyện qua cáp USB (Serial, 115200 baud).

## Phần cứng và sơ đồ chân

| Module | Giao tiếp | Chân | Nguồn |
|---|---|---|---|
| MAX30102 (DFRobot SEN0344) | I2C, nối vào **ESP32-C3** | SDA 6 · SCL 7 | 3.3V |
| OLED 1.3" SH1106 | I2C bus 2 | SDA 16 · SCL 17 | 3.3V |
| MAX98357A + loa | I2S | BCLK 26 · LRC 25 · DIN 22 | 5V |
| Micro SD | SPI | SCK 18 · MISO 19 · MOSI 23 · CS 5 | 3.3V |

OLED, loa và thẻ nhớ nối vào ESP32 master, mọi GND nối chung. ESP32-C3 cấp nguồn riêng (USB hoặc pin), không cần dây nối với master. Thẻ nhớ định dạng FAT32, chứa `warning1.mp3`, `warning2.mp3`, `warning3.mp3` trong thư mục `/AUDIO` (hoặc ở gốc thẻ).

## Nạp firmware

```bash
pio run -e esp32dev -t upload                   # ESP32 master
pio run -e sensor_c3 -t upload -t monitor       # ESP32-C3 đọc MAX30102
pio run -e audio_test -t upload -t monitor      # test riêng loa và thẻ nhớ
```

## Chạy chương trình camera

Cần Python 3.10 (3.9–3.12 đều được). Chạy bằng 1 lệnh:

```bash
cd camera
./start.sh        # Windows: start.bat
```

Lần đầu `start.sh` / `start.bat` tự tạo môi trường `.venv`, cài `requirements.txt` và tải mô hình (`download_models.py`); sau đó mở cửa sổ camera (nhấn q để thoát) và tự dò cổng ESP32. Chạy thủ công (trong `.venv`):

```bash
python drowsy.py --port auto          # cửa sổ OpenCV, nhấn q để thoát
```

Bỏ `--port` để chạy riêng camera. Khi chương trình camera đang chạy, phải tắt nó trước khi nạp lại ESP32 (cả hai cùng dùng cổng Serial).

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
