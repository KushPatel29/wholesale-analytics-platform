"""Exports (XLSX) using xlsxwriter engine and formatting helpers."""

from __future__ import annotations

from io import BytesIO
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence
import importlib.util
import tempfile
from pathlib import Path
import zipfile
from xml.sax.saxutils import escape

from flask import send_file, after_this_request

import pandas as pd

from app.core.sensitive_data import mask_dataframe, mask_export_sheets

try:
    from flask_login import current_user  # type: ignore
except Exception:  # pragma: no cover
    current_user = None  # type: ignore


def export_status() -> str:
    return "exports ready"


def _xlsx_engine_name() -> str | None:
    if importlib.util.find_spec("xlsxwriter") is not None:
        return "xlsxwriter"
    if importlib.util.find_spec("openpyxl") is not None:
        return "openpyxl"
    return None


def xlsx_export_available() -> bool:
    return True


def _csv_filename_from_excel_name(filename: str) -> str:
    stem = str(filename or "export.xlsx")
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    return f"{stem}.csv"


# --- literal text ------------------------------------------------------------
#
# A cell whose text starts with "=" is a formula to Excel, so a product called
# "=Rebate" arrived as a broken formula rather than as its own name. xlsxwriter
# offers a switch for this; openpyxl infers the formula from the leading "=" as
# the cell is assigned, so those cells are demoted back to text after the frame
# is written. Neither approach alters the string - no apostrophe is prepended -
# so the exported value still reads back as what the page displayed.

_XLSXWRITER_ENGINE_KWARGS = {"options": {"strings_to_formulas": False, "strings_to_urls": False}}


def _excel_writer(target: Any, engine: str):
    """`pd.ExcelWriter` with text kept as text."""
    if str(engine or "").strip().lower() == "xlsxwriter":
        return pd.ExcelWriter(target, engine=engine, engine_kwargs=_XLSXWRITER_ENGINE_KWARGS)
    return pd.ExcelWriter(target, engine=engine)


def _demote_openpyxl_formulas(writer, sheet_name: str) -> None:
    """Turn any cell openpyxl decided was a formula back into a string."""
    if str(getattr(writer, "engine", "")).strip().lower() != "openpyxl":
        return
    try:
        ws = writer.sheets[sheet_name]
        for row in ws.iter_rows():
            for cell in row:
                if cell.data_type == "f":
                    cell.data_type = "s"
    except Exception:
        pass


# --- column units ------------------------------------------------------------
#
# A workbook that disagrees with the page it was downloaded from is worse than
# no workbook, so the number format is chosen from an explicit unit rather than
# from whichever substring happened to match first.
#
# The house convention for a percentage is the 0-100 scale - see
# `app/services/formatting.py:percent`, "a percentage that is already on the
# 0-100 scale", which prints 22.96 as "23.0%". Excel's own `0.0%` format
# multiplies the stored number by 100 before displaying it, so a margin of
# 22.96 rendered as **2296.0%**. `PERCENT_FORMAT` therefore prints a literal
# percent sign and leaves the number alone; the cell still holds 22.96, which
# is what the page, the API and the CSV of the same export all hold.

CURRENCY_FORMAT = "$#,##0.00"
PERCENT_FORMAT = '0.0"%"'
COUNT_FORMAT = "#,##0"
# `#,##0` on a coefficient of variation of 0.41 prints "0", so a unitless
# column only gets the integer format when its values actually are integers.
DECIMAL_FORMAT = "#,##0.00"

# Matched against whole words, not substrings: "generated_at" contains "rate"
# and was being formatted as a percentage.
# "ratio" is deliberately absent: `current_ratio` is 1.8, not 1.8%. A ratio
# gets no unit rather than a wrong one.
_PERCENT_WORDS = {"pct", "percent", "percentage", "rate", "share"}

