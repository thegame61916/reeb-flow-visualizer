from __future__ import annotations

from pathlib import Path


def shared_viewer_css() -> str:
    return """
#rangeBar,
#rangeBar * {
  -webkit-user-select: none;
  -moz-user-select: none;
  -ms-user-select: none;
  user-select: none;
}
.range-drag-surface {
  fill: transparent;
  cursor: crosshair;
  pointer-events: all;
}
.range-drag-preview {
  fill: rgba(47, 128, 201, 0.2);
  stroke: #2f80c9;
  stroke-width: 1;
  pointer-events: none;
}
.range-selected {
  fill: #6aa3d8;
  opacity: 0.55;
  cursor: pointer;
  pointer-events: all;
}
.range-selected:hover {
  opacity: 0.82;
  stroke: #15202b;
  stroke-width: 1;
}
.range-selected.selected {
  fill: #2f80c9;
  opacity: 0.9;
  stroke: #15202b;
  stroke-width: 1.1;
}
.viewport-window {
  fill: rgba(0, 0, 0, 0.22);
  stroke: #000;
  stroke-width: 1.2;
  pointer-events: all;
  cursor: grab;
}
.viewport-window.dragging {
  cursor: grabbing;
}
.range-label {
  font-size: 11px;
  fill: #425160;
  pointer-events: none;
}
.range-tick {
  stroke: #b6c0cb;
  stroke-width: 1;
}
"""


