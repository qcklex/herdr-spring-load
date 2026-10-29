"""Golden tests: the renderer replica against screens of a real herdr 0.9.3
(tests/golden, recorded by tools/record_golden.py in scratch sessions)."""
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'tests'))
from spring_load import render
from vt import char_width

GOLDEN = ROOT/'tests/golden'
ICONS = set('●○·×◐✓◌◉')

def cells(text):
    out = []
    for ch in text:
        w = char_width(ch)
        if w:
            out.append(ch)
            if w == 2:
                out.append(None)
    return out

def screen_cells(line, width):
    row = cells(line)
    return row + [' '] * (width - len(row))

def load(name):
    """A fixture plus a model built from it, with its git repos recreated."""
    record = json.loads((GOLDEN/f'{name}.json').read_text())
    temp = tempfile.mkdtemp(prefix='sl-render.')
    text = json.dumps(record).replace('$TEMP', temp)
    record = json.loads(text)
    for repo in record.get('git_repos', []):
        git = Path(repo)/'.git'
        (git/'refs/heads').mkdir(parents=True)
        (git/'HEAD').write_text('ref: refs/heads/main\n')
    ui = render.ui_from_config(tomllib.loads(record['config']))
    prefs = render.preferences_from_json(record['preferences'])
    model = render.build_model(record['cols'], record['rows'], record['snapshot'], ui, prefs)
    return record, model, ui

class GoldenTest(unittest.TestCase):
    def assert_tabs(self, record, model, ui):
        grid = [screen_cells(l, record['cols']) for l in record['screen']]
        snapshot = record['snapshot']
        zoomed = {l['tab_id']: l['zoomed'] for l in snapshot['layouts']}
        tabs = {t['tab_id']: t for t in snapshot['tabs']}
        targets = [t for t in model.targets if t.kind == 'tab']
        focused = [t for t in snapshot['tabs'] if t['workspace_id'] == snapshot['focused_workspace_id']]
        self.assertEqual(len(targets), len(focused), 'one target per tab of the focused space')
        for target in targets:
            label = tabs[target.id]['label'] + (' Z' if zoomed.get(target.id) else '')
            shown = cells(label)
            left = (target.rect.width - len(shown)) // 2
            expected = [' '] * left + shown + [' '] * (target.rect.width - left - len(shown))
            self.assertEqual(grid[target.rect.y][target.rect.x:target.rect.right], expected, f'tab {label!r}')
        if targets and ui.mouse_capture:
            last = max(targets, key=lambda t: t.rect.x)
            self.assertEqual(''.join(grid[last.rect.y][last.rect.right + 1:last.rect.right + 4]), ' + ', 'new-tab control follows')

    def assert_spaces(self, record, model):
        grid = [screen_cells(l, record['cols']) for l in record['screen']]
        workspaces = {w['workspace_id']: w for w in record['snapshot']['workspaces']}
        targets = [t for t in model.targets if t.kind == 'space']
        self.assertEqual(len(targets), len(workspaces))
        for index, target in enumerate(sorted(targets, key=lambda t: t.rect.y)):
            row = grid[target.rect.y]
            label = cells(workspaces[target.id]['label'])
            if target.rect.width <= 4:
                self.assertEqual(''.join(c for c in row[:2] if c), f'{index + 1:<2}', 'collapsed number')
                self.assertIn(row[2], ICONS)
                continue
            self.assertEqual(row[0], ' ')
            self.assertIn(row[1], ICONS, f'icon of {workspaces[target.id]["label"]}')
            self.assertEqual(row[2:3 + len(label)], [' '] + label)
            for extra in range(1, target.rect.height):
                below = grid[target.rect.y + extra]
                self.assertEqual(below[:3], [' '] * 3)
                self.assertNotEqual(below[3], ' ', 'continuation row (branch)')
        # Nothing else in the Spaces section looks like an entry.
        spaces_rows = {t.rect.y for t in targets}
        if targets and targets[0].rect.width > 4:
            bottom = max(t.rect.bottom for t in targets)
            for y in range(2, bottom + 3):
                if y not in spaces_rows and y < len(grid):
                    self.assertNotIn(grid[y][1], ICONS, f'row {y} is an entry the replica missed')

    def assert_panes(self, record, model):
        grid = [screen_cells(l, record['cols']) for l in record['screen']]
        panes = [t for t in model.targets if t.kind == 'pane']
        if len(panes) < 2:
            return # herdr draws borders only around split panes (pane_borders = auto)
        for target in panes:
            r = target.rect
            self.assertIn(grid[r.y][r.x], '┌╭', f'pane {target.id} top-left corner')
            self.assertIn(grid[r.bottom - 1][r.right - 1], '┘╯', f'pane {target.id} bottom-right corner')

    def check(self, name, kinds, empty=()):
        record, model, ui = load(name)
        self.assertEqual(record['herdr'], '0.9.3')
        for kind in ('space', 'tab', 'pane'):
            if kind in kinds:
                self.assertNotIn(kind, model.refused, f'{name}: {kind}')
            else:
                self.assertIn(kind, model.refused, f'{name}: {kind} must be refused')
                self.assertFalse([t for t in model.targets if t.kind == kind])
        for kind in empty:
            self.assertEqual([t for t in model.targets if t.kind == kind], [], f'{name}: no {kind} targets')
        if 'tab' in kinds and 'tab' not in empty:
            self.assert_tabs(record, model, ui)
        if 'space' in kinds:
            self.assert_spaces(record, model)
        if 'pane' in kinds:
            self.assert_panes(record, model)
        return record, model

    def test_default(self):
        self.check('default', {'space', 'tab', 'pane'})

    def test_spaces_tabs_and_split(self):
        _, model = self.check('spaces_tabs', {'space', 'tab', 'pane'})
        self.assertEqual(len([t for t in model.targets if t.kind == 'pane']), 2)

    def test_zoomed_tab_label(self):
        self.check('zoomed', {'space', 'tab', 'pane'}, empty={'pane'})

    def test_branch_rows(self):
        _, model = self.check('git_rows', {'space', 'tab', 'pane'})
        self.assertEqual([t.rect.height for t in sorted((t for t in model.targets if t.kind == 'space'), key=lambda t: t.rect.y)], [1, 2, 1, 2])

    def test_bottom_tab_bar_wide_sidebar_row_gap(self):
        _, model = self.check('bottom_wide_gap', {'space', 'tab', 'pane'})
        tab = next(t for t in model.targets if t.kind == 'tab')
        self.assertEqual(tab.rect.y, 39)

    def test_single_row_spaces(self):
        self.check('one_row_spaces', {'space', 'tab', 'pane'})

    def test_collapsed_sidebar_from_preferences(self):
        self.check('collapsed_prefs', {'space', 'tab', 'pane'})

    def test_preferences_width_and_split(self):
        self.check('narrow_prefs_split', {'space', 'tab', 'pane'})

    def test_hidden_single_tab_bar(self):
        self.check('hidden_single_tab', {'space', 'tab', 'pane'}, empty={'tab'})

    def test_status_text_reserves_width(self):
        self.check('status_text', {'space', 'tab', 'pane'})

    def test_wide_labels(self):
        self.check('wide_labels', {'space', 'tab', 'pane'})

    def test_overflow_refused(self):
        self.check('tab_overflow', {'space', 'pane'})

    def test_space_scroll_refused(self):
        self.check('space_scroll', {'tab', 'pane'})

    def test_mobile_refused(self):
        self.check('mobile', {'pane'})

