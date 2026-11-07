#!/usr/bin/env python3
"""Fail if index.html points at local media that is missing or not a real file.

Render publishes the repository root. References such as assets/logo.png only
work when that relative path exists. This check parses index.html, resolves
every local image/video URL against the repo root, and rejects missing files,
Git LFS pointer text, and containers that cannot report a positive size or
duration.
"""

import re
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "index.html"
MEDIA_RE = re.compile(
    r"""(?P<q>["'])(?P<url>(?!https?:|//|data:|mailto:|#)[^"'?#]+\.(?:png|jpe?g|gif|webp|svg|mp4|webm|mov|m4v))(?:[?#][^"']*)?(?P=q)""",
    re.IGNORECASE,
)
LFS_PREFIX = b"version https://git-lfs.github.com/spec/v1"
BROWSER_VIDEO_CODECS = (b"avc1", b"avc3", b"hvc1", b"hev1", b"vp09", b"vp08")


def local_media_urls(html: str) -> list[str]:
    found = []
    seen = set()
    for match in MEDIA_RE.finditer(html):
        url = match.group("url")
        if url not in seen:
            seen.add(url)
            found.append(url)
    return found


def resolve_inside_root(url: str) -> Path:
    relative = Path(url)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{url} is not a path inside the repository root")
    path = (ROOT / relative).resolve()
    if ROOT not in path.parents and path != ROOT:
        raise ValueError(f"{url} escapes the repository root")
    return path


def read_box_header(data: bytes, offset: int):
    if offset + 8 > len(data):
        return None
    size, box_type = struct.unpack_from(">I4s", data, offset)
    header = 8
    if size == 1:
        if offset + 16 > len(data):
            return None
        size = struct.unpack_from(">Q", data, offset + 8)[0]
        header = 16
    elif size == 0:
        size = len(data) - offset
    if size < header or offset + size > len(data):
        return None
    return box_type, offset + header, offset + size


def iter_boxes(data: bytes, start: int, end: int):
    offset = start
    while offset + 8 <= end:
        parsed = read_box_header(data, offset)
        if parsed is None:
            break
        box_type, payload, next_offset = parsed
        yield box_type, payload, next_offset
        offset = next_offset


def mp4_duration_seconds(data: bytes) -> float:
    moov = None
    for box_type, payload, end in iter_boxes(data, 0, len(data)):
        if box_type == b"moov":
            moov = (payload, end)
            break
    if moov is None:
        raise ValueError("MP4 has no moov atom")
    payload, end = moov
    for box_type, child, child_end in iter_boxes(data, payload, end):
        if box_type != b"mvhd":
            continue
        version = data[child]
        if version == 0:
            timescale, duration = struct.unpack_from(">II", data, child + 12)
        elif version == 1:
            timescale, duration = struct.unpack_from(">IQ", data, child + 20)
        else:
            raise ValueError(f"unsupported mvhd version {version}")
        if timescale <= 0 or duration <= 0:
            raise ValueError("MP4 duration is zero")
        return duration / timescale
    raise ValueError("MP4 moov has no mvhd atom")


def mp4_has_video(data: bytes) -> bool:
    moov = None
    for box_type, payload, end in iter_boxes(data, 0, len(data)):
        if box_type == b"moov":
            moov = data[payload:end]
            break
    if moov is None:
        return False
    if b"vide" not in moov:
        return False
    return any(codec in moov for codec in BROWSER_VIDEO_CODECS)


