"""
SEEA EA ecosystem service flow accounts (physical + monetary, supply
side by ecosystem type) and the monetary ecosystem asset account, from
the same raster time series used by SeeaExtentAlgorithm plus two
files that follow strategicc's own established schemas -- so if
you already maintain a strategicc/ST-Sim project, you can point this
at the same EcosystemServices.csv and AssetValuationParams.csv you
already have, rather than authoring anything new:

  EcosystemServices.csv     strategicc's per-class service valuation
                             table (accounting/csv_loader.py's
                             load_ecosystem_services() schema, Modes
                             A/B only -- see below).
  AssetValuationParams.csv  strategicc's NPV parameters per class
                             (load_asset_valuation_params() schema),
                             with an "ALL" fallback row.

Differences from strategicc's own accounting engine, and why:

  * Mode C (StockFlowSource -- physical quantity sourced from a
    simulated stock/flow run) is not supported. There is no
    simulated stock/flow log for a stack of purely observed rasters,
    so Mode C rows are skipped with a warning rather than silently
    treated as zero.
  * EcosystemServices.csv has no year dimension in strategicc (it's
    a single valuation table applied across a whole simulation run).
    This plugin accepts an optional extra `Year` column so rates can
    change across your observed years; if the column is absent, each
    row is broadcast across every year, matching strategicc's native
    static usage exactly.
  * The asset account's Enhancement/Degradation split uses the same
    residual method as strategicc's monetary_asset_account_seea()
    (SEEA EA Table 10.1), but Additions/Reductions come from this
    plugin's own pixel-level extent comparison (algorithms._common.
    additions_reductions_ha) rather than a transition log, since
    there is no simulated trans_df for observed-only data. Catastrophic
    losses and Reappraisals are always reported as 0 -- honestly
    absent, not modelled, the same stance strategicc itself takes for
    Reappraisals with no mechanism to generate one.
  * ConditionProxy/ConditionReferenceLevel columns are accepted (so
    the same AssetValuationParams.csv parses without edits) but have
    no effect -- the sanity-check they drive in strategicc needs a
    simulated stock_df this plugin doesn't have.

NPV formula (identical to strategicc's SEEAAccount._npv): a stream of
`asset_life_years` annual cash flows starting at the class's current
total service value and growing at `price_growth_rate` per year,
discounted at `discount_rate`, income earned at year end:

    NPV = sum_{t=0}^{T-1} annual_value * (1+g)^t / (1+r)^(t+1)

which collapses to the familiar finite annuity annual_value *
(1-(1+r)^-T)/r when price_growth_rate = 0.
"""

import csv

from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterFile,
    QgsProcessingParameterString,
    QgsProcessingParameterNumber,
    QgsProcessingParameterFileDestination,
    QgsProcessingException,
)

from ._common import (
    load_state_classes, read_raster_series, parse_years,
    extent_by_class_ha, additions_reductions_ha,
)

VALID_SERVICE_TYPES = {"Provisioning", "Regulating", "Cultural"}


def _npv(annual_value, discount_rate, asset_life_years, price_growth_rate=0.0):
    if asset_life_years <= 0:
        return 0.0
    r, g = discount_rate, price_growth_rate
    return sum(
        annual_value * ((1 + g) ** t) / ((1 + r) ** (t + 1))
        for t in range(int(asset_life_years))
    )


class EcosystemService:
    __slots__ = (
        "state_class", "service_name", "service_type", "value_per_unit_area",
        "currency", "physical_unit", "physical_per_unit_area", "year",
        "user_type", "has_explicit_user",
    )

    def __init__(self, state_class, service_name, service_type,
                 value_per_unit_area, currency, physical_unit,
                 physical_per_unit_area, year, user_type, has_explicit_user):
        self.state_class = state_class
        self.service_name = service_name
        self.service_type = service_type
        self.value_per_unit_area = value_per_unit_area
        self.currency = currency
        self.physical_unit = physical_unit
        self.physical_per_unit_area = physical_per_unit_area
        self.year = year  # None if the CSV has no Year column (strategicc-native)
        self.user_type = user_type
        self.has_explicit_user = has_explicit_user