# A dispersion measure carries neither unit, and the words around it lie about
# which: `Rate Volatility` was being printed as a percentage on the strength of
# "rate", and `Cost Volatility` as dollars on the strength of "cost", so the
# same statistic appeared twice in one sheet in two different units. These win
# over both word lists.
_UNITLESS_WORDS = {"volatility", "ratio", "index", "score", "hhi", "zscore", "stddev", "sigma"}
_CURRENCY_WORDS = {
    "revenue", "cost", "cogs", "spend", "price", "profit", "amount", "sales",
    "value", "dollars", "usd",
}

# `rate` is the ambiguous one: `repeat_rate` is a percentage and `blended_rate`
# is labor cost divided by paid hours - dollars per hour. Names whose `rate` is
# a unit price rather than a proportion are listed here so the word does not
# have to be dropped entirely.
_CURRENCY_RATE_NAMES = {
    "blended_rate", "prior_blended_rate", "effective_rate", "hourly_rate",
    "pay_rate", "bill_rate", "run_rate", "exchange_rate", "rate", "avg_rate",
    "average_rate", "labor_rate", "department_rate",
}


def _split_name(name: str) -> list[str]:
    """Split a column name into lowercase words, handling snake and camel case."""
    text = str(name or "")
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    return [word for word in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if word]


def _name_words(name: str) -> set[str]:
    return set(_split_name(name))


def _normalized_name(name: str) -> str:
    """`blended_rate`, `Blended Rate` and `blendedRate` all normalise alike.

    Exports rename columns to display labels on the way out (`blended_rate` ->
    `Blended Rate`), so a lookup against the raw name would miss the renamed
    half of them.
    """
    return "_".join(_split_name(name))


def column_unit(name: str) -> str:
    """Return "percent", "currency", "count" or "plain" for a column name.

    Percent wins over currency: `revenue_share_pct` and `cost_coverage_pct` are
    percentages that merely mention money, and the old ordering printed them as
    `$23.40`.
    """
    raw = str(name or "").strip()
    if not raw:
        return "plain"
    if _normalized_name(raw) in _CURRENCY_RATE_NAMES:
        return "currency"
    words = _name_words(raw)
    if words & _UNITLESS_WORDS:
        return "count"
    # Percent wins outright. Naming the measure a column is a proportion *of*
    # does not make it money: `core_revenue_share`, `cost_null_rate` and
    # `below_target_revenue_share` are all percentages, and an earlier version
    # of this that let a currency word override them printed each as dollars.
    if raw.endswith("%") or (words & _PERCENT_WORDS):
        return "percent"
    if words & _CURRENCY_WORDS:
        return "currency"
    return "count"


# Exports run to 100,000 rows (`threshold_rows`), and these two questions are
# asked once per column. Coercing a whole column to answer either is work the
# answer does not need, so both read a bounded head of it - a column whose
# first thousand values are all whole numbers, or all unparseable, is not going
# to be described differently by row 90,000.
_UNIT_SAMPLE_ROWS = 1_000


def _sample(series: pd.Series) -> pd.Series:
    return series.head(_UNIT_SAMPLE_ROWS) if len(series) > _UNIT_SAMPLE_ROWS else series


def _holds_numbers(series: pd.Series) -> bool:
    """Whether a column carries numbers, whatever dtype pandas settled on."""
    try:
        if pd.api.types.is_bool_dtype(series):
            return False
        if pd.api.types.is_numeric_dtype(series):
            return bool(series.notna().any())
        return bool(pd.to_numeric(_sample(series), errors="coerce").notna().any())
    except Exception:
        return False


def _is_integral(series: pd.Series) -> bool:
    """Whether the values in a numeric column are whole numbers."""
    try:
        numbers = pd.to_numeric(_sample(series), errors="coerce").dropna()
        if numbers.empty:
            return True
        return bool((numbers % 1 == 0).all())
    except Exception:
        return True


