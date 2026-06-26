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


def shared_viewer_script_tags(include_sankey: bool = False, version: str = "4") -> str:
    scripts = ['<script src="https://cdn.jsdelivr.net/npm/d3@7"></script>']
    if include_sankey:
        scripts.append('<script src="https://cdn.jsdelivr.net/npm/d3-sankey@0.12.3/dist/d3-sankey.min.js"></script>')
    scripts.append(f'<script src="viewer_common.js?v={version}"></script>')
    scripts.append(f'<script src="viewer.js?v={version}"></script>')
    return "\n  ".join(scripts)


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

window.ReebViewerCommon.createTimestepLookup = function(timesteps, opts) {
  const indexField = opts?.indexField || "index";
  const labelField = opts?.labelField || "label";
  const rows = Array.isArray(timesteps) ? timesteps : [];
  const byIndex = new Map();
  let maxIndex = 0;

  for (const row of rows) {
    const raw = Number(row?.[indexField]);
    if (!Number.isFinite(raw)) continue;
    const index = Math.max(0, Math.round(raw));
    byIndex.set(index, row);
    if (index > maxIndex) maxIndex = index;
  }

  const labelAt = (index, fallback = null) => {
    const i = Math.max(0, Math.round(Number(index) || 0));
    const row = byIndex.get(i);
    if (!row) return fallback ?? String(i);
    const label = row?.[labelField];
    if (label === undefined || label === null || String(label).length === 0) {
      return fallback ?? String(i);
    }
    return String(label);
  };

  const itemAt = index => {
    const i = Math.max(0, Math.round(Number(index) || 0));
    return byIndex.get(i) || null;
  };

  const tickValues = targetTickCount => {
    const count = Math.max(1, Math.round(Number(targetTickCount) || 12));
    const step = Math.max(1, Math.ceil(maxIndex / count));
    return d3.range(0, maxIndex + 1, step);
  };

  return {
    byIndex,
    maxIndex,
    labelAt,
    itemAt,
    has: index => byIndex.has(Math.max(0, Math.round(Number(index) || 0))),
    clampIndex: index => Math.max(0, Math.min(maxIndex, Math.round(Number(index) || 0))),
    tickValues
  };
};

window.ReebViewerCommon.fitAndCenter = function(camera, bounds, fitZoomFn, opts) {
  if (!camera || !bounds) return false;
  const fit = opts?.fit !== false;
  camera.centerOnBounds(bounds, fitZoomFn, fit);
  return true;
};

window.ReebViewerCommon.createRangeActionDispatcher = function(opts) {
  const applyRangeAction = typeof opts?.applyRangeAction === "function" ? opts.applyRangeAction : null;
  const getState = typeof opts?.getState === "function" ? opts.getState : (() => ({}));
  const handlers = opts?.handlers || {};
  const plans = {
    rowCommit: ["rows", "main", "bar"],
    rangeCommitted: ["rows", "main", "bar"],
    barOnly: ["bar"],
    ...(opts?.plans || {})
  };

  const runPlan = planName => {
    const steps = Array.isArray(plans[planName]) ? plans[planName] : [];
    for (const step of steps) {
      const fn = handlers[step];
      if (typeof fn === "function") fn();
    }
  };

  const dispatch = (action, planName) => {
    const before = getState();
    const result = applyRangeAction ? applyRangeAction(action) : null;
    if (planName) runPlan(planName);
    return {
      before,
      result,
      after: getState()
    };
  };

  return {
    runPlan,
    dispatch,
    selectRange: index => dispatch({ type: "select", index }, "rangeCommitted"),
    commitRangeRow: (index, startValue, endValue) => dispatch({ type: "commit", index, startValue, endValue }, "rowCommit"),
    addRange: () => dispatch({ type: "add" }, "rangeCommitted"),
    addExplicitRange: (start, end) => dispatch({ type: "add-explicit", start, end }, "rangeCommitted"),
    deleteRange: index => dispatch({ type: "delete", index }, "rangeCommitted")
  };
};