def _load_ecosystem_services(path, valid_names, feedback):
    """Parse EcosystemServices.csv (strategicc schema, Modes A/B only,
    plus an optional plugin-specific `Year` column)."""
    services = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        fieldnames = reader.fieldnames or []
        value_col = next(
            (c for c in ("ValuePerUnitArea", "ValuePerUnit", "ValuePerHa") if c in fieldnames),
            "ValuePerUnitArea",
        )
        phys_col = next(
            (c for c in ("PhysicalValuePerUnitArea", "PhysicalValuePerUnit", "PhysicalValuePerHa") if c in fieldnames),
            "PhysicalValuePerUnitArea",
        )
        has_year_col = "Year" in fieldnames
        if not has_year_col:
            feedback.pushInfo(
                "EcosystemServices.csv has no 'Year' column - each row will "
                "be applied to every year (strategicc's native behaviour)."
            )

        for i, row in enumerate(reader, start=2):
            state_class = (row.get("StateClassId") or "").strip()
            service_name = (row.get("ServiceName") or "").strip()
            service_type = (row.get("ServiceType") or "").strip()
            currency = (row.get("Currency") or "").strip()

            if not state_class or not service_name:
                feedback.pushWarning(f"EcosystemServices.csv row {i}: missing StateClassId/ServiceName - skipped.")
                continue
            if state_class not in valid_names:
                feedback.pushWarning(
                    f"EcosystemServices.csv row {i}: StateClassId '{state_class}' "
                    "not found in StateClasses.csv - skipped."
                )
                continue
            if service_type not in VALID_SERVICE_TYPES:
                feedback.pushWarning(
                    f"EcosystemServices.csv row {i}: ServiceType '{service_type}' "
                    f"not one of {sorted(VALID_SERVICE_TYPES)} - skipped."
                )
                continue

            try:
                value_per_unit_area = float((row.get(value_col) or "").strip())
            except ValueError:
                feedback.pushWarning(f"EcosystemServices.csv row {i}: invalid {value_col} - skipped.")
                continue

            sf_source = (row.get("StockFlowSource") or "").strip() or None
            if sf_source:
                feedback.pushWarning(
                    f"EcosystemServices.csv row {i} ({state_class}/{service_name}): "
                    "Mode C (StockFlowSource) is not supported here - no simulated "
                    "stock/flow data exists for observed rasters. Row skipped."
                )
                continue

            phys_unit = (row.get("PhysicalUnit") or "").strip() or None
            phys_raw = (row.get(phys_col) or "").strip()
            try:
                phys_per_unit_area = float(phys_raw) if phys_raw else None
            except ValueError:
                phys_per_unit_area = None
            if (phys_unit is None) != (phys_per_unit_area is None):
                feedback.pushWarning(
                    f"EcosystemServices.csv row {i} ({state_class}/{service_name}): "
                    "PhysicalUnit and PhysicalValuePerUnitArea must both be present "
                    "or both absent - treating as monetary-only."
                )
                phys_unit = phys_per_unit_area = None

            year = None
            if has_year_col:
                year_raw = (row.get("Year") or "").strip()
                if year_raw:
                    year = int(year_raw)
                # else: blank Year in a file that has the column -> broadcast
                # to every year, same as a file with no Year column at all.

            user_type_field = (row.get("UserType") or "").strip()
            has_explicit_user = bool(user_type_field)

            services.append(EcosystemService(
                state_class, service_name, service_type, value_per_unit_area,
                currency, phys_unit, phys_per_unit_area, year,
                user_type_field or "Unspecified", has_explicit_user,
            ))

    if not services:
        raise QgsProcessingException("No usable rows found in EcosystemServices.csv after validation.")

    # Canonicalise: a group of rows sharing (state_class, service_name)
    # where every row has an explicit UserType is a use-side split of ONE
    # total (avoid double-counting it); otherwise every row is an
    # additive sub-component (e.g. stock:AGB + stock:Soil) and is kept.
    by_key = {}
    for s in services:
        by_key.setdefault((s.state_class, s.service_name, s.year), []).append(s)
    canonical = []
    for (_sc, _sn, _yr), group in by_key.items():
        is_split_group = len(group) > 1 and all(s.has_explicit_user for s in group)
        canonical.extend([group[0]] if is_split_group else group)

    n_phys = sum(1 for s in canonical if s.physical_per_unit_area is not None)
    feedback.pushInfo(f"Loaded {len(canonical)} ecosystem service entries ({n_phys} with physical units).")
    return canonical


