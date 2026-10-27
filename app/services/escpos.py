"""
ESC/POS 熱感印表機指令 (網路印表機以 TCP 9100 直接送出原始位元組)。
中文：台灣常見的熱感機為 Big5 (部分為 GB18030)；以 FS & 進入中文模式。寬度以「半形字元」計算 (中文佔 2 格)。
"""
import socket
import unicodedata

ESC, GS, FS = b'\x1b', b'\x1d', b'\x1c'


def char_width(ch):
    return 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1


def text_width(text):
    return sum(char_width(c) for c in text)


def fit(text, width):
    """截斷到指定寬度 (不切半個中文字)"""
    out, used = [], 0
    for c in text:
        w = char_width(c)
        if used + w > width:
            break
        out.append(c)
        used += w
    return ''.join(out)


def wrap(text, width):
    lines, current, used = [], [], 0
    for c in str(text):
        w = char_width(c)
        if c == '\n' or used + w > width:
            lines.append(''.join(current))
            current, used = [], 0
            if c == '\n':
                continue
        current.append(c)
        used += w
    if current:
        lines.append(''.join(current))
    return lines or ['']


class Ticket:
    def __init__(self, paper_width=80, encoding='big5'):
        self.width = 32 if int(paper_width or 80) <= 58 else 48
        self.encoding = (encoding or 'big5').lower()
        self.buf = bytearray(ESC + b'@')
        if self.encoding in ('big5', 'gb18030', 'gbk'):
            self.buf += FS + b'&'

    def _enc(self, text):
        enc = 'utf-8' if self.encoding == 'utf8' else self.encoding
        return str(text).encode(enc, errors='replace')

    def align(self, where='left'):
        self.buf += ESC + b'a' + bytes([{'left': 0, 'center': 1, 'right': 2}[where]])
        return self

    def style(self, bold=False, size=1):
        self.buf += ESC + b'E' + bytes([1 if bold else 0])
        n = {1: 0x00, 2: 0x11, 3: 0x22}.get(size, 0)
        self.buf += GS + b'!' + bytes([n])
        return self

    def text(self, value, bold=False, size=1, align='left'):
        self.align(align).style(bold, size)
        width = max(8, self.width // max(1, size))
        for line in wrap(value, width):
            self.buf += self._enc(line) + b'\n'
        return self.style().align()

    def row(self, left, right, bold=False):
        """左右對齊的一行 (左邊過長時換行)"""
        self.style(bold)
        right = str(right)
        room = self.width - text_width(right) - 1
        left_lines = wrap(left, max(8, room))
        for i, piece in enumerate(left_lines):
            if i == len(left_lines) - 1:
                pad = self.width - text_width(piece) - text_width(right)
                self.buf += self._enc(piece + ' ' * max(1, pad) + right) + b'\n'
            else:
                self.buf += self._enc(piece) + b'\n'
        return self.style()

    def rule(self, ch='-'):
        self.buf += self._enc(ch * self.width) + b'\n'
        return self

    def feed(self, lines=1):
        self.buf += ESC + b'd' + bytes([max(0, min(10, lines))])
        return self

    def qr(self, data, size=6):
        """QR code (GS ( k，大多數 ESC/POS 熱感機支援)"""
        payload = str(data).encode('utf-8')
        self.align('center')
        self.buf += GS + b'(k' + bytes([4, 0, 49, 65, 50, 0])           # 型號 2
        self.buf += GS + b'(k' + bytes([3, 0, 49, 67, max(1, min(16, size))])
        self.buf += GS + b'(k' + bytes([3, 0, 49, 69, 49])               # 容錯 M
        n = len(payload) + 3
        self.buf += GS + b'(k' + bytes([n % 256, n // 256, 49, 80, 48]) + payload
        self.buf += GS + b'(k' + bytes([3, 0, 49, 81, 48])
        self.buf += b'\n'
        return self.align()

    def drawer(self, pin=0):
        """開錢櫃 (ESC p m t1 t2)：接在印表機 RJ11 的錢櫃送出 50ms / 500ms 脈衝 (pin 0 = 第 2 腳，1 = 第 5 腳)"""
        self.buf += ESC + b'p' + bytes([1 if pin else 0, 25, 250])
        return self

    def beep(self):
        self.buf += ESC + b'B' + bytes([2, 2])
        return self

    def cut(self):
        self.feed(3)
        self.buf += GS + b'V' + bytes([66, 0])
        return self

    def bytes(self):
        return bytes(self.buf)


def send_raw(host, port, data, timeout=5):
    """送出原始位元組到網路印表機 (測試時以 monkeypatch 取代)"""
    with socket.create_connection((host, int(port or 9100)), timeout=timeout) as sock:
        sock.sendall(data)
