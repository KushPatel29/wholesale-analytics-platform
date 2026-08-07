"""Exports (XLSX) using xlsxwriter engine and formatting helpers."""

from __future__ import annotations

from io import BytesIO
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


def _apply_worksheet_formatting(writer, sheet_name: str, df: pd.DataFrame) -> None:
    try:
        wb = writer.book
        ws = writer.sheets[sheet_name]
        # Basic formats
        fmt_curr = wb.add_format({"num_format": "$#,##0.00"})
        fmt_int = wb.add_format({"num_format": "#,##0"})
        fmt_pct = wb.add_format({"num_format": "0.0%"})
        # Column widths heuristic
        for i, col in enumerate(df.columns):
            col_series = df[col]
            # Choose format by column name
            name = str(col).lower()
            f = None
            if any(k in name for k in ["revenue", "cost", "spend", "price", "profit", "amount", "avgprice"]):
                f = fmt_curr
            elif any(k in name for k in ["pct", "percent", "rate"]):
                f = fmt_pct
            elif pd.api.types.is_integer_dtype(col_series) or pd.api.types.is_float_dtype(col_series):
                f = fmt_int
            width = max(10, min(40, int(col_series.astype(str).str.len().quantile(0.9)) + 2))
            ws.set_column(i, i, width, f)
        # Freeze header and autofilter
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
                "<dc:creator>Wholesale Analytics</dc:creator>"
                "<cp:lastModifiedBy>Wholesale Analytics</cp:lastModifiedBy>"
                "</cp:coreProperties>"
            ),
        )
        zf.writestr(
            "docProps/app.xml",
            (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
                'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
                "<Application>Wholesale Analytics</Application>"
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
    with pd.ExcelWriter(output, engine=engine) as writer:
        for name, df in sheets.items():
            # Ensure a safe sheet name (max 31 chars, no special characters)
            safe = str(name)[:31].replace(":", "_").replace("/", "_")
            frame = df if isinstance(df, pd.DataFrame) else pd.DataFrame()
            safe_sheets[safe] = frame
            frame.to_excel(writer, sheet_name=safe, index=False)
            _apply_worksheet_formatting(writer, safe, frame)
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
            with pd.ExcelWriter(tmp_path.as_posix(), engine=engine) as writer:
                for name, df in sheets.items():
                    safe = str(name)[:31].replace(":", "_").replace("/", "_")
                    (df if df is not None else pd.DataFrame()).to_excel(writer, sheet_name=safe, index=False)
                    _apply_worksheet_formatting(writer, safe, df if df is not None else pd.DataFrame())

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
            with pd.ExcelWriter(tmp_path.as_posix(), engine=engine) as writer:
                for name, df in sheets.items():
                    safe = str(name)[:31].replace(":", "_").replace("/", "_")
                    (df if df is not None else pd.DataFrame()).to_excel(writer, sheet_name=safe, index=False)
                    _apply_worksheet_formatting(writer, safe, df if df is not None else pd.DataFrame())

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
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        # Write data
        for name, sdf in sheets.items():
            safe = str(name)[:31].replace(":", "_").replace("/", "_")
            sdf.to_excel(writer, sheet_name=safe, index=False)
            _apply_worksheet_formatting(writer, safe, sdf)

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
