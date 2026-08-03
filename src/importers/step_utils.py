"""Utilities for post-processing STEP 3D models."""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple
from logging import debug, warning, error


class StepProcessingUnavailable(RuntimeError):
    """Raised when the external OpenCascade runtime is unavailable."""


def _call_static(owner, name: str, *args):
    """Call an OCP static method across pre/post 7.8 binding conventions."""

    method = getattr(owner, f"{name}_s", None) or getattr(owner, name, None)
    if method is None:
        raise AttributeError(f"{owner.__name__}.{name} is unavailable")
    return method(*args)


def _load_ocp():
    try:
        from OCP.Bnd import Bnd_Box
        from OCP.BRepBndLib import BRepBndLib
        from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
        from OCP.gp import gp_Pnt, gp_Trsf, gp_Vec
        from OCP.IFSelect import IFSelect_RetDone
        from OCP.STEPControl import STEPControl_AsIs, STEPControl_Reader, STEPControl_Writer
    except ImportError as exc:
        raise StepProcessingUnavailable(
            "cadquery-ocp is required to normalize STEP models in the IPC plugin"
        ) from exc

    return {
        "Bnd_Box": Bnd_Box,
        "BRepBndLib": BRepBndLib,
        "BRepBuilderAPI_Transform": BRepBuilderAPI_Transform,
        "gp_Pnt": gp_Pnt,
        "gp_Trsf": gp_Trsf,
        "gp_Vec": gp_Vec,
        "IFSelect_RetDone": IFSelect_RetDone,
        "STEPControl_AsIs": STEPControl_AsIs,
        "STEPControl_Reader": STEPControl_Reader,
        "STEPControl_Writer": STEPControl_Writer,
    }


def _bounding_box(shape, ocp) -> Tuple[float, float, float, float, float, float]:
    box = ocp["Bnd_Box"]()
    _call_static(ocp["BRepBndLib"], "Add", shape, box)
    if box.IsVoid():
        raise ValueError("STEP model has no geometry")
    return tuple(float(value) for value in box.Get())


def _transform(shape, transformation, ocp):
    return ocp["BRepBuilderAPI_Transform"](shape, transformation, True).Shape()


def fixup_step_model(
    step_path: "str | Path",
    fit_dims_mils: Optional[Tuple[float, float]] = None,
    output_path: Optional["str | Path"] = None,
) -> bool:
    """Center a STEP model on XY and translate its base to Z=0.

    Optionally scales the model to fit the given width/height dimensions
    (given in mils, as stored in the EasyEDA ``3D Model Transform`` attribute).

    Returns True on success, False on failure.
    """
    source_path = Path(step_path)
    destination_path = Path(output_path) if output_path is not None else source_path
    file_name = source_path.stem
    ocp = _load_ocp()

    reader = ocp["STEPControl_Reader"]()
    if reader.ReadFile(str(source_path)) != ocp["IFSelect_RetDone"]:
        error("fixup_step_model: failed to load '%s'", file_name)
        return False
    if reader.TransferRoots() <= 0:
        error("fixup_step_model: STEP file '%s' contains no transferable roots", file_name)
        return False
    shape = reader.OneShape()

    if fit_dims_mils is not None:
        try:
            fit_x_mm = fit_dims_mils[0] / 39.37
            fit_y_mm = fit_dims_mils[1] / 39.37
            xmin, ymin, _zmin, xmax, ymax, _zmax = _bounding_box(shape, ocp)
            width = xmax - xmin
            height = ymax - ymin
            if width > 0 and height > 0:
                scale_x = fit_x_mm / width
                scale_y = fit_y_mm / height
                scale = (scale_x + scale_y) / 2
                if abs(scale_x - scale_y) > 0.1:
                    warning(
                        "fixup_step_model: scale factors do not match X=%.3f Y=%.3f for '%s' — model may be misoriented"
                        % (scale_x, scale_y, file_name)
                    )
                elif abs(scale - 1.0) > 0.01:
                    debug("fixup_step_model: scaling '%s' by %.4f" % (file_name, scale))
                    scale_transform = ocp["gp_Trsf"]()
                    scale_transform.SetScale(ocp["gp_Pnt"](0.0, 0.0, 0.0), scale)
                    shape = _transform(shape, scale_transform, ocp)
        except Exception as exc:
            warning("fixup_step_model: scale error for '%s': %s" % (file_name, exc))

    xmin, ymin, zmin, xmax, ymax, _zmax = _bounding_box(shape, ocp)
    translation = ocp["gp_Trsf"]()
    translation.SetTranslation(
        ocp["gp_Vec"](-((xmin + xmax) / 2.0), -((ymin + ymax) / 2.0), -zmin)
    )
    shape = _transform(shape, translation, ocp)

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    writer = ocp["STEPControl_Writer"]()
    if writer.Transfer(shape, ocp["STEPControl_AsIs"]) != ocp["IFSelect_RetDone"]:
        error("fixup_step_model: failed to prepare '%s' for saving", file_name)
        return False
    if writer.Write(str(destination_path)) != ocp["IFSelect_RetDone"]:
        error("fixup_step_model: failed to save '%s'", file_name)
        return False
    debug("fixup_step_model: saved '%s'" % file_name)
    return True