def _apply_worksheet_formatting(writer, sheet_name: str, df: pd.DataFrame) -> None:
    """Set column widths and number formats on a written sheet.

    Every column is handled independently. This used to be one `try` around the
    whole loop, so a single column that raised - an all-`None` column, which is
    exactly what permission masking produces - silently left every column after
    it unformatted.
    """
    try:
        wb = writer.book
        ws = writer.sheets[sheet_name]
        formats = {
            "currency": wb.add_format({"num_format": CURRENCY_FORMAT}),
            "percent": wb.add_format({"num_format": PERCENT_FORMAT}),
            "count": wb.add_format({"num_format": COUNT_FORMAT}),
            "decimal": wb.add_format({"num_format": DECIMAL_FORMAT}),
        }
    except Exception:
        return

    for i, col in enumerate(df.columns):
        try:
            col_series = df[col]
            unit = column_unit(col)
            # `is_numeric_dtype` is False for an object column, which is what a
            # frame carrying `pd.NA` alongside floats ends up as - so several
            # percentage columns shipped unformatted and printed as
            # `3.106348459681461`. What matters is whether the *values* are
            # numbers, not which dtype pandas settled on. A masked column is
            # all-null and therefore still gets no format: a unit on an empty
            # cell would imply a value that is not there.
            numeric = _holds_numbers(col_series)
            if numeric and unit == "count" and not _is_integral(col_series):
                unit = "decimal"
            fmt = formats.get(unit) if numeric else None
            try:
                widths = col_series.astype(str).str.len()
                quantile = widths.quantile(0.9)
                width = max(10, min(40, int(quantile) + 2)) if pd.notna(quantile) else 12
            except Exception:
                width = 12
            width = max(width, min(40, len(str(col)) + 2))
            ws.set_column(i, i, width, fmt)
        except Exception:
            continue

    try:
        max_row, max_col = df.shape
        if max_row and max_col:
            ws.autofilter(0, 0, max_row, max_col - 1)
        ws.freeze_panes(1, 0)
    except Exception:
        pass


def _apply_xlsxwriter_charts(
    writer,
    sheets: Mapping[str, pd.DataFrame],
    chart_specs: Sequence[Mapping[str, Any]] | None = None,
) -> None:
    if not chart_specs:
        return
    try:
        if str(getattr(writer, "engine", "")).strip().lower() != "xlsxwriter":
            return
        wb = writer.book
        for raw in list(chart_specs or []):
            if not isinstance(raw, Mapping):
                continue
            source_sheet = str(raw.get("source_sheet") or "").strip()
            if not source_sheet or source_sheet not in writer.sheets:
                continue
            frame = sheets.get(source_sheet)
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                continue
            source_cols = list(frame.columns)
            if not source_cols:
                continue
            category_col = str(raw.get("category_col") or source_cols[0]).strip()
            if category_col not in frame.columns:
                category_col = source_cols[0]
            value_cols = [str(item).strip() for item in list(raw.get("value_cols") or []) if str(item).strip() in frame.columns]
            if not value_cols:
                numeric_cols = [
                    col
                    for col in source_cols
                    if col != category_col and pd.api.types.is_numeric_dtype(frame[col])
                ]
                value_cols = numeric_cols[:3] if numeric_cols else []
            if not value_cols:
                continue
            chart_type = str(raw.get("chart_type") or "line").strip().lower()
            if chart_type not in {"line", "bar", "column"}:
                chart_type = "line"
            chart = wb.add_chart({"type": "column" if chart_type == "bar" else chart_type})

            category_idx = int(frame.columns.get_loc(category_col))
            row_count = int(len(frame.index))
            if row_count <= 0:
                continue
            for col_name in value_cols[:5]:
                val_idx = int(frame.columns.get_loc(col_name))
                chart.add_series(
                    {
                        "name": [source_sheet, 0, val_idx],
                        "categories": [source_sheet, 1, category_idx, row_count, category_idx],
                        "values": [source_sheet, 1, val_idx, row_count, val_idx],
                    }
                )
            title = str(raw.get("title") or "").strip()
            x_axis = str(raw.get("x_axis") or category_col).strip()
            y_axis = str(raw.get("y_axis") or "Value").strip()
            if title:
                chart.set_title({"name": title})
            if x_axis:
                chart.set_x_axis({"name": x_axis})
            if y_axis:
                chart.set_y_axis({"name": y_axis})
            chart_sheet = str(raw.get("chart_sheet") or "Charts").strip()[:31].replace(":", "_").replace("/", "_")
            if not chart_sheet:
                chart_sheet = "Charts"
            ws = writer.sheets.get(chart_sheet)
            if ws is None:
                ws = wb.add_worksheet(chart_sheet)
                writer.sheets[chart_sheet] = ws
            insert_cell = str(raw.get("insert_cell") or "B2").strip() or "B2"
            ws.insert_chart(insert_cell, chart, {"x_scale": 1.35, "y_scale": 1.2})
    except Exception:
        # Chart embedding is best-effort and must never break export generation.
        pass


