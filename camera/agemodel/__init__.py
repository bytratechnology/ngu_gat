import json
import os

import cv2
import numpy as np
import torch
from safetensors.torch import load_file

from .backbone import build_d1_384

MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def letterbox(im, size):
    h, w = im.shape[:2]
    if h == size and w == size:
        return im
    r = min(size / h, size / w)
    new_w, new_h = int(round(w * r)), int(round(h * r))
    if (w, h) != (new_w, new_h):
        im = cv2.resize(im, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    dw, dh = (size - new_w) / 2, (size - new_h) / 2
    top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
    left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
    return cv2.copyMakeBorder(im, top, bottom, left, right, cv2.BORDER_CONSTANT, value=(0, 0, 0))


def preprocess(img_bgr, size):
    if img_bgr is None:
        x = np.broadcast_to(-MEAN / STD, (size, size, 3))
    else:
        x = cv2.cvtColor(letterbox(img_bgr, size), cv2.COLOR_BGR2RGB) / 255.0
        x = (x - MEAN) / STD
    x = np.ascontiguousarray(x.astype(np.float32).transpose(2, 0, 1))
    return torch.from_numpy(x).unsqueeze(0)


class AgeModel(torch.nn.Module):
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.backbone = build_d1_384(in_chans=6, num_classes=config["num_classes"])

    @torch.no_grad()
    def predict_age(self, face_bgr, body_bgr):
        size = self.config["input_size"]
        x = torch.cat((preprocess(face_bgr, size), preprocess(body_bgr, size)), dim=1)
        out = self.backbone(x)
        raw_age = out[0, self.config["num_classes_gender"]:]
        cfg = self.config
        return float(raw_age[0] * (cfg["max_age"] - cfg["min_age"]) + cfg["avg_age"])


def load_age_model(model_dir):
    with open(os.path.join(model_dir, "config.json")) as f:
        config = json.load(f)
    model = AgeModel(config)
    model.load_state_dict(load_file(os.path.join(model_dir, "model.safetensors")), strict=True)
    return model.eval()
