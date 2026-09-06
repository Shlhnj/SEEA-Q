"""
Shared helpers used by every algorithm in the SEEA EA Toolkit:
QGIS-native raster reading, StateClasses.csv parsing, and the
raster-stack extent computation. No third-party package required
beyond what QGIS already ships (numpy).
"""

import csv

from qgis.core import (
    Qgis,
    QgsProcessing,
    QgsProcessingException,
    QgsProject,
    QgsDistanceArea,
    QgsRectangle,
    QgsGeometry,
    QgsRasterLayer,
    QgsVectorLayer,
)


# Qgis::DataType integer values (stable across QGIS 3.x) -> numpy dtype.
# 1=Byte 2=UInt16 3=Int16 4=UInt32 5=Int32 6=Float32 7=Float64
_QGIS_DTYPE_TO_NUMPY = {
    1: "uint8",
    2: "uint16",
    3: "int16",
    4: "uint32",
    5: "int32",
    6: "float32",
    7: "float64",
}


class StateClass:
    """One row from StateClasses.csv (ST-Sim schema)."""

    __slots__ = ("id", "name", "full_name")

    def __init__(self, id_, name, full_name):
        self.id = id_
        self.name = name
        self.full_name = full_name


def load_state_classes(path):
    """
    Parse a StateClasses.csv (ST-Sim schema).

    Expected columns: Name, StateLabelXId, StateLabelYId, Id, Color,
    Legend, Description, IsAutoName. `Id` must match the raster pixel
    values; `StateLabelXId` is used as the short ecosystem type name in
    every output table (matching ST-Sim/strategicc convention); `Name`
    is kept as the longer display name but not otherwise used here.

    Returns
    -------
    dict mapping class_id (int) -> StateClass
    """
    classes = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing = {"Id", "StateLabelXId"} - set(reader.fieldnames or [])
        if missing:
            raise QgsProcessingException(
                f"StateClasses.csv is missing required column(s): {sorted(missing)}. "
                f"Found columns: {reader.fieldnames}"
            )
        for row in reader:
            cid = int(row["Id"].strip())
            name = row["StateLabelXId"].strip()
            full = (row.get("Name") or "").strip() or name
            classes[cid] = StateClass(cid, name, full)
    return classes


# ── QGIS-native raster reading (no rasterio, no Pillow tag parsing) ────────

def read_layer_as_array(layer, feedback):
    """Read band 1 of a QgsRasterLayer into a 2D numpy array, using the
    layer's own data provider -- works for any raster format/CRS QGIS
    itself can open."""
    import numpy as np

    provider = layer.dataProvider()
    width = layer.width()
    height = layer.height()
    extent = layer.extent()
    block = provider.block(1, extent, width, height)
    if block is None or not block.isValid():
        raise QgsProcessingException(
            f"Could not read band 1 of '{layer.name()}' via QGIS's raster provider."
        )

    dtype_code = int(block.dataType())
    np_dtype = _QGIS_DTYPE_TO_NUMPY.get(dtype_code)

    arr = None
    if np_dtype is not None:
        raw = bytes(block.data())
        expected_bytes = width * height * np.dtype(np_dtype).itemsize
        if len(raw) >= expected_bytes:
            arr = np.frombuffer(raw, dtype=np_dtype, count=width * height)
            arr = arr.reshape((height, width))

    if arr is None:
        # Fallback: slow but always correct, for data types the fast
        # buffer path above doesn't recognise.
        feedback.pushWarning(
            f"'{layer.name()}': falling back to a slower per-pixel read "
            f"(unrecognised raster data type code {dtype_code})."
        )
        arr = np.zeros((height, width), dtype=np.float64)
        for row in range(height):
            if feedback.isCanceled():
                break
            for col in range(width):
                arr[row, col] = block.value(row, col)

    no_data = provider.sourceNoDataValue(1) if provider.sourceHasNoDataValue(1) else None
    if no_data is not None:
        arr = np.where(arr == no_data, 0, arr)

    # ST-Sim/strategicc convention: uint8 class IDs, 0 = no class/nodata.
    return np.nan_to_num(arr, nan=0.0).astype(np.uint8)


def pixel_area_ha(layer):
    """Ellipsoid-correct pixel area in hectares via QgsDistanceArea, using
    a one-pixel rectangle centred on the raster's extent."""
    px_w = layer.rasterUnitsPerPixelX()
    px_h = layer.rasterUnitsPerPixelY()
    centre = layer.extent().center()
    rect = QgsRectangle(
        centre.x() - px_w / 2.0, centre.y() - px_h / 2.0,
        centre.x() + px_w / 2.0, centre.y() + px_h / 2.0,
    )
    geom = QgsGeometry.fromRect(rect)

    da = QgsDistanceArea()
    da.setSourceCrs(layer.crs(), QgsProject.instance().transformContext())
    da.setEllipsoid(QgsProject.instance().ellipsoid() or "WGS84")
    area_m2 = da.measureArea(geom)
    area_m2 = da.convertAreaMeasurement(area_m2, Qgis.AreaUnit.SquareMeters)
    return area_m2 / 10000.0


