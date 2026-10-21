"""RER Explorer: an on-demand IMF real exchange rate app.

Deploy with: streamlit run app.py
Run a connection check with: python app.py --check-live
No observations are downloaded until Load data is selected. Country-list
metadata loads when the app opens. No synthetic data is used by this app.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
from email.utils import parsedate_to_datetime
from io import StringIO
from http.client import HTTPException
import json
import os
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

import numpy as np
import pandas as pd

BASE_URL = "https://api.imf.org/external/sdmx/3.0"
AGENCY = "IMF.STA"
EARLIEST_MONTH = "1976-01"
CSV_ACCEPT = "application/vnd.sdmx.data+csv;version=2.0.0;labels=id"
STRUCTURE_ACCEPT = "application/vnd.sdmx.structure+xml;version=3.0.0"
EXPECTED_DIMENSIONS = {
    "CPI": ("COUNTRY", "INDEX_TYPE", "COICOP_1999", "TYPE_OF_TRANSFORMATION", "FREQUENCY"),
    "ER": ("COUNTRY", "INDICATOR", "TYPE_OF_TRANSFORMATION", "FREQUENCY"),
}


class IMFError(RuntimeError):
    """An IMF connection, response or metadata error."""


class NoDataError(IMFError):
    """The requested monthly series has no observations."""


class HistoryError(ValueError):
    """The sample spans an unresolved unit, currency or price-index break."""


def month(value: str | pd.Period) -> pd.Period:
    """Parse both YYYY-MM and the IMF SDMX form YYYY-Mmm; reject other frequencies."""
    if isinstance(value, pd.Period):
        if value.freqstr != "M":
            raise ValueError("A monthly period is required.")
        return value
    normalized = str(value).strip()
    normalized = re.sub(r"^(\d{4})-?M(\d{1,2})$", r"\1-\2", normalized)
    if not re.fullmatch(r"\d{4}-\d{1,2}", normalized):
        raise ValueError(f"Expected a month such as 2020-01; received {value!r}.")
    try:
        return pd.Period(normalized, freq="M")
    except ValueError as exc:
        raise ValueError(f"Invalid calendar month: {value!r}.") from exc


def country_code(value: str) -> str:
    value = str(value).strip().upper()
    if not re.fullmatch(r"[A-Z]{3}|G163", value):
        raise ValueError("Choose an IMF three-letter country code or G163 (Euro area).")
    return value


@dataclass
class MonthlySeries:
    country: str
    flow: str
    key: str
    observations: pd.DataFrame
    source: str
    retrieved_at: str


@dataclass
class PairData:
    country1: str
    country2: str
    observations: pd.DataFrame
    inputs: dict[str, MonthlySeries]


@dataclass
class SampleResult:
    observations: pd.DataFrame
    rer_star: float
    valid_months: int
    total_months: int
    first_valid: str | None
    last_valid: str | None
    warnings: tuple[str, ...]


def parse_csv(payload: bytes, country: str, flow: str, index_type: str = "CPI") -> pd.DataFrame:
    """Parse one IMF series, retaining raw values and all supplied attributes.

    Unexpected series or duplicate months are errors, never averaged together.
    Unknown nonnumeric values are errors; explicit missing markers remain NaN.
    """
    try:
        frame = pd.read_csv(StringIO(payload.decode("utf-8-sig")), dtype=str,
                            keep_default_na=False)
    except (UnicodeError, pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
        raise IMFError("IMF did not return a readable SDMX CSV response.") from exc
    frame.columns = [str(col).strip().upper() for col in frame.columns]
    if not {"TIME_PERIOD", "OBS_VALUE"}.issubset(frame.columns):
        raise IMFError("IMF response lacks TIME_PERIOD or OBS_VALUE; CSV format must be checked.")
    # IMF represents an empty SDMX data set as a structure-only CSV row.
    empty_time = frame["TIME_PERIOD"].str.strip().eq("")
    empty_value = frame["OBS_VALUE"].str.strip().eq("")
    frame = frame.loc[~(empty_time & empty_value)].copy()
    if frame.empty:
        raise NoDataError(f"No monthly {flow} observations for {country}.")
    expected = {"COUNTRY": country, "FREQUENCY": "M", "FREQ": "M"}
    expected.update({"INDEX_TYPE": index_type, "COICOP_1999": "_T", "TYPE_OF_TRANSFORMATION": "IX"}
                    if flow == "CPI" else
                    {"INDICATOR": "XDC_USD", "TYPE_OF_TRANSFORMATION": "PA_RT"})
    for name, value in expected.items():
        if name in frame and not frame[name].eq(value).all():
            raise IMFError(f"IMF returned an unexpected {name}; expected {value}.")
    try:
        index = pd.PeriodIndex([month(item) for item in frame["TIME_PERIOD"]], freq="M", name="month")
    except ValueError as exc:
        raise IMFError("IMF returned a nonmonthly or invalid observation date.") from exc
    if index.has_duplicates:
        raise IMFError("Multiple observations for one month; check series dimensions or IMF revisions.")
    raw = frame["OBS_VALUE"].str.strip()
    missing = raw.str.upper().isin(["", "NA", "N/A", "NAN", "NULL", ":", ".."])
    numeric = pd.to_numeric(raw.where(~missing), errors="coerce")
    if (numeric.isna() & ~missing).any():
        raise IMFError("Unexpected nonnumeric IMF observation; inspect the response.")
    scale_name = "SCALE" if "SCALE" in frame else "UNIT_MULT" if "UNIT_MULT" in frame else None
    scale = pd.Series(0.0, index=frame.index)
    if scale_name:
        scale = pd.to_numeric(frame[scale_name].replace("", "0"), errors="coerce")
        if scale.isna().any() or (~np.isfinite(scale)).any() or (scale.abs() > 300).any():
            raise IMFError("Invalid IMF observation scale.")
    result = frame.copy()
    result["reported_value"] = numeric
    result["value"] = numeric * (10.0 ** scale)
    result.index = index
    return result.sort_index()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _english_name(element: ET.Element) -> str:
    names = [child for child in element if _local(child.tag) == "Name"]
    return next((child.text or "" for child in names
                 if child.get("{http://www.w3.org/XML/1998/namespace}lang") == "en"),
                (names[0].text or "") if names else element.get("id", ""))


def parse_country_catalog(payload: bytes) -> dict[str, str]:
    """Read the IMF country codelist without downloading unrelated metadata."""
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise IMFError("IMF did not return readable country-list XML.") from exc
    lists = [node for node in root.iter()
             if _local(node.tag) == "Codelist" and node.get("id") == "CL_COUNTRY"]
    if len(lists) != 1:
        raise IMFError("IMF country codelist is missing or ambiguous.")
    countries = {}
    for node in lists[0].iter():
        if _local(node.tag) == "Code" and re.fullmatch(r"[A-Z]{3}|G163", node.get("id", "")):
            countries[node.get("id")] = _english_name(node)
    if not countries:
        raise IMFError("IMF returned an empty country codelist.")
    countries.setdefault("G163", "Euro area")
    return dict(sorted(countries.items(), key=lambda item: item[1].casefold()))


def parse_structure(payload: bytes, flow: str, country_catalog: dict[str, str] | None = None) -> dict:
    """Inspect the actual DSD and its referenced country codelist (metadata only)."""
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise IMFError("IMF did not return readable SDMX structure XML.") from exc
    dataflow = next((node for node in root.iter()
                     if _local(node.tag) == "Dataflow" and node.get("id") == flow), None)
    if dataflow is None:
        raise IMFError(f"Dataflow {flow} was not present in the structure response.")
    structures = [node for node in root.iter() if _local(node.tag) == "DataStructure"]
    dsd = next((node for node in structures if node.get("id") == "DSD_" + flow), None)
    if dsd is None and len(structures) == 1:
        dsd = structures[0]
    if dsd is None:
        raise IMFError("Data structure not included; request references=all.")
    dimensions = sorted((node for node in dsd.iter() if _local(node.tag) == "Dimension" and node.get("id")),
                        key=lambda node: int(node.get("position", "0")))
    ids = tuple(node.get("id") for node in dimensions)
    if ids != EXPECTED_DIMENSIONS[flow]:
        raise IMFError(f"IMF {flow} dimensions changed: {ids}; update the series-key mapping.")
    if country_catalog is not None:
        return {"flow": flow, "version": dataflow.get("version"),
                "dimensions": ids, "countries": dict(country_catalog)}
    country_dim = next(node for node in dimensions if node.get("id") == "COUNTRY")
    reference = next((node for node in country_dim.iter() if _local(node.tag) == "Ref"
                      and node.get("class") in (None, "Codelist")), None)
    agency, identifier, version = None, None, None
    if reference is not None:
        agency, identifier, version = (reference.get("agencyID"), reference.get("id"),
                                       reference.get("version"))
    else:
        # SDMX 3.0 often defines COUNTRY's enumeration on its referenced
        # concept's CoreRepresentation, using a URN rather than a Ref element.
        holders = [country_dim]
        for scheme in root.iter():
            if _local(scheme.tag) == "ConceptScheme" and scheme.get("id") == "CS_MASTER_DATA":
                holders.extend(child for child in scheme
                               if _local(child.tag) == "Concept" and child.get("id") == "COUNTRY")
        for holder in holders:
            for node in holder.iter():
                if _local(node.tag) == "Enumeration" and node.text:
                    match = re.search(r"Codelist=([^:]+):([^(]+)\(([^)]+)\)", node.text)
                    if match:
                        agency, identifier, version = match.groups()
                        break
            if identifier:
                break
    if not identifier:
        raise IMFError("Country codelist reference not found in the IMF DSD or concept scheme.")
    codelists = [node for node in root.iter() if _local(node.tag) == "Codelist"
                 and node.get("id") == identifier
                 and (not agency or node.get("agencyID") == agency)
                 and (not version or "+" in version or node.get("version") == version)]
    if not codelists:
        raise IMFError("Country codelist is missing in IMF metadata.")
    def version_key(node: ET.Element) -> tuple:
        return tuple(int(part) if part.isdigit() else 0 for part in node.get("version", "0").split("."))
    codelists.sort(key=version_key, reverse=True)
    countries = {}
    for node in codelists[0].iter():
        if _local(node.tag) == "Code" and re.fullmatch(r"[A-Z]{3}|G163", node.get("id", "")):
            countries[node.get("id")] = _english_name(node)
    if not countries:
        raise IMFError("No countries found in the IMF country codelist.")
    return {"flow": flow, "version": dataflow.get("version"),
            "dimensions": ids, "countries": countries}


class IMFClient:
    """On-demand IMF client with bounded, per-instance in-memory caching.

    The UI keeps one instance per user session across reruns. Country metadata is
    cached for a week; observations for six hours. refresh=True forces a fetch.
    No bulk observation downloads, stale-on-error substitution or interpolation.
    Set IMF_API_KEY in the environment only if your IMF access requires a key.
    """
    def __init__(self, timeout: float = 25, retries: int = 2, api_key: str | None = None):
        self.api_key = api_key if api_key is not None else os.environ.get("IMF_API_KEY", "")
        self.timeout = timeout
        self.retries = retries
        self._cache: dict[tuple, tuple[float, bytes]] = {}

    def clear_cache(self) -> None:
        self._cache.clear()

    def _get(self, path: str, params: dict, accept: str, *, refresh: bool = False,
             ttl: float = 21600) -> tuple[bytes, str]:
        url = BASE_URL + "/" + path + ("?" + urlencode(params) if params else "")
        key = (url, accept)
        cached = self._cache.get(key)
        if not refresh and cached and time.monotonic() - cached[0] < ttl:
            return cached[1], url
        headers = {"Accept": accept}
        if self.api_key:
            headers["Ocp-Apim-Subscription-Key"] = self.api_key
        for attempt in range(self.retries + 1):
            try:
                with urlopen(Request(url, headers=headers), timeout=self.timeout) as response:
                    if response.status == 204:
                        raise NoDataError("IMF returned no observations (HTTP 204).")
                    if response.headers.get("Link") and 'rel="next"' in response.headers["Link"]:
                        raise IMFError("IMF paginated this response; pagination support must be added before use.")
                    data = response.read()
                    if not data.strip():
                        raise NoDataError("IMF returned an empty response.")
                    self._cache[key] = (time.monotonic(), data)
                    # Bound the number of cached payloads; oldest inserted items are evicted.
                    while len(self._cache) > 64:
                        self._cache.pop(next(iter(self._cache)))
                    return data, url
            except HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == self.retries:
                    message = {
                        400: "Request rejected; verify IMF series dimensions and time filters.",
                        401: "IMF authentication required; check configured API access.",
                        403: "Access denied by IMF or the network.",
                        404: "Series or endpoint not found, or access masked by the gateway; do not assume missing data.",
                        406: "Requested SDMX CSV/XML format is unavailable; verify IMF supported formats.",
                        413: "IMF request too large; split the requested history.",
                        429: "IMF rate limit reached; retry later.",
                    }.get(exc.code, "IMF service request failed.")
                    raise IMFError(f"HTTP {exc.code}: {message}") from exc
                delay = min(2 ** attempt, 5)
                retry_after = exc.headers.get("Retry-After", "")
                if retry_after:
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        try:
                            delay = parsedate_to_datetime(retry_after).timestamp() - time.time()
                        except (ValueError, TypeError):
                            pass
                    if delay > 5:
                        raise IMFError("IMF requested a longer retry delay; try again later.") from exc
                time.sleep(max(0, delay))
            except (URLError, TimeoutError, OSError, HTTPException) as exc:
                if attempt == self.retries:
                    raise IMFError("Could not reach IMF. Check network access and try again.") from exc
                time.sleep(min(2 ** attempt, 5))
        raise IMFError("IMF request failed.")

    def structure(self, flow: str, *, refresh: bool = False) -> dict:
        if flow not in EXPECTED_DIMENSIONS:
            raise ValueError("Only CPI and ER are used by this app.")
        data, _ = self._get(f"structure/dataflow/{AGENCY}/{flow}/+", {"references": "children"},
                            STRUCTURE_ACCEPT, refresh=refresh, ttl=604800)
        return parse_structure(data, flow, country_catalog=self.countries())

    def countries(self, *, refresh: bool = False) -> dict[str, str]:
        # This downloads only a small metadata codelist, not country observations.
        data, _ = self._get("structure/codelist/IMF/CL_COUNTRY/+", {},
                            STRUCTURE_ACCEPT, refresh=refresh, ttl=604800)
        return parse_country_catalog(data)

    def series(self, country: str, flow: str, *, start: str = EARLIEST_MONTH,
               end: str | None = None, refresh: bool = False) -> MonthlySeries:
        code = country_code(country)
        first = month(start)
        last = month(end) if end else pd.Period(datetime.now(ZoneInfo("America/Chicago")).date(), freq="M")
        if first < month(EARLIEST_MONTH) or first > last:
            raise ValueError("Start must be January 1976 or later and no later than end.")
        if flow not in EXPECTED_DIMENSIONS:
            raise ValueError("Only CPI and ER are used by this app.")
        structure = self.structure(flow, refresh=refresh)
        if code not in structure["countries"]:
            raise NoDataError(f"{code} is not listed in IMF {flow} metadata.")
        # The Euro area publishes HICP, not a national CPI-labelled series.
        # Try CPI first, then an explicitly labelled HICP only for G163.
        types = ("CPI", "HICP") if code == "G163" and flow == "CPI" else ("CPI",)
        version = structure["version"] or "+"
        for index_type in types:
            key = (f"{code}.{index_type}._T.IX.M" if flow == "CPI"
                   else f"{code}.XDC_USD.PA_RT.M")
            try:
                data, url = self._get(f"data/dataflow/{AGENCY}/{flow}/{version}/{key}",
                                     {"c[TIME_PERIOD]": f"ge:{first}+le:{last}"}, CSV_ACCEPT,
                                     refresh=refresh)
                frame = parse_csv(data, code, flow, index_type=index_type)
                frame = frame.loc[(frame.index >= first) & (frame.index <= last)].copy()
                if frame.empty:
                    raise NoDataError(f"No monthly {flow} observations for {code} in this period.")
                return MonthlySeries(code, flow, key, frame, url,
                                     pd.Timestamp.now(tz="UTC").isoformat())
            except NoDataError:
                if index_type == types[-1]:
                    raise
        raise NoDataError(f"No monthly {flow} observations for {code}.")



def load_pair(client: IMFClient, country1: str = "USA", country2: str = "GBR", *,
              start: str = EARLIEST_MONTH, end: str | None = None,
              refresh: bool = False) -> PairData:
    """Load only the selected economies, joining on an unfilled monthly grid."""
    country1, country2 = country_code(country1), country_code(country2)
    first = month(start)
    last = month(end) if end else pd.Period(datetime.now(ZoneInfo("America/Chicago")).date(), freq="M")
    if first < month(EARLIEST_MONTH) or first > last:
        raise ValueError("Invalid retrieval period.")
    inputs: dict[str, MonthlySeries] = {}
    loaded: dict[tuple[str, str], MonthlySeries] = {}
    def retrieve(code: str, flow: str) -> MonthlySeries:
        try:
            return client.series(code, flow, start=str(first), end=str(last), refresh=refresh)
        except NoDataError as exc:
            # An explicitly unavailable series can coexist with another available
            # series (e.g. nominal FX with no CPI). Connection errors still fail.
            empty = pd.DataFrame({"value": pd.Series(dtype=float)},
                                 index=pd.PeriodIndex([], freq="M", name="month"))
            return MonthlySeries(code, flow, "", empty, str(exc), "")
    for code in dict.fromkeys([country1, country2]):
        loaded[(code, "CPI")] = retrieve(code, "CPI")
        if code == "USA":
            # USD per USD is the exact identity 1, not an estimated or forward-filled IMF value.
            identity = pd.DataFrame({"value": 1.0, "reported_value": 1.0, "CURRENCY": "USD",
                                     "SOURCE_NOTE": "USD per USD = 1 (currency identity)"},
                                    index=pd.period_range(first, last, freq="M", name="month"))
            loaded[(code, "ER")] = MonthlySeries(code, "ER", "USD_PER_USD_IDENTITY", identity,
                                                  "Currency identity, not downloaded", "")
        else:
            loaded[(code, "ER")] = retrieve(code, "ER")
    observations = pd.DataFrame(index=pd.period_range(first, last, freq="M", name="month"))
    for column, code, flow in [("cpi_1", country1, "CPI"), ("cpi_2", country2, "CPI"),
                               ("fx_1_lcu_per_usd", country1, "ER"), ("fx_2_lcu_per_usd", country2, "ER")]:
        item = loaded[(code, flow)]
        inputs[column] = item
        observations[column] = item.observations["value"]
        for attr in item.observations.columns:
            if attr not in ("value",):
                observations[f"{column}__{attr}"] = item.observations[attr]
    return PairData(country1, country2, observations, inputs)


def calculate_sample(pair: PairData, start: str, end: str) -> SampleResult:
    """Apply the chosen sample first, then recompute its RER mean and real index.

    Missing, zero, negative and infinite inputs never enter the RER mean.
    Nominal FX is valid independently of CPI availability. No interpolation.
    Changes in reported currency/unit/reference period or explicit break flags
    stop calculation across a potential discontinuity; no automatic splicing.
    Unreported historical breaks cannot be detected mechanically.
    """
    first, last = month(start), month(end)
    if first > last:
        raise ValueError("Start month must not be after end month.")
    if pair.observations.empty or first < pair.observations.index.min() or last > pair.observations.index.max():
        raise ValueError("Selected dates are outside the retrieved monthly grid.")
    frame = pair.observations.loc[first:last].copy()
    columns = ["cpi_1", "cpi_2", "fx_1_lcu_per_usd", "fx_2_lcu_per_usd"]
    warnings = []
    for column in columns:
        if column not in frame:
            raise ValueError(f"Missing calculation input: {column}.")
        numeric = pd.to_numeric(frame[column], errors="coerce")
        valid = numeric.gt(0) & np.isfinite(numeric)
        frame[column] = numeric
        frame[f"{column}_valid"] = valid
        for suffix in ("CURRENCY", "CURRENCY_CODE", "UNIT", "COMMON_REFERENCE_PERIOD", "BASE_YEAR"):
            attr = f"{column}__{suffix}"
            if attr in frame:
                distinct = frame.loc[valid, attr].dropna().astype(str).str.strip()
                distinct = distinct[distinct.ne("")]
                if distinct.nunique() > 1:
                    raise HistoryError(f"{column}: {suffix} changes inside the sample; verify and harmonize units first.")
        status_column = next((f"{column}__{name}" for name in ("OBS_STATUS", "STATUS")
                              if f"{column}__{name}" in frame), None)
        if status_column:
            statuses = frame[status_column].fillna("").astype(str).str.upper()
            # A break at the first valid observation has no earlier sample segment to bridge.
            valid_indices = frame.index[valid]
            if len(valid_indices) and ((statuses == "B") & valid & (frame.index > valid_indices[0])).any():
                raise HistoryError(f"{column}: IMF flags a break inside the sample; narrow the dates or review the series.")
            if statuses.ne("").any():
                warnings.append(f"{column}: IMF observation status attributes retained for review.")
    fx_valid = frame["fx_1_lcu_per_usd_valid"] & frame["fx_2_lcu_per_usd_valid"]
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        frame["nominal_s"] = (frame["fx_2_lcu_per_usd"] / frame["fx_1_lcu_per_usd"]).where(fx_valid)
        frame["nominal_s"] = frame["nominal_s"].where(np.isfinite(frame["nominal_s"]) & frame["nominal_s"].gt(0))
        valid = frame[[f"{name}_valid" for name in columns]].all(axis=1)
        frame["rer"] = (frame["nominal_s"] * frame["cpi_1"] / frame["cpi_2"]).where(valid)
        frame["rer"] = frame["rer"].where(np.isfinite(frame["rer"]) & frame["rer"].gt(0))
    frame["valid_rer"] = frame["rer"].notna()
    count = int(frame["valid_rer"].sum())
    real_values = frame.loc[frame["valid_rer"], "rer"]
    # Scaling avoids overflow while computing the arithmetic mean of finite values.
    maximum = float(real_values.max()) if count else 1.0
    mean = float((real_values / maximum).mean() * maximum) if count else float("nan")
    frame["rer_star"] = mean
    frame["real_index"] = (frame["rer"] / mean) * 100.0 if count else np.nan
    frame["country_1"] = pair.country1
    frame["country_2"] = pair.country2
    if not count:
        warnings.append("No valid real-exchange-rate observations in the selected period.")
    # Preserve IMF-reported currency conventions. Do not apply guessed conversion factors.
    valid_dates = frame.index[frame["valid_rer"]]
    return SampleResult(frame, mean, count, len(frame),
                        str(valid_dates.min()) if count else None,
                        str(valid_dates.max()) if count else None,
                        tuple(warnings))



# ---------------------------- presentation and export -------------------------

def display_month(value: str | pd.Period) -> str:
    return month(value).strftime("%b %Y")


def currency_name(pair: PairData, which: int) -> str:
    """Prefer IMF currency metadata; otherwise use an explicitly generic unit."""
    code = pair.country1 if which == 1 else pair.country2
    key = f"fx_{which}_lcu_per_usd"
    item = pair.inputs.get(key)
    if item:
        for attr in ("CURRENCY", "CURRENCY_CODE"):
            if attr in item.observations:
                entries = item.observations[attr].dropna().astype(str).str.strip()
                entries = entries[entries.ne("")]
                unique = list(entries.unique())
                if len(unique) == 1 and re.fullmatch(r"[A-Z]{3}", unique[0]):
                    return unique[0]
    # Only these stable units are needed for the requested default pair and area.
    return {"USA": "USD", "GBR": "GBP", "G163": "EUR"}.get(code, f"{code} currency units")


def chart_spec(result: SampleResult, mode: str, nominal_units: str) -> dict:
    """A Plotly figure dictionary, keeping nominal units and real-index axes apart."""
    if mode not in ("Real", "Nominal", "Both"):
        raise ValueError("Chart mode must be Real, Nominal, or Both.")
    frame = result.observations
    x = frame.index.to_timestamp().strftime("%Y-%m-%d").tolist()
    traces = []
    def points(column: str) -> list:
        return [float(value) if pd.notna(value) and np.isfinite(value) else None
                for value in frame[column]]
    if mode in ("Real", "Both"):
        traces.append({
            "type": "scatter", "mode": "lines+markers" if result.valid_months == 1 else "lines",
            "x": x, "y": points("real_index"), "name": "Real index",
            "line": {"color": "#3b82f6", "width": 2.5},
            "marker": {"size": 6}, "connectgaps": False, "yaxis": "y",
            "hovertemplate": "Real index: %{y:.2f}<extra></extra>",
        })
    if mode in ("Nominal", "Both"):
        traces.append({
            "type": "scatter",
            "mode": "lines+markers" if frame["nominal_s"].notna().sum() == 1 else "lines",
            "x": x, "y": points("nominal_s"), "name": f"Nominal · {nominal_units}",
            "line": {"color": "#d97706", "width": 2, "dash": "dash"},
            "marker": {"size": 6}, "connectgaps": False,
            "yaxis": "y2" if mode == "Both" else "y",
            "hovertemplate": "Nominal: %{y:.6g}<extra></extra>",
        })
    layout = {
        "height": 450, "hovermode": "x unified",
        "margin": {"l": 65, "r": 85 if mode == "Both" else 20, "t": 35, "b": 50},
        "paper_bgcolor": "rgba(0,0,0,0)", "plot_bgcolor": "rgba(0,0,0,0)",
        "legend": {"orientation": "h", "y": 1.13, "x": 0, "font": {"size": 12}},
        "xaxis": {"title": {"text": "Month"}, "type": "date", "showgrid": False,
                  "hoverformat": "%b %Y", "zeroline": False},
        "yaxis": {
            "title": {"text": "Real index (sample average = 100)" if mode != "Nominal" else nominal_units},
            "zeroline": False, "gridcolor": "rgba(128,128,128,0.15)",
            "tickformat": ".1f" if mode != "Nominal" else ".5g",
        },
        "uirevision": f"{mode}:{frame.index.min()}:{frame.index.max()}",
    }
    if mode == "Both":
        layout["yaxis2"] = {"title": {"text": nominal_units}, "overlaying": "y",
                            "side": "right", "showgrid": False, "zeroline": False,
                            "tickformat": ".5g"}
    if mode in ("Real", "Both") and result.valid_months:
        layout["shapes"] = [{
            "type": "line", "xref": "paper", "x0": 0, "x1": 1,
            "yref": "y", "y0": 100, "y1": 100,
            "line": {"color": "rgba(128,128,128,0.5)", "width": 1, "dash": "dot"},
            "layer": "below",
        }]
    return {"data": traces, "layout": layout}


def export_frame(result: SampleResult, pair: PairData, names: dict[str, str]) -> pd.DataFrame:
    """Export exactly the selected sample, including missing months and provenance."""
    frame = result.observations.copy()
    frame.index = frame.index.astype(str)
    frame.index.name = "month"
    frame = frame.reset_index()
    frame["country_1_name"] = names.get(pair.country1, pair.country1)
    frame["country_2_name"] = names.get(pair.country2, pair.country2)
    frame["nominal_units"] = f"{currency_name(pair, 2)} per {currency_name(pair, 1)}"
    frame["sample_start"] = frame["month"].iloc[0]
    frame["sample_end"] = frame["month"].iloc[-1]
    frame["valid_months_in_sample"] = result.valid_months
    inputs = ["cpi_1", "cpi_2", "fx_1_lcu_per_usd", "fx_2_lcu_per_usd"]
    def exclusion(row: pd.Series) -> str:
        invalid = [name for name in inputs if not row[f"{name}_valid"]]
        if invalid:
            return "; ".join(f"{name}: missing, nonpositive, or nonfinite" for name in invalid)
        return "" if row["valid_rer"] else "Calculated RER is nonfinite or nonpositive"
    frame["exclusion_reason"] = frame.apply(exclusion, axis=1)
    for key, item in pair.inputs.items():
        frame[f"{key}__series_key"] = item.key
        frame[f"{key}__source"] = item.source
        frame[f"{key}__retrieved_at_utc"] = item.retrieved_at
    first = [
        "month", "country_1", "country_1_name", "country_2", "country_2_name",
        "nominal_units", "cpi_1", "cpi_2", "fx_1_lcu_per_usd", "fx_2_lcu_per_usd",
        "nominal_s", "rer", "rer_star", "real_index", "valid_rer", "exclusion_reason",
        "sample_start", "sample_end", "valid_months_in_sample",
    ]
    return frame[first + [column for column in frame if column not in first]]


def csv_bytes(frame: pd.DataFrame) -> bytes:
    """UTF-8 BOM supports Excel; empty fields represent unavailable calculations."""
    return frame.to_csv(index=False, na_rep="", lineterminator="\n").encode("utf-8-sig")


def _api_key(st) -> str:
    value = os.environ.get("IMF_API_KEY", "")
    if value:
        return value
    try:
        return str(st.secrets.get("IMF_API_KEY", "") or "")
    except (FileNotFoundError, KeyError):
        return ""


def run_app(client: IMFClient | None = None) -> None:
    import streamlit as st

    st.set_page_config(page_title="RER Explorer", page_icon="📈", layout="wide",
                       initial_sidebar_state="expanded")
    st.markdown(
        "<style>.block-container{max-width:1350px;padding-top:2.1rem;"
        "padding-bottom:2rem}div[data-testid='stMetricValue']{font-weight:400}"
        "div[data-testid='stSidebar'] h2{font-size:1.05rem}</style>",
        unsafe_allow_html=True,
    )
    st.caption("MONTHLY · BILATERAL · IMF")
    st.title("Real Exchange Rate")
    st.write("Compare nominal exchange rates and movements in relative prices.")

    if client is None:
        # Keep only plain cache data across reruns. Reusing an instance of a
        # script-defined class can retain exception classes from an older run.
        client = IMFClient(api_key=_api_key(st))
        if "_imf_cache" not in st.session_state:
            st.session_state["_imf_cache"] = {}
        client._cache = st.session_state["_imf_cache"]
    try:
        with st.spinner("Loading the IMF country list…"):
            names = client.countries()
    except IMFError as exc:
        st.error("The IMF country list could not be loaded.")
        st.write(str(exc))
        st.caption("Check the app's internet access. If your IMF access requires a subscription key, "
                   "add IMF_API_KEY in Streamlit Secrets. A missing series and an access error "
                   "are different conditions; the app does not replace data on failure.")
        if st.button("Retry IMF connection", type="primary"):
            client.clear_cache()
            st.rerun()
        st.stop()

    options = list(names)
    if not options:
        st.error("IMF returned an empty country list.")
        st.stop()

    with st.sidebar:
        st.markdown("### 📈 RER Explorer")
        st.header("Choose countries")
        with st.form("countries", border=False):
            c1 = st.selectbox("Country 1", options,
                              index=options.index("USA") if "USA" in options else 0,
                              format_func=lambda code: f"{names[code]} ({code})", key="country1")
            c2 = st.selectbox("Country 2", options,
                              index=options.index("GBR") if "GBR" in options else 0,
                              format_func=lambda code: f"{names[code]} ({code})", key="country2")
            submitted = st.form_submit_button("Load data", type="primary", width="stretch")

    def fetch_pair(selected: tuple[str, str], refresh: bool = False) -> None:
        had_pair = "_loaded_pair" in st.session_state
        if refresh:
            client.clear_cache()
        try:
            with st.spinner(f"Loading monthly IMF data for {names[selected[0]]} and {names[selected[1]]}…"):
                loaded = load_pair(client, *selected)
            st.session_state["_loaded_pair"] = loaded
            st.session_state["_loaded_names"] = {**names}
            st.session_state.pop("_load_error", None)
            if not had_pair:
                nonempty = loaded.observations[
                    ["cpi_1", "cpi_2", "fx_1_lcu_per_usd", "fx_2_lcu_per_usd"]].notna().all(axis=1)
                available = loaded.observations.index[nonempty]
                if len(available):
                    st.session_state["sample_end"] = str(available.max())
        except (IMFError, ValueError) as exc:
            st.session_state["_load_error"] = str(exc)

    if submitted:
        fetch_pair((c1, c2))
    # Render after the load action so Refresh is enabled immediately on success.
    with st.sidebar:
        refresh = st.button("Refresh loaded pair", width="stretch",
                            disabled="_loaded_pair" not in st.session_state,
                            help="Fetch fresh IMF observations for the pair currently displayed.")
    if refresh:
        loaded = st.session_state["_loaded_pair"]
        fetch_pair((loaded.country1, loaded.country2), refresh=True)

    pair = st.session_state.get("_loaded_pair")
    current = pd.Period(datetime.now(ZoneInfo("America/Chicago")).date(), freq="M")
    months = [str(item) for item in pd.period_range(EARLIEST_MONTH, current, freq="M")]
    if "sample_start" not in st.session_state:
        st.session_state["sample_start"] = "2000-01" if "2000-01" in months else months[0]
    if "sample_end" not in st.session_state:
        st.session_state["sample_end"] = months[-1]
    # Keep saved sessions safe when crossing a month boundary or changing retrieval coverage.
    if pair is not None:
        months = [str(item) for item in pair.observations.index]
    for key, fallback in [("sample_start", months[0]), ("sample_end", months[-1])]:
        if st.session_state.get(key) not in months:
            st.session_state[key] = fallback
    with st.sidebar:
        st.divider()
        st.header("Sample period")
        start = st.selectbox("Start month", months, format_func=display_month, key="sample_start")
        end = st.selectbox("End month", months, format_func=display_month, key="sample_end")
        st.caption("The real index is centered on the average of the selected sample.")
        st.caption("Data requested from January 1976; availability varies by country and series.")

    if "_load_error" in st.session_state:
        st.error("The requested IMF data could not be loaded: " + st.session_state["_load_error"])
        if pair is not None:
            st.info("The previous successfully loaded pair remains displayed below.")
    if pair is None:
        st.info("Choose two countries and select Load data to begin.")
        with st.expander("How the app works"):
            st.write("Choose the sample months after loading. The real index averages 100 over valid "
                     "months in that sample. Nominal exchange rates stay in currency units.")
        st.stop()

    loaded_names = st.session_state.get("_loaded_names", names)
    if (c1, c2) != (pair.country1, pair.country2):
        st.info("The chart shows the loaded pair. Select Load data to apply the new country selection.")
    st.subheader(f"{loaded_names.get(pair.country1, pair.country1)} → "
                 f"{loaded_names.get(pair.country2, pair.country2)}")
    st.caption(f"{display_month(start)} – {display_month(end)} · "
               f"S = {currency_name(pair, 2)} per 1 {currency_name(pair, 1)}")
    if month(start) > month(end):
        st.error("Start month must be no later than end month.")
        st.stop()
    try:
        result = calculate_sample(pair, start, end)
    except HistoryError as exc:
        st.error("The selected period spans a reported currency, unit, or price-index break.")
        st.write(str(exc))
        st.info("Choose dates within one consistent segment before comparing real exchange rates.")
        raw = pair.observations.loc[month(start):month(end)].copy()
        raw.index = raw.index.astype(str)
        st.download_button("Download raw selected observations", csv_bytes(raw.reset_index()),
                           file_name=f"rer_raw_{pair.country1}_{pair.country2}_{start}_{end}.csv",
                           mime="text/csv", on_click="ignore")
        st.stop()
    except ValueError as exc:
        st.error(str(exc))
        st.stop()

    if any(".HICP." in item.key for item in pair.inputs.values()):
        st.caption("Euro-area prices use IMF's all-items Harmonised Index of Consumer Prices (HICP).")
    frame = result.observations
    unit = f"{currency_name(pair, 2)} per {currency_name(pair, 1)}"
    real = frame.loc[frame["valid_rer"]]
    nominal = frame.loc[frame["nominal_s"].notna()]
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Latest real index", f"{real['real_index'].iloc[-1]:.1f}" if len(real) else "—")
        st.caption("Sample average = 100" + (f" · {display_month(real.index[-1])}" if len(real) else ""))
    with col2:
        st.metric("Latest nominal rate", f"{nominal['nominal_s'].iloc[-1]:.6g}" if len(nominal) else "—")
        st.caption(unit + (f" · {display_month(nominal.index[-1])}" if len(nominal) else ""))
    with col3:
        st.metric("Valid months", f"{result.valid_months:,}")
        st.caption(f"{result.valid_months:,} of {result.total_months:,} selected months")
    st.divider()
    heading, controls = st.columns([2, 1])
    with heading:
        st.markdown("#### Exchange rates over time")
    with controls:
        mode = st.radio("Show", ["Real", "Nominal", "Both"], index=2, horizontal=True,
                        label_visibility="collapsed", key="chart_mode")
    if not result.valid_months and mode in ("Real", "Both"):
        st.warning("There are no valid real exchange rates in this sample. Try different dates or "
                   "countries. Available nominal observations are still included in the CSV.")
    if not len(nominal):
        st.warning("There are no valid nominal exchange rates in this sample.")
    st.plotly_chart(chart_spec(result, mode, unit), width="stretch", theme="streamlit",
                    config={"displaylogo": False, "scrollZoom": False,
                            "toImageButtonOptions": {"filename": f"rer_{pair.country1}_{pair.country2}"}},
                    key=f"rer_chart_{pair.country1}_{pair.country2}")
    st.caption(f"Sample mean RER*: {result.rer_star:.6g}" if result.valid_months else "Sample mean RER*: unavailable")
    if result.valid_months:
        st.caption(f"Valid real observations: {display_month(result.first_valid)} – "
                   f"{display_month(result.last_valid)}. Gaps are not filled.")
    if result.valid_months < result.total_months:
        st.caption(f"{result.total_months - result.valid_months:,} months excluded from the real benchmark "
                   "because a required input or calculation is unavailable or invalid.")

    st.divider()
    info, action = st.columns([3, 1])
    with info:
        st.markdown("#### Explore the underlying data")
        st.caption("Monthly CPIs, exchange rates, and all calculations for the selected sample.")
    exported = export_frame(result, pair, loaded_names)
    with action:
        st.download_button("Download CSV", data=csv_bytes(exported),
                           file_name=f"rer_{pair.country1}_{pair.country2}_{start}_{end}.csv",
                           mime="text/csv", on_click="ignore", width="stretch")
    with st.expander("View observations"):
        st.dataframe(exported[["month", "cpi_1", "cpi_2", "nominal_s", "rer", "rer_star",
                               "real_index", "valid_rer"]], hide_index=True, width="stretch")
    with st.expander("Methodology and data coverage"):
        st.latex(r"S(t)=\frac{E_2(t)}{E_1(t)},\quad RER(t)=S(t)\frac{P_1(t)}{P_2(t)}")
        st.latex(r"RER^*=\frac{1}{N}\sum_{t\in V}RER(t),\quad Real\ Index(t)=100\frac{RER(t)}{RER^*}")
        st.write("E1 and E2 are monthly period-average local-currency units per US dollar. "
                 "V contains valid months in the selected inclusive sample; N is their count. "
                 "USD per USD is exactly 1. CPI index base years can differ: constant rebasing "
                 "cancels from the normalized real index. For the Euro area, all-items HICP "
                 "is used when a CPI-labelled series is unavailable.")
        st.write("Changing the sample months recalculates the benchmark. Zooming or hiding a "
                 "chart line does not change the sample. The nominal exchange rate is never normalized.")
        st.write("Missing months are retained and not interpolated. Zero, negative, infinite, and "
                 "missing inputs are excluded from the real benchmark. A high real index is above "
                 "this sample's historical average; it is not proof of overvaluation.")
        st.write("Historical currency conventions are those reported by IMF. The app does not "
                 "guess redenomination factors or create pre-history for the Euro area. Reported "
                 "currency/unit/reference-period changes and explicit breaks stop comparisons across "
                 "the affected sample; breaks not reported in the data may require separate review.")
        status = []
        for key, item in pair.inputs.items():
            valid_input = item.observations["value"].replace([np.inf, -np.inf], np.nan).dropna()
            valid_input = valid_input[valid_input.gt(0)]
            status.append({
                "Input": key, "IMF series": item.key or "Unavailable",
                "First positive observation": str(valid_input.index.min()) if len(valid_input) else "",
                "Last positive observation": str(valid_input.index.max()) if len(valid_input) else "",
                "Observations": len(valid_input), "Retrieved UTC": item.retrieved_at or "",
            })
        st.dataframe(pd.DataFrame(status), hide_index=True, width="stretch")
        for message in result.warnings:
            st.caption(message)
        st.caption("Source: IMF Consumer Price Index (CPI) and Exchange Rates (ER) datasets. "
                   "Observations are fetched on demand and cached for six hours per user session. "
                   "Refresh loaded pair bypasses the cache.")
        st.markdown("[IMF CPI](https://data.imf.org/en/datasets/IMF.STA:CPI) · "
                    "[IMF exchange rates](https://data.imf.org/en/datasets/IMF.STA:ER)")


def connection_check() -> None:
    parser = argparse.ArgumentParser(description="Check the live IMF retrieval and RER calculations.")
    parser.add_argument("--check-live", action="store_true")
    parser.add_argument("--country1", default="USA")
    parser.add_argument("--country2", default="GBR")
    parser.add_argument("--start", default=EARLIEST_MONTH)
    parser.add_argument("--end", default=None)
    args = parser.parse_args()
    try:
        client = IMFClient()
        names = client.countries()
        pair = load_pair(client, args.country1, args.country2, end=args.end)
        result = calculate_sample(pair, args.start, args.end or str(pair.observations.index.max()))
        print(json.dumps({
            "pair": [pair.country1, pair.country2], "imf_economies": len(names),
            "sample": [args.start, args.end or str(pair.observations.index.max())],
            "valid_months": result.valid_months, "total_months": result.total_months,
            "first_valid": result.first_valid, "last_valid": result.last_valid,
            "rer_star": result.rer_star if result.valid_months else None,
            "real_index_mean": float(result.observations["real_index"].mean()) if result.valid_months else None,
            "warnings": result.warnings,
        }, indent=2, allow_nan=False))
        if not result.valid_months:
            raise SystemExit(2)
    except (IMFError, ValueError) as exc:
        parser.exit(1, f"Check failed: {exc}\n")


if __name__ == "__main__":
    import sys
    if "--check-live" in sys.argv:
        connection_check()
    else:
        run_app()
