"""Minimal, dependency-free XLSX reader/writer for the agent sandbox.

The sandbox runs with `--network none`, so openpyxl cannot be installed. This
module speaks OOXML directly using only the standard library, which makes Excel
support work in any image without a build step.

It is deliberately narrow. It writes and reads rectangular sheets with typed
cells and per-column number formats, which is everything the `excel_*` tools
need. It is not a general Excel engine: no charts, no images, no pivot tables,
no formula evaluation (formulas are stored; `read` reports the last cached
value).

Public API:
    create_book()                  -> Book
    load_book(path)                -> Book
    book.sheets                    -> list[Sheet]
    book.get(name) / book.ensure(name) / book.remove(name)
    sheet.append(cells, formats=..) / sheet.add_rows(rows, headers=..)
    book.save(path)

Cell values may be str, int, float, bool, datetime.date, datetime.datetime,
or None. Anything else is stringified.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import shutil
import tempfile
import zipfile
from xml.sax.saxutils import escape, quoteattr

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
NS_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
NS_XML = "http://www.w3.org/XML/1998/namespace"

# Excel's cell-number formats are indexed; these ids are built in.
BUILTIN_FORMATS = {
    0: "General",
    1: "0",
    2: "0.00",
    3: "#,##0",
    4: "#,##0.00",
    9: "0%",
    10: "0.00%",
    11: "0.00E+00",
    12: "# ?/?",
    13: "# ??/??",
    14: "mm-dd-yy",
    15: "d-mmm-yy",
    16: "d-mmm",
    17: "mmm-yy",
    18: "h:mm AM/PM",
    19: "h:mm:ss AM/PM",
    20: "h:mm",
    21: "h:mm:ss",
    22: "m/d/yy h:mm",
    37: "#,##0 ;(#,##0)",
    38: "#,##0 ;[Red](#,##0)",
    39: "#,##0.00;(#,##0.00)",
    40: "#,##0.00;[Red](#,##0.00)",
    49: "@",
}

MAX_CELL_CHARS = 32767
MAX_SHEET_CHARS = 31

_ILLEGAL_SHEET = re.compile(r"[\[\]:*?/\\]")
_DATE_RE = re.compile(r"^(-?\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?")


class ExcelError(Exception):
    """Raised for malformed books or illegal operations."""


def _col_letter(index: int) -> str:
    """0-based column index -> A, B, ..., Z, AA, ..."""
    if index < 0:
        raise ExcelError("column index must be >= 0")
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _col_index(letters: str) -> int:
    """A, AA, ... -> 0-based column index."""
    value = 0
    for ch in letters.strip().upper():
        if not ("A" <= ch <= "Z"):
            raise ExcelError(f"bad column reference: {letters!r}")
        value = value * 26 + (ord(ch) - 64)
    return value - 1


def _coord(row: int, col: int) -> str:
    """1-based row, 0-based column -> 'B7'."""
    return f"{_col_letter(col)}{row}"


def _serial(value: _dt.date | _dt.datetime) -> float:
    """Excel 1900 date system serial number (with Excel's leap-year bug)."""
    if isinstance(value, _dt.datetime):
        moment = value
    else:
        moment = _dt.datetime(value.year, value.month, value.day)
    epoch = _dt.datetime(1899, 12, 30)
    delta = moment - epoch
    days = delta.days + delta.seconds / 86400.0
    if delta.days >= 60:  # Excel believes 1900-02-29 existed
        days += 1
    return days


def _from_serial(serial: float) -> _dt.datetime:
    epoch = _dt.datetime(1899, 12, 30)
    if serial >= 60:
        serial -= 1
    try:
        return epoch + _dt.timedelta(days=serial)
    except (OverflowError, ValueError):
        # Negative or absurd serial: hand the caller the raw number instead of
        # a date, so a bad format does not destroy the value.
        return serial  # type: ignore[return-value]


def _is_date_format(fmt: str) -> bool:
    stripped = re.sub(r'"[^"]*"|\[[^\]]*\]', "", fmt)
    return bool(re.search(r"[ymdh]", stripped, re.IGNORECASE)) and not re.search(
        r"0|#", stripped
    )


class Sheet:
    """A rectangular sheet: rows of Python values plus per-column formats."""

    def __init__(self, name: str, book: "Book | None" = None):
        self.name = name
        self.book = book
        self.rows: list[list[object]] = []
        # Original cell text for formula cells, preserved on round-trip.
        self._formulas: dict[tuple[int, int], str] = {}
        # column index -> number format code (0-based)
        self.formats: dict[int, str] = {}
        self.widths: dict[int, float] = {}
        self.frozen: str | None = None

    # -- construction ----------------------------------------------------
    def append(self, cells: list[object], formats: dict[int, str] | None = None) -> int:
        """Append one row. `formats` maps a 0-based column index to a format code."""
        row_index = len(self.rows)
        self.rows.append(list(cells))
        if formats:
            for col, code in formats.items():
                self._set_format(col, code)
        return row_index + 1

    def add_rows(
        self,
        rows: list[list[object]],
        headers: list[str] | None = None,
        formats: dict[int | str, str] | None = None,
    ) -> int:
        """Append `headers` (if any) then every row. Returns rows added.

        `formats` keys may be 0-based indices, column letters, or header names.
        """
        added = 0
        if headers:
            self.append(list(headers))
            added += 1
        for row in rows:
            self.append(list(row))
            added += 1
        for key, code in (formats or {}).items():
            if isinstance(key, int):
                self._set_format(key, code)
            elif str(key).isdigit():
                self._set_format(int(key), code)
            elif headers and str(key) in [str(h) for h in headers]:
                self._set_format([str(h) for h in headers].index(str(key)), code)
            else:
                self._set_format(_col_index(str(key)), code)
        return added

    def _set_format(self, col: int, code: str) -> None:
        if not isinstance(col, int) or col < 0:
            raise ExcelError(f"column index must be a non-negative int, got {col!r}")
        self.formats[col] = code

    def set_format(self, column: int | str, number_format: str) -> None:
        """Set a column's number format. Accepts a 0-based index, 1-based int via
        `set_format_at`, or a column letter. Ambiguity between 0-based and 1-based
        integers is avoided by treating ints as 0-based here and exposing
        `set_format_at` for 1-based callers."""
        if isinstance(column, int):
            self._set_format(column, number_format)
        else:
            self._set_format(_col_index(str(column)), number_format)

    def set_format_at(self, column_1based: int, number_format: str) -> None:
        """Set a column's format using a 1-based column number."""
        self._set_format(int(column_1based) - 1, number_format)

    def set_widths(self, widths: dict[int, float]) -> None:
        """widths: {0-based column index: width in characters}."""
        for col, width in widths.items():
            self.widths[col] = float(width)

    def freeze(self, ref: str) -> None:
        """Freeze panes at `ref`, e.g. 'A2' or 'B2'."""
        self.frozen = ref

    # -- reading ---------------------------------------------------------
    @property
    def header(self) -> list[object]:
        return list(self.rows[0]) if self.rows else []

    def data_rows(self) -> list[list[object]]:
        return self.rows[1:] if self.rows else []

    def column(self, name: str) -> list[object]:
        """Values of the column whose header equals `name` (header row excluded)."""
        if not self.rows:
            return []
        try:
            index = [str(h) for h in self.rows[0]].index(name)
        except ValueError:
            raise ExcelError(
                f"no column named {name!r} in sheet {self.name!r}; "
                f"headers are {[str(h) for h in self.rows[0]]}"
            ) from None
        return [row[index] if index < len(row) else None for row in self.rows[1:]]

    def to_dicts(self) -> list[dict[str, object]]:
        """Rows as dicts keyed by header, blanks padded."""
        if not self.rows:
            return []
        headers = [str(h) for h in self.rows[0]]
        out = []
        for row in self.rows[1:]:
            padded = list(row) + [None] * (len(headers) - len(row))
            out.append(dict(zip(headers, padded)))
        return out

    def __len__(self) -> int:
        return len(self.rows)


class Book:
    """An ordered collection of sheets."""

    def __init__(self):
        self.sheets: list[Sheet] = []
        self._custom_formats: dict[str, int] = {}
        self._shared: dict[str, int] = {}

    # -- sheet management ------------------------------------------------
    def names(self) -> list[str]:
        return [s.name for s in self.sheets]

    def get(self, name: str) -> Sheet:
        for sheet in self.sheets:
            if sheet.name == name:
                return sheet
        raise ExcelError(f"no sheet named {name!r}; sheets are {self.names()}")

    def has(self, name: str) -> bool:
        return any(s.name == name for s in self.sheets)

    def ensure(self, name: str) -> Sheet:
        if self.has(name):
            return self.get(name)
        return self.add(name)

    def remove(self, name: str) -> None:
        self.sheets = [s for s in self.sheets if s.name != name]

    def add(self, name: str) -> Sheet:
        clean = _ILLEGAL_SHEET.sub("", str(name)).strip() or "Sheet"
        clean = clean[:MAX_SHEET_CHARS]
        if self.has(clean):
            raise ExcelError(f"sheet {clean!r} already exists")
        sheet = Sheet(clean, self)
        self.sheets.append(sheet)
        return sheet

    # -- serialisation ---------------------------------------------------
    def _format_id(self, code: str) -> int:
        if not code:
            code = "General"
        for fmt_id, existing in BUILTIN_FORMATS.items():
            if existing == code:
                return fmt_id
        if code not in self._custom_formats:
            self._custom_formats[code] = 164 + len(self._custom_formats)
        return self._custom_formats[code]

    def save(self, path: str) -> None:
        if not self.sheets:
            raise ExcelError("workbook has no sheets")
        if not str(path).lower().endswith(".xlsx"):
            path = str(path) + ".xlsx"

        target = os.path.abspath(path)
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)

        # Detect formats before writing so the style table is complete.
        used_formats: list[str] = []
        for sheet in self.sheets:
            for code in sheet.formats.values():
                if code and code not in BUILTIN_FORMATS.values() and code not in used_formats:
                    used_formats.append(code)
        self._custom_formats = {code: 164 + i for i, code in enumerate(used_formats)}

        tmp_dir = tempfile.mkdtemp(prefix="xlsx-")
        try:
            self._write_package(tmp_dir)
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
                for root, _dirs, files in os.walk(tmp_dir):
                    for name in sorted(files):
                        full = os.path.join(root, name)
                        arc = os.path.relpath(full, tmp_dir).replace(os.sep, "/")
                        zf.write(full, arc)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        self.path = target

    def _write_package(self, root: str) -> None:
        def write(rel: str, text: str) -> None:
            full = os.path.join(root, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(text)

        # Collect the shared-string table first, then render every part from it.
        # The table must be complete before any sheet XML is emitted.
        self._shared = {}
        for sheet in self.sheets:
            for sheet_row in sheet.rows:
                for cell in sheet_row:
                    if isinstance(cell, str) and cell:
                        self._shared_index(cell[:MAX_CELL_CHARS])

        write("[Content_Types].xml", self._content_types())
        write("_rels/.rels", self._root_rels())
        write("xl/workbook.xml", self._workbook_xml())
        write("xl/_rels/workbook.xml.rels", self._workbook_rels())
        write("xl/styles.xml", self._styles_xml())
        write("xl/sharedStrings.xml", self._shared_strings_xml())
        for i, sheet in enumerate(self.sheets, start=1):
            write(f"xl/worksheets/sheet{i}.xml", self._sheet_xml(sheet))

    def _shared_strings_xml(self) -> str:
        items = "".join(
            f"<si><t xml:space=\"preserve\">{escape(text)}</t></si>"
            for text, _ in sorted(self._shared.items(), key=lambda kv: kv[1])
        )
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<sst xmlns="{NS_MAIN}" count="{len(self._shared)}" '
            f'uniqueCount="{len(self._shared)}">{items}</sst>'
        )

    def _content_types(self) -> str:
        sheets = "".join(
            f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
            f'ContentType="application/vnd.openxmlformats-officedocument.'
            f'spreadsheetml.worksheet+xml"/>'
            for i in range(1, len(self.sheets) + 1)
        )
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Types xmlns="{NS_CT}">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
            f"{sheets}"
            "</Types>"
        )

    def _root_rels(self) -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{NS_PKG_REL}">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>"
        )

    def _workbook_xml(self) -> str:
        entries = "".join(
            f'<sheet name={quoteattr(s.name)} sheetId="{i}" r:id="rId{i}"/>'
            for i, s in enumerate(self.sheets, start=1)
        )
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<workbook xmlns="{NS_MAIN}" xmlns:r="{NS_REL}">'
            f"<sheets>{entries}</sheets>"
            "</workbook>"
        )

    def _workbook_rels(self) -> str:
        rels = "".join(
            f'<Relationship Id="rId{i}" '
            f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{i}.xml"/>'
            for i in range(1, len(self.sheets) + 1)
        )
        style_id = len(self.sheets) + 1
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{NS_PKG_REL}">'
            f"{rels}"
            f'<Relationship Id="rId{style_id}" '
            f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
            f'Target="styles.xml"/>'
            "</Relationships>"
        )

    def _styles_xml(self) -> str:
        """Style ids: one cellXf per (role, number format), see _cell_styles()."""
        fonts = (
            '<font><sz val="11"/><name val="Calibri"/></font>'
            '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>'
        )
        fills = (
            '<fill><patternFill patternType="none"/></fill>'
            '<fill><patternFill patternType="gray125"/></fill>'
            '<fill><patternFill patternType="solid"><fgColor rgb="FF1F3864"/>'
            '<bgColor indexed="64"/></patternFill></fill>'
        )
        borders = (
            '<border><left/><right/><top/><bottom/><diagonal/></border>'
            '<border><left/><right/><top/>'
            '<bottom style="thin"><color rgb="FFBFBFBF"/></bottom><diagonal/></border>'
        )
        xfs = []
        for role, code in self._cell_styles():
            fmt = self._format_id(code)
            if role == "header":
                xfs.append(
                    f'<xf numFmtId="{fmt}" fontId="1" fillId="2" borderId="1" '
                    'applyFont="1" applyFill="1" applyBorder="1" applyNumberFormat="1" '
                    'applyAlignment="1"><alignment horizontal="center" '
                    'vertical="center"/></xf>'
                )
            else:
                xfs.append(
                    f'<xf numFmtId="{fmt}" fontId="0" fillId="0" borderId="0" '
                    'applyNumberFormat="1"/>'
                )
        custom = "".join(
            f'<numFmt numFmtId="{fid}" formatCode={quoteattr(code)}/>'
            for code, fid in sorted(self._custom_formats.items(), key=lambda kv: kv[1])
        )
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<styleSheet xmlns="{NS_MAIN}">'
            f'<numFmts count="{len(self._custom_formats)}">{custom}</numFmts>'
            f'<fonts count="2">{fonts}</fonts>'
            f'<fills count="3">{fills}</fills>'
            f'<borders count="2">{borders}</borders>'
            f'<cellXfs count="{len(xfs)}">{"".join(xfs)}</cellXfs>'
            "</styleSheet>"
        )

    def _cell_styles(self) -> list[tuple[str, str]]:
        """Ordered list of (role, number_format_code); index == cellXf id.

        Every (role, format) pair that any cell can reference must exist here,
        and a header cell must be able to reference a data-only format (a
        single-row sheet has no body row to carry it). Body is registered first
        so the header cell can fall back to it.
        """
        styles: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        codes: list[str] = ["General"]
        for sheet in self.sheets:
            for code in sheet.formats.values():
                if code and code not in codes:
                    codes.append(code)
        for code in codes:
            for role in ("body", "header"):
                pair = (role, code)
                if pair not in seen:
                    seen.add(pair)
                    styles.append(pair)
        return styles

    def _styles_lookup(self) -> dict[tuple[str, str], int]:
        return {pair: i for i, pair in enumerate(self._cell_styles())}

    def _sheet_xml(self, sheet: Sheet) -> str:
        lookup = self._styles_lookup()
        has_header = bool(sheet.rows)
        parts = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            f'<worksheet xmlns="{NS_MAIN}">',
        ]
        if sheet.frozen:
            col = _col_index(re.sub(r"\d", "", sheet.frozen)) if sheet.frozen[0].isalpha() else 0
            row = int(re.sub(r"\D", "", sheet.frozen) or "1") - 1
            parts.append(
                '<sheetViews><sheetView workbookViewId="0">'
                f'<pane xSplit="{col}" ySplit="{row}" topLeftCell="{sheet.frozen}" activePane="bottomRight" state="frozen"/>'
                "</sheetView></sheetViews>"
            )
        if sheet.widths:
            cols = "".join(
                f'<col min="{i + 1}" max="{i + 1}" width="{w:.2f}" customWidth="1"/>'
                for i, w in sorted(sheet.widths.items())
            )
            parts.append(f"<cols>{cols}</cols>")
        parts.append("<sheetData>")
        for r, row in enumerate(sheet.rows, start=1):
            cells = []
            for c, raw in enumerate(row):
                cells.append(self._cell_xml(r, c, raw, sheet, lookup, has_header))
            joined = "".join(cells)
            parts.append(f'<row r="{r}">{joined}</row>')
        parts.append("</sheetData></worksheet>")
        return "".join(parts)

    def _cell_xml(self, row, col, value, sheet, lookup, has_header) -> str:
        ref = _coord(row, col)
        code = sheet.formats.get(col, "General")
        kind = "header" if (has_header and row == 1) else "body"
        style = lookup.get((kind, code))
        if style is None:
            style = lookup.get(("body", code), lookup.get(("body", "General"), 0))
        formula = sheet._formulas.get((row - 1, col))
        if formula:
            body = f"<f>{escape(formula.lstrip('='))}</f>"
            cached = self._cached_formula_value(value)
            return f'<c r="{ref}" s="{style}">{body}{cached}</c>'
        if value is None:
            return f'<c r="{ref}" s="{style}"/>'
        if isinstance(value, bool):
            return f'<c r="{ref}" s="{style}" t="b"><v>{1 if value else 0}</v></c>'
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            number = float(value)
            text = str(int(number)) if number.is_integer() and abs(number) < 1e15 else repr(number)
            return f'<c r="{ref}" s="{style}"><v>{text}</v></c>'
        if isinstance(value, (_dt.datetime, _dt.date)):
            return f'<c r="{ref}" s="{style}"><v>{_serial(value)}</v></c>'
        index = self._shared_index(str(value)[:MAX_CELL_CHARS])
        return f'<c r="{ref}" s="{style}" t="s"><v>{index}</v></c>'

    def _cached_formula_value(self, value) -> str:
        """Formulas need a cached result or Excel shows a blank until recalculation."""
        if value is None:
            return ""
        if isinstance(value, bool):
            return f'<v>{1 if value else 0}</v>'
        if isinstance(value, (int, float)):
            return f"<v>{value}</v>"
        if isinstance(value, (_dt.datetime, _dt.date)):
            return f"<v>{_serial(value)}</v>"
        return f"<v>{escape(str(value))}</v>"

    def _shared_index(self, text: str) -> int:
        if text not in self._shared:
            self._shared[text] = len(self._shared)
        return self._shared[text]