def read_raster_series(layers, years, feedback):
    """
    Read a chronological list of classified ecosystem-type raster layers
    into numpy arrays, checking CRS/grid consistency along the way.

    Returns
    -------
    (arrays, px_area_ha) -- arrays is a list of 2D uint8 numpy arrays,
    one per layer/year, all the same shape; px_area_ha is the pixel
    area of the first layer (hectares), with a warning if later layers
    disagree by more than 1%.
    """
    arrays = []
    px_area_ha = None
    for layer, year in zip(layers, years):
        feedback.pushInfo(f"Reading {year}: {layer.name()} ({layer.source()})")
        if layer.crs() != layers[0].crs():
            feedback.pushWarning(
                f"'{layer.name()}' CRS ({layer.crs().authid()}) differs from "
                f"the first layer's ({layers[0].crs().authid()}). Reproject "
                "all layers to the same CRS/grid before running."
            )
        arr = read_layer_as_array(layer, feedback)
        this_px_area_ha = pixel_area_ha(layer)
        if px_area_ha is None:
            px_area_ha = this_px_area_ha
        elif abs(this_px_area_ha - px_area_ha) / px_area_ha > 0.01:
            feedback.pushWarning(
                f"Pixel area for {year} ({this_px_area_ha:.6f} ha) differs "
                f"from the first raster's ({px_area_ha:.6f} ha) by more "
                "than 1%. Confirm all rasters share the same grid/CRS."
            )
        arrays.append(arr)
        if feedback.isCanceled():
            return arrays, px_area_ha

    for i, arr in enumerate(arrays[1:], start=1):
        if arr.shape != arrays[0].shape:
            raise QgsProcessingException(
                f"Raster for year {years[i]} has shape {arr.shape}, "
                f"expected {arrays[0].shape} (year {years[0]}). All "
                "rasters must share the same grid."
            )
    return arrays, px_area_ha


def parse_years(years_raw, n_layers):
    years = [int(y.strip()) for y in years_raw.split(",") if y.strip()]
    if len(years) != n_layers:
        raise QgsProcessingException(
            f"{len(years)} years given for {n_layers} raster layers - "
            "these must match 1:1, in order."
        )
    if sorted(years) != years:
        raise QgsProcessingException("Years must be given in ascending order.")
    return years


def extent_by_class_ha(arr, classes, px_area_ha):
    """dict {class_id: area_ha} for one year's array."""
    return {cid: float((arr == cid).sum()) * px_area_ha for cid in classes}


def additions_reductions_ha(arr0, arr1, classes, px_area_ha):
    """
    Additions/Reductions in hectares between two consecutive years'
    class-ID arrays, keyed by class name -- the same pixel-level
    from/to comparison SeeaExtentAlgorithm uses for its Table 4.1
    account, factored out here so the flow/asset account can use
    identical figures without recomputing them differently.

    Returns (additions, reductions): each a dict {class_name: area_ha}.
    """
    import numpy as np

    class_ids = sorted(classes.keys())
    class_names = [classes[c].name for c in class_ids]
    id_index = {cid: idx for idx, cid in enumerate(class_ids)}

    additions = {name: 0.0 for name in class_names}
    reductions = {name: 0.0 for name in class_names}
    cross = np.zeros((len(class_ids), len(class_ids)), dtype=np.int64)

    valid = np.isin(arr0, class_ids) & np.isin(arr1, class_ids)
    for f_id, t_id in zip(arr0[valid], arr1[valid]):
        cross[id_index[int(f_id)], id_index[int(t_id)]] += 1

    for fi, f_id in enumerate(class_ids):
        for ti, t_id in enumerate(class_ids):
            if fi == ti:
                continue
            area = float(cross[fi, ti]) * px_area_ha
            if area == 0:
                continue
            reductions[classes[f_id].name] += area
            additions[classes[t_id].name] += area

    return additions, reductions


# ── Vector layer support: rasterize onto a common grid ──────────────────────
# Both algorithms accept a mix of raster AND vector polygon layers in one
# input list. Vector layers are rasterized here (via QGIS's own bundled
# GDAL, through the "gdal:rasterize" Processing algorithm -- not a new
# Python dependency, the same GDAL that already backs QGIS's own vector/
# raster I/O) onto a shared grid, then handed to the existing raster
# pipeline unchanged. This keeps exactly one tested code path for the
# actual accounting math, regardless of which formats came in.

