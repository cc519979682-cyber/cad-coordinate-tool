"""Native AutoCAD point picking and a strict, append-only session protocol.

The generated program does not run on load. ``cgp-begin``, ``cgp-accept``
(WCS point, WCS label elbow), ``cgp-undo`` and ``cgp-finish`` are also usable
without mouse input, so the exact production geometry/protocol can be checked
inside an isolated native CAD drawing. All graphics belong to modelspace;
no UCS, object-snap, security, or existing layer setting is changed.
"""
from __future__ import annotations

import math
from pathlib import Path
import re
import uuid


_ERROR_MESSAGES = {
    "Please activate modelspace before picking.": "请先切换到模型空间，再开始取点。",
    "Unable to create picker layer or text style.": "无法创建取点图层或文字样式，请检查当前图纸。",
    "Unable to draw point annotation.": "无法绘制本次坐标标注，已保留之前确认的坐标。",
    "Unable to create point group.": "无法创建本次坐标标注组，已保留之前确认的坐标。",
    "Native picking failed; existing accepted points were preserved.": "取点发生错误，已保留之前确认的坐标，请检查当前图纸后重试。",
    "Point commit could not be verified; drawing and journal preserved.": "坐标提交状态无法核对，图形和原始记录已保留，请恢复取点记录后检查。",
}

_CANCEL_MESSAGES = (
    "FUNCTION CANCELLED", "FUNCTION CANCELED", "QUIT / EXIT ABORT",
    "QUIT/EXIT ABORT", "CONSOLE BREAK", "函数已取消",
)


def _road_name(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("道路名称必须是文本。")
    value = value.strip()
    if not 1 <= len(value) <= 100 or any(
        ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF for c in value
    ):
        raise ValueError("道路名称应为 1–100 个字符，不能包含控制字符。")
    return value


def parse_event(line: str) -> dict:
    """Parse one complete journal record; incomplete lines belong to the reader.

    The journal contains ASCII only. Point names are reconstructed from the
    session road name and integer index, avoiding CSV/Unicode ambiguity.
    """
    if not isinstance(line, str) or not line.isascii():
        raise ValueError("取点记录必须是 ASCII 文本。")
    line = line.removesuffix("\n").removesuffix("\r")
    if not line or any(ord(c) < 32 and c != "\t" for c in line):
        raise ValueError("取点记录含空行或非法控制字符。")
    fields = line.split("\t")
    kind = fields[0]
    if kind in {"READY", "DONE"} and len(fields) == 1:
        return {"type": kind}
    if kind == "ERROR" and len(fields) == 2 and fields[1]:
        return {"type": kind, "message": _ERROR_MESSAGES.get(fields[1], fields[1])}

    def positive_int(value):
        if not re.fullmatch(r"[1-9][0-9]*", value):
            raise ValueError("取点记录序号必须是正整数。")
        number = int(value)
        if number > 2_147_483_647:
            raise ValueError("取点记录序号超出 CAD 支持范围。")
        return number

    if kind == "UNDO" and len(fields) == 2:
        return {"type": kind, "serial": positive_int(fields[1])}
    if kind == "ROAD" and len(fields) == 4:
        codes = fields[3].split(",")
        if not 1 <= len(codes) <= 100:
            raise ValueError("道路切换记录名称长度不正确。")
        values = [positive_int(code) for code in codes]
        if any(code > 0x10FFFF or 0xD800 <= code <= 0xDFFF for code in values):
            raise ValueError("道路切换记录包含无效 Unicode 码点。")
        road = "".join(map(chr, values))
        if _road_name(road) != road:
            raise ValueError("道路切换记录名称不能包含首尾空白。")
        return {"type": kind, "road_id": positive_int(fields[1]),
                "start": positive_int(fields[2]), "road": road}
    if kind == "ADD" and len(fields) in {11, 12}:
        result = {"type": kind, "serial": positive_int(fields[1]),
                  "index": positive_int(fields[2])}
        if len(fields) == 12:
            result["road_id"] = 0 if fields[11] == "0" else positive_int(fields[11])
        keys = ("x", "y", "z", "lx", "ly", "hx", "height", "arrow")
        for key, value in zip(keys, fields[3:]):
            if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value):
                raise ValueError("取点记录含无效数值。")
            number = float(value)
            if not math.isfinite(number):
                raise ValueError("取点记录坐标必须是有限数值。")
            if key in {"height", "arrow"} and number <= 0:
                raise ValueError("取点文字与箭头尺寸必须大于零。")
            result[key] = number
        return result
    raise ValueError("取点记录类型或字段数量不正确。")


