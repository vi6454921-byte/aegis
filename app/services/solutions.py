"""Решения: HTML-файлы или ZIP от админа → data/solutions/{order_id}/."""
from __future__ import annotations

import html
import shutil
import zipfile
from pathlib import Path, PurePosixPath

HTML_EXT = {".html", ".htm"}
ALLOWED_EXT = HTML_EXT | {".zip"}
MAX_ZIP_FILES = 500
MAX_UNZIPPED = 200 * 1024 * 1024
INDEX_NAME = "aegis_index.html"


class SolutionError(Exception):
    pass


def staging_dir(solutions_dir: Path, order_id: int) -> Path:
    return solutions_dir / f".incoming_{order_id}"


def reset_staging(solutions_dir: Path, order_id: int) -> Path:
    d = staging_dir(solutions_dir, order_id)
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    return d


def safe_name(name: str) -> str:
    base = PurePosixPath(name.replace("\\", "/")).name
    cleaned = "".join(c for c in base if c.isalnum() or c in "._- ()[]").strip(" .")
    return cleaned or "file.html"


def _safe_extract(zip_path: Path, dest: Path) -> None:
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as e:
        raise SolutionError("ZIP повреждён") from e
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > MAX_ZIP_FILES:
            raise SolutionError("В архиве слишком много файлов")
        if sum(i.file_size for i in infos) > MAX_UNZIPPED:
            raise SolutionError("Архив слишком большой после распаковки")
        root = dest.resolve()
        for info in infos:
            parts = PurePosixPath(info.filename.replace("\\", "/")).parts
            if not parts or info.filename.startswith(("/", "\\")) or ".." in parts or parts[0].endswith(":"):
                raise SolutionError(f"Недопустимый путь в архиве: {info.filename}")
            if parts[0] == "__MACOSX":
                continue
            target = (dest / PurePosixPath(*parts)).resolve()
            if not target.is_relative_to(root):
                raise SolutionError(f"Недопустимый путь в архиве: {info.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)


def _find_entry(root: Path) -> str:
    htmls = sorted((p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in HTML_EXT),
                   key=lambda p: (len(p.relative_to(root).parts), str(p)))
    if not htmls:
        raise SolutionError("В решении нет HTML-файлов")
    for p in htmls:
        if p.name.lower() in ("index.html", "index.htm"):
            return p.relative_to(root).as_posix()
    if len(htmls) == 1:
        return htmls[0].relative_to(root).as_posix()
    items = "".join(f'<li><a href="{html.escape(p.relative_to(root).as_posix())}">'
                    f"{html.escape(p.relative_to(root).as_posix())}</a></li>" for p in htmls)
    (root / INDEX_NAME).write_text(
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Решение</title>'
        "<style>body{font:16px system-ui,sans-serif;margin:24px}li{margin:10px 0}</style></head>"
        f"<body><h2>Файлы решения</h2><ul>{items}</ul></body></html>", encoding="utf-8")
    return INDEX_NAME


def finalize(solutions_dir: Path, order_id: int) -> str:
    """Собирает файлы из staging в data/solutions/{order_id}/ (старая версия заменяется). Возвращает entry."""
    staging = staging_dir(solutions_dir, order_id)
    files = [p for p in staging.iterdir() if p.is_file()] if staging.exists() else []
    if not files:
        raise SolutionError("Не получено ни одного файла")
    build = solutions_dir / f".build_{order_id}"
    shutil.rmtree(build, ignore_errors=True)
    build.mkdir(parents=True)
    try:
        for f in files:
            if f.suffix.lower() == ".zip":
                _safe_extract(f, build)
            else:
                shutil.copyfile(f, build / f.name)
        entry = _find_entry(build)
    except Exception:
        shutil.rmtree(build, ignore_errors=True)
        raise
    target = solutions_dir / str(order_id)
    shutil.rmtree(target, ignore_errors=True)
    build.rename(target)
    shutil.rmtree(staging, ignore_errors=True)
    return entry


def resolve(solutions_dir: Path, order_id: int, rel: str) -> Path | None:
    base = (solutions_dir / str(order_id)).resolve()
    try:
        p = (base / rel).resolve()
    except (OSError, ValueError):
        return None
    if not p.is_relative_to(base) or not p.is_file():
        return None
    return p
