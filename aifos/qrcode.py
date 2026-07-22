"""纯标准库 QR 码生成:把外网访问地址变成手机可直接扫描的二维码。

不依赖 pillow/qrcode/segno 等第三方库,字节模式足以编码任意 URL。
支持纠错等级 L/M/Q/H 与版本 1–10(远超一条隧道 URL 所需长度)。
可渲染成:
  - 终端半块字符串:Mac 终端里直接对手机扫码(``render_terminal``)
  - SVG:网页“外网访问”面板里显示,手机对着屏幕扫(``render_svg``)

之所以自己写而不装库:平台坚持零第三方依赖,且外网访问这一环恰恰
要在“装不了东西/网络不稳”的现场也能用,内置实现最稳。
"""

# --- GF(256) 伽罗华域(QR 用本原多项式 0x11d) ---------------------------
_EXP = [0] * 512
_LOG = [0] * 256


def _init_gf():
    x = 1
    for i in range(255):
        _EXP[i] = x
        _LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        _EXP[i] = _EXP[i - 255]


_init_gf()


def _gf_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _rs_generator(n):
    """n 个纠错码字对应的生成多项式(系数高位在前)。"""
    g = [1]
    for i in range(n):
        new = [0] * (len(g) + 1)
        for j in range(len(g)):
            new[j] ^= g[j]
            new[j + 1] ^= _gf_mul(g[j], _EXP[i])
        g = new
    return g


def _rs_encode(data, n):
    """Reed-Solomon 纠错码字 = data 多项式除以生成多项式的余数。"""
    gen = _rs_generator(n)
    res = list(data) + [0] * n
    for i in range(len(data)):
        coef = res[i]
        if coef:
            for j in range(len(gen)):
                res[i + j] ^= _gf_mul(gen[j], coef)
    return res[len(data):]


# --- 版本/纠错容量表(ISO/IEC 18004 Table 9,版本 1–10) ----------------
# 每项:(每块纠错码字数, [(块数, 每块数据码字数), ...])
_BLOCKS = {
    1: {"L": (7, [(1, 19)]), "M": (10, [(1, 16)]),
        "Q": (13, [(1, 13)]), "H": (17, [(1, 9)])},
    2: {"L": (10, [(1, 34)]), "M": (16, [(1, 28)]),
        "Q": (22, [(1, 22)]), "H": (28, [(1, 16)])},
    3: {"L": (15, [(1, 55)]), "M": (26, [(1, 44)]),
        "Q": (18, [(2, 17)]), "H": (22, [(2, 13)])},
    4: {"L": (20, [(1, 80)]), "M": (18, [(2, 32)]),
        "Q": (26, [(2, 24)]), "H": (16, [(4, 9)])},
    5: {"L": (26, [(1, 108)]), "M": (24, [(2, 43)]),
        "Q": (18, [(2, 15), (2, 16)]), "H": (22, [(2, 11), (2, 12)])},
    6: {"L": (18, [(2, 68)]), "M": (16, [(4, 27)]),
        "Q": (24, [(4, 19)]), "H": (28, [(4, 15)])},
    7: {"L": (20, [(2, 78)]), "M": (18, [(4, 31)]),
        "Q": (18, [(2, 14), (4, 15)]), "H": (26, [(4, 13), (1, 14)])},
    8: {"L": (24, [(2, 97)]), "M": (22, [(2, 38), (2, 39)]),
        "Q": (22, [(4, 18), (2, 19)]), "H": (26, [(4, 14), (2, 15)])},
    9: {"L": (30, [(2, 116)]), "M": (22, [(3, 36), (2, 37)]),
        "Q": (20, [(4, 16), (4, 17)]), "H": (24, [(4, 12), (4, 13)])},
    10: {"L": (18, [(2, 68), (2, 69)]), "M": (26, [(4, 43), (1, 44)]),
         "Q": (24, [(6, 19), (2, 20)]), "H": (28, [(6, 15), (2, 16)])},
}

# 各版本对齐图案中心坐标(版本 1 无对齐图案)
_ALIGN = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
    6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46],
    10: [6, 28, 50],
}

# 纠错等级 → 格式信息里的 2 位指示符
_ECL_BITS = {"L": 1, "M": 0, "Q": 3, "H": 2}


def _total_data_codewords(version, ecl):
    ec_per, groups = _BLOCKS[version][ecl]
    return sum(nb * dcw for nb, dcw in groups)


def _pick_version(nbytes, ecl):
    for version in range(1, 11):
        cc_bits = 8 if version <= 9 else 16
        cap_bits = _total_data_codewords(version, ecl) * 8
        need = 4 + cc_bits + nbytes * 8
        if need <= cap_bits:
            return version
    raise ValueError("URL 过长,超出内置 QR 版本上限(建议缩短外网地址)")


