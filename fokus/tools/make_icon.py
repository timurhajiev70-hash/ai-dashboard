#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Генератор иконки Fokus: PNG → ICNS на чистом Python (zlib + struct, без PIL).

Рисунок: тёмный скруглённый квадрат (#1f1f1f) с «прицелом»: внешнее красное кольцо (#e5484d),
среднее белое кольцо, в центре красная точка. Края сглажены суперсэмплингом 4×4
(16 выборок на пиксель; пиксели, целиком лежащие внутри одной области, считаются одной выборкой —
результат тот же, но в 10 раз быстрее).

Запуск из корня проекта:
    python3 tools/make_icon.py                 # пишет Fokus.app/Contents/Resources/Fokus.icns и static/icon-256.png
    python3 tools/make_icon.py --out /tmp/x    # то же в другую папку (для тестов)
    python3 tools/make_icon.py --no-1024       # без чанка ic10 (если файл должен быть меньше)
    python3 tools/make_icon.py --check PATH    # разобрать ICNS: заголовок и чанки

Совместимо с Python 3.9, только стандартная библиотека. Результат детерминирован (одинаковые байты при каждом запуске).
"""
import argparse
import math
import os
import struct
import sys
import zlib
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(HERE)
DEFAULT_ICNS = os.path.join(PROJECT_DIR, 'Fokus.app', 'Contents', 'Resources', 'Fokus.icns')
DEFAULT_PNG = os.path.join(PROJECT_DIR, 'static', 'icon-256.png')

# Тип чанка ICNS → размер PNG в пикселях (все — обычные, не @2x, PNG внутри).
ICNS_TYPES = (
    ('icp4', 16), ('icp5', 32), ('icp6', 64), ('ic07', 128),
    ('ic08', 256), ('ic09', 512), ('ic10', 1024),
)

RGBA = Tuple[int, int, int, int]
BG = (0, 0, 0, 0)                 # прозрачный фон за скруглённым квадратом
DARK = (0x1f, 0x1f, 0x1f, 255)    # квадрат
RED = (0xe5, 0x48, 0x4d, 255)     # внешнее кольцо и точка
WHITE = (255, 255, 255, 255)      # среднее кольцо

SUPERSAMPLE = 4                   # 4×4 выборки на пиксель по краям


# ----------------------------------------------------------------- геометрия

class Shape(object):
    """Все размеры в пикселях для холста size×size; центр — середина холста."""

    def __init__(self, size: int):
        s = float(size)
        self.size = size
        self.c = s / 2.0
        self.half = s * 0.42            # половина стороны квадрата (отступ 8% с каждой стороны)
        self.corner = s * 0.19          # радиус скругления углов
        min_w = 1.0                     # кольца не тоньше 1 px (важно для 16 и 32)
        self.ring1_out = s * 0.32
        self.ring1_in = self.ring1_out - max(s * 0.07, min_w)
        self.ring2_out = s * 0.20
        self.ring2_in = self.ring2_out - max(s * 0.055, min_w)
        self.dot = max(s * 0.08, 1.0)

    def region(self, x: float, y: float) -> RGBA:
        """Цвет области, в которую попадает точка (x, y) холста."""
        dx = x - self.c
        dy = y - self.c
        # знаковое расстояние до скруглённого квадрата (отрицательное — внутри)
        qx = abs(dx) - (self.half - self.corner)
        qy = abs(dy) - (self.half - self.corner)
        outside = math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - self.corner
        if outside > 0.0:
            return BG
        r = math.hypot(dx, dy)
        if r <= self.dot:
            return RED
        if self.ring2_in <= r <= self.ring2_out:
            return WHITE
        if self.ring1_in <= r <= self.ring1_out:
            return RED
        return DARK


def pixel(shape: Shape, i: int, j: int) -> RGBA:
    """Цвет пикселя (i, j): одна выборка внутри области, 4×4 выборки на границе."""
    probes = (
        shape.region(i + 0.0, j + 0.0), shape.region(i + 1.0, j + 0.0),
        shape.region(i + 0.0, j + 1.0), shape.region(i + 1.0, j + 1.0),
        shape.region(i + 0.5, j + 0.5), shape.region(i + 0.5, j + 0.0),
        shape.region(i + 0.0, j + 0.5), shape.region(i + 1.0, j + 0.5),
        shape.region(i + 0.5, j + 1.0),
    )
    first = probes[0]
    uniform = True
    for p in probes:
        if p != first:
            uniform = False
            break
    if uniform:
        return first
    n = SUPERSAMPLE
    step = 1.0 / n
    sr = sg = sb = sa = 0
    for a in range(n):
        y = j + (a + 0.5) * step
        for b in range(n):
            x = i + (b + 0.5) * step
            r, g, bl, al = shape.region(x, y)
            # усреднение в premultiplied-пространстве: прозрачные выборки не затемняют цвет края
            sr += r * al
            sg += g * al
            sb += bl * al
            sa += al
    if sa == 0:
        return BG
    cnt = n * n
    return (int(round(sr / float(sa))), int(round(sg / float(sa))),
            int(round(sb / float(sa))), int(round(sa / float(cnt))))


def render(size: int) -> List[bytes]:
    """Строки RGBA (без байта фильтра) для холста size×size.

    Рисунок симметричен относительно обеих осей → считаем правый нижний квадрант и отражаем.
    """
    if size % 2:
        raise ValueError('size must be even')
    shape = Shape(size)
    half = size // 2
    rows = []  # type: List[bytes]
    for j in range(half, size):
        right = bytearray()
        for i in range(half, size):
            right += bytes(pixel(shape, i, j))
        left = bytearray()
        for k in range(half - 1, -1, -1):   # зеркало по x
            left += right[k * 4:k * 4 + 4]
        rows.append(bytes(left + right))
    return rows[::-1] + rows                # зеркало по y


# ----------------------------------------------------------------- PNG / ICNS

def png_chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data) & 0xffffffff
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', crc)


def encode_png(rows: List[bytes], width: int, height: int) -> bytes:
    raw = b''.join(b'\x00' + row for row in rows)   # фильтр 0 для каждой строки
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)   # 8 бит, RGBA
    return (b'\x89PNG\r\n\x1a\n'
            + png_chunk(b'IHDR', ihdr)
            + png_chunk(b'IDAT', zlib.compress(raw, 9))
            + png_chunk(b'IEND', b''))


def make_png(size: int) -> bytes:
    return encode_png(render(size), size, size)


def build_icns(pngs: Dict[str, bytes]) -> bytes:
    """pngs: тип чанка → PNG-данные. Порядок — как в ICNS_TYPES."""
    body = b''
    for kind, _size in ICNS_TYPES:
        if kind not in pngs:
            continue
        data = pngs[kind]
        body += kind.encode('ascii') + struct.pack('>I', 8 + len(data)) + data
    return b'icns' + struct.pack('>I', 8 + len(body)) + body


def png_size(data: bytes) -> Optional[Tuple[int, int]]:
    """(ширина, высота) из IHDR или None, если это не PNG."""
    if data[:8] != b'\x89PNG\r\n\x1a\n' or data[12:16] != b'IHDR':
        return None
    w, h = struct.unpack('>II', data[16:24])
    return w, h


def parse_icns(data: bytes) -> List[dict]:
    """Разбор ICNS: [{'type', 'length', 'png': (w, h) | None}, ...]. ValueError при повреждении."""
    if len(data) < 8 or data[:4] != b'icns':
        raise ValueError('not an ICNS file (no "icns" magic)')
    total = struct.unpack('>I', data[4:8])[0]
    if total != len(data):
        raise ValueError('ICNS length mismatch: header %d, file %d' % (total, len(data)))
    chunks = []
    pos = 8
    while pos < total:
        if pos + 8 > total:
            raise ValueError('truncated chunk header at %d' % pos)
        kind = data[pos:pos + 4].decode('ascii', 'replace')
        length = struct.unpack('>I', data[pos + 4:pos + 8])[0]
        if length < 8 or pos + length > total:
            raise ValueError('bad chunk length %d for %s at %d' % (length, kind, pos))
        payload = data[pos + 8:pos + length]
        chunks.append({'type': kind, 'length': length, 'png': png_size(payload)})
        pos += length
    return chunks


# ----------------------------------------------------------------- CLI

def generate(out_icns: str, out_png: str, with_1024: bool = True, log=None) -> Dict[str, bytes]:
    pngs = {}  # type: Dict[str, bytes]
    for kind, size in ICNS_TYPES:
        if size == 1024 and not with_1024:
            continue
        pngs[kind] = make_png(size)
        if log:
            log('  %s %4d px  %6d байт' % (kind, size, len(pngs[kind])))
    icns = build_icns(pngs)
    for path in (out_icns, out_png):
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
    with open(out_icns, 'wb') as f:
        f.write(icns)
    with open(out_png, 'wb') as f:
        f.write(pngs['ic08'])
    if log:
        log('ICNS: %s (%d байт, %d чанков)' % (out_icns, len(icns), len(pngs)))
        log('PNG:  %s' % out_png)
    return pngs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Иконка Fokus: PNG → ICNS без PIL.')
    ap.add_argument('--out', metavar='DIR', help='папка для Fokus.icns и icon-256.png (по умолчанию — пути проекта)')
    ap.add_argument('--no-1024', action='store_true', help='не включать чанк ic10 (1024 px)')
    ap.add_argument('--check', metavar='PATH', help='только разобрать существующий ICNS и показать чанки')
    args = ap.parse_args(argv)

    if args.check:
        with open(args.check, 'rb') as f:
            data = f.read()
        chunks = parse_icns(data)
        print('%s: %d байт, %d чанков' % (args.check, len(data), len(chunks)))
        for ch in chunks:
            png = ch['png']
            print('  %s  %7d байт  %s' % (ch['type'], ch['length'], ('PNG %dx%d' % png) if png else 'не PNG'))
        return 0

    if args.out:
        out_icns = os.path.join(args.out, 'Fokus.icns')
        out_png = os.path.join(args.out, 'icon-256.png')
    else:
        out_icns, out_png = DEFAULT_ICNS, DEFAULT_PNG
    generate(out_icns, out_png, with_1024=not args.no_1024, log=print)
    return 0


if __name__ == '__main__':
    sys.exit(main())
