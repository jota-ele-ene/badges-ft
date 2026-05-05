(function () {
  const PUBLIC_PATHS = window.PUBLIC_PATHS || ["/", "/login"];
  const PUBLIC_PREFIXES = window.PUBLIC_PREFIXES || ["/static"];
  const LOGIN_PATH = "/login";
  const SESSION_QUERY_PARAM = window.SESSION_QUERY_PARAM || "session_id";
  const currentPath = window.location.pathname;

  function isPublicPath(path) {
    console.log('>>> isPublicPath():', path);
    console.log('>>> PUBLIC_PATHS.includes(path):', PUBLIC_PATHS.includes(path));
    if (PUBLIC_PATHS.includes(path)) return true;
    return PUBLIC_PREFIXES.some((prefix) => path.startsWith(prefix));
  }

  function getQuerySessionId() {
    const url = new URL(window.location.href);
    return url.searchParams.get(SESSION_QUERY_PARAM) || "";
  }

  function getQueryCode() {
    const url = new URL(window.location.href);
    return url.searchParams.get("code") || "";
  }

  function redirectToLogin() {
    if (currentPath !== LOGIN_PATH) {
      const next = encodeURIComponent(window.location.pathname + window.location.search);
      window.location.replace("/login?next=" + next);
    }
  }

  function appendSessionToUrl(urlString, sessionId, code) {
    const url = new URL(urlString, window.location.origin);
    if (!url.searchParams.get(SESSION_QUERY_PARAM)) {
      url.searchParams.set(SESSION_QUERY_PARAM, sessionId);
    }
    if (!url.searchParams.get("code")) {
      url.searchParams.set("code", code);
    }
    return url.pathname + url.search + url.hash;
  }

  function propagateSession(sessionId, code) {
    document.querySelectorAll('a[href^="/"]').forEach(a => {
      const href = a.getAttribute("href");
      if (!href || href.startsWith("/login")) return;
      a.setAttribute("href", appendSessionToUrl(href, sessionId, code));
    });

    document.querySelectorAll('form[action^="/"]').forEach(form => {
      let sessionInput = form.querySelector(`input[name="${SESSION_QUERY_PARAM}"]`);
      let codeInput = form.querySelector(`input[name="code"]`);
      
      if (!sessionInput) {
        sessionInput = document.createElement("input");
        sessionInput.type = "hidden";
        sessionInput.name = SESSION_QUERY_PARAM;
        form.appendChild(sessionInput);
      }
      if (!codeInput) {
        codeInput = document.createElement("input");
        codeInput.type = "hidden";
        codeInput.name = "code";
        form.appendChild(codeInput);
      }
      
      sessionInput.value = sessionId;
      codeInput.value = code;
    });

    const originalFetch = window.fetch.bind(window);
    window.fetch = function (resource, options) {
      try {
        if (typeof resource === "string" && resource.startsWith("/")) {
          resource = appendSessionToUrl(resource, sessionId, code);
        } else if (resource instanceof Request && resource.url.startsWith(window.location.origin + "/")) {
          const newUrl = appendSessionToUrl(resource.url, sessionId, code);
          resource = new Request(newUrl, resource);
        }
      } catch (e) {}
      return originalFetch(resource, options);
    };
  }

  function run() {
    if (isPublicPath(currentPath)) return;

    const sessionId = getQuerySessionId() || window.APP_SESSION_ID || "";
    const code = getQueryCode() || window.APP_SESSION_CODE || "";
    
    if (!sessionId || !code) {
      redirectToLogin();
      return;
    }

    propagateSession(sessionId, code);
  }

  run();
})();