def _excel_column_name(index: int) -> str:
    out = ""
    value = index
    while value > 0:
        value, remainder = divmod(value - 1, 26)
        out = chr(65 + remainder) + out
    return out or "A"


def _xlsx_safe_sheet_names(names: Sequence[str]) -> list[str]:
    used: set[str] = set()
    out: list[str] = []
    for idx, raw in enumerate(names, start=1):
        candidate = (str(raw or f"Sheet{idx}")[:31]).replace(":", "_").replace("/", "_").replace("\\", "_")
        candidate = candidate.strip() or f"Sheet{idx}"
        if candidate not in used:
            used.add(candidate)
            out.append(candidate)
            continue
        base = candidate[:28] or "Sheet"
        suffix = 2
        while True:
            next_name = f"{base}_{suffix}"[:31]
            if next_name not in used:
                used.add(next_name)
                out.append(next_name)
                break
            suffix += 1
    return out


def _xlsx_string_cell(ref: str, value: str) -> str:
    text = escape(value or "")
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'


def _xlsx_number_cell(ref: str, value: float | int) -> str:
    return f'<c r="{ref}"><v>{value}</v></c>'


def _xlsx_bool_cell(ref: str, value: bool) -> str:
    return f'<c r="{ref}" t="b"><v>{1 if value else 0}</v></c>'


