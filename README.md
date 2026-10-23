# sherpa-airspace

Weekly-compiled FAA airspace snapshot for the Lucid controller ("Lucid Bots
Control" / `lucid-controller`). A GitHub Action runs [compile.py](compile.py)
every Monday, pulls public FAA UDDS data, and publishes three small files as a
release. The controller app checks the latest release when it has internet
(at most weekly), downloads new data, and answers "can I fly here, how high?"
offline at the job site — DJI-style, without DJI's servers.

## What's in a release

| Asset | Contents |
|---|---|
| `uasfm.bin.gz` | UAS Facility Map LAANC ceilings, ~380k 30-arcsecond grid cells as a sorted binary (format documented at the top of compile.py) |
| `restrictions.geojson` | National Security UAS Flight Restrictions (full-time / part-time / pending) and Prohibited Areas, as tagged polygons |
| `manifest.json` | Version (the compile date), sha256 + size per file |

**Deliberately not here: TFRs and NOTAMs.** Those change hourly, so a weekly
file would be dangerously stale. The app fetches the live TFR list from
`https://tfr.faa.gov/tfrapi/exportTfrList` directly whenever it is online and
labels it clearly when it could not.

## Consumers

The app-side reader lives in `lucid-controller/lucid-app/src/services/airspace.ts`
(plus the `AirspaceModule` Kotlin native module). It resolves the latest
release via the public GitHub API (`releases/latest`, unauthenticated) — keep
this repo public or the controllers lose updates.

## Disclaimer

Compiled from public FAA sources that are themselves not guaranteed current
("This map is not updated in real time" — FAA). Advisory information for crew
awareness, not a substitute for the operator's own airspace authorization
(LAANC et al.) or a preflight briefing.
