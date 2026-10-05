"""Noise stage: degraded page images (scan / photo) from the clean born-digital PDFs.

The PDF stays the clean version. Each page gets rasterized and degraded with seeded, affine-only
geometry (rotation + scale + shift), so word boxes and gold-field boxes are transformed exactly
and written next to the image.

    out_dir/scans/<type>/<doc_id>_p<n>_<profile>.jpg
    out_dir/scans/<type>/<doc_id>.json      per page: image, profile, params, size, words, boxes
"""

from __future__ import annotations

import io
import json
import math
import random
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image, ImageEnhance, ImageFilter

PROFILES = {
    # name: (rotation deg, scale range, blur radius, noise sigma, jpeg quality, light gradient, vignette)
    "scan": dict(rot=1.5, scale=(0.97, 1.0), blur=(0.0, 0.9), noise=(2, 9), jpeg=(45, 85), tint=0.6,
                 gradient=0.0, vignette=0.0, speckle=0.5),
    "photo": dict(rot=4.0, scale=(0.88, 0.97), blur=(0.4, 1.6), noise=(4, 12), jpeg=(35, 75), tint=0.3,
                  gradient=0.35, vignette=0.35, speckle=0.0),
}


def _affine(w: int, h: int, angle: float, scale: float, dx: float, dy: float) -> np.ndarray:
    """Forward 2x3 matrix: scale + rotate about the page centre, then shift (pixels)."""
    a = math.radians(angle)
    cos, sin = math.cos(a) * scale, math.sin(a) * scale
    cx, cy = w / 2, h / 2
    return np.array([[cos, -sin, cx - cos * cx + sin * cy + dx],
                     [sin, cos, cy - sin * cx - cos * cy + dy]])


def _map_box(m: np.ndarray, box, k: float) -> list[float]:
    x0, y0, x1, y1 = (v * k for v in box)
    pts = np.array([[x0, y0, 1], [x1, y0, 1], [x0, y1, 1], [x1, y1, 1]]) @ m.T
    return [round(float(pts[:, 0].min()), 1), round(float(pts[:, 1].min()), 1),
            round(float(pts[:, 0].max()), 1), round(float(pts[:, 1].max()), 1)]


