# core/exporter.py

import os
import csv
import base64
import json as json_module
from PyQt5.QtCore import QCoreApplication # type: ignore


def tr(message):
    return QCoreApplication.translate("RockMorph", message)


class RockMorphExporter:
    """
    Reusable export utility for all RockMorph tool panels.
    Handles PNG, JPG, SVG (from Plotly dataURL) and CSV/JSON.
    """

    def __init__(self, iface):
        self.iface = iface

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def save_image(self, data_url: str, path: str) -> None:
        """
        Save a Plotly-generated dataURL to disk.
        Handles both base64 and raw SVG formats automatically.
        """
        try:
            fmt, payload = self._parse_data_url(data_url)

            if fmt == "svg":
                self._write_svg(payload, path)
            else:
                self._write_binary(payload, path)

            self._info(tr(f"Exported to {path}"))

        except Exception as e:
            self._error(tr(f"Export failed: {e}"))

    def save_csv(self, rows: list, headers: list, path: str) -> None:
        """
        Save a list of row dicts to CSV.

        Parameters
        ----------
        rows    : list of dicts — data rows
        headers : list of str  — column names
        path    : str          — output file path
        """
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=headers)
                writer.writeheader()
                writer.writerows(rows)
            self._info(tr(f"CSV exported to {path}"))
        except Exception as e:
            self._error(tr(f"CSV export failed: {e}"))

    def save_json(self, data: dict, path: str) -> None:
        """Save a dict to JSON file."""
        try:
            with open(path, "w", encoding="utf-8") as f:
                json_module.dump(data, f, indent=2, ensure_ascii=False)
            self._info(tr(f"JSON exported to {path}"))
        except Exception as e:
            self._error(tr(f"JSON export failed: {e}"))

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _parse_data_url(self, data_url: str) -> tuple:
        """
        Parse a dataURL into (format, payload).

        Handles:
          data:image/png;base64,<b64data>
          data:image/jpeg;base64,<b64data>
          data:image/svg+xml;base64,<b64data>
          data:image/svg+xml;utf8,<rawsvg>
          data:image/svg+xml;charset=utf-8,<rawsvg>
        """
        if "," not in data_url:
            raise ValueError(tr("Invalid dataURL format."))

        header, payload = data_url.split(",", 1)

        # Detect format
        if "svg" in header:
            fmt = "svg"
        elif "jpeg" in header or "jpg" in header:
            fmt = "jpg"
        else:
            fmt = "png"

        # Detect encoding
        is_base64 = "base64" in header

        return fmt, (payload, is_base64)

    def _write_svg(self, payload: tuple, path: str) -> None:
        """Write SVG — handles both base64 and raw utf8 payloads."""
        raw, is_base64 = payload
        if is_base64:
            # Pad base64 if needed
            padding = 4 - len(raw) % 4
            if padding != 4:
                raw += "=" * padding
            svg_bytes = base64.b64decode(raw)
            with open(path, "wb") as f:
                f.write(svg_bytes)
        else:
            # Raw SVG string — URL-decoded
            from urllib.parse import unquote
            svg_str = unquote(raw)
            with open(path, "w", encoding="utf-8") as f:
                f.write(svg_str)

    def _write_binary(self, payload: tuple, path: str) -> None:
        """Write PNG or JPG from base64 payload."""
        raw, is_base64 = payload
        if is_base64:
            padding = 4 - len(raw) % 4
            if padding != 4:
                raw += "=" * padding
            img_bytes = base64.b64decode(raw)
            with open(path, "wb") as f:
                f.write(img_bytes)
        else:
            raise ValueError(tr("Unexpected non-base64 image format."))

    def _info(self, message: str) -> None:
        self.iface.messageBar().pushInfo("RockMorph", message)

    def _error(self, message: str) -> None:
        self.iface.messageBar().pushWarning("RockMorph", message)