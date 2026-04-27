(function () {
  const PUBLIC_PATHS = new Set(["/", "/login", "/verify-id", "/verify-png"]);
  const LOGIN_PATH = "/login";
  const TOKEN_PARAM = window.APP_TOKEN_PARAM || "token";
  const currentPath = window.location.pathname;

  function isPublicPath(path) {
    return PUBLIC_PATHS.has(path);
  }

  function getQueryToken() {
    const url = new URL(window.location.href);
    return url.searchParams.get(TOKEN_PARAM) || "";
  }

  function redirectToLogin() {
    if (currentPath !== LOGIN_PATH) {
      const next = encodeURIComponent(window.location.pathname + window.location.search);
      window.location.replace("/login?next=" + next);
    }
  }

  function appendTokenToUrl(urlString, token) {
    const url = new URL(urlString, window.location.origin);
    if (!url.searchParams.get(TOKEN_PARAM)) {
      url.searchParams.set(TOKEN_PARAM, token);
    }
    return url.pathname + url.search + url.hash;
  }

  function propagateToken(token) {
    document.querySelectorAll('a[href^="/"]').forEach(a => {
      const href = a.getAttribute("href");
      if (!href || href.startsWith("/login")) return;
      a.setAttribute("href", appendTokenToUrl(href, token));
    });

    document.querySelectorAll('form[action^="/"]').forEach(form => {
      let hidden = form.querySelector(`input[name="${TOKEN_PARAM}"]`);
      if (!hidden) {
        hidden = document.createElement("input");
        hidden.type = "hidden";
        hidden.name = TOKEN_PARAM;
        form.appendChild(hidden);
      }
      hidden.value = token;
    });

    const originalFetch = window.fetch.bind(window);
    window.fetch = function (resource, options) {
      try {
        if (typeof resource === "string" && resource.startsWith("/")) {
          resource = appendTokenToUrl(resource, token);
        } else if (resource instanceof Request && resource.url.startsWith(window.location.origin + "/")) {
          const newUrl = appendTokenToUrl(resource.url, token);
          resource = new Request(newUrl, resource);
        }
      } catch (e) {}
      return originalFetch(resource, options);
    };
  }

  function run() {
    if (isPublicPath(currentPath)) return;

    const token = getQueryToken() || window.APP_JWT || "";
    if (!token) {
      redirectToLogin();
      return;
    }

    propagateToken(token);
  }

  run();
})();