def _is_raster(layer):
    return isinstance(layer, QgsRasterLayer)


def _is_vector(layer):
    return isinstance(layer, QgsVectorLayer)


def resolve_mixed_layers_to_rasters(layers, id_field, pixel_size, feedback):
    """
    Given a list of QgsMapLayer (raster and/or vector polygon, any
    order), return an all-raster list of the same length/order:
    raster layers pass through unchanged; vector layers are rasterized
    onto a shared grid.

    Grid choice: if any input is a raster, the FIRST raster layer's
    CRS/extent/resolution is the reference grid every vector layer is
    burned onto (so it lines up pixel-for-pixel with your real data).
    If every input is a vector layer, `pixel_size` (map units, e.g.
    degrees or metres depending on CRS) is required, and the reference
    extent is the union of every vector layer's own extent.

    `id_field` is the attribute field (integer, matching StateClasses.csv's
    Id column and the pixel-value convention every raster in this plugin
    uses) burned into the output raster for each vector layer.
    """
    if not any(_is_vector(l) for l in layers):
        return list(layers)  # nothing to do -- pure raster input, unchanged

    if not id_field:
        raise QgsProcessingException(
            "One or more inputs is a vector layer, so the ecosystem type "
            "ID field (matching StateClasses.csv's Id column) is required."
        )

    reference_raster = next((l for l in layers if _is_raster(l)), None)

    if reference_raster is not None:
        ref_crs = reference_raster.crs()
        ref_extent = reference_raster.extent()
        ref_width = reference_raster.width()
        ref_height = reference_raster.height()
    else:
        if not pixel_size or pixel_size <= 0:
            raise QgsProcessingException(
                "All inputs are vector layers, so a pixel size (map units) "
                "is required to rasterize them -- none of the layers has "
                "an existing grid to match."
            )
        vector_layers = [l for l in layers if _is_vector(l)]
        ref_crs = vector_layers[0].crs()
        ref_extent = QgsRectangle(vector_layers[0].extent())
        for l in vector_layers[1:]:
            if l.crs() != ref_crs:
                feedback.pushWarning(
                    f"'{l.name()}' CRS ({l.crs().authid()}) differs from "
                    f"'{vector_layers[0].name()}'s ({ref_crs.authid()}) - "
                    "extents are being combined without reprojection; "
                    "reproject all vector layers to the same CRS first."
                )
            ref_extent.combineExtentWith(l.extent())
        ref_width = max(1, int(round(ref_extent.width() / pixel_size)))
        ref_height = max(1, int(round(ref_extent.height() / pixel_size)))

    # Validate every vector layer's field upfront, before importing
    # `processing` or doing any rasterization work, so a typo'd field
    # name fails fast with a clear message.
    for layer in layers:
        if _is_vector(layer) and layer.fields().indexFromName(id_field) < 0:
            raise QgsProcessingException(
                f"'{layer.name()}' has no field named '{id_field}' - set the "
                "ecosystem type ID field to match a real attribute column."
            )

    import os
    import tempfile
    import processing

    extent_str = (
        f"{ref_extent.xMinimum()},{ref_extent.xMaximum()},"
        f"{ref_extent.yMinimum()},{ref_extent.yMaximum()} [{ref_crs.authid()}]"
    )

    resolved = []
    for layer in layers:
        if _is_raster(layer):
            resolved.append(layer)
            continue
        if not _is_vector(layer):
            raise QgsProcessingException(f"'{layer.name()}' is neither a raster nor a vector layer.")

        feedback.pushInfo(f"Rasterizing '{layer.name()}' onto the reference grid ({ref_width}x{ref_height})...")
        out_path = os.path.join(tempfile.gettempdir(), f"seeaq_rasterized_{id(layer)}.tif")
        processing.run("gdal:rasterize", {
            "INPUT": layer,
            "FIELD": id_field,
            "BURN": 0,
            "USE_Z": False,
            "UNITS": 0,  # 0 = georeferenced units (width/height below), not pixel counts
            "WIDTH": ref_width,
            "HEIGHT": ref_height,
            "EXTENT": extent_str,
            "NODATA": 0,
            "OPTIONS": "",
            "DATA_TYPE": 1,  # Byte
            "INIT": None,
            "INVERT": False,
            "EXTRA": "",
            "OUTPUT": out_path,
        })

        rlayer = QgsRasterLayer(out_path, f"{layer.name()} (rasterized)")
        if not rlayer.isValid():
            raise QgsProcessingException(f"Rasterizing '{layer.name()}' failed - output raster is invalid.")
        resolved.append(rlayer)

    return resolved
