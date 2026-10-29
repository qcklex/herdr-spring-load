"""Replica of herdr 0.9.3's client shell geometry: where the sidebar Spaces, the
tab bar tabs and the panes sit on a client's grid. Every function either returns
exact cell rectangles or refuses (no targets plus a reason): a skipped
navigation is acceptable, a wrong one is not.

Sources (herdr v0.9.3, 7b116c0):
  layout       src/client/shell/config.rs        ClientShellConfig::layout
  tab bar      src/client/shell/tabs.rs          render_tab_bar, tab_bar_content_area
  sidebar      src/client/shell/sidebar.rs       render_sidebar, render_collapsed_sidebar,
                                                 workspace_entries, workspace_rows
               src/ui/sidebar.rs                 sidebar_section_heights
               src/ui/sidebar/tokens.rs          space_rows
               src/client/shell/scroll.rs        list_scroll_metrics
  preferences  src/client/shell/preferences.rs   path_for_local_endpoint
  git branch   src/workspace/git/discovery.rs    git_branch
"""
from dataclasses import dataclass, field
import os
from pathlib import Path
import unicodedata

SUPPORTED_VERSIONS = frozenset({'0.9.3'})

MIN_TAB_WIDTH = 8
NEW_TAB_WIDTH = 3
TAB_SCROLL_BUTTON_WIDTH = 3
MIN_TAB_STRIP_WIDTH = MIN_TAB_WIDTH + NEW_TAB_WIDTH + 2 * TAB_SCROLL_BUTTON_WIDTH
WORKSPACE_HEADER_ROWS = 2
DEFAULT_SPACE_ROWS = [['state_icon', 'workspace'], ['branch', 'git_status']]
SPACE_TOKENS = {'state_icon', 'state_text', 'workspace', 'branch', 'git_status'}

class Refused(Exception):
    """The layout cannot be replicated exactly; the message says why."""

@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    @property
    def right(self):
        return self.x + self.width

    @property
    def bottom(self):
        return self.y + self.height

    def contains(self, column, row):
        return self.x <= column < self.right and self.y <= row < self.bottom

@dataclass(frozen=True)
class Target:
    kind: str    # 'space', 'tab' or 'pane'
    id: str      # workspace_id, tab_id or pane_id
    rect: Rect   # grid cells of the whole client

@dataclass
class Model:
    """The hit map of one client: targets plus why some kinds are missing."""
    cols: int
    rows: int
    targets: list = field(default_factory=list)
    refused: dict = field(default_factory=dict)   # kind -> reason

    def at(self, column, row):
        return next((t for t in self.targets if t.rect.contains(column, row)), None)

    def find(self, kind, target_id):
        return next((t for t in self.targets if t.kind == kind and t.id == target_id), None)

# MARK: settings

@dataclass
class Ui:
    """The ui settings the shell geometry depends on, with herdr's defaults."""
    sidebar_width: int = 26
    sidebar_min_width: int = 18
    sidebar_max_width: int = 36
    sidebar_start_collapsed: bool = False
    sidebar_collapsed_mode: str = 'compact'
    mobile_width_threshold: int = 64
    mouse_capture: bool = True
    hide_tab_bar_when_single_tab: bool = False
    tab_bar_position: str = 'top'
    tab_bar_right: list = field(default_factory=list)
    tab_bar_right_separator: str = ' '
    space_rows: list = field(default_factory=lambda: [list(r) for r in DEFAULT_SPACE_ROWS])
    space_row_gap: int = 0
    problems: list = field(default_factory=list)   # settings that cannot be replicated

def _int_setting(table, key, default, problems, low=0, high=65535):
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        problems.append(f'ui.{key} = {value!r} is not understood')
        return default
    return value