def _build_minimal_xlsx_bytes(sheets: Dict[str, pd.DataFrame]) -> bytes:
    safe_sheet_names = _xlsx_safe_sheet_names(list((sheets or {}).keys()) or ["Sheet1"])
    output = BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        sheet_paths = [f"xl/worksheets/sheet{idx}.xml" for idx in range(1, len(safe_sheet_names) + 1)]
        overrides = "".join(
            f'<Override PartName="/{path}" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for path in sheet_paths
        )
        zf.writestr(
            "[Content_Types].xml",
            (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/xl/workbook.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                f"{overrides}"
                '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
                '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
                "</Types>"
            ),
        )
        zf.writestr(
            "_rels/.rels",
            (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
                '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
                "</Relationships>"
            ),
        )
        zf.writestr(
            "docProps/core.xml",
            (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                'xmlns:dc="http://purl.org/dc/elements/1.1/" '
                'xmlns:dcterms="http://purl.org/dc/terms/" '
                'xmlns:dcmitype="http://purl.org/dc/dcmitype/" '
                'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
                "<dc:creator>Northgate Retail Analytics</dc:creator>"
                "<cp:lastModifiedBy>Northgate Retail Analytics</cp:lastModifiedBy>"
                "</cp:coreProperties>"
            ),
        )
        zf.writestr(
            "docProps/app.xml",
            (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
                'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
                "<Application>Northgate Retail Analytics</Application>"
                f"<Sheets>{len(safe_sheet_names)}</Sheets>"
                "</Properties>"
            ),
        )
        workbook_sheets = "".join(
            f'<sheet name="{escape(name)}" sheetId="{idx}" r:id="rId{idx}"/>'
            for idx, name in enumerate(safe_sheet_names, start=1)
        )
        zf.writestr(
            "xl/workbook.xml",
            (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                f"<sheets>{workbook_sheets}</sheets>"
                "</workbook>"
            ),
        )
        workbook_rels = "".join(
            f'<Relationship Id="rId{idx}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{idx}.xml"/>'
            for idx in range(1, len(safe_sheet_names) + 1)
        )
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                f"{workbook_rels}"
                "</Relationships>"
            ),
        )

        sheet_frames = list((sheets or {}).values()) or [pd.DataFrame()]
        for idx, (sheet_name, frame) in enumerate(zip(safe_sheet_names, sheet_frames), start=1):
            working = (frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()).copy()
            rows_xml: list[str] = []
            if not working.empty or list(working.columns):
                header_cells = []
                for col_idx, column in enumerate(working.columns, start=1):
                    ref = f"{_excel_column_name(col_idx)}1"
                    header_cells.append(_xlsx_string_cell(ref, str(column)))
                rows_xml.append(f'<row r="1">{"".join(header_cells)}</row>')
                for row_idx, record in enumerate(working.itertuples(index=False, name=None), start=2):
                    cell_xml: list[str] = []
                    for col_idx, value in enumerate(record, start=1):
                        ref = f"{_excel_column_name(col_idx)}{row_idx}"
                        if value is None:
                            continue
                        try:
                            if pd.isna(value):
                                continue
                        except Exception:
                            pass
                        if isinstance(value, bool):
                            cell_xml.append(_xlsx_bool_cell(ref, bool(value)))
                        elif isinstance(value, (int, float)) and not isinstance(value, bool):
                            cell_xml.append(_xlsx_number_cell(ref, value))
                        else:
                            if hasattr(value, "isoformat"):
                                rendered = value.isoformat()
                            else:
                                rendered = str(value)
                            cell_xml.append(_xlsx_string_cell(ref, rendered))
                    if cell_xml:
                        rows_xml.append(f'<row r="{row_idx}">{"".join(cell_xml)}</row>')
            dimension = "A1"
            if list(working.columns):
                last_col = _excel_column_name(max(1, len(working.columns)))
                last_row = max(1, len(working.index) + 1)
                dimension = f"A1:{last_col}{last_row}"
            sheet_xml = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                f'<dimension ref="{dimension}"/>'
                "<sheetData>"
                f"{''.join(rows_xml)}"
                "</sheetData>"
                "</worksheet>"
            )
            zf.writestr(f"xl/worksheets/sheet{idx}.xml", sheet_xml)
    output.seek(0)
    return output.read()


def dataframes_to_xlsx_bytes(
    sheets: Dict[str, pd.DataFrame],
    *,
    chart_specs: Sequence[Mapping[str, Any]] | None = None,
) -> bytes:
    """Return an in-memory XLSX with given sheet name -> DataFrame mapping."""
    engine = _xlsx_engine_name()
    sheets = mask_export_sheets(sheets, getattr(current_user, "_get_current_object", lambda: current_user)())
    if not engine:
        return _build_minimal_xlsx_bytes(sheets)
    output = BytesIO()
    safe_sheets: Dict[str, pd.DataFrame] = {}
    with _excel_writer(output, engine) as writer:
        for name, df in sheets.items():
            # Ensure a safe sheet name (max 31 chars, no special characters)
            safe = str(name)[:31].replace(":", "_").replace("/", "_")
            frame = df if isinstance(df, pd.DataFrame) else pd.DataFrame()
            safe_sheets[safe] = frame
            frame.to_excel(writer, sheet_name=safe, index=False)
            _apply_worksheet_formatting(writer, safe, frame)
            _demote_openpyxl_formulas(writer, safe)
        _apply_xlsxwriter_charts(writer, safe_sheets, chart_specs=chart_specs)
    output.seek(0)
    return output.read()


def _log_export_event(filename: str, fmt: str, meta: Optional[Dict[str, object]] = None) -> None:
    try:
        from flask import g, request  # type: ignore
        from flask_login import current_user  # type: ignore
        from app.core.audit import log_audit

        if getattr(g, "_export_logged", False):
            return
        if not getattr(current_user, "is_authenticated", False):
            return
        payload: Dict[str, object] = {"resource": filename, "format": fmt, "path": request.path}
        if meta:
            payload.update(meta)
        log_audit(current_user, "export", payload)
        g._export_logged = True
    except Exception:
        pass


