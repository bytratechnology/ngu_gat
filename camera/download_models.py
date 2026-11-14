import io
import json
import os
import ssl
import tempfile
import urllib.request
import zipfile

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")

AGE_MODEL_DIR = os.path.join(OUT, "AgeModel")
AGE_MODEL_REPO = "iitolstykh/mivolo_v2"
AGE_MODEL_REVISION = "e8bdd8aec21ae468cbfe73b41f52550105ad8fbf"
AGE_MODEL_NOTICE = """AgeModel - trong so mo hinh uoc luong tuoi

Tac gia goc: Maksim Kuprashevich, Irina Tolstykh (2023)
Giay phep: CC BY-NC 4.0 - https://creativecommons.org/licenses/by-nc/4.0/
  Chi duoc dung cho muc dich PHI THUONG MAI, phai ghi nguon tac gia goc.

Thay doi: doi ten cac tham so trong file model.safetensors va rut gon config.json;
gia tri trong so giu nguyen.
"""

INSIGHTFACE_URL = "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_s.zip"
INSIGHTFACE_NAME = "genderage.onnx"


def download_age_model():
    if os.path.exists(os.path.join(AGE_MODEL_DIR, "model.safetensors")):
        return
    from huggingface_hub import snapshot_download
    from safetensors.torch import load_file, save_file
    print("Tai AgeModel ...")
    with tempfile.TemporaryDirectory() as tmp:
        snapshot_download(AGE_MODEL_REPO, revision=AGE_MODEL_REVISION, local_dir=tmp,
                          allow_patterns=["config.json", "model.safetensors"])
        weights = load_file(os.path.join(tmp, "model.safetensors"))
        src_cfg = json.load(open(os.path.join(tmp, "config.json")))
    prefix = next(iter(weights)).split(".")[0] + ".model."
    weights = {"backbone." + k[len(prefix):]: v.contiguous() for k, v in weights.items()}
    cfg = {k: src_cfg[k] for k in ("num_classes", "num_classes_gender", "input_size",
                                   "min_age", "max_age", "avg_age")}
    os.makedirs(AGE_MODEL_DIR, exist_ok=True)
    save_file(weights, os.path.join(AGE_MODEL_DIR, "model.safetensors"), metadata={"format": "pt"})
    with open(os.path.join(AGE_MODEL_DIR, "config.json"), "w") as f:
        json.dump(cfg, f, indent=2)
    with open(os.path.join(AGE_MODEL_DIR, "NOTICE.txt"), "w") as f:
        f.write(AGE_MODEL_NOTICE)


def download_insightface():
    path = os.path.join(OUT, INSIGHTFACE_NAME)
    if os.path.exists(path):
        return
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = None
    print("Tai buffalo_s.zip ...")
    with urllib.request.urlopen(INSIGHTFACE_URL, context=ctx) as resp:
        data = resp.read()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        member = next(n for n in z.namelist() if n.endswith(INSIGHTFACE_NAME))
        with open(path, "wb") as f:
            f.write(z.read(member))


os.makedirs(OUT, exist_ok=True)
download_age_model()
download_insightface()
print("Xong:", OUT)