window.ReebViewerCommon.rangeReducer = function(state, action, opts) {
  const source = state || {};
  const step = action || {};
  const max = Math.max(0, Number.isFinite(+opts?.timestepMax) ? +opts.timestepMax : 0);
  const keepOne = opts?.keepOne !== false;
  const fallbackRange = opts?.fallbackRange || { start: 0, end: 0 };
  const defaultSpan = Math.max(0, Math.round(Number(opts?.defaultSpan ?? 20)));
  const minSpan = Math.max(0, Math.round(Number(opts?.minSpan ?? 1)));
  const emptySelectedIndex = Number.isFinite(+opts?.emptySelectedIndex)
    ? Math.round(+opts.emptySelectedIndex)
    : (keepOne ? 0 : -1);

  let next = {
    ranges: window.ReebViewerCommon.normalizeRanges(source.ranges, max, keepOne ? { fallbackRange } : undefined),
    selectedRangeIndex: window.ReebViewerCommon.selectRangeIndex(
      source.selectedRangeIndex,
      Array.isArray(source.ranges) ? source.ranges.length : 0,
      emptySelectedIndex
    ),
    rangeDrag: source.rangeDrag || null
  };
  next.selectedRangeIndex = window.ReebViewerCommon.selectRangeIndex(
    next.selectedRangeIndex,
    next.ranges.length,
    emptySelectedIndex
  );

  switch (step.type) {
    case 'normalize':
      return next;
    case 'select':
      next.selectedRangeIndex = window.ReebViewerCommon.selectRangeIndex(step.index, next.ranges.length, emptySelectedIndex);
      return next;
    case 'commit':
      next.ranges = window.ReebViewerCommon.commitRangeAt(
        next.ranges,
        step.index,
        step.startValue,
        step.endValue,
        max
      );
      next.selectedRangeIndex = window.ReebViewerCommon.selectRangeIndex(next.selectedRangeIndex, next.ranges.length, emptySelectedIndex);
      return next;
    case 'add': {
      const added = window.ReebViewerCommon.addRangeAfterLast(next.ranges, max, { span: defaultSpan });
      next.ranges = added.ranges;
      next.selectedRangeIndex = added.selectedRangeIndex;
      return next;
    }
    case 'add-explicit': {
      const committed = window.ReebViewerCommon.finishRangeDrag(
        { start: step.start, current: step.end },
        max,
        { minSpan }
      );
      if (!committed) return next;
      next.ranges = [...next.ranges, committed];
      next.selectedRangeIndex = next.ranges.length - 1;
      return next;
    }
    case 'delete': {
      const removed = window.ReebViewerCommon.removeRangeAt(
        next.ranges,
        next.selectedRangeIndex,
        step.index,
        max,
        {
          keepOne,
          fallbackRange
        }
      );
      next.ranges = removed.ranges;
      next.selectedRangeIndex = removed.selectedRangeIndex;
      return next;
    }
    case 'drag-start':
      next.rangeDrag = {
        start: window.ReebViewerCommon.clampInt(step.index, 0, max, 0),
        current: window.ReebViewerCommon.clampInt(step.index, 0, max, 0)
      };
      return next;
    case 'drag-move':
      if (!next.rangeDrag) return next;
      next.rangeDrag = {
        ...next.rangeDrag,
        current: window.ReebViewerCommon.clampInt(step.index, 0, max, next.rangeDrag.current)
      };
      return next;
    case 'drag-clear':
      next.rangeDrag = null;
      return next;
    case 'drag-commit': {
      const committed = window.ReebViewerCommon.finishRangeDrag(next.rangeDrag, max, { minSpan });
      next.rangeDrag = null;
      if (!committed) return next;
      next.ranges = [...next.ranges, committed];
      next.selectedRangeIndex = next.ranges.length - 1;
      return next;
    }
    default:
      return next;
  }
};

