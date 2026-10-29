// Vendored from walid-mos/herdr-focus-pane (Sources/HerdrFocusCore/Geometry.swift
// and Sources/HerdrFocusPane/Bridge.swift): CellGeometry, PaneRectEntry,
// PaneLayoutSnapshot, deriveScalePadding, parsePaneLayout, ttyGrid and the AX
// text-area lookup. Changes: dropped `public`, unused helpers and the pinned
// config; the text-area lookup also returns the AX element so its text can be
// read; ttyGrid opens with O_NOCTTY.
//
// MIT License
//
// Copyright (c) 2026 Walid Mostefaoui
//
// Permission is hereby granted, free of charge, to any person obtaining a copy
// of this software and associated documentation files (the "Software"), to deal
// in the Software without restriction, including without limitation the rights
// to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
// copies of the Software, and to permit persons to whom the Software is
// furnished to do so, subject to the following conditions:
//
// The above copyright notice and this permission notice shall be included in all
// copies or substantial portions of the Software.
//
// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
// IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
// FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
// AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
// LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
// OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
// SOFTWARE.

import ApplicationServices
import CoreGraphics
import Darwin
import Foundation

struct GateError: Error, CustomStringConvertible {
    let description: String
}

/// Pure geometry for mapping screen points to terminal cells and back to
/// Ghostty's surface-local mouse coordinates.
struct CellGeometry: Equatable {
    let frame: CGRect
    let columns: Int
    let rows: Int
    let padding: CGSize

    var cellWidth: Double { floor((frame.width - 2 * padding.width) / Double(columns)) }
    var cellHeight: Double { floor((frame.height - 2 * padding.height) / Double(rows)) }

    func cell(at point: CGPoint) -> (column: Int, row: Int)? {
        guard cellWidth > 0, cellHeight > 0 else { return nil }
        let column = Int(floor((Double(point.x) - frame.minX - padding.width) / cellWidth))
        let row = Int(floor((Double(point.y) - frame.minY - padding.height) / cellHeight))
        guard column >= 0, column < columns, row >= 0, row < rows else { return nil }
        return (column, row)
    }

    /// Surface-local coordinates for Ghostty's `send mouse position`. The
    /// MousePosEvent origin is the view's top-left (x rightward, y downward).
    func surfacePoint(_ cell: (column: Int, row: Int)) -> CGPoint {
        CGPoint(x: padding.width + (Double(cell.column) + 0.5) * cellWidth,
                y: padding.height + (Double(cell.row) + 0.5) * cellHeight)
    }
}

/// One herdr pane rectangle in tab-surface cell coordinates.
struct PaneRectEntry: Equatable {
    let paneID: String
    let rect: CGRect
}

struct PaneLayoutSnapshot: Equatable {
    let focusedPaneID: String
    let panes: [PaneRectEntry]
    /// Cells between the terminal grid origin and herdr's own layout origin
    /// (sidebar + chrome), resolved from `grid - area`.
    let offsetColumns: Int
    let offsetRows: Int

    init(focusedPaneID: String, panes: [PaneRectEntry],
         offsetColumns: Int = 0, offsetRows: Int = 0) {
        self.focusedPaneID = focusedPaneID
        self.panes = panes
        self.offsetColumns = offsetColumns
        self.offsetRows = offsetRows
    }

    func gridRect(for pane: PaneRectEntry) -> CGRect {
        pane.rect.offsetBy(dx: Double(offsetColumns), dy: Double(offsetRows))
    }

    func pane(atCell cell: (column: Int, row: Int)) -> PaneRectEntry? {
        panes.first { pane in
            let rect = gridRect(for: pane)
            return rect.minX <= Double(cell.column) && Double(cell.column) < rect.maxX
                && rect.minY <= Double(cell.row) && Double(cell.row) < rect.maxY
        }
    }
}

