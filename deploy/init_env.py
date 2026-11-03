#!/usr/bin/env python3
"""Create/update .env from .env.example: keeps existing values, generates missing secrets, sets the public URL.
Never prints secret values.   python3 deploy/init_env.py [--public-url https://fitcheck.example.com]"""
import argparse
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GEN = {"POSTGRES_PASSWORD": 32, "MINIO_ROOT_PASSWORD": 32, "AUTH_SECRET": 48, "BACKEND_JWT_SECRET": 48,
       "INTERNAL_API_SECRET": 48, "MEDIA_SIGNING_SECRET": 48}


def parse(p: Path) -> list[tuple[str, str | None, str]]:
    out = []
    for line in p.read_text().splitlines() if p.exists() else []:
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out.append((k.strip(), v, line))
        else:
            out.append(("", None, line))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--public-url")
    a = ap.parse_args()
    current = {k: v for k, v, _ in parse(ROOT / ".env") if k}
    lines, made = [], []
    for k, v, raw in parse(ROOT / ".env.example"):
        if not k:
            lines.append(raw)
            continue
        val = current.get(k, v)
        if k in GEN and not val:
            val = secrets.token_urlsafe(GEN[k])[:GEN[k]].replace("-", "x").replace("_", "y")
            made.append(k)
        if a.public_url and k in ("PUBLIC_URL", "AUTH_URL", "APP_URL", "ALLOWED_ORIGINS"):
            val = a.public_url.rstrip("/")
        lines.append(f"{k}={val}")
    extra = [f"{k}={v}" for k, v in current.items() if k not in {k2 for k2, _, _ in parse(ROOT / '.env.example')}]
    path = ROOT / ".env"
    path.write_text("\n".join(lines + extra) + "\n")
    os.chmod(path, 0o600)
    print(f"wrote {path} (generated: {', '.join(made) or 'none'}; public url: {a.public_url or current.get('PUBLIC_URL')})")


if __name__ == "__main__":
    main()