window.ReebViewerCommon.createCameraController = function(opts) {
  const zoomMin = Number.isFinite(+opts?.zoomMin) ? +opts.zoomMin : 0.1;
  const zoomMax = Number.isFinite(+opts?.zoomMax) ? +opts.zoomMax : 20;
  const zoomStep = Number.isFinite(+opts?.zoomStep) ? +opts.zoomStep : 1.2;
  const wheelFactor = Number.isFinite(+opts?.wheelFactor) ? +opts.wheelFactor : 0.0015;
  const panDragThreshold = Number.isFinite(+opts?.panDragThreshold) ? +opts.panDragThreshold : 4;
  const applyTransform = typeof opts?.applyTransform === 'function' ? opts.applyTransform : null;

  const state = {
    zoomScale: Number.isFinite(+opts?.initialZoomScale) ? +opts.initialZoomScale : 1,
    viewFocus: opts?.initialViewFocus && Number.isFinite(+opts.initialViewFocus.x) && Number.isFinite(+opts.initialViewFocus.y)
      ? { x: +opts.initialViewFocus.x, y: +opts.initialViewFocus.y }
      : null,
    panDrag: null,
    pending: false
  };

  const clampZoom = scale => Math.max(zoomMin, Math.min(zoomMax, Number(scale) || 1));
  const getZoomScale = () => state.zoomScale;
  const getViewFocus = () => state.viewFocus ? { x: state.viewFocus.x, y: state.viewFocus.y } : null;
  const clearViewFocus = () => {
    state.viewFocus = null;
    return state.viewFocus;
  };
  const setViewFocus = focus => {
    if (!focus || !Number.isFinite(+focus.x) || !Number.isFinite(+focus.y)) return state.viewFocus;
    state.viewFocus = { x: +focus.x, y: +focus.y };
    return state.viewFocus;
  };
  const currentTransform = () => ({
    zoomScale: state.zoomScale,
    viewFocus: state.viewFocus
  });
  const applyNow = () => {
    if (!applyTransform) return;
    state.pending = false;
    applyTransform(currentTransform());
  };
  const scheduleApply = () => {
    if (!applyTransform || state.pending) return;
    state.pending = true;
    requestAnimationFrame(() => {
      state.pending = false;
      applyTransform(currentTransform());
    });
  };
  const setZoomScale = nextScale => {
    const clamped = clampZoom(nextScale);
    if (Math.abs(clamped - state.zoomScale) < 1e-9) return;
    state.zoomScale = clamped;
    scheduleApply();
  };
  const zoomBy = factor => setZoomScale(state.zoomScale * (Number(factor) || 1));
  const centerOnBounds = (bounds, fitZoomFn, fit) => {
    if (!bounds) return;
    const minX = Number(bounds.minX);
    const maxX = Number(bounds.maxX);
    const minY = Number(bounds.minY);
    const maxY = Number(bounds.maxY);
    if (![minX, maxX, minY, maxY].every(Number.isFinite)) return;
    state.viewFocus = { x: (minX + maxX) / 2, y: (minY + maxY) / 2 };
    if (fit && typeof fitZoomFn === 'function') {
      state.zoomScale = clampZoom(fitZoomFn(bounds));
    }
    scheduleApply();
  };

  const bindPanAndWheel = (targetNode, bindOpts) => {
    if (!targetNode) return () => {};
    const cursorTarget = bindOpts?.cursorTarget || targetNode;
    const isPanTarget = typeof bindOpts?.isPanTarget === 'function' ? bindOpts.isPanTarget : (() => true);
    const ensureFocus = typeof bindOpts?.ensureFocus === 'function' ? bindOpts.ensureFocus : null;
    const onPanState = typeof bindOpts?.onPanState === 'function' ? bindOpts.onPanState : null;
    const onActive = typeof bindOpts?.onActive === 'function' ? bindOpts.onActive : null;
    const allowWheel = bindOpts?.allowWheel !== false;

    const onPointerDown = event => {
      if (event.button !== 0) return;
      if (!isPanTarget(event.target)) return;
      if (!state.viewFocus && ensureFocus) {
        const focus = ensureFocus();
        if (focus && Number.isFinite(+focus.x) && Number.isFinite(+focus.y)) {
          state.viewFocus = { x: +focus.x, y: +focus.y };
        }
      }
      if (!state.viewFocus) return;
      state.panDrag = {
        startX: event.clientX,
        startY: event.clientY,
        focusX: state.viewFocus.x,
        focusY: state.viewFocus.y,
        moved: false
      };
      if (onActive) onActive(event);
      if (onPanState) onPanState(true);
      if (cursorTarget?.style) cursorTarget.style.cursor = "grabbing";
      targetNode.setPointerCapture(event.pointerId);
      event.preventDefault();
    };

    const onPointerMove = event => {
      if (!state.panDrag) return;
      const dx = event.clientX - state.panDrag.startX;
      const dy = event.clientY - state.panDrag.startY;
      if (!state.panDrag.moved && Math.hypot(dx, dy) < panDragThreshold) return;
      state.panDrag.moved = true;
      state.viewFocus = {
        x: state.panDrag.focusX - dx / state.zoomScale,
        y: state.panDrag.focusY - dy / state.zoomScale
      };
      scheduleApply();
      event.preventDefault();
    };

    const endPan = event => {
      if (!state.panDrag) return;
      state.panDrag = null;
      if (onPanState) onPanState(false);
      if (cursorTarget?.style) cursorTarget.style.cursor = "grab";
      try {
        if (targetNode.hasPointerCapture(event.pointerId)) {
          targetNode.releasePointerCapture(event.pointerId);
        }
      } catch (_) {}
    };

    const onWheel = event => {
      if (!allowWheel) return;
      event.preventDefault();
      const factor = Math.exp(-event.deltaY * wheelFactor);
      zoomBy(factor);
    };

    targetNode.addEventListener('pointerdown', onPointerDown);
    targetNode.addEventListener('pointermove', onPointerMove);
    targetNode.addEventListener('pointerup', endPan);
    targetNode.addEventListener('pointercancel', endPan);
    if (allowWheel) {
      targetNode.addEventListener('wheel', onWheel, { passive: false });
    }

    return () => {
      targetNode.removeEventListener('pointerdown', onPointerDown);
      targetNode.removeEventListener('pointermove', onPointerMove);
      targetNode.removeEventListener('pointerup', endPan);
      targetNode.removeEventListener('pointercancel', endPan);
      if (allowWheel) {
        targetNode.removeEventListener('wheel', onWheel);
      }
    };
  };

  return {
    clampZoom,
    getZoomScale,
    getViewFocus,
    clearViewFocus,
    setViewFocus,
    setZoomScale,
    zoomBy,
    centerOnBounds,
    scheduleApply,
    applyNow,
    bindPanAndWheel,
    get zoomStep() {
      return zoomStep;
    }
  };
};