class ReplicaRulesTest(unittest.TestCase):
    def test_unknown_version_goes_inert(self):
        record, _, ui = load('default')
        snapshot = dict(record['snapshot'], version='0.9.4')
        model = render.build_model(record['cols'], record['rows'], snapshot, ui, render.Preferences())
        self.assertEqual(model.targets, [])
        self.assertEqual(set(model.refused), {'space', 'tab', 'pane'})

    def test_surface_mismatch_refuses_everything(self):
        record, _, ui = load('spaces_tabs')
        prefs = render.Preferences(sidebar_width=30)   # the client actually uses 26
        model = render.build_model(record['cols'], record['rows'], record['snapshot'], ui, prefs)
        self.assertEqual(model.targets, [])
        self.assertIn('another size', model.refused['pane'])

    def test_unknown_git_state_refuses_spaces(self):
        record, _, ui = load('git_rows')
        model = render.build_model(record['cols'], record['rows'], record['snapshot'], ui, render.Preferences(),
                                   branch_present=lambda cwd: None)
        self.assertIn('space', model.refused)
        self.assertNotIn('tab', model.refused)

    def test_runtime_status_entries_refuse_tabs(self):
        record, _, _ = load('default')
        ui = render.ui_from_config({'ui': {'tab_bar_right': [{'type': 'datetime'}]}})
        model = render.build_model(record['cols'], record['rows'], record['snapshot'], ui, render.Preferences())
        self.assertIn('runtime width', model.refused['tab'])

    def test_emoji_labels_refuse(self):
        with self.assertRaises(render.Refused):
            render.display_width('build 🚀')
        self.assertEqual(render.display_width('表示 tab'), 8)

    def test_remote_endpoints_refuse_spaces(self):
        record, _, ui = load('default')
        model = render.build_model(record['cols'], record['rows'], record['snapshot'], ui, render.Preferences(), remote_endpoints=True)
        self.assertIn('remote machines', model.refused['space'])

    def test_worktree_groups_refuse_spaces(self):
        record, _, ui = load('git_rows')
        snapshot = json.loads(json.dumps(record['snapshot']))
        for w in snapshot['workspaces'][1:3]:
            w['worktree'] = {'repo_key': 'k', 'repo_name': 'r', 'repo_root': '/r', 'checkout_path': '/r', 'is_linked_worktree': False}
        model = render.build_model(record['cols'], record['rows'], snapshot, ui, render.Preferences())
        self.assertIn('worktree', model.refused['space'])

    def test_preferences_path_matches_herdr(self):
        # herdr's own test vector is not public; this pins FNV-1a 64 itself.
        self.assertEqual(render.preferences_path('', '/s').name, 'local-cbf29ce484222325.json')
        self.assertEqual(render.preferences_path('a', '/s').name, 'local-af63dc4c8601ec8c.json')

    def test_rust_rounding(self):
        self.assertEqual(render.section_heights(13, 0.5), (7, 6))
        self.assertEqual(render.section_heights(5, 0.5), (3, 2))
