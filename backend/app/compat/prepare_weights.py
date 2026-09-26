"""One-time weight preparation for the OutfitTransformer compatibility scorer.

Downloads the community OutfitTransformer (CLIP variant) checkpoints released by
owj0421/outfit-transformer (MIT license) from Google Drive, extracts the
compatibility-prediction checkpoint, and splits it into three safetensors files
so the runtime can load them with low memory overhead and fully offline:

  models/outfit_transformer/clip_vision.safetensors   (frozen fashion-clip image tower)
  models/outfit_transformer/clip_text.safetensors     (frozen fashion-clip text tower)
  models/outfit_transformer/head.safetensors          (OutfitTransformer encoder + CP head)
  models/outfit_transformer/fashion-clip/             (HF config/tokenizer/processor files)
  models/outfit_transformer/head_config.json

Usage:  python -m app.compat.prepare_weights        (run from backend/)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

GDRIVE_ID = "1mzNqGBmd8UjVJjKwVa5GdGYHKutZKSSi"  # from owj0421/outfit-transformer README
CKPT_NAME = "compatibillity_clip_best.pth"  # (sic) name inside the zip
CLIP_REPO = "patrickjohncyh/fashion-clip"

BACKEND_DIR = Path(__file__).resolve().parents[2]
MODEL_DIR = Path(os.environ.get("FITCHECK_COMPAT_MODEL_DIR", BACKEND_DIR / "models" / "outfit_transformer"))


def _download_ckpt() -> Path:
    ckpt = MODEL_DIR / CKPT_NAME
    if ckpt.exists():
        return ckpt
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    zpath = MODEL_DIR / "checkpoints.zip"
    if not zpath.exists():
        try:
            import gdown  # noqa: F401
        except ImportError:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "gdown"])
        import gdown
        gdown.download(id=GDRIVE_ID, output=str(zpath), quiet=False)
    with zipfile.ZipFile(zpath) as zf:
        zf.extract(CKPT_NAME, MODEL_DIR)
    zpath.unlink()
    return ckpt


def main(keep_pth: bool = False) -> None:
    import torch
    from huggingface_hub import hf_hub_download
    from safetensors.torch import save_file

    if (MODEL_DIR / "head.safetensors").exists():
        print("already prepared:", MODEL_DIR)
        return
    ckpt_path = _download_ckpt()
    print("loading", ckpt_path)
    sd = torch.load(ckpt_path, map_location="cpu", weights_only=False)
    cfg, model_sd = sd["config"], sd["model"]
    model_sd = {k.replace("module.", ""): v for k, v in model_sd.items()}

    vis = {k[len("item_enc.image_enc.model."):]: v.contiguous() for k, v in model_sd.items()
           if k.startswith("item_enc.image_enc.model.")}
    txt = {k[len("item_enc.text_enc.model."):]: v.contiguous() for k, v in model_sd.items()
           if k.startswith("item_enc.text_enc.model.")}
    head = {k: v.contiguous() for k, v in model_sd.items() if not k.startswith("item_enc.")}
    assert vis and txt and head, "unexpected checkpoint layout"
    save_file(vis, str(MODEL_DIR / "clip_vision.safetensors"))
    save_file(txt, str(MODEL_DIR / "clip_text.safetensors"))
    save_file(head, str(MODEL_DIR / "head.safetensors"))
    (MODEL_DIR / "head_config.json").write_text(json.dumps(cfg, indent=2))

    clip_dir = MODEL_DIR / "fashion-clip"
    clip_dir.mkdir(exist_ok=True)
    for fn in ["config.json", "preprocessor_config.json", "tokenizer_config.json", "tokenizer.json",
               "vocab.json", "merges.txt", "special_tokens_map.json"]:
        try:
            shutil.copy(hf_hub_download(CLIP_REPO, fn), clip_dir / fn)
        except Exception as e:  # some files are optional
            print("skip", fn, e)
    if not keep_pth:
        ckpt_path.unlink()
    print("done:", sorted(p.name for p in MODEL_DIR.iterdir()))


if __name__ == "__main__":
    main(keep_pth="--keep-pth" in sys.argv)