# ---------------------------------------------------------------- reading
_XLSX_NS = {"m": NS_MAIN}
_TAG = re.compile(r"\{[^}]*\}")
_REF = re.compile(r"([A-Z]+)(\d+)")

# Number formats that are actually dates, per the built-in table.
_DATE_FORMAT_IDS = {14, 15, 16, 17, 18, 19, 20, 21, 22, 45, 46, 47}


def _cell_text(raw_xml: str) -> str:
    """Extract concatenated <t> runs from an inline or shared string fragment."""
    return "".join(re.findall(r"<t[^>]*>(.*?)</t>", raw_xml, re.DOTALL))


def _attr(tag: str, name: str) -> str | None:
    """Read an XML attribute value, tolerating single or double quoting."""
    match = re.search(name + r"\s*=\s*(\"[^\"]*\"|'[^']*')", tag)
    if not match:
        return None
    return match.group(1)[1:-1]


def _unescape(text: str) -> str:
    return (
        text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
        .replace("&amp;", "&")
    )


def create_book() -> Book:
    """A new workbook with no sheets."""
    return Book()


def load_book(path: str) -> Book:
    """Read a workbook. Formulas are preserved; cached values are returned."""
    if not os.path.exists(path):
        raise ExcelError(f"file not found: {path}")
    book = Book()
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        styles = _read_styles(zf, names)
        shared = _read_shared_strings(zf, names)
        workbook_xml = zf.read("xl/workbook.xml").decode("utf-8", "replace")
        rels_xml = zf.read("xl/_rels/workbook.xml.rels").decode("utf-8", "replace")
        targets = dict(re.findall(r'Id="([^"]+)"[^>]*Target="([^"]+)"', rels_xml))
        for sheet_match in re.finditer(r"<sheet\b[^>]*>", workbook_xml):
            tag = sheet_match.group(0)
            name = _unescape(re.search(r'name="([^"]*)"', tag).group(1))
            rid = re.search(r'r:id="([^"]+)"', tag)
            target = targets.get(rid.group(1), "") if rid else ""
            if not target:
                continue
            part = target.lstrip("/")
            if not part.startswith("xl/"):
                part = "xl/" + part.lstrip("./")
            if part not in names:
                continue
            book.sheets.append(_read_sheet(zf.read(part).decode("utf-8", "replace"), name, styles, shared))
    if not book.sheets:
        raise ExcelError(f"no sheets found in {path}")
    book.path = os.path.abspath(path)
    return book