def _lisp_string(value: str) -> str:
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("CAD 配置不能包含换行或控制字符。")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def picker_expression(source: str, *, auto_start: bool = True) -> str:
    """Return one command-line expression, preserving strings and comments.

    Used by native ``Document.SendCommand``; never changes trusted paths or
    SECURELOAD. It can also be inserted in a Core Console verification script.
    """
    output = []
    quoted = escaped = comment = False
    depth = 0
    for char in source:
        if comment:
            if char in "\r\n":
                comment = False
                if output and output[-1] != " ":
                    output.append(" ")
            continue
        if quoted:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == ";":
            comment = True
        elif char == '"':
            quoted = True
            output.append(char)
        elif char.isspace():
            if output and output[-1] != " ":
                output.append(" ")
        else:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth < 0:
                    raise ValueError("CAD 取点程序括号不匹配。")
            output.append(char)
    if quoted or depth or not "".join(output).strip():
        raise ValueError("CAD 取点程序不完整。")
    suffix = " (c:CGPICK)" if auto_start else ""
    return "(progn " + "".join(output).strip() + suffix + ")\n"


def picker_commands(source: str, *, auto_start: bool = True) -> list[str]:
    """Split only between complete top-level forms for native command buffers.

    Each returned command ends with a newline. In particular no road/path
    string is interpreted as a command separator, even when it has quotes,
    parentheses, semicolons, or backslashes. The last command starts picking.
    """
    compact = picker_expression(source, auto_start=False)[7:-2]
    commands = []
    quoted = escaped = False
    depth = begin = 0
    for index, char in enumerate(compact):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "(":
            if depth == 0:
                begin = index
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                commands.append(compact[begin:index + 1] + "\n")
        elif depth == 0 and not char.isspace():
            raise ValueError("CAD 取点命令必须由完整表达式组成。")
    if auto_start:
        commands.append("(c:CGPICK)\n")
    return commands


def generate_picker_lsp(event_path: Path, stop_path: Path, road: str, start: int,
                        scale: float, offset_e: float, offset_n: float,
                        height: float, arrow_factor: float = 1, *,
                        road_numbers: dict[str, int] | None = None,
                        bridge_dir: Path | None = None,
                        bridge_token: str | None = None) -> str:
    """Create a Unicode AutoLISP source program (AutoCAD 2021+ Unicode engine).

    Paths are private per-session files. X/Y/Z in ADD are unrounded WCS CAD
    coordinates; the UI applies captured unit/offset settings. Label and arrow
    dimensions are also in CAD units. The caller owns session lifecycle.
    """
    road = _road_name(road)
    if not isinstance(start, int) or isinstance(start, bool) or not 1 <= start < 2_147_483_647:
        raise ValueError("起始编号必须是有效正整数。")
    values = dict(scale=scale, offset_e=offset_e, offset_n=offset_n,
                  height=height, arrow_factor=arrow_factor)
    for key, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
            raise ValueError("取点比例、偏移和尺寸必须是有限数值。")
        if key in {"scale", "height", "arrow_factor"} and value <= 0:
            raise ValueError("取点比例与尺寸必须大于零。")
    event = str(Path(event_path).resolve()).replace("\\", "/")
    stop = str(Path(stop_path).resolve()).replace("\\", "/")
    if event.casefold() == stop.casefold():
        raise ValueError("取点事件和停止信号不能使用同一文件。")
    if (bridge_dir is None) != (bridge_token is None):
        raise ValueError("CAD 续接桥目录和标识必须同时提供。")
    if bridge_token is not None and (not isinstance(bridge_token, str)
            or not bridge_token or not bridge_token.isascii()
            or any(ord(c) <= 32 or ord(c) >= 127 for c in bridge_token)):
        raise ValueError("CAD 续接桥标识必须是不含空白或控制字符的 ASCII 文本。")
    bridge_path = (str(Path(bridge_dir).resolve()).replace("\\", "/").rstrip("/")
                   if bridge_dir is not None else None)
    if road_numbers is not None and not isinstance(road_numbers, dict):
        raise ValueError("道路起始编号必须是名称和编号字典。")
    baselines = {}
    for name, number in (road_numbers or {}).items():
        name = _road_name(name)
        if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= 2_147_483_648:
            raise ValueError("道路起始编号必须是有效正整数。")
        prior = baselines.get(name.casefold())
        baselines[name.casefold()] = (prior[0] if prior else name, max(number, prior[1] if prior else 1))
    prior = baselines.get(road.casefold())
    baselines[road.casefold()] = (prior[0] if prior else road, max(start, prior[1] if prior else 1))
    # Each road is a separate expression, avoiding the native command reader's
    # length limit when the project already contains many roads.
    number_forms = "\n".join(
        "(setq *cgp-base-numbers* (cons (list " + _lisp_string(name) + " " +
        (str(number) if number <= 2_147_483_647 else "2147483648.0") +
        ") *cgp-base-numbers*))" for name, number in reversed(list(baselines.values()))
    )
    config = {
        "EVENT": _lisp_string(event), "STOP": _lisp_string(stop),
        "ROAD": _lisp_string(road.strip()), "START": str(start),
        "SCALE": repr(float(scale)), "OE": repr(float(offset_e)),
        "ON": repr(float(offset_n)), "HEIGHT": repr(float(height)),
        "FACTOR": repr(float(min(3, max(.2, arrow_factor)))),
        "PREFIX": _lisp_string("CGP_" + uuid.uuid4().hex[:16].upper()),
        "ROADNUMBERS": number_forms,
        "ZBDIR": _lisp_string(bridge_path) if bridge_path is not None else "nil",
        "ZBTOKEN": _lisp_string(bridge_token) if bridge_token is not None else "nil",
        "ERRORMESSAGES": "(" + " ".join(
            "(" + _lisp_string(code) + " . " + _lisp_string(message) + ")"
            for code, message in _ERROR_MESSAGES.items()) + ")",
        "CANCELMESSAGES": "(" + " ".join(map(_lisp_string, _CANCEL_MESSAGES)) + ")",
    }
    return re.sub(r"@@([A-Z]+)@@", lambda match: config[match[1]], _PROGRAM)