window.ReebViewerCommon.createTooltipEngine = function(nodeOrSelection, opts) {
  const node = nodeOrSelection?.node ? nodeOrSelection.node() : nodeOrSelection;
  const hiddenClass = opts?.hiddenClass || null;
  const offsetX = Number.isFinite(+opts?.offsetX) ? +opts.offsetX : 14;
  const offsetY = Number.isFinite(+opts?.offsetY) ? +opts.offsetY : 14;
  const edgePad = Number.isFinite(+opts?.edgePad) ? +opts.edgePad : 12;
  if (!node) {
    return {
      showAt: () => {},
      showFromEvent: () => {},
      hide: () => {}
    };
  }

  const setHidden = hidden => {
    if (hiddenClass) {
      node.classList.toggle(hiddenClass, hidden);
    }
    node.style.display = hidden ? 'none' : 'block';
  };

  const showAt = (html, x, y) => {
    node.innerHTML = html ?? '';
    setHidden(false);
    const rect = node.getBoundingClientRect();
    const left = Math.min(
      Math.max(edgePad, (Number(x) || 0) + offsetX),
      Math.max(edgePad, window.innerWidth - rect.width - edgePad)
    );
    const top = Math.min(
      Math.max(edgePad, (Number(y) || 0) + offsetY),
      Math.max(edgePad, window.innerHeight - rect.height - edgePad)
    );
    node.style.left = `${left}px`;
    node.style.top = `${top}px`;
  };

  const showFromEvent = (event, html) => {
    showAt(html, event?.clientX, event?.clientY);
  };

  const hide = () => {
    setHidden(true);
    node.innerHTML = '';
  };

  hide();
  return { showAt, showFromEvent, hide };
};