def dataframes_to_xlsx_response(sheets: Dict[str, pd.DataFrame], filename: str = "export.xlsx", threshold_rows: int = 100_000):
    """Return a Flask response streaming an XLSX file.

    - If total rows > threshold_rows, writes to a temporary file and streams it via send_file.
    - Otherwise, uses in-memory bytes.
    """
    sheets = mask_export_sheets(sheets, getattr(current_user, "_get_current_object", lambda: current_user)())
    engine = _xlsx_engine_name()
    total_rows = sum(int(len(df)) for df in (sheets or {}).values())
    _log_export_event(filename, "xlsx", {"rows": total_rows, "sheets": list((sheets or {}).keys())})
    mimetype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if not engine:
        data = dataframes_to_xlsx_bytes(sheets)
        return send_file(BytesIO(data), as_attachment=True, download_name=filename, mimetype=mimetype)
    if total_rows > threshold_rows:
        # Write to a temp file on disk to keep memory bounded
        tmp = tempfile.NamedTemporaryFile(prefix="wa_export_", suffix=".xlsx", delete=False)
        tmp_path = Path(tmp.name)
        tmp.close()
        try:
            with _excel_writer(tmp_path.as_posix(), engine) as writer:
                for name, df in sheets.items():
                    safe = str(name)[:31].replace(":", "_").replace("/", "_")
                    (df if df is not None else pd.DataFrame()).to_excel(writer, sheet_name=safe, index=False)
                    _apply_worksheet_formatting(writer, safe, df if df is not None else pd.DataFrame())
                    _demote_openpyxl_formulas(writer, safe)

            @after_this_request
            def _cleanup(resp):  # pragma: no cover - side effect
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass
                return resp

            return send_file(tmp_path.as_posix(), as_attachment=True, download_name=filename, mimetype=mimetype)
        except Exception:
            # Fallback to in-memory if anything goes wrong
            if tmp_path.exists():
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass
    # In-memory path
    data = dataframes_to_xlsx_bytes(sheets)
    return send_file(BytesIO(data), as_attachment=True, download_name=filename, mimetype=mimetype)


def sanitize_filename(name: str, default: str = "export") -> str:
    """Return a filesystem- and header-safe filename stem.

    Keeps alphanumerics, dash, underscore, and dot; replaces whitespace with underscores.
    Trims to a reasonable length.
    """
    try:
        import re

        if not name:
            name = default
        # Normalize spaces
        s = str(name).strip().replace(" ", "_")
        # Remove disallowed chars
        s = re.sub(r"[^A-Za-z0-9._-]", "", s)
        # Avoid empty
        s = s or default
        # Limit length
        if len(s) > 80:
            s = s[:80]
        return s
    except Exception:
        return default