def _read_styles(zf, names) -> list[str]:
    """Return a list indexed by cellXf id -> number format code."""
    if "xl/styles.xml" not in names:
        return []
    xml = zf.read("xl/styles.xml").decode("utf-8", "replace")
    custom: dict[int, str] = {}
    for raw in re.findall(r"<numFmt\b[^>]*>", xml):
        fmt_id = _attr(raw, "numFmtId")
        code = _attr(raw, "formatCode")
        if fmt_id is not None and code is not None:
            custom[int(fmt_id)] = _unescape(code)
    block = re.search(r"<cellXfs[^>]*>(.*?)</cellXfs>", xml, re.DOTALL)
    if not block:
        return []
    formats = []
    for xf in re.findall(r"<xf\b[^>]*>", block.group(1)):
        raw_id = _attr(xf, "numFmtId")
        fmt_id = int(raw_id) if raw_id is not None else 0
        formats.append(custom.get(fmt_id, BUILTIN_FORMATS.get(fmt_id, "General")))
    return formats


def _read_shared_strings(zf, names) -> list[str]:
    if "xl/sharedStrings.xml" not in names:
        return []
    xml = zf.read("xl/sharedStrings.xml").decode("utf-8", "replace")
    return [_unescape(_cell_text(si)) for si in re.findall(r"<si>(.*?)</si>", xml, re.DOTALL)]


