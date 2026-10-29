"""A minimal VT screen for golden tests: enough of ECMA-48 to rebuild what
herdr (ratatui over crossterm) draws: cursor addressing, erase, text, wide
characters. Colours and modes are ignored. Standard library only."""
import codecs
import re
import unicodedata

def char_width(ch):
    if unicodedata.combining(ch) or unicodedata.category(ch) in ('Mn', 'Me', 'Cf'):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1

class Screen:
    def __init__(self, cols, rows):
        self.cols, self.rows = cols, rows
        self.grid = [[' '] * cols for _ in range(rows)]
        self.x = self.y = 0
        self.saved = (0, 0)
        self.pending = ''
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')

    def lines(self):
        return [''.join(c for c in row if c is not None) for row in self.grid]

    def cells(self, row, start, end):
        """Text of cells [start, end) of a row (wide characters counted once)."""
        return ''.join(c for c in self.grid[row][start:end] if c is not None)

    def _put(self, ch):
        width = char_width(ch)
        if width == 0:
            return
        if self.x + width > self.cols:
            self.x = 0
            self.y = min(self.y + 1, self.rows - 1)
        self.grid[self.y][self.x] = ch
        if width == 2 and self.x + 1 < self.cols:
            self.grid[self.y][self.x + 1] = None
        self.x += width

    def _csi(self, params, final):
        private = params[:1] in ('?', '>', '<', '=')
        if private:
            return
        args = [int(p) if p.isdigit() else 0 for p in params.split(';')] if params else []
        def arg(i, default=1):
            return args[i] if len(args) > i and args[i] else default
        if final in 'Hf':
            self.y = min(arg(0) - 1, self.rows - 1)
            self.x = min(arg(1) - 1, self.cols - 1)
        elif final == 'G':
            self.x = min(arg(0) - 1, self.cols - 1)
        elif final == 'd':
            self.y = min(arg(0) - 1, self.rows - 1)
        elif final == 'A':
            self.y = max(0, self.y - arg(0))
        elif final == 'B':
            self.y = min(self.rows - 1, self.y + arg(0))
        elif final == 'C':
            self.x = min(self.cols - 1, self.x + arg(0))
        elif final == 'D':
            self.x = max(0, self.x - arg(0))
        elif final == 'K':
            mode = args[0] if args else 0
            start, end = {0: (self.x, self.cols), 1: (0, self.x + 1), 2: (0, self.cols)}.get(mode, (0, 0))
            for i in range(start, end):
                self.grid[self.y][i] = ' '
        elif final == 'J':
            mode = args[0] if args else 0
            if mode in (2, 3):
                self.grid = [[' '] * self.cols for _ in range(self.rows)]
            elif mode == 0:
                self._csi('0', 'K')
                for r in range(self.y + 1, self.rows):
                    self.grid[r] = [' '] * self.cols
        elif final == 'X':
            for i in range(self.x, min(self.cols, self.x + arg(0))):
                self.grid[self.y][i] = ' '

    TOKEN = re.compile(r'\x1b\[([0-9;?<>=:]*)[ -/]*([@-~])|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1bP.*?\x1b\\|\x1b_.*?\x1b\\'
                       r'|\x1b[()][0-9A-Za-z]|\x1b([78=>DEMc])|([\r\n\b\t])|(\x1b)$|([^\x1b\r\n\b\t]+)', re.S)

    def feed(self, data):
        text = self.pending + (self.decoder.decode(data) if isinstance(data, bytes) else data)
        self.pending = ''
        position = 0
        while position < len(text):
            match = self.TOKEN.match(text, position)
            if not match or (match.group(0).startswith('\x1b') and match.end() == len(text) and match.group(0) in ('\x1b', '\x1b[')):
                self.pending = text[position:]
                return
            position = match.end()
            csi_params, csi_final, esc, control, _, plain = match.groups()
            if csi_final is not None:
                self._csi(csi_params, csi_final)
            elif esc == '7':
                self.saved = (self.x, self.y)
            elif esc == '8':
                self.x, self.y = self.saved
            elif control == '\r':
                self.x = 0
            elif control == '\n':
                self.y = min(self.y + 1, self.rows - 1)
            elif control == '\b':
                self.x = max(0, self.x - 1)
            elif plain is not None:
                for ch in plain:
                    if ord(ch) >= 0x20:
                        self._put(ch)