_PROGRAM = r'''; Coordinate Generator native picker. Loading only defines helpers.
(vl-load-com)
(setq *cgp-event* @@EVENT@@ *cgp-stop* @@STOP@@ *cgp-road* @@ROAD@@
      *cgp-start* @@START@@ *cgp-scale* @@SCALE@@ *cgp-oe* @@OE@@
      *cgp-on* @@ON@@ *cgp-height* @@HEIGHT@@ *cgp-factor* @@FACTOR@@
      *cgp-prefix* @@PREFIX@@ *cgp-active* nil *cgp-preview* nil
      *cgp-file* nil *cgp-abort* nil *cgp-done* nil *cgp-failed* nil *cgp-base-numbers* nil
      *cgp-transaction* nil *cgp-reconcile-reported* nil *cgp-error-recorded* nil
      *cgp-zb-dir* @@ZBDIR@@ *cgp-zb-token* @@ZBTOKEN@@
      *cgp-zb-file* nil *cgp-zb-own-temp* nil)
@@ROADNUMBERS@@

(defun cgp-clean-preview (/ en)
  (foreach en *cgp-preview* (if (and en (entget en)) (entdel en)))
  (setq *cgp-preview* nil))

(defun cgp-write (row)
  (if (not (setq *cgp-file* (open *cgp-event* "a")))
    (progn (setq *cgp-abort* T) nil)
    (progn (write-line row *cgp-file*) (close *cgp-file*)
           (setq *cgp-file* nil) T)))

(defun cgp-error-text (message / text)
  (setq text (cdr (assoc message '@@ERRORMESSAGES@@)))
  (if text text "取点发生错误，已保留之前确认的坐标，请在生成器中查看详情。"))

(defun cgp-emit (row / result)
  (if (equal (substr row 1 6) "ERROR\t")
    (progn (setq *cgp-failed* T) (prompt (strcat "\n" (cgp-error-text (substr row 7))))))
  (setq result (vl-catch-all-apply 'cgp-write (list row)))
  (if *cgp-file*
    (progn (vl-catch-all-apply 'close (list *cgp-file*)) (setq *cgp-file* nil)))
  (if (or (vl-catch-all-error-p result) (not result))
    (progn (setq *cgp-abort* T)
      (if (and (vl-catch-all-error-p result)
               (cgp-cancelled-p (vl-catch-all-error-message result)))
        (prompt "\n已取消，正在核对最后一条坐标记录。")
        (progn (setq *cgp-failed* T)
          (prompt "\n无法写入坐标回传文件，取点已停止。请检查生成器连接与工作目录。"))) nil)
    (progn (if (equal (substr row 1 6) "ERROR\t") (setq *cgp-error-recorded* T)) T)))

(defun cgp-num (v) (rtos v 2 16))
(defun cgp-dec3 (v / result dot)
  (setq result (rtos v 2 3))
  (if (= (substr result 1 1) ".") (setq result (strcat "0" result)))
  (if (= (substr result 1 2) "-.") (setq result (strcat "-0" (substr result 2))))
  (setq dot (vl-string-search "." result))
  (if (not dot) (setq result (strcat result ".") dot (1- (strlen result))))
  (while (< (- (strlen result) dot 1) 3) (setq result (strcat result "0")))
  result)
(defun cgp-name ()
  (strcat *cgp-road* "-" (if (< *cgp-index* 10) "0" "") (itoa *cgp-index*)))
(defun cgp-stopped () (or *cgp-abort* (findfile *cgp-stop*)))

(defun cgp-codepoints (txt / units code low out)
  (setq units (vl-string->list txt) out nil)
  (while units
    (setq code (car units) units (cdr units))
    (if (and (>= code 55296) (<= code 56319) units
             (>= (car units) 56320) (<= (car units) 57343))
      (setq low (car units) units (cdr units)
            code (+ 65536 (* (- code 55296) 1024) (- low 56320))))
    (setq out (cons code out)))
  (reverse out))

(defun cgp-trim-road (name)
  (vl-string-trim (vl-list->string '(32 160 5760 8192 8193 8194 8195 8196 8197
      8198 8199 8200 8201 8202 8232 8233 8239 8287 12288)) name))

(defun cgp-valid-road (name / codes valid code)
  (setq codes (cgp-codepoints name) valid (and codes (<= (length codes) 100)))
  (foreach code codes
    (if (or (< code 32) (and (>= code 127) (<= code 159))
            (and (>= code 55296) (<= code 57343)) (> code 1114111))
      (setq valid nil)))
  valid)

(defun cgp-road-entry (name / result entry)
  (foreach entry *cgp-road-table*
    (if (equal (strcase (car entry)) (strcase name)) (setq result entry)))
  result)

(defun cgp-next-index (name / entry number record key)
  (setq entry (cgp-road-entry name) number (if entry (cadr entry) 1) key (strcase name))
  (foreach record *cgp-history*
    (if (equal (nth 4 record) key) (setq number (max number (1+ (cadr record))))))
  number)

(defun cgp-road-code-string (name / result code)
  (setq result "")
  (foreach code (cgp-codepoints name)
    (setq result (strcat result (if (= result "") "" ",") (itoa code))))
  result)

(defun cgp-switch-road (name / entry canonical number next-id)
  (if (and *cgp-active* (not (cgp-stopped)) (eq (type name) 'STR))
    (progn
      (setq name (cgp-trim-road name))
      (cond
        ((= name "") (prompt "\n已取消换路，仍保留当前道路。") nil)
        ((not (cgp-valid-road name))
          (prompt "\n道路名称应为 1-100 个字符，不能包含控制字符。") nil)
        (T
          (setq entry (cgp-road-entry name) canonical (if entry (car entry) name)
                number (cgp-next-index canonical) next-id (1+ *cgp-road-id*))
          (if (> number 2147483647)
            (progn (prompt "\n本道路编号已达到上限，请使用新的道路名称。") nil)
          (if (cgp-emit (strcat "ROAD\t" (itoa next-id) "\t" (itoa number) "\t"
                              (cgp-road-code-string canonical)))
            (progn
              (cgp-clean-preview)
              (if (not entry) (setq *cgp-road-table* (cons (list canonical 1) *cgp-road-table*)))
              (setq *cgp-road* canonical *cgp-index* number *cgp-road-id* next-id *cgp-stack* nil)
              (prompt (strcat "\n已切换道路，下一点为 " (cgp-name) "。")) T)
            nil)))))
    nil))

(defun cgp-with-pointer (reader arguments / cursor result restored)
  (setq cursor (getvar "CURSORTYPE")
        result (vl-catch-all-apply
          '(lambda () (setvar "CURSORTYPE" 1) (apply reader arguments)) nil)
        restored (vl-catch-all-apply 'setvar (list "CURSORTYPE" cursor)))
  (if (vl-catch-all-error-p restored) restored result))

(defun cgp-input-road (message)
  (cgp-with-pointer '(lambda () (getstring T message)) nil))

(defun cgp-get-road-name () (cgp-input-road "\n请输入新道路名称（回车或 Esc 取消）："))
(defun cgp-new-road (/ result)
  (setq result (vl-catch-all-apply 'cgp-get-road-name nil))
  (if (or (vl-catch-all-error-p result) (null result))
    (progn (prompt "\n已取消换路，仍保留当前道路。") nil)
    (cgp-switch-road result)))

(defun cgp-begin (/ suffix)
  (setq *cgp-index* *cgp-start* *cgp-serial* 0 *cgp-stack* nil
        *cgp-preview* nil *cgp-abort* nil *cgp-done* nil *cgp-failed* nil *cgp-active* nil
        *cgp-road-id* 0 *cgp-history* nil *cgp-road-table* *cgp-base-numbers*)
  (if (= (getvar "CVPORT") 1)
    (progn (cgp-emit "ERROR\tPlease activate modelspace before picking.") nil)
    (progn
      (setq *cgp-owner* (cdr (assoc 330 (entget (tblobjname "BLOCK" "*Model_Space"))))
            *cgp-layer* *cgp-prefix* suffix 0)
      (while (tblsearch "LAYER" *cgp-layer*)
        (setq suffix (1+ suffix) *cgp-layer* (strcat *cgp-prefix* "_" (itoa suffix))))
      (setq *cgp-style* (strcat *cgp-layer* "_TEXT") suffix 0)
      (while (tblsearch "STYLE" *cgp-style*)
        (setq suffix (1+ suffix) *cgp-style* (strcat *cgp-layer* "_TEXT_" (itoa suffix))))
      (if (and *cgp-owner* (entmake (list '(0 . "LAYER") '(100 . "AcDbSymbolTableRecord")
              '(100 . "AcDbLayerTableRecord") (cons 2 *cgp-layer*) '(70 . 0) '(62 . 3)))
              (entmake (list '(0 . "STYLE") '(100 . "AcDbSymbolTableRecord")
                '(100 . "AcDbTextStyleTableRecord") (cons 2 *cgp-style*) '(70 . 0)
                '(40 . 0.0) '(41 . 1.0) '(50 . 0.0) '(71 . 0) '(42 . 2.5)
                '(3 . "simhei.ttf") '(4 . ""))))
        (progn (setq *cgp-active* T) (if (cgp-emit "READY") T (setq *cgp-active* nil)))
        (progn (cgp-emit "ERROR\tUnable to create picker layer or text style.") nil)))))

(defun cgp-ent (data / en)
  (setq en (entmakex (append (list (car data) (cons 330 *cgp-owner*)
         '(100 . "AcDbEntity") (cons 8 *cgp-layer*) '(62 . 2) '(67 . 0)
         '(410 . "Model")) (cdr data))))
  (if en (setq *cgp-preview* (cons en *cgp-preview*)))
  en)

(defun cgp-line (a b)
  (cgp-ent (list '(0 . "LINE") '(100 . "AcDbLine") (cons 10 a) (cons 11 b))))

(defun cgp-text (txt anchor right / definition)
  (setq definition (list '(0 . "TEXT") '(100 . "AcDbText") (cons 10 anchor)
     (cons 40 *cgp-height*) (cons 1 txt) '(50 . 0.0) '(41 . 1.0) '(51 . 0.0)
     (cons 7 *cgp-style*) '(71 . 0) (cons 72 (if right 0 2))
     (cons 11 anchor) '(210 0.0 0.0 1.0) '(100 . "AcDbText") '(73 . 0)))
  (cgp-ent definition))

(defun cgp-width (txt / box)
  (setq box (textbox (list '(0 . "TEXT") (cons 1 txt) (cons 40 *cgp-height*)
                    (cons 7 *cgp-style*) '(41 . 1.0) '(51 . 0.0))))
  (if box (abs (- (car (cadr box)) (caar box))) (* (strlen txt) *cgp-height* 0.7)))

(defun cgp-draw (pt elbow / z p q right sign text1 text2 text3 width hx ang wing a b)
  (cgp-clean-preview)
  (setq z (if (caddr pt) (caddr pt) 0.0) p (list (car pt) (cadr pt) z)
        q (list (car elbow) (cadr elbow) z) right (>= (car q) (car p))
        sign (if right 1.0 -1.0) text1 (cgp-name)
        text2 (strcat "E=" (cgp-dec3 (- (/ (car p) *cgp-scale*) *cgp-oe*)))
        text3 (strcat "N=" (cgp-dec3 (- (/ (cadr p) *cgp-scale*) *cgp-on*)))
        width (max (* 2.0 *cgp-height*) (cgp-width text1) (cgp-width text2) (cgp-width text3))
        hx (+ (car q) (* sign (+ width (* 0.5 *cgp-height*))))
        ang (atan (- (cadr q) (cadr p)) (- (car q) (car p)))
        wing (* 0.5 *cgp-height* *cgp-factor*)
        a (list (+ (car p) (* wing (cos (+ ang (/ pi 6.0)))))
                (+ (cadr p) (* wing (sin (+ ang (/ pi 6.0))))))
        b (list (+ (car p) (* wing (cos (- ang (/ pi 6.0)))))
                (+ (cadr p) (* wing (sin (- ang (/ pi 6.0)))))))
  (if (and
    (cgp-ent (list '(0 . "LWPOLYLINE") '(100 . "AcDbPolyline") '(90 . 3) '(70 . 0)
                   (cons 38 z) (cons 10 a) (cons 10 (list (car p) (cadr p)))
                   (cons 10 b) '(210 0.0 0.0 1.0)))
    (cgp-line p q) (cgp-line q (list hx (cadr q) z))
    (cgp-text text1 (list (car q) (+ (cadr q) (* 0.3 *cgp-height*)) z) right)
    (cgp-text text2 (list (car q) (- (cadr q) (* 1.2 *cgp-height*)) z) right)
    (cgp-text text3 (list (car q) (- (cadr q) (* 2.7 *cgp-height*)) z) right))
      (list p q hx wing)
      (progn (cgp-clean-preview) (setq *cgp-abort* T)
             (cgp-emit "ERROR\tUnable to draw point annotation.") nil)))

(defun cgp-group (entities serial / dict group name)
  (setq dict (cdr (assoc -1 (dictsearch (namedobjdict) "ACAD_GROUP")))
        name (strcat *cgp-layer* "_" (itoa serial)))
  (if dict
    (progn
      (setq group (entmakex (append (list '(0 . "GROUP") '(100 . "AcDbGroup")
                 '(300 . "Coordinate Generator") '(70 . 0) '(71 . 1))
                  (mapcar '(lambda (en) (cons 340 en)) entities))))
      (if (and group (dictadd dict name group)) (list dict name group)
        (progn (if group (entdel group)) nil)))
    nil))

(defun cgp-delete-group (record / en)
  (if (nth 3 record)
    (progn (dictremove (car (nth 3 record)) (cadr (nth 3 record)))
           (if (entget (caddr (nth 3 record))) (entdel (caddr (nth 3 record))))))
  (foreach en (nth 2 record) (if (entget en) (entdel en))))

(defun cgp-record-add (record)
  (if (not (assoc (car record) *cgp-stack*)) (setq *cgp-stack* (cons record *cgp-stack*)))
  (if (not (assoc (car record) *cgp-history*)) (setq *cgp-history* (cons record *cgp-history*)))
  (setq *cgp-serial* (max *cgp-serial* (car record))
        *cgp-index* (1+ (cadr record)) *cgp-preview* nil))

(defun cgp-record-undo (record)
  (cgp-delete-group record)
  (setq *cgp-stack* (vl-remove record *cgp-stack*) *cgp-index* (cadr record)
        *cgp-history* (vl-remove record *cgp-history*)))

(defun cgp-row-written (row / code line found)
  (if (not (setq *cgp-file* (open *cgp-event* "r"))) 'UNREADABLE
    (progn
      (setq line "")
      (while (and (not found) (setq code (read-char *cgp-file*)))
        (cond ((= code 10) (setq found (equal (vl-string-right-trim "\r" line) row) line ""))
              (T (setq line (strcat line (chr code))))))
      (close *cgp-file*) (setq *cgp-file* nil) found)))

(defun cgp-resolve-transaction (/ record written)
  (setq record (cadr *cgp-transaction*) written (cgp-row-written (caddr *cgp-transaction*)))
  (if (eq written 'UNREADABLE) nil
    (progn
      (if (eq (car *cgp-transaction*) 'ADD)
        (if written (cgp-record-add record)
          (progn (cgp-delete-group record) (setq *cgp-preview* nil)))
        (if written (cgp-record-undo record)))
      (setq *cgp-transaction* nil) T)))

(defun cgp-recover-transaction (/ result)
  (if *cgp-file*
    (progn (vl-catch-all-apply 'close (list *cgp-file*)) (setq *cgp-file* nil)))
  (if (null *cgp-transaction*) T
    (progn
      (setq result (vl-catch-all-apply 'cgp-resolve-transaction nil))
      (if *cgp-file*
        (progn (vl-catch-all-apply 'close (list *cgp-file*)) (setq *cgp-file* nil)))
      (if (and (not (vl-catch-all-error-p result)) result) T
        (progn
          (setq *cgp-failed* T *cgp-abort* T *cgp-preview* nil)
          (if (not *cgp-reconcile-reported*)
            (progn (setq *cgp-reconcile-reported* T)
              (cgp-emit "ERROR\tPoint commit could not be verified; drawing and journal preserved.")))
          nil)))))

(defun cgp-accept (pt elbow / geometry p q hx wing serial group row en data record)
  (if (and *cgp-active* (not (cgp-stopped)) (setq geometry (cgp-draw pt elbow)))
    (progn
      (setq p (nth 0 geometry) q (nth 1 geometry) hx (nth 2 geometry)
            wing (nth 3 geometry) serial (1+ *cgp-serial*)
            group (cgp-group *cgp-preview* serial))
      (if group
        (progn
          (setq record (list serial *cgp-index* *cgp-preview* group (strcase *cgp-road*))
                row (strcat "ADD\t" (itoa serial) "\t" (itoa *cgp-index*) "\t"
                  (cgp-num (car p)) "\t" (cgp-num (cadr p)) "\t" (cgp-num (caddr p)) "\t"
                  (cgp-num (car q)) "\t" (cgp-num (cadr q)) "\t" (cgp-num hx) "\t"
                  (cgp-num *cgp-height*) "\t" (cgp-num wing) "\t" (itoa *cgp-road-id*))
                *cgp-transaction* (list 'ADD record row))
          (foreach en *cgp-preview*
            (setq data (entget en))
            (entmod (subst '(62 . 256) (assoc 62 data) data)) (entupd en))
          (if (cgp-emit row)
            (progn
              (cgp-record-add record) (setq *cgp-transaction* nil) serial)
            (progn (cgp-recover-transaction) nil)))
        (progn (cgp-clean-preview) (setq *cgp-abort* T)
          (cgp-emit "ERROR\tUnable to create point group.") nil)))
    nil))

(defun cgp-undo (/ record row)
  (if (and *cgp-active* (not (cgp-stopped)) *cgp-stack*)
    (progn (setq record (car *cgp-stack*) row (strcat "UNDO\t" (itoa (car record)))
                 *cgp-transaction* (list 'UNDO record row))
      (if (cgp-emit row)
        (progn (cgp-record-undo record) (setq *cgp-transaction* nil) T)
        (progn (cgp-recover-transaction) nil)))
    (progn (prompt "\n当前道路没有可撤销的已确认坐标点。") nil)))

(defun cgp-finish ()
  (cgp-recover-transaction)
  (cgp-clean-preview)
  (if (and *cgp-active* (not *cgp-done*))
    (progn
      (if (and *cgp-failed* (not *cgp-error-recorded*))
        (cgp-emit "ERROR\tNative picking failed; existing accepted points were preserved."))
      (cgp-emit "DONE") (setq *cgp-done* T)
      (if *cgp-failed*
        (prompt "\n本轮取点发生错误，请在生成器中查看原因并重新启动 CAD 取点。")
        (prompt "\n本轮取点已结束。继续取点请输入 ZB，然后输入道路名称（保持生成器打开）。"))))
  (setq *cgp-active* nil)
  (princ))

(defun cgp-place (pt / cursor next-cursor event kind key result)
  (setq cursor (list (+ (car pt) (* 5.0 *cgp-height*))
                    (+ (cadr pt) (* 5.0 *cgp-height*)) (caddr pt)))
  (cgp-draw pt cursor)
  (prompt "\n移动鼠标放置标注；左键或空格确认；[ 缩小箭头，] 放大箭头；Esc 或右键取消本点。")
  (while (and (not result) (not (cgp-stopped)))
    (setq event (grread T 15 0) kind (car event))
    (cond
      ((= kind 5)
        (setq next-cursor (trans (cadr event) 1 0))
        (if (not (equal next-cursor cursor))
          (progn (setq cursor next-cursor) (cgp-draw pt cursor))))
      ((= kind 3) (setq cursor (trans (cadr event) 1 0) result 'ACCEPT))
      ((= kind 2)
        (setq key (cadr event))
        (cond ((or (= key 32) (= key 13)) (setq result 'ACCEPT))
              ((= key 27) (setq result 'CANCEL))
              ((= key 91) (setq *cgp-factor* (max 0.2 (- *cgp-factor* 0.1))) (cgp-draw pt cursor))
              ((= key 93) (setq *cgp-factor* (min 3.0 (+ *cgp-factor* 0.1))) (cgp-draw pt cursor))))
      ((or (= kind 11) (= kind 25)) (setq result 'CANCEL))))
  (if (and (eq result 'ACCEPT) (not (cgp-stopped))) (cgp-accept pt cursor) (cgp-clean-preview)))

(defun cgp-road-menu-loop (/ choice resume)
  (prompt (strcat "\n道路 " *cgp-road* " 已结束。N/回车：换道路；C：继续本路；Q：结束全部取点。"))
  (while (and (not resume) (not (cgp-stopped)))
    (initget "New Continue Quit 换路 继续 结束")
    (setq choice (getkword "\n请选择 [换路(N)/继续(C)/结束(Q)] <换路>："))
    (if (not (cgp-stopped)) (cond
      ((or (null choice) (member choice '("New" "换路")))
        (if (cgp-new-road) (setq resume T)))
      ((member choice '("Continue" "继续")) (setq resume T))
      ((member choice '("Quit" "结束")) (setq *cgp-abort* T)))))
  resume)

(defun cgp-road-menu (/ result)
  (setq result (cgp-with-pointer 'cgp-road-menu-loop nil))
  (if (vl-catch-all-error-p result)
    (progn
      (setq *cgp-abort* T)
      (if (not (cgp-cancelled-p (vl-catch-all-error-message result)))
        (cgp-emit "ERROR\tNative picking failed; existing accepted points were preserved."))
      nil)
    result))

(defun cgp-zb-path (name) (strcat *cgp-zb-dir* "/" name))

(defun cgp-zb-cleanup ()
  (if *cgp-zb-file*
    (progn (vl-catch-all-apply 'close (list *cgp-zb-file*)) (setq *cgp-zb-file* nil)))
  (if (and *cgp-zb-own-temp* *cgp-zb-dir*)
    (vl-catch-all-apply 'vl-file-delete (list (cgp-zb-path "zb.request.tmp"))))
  (setq *cgp-zb-own-temp* nil))

(defun cgp-zb-expiry-valid (stamp / valid position code)
  (setq valid (and (eq (type stamp) 'STR) (= (strlen stamp) 15)) position 0)
  (if valid
    (progn
      (foreach code (vl-string->list stamp)
        (if (if (= position 8) (/= code 46) (or (< code 48) (> code 57))) (setq valid nil))
        (setq position (1+ position)))
      (and valid
           (<= 1 (atoi (substr stamp 5 2)) 12) (<= 1 (atoi (substr stamp 7 2)) 31)
           (< (atoi (substr stamp 10 2)) 24) (< (atoi (substr stamp 12 2)) 60)
           (< (atoi (substr stamp 14 2)) 60) (<= (getvar "CDATE") (atof stamp))))))

(defun cgp-zb-read-ready (/ marker token expires extra)
  (if (setq *cgp-zb-file* (open (cgp-zb-path "zb.ready") "r"))
    (progn
      (setq marker (read-line *cgp-zb-file*) token (read-line *cgp-zb-file*)
            expires (read-line *cgp-zb-file*) extra (read-line *cgp-zb-file*))
      (close *cgp-zb-file*) (setq *cgp-zb-file* nil)
      (and (equal marker "CGP_ZB_V1") (equal token *cgp-zb-token*)
           (null extra) (cgp-zb-expiry-valid expires)))))

(defun cgp-zb-ready-p (/ result)
  (if (and *cgp-zb-dir* *cgp-zb-token*)
    (progn
      (setq result (vl-catch-all-apply 'cgp-zb-read-ready nil))
      (if *cgp-zb-file*
        (progn (vl-catch-all-apply 'close (list *cgp-zb-file*)) (setq *cgp-zb-file* nil)))
      (and (not (vl-catch-all-error-p result)) result))))

(defun cgp-zb-pending-p ()
  (or (findfile (cgp-zb-path "zb.request")) (findfile (cgp-zb-path "zb.request.tmp"))))

(defun cgp-zb-write-request (name / target temporary)
  (setq target (cgp-zb-path "zb.request") temporary (cgp-zb-path "zb.request.tmp"))
  (cond
    ((cgp-zb-pending-p) 'BUSY)
    ((not (cgp-zb-ready-p)) 'NOTREADY)
    ((not (setq *cgp-zb-file* (open temporary "w"))) 'ERROR)
    (T
      (setq *cgp-zb-own-temp* T)
      (write-line (strcat "ZB1\t" *cgp-zb-token* "\t" (cgp-road-code-string name)) *cgp-zb-file*)
      (close *cgp-zb-file*) (setq *cgp-zb-file* nil)
      (if (vl-file-rename temporary target)
        (progn (setq *cgp-zb-own-temp* nil) 'SENT)
        (if (findfile target) 'BUSY 'ERROR)))))

(defun cgp-zb-publish (name / result)
  (setq *cgp-zb-own-temp* nil result (vl-catch-all-apply 'cgp-zb-write-request (list name)))
  (cgp-zb-cleanup)
  (if (vl-catch-all-error-p result) 'ERROR result))

(defun cgp-zb-get-name ()
  (cgp-input-road "\n请输入本次要取点的道路名称（回车或 Esc 取消）："))

(defun cgp-zb-request-road (/ entered name status)
  (setq entered (vl-catch-all-apply 'cgp-zb-get-name nil))
  (cond
    ((or (vl-catch-all-error-p entered) (null entered)) (prompt "\n已取消，没有提交续接请求。"))
    ((= (setq name (cgp-trim-road entered)) "") (prompt "\n已取消，没有提交续接请求。"))
    ((not (cgp-valid-road name)) (prompt "\n道路名称应为 1-100 个字符，不能包含控制字符。"))
    ((not (cgp-zb-ready-p)) (prompt "\n生成器续接状态已失效，请保持生成器打开，稍后重新输入 ZB。"))
    (T
      (setq status (cgp-zb-publish name))
      (cond
        ((eq status 'SENT) (prompt (strcat "\n正在准备道路 " name "，请等待生成器确认。")))
        ((eq status 'BUSY) (prompt "\n上一条续接请求正在处理，请稍候。"))
        ((eq status 'NOTREADY) (prompt "\n生成器续接状态已失效，请稍后重新输入 ZB。"))
        (T (prompt "\n无法提交续接请求，请在生成器中检查连接。"))))))

(defun c:ZB (/ *error*)
  (defun *error* (message)
    (cgp-zb-cleanup)
    (prompt "\nZB 续接请求未完成，请在生成器中检查连接。") (princ))
  (cond
    ((not (and *cgp-zb-dir* *cgp-zb-token*)) (prompt "\n请先从新版坐标生成器启动 CAD 取点，再使用 ZB。"))
    ((or *cgp-active* (not *cgp-done*)) (prompt "\n当前取点尚未结束。可用 N 换路；结束全部取点后再输入 ZB。"))
    (*cgp-failed* (prompt "\n上一轮取点未正常完成，请从生成器重新启动 CAD 取点。"))
    ((cgp-zb-pending-p) (prompt "\n上一条续接请求正在处理，请稍候。"))
    ((not (cgp-zb-ready-p)) (prompt "\n生成器未准备好续接或已关闭。请保持生成器打开，稍后重新输入 ZB。"))
    (T (cgp-zb-request-road)))
  (princ))

(defun cgp-cancelled-p (message)
  (and (eq (type message) 'STR)
       (member (strcase (vl-string-trim " \t\r\n" message)) '@@CANCELMESSAGES@@)))

(defun c:CGPICK (/ *error* input)
  (defun *error* (message)
    (cgp-recover-transaction)
    (cgp-clean-preview)
    (if (and message (not (cgp-cancelled-p message)))
      (cgp-emit "ERROR\tNative picking failed; existing accepted points were preserved."))
    (cgp-finish) (princ))
  (if *cgp-done*
    (prompt "\n本轮取点已结束。继续取点请输入 ZB，然后输入道路名称。")
  (if (cgp-begin)
    (progn
      (while (not (cgp-stopped))
        (initget "Undo New Quit 撤销 换路 结束")
        (setq input (getpoint (strcat "\n请拾取 " (cgp-name) " [撤销(U)/换路(N)/结束(Q)] <回车：结束本道路，进入道路菜单>：")))
        (if (not (cgp-stopped)) (cond ((null input) (cgp-road-menu))
              ((member input '("Undo" "撤销")) (cgp-undo))
              ((member input '("New" "换路")) (cgp-new-road))
              ((member input '("Quit" "结束")) (setq *cgp-abort* T))
              ((listp input) (cgp-place (trans input 1 0))))))
      (cgp-finish))))
  (princ))
(princ)
'''