def _read_sheet(xml: str, name: str, styles: list[str], shared: list[str]) -> Sheet:
    sheet = Sheet(name)
    seen_formats: dict[int, str] = {}
    header_formats: dict[int, str] = {}
    for row_match in re.finditer(r"<row\b[^>]*>(.*?)</row>", xml, re.DOTALL):
        values: list[object] = []
        for cell_match in re.finditer(r"<c\b([^>]*)(?:/>|>(.*?)</c>)", row_match.group(1), re.DOTALL):
            attrs, body = cell_match.group(1), cell_match.group(2) or ""
            ref = re.search(r'r="([A-Z]+\d+)"', attrs)
            col = _col_index(_REF.match(ref.group(1)).group(1)) if ref else len(values)
            while len(values) < col:
                values.append(None)
            ctype = re.search(r't="([^"]+)"', attrs)
            ctype = ctype.group(1) if ctype else "n"
            style_id = re.search(r's="(\d+)"', attrs)
            style_id = int(style_id.group(1)) if style_id else 0
            fmt = styles[style_id] if style_id < len(styles) else "General"
            is_header_row = not sheet.rows
            formula = re.search(r"<f[^>]*>(.*?)</f>", body, re.DOTALL)
            if formula:
                sheet._formulas[(len(sheet.rows), col)] = _unescape(formula.group(1))
            # Formats are a per-column property. Record the first non-General
            # format seen, preferring data rows over the styled header row.
            if fmt != "General":
                if is_header_row:
                    header_formats.setdefault(col, fmt)
                elif col not in seen_formats:
                    seen_formats[col] = fmt
            if ctype == "s":
                idx_match = re.search(r"<v>(.*?)</v>", body, re.DOTALL)
                idx = int(idx_match.group(1)) if idx_match else -1
                value = shared[idx] if 0 <= idx < len(shared) else None
            elif ctype == "inlineStr":
                value = _unescape(_cell_text(body)) or None
            elif ctype == "b":
                value = bool(int(re.search(r"<v>(\d+)</v>", body).group(1))) if "<v>" in body else None
            elif ctype == "str":
                value = _unescape(_cell_text(body)) or None
            elif ctype == "e":
                value = _unescape(re.search(r"<v>(.*?)</v>", body, re.DOTALL).group(1))
            else:
                vmatch = re.search(r"<v>(.*?)</v>", body, re.DOTALL)
                if not vmatch:
                    value = None
                else:
                    try:
                        number = float(vmatch.group(1))
                    except ValueError:
                        value = _unescape(vmatch.group(1))
                    else:
                        if _is_date_format(fmt) and number >= 1:
                            value = _from_serial(number)
                        else:
                            value = int(number) if number.is_integer() else number
            values.append(value)
        sheet.rows.append(values)
    # Widths, for round-tripping the layout we wrote.
    for col_match in re.finditer(r'<col[^>]*min="(\d+)"[^>]*width="([\d.]+)"', xml):
        sheet.widths[int(col_match.group(1)) - 1] = float(col_match.group(2))
    pane = re.search(r'<pane[^>]*topLeftCell="([A-Z]+\d+)"', xml)
    if pane:
        sheet.frozen = pane.group(1)
    for col, fmt in header_formats.items():
        seen_formats.setdefault(col, fmt)
    sheet.formats = seen_formats
    return sheet


def describe(sheet: Sheet, *, max_rows: int = 10) -> dict:
    """Compact structural summary used by the `excel_read` tool."""
    preview = []
    for row in sheet.rows[:max_rows]:
        preview.append([_display(v) for v in row])
    column_formats = {
        _col_letter(col): fmt for col, fmt in sorted(sheet.formats.items()) if fmt != "General"
    }
    return {
        "sheet": sheet.name,
        "rows": len(sheet.rows),
        "columns": max((len(r) for r in sheet.rows), default=0),
        "headers": [str(h) if h is not None else "" for h in sheet.header],
        "number_formats": column_formats,
        "preview": preview,
        "formulas": len(sheet._formulas),
    }


def _display(value: object) -> object:
    if isinstance(value, _dt.datetime):
        if value.time() == _dt.time(0, 0):
            return value.date().isoformat()
        return value.isoformat(sep=" ")
    if isinstance(value, _dt.date):
        return value.isoformat()
    return value