# --- 数据编码(字节模式) ----------------------------------------------
def _encode_bits(data, version, ecl):
    total = _total_data_codewords(version, ecl)
    bits = []

    def put(val, n):
        for i in range(n - 1, -1, -1):
            bits.append((val >> i) & 1)

    put(0b0100, 4)                       # 字节模式
    put(len(data), 8 if version <= 9 else 16)
    for b in data:
        put(b, 8)
    cap = total * 8
    put(0, min(4, cap - len(bits)))       # 终止符
    # 补齐到码字边界:与 ISO 参考实现一致,已对齐时也补满一字节
    put(0, min(8 - (len(bits) % 8), cap - len(bits)))
    pad = (0xEC, 0x11)
    i = 0
    while len(bits) < cap:
        put(pad[i % 2], 8)
        i += 1
    codewords = []
    for k in range(0, len(bits), 8):
        byte = 0
        for b in bits[k:k + 8]:
            byte = (byte << 1) | b
        codewords.append(byte)
    return codewords


def _interleave(data_cw, version, ecl):
    ec_per, groups = _BLOCKS[version][ecl]
    blocks = []
    idx = 0
    for nb, dcw in groups:
        for _ in range(nb):
            block = data_cw[idx:idx + dcw]
            idx += dcw
            blocks.append((block, _rs_encode(block, ec_per)))
    out = []
    max_d = max(len(d) for d, _ in blocks)
    for i in range(max_d):
        for d, _ in blocks:
            if i < len(d):
                out.append(d[i])
    max_e = max(len(e) for _, e in blocks)
    for i in range(max_e):
        for _, e in blocks:
            if i < len(e):
                out.append(e[i])
    return out


# --- 模块布局 ----------------------------------------------------------
_MASKS = [
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]


def _blank(n):
    return [[0] * n for _ in range(n)]


def _draw_function_patterns(mod, res, version, n):
    def finder(top, left):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = top + dr, left + dc
                if not (0 <= r < n and 0 <= c < n):
                    continue
                res[r][c] = True
                ring = (0 <= dr <= 6 and dc in (0, 6)) or \
                       (0 <= dc <= 6 and dr in (0, 6))
                core = 2 <= dr <= 4 and 2 <= dc <= 4
                mod[r][c] = 1 if (ring or core) else 0

    finder(0, 0)
    finder(0, n - 7)
    finder(n - 7, 0)

    for i in range(8, n - 8):            # 定时图案
        bit = 1 if i % 2 == 0 else 0
        mod[6][i] = bit
        res[6][i] = True
        mod[i][6] = bit
        res[i][6] = True

    centers = _ALIGN[version]
    for r in centers:
        for c in centers:
            if (r <= 7 and c <= 7) or (r <= 7 and c >= n - 8) or \
                    (r >= n - 8 and c <= 7):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    rr, cc = r + dr, c + dc
                    res[rr][cc] = True
                    edge = max(abs(dr), abs(dc))
                    mod[rr][cc] = 1 if edge != 1 else 0

    res[n - 8][8] = True                 # 固定黑点位:掩码评估时保持 0,
    #                                      画格式信息(_draw_format)时才置黑

    for i in range(9):                   # 预留格式信息区
        for (r, c) in ((8, i), (i, 8)):
            if 0 <= r < n and 0 <= c < n:
                res[r][c] = True
    for i in range(8):
        res[8][n - 1 - i] = True
        res[n - 1 - i][8] = True

    if version >= 7:                     # 预留版本信息区
        for i in range(6):
            for j in range(3):
                res[n - 11 + j][i] = True
                res[i][n - 11 + j] = True


def _place_data(mod, res, bits, n):
    idx = 0
    col = n - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        for k in range(n):
            row = (n - 1 - k) if upward else k
            for c in (col, col - 1):
                if not res[row][c]:
                    mod[row][c] = bits[idx] if idx < len(bits) else 0
                    idx += 1
        col -= 2
        upward = not upward


