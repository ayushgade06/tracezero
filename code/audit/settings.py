"""Dataset conventions the engine relies on, as named settings (P2). Defaults are the reference dataset's
conventions, so a run without a settings file behaves exactly as before. A settings file is a JSON
object holding any subset of the keys below; see docs/CONFIGURATION.md.

  sql_point_in_time_header    {"label": ..., "value": ...} or null. A SQL dump whose leading comment
                              header has the line "-- <label>: <value>" is a point-in-time snapshot:
                              a resource missing from every copy of one of its tables is read as absent
                              (state.py, R4e). null: no dump is treated that way.
  screenshot_template_marker  text or null. A screenshot is parsed with the admin-console template
                              (ocr.layout) only if a sidebar box contains this text; otherwise it is
                              UNRECOGNISED_LAYOUT and gives context only. null: no screenshot matches.
"""
import json
import re
from dataclasses import dataclass, fields
from pathlib import Path

from audit.parsers import _snake

_HEADER_LABEL = re.compile(r"[A-Za-z][A-Za-z _]{0,40}")  # what parsers.parse_sql accepts as a header label


class SettingsError(ValueError):
    """The settings file cannot be used; the message names the file and every problem."""


@dataclass(frozen=True)
class Settings:
    sql_point_in_time_header: tuple = ("Ghost Data Still Exists", "point-in-time database snapshot")  # (label, value) or None
    screenshot_template_marker: str = "Northstar"  # or None

    @property
    def point_in_time_context(self):
        """-> (context key, value) as parsers.parse_sql stores the header line, or None."""
        h = self.sql_point_in_time_header
        return None if h is None else (_snake(h[0]), h[1])


DEFAULT = Settings()


def load_settings(path):
    """settings JSON -> Settings (unset keys keep their defaults). Raises SettingsError listing every problem."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise SettingsError(f"{path}: cannot read settings file: {e.strerror or e}") from None
    except UnicodeDecodeError as e:
        raise SettingsError(f"{path}: not UTF-8 text ({e.reason} at byte {e.start})") from None
    except json.JSONDecodeError as e:
        raise SettingsError(f"{path}: not valid JSON: {e}") from None
    if not isinstance(data, dict):
        raise SettingsError(f"{path}: must be a JSON object")
    errors, values = [], {}
    known = [f.name for f in fields(Settings)]
    errors += [f"{path}: unknown setting {k!r} (known: {known})" for k in data if k not in known]
    if "sql_point_in_time_header" in data:
        h = data["sql_point_in_time_header"]
        if h is None:
            values["sql_point_in_time_header"] = None
        elif (isinstance(h, dict) and set(h) == {"label", "value"} and all(isinstance(v, str) for v in h.values())
              and _HEADER_LABEL.fullmatch(h["label"].strip()) and h["value"].strip()):
            values["sql_point_in_time_header"] = (h["label"].strip(), h["value"].strip())
        else:
            errors.append(f"{path}: sql_point_in_time_header must be null or {{\"label\": ..., \"value\": ...}} with a "
                          f"label of letters, spaces and underscores (starting with a letter, at most 41 characters) "
                          f"and a non-empty value; got {h!r}")
    if "screenshot_template_marker" in data:
        m = data["screenshot_template_marker"]
        if m is None or (isinstance(m, str) and m.strip()):
            values["screenshot_template_marker"] = m
        else:
            errors.append(f"{path}: screenshot_template_marker must be null or non-empty text; got {m!r}")
    if errors:
        raise SettingsError("\n".join(errors))
    return Settings(**values)
