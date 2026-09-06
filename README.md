# SEEA-Q (QGIS plugin)

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.22282872.svg)](https://doi.org/10.5281/zenodo.22282872)
![GitHub Release](https://img.shields.io/github/v/release/Shlhnj/SEEA-Q)
![GitHub License](https://img.shields.io/github/license/Shlhnj/SEEA-Q)
![GitHub repo size](https://img.shields.io/github/repo-size/Shlhnj/SEEA-Q)

See the plugin page at https://plugins.qgis.org/plugins/seeaq/
Repository: https://github.com/Shlhnj/SEEAQ.

Two QGIS Processing algorithms, both reading the same raster time
series, that together build a UN SEEA Ecosystem Accounting extent
account, change matrix, physical/monetary flow accounts, and a
monetary ecosystem asset account.

**No third-party Python package to install.** Raster reading uses
QGIS's own `QgsRasterBlock`, pixel area uses `QgsDistanceArea`
(ellipsoid-correct), and every CSV is parsed with the Python standard
library `csv` module. The only import beyond QGIS's own `qgis.core` is
`numpy`, which ships with QGIS's bundled Python already.

## Install

Zip the `seea_ea_toolkit/` folder (so the zip contains
`seea_ea_toolkit/metadata.txt` at its root), then in QGIS: **Plugins >
Manage and Install Plugins > Install from ZIP**. Or copy the folder
into your QGIS profile's `python/plugins/` directory and enable it
under **Installed**.

Once enabled, both algorithms are reachable two ways:

- **Plugins menu > SEEA EA Toolkit**: a submenu with one entry per
  algorithm; each opens that algorithm's normal parameter dialog.
- **Processing Toolbox > SEEA EA Toolkit**: same two algorithms,
  for anyone who prefers the Toolbox or wants to chain them into a
  Processing model/batch run.

## Common inputs (both algorithms)

- **Layers**: two or more classified ecosystem-type layers, one per
  year (rasters, vector polygon layers, or a mix). Rasters: same
  grid/CRS/resolution, pixel values = integer class IDs, any format
  QGIS can open. Vector layers: polygons with an integer attribute
  field holding the class ID (see "Using vector layers" below). They
  get rasterized onto a common grid automatically before the
  accounting math runs, so the rest of the pipeline never knows the
  difference.
- **StateClasses.csv** (ST-Sim schema): columns `Name, StateLabelXId,
  StateLabelYId, Id, Color, Legend, Description, IsAutoName` (only
  `Id` and `StateLabelXId` are required). `Id` matches raster pixel
  values (and vector layers' ID field values); `StateLabelXId` is used
  as the ecosystem type name everywhere in the outputs.
- **Years**: comma-separated, same order as the layers, e.g.
  `2000,2010,2020`.

### Using vector layers

Set two extra parameters when any input is a vector layer:

- **Ecosystem type ID field**: the vector layer's attribute field
  holding the integer class ID (matching StateClasses.csv's `Id`
  column). Required whenever at least one input is a vector layer, and
  the same field name is used across all of them.
- **Pixel size for rasterizing vector layers**: only needed if
  *every* input is a vector layer (no raster to borrow a grid from).
  If at least one raster is included, its own grid/resolution/CRS
  becomes the reference every vector layer is rasterized onto, so
  everything lines up pixel-for-pixel.

Rasterization uses QGIS's own bundled GDAL (the `gdal:rasterize`
Processing algorithm), meaning no new Python dependency, no rasterio, and no
GDAL Python bindings are required. Mixing formats freely is fine: e.g. an older
digitized land-use map (vector) as your earliest year, alongside newer
classified satellite rasters for later years.

**Note**: the actual rasterization step (the `gdal:rasterize` call
itself) needs a live QGIS session with GDAL to execute and hasn't been
run end-to-end outside one. The grid-selection and validation logic
around it (reference grid choice, missing-field errors, missing-
pixel-size errors) is unit-tested, but give this feature a smoke test
with a real vector layer before relying on it for something important.

---

## 1. SEEA Extent Account + Change Matrix

Outputs three CSVs:
- **Flat extent table**: one row per (year, class_name, area_ha).
- **SEEA EA extent account**: Table 4.1 layout, chained across every
  consecutive period: Opening extent / Additions / Reductions / Net
  change in extent / Closing extent, by ecosystem type + Total.
- **Change matrix**: From x To cross-tabulation per consecutive
  period, in hectares, with Opening/Closing margins.

Additions/Reductions and the change matrix come directly from
pixel-level from/to comparisons between each consecutive raster pair,
which is the natural approach for purely observed maps. Opening + Net change =
Closing exactly for every class.

---

## 2. SEEA Ecosystem Service Flow + Asset Account

**Scope**: this builds the *supply side by ecosystem type* of the
SEEA EA physical/monetary ecosystem service flow accounts, plus the
monetary ecosystem asset account. It does **not** build the industry
*use* side ("Agriculture / Forestry / Fisheries / ..."), as that needs
economic survey data with no relationship to a land-cover raster.

### If you already use strategicc/ST-Sim: reuse your existing files

This algorithm's two extra inputs are **strategicc's own established
schemas**, not something new to author: `accounting/csv_loader.py`'s
`EcosystemServices.csv` and `AssetValuationParams.csv`. If you already
maintain a strategicc project, point the algorithm at the same files.

#### EcosystemServices.csv (required)


```

StateClassId,ServiceName,ServiceType,ValuePerUnitArea,Currency,PhysicalUnit,PhysicalValuePerUnitArea
Forest,Wood Provisioning,Provisioning,210000,IDR,m3,3.5
Cropland,Crop Provisioning,Provisioning,20000,IDR,tonnes,2.5

```

- `ValuePerUnitArea`: monetary value **per hectare per year** (Mode A),
  or with `PhysicalUnit`/`PhysicalValuePerUnitArea` also given (Mode B),
  an independent physical quantity per hectare alongside it. Both are
  always hectare-denominated, exactly as in strategicc.
- `ServiceType` must be `Provisioning`, `Regulating`, or `Cultural`.
- **Mode C** (`StockFlowSource`, physical quantity sourced from a
  simulated stock/flow run) **is not supported**: there's no
  simulated stock/flow log for purely observed rasters. Mode C rows
  are skipped with a warning.
- `UserType`/`UserShare` columns are accepted (so a file used
  elsewhere for strategicc's use-side split still parses) but have no
  effect here, since this plugin doesn't build a use table.
- **Plugin extension**: an optional `Year` column. strategicc's own
  files have no year dimension (one valuation table per whole
  simulation run). Add `Year` here if your rates/prices should change
  across your observed years. Leave the column out entirely (or leave
  a cell blank) to broadcast a row across every year, matching
  strategicc's native static behaviour. A row with an explicit `Year`
  always takes priority over a broadcast row for the same ecosystem
  type/service, for that year.

Neither `ValuePerUnitArea` nor `PhysicalValuePerUnitArea` can be
derived from a classified raster; they're estimates you supply.

#### AssetValuationParams.csv (optional)


```

StateClassId,DiscountRate,AssetLifeYears,PriceGrowthRate
Forest,0.02,100,0.00
Cropland,0.02,50,0.01
ALL,0.02,100,0.00

```

`StateClassId` = `ALL` supplies the fallback for any class without its
own row (strategicc's own convention). If this file is omitted
entirely, every class uses the algorithm's two default parameters
instead. `ConditionProxy`/`ConditionReferenceLevel` columns are
accepted for file compatibility but have no effect (see below).

### NPV formula (identical to strategicc's `SEEAAccount._npv`)

A stream of `AssetLifeYears` annual cash flows starting at the class's
current total service value and growing at `PriceGrowthRate` per year,
discounted at `DiscountRate`, income earned at year end:


```

NPV = sum_{t=0}^{T-1} annual_value * (1+g)^t / (1+r)^(t+1)

```

which collapses to the familiar finite annuity `annual_value *
(1-(1+r)^-T)/r` when `PriceGrowthRate = 0`. Verified against the SEEA
EA online supplement's stylised example (a $9,000 exchange value at
T=100, r=2% gives NPV = $387,885.16) and against a growing-annuity
closed form for `PriceGrowthRate > 0`.

### Outputs (four CSVs)

- **Physical flow account**: extent_ha × PhysicalValuePerUnitArea,
  per (year, ecosystem_type, service) (only for services with a
  physical figure in Mode B).
- **Monetary flow account**: extent_ha × ValuePerUnitArea, per (year,
  ecosystem_type, service), plus TOTAL rows per ecosystem type and per
  year.
- **NPV by ecosystem type**: total service value, discount rate,
  asset life, price growth rate, and NPV per (year, ecosystem_type),
  plus TOTAL rows. This mirrors strategicc's own NPV granularity (NPV is
  computed once per class per year off the *total* service value, not
  per service, exactly as `monetary_asset_account_seea()` does).
- **Monetary asset account**: SEEA EA Table 10.1 layout, chained
  across every consecutive period: Opening value / Ecosystem
  enhancement / Ecosystem degradation / Ecosystem conversions
  (additions, reductions) / Other changes in volume (catastrophic
  losses, reappraisals; always 0, see below) / Revaluations / Net
  change in value / Closing value, by ecosystem type + Total, plus a
  Check row confirming everything reconciles exactly.

### How the asset account matches strategicc's own methodology

This mirrors `SEEAAccount.monetary_asset_account_seea()` almost
exactly, with the differences documented in
`algorithms/flows_assets.py`'s module docstring:

- **Opening/Closing value**: NPV of that period's actual total
  service value for the class, per the formula above.
- **Ecosystem conversions**: Additions valued at the class's per-area
  value in the period's *closing* year; Reductions at its per-area
  value in the *opening* year. This matches SEEA EA's own requirement
  that these align with the physical extent account. Additions/
  Reductions themselves come from this plugin's own pixel-level
  extent comparison (same numbers as algorithm 1's extent account),
  not a simulated transition log, as there isn't one for observed-only
  data.
- **Revaluations**: isolates the pure price-growth contribution (NPV
  at `PriceGrowthRate` minus NPV of the same annual value at 0
  growth). This is zero whenever `PriceGrowthRate` is 0.
- **Ecosystem enhancement/degradation**: the *residual* needed so Net
  change in value reconciles exactly with Closing − Opening
  (Enhancement = `max(residual, 0)`, Degradation = `min(residual, 0)`).
  This is a documented approximation, not SEEA EA's condition-attributed
  split. That needs a compiled condition account this plugin doesn't
  have, which is the same limitation strategicc itself documents.
- **Catastrophic losses / Reappraisals**: always reported as 0, being
  honestly absent rather than silently omitted, since there's no
  transition-group classification or methodology-change mechanism
  available here (strategicc takes the same stance for Reappraisals).
- **ConditionProxy**: strategicc uses this only as a sanity-check
  warning on the enhancement/degradation split's sign, comparing it
  against a simulated stock trajectory. This plugin has no simulated
  stock data, so the column is accepted but does nothing.

  (companion plugin: **strategiccq** for STSM simulation).