def _load_asset_valuation_params(path, valid_names, feedback):
    params = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for i, row in enumerate(reader, start=2):
            state_class = (row.get("StateClassId") or "").strip()
            if not state_class:
                feedback.pushWarning(f"AssetValuationParams.csv row {i}: missing StateClassId - skipped.")
                continue
            if state_class != "ALL" and state_class not in valid_names:
                feedback.pushWarning(
                    f"AssetValuationParams.csv row {i}: StateClassId '{state_class}' "
                    "not found in StateClasses.csv - skipped."
                )
                continue
            try:
                discount_rate = float((row.get("DiscountRate") or "").strip())
                asset_life_years = int(float((row.get("AssetLifeYears") or "").strip()))
            except ValueError:
                feedback.pushWarning(f"AssetValuationParams.csv row {i}: invalid DiscountRate/AssetLifeYears - skipped.")
                continue
            growth_raw = (row.get("PriceGrowthRate") or "").strip()
            try:
                price_growth_rate = float(growth_raw) if growth_raw else 0.0
            except ValueError:
                price_growth_rate = 0.0
            params[state_class] = (discount_rate, asset_life_years, price_growth_rate)

    feedback.pushInfo(
        f"Loaded {len(params)} asset valuation parameter row(s) "
        f"({'has' if 'ALL' in params else 'no'} ALL default)."
    )
    return params


