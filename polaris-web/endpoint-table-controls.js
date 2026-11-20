(function () {
  document.addEventListener("input", event => {
    if (!event.target.matches("[data-endpoint-table-size]")) return;
    const controls = event.target.closest(".endpoint-table-controls");
    const main = controls.closest("main");
    const size = Number(event.target.value);
    main.style.setProperty("--endpoint-table-scale", size / 100);
    controls.querySelector("output").value = `${size}%`;
  });
  document.addEventListener("change", event => {
    if (!event.target.matches("[data-endpoint-table-wide]")) return;
    event.target.closest("main").classList.toggle("endpoint-table-wide", event.target.checked);
  });
})();
