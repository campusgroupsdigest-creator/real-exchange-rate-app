"""Real Exchange Rate App: backend checkpoint, steps 1-4 only.

No Streamlit UI or charts are implemented yet. Dependencies: pandas, numpy.
Network requests happen only when IMFClient methods are explicitly called.
Live IMF connectivity and coverage have NOT been verified in this workspace.

Run a live USA/UK check from a network-enabled computer:
    python app.py --check-live --start 1976-01
Import IMFClient, load_pair and calculate_sample from the future Streamlit UI.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from io import StringIO
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
STRUCTURE_ACCEPT = "application/vnd.sdmx.structure+xml;version=2.1"
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


def parse_csv(payload: bytes, country: str, flow: str) -> pd.DataFrame:
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
    if frame.empty:
        raise NoDataError(f"No monthly {flow} observations for {country}.")
    expected = {"COUNTRY": country, "FREQUENCY": "M", "FREQ": "M"}
    expected.update({"INDEX_TYPE": "CPI", "COICOP_1999": "_T", "TYPE_OF_TRANSFORMATION": "IX"}
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


def parse_structure(payload: bytes, flow: str) -> dict:
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
    dimensions = sorted((node for node in dsd.iter() if _local(node.tag) == "Dimension"),
                        key=lambda node: int(node.get("position", "0")))
    ids = tuple(node.get("id") for node in dimensions)
    if ids != EXPECTED_DIMENSIONS[flow]:
        raise IMFError(f"IMF {flow} dimensions changed: {ids}; update the series-key mapping.")
    country_dim = next(node for node in dimensions if node.get("id") == "COUNTRY")
    reference = next((node for node in country_dim.iter() if _local(node.tag) == "Ref"
                      and node.get("class") in (None, "Codelist")), None)
    if reference is None:
        raise IMFError("Country codelist reference not found in the IMF DSD.")
    codelists = [node for node in root.iter() if _local(node.tag) == "Codelist"
                 and node.get("id") == reference.get("id")
                 and (not reference.get("agencyID") or node.get("agencyID") == reference.get("agencyID"))
                 and (not reference.get("version") or node.get("version") == reference.get("version"))]
    if len(codelists) != 1:
        raise IMFError("Country codelist is missing or ambiguous in IMF metadata.")
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

    The future UI should keep one instance across reruns. Country metadata is
    cached for a week; observations for six hours. refresh=True forces a fetch.
    No bulk observation downloads, stale-on-error substitution or interpolation.
    Set IMF_API_KEY in the environment only if your IMF access requires a key.
    """
    def __init__(self, timeout: float = 25, retries: int = 2):
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
        if os.environ.get("IMF_API_KEY"):
            headers["Ocp-Apim-Subscription-Key"] = os.environ["IMF_API_KEY"]
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
                    # LRU-style bound on cached payload count, avoiding unlimited memory growth.
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
            except (URLError, TimeoutError, OSError) as exc:
                if attempt == self.retries:
                    raise IMFError("Could not reach IMF. Check network access and try again.") from exc
                time.sleep(min(2 ** attempt, 5))
        raise IMFError("IMF request failed.")

    def structure(self, flow: str, *, refresh: bool = False) -> dict:
        if flow not in EXPECTED_DIMENSIONS:
            raise ValueError("Only CPI and ER are used by this app.")
        data, _ = self._get(f"structure/dataflow/{AGENCY}/{flow}/+", {"references": "all"},
                            STRUCTURE_ACCEPT, refresh=refresh, ttl=604800)
        return parse_structure(data, flow)

    def countries(self, *, refresh: bool = False) -> dict[str, str]:
        # This loads metadata only, not CPI or FX observations for every country.
        cpi = self.structure("CPI", refresh=refresh)["countries"]
        er = self.structure("ER", refresh=refresh)["countries"]
        countries = dict(er)
        countries.update(cpi)
        if "G163" not in countries:
            raise IMFError("Euro area G163 not found in IMF metadata; investigate before enabling it.")
        return dict(sorted(countries.items(), key=lambda item: item[1].casefold()))

    def series(self, country: str, flow: str, *, start: str = EARLIEST_MONTH,
               end: str | None = None, refresh: bool = False) -> MonthlySeries:
        code = country_code(country)
        first = month(start)
        last = month(end) if end else pd.Period(datetime.now().date(), freq="M")
        if first < month(EARLIEST_MONTH) or first > last:
            raise ValueError("Start must be January 1976 or later and no later than end.")
        if flow not in EXPECTED_DIMENSIONS:
            raise ValueError("Only CPI and ER are used by this app.")
        structure = self.structure(flow, refresh=refresh)
        if code not in structure["countries"]:
            raise NoDataError(f"{code} is not listed in IMF {flow} metadata.")
        key = f"{code}.CPI._T.IX.M" if flow == "CPI" else f"{code}.XDC_USD.PA_RT.M"
        # A resolved metadata version keeps the key schema consistent with the request.
        version = structure["version"] or "+"
        data, url = self._get(f"data/dataflow/{AGENCY}/{flow}/{version}/{key}",
                             {"startPeriod": str(first), "endPeriod": str(last)}, CSV_ACCEPT,
                             refresh=refresh)
        frame = parse_csv(data, code, flow)
        frame = frame.loc[(frame.index >= first) & (frame.index <= last)].copy()
        if frame.empty:
            raise NoDataError(f"No monthly {flow} observations for {code} in this period.")
        return MonthlySeries(code, flow, key, frame, url,
                             pd.Timestamp.now(tz="UTC").isoformat())