def ui_from_config(parsed):
    """Ui from a parsed herdr config.toml (a dict, as tomllib returns it)."""
    ui = Ui()
    table = parsed.get('ui', {}) if isinstance(parsed, dict) else {}
    if not isinstance(table, dict):
        ui.problems.append('ui is not a table')
        return ui
    p = ui.problems
    ui.sidebar_width = _int_setting(table, 'sidebar_width', ui.sidebar_width, p)
    ui.sidebar_min_width = _int_setting(table, 'sidebar_min_width', ui.sidebar_min_width, p)
    ui.sidebar_max_width = _int_setting(table, 'sidebar_max_width', ui.sidebar_max_width, p)
    ui.mobile_width_threshold = _int_setting(table, 'mobile_width_threshold', ui.mobile_width_threshold, p)
    for key in ('sidebar_start_collapsed', 'mouse_capture', 'hide_tab_bar_when_single_tab'):
        value = table.get(key, getattr(ui, key))
        if isinstance(value, bool):
            setattr(ui, key, value)
        else:
            p.append(f'ui.{key} = {value!r} is not understood')
    mode = table.get('sidebar_collapsed_mode', 'compact')
    if mode in ('compact', 'hidden'):
        ui.sidebar_collapsed_mode = mode
    else:
        p.append(f'ui.sidebar_collapsed_mode = {mode!r} is not understood')
    position = table.get('tab_bar_position', 'top')
    if position in ('top', 'bottom'):
        ui.tab_bar_position = position
    else:
        p.append(f'ui.tab_bar_position = {position!r} is not understood')
    right = table.get('tab_bar_right', [])
    ui.tab_bar_right = right if isinstance(right, list) else []
    if not isinstance(right, list):
        p.append('ui.tab_bar_right is not a list')
    separator = table.get('tab_bar_right_separator', ' ')
    ui.tab_bar_right_separator = separator if isinstance(separator, str) else ' '
    sidebar = table.get('sidebar', {})
    spaces = sidebar.get('spaces', {}) if isinstance(sidebar, dict) else {}
    if isinstance(spaces, dict):
        rows = spaces.get('rows', DEFAULT_SPACE_ROWS)
        if isinstance(rows, list) and len(rows) <= 16 and all(isinstance(r, list) and len(r) <= 16 for r in rows):
            ui.space_rows = [list(r) for r in rows]
        else:
            p.append('ui.sidebar.spaces.rows is not understood')
        ui.space_row_gap = _int_setting(spaces, 'row_gap', 0, p)
    return ui

@dataclass
class Preferences:
    """The client's saved chrome (state_dir/client-shell/local-<hash>.json)."""
    sidebar_width: int | None = None
    sidebar_section_split: float | None = None
    sidebar_collapsed: bool | None = None
    collapsed_groups: tuple = ()

def preferences_path(client_socket, state_dir):
    """herdr's FNV-1a 64 of the client socket path (<session dir>/herdr-client.sock,
    server/socket_paths.rs), as path_for_local_endpoint does."""
    value = 0xcbf29ce484222325
    for byte in str(client_socket).encode('utf-8', 'surrogateescape'):
        value ^= byte
        value = (value * 0x100000001b3) & 0xFFFFFFFFFFFFFFFF
    return Path(state_dir)/'client-shell'/f'local-{value:016x}.json'

def preferences_from_json(data):
    if not isinstance(data, dict):
        return Preferences()
    width = data.get('sidebar_width')
    split = data.get('sidebar_section_split')
    collapsed = data.get('sidebar_collapsed')
    groups = data.get('collapsed_groups', [])
    return Preferences(
        sidebar_width=width if isinstance(width, int) and not isinstance(width, bool) and width >= 0 else None,
        sidebar_section_split=float(split) if isinstance(split, (int, float)) and not isinstance(split, bool) else None,
        sidebar_collapsed=collapsed if isinstance(collapsed, bool) else None,
        collapsed_groups=tuple(g for g in groups if isinstance(g, str)) if isinstance(groups, list) else ())

# MARK: text width

def display_width(text):
    """Cell width of a label the way unicode-width measures it, for the
    characters where the two provably agree. Anything else (emoji, zero-width
    joiners, ambiguous East Asian width, controls) refuses."""
    width = 0
    for ch in text:
        code = ord(ch)
        if 0x20 <= code < 0x7F:
            width += 1
            continue
        category = unicodedata.category(ch)
        east = unicodedata.east_asian_width(ch)
        if category in ('Mn', 'Me') and code < 0x1F000:
            continue
        if category[0] in 'LNP' or category in ('Sm', 'Sc', 'Zs'):
            if east in ('W', 'F'):
                width += 2
                continue
            # unicode-width's default (non-CJK) width counts ambiguous as 1.
            if east in ('N', 'Na', 'H', 'A') and not 0x2E80 <= code <= 0xA4CF:
                width += 1
                continue
        raise Refused(f'label {text!r} has characters whose width herdr may measure differently')
    return width

# MARK: shell layout

@dataclass(frozen=True)
class ShellLayout:
    sidebar: Rect
    tab_bar: Rect
    mobile_header: Rect
    pane_surface: Rect
    sidebar_collapsed: bool

