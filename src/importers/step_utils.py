"""Utilities for post-processing STEP 3D models."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Tuple
from logging import debug, warning, error


def fixup_step_model(
    step_path: "str | Path",
    fit_dims_mils: Optional[Tuple[float, float]] = None,
) -> bool:
    """Center a STEP model on XY and translate its base to Z=0.

    Optionally scales the model to fit the given width/height dimensions
    (given in mils, as stored in the EasyEDA ``3D Model Transform`` attribute).

    Returns True on success, False on failure.
    """
    try:
        from pcbnew import UTILS_STEP_MODEL  # type: ignore
    except ImportError:
        return False

    step_path = str(step_path)
    file_name = os.path.splitext(os.path.basename(step_path))[0]

    model = UTILS_STEP_MODEL.LoadSTEP(step_path)
    if not model:
        error("fixup_step_model: failed to load '%s'" % file_name)
        return False

    if fit_dims_mils is not None:
        try:
            fit_x_mm = fit_dims_mils[0] / 39.37
            fit_y_mm = fit_dims_mils[1] / 39.37
            bbox = model.GetBoundingBox()
            bsize = bbox.GetSize()
            if bsize.x > 0 and bsize.y > 0:
                scale_x = fit_x_mm / bsize.x
                scale_y = fit_y_mm / bsize.y
                scale = (scale_x + scale_y) / 2
                if abs(scale_x - scale_y) > 0.1:
                    warning(
                        "fixup_step_model: scale factors do not match X=%.3f Y=%.3f for '%s' — model may be misoriented"
                        % (scale_x, scale_y, file_name)
                    )
                elif abs(scale - 1.0) > 0.01:
                    debug("fixup_step_model: scaling '%s' by %.4f" % (file_name, scale))
                    model.Scale(scale)
        except Exception as exc:
            warning("fixup_step_model: scale error for '%s': %s" % (file_name, exc))

    bbox = model.GetBoundingBox()
    center = bbox.GetCenter()
    model.Translate(-center.x, -center.y, -bbox.Min().z)

    model.SaveSTEP(step_path)
    debug("fixup_step_model: saved '%s'" % file_name)
    return True
