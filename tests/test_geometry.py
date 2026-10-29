"""Grid fitting, target resolution and click calibration."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from spring_load import geometry, render

class GeometryTest(unittest.TestCase):
    def test_one_disagreeing_click_does_not_replace_calibration(self):
        # Review M3
        with tempfile.TemporaryDirectory() as temp:
            calibration = geometry.Calibration(Path(temp)/'c.json')
            grid = geometry.Grid(10, 10, 10, 10, geometry.Interval(0, 5), geometry.Interval(0, 60))
            tab = render.Rect(0, 0, 10, 1)
            self.assertEqual(calibration.learn('k', grid, 5, 32, tab), 'learned')      # origin y in [22, 32]
            self.assertEqual(calibration.learn('k', grid, 5, 25, tab), 'agrees')       # [22, 25]
            good = calibration.apply('k', grid).origin_y
            self.assertEqual(calibration.learn('k', grid, 5, 55, tab), 'disagrees')    # [45, 55]: misattributed?
            self.assertEqual(calibration.apply('k', grid).origin_y, good, 'kept the old origin')
            self.assertEqual(calibration.learn('k', grid, 5, 52, tab), 'relearned')    # a second click agrees
            self.assertEqual((calibration.apply('k', grid).origin_y.low, calibration.apply('k', grid).origin_y.high), (45, 52))
            self.assertEqual(calibration.learn('k', grid, 5, 200, tab), 'impossible')
            self.assertEqual(calibration.learn('p', grid, 5, 32, tab, provisional=True), 'candidate')   # review N1
            self.assertEqual(calibration.apply('p', grid).origin_y, grid.origin_y, 'a candidate is not used')
            self.assertEqual(calibration.learn('p', grid, 5, 25, tab, provisional=True), 'learned')
            self.assertEqual((calibration.apply('p', grid).origin_y.low, calibration.apply('p', grid).origin_y.high), (22, 25))
            self.assertEqual(calibration.calibrated(), 2)
            legacy = geometry.Calibration(Path(temp)/'old.json')
            legacy.data = {'k': [0, 1, 22, 25]}
            self.assertEqual(legacy.apply('k', grid).origin_y.high, 25, 'the first 0.3.0 format still loads')

    def test_fit_picks_the_unit_and_refuses_ambiguity(self):
        client = {'cols': 239, 'rows': 68, 'xpixel': 1916, 'ypixel': 1160}
        grid = geometry.fit((1920, 1164), client, 2.0)
        self.assertAlmostEqual(grid.cell_width, 1916 / 239)
        self.assertEqual((grid.origin_x.high, grid.origin_y.high), (4, 4))
        self.assertIsNone(geometry.fit((1000, 1164), client, 2.0), 'grid wider than the window')
        self.assertIsNone(geometry.fit((1920, 1164), {**client, 'xpixel': 0}, 2.0), 'no pixel size')
        retina = geometry.fit((960, 600), {'cols': 100, 'rows': 30, 'xpixel': 1904, 'ypixel': 1080}, 2.0)
        self.assertAlmostEqual(retina.cell_width, 9.52)

    def test_real_windows_console_frame_and_scrollbar(self):
        # Real CI run 40906804772: its 19 px frame/scrollbar remainder was
        # rejected despite a valid 120x30, 8x16 console grid.
        client = {'cols': 120, 'rows': 30, 'xpixel': 960, 'ypixel': 480,
                  'grid_origin': {'x': 1, 'y': 31}}
        grid = geometry.fit((979, 512), client, 1.0)
        self.assertIsNotNone(grid)
        self.assertEqual((grid.cell_width, grid.cell_height), (8, 16))
        self.assertEqual(grid.cells(1 + 119.5*8, 31 + 29.5*16), ((119,119), (29,29)))
        # Only the measured grid gets this treatment; generic matching stays strict.
        self.assertIsNone(geometry.fit((979,512), {k:v for k,v in client.items() if k!='grid_origin'},1.0))
        for origin in ({'x':-1,'y':31}, {'x':20,'y':31}, {'x':1,'y':33},
                       {'x':float('nan'),'y':31}, {'x':True,'y':31}):
            self.assertIsNone(geometry.fit((979,512), {**client,'grid_origin':origin},1.0))
        self.assertIsNone(geometry.fit((979,512),{**client,'xpixel':959},1.0))
        other = {**client,'grid_origin':{'x':2,'y':31}}
        self.assertNotEqual(geometry.calibration_key('conhost',(979,512),client),
                            geometry.calibration_key('conhost',(979,512),other))

    def test_resolve_needs_agreement(self):
        model = render.Model(10, 10, [render.Target('pane', 'a', render.Rect(0, 1, 5, 9)), render.Target('pane', 'b', render.Rect(5, 1, 5, 9)),
                                      render.Target('tab', 't', render.Rect(0, 0, 10, 1))])
        grid = geometry.Grid(10, 10, 10, 10, geometry.Interval(0, 5), geometry.Interval(0, 15))
        self.assertEqual(geometry.resolve(model, grid, 25, 60).id, 'a')
        self.assertIsNone(geometry.resolve(model, grid, 52, 60), 'straddles two panes')
        self.assertIsNone(geometry.resolve(model, grid, 25, 12), 'row 0 or row 1')
        calibrated = grid.narrowed(geometry.Interval(0, 0), geometry.Interval(2, 3))
        self.assertEqual(geometry.resolve(model, calibrated, 25, 9).id, 't')

if __name__ == '__main__':
    unittest.main()