def dataframe_to_csv_response(df: pd.DataFrame, filename: str = "export.csv"):
    """Stream a CSV for a single DataFrame by writing to a temporary file.

    This avoids building a giant in-memory bytes object for very large tables.
    """
    df = mask_dataframe(df, getattr(current_user, "_get_current_object", lambda: current_user)(), for_export=True)
    mimetype = "text/csv"
    try:
        rows = int(len(df)) if df is not None else 0
    except Exception:
        rows = 0
    _log_export_event(filename, "csv", {"rows": rows})
    tmp = tempfile.NamedTemporaryFile(prefix="wa_export_", suffix=".csv", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()
    try:
        (df if df is not None else pd.DataFrame()).to_csv(tmp_path.as_posix(), index=False)

        @after_this_request
        def _cleanup(resp):  # pragma: no cover - side effect
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
            return resp

        return send_file(tmp_path.as_posix(), as_attachment=True, download_name=filename, mimetype=mimetype)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
        # Fallback to in-memory
        data = (df if df is not None else pd.DataFrame()).to_csv(index=False).encode("utf-8")
        return send_file(BytesIO(data), as_attachment=True, download_name=filename, mimetype=mimetype)


def dataframes_to_xlsx_response(sheets: Dict[str, pd.DataFrame], filename: str = "export.xlsx", threshold_rows: int = 100_000):  # noqa: F811
    """Return a Flask response streaming an XLSX file.

    - If total rows > threshold_rows, writes to a temporary file and streams it via send_file.
    - Otherwise, uses in-memory bytes.
    """
    sheets = mask_export_sheets(sheets, getattr(current_user, "_get_current_object", lambda: current_user)())
    engine = _xlsx_engine_name()
    total_rows = sum(int(len(df)) for df in (sheets or {}).values())
    _log_export_event(filename, "xlsx", {"rows": total_rows, "sheets": list((sheets or {}).keys())})
    mimetype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if not engine:
        data = dataframes_to_xlsx_bytes(sheets)
        return send_file(BytesIO(data), as_attachment=True, download_name=filename, mimetype=mimetype)
    if total_rows > threshold_rows:
        # Write to a temp file on disk to keep memory bounded
        tmp = tempfile.NamedTemporaryFile(prefix="wa_export_", suffix=".xlsx", delete=False)
        tmp_path = Path(tmp.name)
        tmp.close()
        try:
            with _excel_writer(tmp_path.as_posix(), engine) as writer:
                for name, df in sheets.items():
                    safe = str(name)[:31].replace(":", "_").replace("/", "_")
                    (df if df is not None else pd.DataFrame()).to_excel(writer, sheet_name=safe, index=False)
                    _apply_worksheet_formatting(writer, safe, df if df is not None else pd.DataFrame())
                    _demote_openpyxl_formulas(writer, safe)

            @after_this_request
            def _cleanup(resp):  # pragma: no cover - side effect
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass
                return resp

            return send_file(tmp_path.as_posix(), as_attachment=True, download_name=filename, mimetype=mimetype)
        except Exception:
            # Fallback to in-memory if anything goes wrong
            if tmp_path.exists():
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass
    # In-memory path
    data = dataframes_to_xlsx_bytes(sheets)
    return send_file(BytesIO(data), as_attachment=True, download_name=filename, mimetype=mimetype)


def to_excel_bytes(df: pd.DataFrame, sheet_name: str = "Data", instructions: Optional[List[str]] = None) -> bytes:
    """Write a single DataFrame to XLSX bytes.

    - `sheet_name`: the sheet to place the data into (default 'Data').
    - `instructions`: optional text placed into a separate 'Instructions' sheet.
    """
    sheets = {sheet_name: df if df is not None else pd.DataFrame()}
    output = BytesIO()
    with _excel_writer(output, "xlsxwriter") as writer:
        # Write data
        for name, sdf in sheets.items():
            safe = str(name)[:31].replace(":", "_").replace("/", "_")
            sdf.to_excel(writer, sheet_name=safe, index=False)
            _apply_worksheet_formatting(writer, safe, sdf)
            _demote_openpyxl_formulas(writer, safe)

        if instructions:
            instr_name = "Instructions"
            ws = writer.book.add_worksheet(instr_name)
            wrap_format = writer.book.add_format({"text_wrap": True, "valign": "top"})
            colw = 100
            ws.set_column(0, 0, colw)
            for i, line in enumerate(instructions):
                ws.write(i, 0, str(line), wrap_format)
                ws.set_row(i, 20)
    output.seek(0)
    return output.read()


# Formatting helpers (usable in code paths or as Jinja filters)
def fmt_currency(value) -> str:
    try:
        return f"${float(value):,.2f}"
    except Exception:
        return str(value)


def fmt_percent(value, decimals: int = 1) -> str:
    try:
        return f"{float(value):.{decimals}f}%"
    except Exception:
        return str(value)


def fmt_intcomma(value) -> str:
    try:
        return f"{int(float(value)):,}"
    except Exception:
        return str(value)
