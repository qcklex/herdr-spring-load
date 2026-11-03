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

    def test_ghostty_resize_reuses_only_compatible_precise_offsets(self):
        with tempfile.TemporaryDirectory() as temp:
            cal = geometry.Calibration(Path(temp)/'c.json')
            old = {'cols':126,'rows':54,'xpixel':1013,'ypixel':928}
            new = {'cols':272,'rows':67,'xpixel':2179,'ypixel':1141}
            app = 'com.mitchellh.ghostty'
            old_key = geometry.calibration_key(app,(1017,964),old)
            key = geometry.calibration_key(app,(2183,1177),new)
            cal.data[old_key] = {'origin':[0,4,31,36], 'candidate':None}
            grid = geometry.fit((2183,1177),new,1)
            precise = cal.apply(key,grid)
            self.assertEqual(precise.origin_y,geometry.Interval(31,36))
            self.assertEqual(geometry.Calibration(cal.path).apply(key,grid).origin_y,precise.origin_y)
            # A pane-only observation for the new size must also be refined.
            cal.data[key] = {'origin':[0,4,0,36], 'candidate':None}
            self.assertEqual(cal.apply(key,grid).origin_y,geometry.Interval(31,36))

    def test_terminal_app_resize_reuses_its_calibration(self):
        # Terminal.app measured at three sizes: 37 x 49 points of chrome each
        # time while windowed, 37 x 17 in fullscreen.
        with tempfile.TemporaryDirectory() as temp:
            cal = geometry.Calibration(Path(temp)/'c.json')
            app = 'com.apple.Terminal'
            small = {'cols':120,'rows':30,'xpixel':840,'ypixel':450}
            cal.data[geometry.calibration_key(app,(877,499),small)] = {'origin':[2,9,33,39], 'candidate':None}
            large = {'cols':152,'rows':41,'xpixel':1064,'ypixel':615}
            grid = geometry.fit((1101,664),large,2.0)
            self.assertEqual(cal.apply(geometry.calibration_key(app,(1101,664),large),grid).origin_y,
                             geometry.Interval(33,39))
            full = {'cols':269,'rows':78,'xpixel':1883,'ypixel':1183}
            grid = geometry.fit((1920,1200),full,2.0)
            self.assertEqual(cal.apply(geometry.calibration_key(app,(1920,1200),full),grid),grid,
                             'fullscreen has other chrome and needs its own calibration')

    def test_ghostty_resize_refuses_changed_chrome_fonts_and_conflicting_offsets(self):
        with tempfile.TemporaryDirectory() as temp:
            cal = geometry.Calibration(Path(temp)/'c.json')
            old = {'cols':126,'rows':54,'xpixel':1013,'ypixel':928}
            app='com.mitchellh.ghostty'
            old_key=geometry.calibration_key(app,(1017,964),old)
            cal.data[old_key]={'origin':[0,4,31,36],'candidate':None}
            for owner,bounds,client in (
                (app,(2183,1145),{'cols':272,'rows':67,'xpixel':2179,'ypixel':1141}),
                (app,(2183,1244),{'cols':272,'rows':67,'xpixel':2179,'ypixel':1208}),
                ('another.terminal',(2183,1177),{'cols':272,'rows':67,'xpixel':2179,'ypixel':1141}),
            ):
                grid=geometry.fit(bounds,client,1)
                self.assertEqual(cal.apply(geometry.calibration_key(owner,bounds,client),grid),grid)
            client={'cols':272,'rows':67,'xpixel':2179,'ypixel':1141}
            grid=geometry.fit((2183,1177),client,1)
            key=geometry.calibration_key(app,(2183,1177),client)
            other={'cols':200,'rows':60,'xpixel':1602,'ypixel':1022}
            cal.data[geometry.calibration_key(app,(1606,1058),other)]={'origin':[0,4,1,5],'candidate':None}
            self.assertEqual(cal.apply(key,grid),grid,'inconsistent chrome offsets cannot be transferred')
            cal.data.pop(geometry.calibration_key(app,(1606,1058),other))
            cal.data[key]={'origin':[0,4,0,36],'candidate':[0,4,0,3]}
            self.assertEqual(cal.apply(key,grid),grid,'a pending disagreement must not be overwritten')

    def test_fit_picks_the_unit_and_refuses_ambiguity(self):
        client = {'cols': 239, 'rows': 68, 'xpixel': 1916, 'ypixel': 1160}
        grid = geometry.fit((1920, 1164), client, 2.0)
        self.assertAlmostEqual(grid.cell_width, 1916 / 239)
        self.assertEqual((grid.origin_x.high, grid.origin_y.high), (4, 4))
        self.assertIsNone(geometry.fit((1000, 1164), client, 2.0), 'grid wider than the window')
        self.assertIsNone(geometry.fit((1920, 1164), {**client, 'xpixel': 0}, 2.0), 'no pixel size')
        retina = geometry.fit((960, 600), {'cols': 100, 'rows': 30, 'xpixel': 1904, 'ypixel': 1080}, 2.0)
        self.assertAlmostEqual(retina.cell_width, 9.52)

    def test_terminal_app_inset_and_scrollbar_gutter(self):
        # Measured on macOS Terminal.app: a 120x30 window reports 840x450 pixels
        # in a 877x499 frame, 37 points (over five columns) of side chrome.
        client = {'cols': 120, 'rows': 30, 'xpixel': 840, 'ypixel': 450}
        grid = geometry.fit((877, 499), client, 2.0)
        self.assertIsNotNone(grid)
        self.assertEqual((grid.cell_width, grid.cell_height), (7, 15))
        self.assertEqual((grid.origin_x, grid.origin_y), (geometry.Interval(0, 37), geometry.Interval(0, 49)))
        self.assertIsNone(geometry.fit((889, 499), client, 2.0), 'beyond 48 points and 1.5 cells')

    def test_real_windows_console_frame_and_scrollbar(self):
        # Real CI run 40906804772: its 19 px frame/scrollbar remainder was
        # rejected despite a valid 120x30, 8x16 console grid.
        client = {'cols': 120, 'rows': 30, 'xpixel': 960, 'ypixel': 480,
                  'grid_origin': {'x': 1, 'y': 31}}
        grid = geometry.fit((979, 512), client, 1.0)
        self.assertIsNotNone(grid)
        self.assertEqual((grid.cell_width, grid.cell_height), (8, 16))
        self.assertEqual(grid.cells(1 + 119.5*8, 31 + 29.5*16), ((119,119), (29,29)))
        # Only the measured grid is placed exactly; generic matching leaves the
        # whole remainder uncertain, and refuses one beyond the side allowance.
        generic = geometry.fit((979,512), {k:v for k,v in client.items() if k!='grid_origin'},1.0)
        self.assertEqual((generic.origin_x, generic.origin_y), (geometry.Interval(0,19), geometry.Interval(0,32)))
        self.assertIsNone(geometry.fit((1009,512), {k:v for k,v in client.items() if k!='grid_origin'},1.0))
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

