(function () {
  const storageKey = "polaris-theme";

  function preferredTheme() {
    const stored = localStorage.getItem(storageKey);
    if (stored === "dark" || stored === "light") {
      return stored;
    }

    return "light";
  }

  function applyTheme(theme) {
    document.body.dataset.theme = theme;
    const button = document.querySelector("[data-theme-toggle]");
    if (button) {
      button.textContent = theme === "dark" ? "light background" : "dark background";
      button.setAttribute("aria-pressed", String(theme === "dark"));
    }
  }

  function installToggle() {
    applyTheme(preferredTheme());

    const topbar = document.querySelector(".topbar");
    if (!topbar || document.querySelector("[data-theme-toggle]")) {
      return;
    }

    const button = document.createElement("button");
    button.className = "theme-toggle";
    button.type = "button";
    button.dataset.themeToggle = "true";
    button.addEventListener("click", () => {
      const nextTheme = document.body.dataset.theme === "dark" ? "light" : "dark";
      localStorage.setItem(storageKey, nextTheme);
      applyTheme(nextTheme);
    });

    const nav = topbar.querySelector(".nav-actions");
    if (nav) {
      nav.append(button);
    } else {
      topbar.append(button);
    }

    applyTheme(document.body.dataset.theme || preferredTheme());
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", installToggle);
  } else {
    installToggle();
  }
}());
