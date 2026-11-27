document.querySelectorAll("[data-submit-once]").forEach(function (button) {
  const form = button.form;
  if (!form) return;

  form.addEventListener("submit", function (event) {
    form.querySelectorAll("input[data-submit-once-marker]").forEach(function (stale) {
      stale.remove();
    });
    if (event.submitter && event.submitter !== button) return;
    const name = button.getAttribute("name");
    if (name) {
      const marker = document.createElement("input");
      marker.type = "hidden";
      marker.name = name;
      marker.value = button.value;
      marker.dataset.submitOnceMarker = "true";
      form.appendChild(marker);
    }
    button.disabled = true;
    button.textContent = "Importing…";
  });
});
