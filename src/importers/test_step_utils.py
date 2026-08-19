"""Tests for the out-of-process STEP normalization backend."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from . import step_utils
from .step_utils import fixup_step_model


class _Shape:
    def __init__(self, bounds):
        self.bounds = tuple(float(value) for value in bounds)


class _Box:
    def IsVoid(self):
        return not hasattr(self, "bounds")

    def Get(self):
        return self.bounds


class _Bounds:
    @staticmethod
    def Add_s(shape, box):
        box.bounds = shape.bounds


class _Point:
    def __init__(self, x, y, z):
        self.values = (x, y, z)


class _Vector(_Point):
    pass


class _Transformation:
    def SetScale(self, _origin, factor):
        self.scale = float(factor)

    def SetTranslation(self, vector):
        self.translation = vector.values


class _TransformShape:
    def __init__(self, shape, transform, _copy):
        xmin, ymin, zmin, xmax, ymax, zmax = shape.bounds
        if hasattr(transform, "scale"):
            factor = transform.scale
            self.shape = _Shape(
                (xmin * factor, ymin * factor, zmin * factor,
                 xmax * factor, ymax * factor, zmax * factor)
            )
        else:
            dx, dy, dz = transform.translation
            self.shape = _Shape(
                (xmin + dx, ymin + dy, zmin + dz,
                 xmax + dx, ymax + dy, zmax + dz)
            )

    def Shape(self):
        return self.shape


class _Reader:
    def ReadFile(self, _path):
        return "done"

    def TransferRoots(self):
        return 1

    def OneShape(self):
        return _Shape((0, 0, 1, 10, 20, 5))


class _Writer:
    written_shape = None
    written_path = None

    def Transfer(self, shape, _mode):
        type(self).written_shape = shape
        return "done"

    def Write(self, path):
        type(self).written_path = path
        return "done"


def _fake_ocp():
    return {
        "Bnd_Box": _Box,
        "BRepBndLib": _Bounds,
        "BRepBuilderAPI_Transform": _TransformShape,
        "gp_Pnt": _Point,
        "gp_Trsf": _Transformation,
        "gp_Vec": _Vector,
        "IFSelect_RetDone": "done",
        "STEPControl_AsIs": "as-is",
        "STEPControl_Reader": _Reader,
        "STEPControl_Writer": _Writer,
    }


class StepUtilsTests(unittest.TestCase):
    def test_model_is_scaled_centered_and_placed_on_z_zero(self):
        with TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.step"
            destination = Path(temp_dir) / "normalized.step"
            with patch.object(step_utils, "_load_ocp", return_value=_fake_ocp()):
                result = fixup_step_model(
                    source,
                    fit_dims_mils=(787.4, 1574.8),
                    output_path=destination,
                )

        self.assertTrue(result)
        self.assertEqual(_Writer.written_path, str(destination))
        self.assertEqual(
            _Writer.written_shape.bounds,
            (-10.0, -20.0, 0.0, 10.0, 20.0, 8.0),
        )


if __name__ == "__main__":
    unittest.main()