def validate_bytes(path: Path, data: bytes) -> str:
    if not data:
        raise ValueError("file is empty")
    if data.startswith(LFS_PREFIX) or data.startswith(b"version https://git-lfs.github.com/spec/"):
        raise ValueError("file is a Git LFS pointer, not media content")
    suffix = path.suffix.lower()
    if suffix == ".png":
        if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
            raise ValueError("not a PNG image")
        width, height = struct.unpack_from(">II", data, 16)
        if width <= 0 or height <= 0:
            raise ValueError("PNG has no pixel dimensions")
        return f"PNG {width}x{height}"
    if suffix in {".jpg", ".jpeg"}:
        if data[:3] != b"\xff\xd8\xff":
            raise ValueError("not a JPEG image")
        offset = 2
        while offset + 8 < len(data):
            if data[offset] != 0xFF:
                offset += 1
                continue
            marker = data[offset + 1]
            if marker in (0xD8, 0xD9):
                offset += 2
                continue
            if offset + 4 > len(data):
                break
            segment = struct.unpack_from(">H", data, offset + 2)[0]
            if marker in (0xC0, 0xC1, 0xC2):
                height, width = struct.unpack_from(">HH", data, offset + 5)
                if width <= 0 or height <= 0:
                    raise ValueError("JPEG has no pixel dimensions")
                return f"JPEG {width}x{height}"
            if segment < 2:
                break
            offset += 2 + segment
        raise ValueError("JPEG has no start-of-frame marker")
    if suffix == ".mp4":
        if len(data) < 12 or data[4:8] != b"ftyp":
            raise ValueError("not an MP4 file")
        if not mp4_has_video(data):
            raise ValueError("MP4 has no browser-playable video track")
        seconds = mp4_duration_seconds(data)
        return f"MP4 {seconds:.2f}s"
    if suffix == ".gif":
        if not data.startswith((b"GIF87a", b"GIF89a")):
            raise ValueError("not a GIF image")
        return "GIF"
    if suffix == ".webp":
        if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
            raise ValueError("not a WebP image")
        return "WebP"
    if suffix == ".svg":
        head = data[:200].lstrip().lower()
        if b"<svg" not in head:
            raise ValueError("not an SVG image")
        return "SVG"
    if suffix in {".webm", ".mov", ".m4v"}:
        if len(data) < 16:
            raise ValueError("media file is too small")
        return suffix[1:].upper()
    raise ValueError(f"unsupported media type {suffix}")


def self_test() -> int:
    html = '<img src="https://example.com/a.png"><img src="luma-logo.png"><img src="assets/missing.png">'
    urls = local_media_urls(html)
    if urls != ["luma-logo.png", "assets/missing.png"]:
        print(f"self-test url parse failed: {urls}", file=sys.stderr)
        return 1
    try:
        validate_bytes(Path("pointer.png"), LFS_PREFIX + b"\noid sha256:abc\nsize 1\n")
    except ValueError as exc:
        if "Git LFS pointer" not in str(exc):
            print(f"self-test LFS message failed: {exc}", file=sys.stderr)
            return 1
    else:
        print("self-test failed to reject a Git LFS pointer", file=sys.stderr)
        return 1
    try:
        validate_bytes(Path("bad.png"), b"Not Found\n")
    except ValueError:
        pass
    else:
        print("self-test failed to reject a non-image", file=sys.stderr)
        return 1
    try:
        resolve_inside_root("../outside.png")
    except ValueError:
        pass
    else:
        print("self-test failed to reject a path outside the repo", file=sys.stderr)
        return 1
    print("self-test ok")
    return 0


def main() -> int:
    if not INDEX.is_file():
        print("missing index.html", file=sys.stderr)
        return 1
    html = INDEX.read_text(encoding="utf-8")
    urls = local_media_urls(html)
    if not urls:
        print("index.html has no local media references", file=sys.stderr)
        return 1
    failures = []
    for url in urls:
        try:
            path = resolve_inside_root(url)
            if not path.is_file():
                raise ValueError("file does not exist")
            detail = validate_bytes(path, path.read_bytes())
        except Exception as exc:  # one bad asset should not hide the rest
            failures.append(f"{url}: {exc}")
            print(f"FAIL  {url}: {exc}")
            continue
        print(f"OK    {url} -> {path.relative_to(ROOT)} ({detail})")
    if failures:
        print(f"\n{len(failures)} media reference(s) failed", file=sys.stderr)
        return 1
    print(f"\n{len(urls)} local media reference(s) resolve to real files")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    sys.exit(main())