def sidebar_bounds(ui):
    return (ui.sidebar_min_width, ui.sidebar_max_width) if ui.sidebar_min_width <= ui.sidebar_max_width else (18, 36)

def shell_layout(cols, rows, ui, prefs, tab_count):
    """ClientShellConfig::layout with the client's initial sidebar state."""
    if cols <= ui.mobile_width_threshold:
        header = min(rows, 2)
        return ShellLayout(Rect(0, 0, 0, 0), Rect(0, 0, 0, 0), Rect(0, 0, cols, header),
                           Rect(0, header, cols, max(0, rows - header)), False)
    collapsed = prefs.sidebar_collapsed if prefs.sidebar_collapsed is not None else ui.sidebar_start_collapsed
    low, high = sidebar_bounds(ui)
    if collapsed:
        width = 4 if ui.sidebar_collapsed_mode == 'compact' else 0
    else:
        width = min(max(prefs.sidebar_width if prefs.sidebar_width is not None else ui.sidebar_width, low), high)
    width = min(width, max(0, cols - 1))
    main_width = max(0, cols - width)
    show_tab_bar = rows > 1 and not (ui.hide_tab_bar_when_single_tab and tab_count == 1)
    tab_height = int(show_tab_bar)
    if ui.tab_bar_position == 'top':
        tab_bar = Rect(width, 0, main_width, tab_height)
        surface = Rect(width, tab_height, main_width, max(0, rows - tab_height))
    else:
        tab_bar = Rect(width, max(0, rows - tab_height), main_width, tab_height)
        surface = Rect(width, 0, main_width, max(0, rows - tab_height))
    return ShellLayout(Rect(0, 0, width, rows), tab_bar, Rect(0, 0, 0, 0), surface, collapsed)

# MARK: tab bar

def tab_bar_status_width(ui, focused_tab_zoomed):
    """Width of the right-hand status segments, from the config alone. Entries
    whose text herdr computes at runtime (hostname, datetime, command) refuse."""
    texts = []
    for entry in ui.tab_bar_right[:16]:
        kind = entry.get('type') if isinstance(entry, dict) else None
        if kind == 'zoom':
            if focused_tab_zoomed:
                texts.append('ZOOM')
        elif kind == 'text' and isinstance(entry.get('text'), str):
            if entry['text']:
                texts.append(entry['text'])
        else:
            raise Refused(f'tab_bar_right entry {kind!r} has a runtime width')
    if not texts:
        return 0
    return sum(display_width(t) for t in texts) + display_width(ui.tab_bar_right_separator) * (len(texts) - 1)

def tab_targets(area, tabs, ui, zoomed):
    """render_tab_bar without overflow; overflow scrolls by a client-local offset, so it refuses."""
    if area.height == 0:
        return []
    labels = [t['label'] + (' Z' if zoomed.get(t['tab_id']) else '') for t in tabs]
    widths = [max(display_width(label) + 4, MIN_TAB_WIDTH) for label in labels]
    focused_zoomed = any(t.get('focused') and zoomed.get(t['tab_id']) for t in tabs)
    status = tab_bar_status_width(ui, focused_zoomed)
    reserved = status + 1 if status and area.width - (status + 1) >= MIN_TAB_STRIP_WIDTH else 0
    content_width = max(0, area.width - reserved)
    new_tab = NEW_TAB_WIDTH if ui.mouse_capture else 0
    total = sum(widths) + max(0, len(tabs) - 1) + new_tab
    if total > content_width:
        raise Refused('the tab bar overflows and scrolls')
    targets = []
    x = area.x
    for tab, width in zip(tabs, widths):
        targets.append(Target('tab', tab['tab_id'], Rect(x, area.y, width, 1)))
        x += width + 1
    return targets

# MARK: sidebar

def collapsed_sidebar_targets(area, workspaces):
    content = Rect(area.x, area.y, max(0, area.width - 1), area.height)
    if content.width == 0 or content.height == 0:
        return []
    height = content.height if content.height < 7 else (content.height + 1) // 2
    return [Target('space', w['workspace_id'], Rect(content.x, content.y + i, content.width, 1))
            for i, w in enumerate(workspaces[:height])]

def section_heights(total, split):
    if total == 0:
        return 0, 0
    if total < 6:
        top = (total + 1) // 2
        return top, total - top
    top = round_half_away(total * min(max(split, 0.1), 0.9))
    top = min(max(top, 3), total - 3)
    return top, total - top