window.ReebViewerCommon.formatRangeLabel = function(range, opts) {
  const getLabel = typeof opts?.getLabel === 'function' ? opts.getLabel : (value => value);
  const start = Math.min(Number(range?.start) || 0, Number(range?.end) || 0);
  const end = Math.max(Number(range?.start) || 0, Number(range?.end) || 0);
  return `${getLabel(start)} .. ${getLabel(end)}`;
};

window.ReebViewerCommon.formatFsFromLabel = function(labelValue, opts) {
  const divisor = Number.isFinite(+opts?.divisor) ? +opts.divisor : 41.341374575751;
  const digits = Number.isFinite(+opts?.digits) ? +opts.digits : 2;
  const n = Number(labelValue);
  if (!Number.isFinite(n) || !(divisor > 0)) return '';
  return `${n / divisor}`.replace(/(\\.\\d*?[1-9])0+$/u, '$1').replace(/\\.0+$/u, '.0');
};

window.ReebViewerCommon.formatTimestepPrimary = function(index, label) {
  return `${index}. ${label}`;
};

window.ReebViewerCommon.appendTimestepLabel = function(textSelection, data, opts) {
  const indexAccessor = typeof opts?.indexAccessor === 'function' ? opts.indexAccessor : d => d.index;
  const labelAccessor = typeof opts?.labelAccessor === 'function' ? opts.labelAccessor : d => d.label;
  const unit = opts?.unit || 'fs';
  const digits = Number.isFinite(+opts?.digits) ? +opts.digits : 2;
  const index = indexAccessor(data);
  const label = labelAccessor(data);
  const primary = window.ReebViewerCommon.formatTimestepPrimary(index, label);
  const fsRaw = window.ReebViewerCommon.formatFsFromLabel(label, opts);
  const fsText = fsRaw ? `${Number(fsRaw).toFixed(digits)} ${unit}` : '';

  textSelection.append("tspan")
    .attr("dy", "-0.55em")
    .attr("text-anchor", "middle")
    .text(primary);
  textSelection.append("tspan")
    .attr("dy", "1.10em")
    .attr("font-size", 11)
    .attr("fill", "#6f7d8b")
    .attr("text-anchor", "middle")
    .text(fsText);
};

window.ReebViewerCommon.bindThresholdControl = function(opts) {
  const slider = opts?.slider;
  const box = opts?.box || null;
  const labelNode = opts?.label || null;
  if (!slider) return null;

  const min = Number.isFinite(+opts?.min) ? +opts.min : Number(slider.min || 0);
  const max = Number.isFinite(+opts?.max) ? +opts.max : Number(slider.max || 100);
  const step = Number.isFinite(+opts?.step) ? +opts.step : Number(slider.step || 0.5);
  const onPreview = typeof opts?.onPreview === 'function' ? opts.onPreview : null;
  const onCommit = typeof opts?.onCommit === 'function' ? opts.onCommit : null;

  let value = Number.isFinite(+opts?.initialValue)
    ? +opts.initialValue
    : Number(slider.value || min);
  let previewPending = false;
  let pendingPreview = value;

  const decimals = (() => {
    const s = String(step);
    const idx = s.indexOf(".");
    return idx >= 0 ? Math.max(0, s.length - idx - 1) : 0;
  })();
  const clamp = next => {
    const n = Number(next);
    if (!Number.isFinite(n)) return value;
    return Math.max(min, Math.min(max, n));
  };
  const formatValue = next => decimals > 0 ? Number(next).toFixed(decimals).replace(/\\.0+$/u, '').replace(/(\\.\\d*?[1-9])0+$/u, '$1') : String(Math.round(next));

  const syncUI = next => {
    const text = formatValue(next);
    slider.value = text;
    if (box) box.value = text;
    if (labelNode) labelNode.textContent = `${text}%`;
  };

  const queuePreview = next => {
    pendingPreview = next;
    if (previewPending) return;
    previewPending = true;
    requestAnimationFrame(() => {
      previewPending = false;
      if (onPreview) onPreview(pendingPreview);
    });
  };

  const applyValue = (next, commit, previewAlso) => {
    value = clamp(next);
    syncUI(value);
    if (previewAlso) queuePreview(value);
    if (commit && onCommit) onCommit(value);
  };

  slider.addEventListener('input', event => {
    applyValue(event.target.value, false, true);
  });
  slider.addEventListener('change', event => {
    applyValue(event.target.value, true, false);
  });

  if (box) {
    window.ReebViewerCommon.bindCommittedNumberInput(box, raw => {
      applyValue(raw, true, true);
    });
  }

  syncUI(clamp(value));
  return {
    getValue: () => value,
    setValue: next => applyValue(next, false, false),
    commitValue: next => applyValue(next, true, true)
  };
};