# Window shapes recorded from Ghostty 1.4 at one font size: (width, height, cols, rows, xpixel, ypixel).
GHOSTTY_SHAPES = [(1017, 964, 126, 54, 1013, 928), (1920, 1200, 239, 70, 1916, 1196), (2560, 1440, 319, 84, 2556, 1436),
                  (2183, 1177, 272, 67, 2179, 1141), (1920, 1170, 239, 66, 1916, 1134)]

class GhosttyCellSizeTest(unittest.TestCase):
    def client(self, shape):
        _, _, cols, rows, xpixel, ypixel = shape
        return {'cols': cols, 'rows': rows, 'xpixel': xpixel, 'ypixel': ypixel}

    def test_every_window_gets_the_one_cell_the_font_has(self):
        for shape in GHOSTTY_SHAPES:
            grid = geometry.fit(shape[:2], self.client(shape), 2.0, whole_cells=True)
            self.assertEqual((grid.cell_width, grid.cell_height), (8, 17), shape)

    def test_dividing_the_surface_is_a_row_off_low_in_the_window(self):
        # The window is 239x66 with 34 points above row 0. The centre of row 60.
        shape = GHOSTTY_SHAPES[4]
        dy = 34 + 60.5 * 17
        whole = geometry.fit(shape[:2], self.client(shape), 2.0, whole_cells=True)
        ratio = geometry.fit(shape[:2], self.client(shape), 2.0)
        row = lambda grid: int((dy - 34) // grid.cell_height)
        self.assertEqual((row(whole), row(ratio)), (60, 59))

    def test_half_point_cells_on_a_retina_display(self):
        client = {'cols': 100, 'rows': 40, 'xpixel': 853, 'ypixel': 703}   # 8.5 x 17.5 points, surface left over
        grid = geometry.fit((857, 739), client, 2.0, whole_cells=True)
        self.assertEqual((grid.cell_width, grid.cell_height), (8.5, 17.5))

    def test_other_terminals_keep_the_exact_ratio(self):
        client = {'cols': 100, 'rows': 40, 'xpixel': 853, 'ypixel': 703}
        grid = geometry.fit((857, 739), client, 2.0)
        self.assertEqual((grid.cell_width, grid.cell_height), (8.53, 17.575))

    def test_a_resized_window_needs_no_click(self):
        # One calibrated window of each kind, then windows of sizes never seen.
        with tempfile.TemporaryDirectory() as temp:
            calibration = geometry.Calibration(Path(temp)/'calibration.json')
            seen = (1017, 964, 126, 54, 1013, 928)
            calibration.data[geometry.calibration_key(geometry.GHOSTTY, seen[:2], self.client(seen))] = \
                {'origin': [1.5, 2.5, 32, 36], 'candidate': None}
            for width, height in ((800, 600), (1300, 900), (1920, 1170), (2183, 1177), (640, 480)):
                xpixel, ypixel = width - 4, height - 36
                shape = (width, height, xpixel // 8, ypixel // 17, xpixel, ypixel)
                grid = geometry.fit(shape[:2], self.client(shape), 2.0, whole_cells=True)
                key = geometry.calibration_key(geometry.GHOSTTY, shape[:2], self.client(shape))
                applied = calibration.apply(key, grid)
                self.assertLessEqual(applied.origin_y.width, grid.cell_height / 2, shape)

if __name__ == '__main__':
    unittest.main()