def degrade(img: Image.Image, rng: random.Random, profile: str) -> tuple[Image.Image, np.ndarray, dict]:
    p = PROFILES[profile]
    w, h = img.size
    params = {"angle": round(rng.uniform(-p["rot"], p["rot"]), 2), "scale": round(rng.uniform(*p["scale"]), 3),
              "dx": round(rng.uniform(-0.015, 0.015) * w, 1), "dy": round(rng.uniform(-0.015, 0.015) * h, 1),
              "blur": round(rng.uniform(*p["blur"]), 2), "noise": round(rng.uniform(*p["noise"]), 1),
              "jpeg": rng.randint(*p["jpeg"]), "contrast": round(rng.uniform(0.8, 1.1), 2),
              "brightness": round(rng.uniform(0.9, 1.08), 2)}
    m = _affine(w, h, params["angle"], params["scale"], params["dx"], params["dy"])
    inv = np.linalg.inv(np.vstack([m, [0, 0, 1]]))[:2]           # PIL wants output -> input
    bg = (250, 248, 240) if profile == "scan" else (rng.randint(150, 200),) * 3
    img = img.transform((w, h), Image.AFFINE, tuple(inv.flatten()), resample=Image.BICUBIC, fillcolor=bg)

    if rng.random() < p["tint"]:                                  # yellowed / grey paper
        tint = np.array([1.0, rng.uniform(0.96, 1.0), rng.uniform(0.86, 0.97)])
        img = Image.fromarray(np.clip(np.asarray(img, dtype=np.float32) * tint, 0, 255).astype(np.uint8))
    img = ImageEnhance.Contrast(img).enhance(params["contrast"])
    img = ImageEnhance.Brightness(img).enhance(params["brightness"])
    if params["blur"] > 0.05:
        img = img.filter(ImageFilter.GaussianBlur(params["blur"]))

    arr = np.asarray(img, dtype=np.float32)
    nrng = np.random.default_rng(rng.getrandbits(32))
    arr += nrng.normal(0, params["noise"], arr.shape)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    if p["gradient"]:                                             # uneven lighting across the page
        ang = rng.uniform(0, 2 * math.pi)
        g = (xx / w * math.cos(ang) + yy / h * math.sin(ang))
        arr *= (1 - p["gradient"] * (g - g.min()) / (np.ptp(g) or 1))[..., None]
    if p["vignette"]:
        r = np.sqrt(((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2)
        arr *= (1 - p["vignette"] * np.clip(r - 0.5, 0, None))[..., None]
    if p["speckle"] and rng.random() < p["speckle"]:              # dust specks from the scanner glass
        n = rng.randint(30, 200)
        ys, xs = nrng.integers(0, h, n), nrng.integers(0, w, n)
        arr[ys, xs] = nrng.uniform(0, 90, (n, 1))
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=params["jpeg"])
    return Image.open(io.BytesIO(buf.getvalue())), m, params


def augment_doc(task: dict) -> dict:
    run, doc_type, doc_id = Path(task["run"]), task["type"], task["doc_id"]
    rng = random.Random(f"augment:{task['seed']}:{doc_id}")
    gold = json.loads((run / "gold" / doc_type / f"{doc_id}.json").read_text())
    words_path = run / "words" / doc_type / f"{doc_id}.json"
    words = json.loads(words_path.read_text()) if words_path.exists() else []
    out = run / "scans" / doc_type
    out.mkdir(parents=True, exist_ok=True)
    k = task["dpi"] / 72
    pages = []
    with pymupdf.open(run / "pdf" / doc_type / f"{doc_id}.pdf") as pdf:
        for pno, page in enumerate(pdf):
            pix = page.get_pixmap(dpi=task["dpi"])
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            profile = task["profile"] if task["profile"] != "mixed" else rng.choice(["scan", "scan", "photo"])
            img, m, params = degrade(img, rng, profile)
            name = f"{doc_id}_p{pno}_{profile}.jpg"
            img.save(out / name, "JPEG", quality=params["jpeg"])
            pages.append({
                "page": pno, "image": f"scans/{doc_type}/{name}", "profile": profile, "params": params,
                "width": img.width, "height": img.height, "dpi": task["dpi"],
                "words": [{"text": w["text"], "bbox": _map_box(m, w["bbox"], k)} for w in words if w["page"] == pno],
                "boxes": {f: [_map_box(m, b["bbox"], k) for b in bs if b["page"] == pno]
                          for f, bs in gold.get("boxes", {}).items() if any(b["page"] == pno for b in bs)},
            })
    (out / f"{doc_id}.json").write_text(json.dumps({"doc_id": doc_id, "pages": pages}, ensure_ascii=False))
    return {"doc_id": doc_id, "pages": len(pages)}


def run_augment(run_dir: Path, fraction: float = 1.0, profile: str = "mixed", dpi: int = 150, seed: int = 0,
                workers: int = 4, log=print) -> int:
    rows = [json.loads(l) for l in (run_dir / "manifest.jsonl").open(encoding="utf-8")]
    rows = [r for r in rows if r["status"] == "ok"]
    rng = random.Random(f"augment-select:{seed}")
    tasks = [{"run": str(run_dir), "type": r["type"], "doc_id": r["doc_id"], "dpi": dpi, "profile": profile,
              "seed": seed} for r in rows if rng.random() < fraction]
    log(f"[augment] {len(tasks)} of {len(rows)} documents, profile={profile}, dpi={dpi}")
    started, pages = time.time(), 0
    with ProcessPoolExecutor(workers) as pool:
        for i, res in enumerate(pool.map(augment_doc, tasks, chunksize=8), 1):
            pages += res["pages"]
            if i % 1000 == 0:
                log(f"[augment] {i}/{len(tasks)}")
    log(f"[augment] {pages} page images in {time.time() - started:.0f}s")
    return pages