def round_half_away(value):
    # Rust's f32::round rounds half away from zero; Python's round() does not.
    return int(value + 0.5) if value >= 0 else -int(-value + 0.5)

def worktree_groups(workspaces):
    """Keys of the worktree groups the sidebar would draw (workspace_entries)."""
    members = {}
    for w in workspaces:
        tree = w.get('worktree')
        if isinstance(tree, dict) and tree.get('repo_key'):
            members.setdefault(tree['repo_key'], []).append(tree)
    return {k for k, trees in members.items() if len(trees) >= 2 and any(not t.get('is_linked_worktree') for t in trees)}

def space_row_count(rows_config, workspace, branch):
    """Rows of one workspace entry (space_rows): a row is drawn when at least
    one of its tokens resolves. `branch` is True, False or None (unknown)."""
    count = 0
    tokens = workspace.get('tokens') or {}
    for row in rows_config:
        present = False
        unknown = False
        for token in row:
            if not isinstance(token, str):
                raise Refused('styled sidebar tokens are not replicated')
            if token in ('state_icon', 'state_text', 'workspace'):
                present = True
            elif token == 'branch':
                if branch is None:
                    unknown = True
                present = present or bool(branch)
            elif token == 'git_status':
                # Ahead/behind needs an upstream, which needs a branch; with a
                # branch the count itself is unknown here.
                if branch is None or branch:
                    unknown = True
            elif token.startswith('$') and len(token) > 1:
                present = present or token[1:] in tokens
            else:
                raise Refused(f'unknown sidebar token {token!r}')
        if not present and unknown:
            raise Refused('a sidebar row depends on git state that cannot be read')
        count += int(present)
    return max(count, 1)

def expanded_sidebar_targets(area, workspaces, ui, prefs, branch_of):
    """render_sidebar's workspace rects when the whole list fits (no scrolling)."""
    split = prefs.sidebar_section_split
    split = 0.5 if split is None or split != split or split in (float('inf'), float('-inf')) else min(max(split, 0.1), 0.9)
    content = Rect(area.x, area.y, max(0, area.width - 1), area.height)
    if content.width == 0 or content.height == 0:
        return []
    top, _ = section_heights(content.height, split)
    section = Rect(content.x, content.y, content.width, top)
    body = Rect(section.x, section.y + WORKSPACE_HEADER_ROWS, section.width, max(0, section.height - WORKSPACE_HEADER_ROWS - 1))
    if body.height == 0 or not workspaces:
        return []
    if worktree_groups(workspaces):
        raise Refused('worktree groups are not replicated')
    heights = [min(space_row_count(ui.space_rows, w, branch_of(w)), body.height) for w in workspaces]
    gap = ui.space_row_gap
    if sum(heights) + gap * (len(heights) - 1) > body.height:
        raise Refused('the Spaces list scrolls')
    targets = []
    y = body.y
    for w, height in zip(workspaces, heights):
        targets.append(Target('space', w['workspace_id'], Rect(body.x, y, body.width, height)))
        y += height + gap
    return targets

# MARK: git branch (for the optional branch row)

def git_branch_present(cwd):
    """Whether herdr's git_branch(cwd) is Some: True, False, or None when this
    replica cannot tell (reftable, unreadable files, bare layouts)."""
    try:
        path = Path(cwd)
        if not path.is_absolute():
            return None
        root = None
        for candidate in [path, *path.parents]:
            if (candidate/'.git').exists():
                root = candidate
                break
        if root is None:
            return False
        git = root/'.git'
        if git.is_dir():
            git_dir = git
        else:
            text = git.read_text(errors='strict').strip()
            if not text.startswith('gitdir:'):
                return None
            target = Path(text[len('gitdir:'):].strip())
            git_dir = target if target.is_absolute() else root/target
        common = git_dir
        common_file = git_dir/'commondir'
        if common_file.exists():
            value = Path(common_file.read_text().strip())
            common = value if value.is_absolute() else git_dir/value
        config = common/'config'
        if config.exists() and 'reftable' in config.read_text(errors='replace').lower():
            return None
        head = (git_dir/'HEAD').read_text(errors='strict').strip()
        branch = head[len('ref: refs/heads/'):] if head.startswith('ref: refs/heads/') else ''
        return bool(branch)
    except (OSError, UnicodeDecodeError, ValueError):
        return None

