(() => {
  const storageKey = (id) => `schema-layout:${id}`;
  const scrollKey = "modeling-lab:scroll-position";
  const saveScroll = () => sessionStorage.setItem(scrollKey, String(window.scrollY));
  const restoreScroll = () => {
    const saved = sessionStorage.getItem(scrollKey);
    if (saved === null) return;
    sessionStorage.removeItem(scrollKey);
    requestAnimationFrame(() => window.scrollTo(0, Number(saved)));
  };
  const pointInSvg = (svg, event) => {
    const point = svg.createSVGPoint();
    point.x = event.clientX; point.y = event.clientY;
    return point.matrixTransform(svg.getScreenCTM().inverse());
  };
  const nodePosition = (node) => {
    const match = /translate\(([-\d.]+)[ ,]([-\d.]+)\)/.exec(node.getAttribute("transform") || "");
    return match ? {x: Number(match[1]), y: Number(match[2])} : {x: 0, y: 0};
  };
  const setPosition = (node, pos) => node.setAttribute("transform", `translate(${pos.x} ${pos.y})`);
  const baseViewBox = (svg) => svg.dataset.baseViewbox.split(" ").map(Number);
  const applyViewBox = (svg, next) => {
    const [baseX, baseY, baseWidth, baseHeight] = baseViewBox(svg);
    const width = Math.min(baseWidth, Math.max(baseWidth / 2.25, next.width));
    const height = Math.min(baseHeight, Math.max(baseHeight / 2.25, next.height));
    const x = Math.max(baseX, Math.min(baseX + baseWidth - width, next.x));
    const y = Math.max(baseY, Math.min(baseY + baseHeight - height, next.y));
    svg.setAttribute("viewBox", `${x} ${y} ${width} ${height}`);
    const level = Math.round((baseWidth / width) * 100);
    svg.closest(".schema-diagram").querySelector("[data-diagram-zoom-level]").textContent = `${level}%`;
  };
  const zoom = (svg, multiplier) => {
    const box = svg.viewBox.baseVal;
    const width = box.width / multiplier, height = box.height / multiplier;
    applyViewBox(svg, {x: box.x + (box.width - width) / 2, y: box.y + (box.height - height) / 2, width, height});
  };
  const updateEdges = (svg) => {
    const nodes = Object.fromEntries([...svg.querySelectorAll(".diagram-node")].map((n) => [n.dataset.table, n]));
    const hw = Number(svg.dataset.boxWidth) / 2, hh = Number(svg.dataset.boxHeight) / 2;
    svg.querySelectorAll(".diagram-edges line").forEach((edge) => {
      const from = nodePosition(nodes[edge.dataset.from]), to = nodePosition(nodes[edge.dataset.to]);
      edge.setAttribute("x1", from.x + hw); edge.setAttribute("y1", from.y + hh);
      edge.setAttribute("x2", to.x + hw); edge.setAttribute("y2", to.y + hh);
    });
  };
  const save = (svg) => localStorage.setItem(storageKey(svg.dataset.diagramId), JSON.stringify(Object.fromEntries([...svg.querySelectorAll(".diagram-node")].map((n) => [n.dataset.table, nodePosition(n)]))));
  const restore = (svg) => {
    try {
      const positions = JSON.parse(localStorage.getItem(storageKey(svg.dataset.diagramId)) || "{}");
      svg.querySelectorAll(".diagram-node").forEach((node) => { if (positions[node.dataset.table]) setPosition(node, positions[node.dataset.table]); });
      updateEdges(svg);
    } catch (_) { localStorage.removeItem(storageKey(svg.dataset.diagramId)); }
  };
  const initialise = () => document.querySelectorAll(".schema-canvas").forEach((svg) => {
    if (svg.dataset.ready) return;
    svg.dataset.ready = "true"; restore(svg);
    let drag, pan;
    svg.addEventListener("pointerdown", (event) => {
      const node = event.target.closest(".diagram-node");
      if (!node) { pan = {point: pointInSvg(svg, event)}; svg.setPointerCapture(event.pointerId); return; }
      const point = pointInSvg(svg, event), pos = nodePosition(node);
      drag = {node, x: point.x - pos.x, y: point.y - pos.y}; svg.setPointerCapture(event.pointerId);
    });
    svg.addEventListener("pointermove", (event) => {
      if (!drag) return;
      const point = pointInSvg(svg, event), box = svg.viewBox.baseVal, w = Number(svg.dataset.boxWidth), h = Number(svg.dataset.boxHeight);
      setPosition(drag.node, {x: Math.max(0, Math.min(box.width - w, point.x - drag.x)), y: Math.max(30, Math.min(box.height - h, point.y - drag.y))});
      updateEdges(svg);
    });
    svg.addEventListener("pointermove", (event) => {
      if (!pan) return;
      const point = pointInSvg(svg, event), box = svg.viewBox.baseVal;
      applyViewBox(svg, {x: box.x - (point.x - pan.point.x), y: box.y - (point.y - pan.point.y), width: box.width, height: box.height});
      pan.point = pointInSvg(svg, event);
    });
    svg.addEventListener("pointerup", () => { if (drag) save(svg); drag = undefined; pan = undefined; });
    svg.addEventListener("pointercancel", () => { drag = undefined; pan = undefined; });
  });
  document.addEventListener("click", (event) => {
    const zoomButton = event.target.closest("[data-diagram-zoom]");
    if (zoomButton) {
      const diagram = zoomButton.closest(".schema-diagram");
      zoom(diagram.querySelector(".schema-canvas"), zoomButton.dataset.diagramZoom === "in" ? 1.25 : 0.8);
      return;
    }
    const button = event.target.closest("[data-diagram-reset]"); if (!button) return;
    localStorage.removeItem(storageKey(button.dataset.diagramReset)); saveScroll(); location.reload();
  });
  document.addEventListener("htmx:beforeRequest", saveScroll);
  document.addEventListener("htmx:afterSettle", restoreScroll);
  document.addEventListener("DOMContentLoaded", initialise);
  document.addEventListener("DOMContentLoaded", restoreScroll);
  document.addEventListener("htmx:load", initialise);
})();
