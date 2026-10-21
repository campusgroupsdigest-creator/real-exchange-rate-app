# real-exchange-rate-app

1. Upload app.py and requirements.txt to the root of your GitHub repository.
   Replace the earlier backend-only app.py with this completed version.
2. Edit your existing README.md. Replace the app-specific description and
   installation instructions with the Markdown between BEGIN README CONTENT
   and END README CONTENT below. Keep unrelated repository sections.
3. Do not paste this instruction section or the BEGIN/END markers into README.md.
4. After deploying, add your actual Streamlit URL below the README title.
5. README_edits.txt is a guide for you; Streamlit only needs app.py and
   requirements.txt. No additional data, configuration, or image files are required.

BEGIN README CONTENT

# RER Explorer — Real Exchange Rate App

A Streamlit app that calculates monthly bilateral real exchange rates using IMF price indexes and exchange rates. The default comparison is United Kingdom (Country 1) and United States (Country 2).

## Features

- Choose two economies from IMF country metadata, including the Euro area (G163).
- Retrieve observations only for the selected economies, starting in January 1976 where available.
- Choose inclusive start and end months.
- Display Real, Nominal, or Both in interactive charts. Both uses separate vertical axes.
- Keep nominal exchange rates in actual currency units.
- Download every month in the selected sample, including input data, calculations, validity flags, and source details.
- Inspect data coverage and methodology inside the app.
- Use cached observations for six hours per session, or select Refresh loaded pair to fetch fresh data.

## Definitions and calculations

Country 1 is the base currency and Country 2 is the quote currency.

Let E1(t) and E2(t) be the IMF's monthly period-average exchange rates, measured as local-currency units per US dollar.

```text
S(t) = E2(t) / E1(t)
RER(t) = S(t) × P1(t) / P2(t)

RER* = arithmetic average of RER(t) over valid selected months

Real Index(t) = 100 × RER(t) / RER*
```

S(t) is Country 2 currency units per one unit of Country 1 currency. For UK → USA, this is US dollars per pound sterling. USD per USD is exactly 1 and is provided as a currency identity.

P1(t) and P2(t) are monthly all-items price indexes. For the Euro area, the app uses IMF all-items HICP when the CPI-labelled series is unavailable; this is labelled in the app and recorded in the CSV.

Changing the start or end month recalculates RER* and the entire displayed real index. Chart zooming or hiding a line does not alter the sample.

The real index averages 100 over valid months in the selected sample. Nominal S(t) is never normalized. Constant CPI rebasing cancels from the real index, so the two countries do not need the same CPI base year. An index above 100 means above this sample's average; it is not evidence by itself of economic overvaluation.

## IMF data

Base API: https://api.imf.org/external/sdmx/3.0

```text
Price index:  IMF.STA / CPI / {ISO3}.CPI._T.IX.M
Euro prices:  IMF.STA / CPI / G163.HICP._T.IX.M (when CPI is unavailable)
Exchange rate: IMF.STA / ER / {ISO3}.XDC_USD.PA_RT.M
```

The app discovers dataflow versions and country names from SDMX 3.0 structure metadata. Monthly server-side time filters use c[TIME_PERIOD]. Observations are also filtered locally to the requested retrieval range.

Sources:
- [IMF Consumer Price Index](https://data.imf.org/en/datasets/IMF.STA:CPI)
- [IMF Exchange Rates](https://data.imf.org/en/datasets/IMF.STA:ER)
- [IMF API information](https://data.imf.org/en/Resource-Pages/IMF-API)

Coverage varies by economy and series. Missing months are retained as gaps and are never interpolated or forward-filled. RER is valid only when both price indexes and both exchange-rate inputs, and the resulting calculation, are positive and finite. Nominal rates remain available where exchange rates exist even if CPI is missing.

Historical currency units and links follow IMF's published series. The app does not invent pre-Euro data or guess redenomination conversion factors. If supplied metadata changes currency, units, or price-reference periods, or explicitly flags a break within the sample, the app stops the comparison and offers raw observations for review. Breaks not documented in the supplied attributes may need separate investigation.

## CSV download

The download includes all months within the selected start/end range. It contains:

- Country codes and names, sample dates, and nominal-rate units.
- Both price indexes and both exchange-rate inputs.
- nominal_s, rer, rer_star, and real_index.
- valid_rer, per-input validity flags, and exclusion_reason.
- Original IMF attributes and reported values where provided, series keys, source URLs, and UTC load timestamps.

Missing observations or unavailable calculations are blank. The file uses UTF-8 with a byte-order mark for Excel compatibility.

## Run locally

Use Python 3.12, the tested runtime. Open a terminal in the repository folder and run:

```bash
python -m venv .venv
```

Activate it on Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Or on macOS/Linux:

```bash
source .venv/bin/activate
```

Then install and launch:

```bash
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

Open the local URL printed by Streamlit. Choose two countries and select Load data, then adjust the sample months and chart mode.

## Deploy on Streamlit Community Cloud

1. Commit app.py and requirements.txt in the root of the GitHub repository.
2. Sign in at [Streamlit Community Cloud](https://share.streamlit.io) and connect your GitHub account.
3. Select Create app and choose the existing GitHub app option.
4. Select your repository and branch, and set the main file path to app.py.
5. In Advanced settings, select Python 3.12.
6. Deploy. Streamlit installs requirements.txt automatically.
7. Open the app, load UK → USA, and check the chart and CSV. Use the app's sharing controls to set access and copy its URL.
8. Add the deployed URL near the top of this README.

Public IMF requests worked without an API key during validation. If your IMF access requires one, add the following in Streamlit's Secrets settings, not in the repository:

```toml
IMF_API_KEY = "your-subscription-key"
```

For local use, the same value can be supplied through the IMF_API_KEY environment variable or a local .streamlit/secrets.toml file. Do not commit secret values.

## Troubleshooting

- If the country list or data cannot load, check the Streamlit logs and try again. API failures are shown rather than replaced with sample data.
- If IMF returns HTTP 401/403, check network access and any required subscription key. HTTP 404 can mean an invalid endpoint/series or an access restriction; it is not silently interpreted as missing history.
- If no real observations exist in the sample, change dates or countries. Available nominal data can still be downloaded.
- If a historical break is reported, choose a consistent date segment or inspect the raw observations.
- Use Refresh loaded pair after an IMF update. Changes to chart mode or dates reuse loaded observations.

Optional live connection check from a terminal:

```bash
python app.py --check-live --country1 GBR --country2 USA --start 1976-01 --end 2025-12
```

This prints coverage, the sample mean RER, and the mean real index. A successful nonempty real index should average approximately 100.

Implementation references:
- [Streamlit deployment guide](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy)
- [Streamlit secrets management](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management)

END README CONTENT

