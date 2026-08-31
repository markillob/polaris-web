(function () {
  const storageKey = "polaris-theme";
  const configPath = "/api/config";

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

    const host = document.querySelector("[data-theme-toggle-host]");
    const topbar = document.querySelector(".topbar");
    if ((!host && !topbar) || document.querySelector("[data-theme-toggle]")) {
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

    const nav = topbar?.querySelector(".nav-actions");
    if (host) {
      host.append(button);
    } else if (nav) {
      nav.append(button);
    } else {
      topbar.append(button);
    }

    applyTheme(document.body.dataset.theme || preferredTheme());
  }

  function configuredSiteName(config) {
    const value = config?.site_name?.main_site || config?.main_site || "polaris";
    return String(value).trim() || "polaris";
  }

  function replaceLeadingSiteName(value, siteName) {
    return String(value || "").replace(/^Polaris\b/i, siteName);
  }

  async function applyConfiguredSiteName() {
    try {
      const response = await fetch(configPath, { cache: "no-store" });
      if (!response.ok) {
        return;
      }

      const config = await response.json();
      const siteName = configuredSiteName(config);
      document.querySelectorAll(".brand").forEach((element) => {
        element.textContent = siteName;
      });

      document.title = replaceLeadingSiteName(document.title, siteName);

      const pageTitle = document.getElementById("page-title");
      if (pageTitle && pageTitle.textContent.trim().toLowerCase() === "polaris") {
        pageTitle.textContent = siteName;
      }

      document.querySelectorAll(".eyebrow").forEach((element) => {
        element.textContent = replaceLeadingSiteName(element.textContent, siteName);
      });
    } catch (error) {
      console.error("Unable to load site config", error);
    }
  }

  function boot() {
    installToggle();
    applyConfiguredSiteName();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
}());
