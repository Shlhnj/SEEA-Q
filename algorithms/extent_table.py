"""
SEEA EA extent account + change matrix, from two or more classified
ecosystem-type rasters (one per point in time). Fully self-contained --
see algorithms/_common.py for the shared QGIS-native raster reading.

Outputs:
  1) Flat extent table       - one row per (year, class_name, area_ha)
  2) SEEA EA Table 4.1 style extent account, chained across every
     consecutive period: Opening extent / Additions / Reductions /
     Net change in extent / Closing extent, by ecosystem type + Total.
     Additions/Reductions are derived directly from pixel-level
     from/to comparisons between each consecutive raster pair -- the
     natural approach for purely observed maps (no simulation log
     needed).
  3) Change matrix per consecutive period (From x To, in hectares),
     with Opening/Closing margins.
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

from ._common import load_state_classes, read_raster_series, parse_years, additions_reductions_ha


class SeeaExtentAlgorithm(QgsProcessingAlgorithm):

    INPUT_RASTERS = "INPUT_RASTERS"
    STATE_CLASSES_CSV = "STATE_CLASSES_CSV"
    YEARS = "YEARS"
    PIXEL_AREA_HA = "PIXEL_AREA_HA"
    OUTPUT_FLAT = "OUTPUT_FLAT"
    OUTPUT_SEEA = "OUTPUT_SEEA"
    OUTPUT_CHANGE_MATRIX = "OUTPUT_CHANGE_MATRIX"

    def name(self):
        return "seea_extent"

    def displayName(self):
        return "1. SEEA Extent Account + Change Matrix (2+ raster layers)"

    def group(self):
        return "SEEA EA Toolkit"

    def groupId(self):
        return "seea_ea"

    def shortHelpString(self):
        return (
            "Builds a SEEA EA extent account and change matrix from two or "
            "more classified ecosystem-type rasters (one per year). Fully "
            "self-contained -- rasters are read with QGIS's own raster "
            "provider (any format QGIS can open) and no third-party "
            "package needs installing. Requires a StateClasses.csv "
            "(ST-Sim schema) mapping the raster pixel values to "
            "ecosystem type names. All rasters must share the same "
            "grid/CRS/resolution."
        )

    def createInstance(self):
        return SeeaExtentAlgorithm()

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
                "StateClasses.csv (ST-Sim schema: Name, StateLabelXId, "
                "StateLabelYId, Id, Color, Legend, Description, IsAutoName)",
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
            QgsProcessingParameterFileDestination(
                self.OUTPUT_FLAT,
                "Flat extent table (CSV)",
                fileFilter="CSV files (*.csv)",
            )
        )
        self.addParameter(
            QgsProcessingParameterFileDestination(
                self.OUTPUT_SEEA,
                "SEEA EA Table 4.1 style extent account, chained across periods (CSV)",
                fileFilter="CSV files (*.csv)",
            )
        )
        self.addParameter(
            QgsProcessingParameterFileDestination(
                self.OUTPUT_CHANGE_MATRIX,
                "Ecosystem type change matrix, per consecutive period (CSV)",
                fileFilter="CSV files (*.csv)",
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        import numpy as np

        layers = self.parameterAsLayerList(parameters, self.INPUT_RASTERS, context)
        if len(layers) < 2:
            raise QgsProcessingException(
                "Provide at least two classified ecosystem-type rasters (one per year)."
            )

        csv_path = self.parameterAsFile(parameters, self.STATE_CLASSES_CSV, context)
        classes = load_state_classes(csv_path)
        feedback.pushInfo(f"Loaded {len(classes)} classes from {csv_path}")

        years = parse_years(self.parameterAsString(parameters, self.YEARS, context).strip(), len(layers))

        override_px_area_ha = self.parameterAsDouble(parameters, self.PIXEL_AREA_HA, context)
        arrays, px_area_ha = read_raster_series(layers, years, feedback)
        if feedback.isCanceled():
            return {}
        if override_px_area_ha:
            px_area_ha = override_px_area_ha
            feedback.pushInfo(f"Using pixel-area override: {px_area_ha} ha")
        else:
            feedback.pushInfo(f"Pixel area from QGIS raster provider: {px_area_ha} ha")

        stack = np.array(arrays, dtype=np.uint8)

        # 1) Flat extent table: one row per (year, class_name, area_ha).
        flat_rows = []
        for year, arr in zip(years, stack):
            for cid, sc in classes.items():
                n_cells = int((arr == cid).sum())
                if n_cells == 0:
                    continue
                flat_rows.append({
                    "year": year,
                    "class_name": sc.name,
                    "area_ha": round(n_cells * px_area_ha, 4),
                })
        flat_path = self.parameterAsFileOutput(parameters, self.OUTPUT_FLAT, context)
        with open(flat_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=["year", "class_name", "area_ha"])
            writer.writeheader()
            writer.writerows(flat_rows)
        feedback.pushInfo(f"Flat extent table written to {flat_path}")

        # 2) SEEA EA Table 4.1 style period account + 3) change matrix,
        # derived directly from consecutive raster pairs (see module
        # docstring).
        class_ids = sorted(classes.keys())
        class_names = [classes[c].name for c in class_ids]

        seea_rows = []
        matrix_blocks = []

        for i in range(len(years) - 1):
            y0, y1 = years[i], years[i + 1]
            a0, a1 = arrays[i], arrays[i + 1]
            period = f"{y0}\u2013{y1}"

            opening = {}
            closing = {}
            id_index = {cid: idx for idx, cid in enumerate(class_ids)}

            for cid, name in zip(class_ids, class_names):
                opening[name] = float((a0 == cid).sum()) * px_area_ha
                closing[name] = float((a1 == cid).sum()) * px_area_ha

            additions, reductions = additions_reductions_ha(a0, a1, classes, px_area_ha)

            # cross-tab kept for the change-matrix output below
            cross = np.zeros((len(class_ids), len(class_ids)), dtype=np.int64)
            valid = np.isin(a0, class_ids) & np.isin(a1, class_ids)
            for f_id, t_id in zip(a0[valid], a1[valid]):
                cross[id_index[int(f_id)], id_index[int(t_id)]] += 1

            net_change = {
                name: additions[name] - reductions[name] for name in class_names
            }

            for entry_name, series in [
                ("Opening extent", opening),
                ("Additions", additions),
                ("Reductions", reductions),
                ("Net change in extent", net_change),
                ("Closing extent", closing),
            ]:
                row = {"Period": period, "Entry": entry_name}
                row.update({name: round(series[name], 4) for name in class_names})
                row["Total"] = round(sum(series.values()), 4)
                seea_rows.append(row)

            matrix_blocks.append((period, cross.astype(float) * px_area_ha))

        seea_path = self.parameterAsFileOutput(parameters, self.OUTPUT_SEEA, context)
        with open(seea_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(
                f, fieldnames=["Period", "Entry"] + class_names + ["Total"]
            )
            writer.writeheader()
            writer.writerows(seea_rows)
        feedback.pushInfo(f"SEEA EA extent account written to {seea_path}")

        matrix_path = self.parameterAsFileOutput(
            parameters, self.OUTPUT_CHANGE_MATRIX, context
        )
        with open(matrix_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            for period, matrix in matrix_blocks:
                writer.writerow([f"Change matrix {period} (ha)"])
                writer.writerow(["From \\ To"] + class_names + ["Opening"])
                for fi, f_name in enumerate(class_names):
                    row = [f_name] + [round(v, 4) for v in matrix[fi]]
                    row.append(round(float(matrix[fi].sum()), 4))
                    writer.writerow(row)
                closing_row = ["Closing"] + [
                    round(float(matrix[:, ti].sum()), 4) for ti in range(len(class_names))
                ]
                closing_row.append(round(float(matrix.sum()), 4))
                writer.writerow(closing_row)
                writer.writerow([])
        feedback.pushInfo(f"Change matrix written to {matrix_path}")

        return {
            self.OUTPUT_FLAT: flat_path,
            self.OUTPUT_SEEA: seea_path,
            self.OUTPUT_CHANGE_MATRIX: matrix_path,
        }