class SeeaFlowsAssetsAlgorithm(QgsProcessingAlgorithm):

    INPUT_RASTERS = "INPUT_RASTERS"
    STATE_CLASSES_CSV = "STATE_CLASSES_CSV"
    YEARS = "YEARS"
    PIXEL_AREA_HA = "PIXEL_AREA_HA"
    ECOSYSTEM_SERVICES_CSV = "ECOSYSTEM_SERVICES_CSV"
    ASSET_VALUATION_PARAMS_CSV = "ASSET_VALUATION_PARAMS_CSV"
    ASSET_LIFE_YEARS = "ASSET_LIFE_YEARS"
    DISCOUNT_RATE = "DISCOUNT_RATE"
    OUTPUT_PHYSICAL_FLOW = "OUTPUT_PHYSICAL_FLOW"
    OUTPUT_MONETARY_FLOW = "OUTPUT_MONETARY_FLOW"
    OUTPUT_NPV_BY_ET = "OUTPUT_NPV_BY_ET"
    OUTPUT_ASSET_ACCOUNT = "OUTPUT_ASSET_ACCOUNT"

    def name(self):
        return "seea_flows_assets"

    def displayName(self):
        return "2. SEEA Ecosystem Service Flow + Asset Account (2+ raster layers)"

    def group(self):
        return "SEEA EA Toolkit"

    def groupId(self):
        return "seea_ea"

    def shortHelpString(self):
        return (
            "Builds SEEA EA physical and monetary ecosystem service flow "
            "accounts (supply side, by ecosystem type) and a monetary "
            "ecosystem asset account (NPV-based, SEEA EA Table 10.1 style), "
            "from two or more classified ecosystem-type rasters plus "
            "strategicc's own EcosystemServices.csv and "
            "AssetValuationParams.csv schemas -- if you already maintain a "
            "strategicc/ST-Sim project, point this at the same files. Mode C "
            "(StockFlowSource) rows are skipped, since there is no simulated "
            "stock/flow run for observed rasters."
        )

    def createInstance(self):
        return SeeaFlowsAssetsAlgorithm()

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterMultipleLayers(
                self.INPUT_RASTERS,
                "Classified ecosystem-type rasters, in chronological order (2 or more)",
                layerType=3,  # QgsProcessing.TypeRaster
            )
        )
        self.addParameter(
            QgsProcessingParameterFile(
                self.STATE_CLASSES_CSV,
                "StateClasses.csv (ST-Sim schema)",
                extension="csv",
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.YEARS,
                "Years, comma-separated, same order as the raster layers "
                "(e.g. 2000,2010,2020)",
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.PIXEL_AREA_HA,
                "Pixel area override, hectares (leave 0 to use QGIS's own "
                "ellipsoidal area calculation from the first raster)",
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.0,
                minValue=0.0,
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterFile(
                self.ECOSYSTEM_SERVICES_CSV,
                "EcosystemServices.csv (strategicc schema: StateClassId, "
                "ServiceName, ServiceType, ValuePerUnitArea, Currency, "
                "[PhysicalUnit, PhysicalValuePerUnitArea], [Year])",
                extension="csv",
            )
        )
        self.addParameter(
            QgsProcessingParameterFile(
                self.ASSET_VALUATION_PARAMS_CSV,
                "AssetValuationParams.csv (strategicc schema: StateClassId, "
                "DiscountRate, AssetLifeYears, [PriceGrowthRate]) - optional, "
                "'ALL' row (or the defaults below) covers classes without "
                "their own row",
                extension="csv",
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.ASSET_LIFE_YEARS,
                "Default asset life (years), used if no AssetValuationParams.csv row applies",
                type=QgsProcessingParameterNumber.Double,
                defaultValue=100.0,
                minValue=1.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.DISCOUNT_RATE,
                "Default discount rate (e.g. 0.02 for 2%), used if no "
                "AssetValuationParams.csv row applies",
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.02,
                minValue=0.0,
                maxValue=1.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterFileDestination(
                self.OUTPUT_PHYSICAL_FLOW,
                "Physical ecosystem service flow account (CSV)",
                fileFilter="CSV files (*.csv)",
            )
        )
        self.addParameter(
            QgsProcessingParameterFileDestination(
                self.OUTPUT_MONETARY_FLOW,
                "Monetary ecosystem service flow account (CSV)",
                fileFilter="CSV files (*.csv)",
            )
        )
        self.addParameter(
            QgsProcessingParameterFileDestination(
                self.OUTPUT_NPV_BY_ET,
                "NPV by ecosystem type (CSV)",
                fileFilter="CSV files (*.csv)",
            )
        )
        self.addParameter(
            QgsProcessingParameterFileDestination(
                self.OUTPUT_ASSET_ACCOUNT,
                "Monetary ecosystem asset account, SEEA EA Table 10.1 style, chained across periods (CSV)",
                fileFilter="CSV files (*.csv)",
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        layers = self.parameterAsLayerList(parameters, self.INPUT_RASTERS, context)
        if len(layers) < 2:
            raise QgsProcessingException(
                "Provide at least two classified ecosystem-type rasters (one per year)."
            )

        classes = load_state_classes(self.parameterAsFile(parameters, self.STATE_CLASSES_CSV, context))
        valid_names = {sc.name for sc in classes.values()}
        feedback.pushInfo(f"Loaded {len(classes)} classes")

        years = parse_years(self.parameterAsString(parameters, self.YEARS, context).strip(), len(layers))

        override_px_area_ha = self.parameterAsDouble(parameters, self.PIXEL_AREA_HA, context)
        arrays, px_area_ha = read_raster_series(layers, years, feedback)
        if feedback.isCanceled():
            return {}
        if override_px_area_ha:
            px_area_ha = override_px_area_ha

        # extents_by_name[year][ecosystem_type_name] = area_ha
        extents_by_name = {}
        for year, arr in zip(years, arrays):
            by_id = extent_by_class_ha(arr, classes, px_area_ha)
            extents_by_name[year] = {classes[cid].name: ha for cid, ha in by_id.items()}

        services = _load_ecosystem_services(
            self.parameterAsFile(parameters, self.ECOSYSTEM_SERVICES_CSV, context),
            valid_names, feedback,
        )

        assumptions_path = self.parameterAsFile(parameters, self.ASSET_VALUATION_PARAMS_CSV, context)
        params = (
            _load_asset_valuation_params(assumptions_path, valid_names, feedback)
            if assumptions_path else {}
        )
        default_life = self.parameterAsDouble(parameters, self.ASSET_LIFE_YEARS, context)
        default_rate = self.parameterAsDouble(parameters, self.DISCOUNT_RATE, context)

        def params_for(state_class):
            if state_class in params:
                return params[state_class]
            if "ALL" in params:
                return params["ALL"]
            return (default_rate, default_life, 0.0)

        def services_for_year(year):
            """Services that apply to `year`: rows with an explicit Year
            matching it take priority; broadcast rows (no Year at all) only
            fill in for (ecosystem_type, service) pairs that have no
            exact-year row for this year, so a pair with both a broadcast
            row and a specific override is never double-counted."""
            exact = [s for s in services if s.year == year]
            covered = {(s.state_class, s.service_name) for s in exact}
            broadcast = [
                s for s in services
                if s.year is None and (s.state_class, s.service_name) not in covered
            ]
            return exact + broadcast

        # ── 1) Physical flow account ─────────────────────────────────────
        physical_rows = []
        for year in years:
            for s in services_for_year(year):
                if s.physical_per_unit_area is None:
                    continue
                extent_ha = extents_by_name.get(year, {}).get(s.state_class, 0.0)
                physical_flow = extent_ha * s.physical_per_unit_area
                physical_rows.append({
                    "year": year, "ecosystem_type": s.state_class,
                    "service": s.service_name, "unit": s.physical_unit or "",
                    "extent_ha": round(extent_ha, 4),
                    "physical_value_per_ha": s.physical_per_unit_area,
                    "physical_flow": round(physical_flow, 4),
                })

        physical_path = self.parameterAsFileOutput(parameters, self.OUTPUT_PHYSICAL_FLOW, context)
        with open(physical_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "year", "ecosystem_type", "service", "unit",
                "extent_ha", "physical_value_per_ha", "physical_flow",
            ])
            writer.writeheader()
            writer.writerows(physical_rows)
        feedback.pushInfo(f"Physical flow account written to {physical_path}")

        # ── 2) Monetary flow account (+ totals) + total_value_by_class ──
        monetary_rows = []
        total_value_by_class = {}  # (year, state_class) -> $
        for year in years:
            for s in services_for_year(year):
                extent_ha = extents_by_name.get(year, {}).get(s.state_class, 0.0)
                exchange_value = extent_ha * s.value_per_unit_area
                monetary_rows.append({
                    "year": year, "ecosystem_type": s.state_class,
                    "service": s.service_name, "currency": s.currency,
                    "extent_ha": round(extent_ha, 4),
                    "value_per_ha": s.value_per_unit_area,
                    "exchange_value": round(exchange_value, 4),
                })
                key = (year, s.state_class)
                total_value_by_class[key] = total_value_by_class.get(key, 0.0) + exchange_value

        for (year, et), total in sorted(total_value_by_class.items()):
            monetary_rows.append({
                "year": year, "ecosystem_type": et, "service": "TOTAL", "currency": "",
                "extent_ha": "", "value_per_ha": "", "exchange_value": round(total, 4),
            })
        totals_by_year = {}
        for (year, et), total in total_value_by_class.items():
            totals_by_year[year] = totals_by_year.get(year, 0.0) + total
        for year, total in sorted(totals_by_year.items()):
            monetary_rows.append({
                "year": year, "ecosystem_type": "TOTAL", "service": "TOTAL", "currency": "",
                "extent_ha": "", "value_per_ha": "", "exchange_value": round(total, 4),
            })

        monetary_path = self.parameterAsFileOutput(parameters, self.OUTPUT_MONETARY_FLOW, context)
        with open(monetary_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "year", "ecosystem_type", "service", "currency",
                "extent_ha", "value_per_ha", "exchange_value",
            ])
            writer.writeheader()
            writer.writerows(monetary_rows)
        feedback.pushInfo(f"Monetary flow account written to {monetary_path}")

        # ── 3) NPV by ecosystem type (total service value per class) ────
        npv_rows = []
        npv_by_class = {}  # (year, state_class) -> NPV
        for (year, et), tv in sorted(total_value_by_class.items()):
            r, T, g = params_for(et)
            npv = _npv(tv, r, T, g)
            npv_by_class[(year, et)] = npv
            npv_rows.append({
                "year": year, "ecosystem_type": et, "total_exchange_value": round(tv, 4),
                "discount_rate": r, "asset_life_years": T, "price_growth_rate": g,
                "npv": round(npv, 4),
            })
        for year, total in sorted(totals_by_year.items()):
            npv_rows.append({
                "year": year, "ecosystem_type": "TOTAL", "total_exchange_value": round(total, 4),
                "discount_rate": "", "asset_life_years": "", "price_growth_rate": "",
                "npv": round(sum(v for (y, _e), v in npv_by_class.items() if y == year), 4),
            })

        npv_path = self.parameterAsFileOutput(parameters, self.OUTPUT_NPV_BY_ET, context)
        with open(npv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "year", "ecosystem_type", "total_exchange_value",
                "discount_rate", "asset_life_years", "price_growth_rate", "npv",
            ])
            writer.writeheader()
            writer.writerows(npv_rows)
        feedback.pushInfo(f"NPV by ecosystem type written to {npv_path}")

        # ── 4) Monetary asset account, SEEA EA Table 10.1 style ─────────
        et_names = sorted({s.state_class for s in services})
        asset_rows = []

        for i in range(len(years) - 1):
            y0, y1 = years[i], years[i + 1]
            period = f"{y0}\u2013{y1}"
            a0, a1 = arrays[i], arrays[i + 1]
            additions_ha, reductions_ha = additions_reductions_ha(a0, a1, classes, px_area_ha)

            entries = {
                "Opening value": {}, "Ecosystem enhancement": {}, "Ecosystem degradation": {},
                "Ecosystem conversions \u2014 additions": {}, "Ecosystem conversions \u2014 reductions": {},
                "Other changes in volume \u2014 catastrophic losses": {},
                "Other changes in volume \u2014 reappraisals": {},
                "Revaluations": {}, "Net change in value": {}, "Closing value": {},
            }

            for et in et_names:
                r, T, g = params_for(et)
                tv0 = total_value_by_class.get((y0, et), 0.0)
                tv1 = total_value_by_class.get((y1, et), 0.0)
                opening_value = _npv(tv0, r, T, g)
                closing_value = _npv(tv1, r, T, g)

                opening_area = extents_by_name.get(y0, {}).get(et, 0.0)
                closing_area = extents_by_name.get(y1, {}).get(et, 0.0)
                val_per_area_y0 = (tv0 / opening_area) if opening_area > 0 else 0.0
                val_per_area_y1 = (tv1 / closing_area) if closing_area > 0 else 0.0

                additions_value = additions_ha.get(et, 0.0) * val_per_area_y1
                reductions_value = reductions_ha.get(et, 0.0) * val_per_area_y0

                revaluation = _npv(tv1, r, T, g) - _npv(tv1, r, T, 0.0)
                net_change = closing_value - opening_value
                explained = additions_value - reductions_value + revaluation
                residual = net_change - explained
                enhancement = max(residual, 0.0)
                degradation = min(residual, 0.0)

                entries["Opening value"][et] = opening_value
                entries["Ecosystem enhancement"][et] = enhancement
                entries["Ecosystem degradation"][et] = degradation
                entries["Ecosystem conversions \u2014 additions"][et] = additions_value
                entries["Ecosystem conversions \u2014 reductions"][et] = -reductions_value
                entries["Other changes in volume \u2014 catastrophic losses"][et] = 0.0
                entries["Other changes in volume \u2014 reappraisals"][et] = 0.0
                entries["Revaluations"][et] = revaluation
                entries["Net change in value"][et] = net_change
                entries["Closing value"][et] = closing_value

            for entry_name, series in entries.items():
                row = {"Period": period, "Entry": entry_name}
                row.update({et: round(series[et], 4) for et in et_names})
                row["Total"] = round(sum(series.values()), 4)
                asset_rows.append(row)

            check_row = {"Period": period, "Entry": "Check (Opening + all changes - Closing)"}
            for et in et_names:
                total_changes = (
                    entries["Ecosystem enhancement"][et] + entries["Ecosystem degradation"][et]
                    + entries["Ecosystem conversions \u2014 additions"][et]
                    + entries["Ecosystem conversions \u2014 reductions"][et]
                    + entries["Other changes in volume \u2014 catastrophic losses"][et]
                    + entries["Other changes in volume \u2014 reappraisals"][et]
                    + entries["Revaluations"][et]
                )
                check_row[et] = round(
                    entries["Opening value"][et] + total_changes - entries["Closing value"][et], 6
                )
            check_row["Total"] = round(sum(v for k, v in check_row.items() if k not in ("Period", "Entry")), 6)
            asset_rows.append(check_row)

        asset_path = self.parameterAsFileOutput(parameters, self.OUTPUT_ASSET_ACCOUNT, context)
        with open(asset_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=["Period", "Entry"] + et_names + ["Total"])
            writer.writeheader()
            writer.writerows(asset_rows)
        feedback.pushInfo(f"Monetary asset account written to {asset_path}")

        return {
            self.OUTPUT_PHYSICAL_FLOW: physical_path,
            self.OUTPUT_MONETARY_FLOW: monetary_path,
            self.OUTPUT_NPV_BY_ET: npv_path,
            self.OUTPUT_ASSET_ACCOUNT: asset_path,
        }
