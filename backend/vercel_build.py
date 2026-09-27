"""Vercel build step for the backend service (stdlib only). Run from backend/.

Fetches everything that is too big for the 100 MB Hobby CLI upload into the function bundle:
  * Hugging Face models (public, pinned revisions) into models/hf/hub in HF-cache layout so
    from_pretrained(repo_id) works with HF_HUB_OFFLINE=1:
      patrickjohncyh/fashion-clip, mattmdjaga/segformer_b2_clothes
  * OutfitTransformer weights into models/outfit_transformer/:
      head.safetensors      <- env OT_HEAD_URL (the user's Vercel Blob store)
      clip_vision/clip_text <- sliced out of fashion-clip model.safetensors (the OT checkpoint's
                               frozen towers are bit-identical to fashion-clip; verified locally)
  * Seed snapshot (DB + FAISS + media) <- env SEED_URL (Vercel Blob tarball), extracted to seed_data/
"""
import json
import os
import struct
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
HUB = HERE / "models" / "hf" / "hub"
OT = HERE / "models" / "outfit_transformer"
REPOS = {
    "patrickjohncyh/fashion-clip": ("7e3ba62ce16b379a1ab479346b66f192e76f51b7",
        ["config.json", "merges.txt", "model.safetensors", "preprocessor_config.json",
         "special_tokens_map.json", "tokenizer.json", "tokenizer_config.json", "vocab.json"]),
    "mattmdjaga/segformer_b2_clothes": ("584abc1e1d260e23c0fc627c5217a09b2b461046",
        ["config.json", "preprocessor_config.json", "model.safetensors"]),
}


def fetch(url: str, dest: Path) -> None:
    if dest.exists() and dest.stat().st_size > 0:
        print("exists", dest, flush=True)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    t = time.time()
    tmp = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "fitcheck-build"}), timeout=120) as r, open(tmp, "wb") as f:
                while True:
                    b = r.read(8 << 20)
                    if not b:
                        break
                    f.write(b)
            break
        except Exception as e:  # noqa: BLE001
            print(f"retry {attempt} {url.split('?')[0][:80]}: {e}", flush=True)
            time.sleep(3)
    else:
        sys.exit(f"download failed: {dest.name}")
    tmp.rename(dest)
    print(f"got {dest.relative_to(HERE)} {dest.stat().st_size/1e6:.0f} MB in {time.time()-t:.0f}s", flush=True)


def slice_safetensors(src: Path, dest: Path, keep) -> None:
    """Write a safetensors file containing only the tensors whose names satisfy keep()."""
    with open(src, "rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        header = json.loads(f.read(n))
        base = 8 + n
        names = [k for k in header if k != "__metadata__" and keep(k)]
        new, off = {}, 0
        for k in names:
            s, e = header[k]["data_offsets"]
            new[k] = {"dtype": header[k]["dtype"], "shape": header[k]["shape"], "data_offsets": [off, off + e - s]}
            off += e - s
        hb = json.dumps(new, separators=(",", ":")).encode()
        hb += b" " * ((8 - len(hb) % 8) % 8)
        with open(dest, "wb") as out:
            out.write(struct.pack("<Q", len(hb)) + hb)
            for k in names:
                s, e = header[k]["data_offsets"]
                f.seek(base + s)
                out.write(f.read(e - s))
    print(f"sliced {dest.name}: {len(names)} tensors {dest.stat().st_size/1e6:.0f} MB", flush=True)


def main() -> None:
    t0 = time.time()
    for repo, (rev, files) in REPOS.items():
        d = HUB / ("models--" + repo.replace("/", "--"))
        (d / "refs").mkdir(parents=True, exist_ok=True)
        (d / "refs" / "main").write_text(rev)
        for fn in files:
            fetch(f"https://huggingface.co/{repo}/resolve/{rev}/{fn}", d / "snapshots" / rev / fn)
    fc = HUB / "models--patrickjohncyh--fashion-clip" / "snapshots" / REPOS["patrickjohncyh/fashion-clip"][0] / "model.safetensors"
    OT.mkdir(parents=True, exist_ok=True)
    slice_safetensors(fc, OT / "clip_vision.safetensors",
                      lambda k: (k.startswith("vision_model.") and not k.endswith("position_ids")) or k == "visual_projection.weight")
    slice_safetensors(fc, OT / "clip_text.safetensors",
                      lambda k: (k.startswith("text_model.") and not k.endswith("position_ids")) or k == "text_projection.weight")
    fetch(os.environ["OT_HEAD_URL"], OT / "head.safetensors")
    tgz = HERE / "seed_data.tgz"
    fetch(os.environ["SEED_URL"], tgz)
    with tarfile.open(tgz) as t:
        t.extractall(HERE, filter="data")
    tgz.unlink()
    print(f"vercel_build done in {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