def write_viewer_common_js(viewer_dir: Path) -> Path:
    path = viewer_dir / 'viewer_common.js'
    path.write_text(
        """window.ReebViewerCommon = window.ReebViewerCommon || {};

window.ReebViewerCommon.bindCommittedNumberInput = function(input, commitFn) {
  input.addEventListener('pointerdown', event => event.stopPropagation());
  input.addEventListener('mousedown', event => event.stopPropagation());
  input.addEventListener('click', event => event.stopPropagation());
  input.addEventListener('keydown', event => {
    if (event.key === 'Enter') {
      event.preventDefault();
      commitFn(input.value);
      input.blur();
    }
  });
  input.addEventListener('blur', () => commitFn(input.value));
};

window.ReebViewerCommon.clampInt = function(value, low, high, fallback) {
  const n = Math.round(Number(value));
  if (!Number.isFinite(n)) {
    return Number.isFinite(fallback) ? Math.round(fallback) : low;
  }
  return Math.max(low, Math.min(high, n));
};

window.ReebViewerCommon.normalizeRanges = function(ranges, timestepMax, opts) {
  const max = Math.max(0, Number.isFinite(+timestepMax) ? +timestepMax : 0);
  const list = Array.isArray(ranges) ? ranges : [];
  const normalized = list
    .map(range => {
      const start = window.ReebViewerCommon.clampInt(
        Math.min(Number(range?.start), Number(range?.end)),
        0,
        max,
        0
      );
      const end = window.ReebViewerCommon.clampInt(
        Math.max(Number(range?.start), Number(range?.end)),
        0,
        max,
        start
      );
      return { start, end };
    })
    .filter(range => Number.isFinite(range.start) && Number.isFinite(range.end));

  const fallback = opts && opts.fallbackRange
    ? {
        start: window.ReebViewerCommon.clampInt(opts.fallbackRange.start, 0, max, 0),
        end: window.ReebViewerCommon.clampInt(opts.fallbackRange.end, 0, max, 0)
      }
    : null;

  if (!normalized.length && fallback) {
    normalized.push({
      start: Math.min(fallback.start, fallback.end),
      end: Math.max(fallback.start, fallback.end)
    });
  }
  return normalized;
};

window.ReebViewerCommon.selectRangeIndex = function(index, rangeCount, fallback) {
  if (!Number.isFinite(+rangeCount) || +rangeCount <= 0) {
    return Number.isFinite(+fallback) ? Math.round(+fallback) : -1;
  }
  return Math.max(0, Math.min(+rangeCount - 1, Math.round(Number(index) || 0)));
};

window.ReebViewerCommon.commitRangeAt = function(ranges, index, startValue, endValue, timestepMax) {
  const next = window.ReebViewerCommon.normalizeRanges(ranges, timestepMax);
  if (index < 0 || index >= next.length) {
    return next;
  }
  const max = Math.max(0, Number.isFinite(+timestepMax) ? +timestepMax : 0);
  const start = window.ReebViewerCommon.clampInt(startValue, 0, max, next[index].start);
  const end = window.ReebViewerCommon.clampInt(endValue, 0, max, next[index].end);
  next[index] = {
    start: Math.min(start, end),
    end: Math.max(start, end)
  };
  return next;
};

window.ReebViewerCommon.addRangeAfterLast = function(ranges, timestepMax, opts) {
  const max = Math.max(0, Number.isFinite(+timestepMax) ? +timestepMax : 0);
  const next = window.ReebViewerCommon.normalizeRanges(ranges, max);
  const span = Math.max(0, Math.round(Number(opts?.span ?? 20)));
  const start = next.length
    ? Math.max(0, Math.min(max, next[next.length - 1].end + 1))
    : 0;
  const end = Math.max(start, Math.min(max, start + span));
  next.push({ start, end });
  return {
    ranges: next,
    selectedRangeIndex: next.length - 1
  };
};

window.ReebViewerCommon.removeRangeAt = function(ranges, selectedRangeIndex, removeIndex, timestepMax, opts) {
  const keepOne = opts?.keepOne !== false;
  const fallbackRange = opts?.fallbackRange || { start: 0, end: 0 };
  let next = window.ReebViewerCommon.normalizeRanges(ranges, timestepMax);
  if (removeIndex < 0 || removeIndex >= next.length) {
    return {
      ranges: next,
      selectedRangeIndex: window.ReebViewerCommon.selectRangeIndex(selectedRangeIndex, next.length, -1)
    };
  }

  next.splice(removeIndex, 1);
  if (!next.length && keepOne) {
    next = window.ReebViewerCommon.normalizeRanges([], timestepMax, { fallbackRange });
  }
  return {
    ranges: next,
    selectedRangeIndex: window.ReebViewerCommon.selectRangeIndex(
      removeIndex,
      next.length,
      keepOne ? 0 : -1
    )
  };
};

window.ReebViewerCommon.finishRangeDrag = function(rangeDrag, timestepMax, opts) {
  if (!rangeDrag || !Number.isFinite(+rangeDrag.start)) return null;
  const max = Math.max(0, Number.isFinite(+timestepMax) ? +timestepMax : 0);
  const minSpan = Math.max(0, Math.round(Number(opts?.minSpan ?? 1)));
  const start = window.ReebViewerCommon.clampInt(rangeDrag.start, 0, max, 0);
  const current = window.ReebViewerCommon.clampInt(
    rangeDrag.current ?? rangeDrag.end ?? rangeDrag.start,
    0,
    max,
    start
  );

  let low = Math.min(start, current);
  let high = Math.max(start, current);
  if (high === low && minSpan > 0) {
    high = Math.min(max, low + minSpan);
  }
  if (low > high) {
    const temp = low;
    low = high;
    high = temp;
  }
  return { start: low, end: high };
};

window.ReebViewerCommon.renderRangeRows = function(holder, opts) {
  const root = holder && holder.nodeType ? holder : null;
  if (!root) return;

  const ranges = Array.isArray(opts.ranges) ? opts.ranges : [];
  const selectedRangeIndex = Number.isFinite(+opts.selectedRangeIndex) ? +opts.selectedRangeIndex : 0;
  const timestepMax = Math.max(0, +opts.timestepMax || 0);
  const onSelectRange = typeof opts.onSelectRange === 'function' ? opts.onSelectRange : null;
  const onCommitRange = typeof opts.onCommitRange === 'function' ? opts.onCommitRange : null;
  const onDeleteRange = typeof opts.onDeleteRange === 'function' ? opts.onDeleteRange : null;

  root.innerHTML = '';

  const rows = ranges.length ? ranges : [{ start: 0, end: 0 }];
  rows.forEach((range, index) => {
    const row = document.createElement('div');
    row.className = `range-row${index === selectedRangeIndex ? ' selected' : ''}`;
    row.setAttribute('tabindex', '0');
    row.innerHTML = `
      <input type="number" min="0" max="${timestepMax}" value="${range.start}">
      <input type="number" min="0" max="${timestepMax}" value="${range.end}">
      <button title="Remove range">Delete</button>`;

    row.addEventListener('click', event => {
      if (event.target.closest('input, button')) return;
      if (onSelectRange) onSelectRange(index, event);
    });

    const inputs = row.querySelectorAll('input');
    const start = inputs[0];
    const end = inputs[1];

    const commitRange = () => {
      if (!onCommitRange) return;
      onCommitRange(index, start.value, end.value);
    };

    [start, end].forEach(input => {
      input.addEventListener('pointerdown', event => event.stopPropagation());
      input.addEventListener('mousedown', event => event.stopPropagation());
      input.addEventListener('click', event => event.stopPropagation());
      input.addEventListener('keydown', event => {
        if (event.key === 'Enter') {
          event.preventDefault();
          commitRange();
          input.blur();
        }
      });
      input.addEventListener('blur', event => {
        if (row.contains(event.relatedTarget)) return;
        commitRange();
      });
    });

    row.querySelector('button').addEventListener('click', event => {
      event.stopPropagation();
      if (onDeleteRange) onDeleteRange(index, event);
    });

    root.appendChild(row);
  });
};

window.ReebViewerCommon.recenterViewportFromBarIndex = function(targetTime, opts) {
  const graphToTime = opts && opts.graphToTime;
  const getViewFocus = opts && opts.getViewFocus;
  const setViewFocus = opts && opts.setViewFocus;
  const scheduleViewportUpdate = opts && opts.scheduleViewportUpdate;
  const visibleWindowFn = opts && opts.visibleWindowFn;
  const maxTime = Math.max(0, +((opts && opts.maxTime) ?? 0));

  if (!graphToTime || typeof graphToTime.invert !== 'function') return;
  if (typeof getViewFocus !== 'function' || typeof setViewFocus !== 'function' || typeof scheduleViewportUpdate !== 'function') return;

  const focus = getViewFocus();
  if (!focus) return;

  const visible = typeof visibleWindowFn === 'function' ? visibleWindowFn() : null;
  const span = visible ? Math.max(0, visible.end - visible.start) : 0;
  const halfSpan = span / 2;
  const minCenter = halfSpan;
  const maxCenter = maxTime - halfSpan;
  const centerTime = minCenter <= maxCenter ? Math.max(minCenter, Math.min(maxCenter, Number(targetTime) || 0)) : maxTime / 2;

  setViewFocus({
    x: graphToTime.invert(centerTime),
    y: focus.y
  });
  scheduleViewportUpdate();
};

window.ReebViewerCommon.renderRangeBar = function(svg, opts) {
  const width = Math.max(1, +opts.width || 1);
  const height = Math.max(1, +opts.height || 1);
  const timestepMax = Math.max(0, +opts.timestepMax || 0);
  const barPadding = Math.max(0, +opts.barPadding || 24);
  const tickY1 = opts.tickY1 ?? 24;
  const tickY2 = opts.tickY2 ?? 34;
  const labelY = opts.labelY ?? 20;
  const rangeY = opts.rangeY ?? 40;
  const rangeHeight = opts.rangeHeight ?? 22;
  const viewportY = opts.viewportY ?? 20;
  const viewportHeight = opts.viewportHeight ?? 46;
  const tickValues = Array.isArray(opts.tickValues) && opts.tickValues.length
    ? opts.tickValues.slice()
    : Array.from({ length: timestepMax + 1 }, (_, i) => i);
  const ranges = Array.isArray(opts.ranges) ? opts.ranges : [];
  const selectedRangeIndex = +opts.selectedRangeIndex || 0;
  const rangeDrag = opts.rangeDrag || null;
  const viewportDrag = opts.viewportDrag || null;
  const visibleWindow = typeof opts.visibleWindow === 'function' ? opts.visibleWindow() : opts.visibleWindow;
  const x = d3.scaleLinear().domain([0, timestepMax || 1]).range([barPadding, width - barPadding]);
  const getRangeLabel = typeof opts.rangeLabelFn === 'function' ? opts.rangeLabelFn : (r => `${r.start} .. ${r.end}`);
  const getTickLabel = typeof opts.tickLabelFn === 'function' ? opts.tickLabelFn : (i => String(i));

  svg.attr('width', width).attr('height', height);
  svg.selectAll('*').remove();

  const g = svg.append('g');
  g.append('rect')
    .attr('class', 'range-bg')
    .attr('x', 0)
    .attr('y', 0)
    .attr('width', width)
    .attr('height', height);

  tickValues.forEach(value => {
    const tx = x(value);
    g.append('line')
      .attr('class', 'range-tick')
      .attr('x1', tx)
      .attr('x2', tx)
      .attr('y1', tickY1)
      .attr('y2', tickY2);
    g.append('text')
      .attr('class', 'range-label')
      .attr('x', tx)
      .attr('y', labelY)
      .attr('text-anchor', 'middle')
      .text(getTickLabel(value));
  });

  g.append('text').attr('x', 20).attr('y', 72).text(0);
  g.append('text').attr('x', width - 20).attr('y', 72).attr('text-anchor', 'end').text(timestepMax);

  const dragSurface = g.append('rect')
    .attr('class', 'range-drag-surface')
    .attr('x', 0)
    .attr('y', 0)
    .attr('width', width)
    .attr('height', height);

  const pointerToIndex = event => {
    const [mx] = d3.pointer(event, svg.node());
    return Math.max(0, Math.min(timestepMax, Math.round(x.invert(mx))));
  };

  dragSurface.on('pointerdown', event => {
    if (event.button !== 0) return;
    event.preventDefault();
    const idx = pointerToIndex(event);
    dragSurface.node().setPointerCapture(event.pointerId);

    const onMove = moveEvent => {
      if (moveEvent.pointerId !== event.pointerId) return;
      if (typeof opts.onRangeDragMove === 'function') opts.onRangeDragMove(pointerToIndex(moveEvent), moveEvent);
      moveEvent.preventDefault();
    };
    const onEnd = endEvent => {
      if (endEvent.pointerId !== event.pointerId) return;
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onEnd);
      window.removeEventListener('pointercancel', onEnd);
      if (typeof opts.onRangeDragEnd === 'function') opts.onRangeDragEnd(pointerToIndex(endEvent), endEvent);
      if (dragSurface.node().hasPointerCapture(event.pointerId)) {
        dragSurface.node().releasePointerCapture(event.pointerId);
      }
      endEvent.preventDefault();
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onEnd);
    window.addEventListener('pointercancel', onEnd);
    if (typeof opts.onRangeDragStart === 'function') opts.onRangeDragStart(idx, event);
  });

  ranges.forEach((range, index) => {
    const x0 = x(range.start);
    const x1 = x(range.end + 1) - 1;
    g.append('rect')
      .attr('class', `range-selected${index === selectedRangeIndex ? ' selected' : ''}`)
      .attr('x', x0)
      .attr('y', rangeY)
      .attr('width', Math.max(2, x1 - x0))
      .attr('height', rangeHeight)
      .on('click', event => {
        event.stopPropagation();
        event.preventDefault();
        if (typeof opts.onRangeSelected === 'function') opts.onRangeSelected(index, event);
      });
    g.append('text')
      .attr('class', 'range-label')
      .attr('x', (x0 + x1) / 2)
      .attr('y', rangeY + (rangeHeight / 2) + 2)
      .attr('text-anchor', 'middle')
      .text(getRangeLabel(range));
  });

  if (rangeDrag) {
    const dragCurrent = rangeDrag.current ?? rangeDrag.end ?? rangeDrag.start;
    const startX = x(rangeDrag.start);
    const curX = x(dragCurrent);
    g.append('rect')
      .attr('class', 'range-drag-preview')
      .attr('x', Math.min(startX, curX))
      .attr('y', rangeY)
      .attr('width', Math.max(2, Math.abs(curX - startX)))
      .attr('height', rangeHeight);
  }

  if (visibleWindow) {
    const low = Math.max(0, Math.min(visibleWindow.start, visibleWindow.end));
    const high = Math.min(timestepMax, Math.max(visibleWindow.start, visibleWindow.end));
    const viewportWindow = g.append('rect')
      .attr('class', `viewport-window${viewportDrag ? ' dragging' : ''}`)
      .attr('x', x(low))
      .attr('y', viewportY)
      .attr('width', Math.max(4, x(high) - x(low)))
      .attr('height', viewportHeight);

    viewportWindow.on('pointerdown', event => {
      if (event.button !== 0) return;
      event.stopPropagation();
      event.preventDefault();
      viewportWindow.node().setPointerCapture(event.pointerId);
      if (typeof opts.onViewportClick === 'function') {
        opts.onViewportClick(pointerToIndex(event), event);
      }
      if (typeof opts.onViewportDragStart === 'function') {
        opts.onViewportDragStart(pointerToIndex(event), event);
      }

      const onMove = moveEvent => {
        if (moveEvent.pointerId !== event.pointerId) return;
        if (typeof opts.onViewportDragMove === 'function') {
          opts.onViewportDragMove(pointerToIndex(moveEvent), moveEvent);
        }
        moveEvent.preventDefault();
      };
      const onEnd = endEvent => {
        if (endEvent.pointerId !== event.pointerId) return;
        window.removeEventListener('pointermove', onMove);
        window.removeEventListener('pointerup', onEnd);
        window.removeEventListener('pointercancel', onEnd);
        if (typeof opts.onViewportDragEnd === 'function') {
          opts.onViewportDragEnd(pointerToIndex(endEvent), endEvent);
        }
        if (viewportWindow.node().hasPointerCapture(event.pointerId)) {
          viewportWindow.node().releasePointerCapture(event.pointerId);
        }
        endEvent.preventDefault();
      };
      window.addEventListener('pointermove', onMove);
      window.addEventListener('pointerup', onEnd);
      window.addEventListener('pointercancel', onEnd);
    });
  }
};
"""
    )
    return path
