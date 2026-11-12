"""Tai mo hinh uoc luong tuoi InsightFace (genderage.onnx) vao thu muc models/.

genderage.onnx nam trong goi buffalo_s cua InsightFace (~120 MB, chi giu lai file 1.3 MB).
"""
import io
import os
import ssl
import urllib.request
import zipfile

URL = "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_s.zip"
NAME = "genderage.onnx"

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
os.makedirs(out, exist_ok=True)
path = os.path.join(out, NAME)
if not os.path.exists(path):
    try:
        import certifi  # Python tren macOS hay thieu chung chi goc
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = None
    print("Tai buffalo_s.zip ...")
    with urllib.request.urlopen(URL, context=ctx) as resp:
        data = resp.read()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        member = next(n for n in z.namelist() if n.endswith(NAME))
        with open(path, "wb") as f:
            f.write(z.read(member))
print("Xong:", path)