def _draw_format(mod, n, ecl, mask):
    data = (_ECL_BITS[ecl] << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    bits = ((data << 10) | rem) ^ 0x5412

    def bit(i):
        return (bits >> i) & 1

    # 第一份(左上角周围):竖排在第 8 列,横排在第 8 行
    for i in range(6):
        mod[i][8] = bit(i)
    mod[7][8] = bit(6)
    mod[8][8] = bit(7)
    mod[8][7] = bit(8)
    for i in range(9, 15):
        mod[8][14 - i] = bit(i)
    # 第二份(右上/左下):横排右侧 + 竖排底部
    for i in range(8):
        mod[8][n - 1 - i] = bit(i)
    for i in range(8, 15):
        mod[n - 15 + i][8] = bit(i)
    mod[n - 8][8] = 1


def _draw_version(mod, n, version):
    if version < 7:
        return
    rem = version
    for _ in range(12):
        rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
    bits = (version << 12) | rem
    for i in range(18):
        b = (bits >> i) & 1
        a = n - 11 + i % 3
        c = i // 3
        mod[a][c] = b
        mod[c][a] = b


_N3_PATTERN = [1, 0, 1, 1, 1, 0, 1]


def _find(seq, pat, start):
    end = len(seq) - len(pat)
    i = start
    while i <= end:
        if seq[i:i + len(pat)] == pat:
            return i
        i += 1
    return -1


def _penalty(mod, n):
    """掩码惩罚分(ISO/IEC 18004 表 11 四条规则),与主流实现严格一致。"""

    def n3(seq):
        count = 0
        idx = _find(seq, _N3_PATTERN, 0)
        while idx != -1:
            offset = idx + 7
            before = any(seq[max(idx - 4, 0):idx])
            after = any(seq[offset:offset + 4])
            if idx in (0, n - 7) or not before or not after:
                count += 40
                idx = _find(seq, _N3_PATTERN, offset)
            else:
                idx = _find(seq, _N3_PATTERN, idx + 4)
        return count

    score = 0
    cols = [[mod[r][c] for r in range(n)] for c in range(n)]
    # 规则一(连续同色)+ 规则三(形似定位图案)按行、按列各算一遍
    for lines in (mod, cols):
        for row in lines:
            run = 1
            for i in range(1, n):
                if row[i] == row[i - 1]:
                    run += 1
                else:
                    if run >= 5:
                        score += run - 2
                    run = 1
            if run >= 5:
                score += run - 2
            score += n3(row)
    # 规则二:2x2 同色块
    for r in range(n - 1):
        for c in range(n - 1):
            v = mod[r][c]
            if v == mod[r][c + 1] == mod[r + 1][c] == mod[r + 1][c + 1]:
                score += 3
    # 规则四:黑色占比偏离 50%
    dark = sum(sum(r) for r in mod)
    percent = dark / (n * n)
    score += 10 * int(abs(percent * 100 - 50) / 5)
    return score


def _build_masked(version, ecl, codewords, mask):
    """铺函数图案 + 数据 + 掩码,但不画格式/版本信息(用于掩码评估)。"""
    n = 17 + 4 * version
    mod = _blank(n)
    res = [[False] * n for _ in range(n)]
    _draw_function_patterns(mod, res, version, n)
    bits = []
    for cw in codewords:
        for i in range(7, -1, -1):
            bits.append((cw >> i) & 1)
    _place_data(mod, res, bits, n)
    for r in range(n):
        for c in range(n):
            if not res[r][c] and _MASKS[mask](r, c):
                mod[r][c] ^= 1
    return mod


def _finalize(masked, n, version, ecl, mask):
    out = [row[:] for row in masked]
    _draw_format(out, n, ecl, mask)
    _draw_version(out, n, version)
    return out


def _build(version, ecl, codewords, mask):
    """完整矩阵(含格式/版本信息)。掩码评估应在铺格式信息前进行。"""
    n = 17 + 4 * version
    return _finalize(_build_masked(version, ecl, codewords, mask),
                     n, version, ecl, mask)


def encode(text, ecl="M"):
    """把字符串编成 QR 模块矩阵(list[list[int]],1=黑)。自动选版本与掩码。"""
    if ecl not in _ECL_BITS:
        raise ValueError(f"未知纠错等级: {ecl}")
    data = text.encode("utf-8")
    version = _pick_version(len(data), ecl)
    codewords = _interleave(_encode_bits(data, version, ecl), version, ecl)
    n = 17 + 4 * version
    best = None
    for mask in range(8):
        masked = _build_masked(version, ecl, codewords, mask)
        pen = _penalty(masked, n)
        if best is None or pen < best[0]:
            best = (pen, mask, masked)
    _, mask, masked = best
    return _finalize(masked, n, version, ecl, mask)


# --- 渲染 --------------------------------------------------------------
def render_terminal(matrix, quiet=4, ansi=True):
    """终端渲染:每个模块占两列(近似正方),显式指定黑/白底,
    与终端配色无关(深色终端也不会反相),手机对着屏幕直接扫。

    ``ansi=False`` 时退化成纯字符("██"=黑,两空格=白),便于测试解析。
    """
    n = len(matrix)
    size = n + quiet * 2
    grid = [[0] * size for _ in range(size)]
    for r in range(n):
        for c in range(n):
            grid[r + quiet][c + quiet] = matrix[r][c]
    if ansi:
        dark, light = "\033[40m  \033[0m", "\033[47m  \033[0m"
    else:
        dark, light = "██", "  "
    lines = []
    for r in range(size):
        lines.append("".join(dark if grid[r][c] else light
                             for c in range(size)))
    return "\n".join(lines)


def render_svg(matrix, scale=8, quiet=4, dark="#0b0b0b", light="#ffffff"):
    """SVG 渲染:嵌进网页面板,手机对着屏幕即可扫码。"""
    n = len(matrix)
    dim = (n + quiet * 2) * scale
    rects = []
    for r in range(n):
        for c in range(n):
            if matrix[r][c]:
                x = (c + quiet) * scale
                y = (r + quiet) * scale
                rects.append(
                    f'<rect x="{x}" y="{y}" width="{scale}" '
                    f'height="{scale}"/>')
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{dim}" '
        f'height="{dim}" viewBox="0 0 {dim} {dim}" '
        f'shape-rendering="crispEdges">'
        f'<rect width="{dim}" height="{dim}" fill="{light}"/>'
        f'<g fill="{dark}">{"".join(rects)}</g></svg>')
