(function () {
  const KEY = "omron-scroll:" + window.location.pathname;

  document.addEventListener("click", function (event) {
    const target = event.target;
    if (!target || !target.closest) return;
    const link = target.closest("a[href]");
    if (!link) return;
    const url = new URL(link.getAttribute("href"), window.location.href);
    if (url.pathname === window.location.pathname) {
      window.sessionStorage.setItem(KEY, String(window.scrollY));
    }
  });

  function restore() {
    const saved = window.sessionStorage.getItem(KEY);
    if (saved === null) return;
    window.sessionStorage.removeItem(KEY);
    window.scrollTo(0, Number(saved) || 0);
  }

  document.addEventListener("DOMContentLoaded", restore);
  window.addEventListener("pageshow", restore);

  const popover = document.getElementById("custom-range-popover");
  const popoverButton = document.querySelector('button[popovertarget="custom-range-popover"]');
  if (popover && popoverButton) {
    popover.addEventListener("toggle", function (event) {
      popoverButton.setAttribute("aria-expanded", String(event.newState === "open"));
    });
  }
})();