def load_pair(client: IMFClient, country1: str = "USA", country2: str = "GBR", *,
              start: str = EARLIEST_MONTH, end: str | None = None,
              refresh: bool = False) -> PairData:
    """Load only the selected economies, joining on an unfilled monthly grid."""
    country1, country2 = country_code(country1), country_code(country2)
    first = month(start)
    last = month(end) if end else pd.Period(datetime.now().date(), freq="M")
    if first < month(EARLIEST_MONTH) or first > last:
        raise ValueError("Invalid retrieval period.")
    inputs: dict[str, MonthlySeries] = {}
    loaded: dict[tuple[str, str], MonthlySeries] = {}
    for code in dict.fromkeys([country1, country2]):
        loaded[(code, "CPI")] = client.series(code, "CPI", start=str(first), end=str(last), refresh=refresh)
        if code == "USA":
            # USD per USD is the exact identity 1, not an estimated or forward-filled IMF value.
            identity = pd.DataFrame({"value": 1.0, "reported_value": 1.0, "CURRENCY": "USD",
                                     "SOURCE_NOTE": "USD per USD = 1 (currency identity)"},
                                    index=pd.period_range(first, last, freq="M", name="month"))
            loaded[(code, "ER")] = MonthlySeries(code, "ER", "USD_PER_USD_IDENTITY", identity,
                                                  "Currency identity, not downloaded", "")
        else:
            loaded[(code, "ER")] = client.series(code, "ER", start=str(first), end=str(last), refresh=refresh)
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
    mean = float(frame.loc[frame["valid_rer"], "rer"].mean()) if count else float("nan")
    frame["rer_star"] = mean
    frame["real_index"] = 100.0 * frame["rer"] / mean if count else np.nan
    frame["country_1"] = pair.country1
    frame["country_2"] = pair.country2
    if not count:
        warnings.append("No valid real-exchange-rate observations in the selected period.")
    if pair.country1 not in ("USA", "GBR") or pair.country2 not in ("USA", "GBR"):
        warnings.append("Historical IMF currency-linking conventions for this pair still require review; no additional redenomination adjustment was applied.")
    valid_dates = frame.index[frame["valid_rer"]]
    return SampleResult(frame, mean, count, len(frame),
                        str(valid_dates.min()) if count else None,
                        str(valid_dates.max()) if count else None,
                        tuple(warnings))


def main() -> None:
    parser = argparse.ArgumentParser(description="RER backend checkpoint; no Streamlit interface yet.")
    parser.add_argument("--check-live", action="store_true", help="Request live IMF data and print a diagnostic summary.")
    parser.add_argument("--country1", default="USA")
    parser.add_argument("--country2", default="GBR")
    parser.add_argument("--start", default=EARLIEST_MONTH)
    parser.add_argument("--end", default=None)
    args = parser.parse_args()
    if not args.check_live:
        parser.print_help()
        return
    try:
        client = IMFClient()
        countries = client.countries()
        pair = load_pair(client, args.country1, args.country2, end=args.end)
        result = calculate_sample(pair, args.start, args.end or str(pair.observations.index.max()))
        output = {"pair": [pair.country1, pair.country2], "imf_economies": len(countries),
                  "requested_sample": [args.start, args.end or str(pair.observations.index.max())],
                  "valid_months": result.valid_months, "total_months": result.total_months,
                  "first_valid": result.first_valid, "last_valid": result.last_valid,
                  "rer_star": result.rer_star if result.valid_months else None,
                  "real_index_mean": float(result.observations["real_index"].mean()) if result.valid_months else None,
                  "warnings": result.warnings}
        print(json.dumps(output, indent=2, allow_nan=False))
        if not result.valid_months:
            raise SystemExit(2)
    except (IMFError, ValueError) as exc:
        parser.exit(1, f"Check failed: {exc}\n")


if __name__ == "__main__":
    main()