def identity_cwd(snapshot, workspace_id):
    """The cwd herdr names a workspace by: its first tab's root pane. The API
    does not say which pane is the root, so all panes of that tab must agree."""
    tabs = sorted((t for t in snapshot.get('tabs', []) if t.get('workspace_id') == workspace_id),
                  key=lambda t: t.get('number', 0))
    if not tabs:
        return None
    cwds = {p.get('cwd') for p in snapshot.get('panes', []) if p.get('tab_id') == tabs[0]['tab_id']}
    return cwds.pop() if len(cwds) == 1 else None

# MARK: whole model

def pane_targets(surface, layout):
    """Pane rects from pane.layout, which must describe this client's surface."""
    if not layout:
        raise Refused('no pane layout')
    area = layout.get('area', {})
    if (area.get('width'), area.get('height')) != (surface.width, surface.height):
        raise Refused('herdr reports a pane area of another size; the client geometry differs from this replica')
    if layout.get('zoomed'):
        return []
    targets = []
    for pane in layout.get('panes', []):
        r = pane.get('rect', {})
        rect = Rect(surface.x + r['x'] - area.get('x', 0), surface.y + r['y'] - area.get('y', 0), r['width'], r['height'])
        if rect.x < surface.x or rect.y < surface.y or rect.right > surface.right or rect.bottom > surface.bottom:
            raise Refused('a pane lies outside the pane surface')
        targets.append(Target('pane', pane['pane_id'], rect))
    return targets

def build_model(cols, rows, snapshot, ui, prefs, remote_endpoints=False, branch_present=git_branch_present):
    """All targets of a client of size cols x rows showing `snapshot`
    (session.snapshot's `snapshot` object, as seen by its single client)."""
    model = Model(cols, rows)
    if snapshot.get('version') not in SUPPORTED_VERSIONS:
        reason = f'herdr {snapshot.get("version")} is not a version this replica was checked against'
        model.refused = {k: reason for k in ('space', 'tab', 'pane')}
        return model
    if ui.problems:
        reason = '; '.join(ui.problems)
        model.refused = {k: reason for k in ('space', 'tab', 'pane')}
        return model
    focused_ws = snapshot.get('focused_workspace_id')
    workspaces = sorted(snapshot.get('workspaces', []), key=lambda w: w.get('number', 0))
    tabs = sorted((t for t in snapshot.get('tabs', []) if t.get('workspace_id') == focused_ws), key=lambda t: t.get('number', 0))
    layouts = snapshot.get('layouts', [])
    zoomed = {l.get('tab_id'): bool(l.get('zoomed')) for l in layouts}
    shell = shell_layout(cols, rows, ui, prefs, len(tabs))
    if shell.mobile_header.height:
        model.refused['space'] = model.refused['tab'] = 'herdr is in its narrow (mobile) layout'
    try:
        model.targets += tab_targets(shell.tab_bar, tabs, ui, zoomed)
    except Refused as why:
        model.refused['tab'] = str(why)
    if shell.sidebar.width:
        try:
            if remote_endpoints:
                raise Refused('remote machines are listed in the sidebar')
            if shell.sidebar_collapsed:
                model.targets += collapsed_sidebar_targets(shell.sidebar, workspaces)
            else:
                cache = {}
                def branch_of(workspace):
                    key = workspace['workspace_id']
                    if key not in cache:
                        cwd = identity_cwd(snapshot, key)
                        cache[key] = None if cwd is None else branch_present(cwd)
                    return cache[key]
                model.targets += expanded_sidebar_targets(shell.sidebar, workspaces, ui, prefs, branch_of)
        except Refused as why:
            model.refused['space'] = str(why)
    elif 'space' not in model.refused:
        model.refused['space'] = 'the sidebar is hidden'
    focused_tab = snapshot.get('focused_tab_id')
    layout = next((l for l in layouts if l.get('tab_id') == focused_tab), None)
    try:
        model.targets += pane_targets(shell.pane_surface, layout)
    except Refused as why:
        model.refused['pane'] = str(why)
        # Without a confirmed surface the sidebar and tab geometry is unproven too.
        model.targets = []
        model.refused.setdefault('space', str(why))
        model.refused.setdefault('tab', str(why))
    return model

def remote_endpoints_listed(state_dir):
    """True when the client's endpoint catalog lists SSH machines (they change the sidebar)."""
    import json
    path = Path(state_dir)/'client'/'endpoints.json'
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True
    # EndpointCatalog { version, selected_profile, ssh: [...] } (client/endpoint/catalog.rs)
    return not isinstance(data, dict) or bool(data.get('ssh')) or data.get('selected_profile') is not None