window.ReebViewerCommon.bindKeyboardShortcuts = function(opts) {
  const target = opts?.target || document;
  const onDeleteRange = typeof opts?.onDeleteRange === 'function' ? opts.onDeleteRange : null;
  const onZoomIn = typeof opts?.onZoomIn === 'function' ? opts.onZoomIn : null;
  const onZoomOut = typeof opts?.onZoomOut === 'function' ? opts.onZoomOut : null;
  const shouldIgnore = typeof opts?.shouldIgnore === 'function'
    ? opts.shouldIgnore
    : (event => {
        const tag = event.target?.tagName?.toLowerCase?.() || '';
        if (tag === 'input' || tag === 'textarea' || tag === 'select') return true;
        if (event.target?.isContentEditable) return true;
        return Boolean(event.metaKey || event.ctrlKey || event.altKey);
      });

  const handler = event => {
    if (shouldIgnore(event)) return;
    if ((event.key === 'Delete' || event.key === 'Backspace') && onDeleteRange) {
      event.preventDefault();
      onDeleteRange(event);
      return;
    }
    if ((event.key === '+' || event.key === '=') && onZoomIn) {
      event.preventDefault();
      onZoomIn(event);
      return;
    }
    if ((event.key === '-' || event.key === '_') && onZoomOut) {
      event.preventDefault();
      onZoomOut(event);
    }
  };

  target.addEventListener('keydown', handler);
  return () => target.removeEventListener('keydown', handler);
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
  const maxExistingEnd = next.length
    ? (d3.max(next, range => Number(range?.end)) ?? -1)
    : -1;
  const start = maxExistingEnd >= 0
    ? Math.max(0, Math.min(max, Math.round(maxExistingEnd) + 1))
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

window.ReebViewerCommon.computeVisibleTimestepWindow = function(opts) {
  const graphToTime = opts?.graphToTime;
  const camera = opts?.camera;
  const viewportWidth = Number(opts?.viewportWidth ?? 0);
  if (!graphToTime || !camera || !(viewportWidth > 0)) return null;

  const viewFocus = camera.getViewFocus?.();
  const zoomScale = camera.getZoomScale?.() ?? 1;
  if (!viewFocus || !Number.isFinite(viewFocus.x) || !Number.isFinite(zoomScale) || zoomScale <= 0) return null;

  const startX = viewFocus.x - viewportWidth / (2 * zoomScale);
  const endX = viewFocus.x + viewportWidth / (2 * zoomScale);
  return {
    start: graphToTime(startX),
    end: graphToTime(endX)
  };
};

window.ReebViewerCommon.recenterCameraFromRangeBar = function(targetTime, opts) {
  const graphToTime = opts?.graphToTime;
  const maxTime = opts?.maxTime;
  const camera = opts?.camera;
  const viewportWidth = Number(opts?.viewportWidth ?? 0);
  const scheduleViewportUpdate = typeof opts?.scheduleViewportUpdate === "function"
    ? opts.scheduleViewportUpdate
    : (() => camera?.scheduleApply?.());

  return window.ReebViewerCommon.recenterViewportFromBarIndex(targetTime, {
    graphToTime,
    maxTime,
    visibleWindowFn: () => window.ReebViewerCommon.computeVisibleTimestepWindow({
      graphToTime,
      camera,
      viewportWidth
    }),
    getViewFocus: () => camera?.getViewFocus?.(),
    setViewFocus: nextFocus => camera?.setViewFocus?.(nextFocus),
    scheduleViewportUpdate
  });
};

window.ReebViewerCommon.createRangeBarController = function(opts) {
  const getState = typeof opts?.getState === "function" ? opts.getState : (() => ({}));
  const applyRangeAction = typeof opts?.applyRangeAction === "function" ? opts.applyRangeAction : null;
  const setViewportDrag = typeof opts?.setViewportDrag === "function" ? opts.setViewportDrag : null;
  const onRangeCommitted = typeof opts?.onRangeCommitted === "function" ? opts.onRangeCommitted : null;
  const onBarOnlyUpdate = typeof opts?.onBarOnlyUpdate === "function" ? opts.onBarOnlyUpdate : null;
  const onViewportRecenter = typeof opts?.onViewportRecenter === "function" ? opts.onViewportRecenter : null;
  const clickAction = opts?.clickAction || "recenter";

  const callBarOnly = () => {
    if (onBarOnlyUpdate) onBarOnlyUpdate();
  };
  const callCommitted = () => {
    if (onRangeCommitted) onRangeCommitted();
  };

  return {
    onRangeSelected(index) {
      if (!applyRangeAction) return;
      applyRangeAction({ type: "select", index });
      callCommitted();
    },
    onRangeDragStart(idx) {
      if (!applyRangeAction) return;
      applyRangeAction({ type: "drag-start", index: idx });
      callBarOnly();
    },
    onRangeDragMove(idx) {
      if (!applyRangeAction) return;
      applyRangeAction({ type: "drag-move", index: idx });
      callBarOnly();
    },
    onRangeDragEnd(idx) {
      if (!applyRangeAction) return;
      const start = getState()?.rangeDrag?.start;
      if (clickAction === "recenter" && Number.isFinite(+start) && +idx === +start) {
        applyRangeAction({ type: "drag-clear" });
        if (onViewportRecenter) onViewportRecenter(idx);
        callBarOnly();
        return;
      }

      applyRangeAction({ type: "drag-move", index: idx });
      const before = Array.isArray(getState()?.ranges) ? getState().ranges.length : 0;
      applyRangeAction({ type: "drag-commit" });
      const after = Array.isArray(getState()?.ranges) ? getState().ranges.length : 0;
      if (after !== before) {
        callCommitted();
      } else {
        callBarOnly();
      }
    },
    onViewportClick(idx) {
      if (onViewportRecenter) onViewportRecenter(idx);
    },
    onViewportDragStart() {
      if (setViewportDrag) setViewportDrag({ active: true });
      callBarOnly();
    },
    onViewportDragMove(idx) {
      if (onViewportRecenter) onViewportRecenter(idx);
    },
    onViewportDragEnd() {
      if (setViewportDrag) setViewportDrag(null);
      callBarOnly();
    }
  };
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

  const pointerToTime = event => {
    const [mx] = d3.pointer(event, svg.node());
    return Math.max(0, Math.min(timestepMax, x.invert(mx)));
  };
  const pointerToIndex = event => Math.round(pointerToTime(event));

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
    const x1 = x(range.end);
    g.append('rect')
      .attr('class', `range-selected${index === selectedRangeIndex ? ' selected' : ''}`)
      .attr('x', Math.min(x0, x1))
      .attr('y', rangeY)
      .attr('width', Math.max(4, Math.abs(x1 - x0)))
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
      const startCenter = (low + high) / 2;
      const grabOffset = startCenter - pointerToTime(event);
      if (typeof opts.onViewportDragStart === 'function') {
        opts.onViewportDragStart(pointerToIndex(event), event);
      }

      const onMove = moveEvent => {
        if (moveEvent.pointerId !== event.pointerId) return;
        if (typeof opts.onViewportDragMove === 'function') {
          opts.onViewportDragMove(pointerToTime(moveEvent) + grabOffset, moveEvent);
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
