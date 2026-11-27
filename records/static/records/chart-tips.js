(function () {
  const PADDING = 6;
  const GAP = 8;

  function titleOf(shape) {
    const title = shape.querySelector("title");
    return title ? title.textContent : "";
  }

  function show(svg, shape, text) {
    const tip = svg.querySelector(".chart-tip");
    if (!tip) return;
    const box = tip.querySelector(".chart-tip-box");
    const label = tip.querySelector(".chart-tip-text");
    label.textContent = text;
    const textBox = label.getBBox();
    const width = textBox.width + PADDING * 2;
    const height = textBox.height + PADDING * 2;
    box.setAttribute("width", width);
    box.setAttribute("height", height);
    const view = svg.getAttribute("viewBox").split(/\s+/).map(Number);
    const shapeBox = shape.getBBox();
    let x = shapeBox.x + shapeBox.width / 2 - width / 2;
    x = Math.max(2, Math.min(x, view[2] - width - 2));
    let y = shapeBox.y - height - GAP;
    if (y < 2) y = shapeBox.y + shapeBox.height + GAP;
    box.setAttribute("x", x);
    box.setAttribute("y", y);
    label.setAttribute("x", x + PADDING);
    label.setAttribute("y", y + height / 2);
    tip.setAttribute("visibility", "visible");
  }

  function hide(svg) {
    const tip = svg.querySelector(".chart-tip");
    if (tip) tip.setAttribute("visibility", "hidden");
  }

  document.querySelectorAll("svg.chart").forEach(function (svg) {
    svg.addEventListener("mouseover", function (event) {
      if (!event.target.closest) return;
      const shape = event.target.closest(".chart-dot, .chart-bar, .chart-line");
      if (!shape || !svg.contains(shape)) return;
      const text = titleOf(shape);
      if (text) show(svg, shape, text);
    });
    svg.addEventListener("mouseout", function (event) {
      if (!svg.contains(event.relatedTarget)) hide(svg);
    });
    svg.addEventListener("focusin", function (event) {
      if (!event.target.closest) return;
      const shape = event.target.closest(".chart-dot, .chart-bar");
      if (!shape || !svg.contains(shape)) return;
      const text = titleOf(shape);
      if (text) show(svg, shape, text);
    });
    svg.addEventListener("focusout", function (event) {
      if (!svg.contains(event.relatedTarget)) hide(svg);
    });
  });
})();