/// Resolve (backingScale, padding) from the AX text area (content plus
/// 2 * padding) and the tty's content pixels.
func deriveScalePadding(axWidth: Double, axHeight: Double,
                        contentPixelsX: Int, contentPixelsY: Int)
    -> (scale: Double, paddingX: Double, paddingY: Double)? {
    for scale in [2.0, 1.0] {
        let padX = (axWidth - Double(contentPixelsX) / scale) / 2
        let padY = (axHeight - Double(contentPixelsY) / scale) / 2
        // Real padding is a small border around the content grid.
        if padX >= 0, padY >= 0, padX <= 24, padY <= 24 {
            return (scale, padX, padY)
        }
    }
    return nil
}

func parsePaneLayout(_ data: Data, gridColumns: Int, gridRows: Int,
                     fallbackOffsetColumns: Int = 0,
                     fallbackOffsetRows: Int = 0) throws -> PaneLayoutSnapshot {
    guard let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let result = payload["result"] as? [String: Any],
          let layout = result["layout"] as? [String: Any],
          let panes = layout["panes"] as? [[String: Any]] else {
        throw GateError(description: "unrecognized pane layout payload")
    }
    let entries = try panes.map { pane -> PaneRectEntry in
        guard let paneID = pane["pane_id"] as? String,
              let rect = pane["rect"] as? [String: Any],
              let x = rect["x"] as? Double, let y = rect["y"] as? Double,
              let width = rect["width"] as? Double, let height = rect["height"] as? Double else {
            throw GateError(description: "unrecognized pane entry \(pane)")
        }
        return PaneRectEntry(paneID: paneID, rect: CGRect(x: x, y: y, width: width, height: height))
    }
    guard let focused = layout["focused_pane_id"] as? String else {
        throw GateError(description: "layout missing focused_pane_id")
    }
    let offsets: (columns: Int, rows: Int)
    if let area = layout["area"] as? [String: Any],
       let areaWidth = area["width"] as? Double, let areaHeight = area["height"] as? Double {
        offsets = (max(0, gridColumns - Int(areaWidth)),
                   max(0, gridRows - Int(areaHeight)))
    } else {
        offsets = (max(0, fallbackOffsetColumns), max(0, fallbackOffsetRows))
    }
    return PaneLayoutSnapshot(focusedPaneID: focused, panes: entries,
                              offsetColumns: offsets.columns, offsetRows: offsets.rows)
}

/// Current tty grid (columns x rows and content pixels), read live.
func ttyGrid(path: String) throws -> (columns: Int, rows: Int, xpixel: Int, ypixel: Int) {
    let descriptor = open(path, O_RDONLY | O_NONBLOCK | O_NOCTTY)
    guard descriptor >= 0 else { throw GateError(description: "cannot open \(path)") }
    defer { close(descriptor) }
    var size = winsize()
    guard ioctl(descriptor, UInt(TIOCGWINSZ), &size) == 0 else {
        throw GateError(description: "TIOCGWINSZ failed")
    }
    guard size.ws_col > 0, size.ws_row > 0 else {
        throw GateError(description: "empty tty grid")
    }
    return (columns: Int(size.ws_col), rows: Int(size.ws_row),
            xpixel: Int(size.ws_xpixel), ypixel: Int(size.ws_ypixel))
}

func axAttribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    AXUIElementCopyAttributeValue(element, name as CFString, &value)
    return value
}

func axFrame(_ element: AXUIElement) -> CGRect? {
    guard let positionValue = axAttribute(element, kAXPositionAttribute),
          let sizeValue = axAttribute(element, kAXSizeAttribute) else { return nil }
    var position = CGPoint.zero, size = CGSize.zero
    AXValueGetValue(positionValue as! AXValue, .cgPoint, &position)
    AXValueGetValue(sizeValue as! AXValue, .cgSize, &size)
    return CGRect(origin: position, size: size)
}

func collectTextAreas(in element: AXUIElement, depth: Int,
                      into found: inout [(element: AXUIElement, frame: CGRect)]) {
    if let role = axAttribute(element, kAXRoleAttribute) as? String, role == kAXTextAreaRole,
       let frame = axFrame(element) {
        found.append((element, frame))
    }
    guard depth > 0 else { return }
    let children = axAttribute(element, kAXChildrenAttribute) as? [AXUIElement] ?? []
    children.forEach { collectTextAreas(in: $0, depth: depth - 1, into: &found) }
}
