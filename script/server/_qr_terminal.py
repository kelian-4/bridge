import struct
import zlib


def decode_png(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("pas un PNG")
    pos = 8
    ihdr = None
    palette = []
    idat = b""
    while pos < len(data):
        length, ctype = struct.unpack(">I4s", data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", chunk)
        elif ctype == b"PLTE":
            palette = [tuple(chunk[i:i + 3]) for i in range(0, len(chunk), 3)]
        elif ctype == b"IDAT":
            idat += chunk
        elif ctype == b"IEND":
            break
    if ihdr is None:
        raise ValueError("PNG sans IHDR")
    width, height, depth, ctype, _, _, interlace = ihdr
    if interlace:
        raise ValueError("PNG entrelacé non pris en charge")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    bits_pp = channels * depth
    bpp = max(1, bits_pp // 8)
    stride = (width * bits_pp + 7) // 8
    raw = zlib.decompress(idat)
    rows = []
    prev = bytearray(stride)
    i = 0
    for _ in range(height):
        f = raw[i]
        line = bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        if f == 1:
            for x in range(bpp, stride):
                line[x] = (line[x] + line[x - bpp]) & 255
        elif f == 2:
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 255
        elif f == 3:
            for x in range(stride):
                left = line[x - bpp] if x >= bpp else 0
                line[x] = (line[x] + ((left + prev[x]) >> 1)) & 255
        elif f == 4:
            for x in range(stride):
                a = line[x - bpp] if x >= bpp else 0
                b = prev[x]
                c = prev[x - bpp] if x >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[x] = (line[x] + pred) & 255
        rows.append(line)
        prev = line

    def sample(line, idx):
        if depth == 8:
            return line[idx]
        if depth == 16:
            return line[idx * 2]
        per_byte = 8 // depth
        byte = line[idx // per_byte]
        shift = 8 - depth * (idx % per_byte + 1)
        return (byte >> shift) & ((1 << depth) - 1)

    dark = []
    for line in rows:
        out = []
        for x in range(width):
            if ctype == 3:
                r, g, b = palette[sample(line, x)][:3]
                lum = 0.299 * r + 0.587 * g + 0.114 * b
            elif ctype == 0:
                lum = sample(line, x) * 255 / ((1 << min(depth, 8)) - 1)
            elif ctype == 4:
                lum = line[x * 2 * (depth // 8)] if depth >= 8 else 255
            else:
                step = channels * (depth // 8)
                r, g, b = line[x * step], line[x * step + (depth // 8)], line[x * step + 2 * (depth // 8)]
                lum = 0.299 * r + 0.587 * g + 0.114 * b
                if ctype == 6 and line[x * step + 3 * (depth // 8)] == 0:
                    lum = 255
            out.append(lum < 128)
        dark.append(out)
    return width, height, dark


def qr_modules(dark):
    height = len(dark)
    width = len(dark[0]) if height else 0
    xs = [x for row in dark for x, v in enumerate(row) if v]
    ys = [y for y, row in enumerate(dark) if any(row)]
    if not xs:
        raise ValueError("aucun pixel sombre")
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    top = dark[miny]
    run = 0
    x = minx
    while x < width and top[x]:
        run += 1
        x += 1
    module = run / 7.0
    span = maxx - minx + 1
    n_est = span / module
    n = min(range(21, 178, 4), key=lambda v: abs(v - n_est))
    size_x = span / n
    size_y = (maxy - miny + 1) / n
    modules = []
    for j in range(n):
        py = int(miny + (j + 0.5) * size_y)
        modules.append([dark[py][int(minx + (i + 0.5) * size_x)] for i in range(n)])
    return modules


def render(modules, quiet=4):
    n = len(modules)
    size = n + 2 * quiet

    def at(y, x):
        yy, xx = y - quiet, x - quiet
        return 0 <= yy < n and 0 <= xx < n and modules[yy][xx]

    lines = []
    for y in range(0, size, 2):
        parts = []
        for x in range(size):
            top, bottom = at(y, x), at(y + 1, x)
            fg = 30 if top else 97
            bg = 40 if bottom else 107
            parts.append(f"\x1b[{fg};{bg}m\u2580")
        lines.append("".join(parts) + "\x1b[0m")
    return "\n".join(lines)


def png_to_terminal(data):
    _, _, dark = decode_png(data)
    return render(qr_modules(dark))
